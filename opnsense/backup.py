#!/usr/bin/env python3
"""
OPNsense Config Backup Script
- Holt Konfigurations-XMLs von 5 OPNsense-Instanzen per API
- Sichert sie lokal per Restic (--tag opnsense), getrennte Retention
  von den Samba-Snapshots
- Vollstaendig eigenstaendig, keine Abhaengigkeit zu anderen lokalen Dateien
"""

import os
import subprocess
import logging
import smtplib
from email.mime.text import MIMEText
from datetime import datetime
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

RESTIC_REPO = "/backup/files/restic/firewalls"
RESTIC_PASSWORD_FILE = "/etc/restic/firewalls-pwd.txt"

CONFIG_STAGING = Path("../../opnsense-backup")
#CONFIG_STAGING = Path("/tmp/opnsense-backup")
#ENV_FILE = "/etc/restic/firewalls.env"
ENV_FILE = "../../firewalls.env"
LOG_FILE = "../../firewalls-backup.log"

RESTIC_HOST_LABEL = "firewalls"
RESTIC_TAG = "opnsense"

# Firewalls: Name -> Zugangsdaten kommen aus der .env
# Format dort: FW_<NAME>_IP, FW_<NAME>_KEY, FW_<NAME>_SECRET
FIREWALLS = ["GS10_01", "GS10_02", "GS10_03", "GS40_01", "GS70_01"]

NOTIFY_MAIL_TO = "admin@firma.de"
NOTIFY_MAIL_FROM = "backup@firma.de"
SMTP_HOST = "localhost"  # lokaler MTA (z.B. Postfix), ggf. anpassen

RETENTION = {
    "keep-daily": "7",      # last 7 days, keep 1 per day
    "keep-weekly": "4",     # last 4 weeks, keep 1 per week
    "keep-monthly": "12",   # last 12 months, keep 1 per month
    "keep-yearly": "2",     # last 2 years, keep 1 per year
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("opnsense_backup")


def log_and_print(msg: str, level: str = "info") -> None:
    print(msg)
    getattr(logger, level)(msg)


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def load_env_file(path: str) -> dict:
    """Laedt simple KEY=VALUE Zeilen aus einer .env Datei in ein dict."""
    env = {}
    env_path = Path(path)
    if not env_path.exists():
        log_and_print(f"WARNUNG: env-Datei {path} nicht gefunden", "warning")
        return env

    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def send_mail(subject: str, body: str) -> None:
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = NOTIFY_MAIL_FROM
        msg["To"] = NOTIFY_MAIL_TO

        with smtplib.SMTP(SMTP_HOST) as smtp:
            smtp.send_message(msg)
    except Exception as exc:
        log_and_print(f"Mail konnte nicht versendet werden: {exc}", "error")


def run_restic(args: list, capture: bool = True) -> subprocess.CompletedProcess:
    cmd = [
        "restic",
        "--repo", RESTIC_REPO,
        "--password-file", RESTIC_PASSWORD_FILE,
    ] + args

    log_and_print(f"Restic Befehl: {' '.join(cmd)}")
    return subprocess.run(cmd, capture_output=capture, text=True)


def apply_retention(tag: str) -> None:
    """Fuehrt forget --prune nur fuer Snapshots mit dem angegebenen Tag aus."""
    args = ["forget", "--prune", "--tag", tag]
    for flag, value in RETENTION.items():
        args += [f"--{flag}", value]

    result = run_restic(args)

    if result.stdout:
        logger.info(result.stdout)
    if result.returncode != 0:
        log_and_print("Retention/Prune FEHLGESCHLAGEN", "error")
        send_mail(
            "⚠️ OPNsense Retention Fehler",
            f"Forget/Prune (OPNsense) ist fehlgeschlagen.\n\n{result.stderr}",
        )


# ---------------------------------------------------------------------------
# Config Download
# ---------------------------------------------------------------------------

def download_configs() -> int:
    """Laedt Configs aller Firewalls herunter. Gibt Anzahl der Fehler zurueck."""
    env = load_env_file(ENV_FILE)
    CONFIG_STAGING.mkdir(parents=True, exist_ok=True)

    errors = 0

    for name in FIREWALLS:
        ip = env.get(f"FW_{name}_IP")
        key = env.get(f"FW_{name}_KEY")
        secret = env.get(f"FW_{name}_SECRET")

        if not all([ip, key, secret]):
            log_and_print(f"FEHLER: Zugangsdaten fuer {name} unvollstaendig", "error")
            errors += 1
            continue

        log_and_print(f"Hole Config von {name} ({ip})...")

        try:
            response = requests.get(
                f"https://{ip}/api/core/backup/download/this",
                auth=HTTPBasicAuth(key, secret),
                verify=False,   # selbstsigniertes Zertifikat
                timeout=30,
            )
        except requests.RequestException as exc:
            log_and_print(f"FEHLER: {name} nicht erreichbar ({exc})", "error")
            errors += 1
            continue

        if response.status_code != 200:
            log_and_print(f"FEHLER: {name} HTTP {response.status_code}", "error")
            errors += 1
            continue

        # Sicherstellen, dass tatsaechlich XML zurueckkam und kein Fehlertext/JSON
        content = response.content
        if not content.lstrip().startswith(b"<?xml"):
            log_and_print(
                f"FEHLER: {name} Antwort ist kein XML (evtl. Fehlertext)", "error"
            )
            errors += 1
            continue

        target_file = CONFIG_STAGING / f"{name}-config.xml"
        target_file.write_bytes(content)
        # mtime explizit aktualisieren, damit Restic die Datei als geaendert erkennt,
        # selbst wenn der Inhalt identisch zum letzten Lauf ist
        os.utime(target_file, None)

        log_and_print(f"OK: {name}")

    if errors:
        send_mail(
            "⚠️ OPNsense Backup Fehler",
            f"{errors} von {len(FIREWALLS)} Firewalls konnten nicht gesichert werden. "
            f"Details im Log: {LOG_FILE}",
        )

    return errors


# ---------------------------------------------------------------------------
# Restic Backup
# ---------------------------------------------------------------------------

def backup_configs() -> bool:
    args = [
        "backup",
        "--host", RESTIC_HOST_LABEL,
        "--tag", RESTIC_TAG,
        "--verbose",
        "--dry-run",    # TODO remove
        str(CONFIG_STAGING),
    ]

    result = run_restic(args)

    if result.stdout:
        logger.info(result.stdout)
    if result.stderr:
        logger.error(result.stderr)

    if result.returncode != 0:
        log_and_print("OPNsense Restic Backup FEHLGESCHLAGEN", "error")
        send_mail(
            "⚠️ OPNsense Restic Backup Fehler",
            f"Restic backup (OPNsense) ist fehlgeschlagen.\n\n{result.stderr}",
        )
        return False

    log_and_print("OPNsense Backup erfolgreich.")
    return True


def cleanup_staging() -> None:
    for f in CONFIG_STAGING.glob("*.xml"):
        f.unlink()


def main() -> None:
    start = datetime.now()
    log_and_print(f"=== OPNsense Backup Start {start:%Y-%m-%d %H:%M:%S} ===")

    download_configs()
    backup_ok = backup_configs()

    if backup_ok:
        apply_retention(RESTIC_TAG)

    cleanup_staging()

    end = datetime.now()
    log_and_print(
        f"=== OPNsense Backup Ende {end:%Y-%m-%d %H:%M:%S} (Dauer: {end - start}) ==="
    )


if __name__ == "__main__":
    main()
