#!/usr/bin/env python3
"""
OPNsense Config Backup Script
- Liest Konfiguration aus /config/firewalls.cfg (nicht im Git-Repo!)
- Holt Konfigurations-XMLs von OPNsense-Instanzen per API
- Sichert sie lokal per Restic (--tag opnsense)
- Versendet Mail bei Erfolg und bei Fehler
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
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Konfiguration laden
# ---------------------------------------------------------------------------

CONFIG_FILE = Path(__file__).parent.parent / "config" / "firewalls.cfg"


def load_cfg(path) -> dict:
    cfg = {}
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Konfigurationsdatei nicht gefunden: {path}")
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


cfg = load_cfg(CONFIG_FILE)

RESTIC_REPO          = cfg["RESTIC_REPO"]
RESTIC_PASSWORD_FILE = cfg["RESTIC_PASSWORD_FILE"]
RESTIC_HOST_LABEL    = cfg.get("RESTIC_HOST_LABEL", "firma-opnsense")
RESTIC_TAG           = cfg.get("RESTIC_TAG", "opnsense")
LOG_FILE             = cfg.get("LOG_FILE", "/var/log/opnsense-backup.log")
NOTIFY_MAIL_TO       = cfg["NOTIFY_MAIL_TO"]
NOTIFY_MAIL_FROM     = cfg["NOTIFY_MAIL_FROM"]
SMTP_HOST            = cfg.get("SMTP_HOST", "localhost")

RETENTION = {
    "keep-daily":   cfg.get("KEEP_DAILY",   "7"),
    "keep-weekly":  cfg.get("KEEP_WEEKLY",  "4"),
    "keep-monthly": cfg.get("KEEP_MONTHLY", "12"),
    "keep-yearly":  cfg.get("KEEP_YEARLY",  "2"),
}

# Firewall-Namen aus Konfiguration ermitteln (alle FRW_*_IP Eintraege)
FIREWALLS = sorted(set(
    k[4:-3] for k in cfg if k.startswith("FRW_") and k.endswith("_IP")
))

CONFIG_STAGING = Path(__file__).parent.parent / "tmp" / "firewalls"

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


def apply_retention(tag: str) -> bool:
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
        return False
    return True


# ---------------------------------------------------------------------------
# Config Download
# ---------------------------------------------------------------------------

def download_configs() -> tuple[int, list[str]]:
    CONFIG_STAGING.mkdir(parents=True, exist_ok=True)
    errors = 0
    successful = []

    for name in FIREWALLS:
        ip     = cfg.get(f"FRW_{name}_IP")
        key    = cfg.get(f"FRW_{name}_KEY")
        secret = cfg.get(f"FRW_{name}_SECRET")

        if not all([ip, key, secret]):
            log_and_print(f"FEHLER: Zugangsdaten fuer {name} unvollstaendig", "error")
            errors += 1
            continue

        log_and_print(f"Hole Config von {name} ({ip})...")

        try:
            response = requests.get(
                f"https://{ip}/api/core/backup/download/this",
                auth=HTTPBasicAuth(key, secret),
                verify=False,
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

        content = response.content
        if not content.lstrip().startswith(b"<?xml"):
            log_and_print(f"FEHLER: {name} Antwort ist kein XML", "error")
            errors += 1
            continue

        target_file = CONFIG_STAGING / f"{name}-config.xml"
        target_file.write_bytes(content)
        os.utime(target_file, None)
        log_and_print(f"OK: {name}")
        successful.append(name)

    if errors:
        send_mail(
            "⚠️ OPNsense Backup Fehler",
            f"{errors} von {len(FIREWALLS)} Firewalls konnten nicht gesichert werden.\n"
            f"Details im Log: {LOG_FILE}",
        )

    return errors, successful


# ---------------------------------------------------------------------------
# Restic Backup
# ---------------------------------------------------------------------------

def backup_configs() -> bool:
    args = [
        "backup",
        "--host", RESTIC_HOST_LABEL,
        "--tag", RESTIC_TAG,
        "--verbose",
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

    if not FIREWALLS:
        log_and_print(
            f"FEHLER: Keine Firewalls in {CONFIG_FILE} gefunden "
            "(erwartet: FRW_<NAME>_IP / FRW_<NAME>_KEY / FRW_<NAME>_SECRET) – Backup abgebrochen.",
            "error",
        )
        send_mail(
            "⚠️ OPNsense Backup Fehler",
            f"Keine Firewalls in {CONFIG_FILE} gefunden.\n"
            "Erwartetes Format: FRW_<NAME>_IP / FRW_<NAME>_KEY / FRW_<NAME>_SECRET\n"
            "Backup wurde abgebrochen, es wurde kein Snapshot erstellt.",
        )
        return

    download_errors, successful_firewalls = download_configs()
    backup_ok = backup_configs()

    retention_ok = True
    if backup_ok:
        retention_ok = apply_retention(RESTIC_TAG)

    cleanup_staging()

    end = datetime.now()
    duration = end - start
    log_and_print(
        f"=== OPNsense Backup Ende {end:%Y-%m-%d %H:%M:%S} (Dauer: {duration}) ==="
    )

    if download_errors == 0 and backup_ok and retention_ok:
        send_mail(
            "✅ OPNsense Backup erfolgreich",
            (
                f"OPNsense Backup erfolgreich abgeschlossen.\n\n"
                f"Start:    {start:%Y-%m-%d %H:%M:%S}\n"
                f"Ende:     {end:%Y-%m-%d %H:%M:%S}\n"
                f"Dauer:    {duration}\n"
                f"Firewalls gesichert ({len(successful_firewalls)}/{len(FIREWALLS)}):\n"
                f"  - " + "\n  - ".join(successful_firewalls) + "\n\n"
                f"Log: {LOG_FILE}"
            ),
        )


if __name__ == "__main__":
    main()
