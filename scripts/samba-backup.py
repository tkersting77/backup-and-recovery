#!/usr/bin/env python3
"""
Samba Backup Script
- Liest Konfiguration aus /etc/restic/samba.env (nicht im Git-Repo!)
- Sichert Samba-Freigaben per Restic Pull via SFTP
- Snapshots werden mit --tag samba versehen
- Versendet Mail bei Erfolg und bei Fehler
"""

import argparse
import subprocess
import logging
import smtplib
from email.mime.text import MIMEText
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Konfiguration aus .env laden
# ---------------------------------------------------------------------------

ENV_FILE = "/config/samba.cfg"


def load_env_file(path: str) -> dict:
    """Laedt simple KEY=VALUE Zeilen aus einer .env Datei in ein dict."""
    env = {}
    env_path = Path(path)
    if not env_path.exists():
        raise FileNotFoundError(f"env-Datei nicht gefunden: {path}")

    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


env = load_env_file(ENV_FILE)

RESTIC_REPO          = env["RESTIC_REPO"]
RESTIC_PASSWORD_FILE = env["RESTIC_PASSWORD_FILE"]
SAMBA_HOST           = env["SAMBA_HOST"]
SAMBA_PATHS          = [p.strip() for p in env["SAMBA_PATHS"].split(",")]
RESTIC_HOST_LABEL    = env.get("RESTIC_HOST_LABEL", "firma-samba")
RESTIC_TAG           = env.get("RESTIC_TAG", "samba")
LOG_FILE             = env.get("LOG_FILE", "/var/log/samba-backup.log")
NOTIFY_MAIL_TO       = env["NOTIFY_MAIL_TO"]
NOTIFY_MAIL_FROM     = env["NOTIFY_MAIL_FROM"]
SMTP_HOST            = env.get("SMTP_HOST", "localhost")

RETENTION = {
    "keep-daily":   env.get("KEEP_DAILY",   "7"),
    "keep-weekly":  env.get("KEEP_WEEKLY",  "4"),
    "keep-monthly": env.get("KEEP_MONTHLY", "12"),
    "keep-yearly":  env.get("KEEP_YEARLY",  "2"),
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("samba_backup")


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
            "⚠️ Samba Retention Fehler",
            f"Forget/Prune (Samba) ist fehlgeschlagen.\n\n{result.stderr}",
        )
        return False

    return True


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def backup_samba(dry_run: bool = False) -> bool:
    sftp_targets = [f"sftp:{SAMBA_HOST}:{path}" for path in SAMBA_PATHS]

    args = [
        "backup",
        "--host", RESTIC_HOST_LABEL,
        "--tag", RESTIC_TAG,
        "--verbose",
    ] + sftp_targets

    if dry_run:
        args.append("--dry-run")
        log_and_print("DRY-RUN Modus – es werden keine Daten geschrieben")

    result = run_restic(args)

    if result.stdout:
        logger.info(result.stdout)
    if result.stderr:
        logger.error(result.stderr)

    if result.returncode != 0:
        log_and_print("Samba Restic Backup FEHLGESCHLAGEN", "error")
        if not dry_run:
            send_mail(
                "⚠️ Samba Backup Fehler",
                f"Restic backup (Samba) ist fehlgeschlagen.\n\n{result.stderr}",
            )
        return False

    log_and_print("Samba Backup erfolgreich." if not dry_run else "DRY-RUN abgeschlossen – kein Snapshot erstellt.")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Samba Backup via Restic")
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Testlauf – zeigt was gesichert wuerde, schreibt nichts, kein Snapshot, keine Mail"
    )
    args = parser.parse_args()

    start = datetime.now()
    log_and_print(f"=== Samba Backup Start {start:%Y-%m-%d %H:%M:%S} ===")

    backup_ok = backup_samba(dry_run=args.dry_run)

    # Bei Dry-Run keine Retention und keine Mail
    retention_ok = True
    if backup_ok and not args.dry_run:
        retention_ok = apply_retention(RESTIC_TAG)

    end = datetime.now()
    duration = end - start
    log_and_print(
        f"=== Samba Backup Ende {end:%Y-%m-%d %H:%M:%S} (Dauer: {duration}) ==="
    )

    if backup_ok and retention_ok and not args.dry_run:
        send_mail(
            "✅ Samba Backup erfolgreich",
            (
                f"Samba Backup erfolgreich abgeschlossen.\n\n"
                f"Start:    {start:%Y-%m-%d %H:%M:%S}\n"
                f"Ende:     {end:%Y-%m-%d %H:%M:%S}\n"
                f"Dauer:    {duration}\n"
                f"Pfade:    {', '.join(SAMBA_PATHS)}\n\n"
                f"Log: {LOG_FILE}"
            ),
        )


if __name__ == "__main__":
    main()