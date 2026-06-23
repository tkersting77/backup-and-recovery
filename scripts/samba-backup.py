#!/usr/bin/env python3
"""
Samba Backup Script
- Sichert Samba-Freigaben (users, groups) per Restic Pull via SFTP
- Snapshots werden mit --tag samba versehen, damit Retention getrennt
  von den OPNsense-Snapshots laufen kann
- Vollstaendig eigenstaendig, keine Abhaengigkeit zu anderen lokalen Dateien
"""

import subprocess
import logging
import smtplib
from email.mime.text import MIMEText
from datetime import datetime

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

RESTIC_REPO = "/backup/files/restic/firma"
RESTIC_PASSWORD_FILE = "/etc/restic/password.txt"

SAMBA_HOST = "samba-user@10.8.0.x"          # VPN-IP des Samba-Servers anpassen
SAMBA_PATHS = [
    "/srv/samba/users",
    "/srv/samba/groups",
]
RESTIC_HOST_LABEL = "firma-samba"            # Label im Snapshot, --host
RESTIC_TAG = "samba"                         # Tag zur Trennung von OPNsense-Snapshots

LOG_FILE = "/var/log/samba-backup.log"

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
            "⚠️ Samba Retention Fehler",
            f"Forget/Prune (Samba) ist fehlgeschlagen.\n\n{result.stderr}",
        )


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def backup_samba() -> bool:
    sftp_targets = [f"sftp:{SAMBA_HOST}:{path}" for path in SAMBA_PATHS]

    args = [
        "backup",
        "--host", RESTIC_HOST_LABEL,
        "--tag", RESTIC_TAG,
        "--verbose",
    ] + sftp_targets

    result = run_restic(args)

    if result.stdout:
        logger.info(result.stdout)
    if result.stderr:
        logger.error(result.stderr)

    if result.returncode != 0:
        log_and_print("Samba Restic Backup FEHLGESCHLAGEN", "error")
        send_mail(
            "⚠️ Samba Backup Fehler",
            f"Restic backup (Samba) ist fehlgeschlagen.\n\n{result.stderr}",
        )
        return False

    log_and_print("Samba Backup erfolgreich.")
    return True


def main() -> None:
    start = datetime.now()
    log_and_print(f"=== Samba Backup Start {start:%Y-%m-%d %H:%M:%S} ===")

    if backup_samba():
        apply_retention(RESTIC_TAG)

    end = datetime.now()
    log_and_print(
        f"=== Samba Backup Ende {end:%Y-%m-%d %H:%M:%S} (Dauer: {end - start}) ==="
    )


if __name__ == "__main__":
    main()
