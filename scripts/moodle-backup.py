#!/usr/bin/env python3
"""
Moodle Backup Script
- Liest Konfiguration aus config/moodle.cfg (nicht im Git-Repo!)
- Unterstuetzt beliebig viele Moodle-Server (SRV_<NAME>_HOST/_PATHS Bloecke)
- Spiegelt pro Server moodledata 1:1 per rsync ueber SSH (SRV_<NAME>_PATHS,
  inkl. geloeschter Dateien, Zeitstempel, Rechte)
- Holt aus dem SQL-Dump-Verzeichnis (SRV_<NAME>_LATEST_PATHS) bewusst NUR die
  jeweils neueste Datei, nicht das komplette (taeglich wachsende) Verzeichnis -
  die Versionsgeschichte alter Dumps liefert bereits restic selbst ueber seine
  eigene Snapshot-Retention, ein zusaetzliches Mitfuehren alter Dump-
  Generationen wuerde die (oft grossen) Dumps mehrfach redundant einlagern
- Beide Arten von Pfaden landen zusammen in EINEM gemeinsamen Restic-Snapshot
  pro Server
- Sichert jeden Server als eigenen Restic-Snapshot (--host <servername>,
  --tag moodle) und wendet die Retention pro Server einzeln an
- Verhindert ueberlappende Laeufe per Lockfile
- Versendet EINE aggregierte Mail am Ende (Erfolg oder Liste der Fehler)
"""

import argparse
import fcntl
import shlex
import socket
import subprocess
import logging
import smtplib
import sys
from email.mime.text import MIMEText
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Konfiguration laden
# ---------------------------------------------------------------------------

CONFIG_FILE = Path(__file__).parent.parent / "config" / "moodle.cfg"
STAGING_DIR = Path(__file__).parent.parent / "tmp" / "moodle"
LOCK_FILE   = Path(__file__).parent.parent / "tmp" / "moodle-backup.lock"


def load_cfg(path) -> dict:
    cfg = {}
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Konfigurationsdatei nicht gefunden: {path}")
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        cfg[key.strip()] = value.strip().strip('"').strip("'")
    return cfg


cfg = load_cfg(CONFIG_FILE)

RESTIC_REPO          = cfg["RESTIC_REPO"]
RESTIC_PASSWORD_FILE = cfg["RESTIC_PASSWORD_FILE"]
RESTIC_TAG           = cfg.get("RESTIC_TAG", "moodle")
SSH_KEY_FILE_DEFAULT = cfg.get("SSH_KEY_FILE", "")
SSH_PORT_DEFAULT     = cfg.get("SSH_PORT", "22")
LOG_FILE             = cfg.get("LOG_FILE", "/var/log/moodle-backup.log")
NOTIFY_MAIL_TO       = cfg["NOTIFY_MAIL_TO"]
NOTIFY_MAIL_FROM     = cfg["NOTIFY_MAIL_FROM"]
SMTP_HOST            = cfg.get("SMTP_HOST", "localhost")
# Kennzeichnet, von welchem Backup-Server die Mail kommt (bei mehreren
# Backup-Servern im Betrieb erkennt man im Betreff sofort, welcher gemeint
# ist). Ohne explizite Konfiguration wird der tatsaechliche Hostname verwendet.
MAIL_SERVER_NAME     = cfg.get("MAIL_SERVER_NAME", socket.gethostname())

RETENTION = {
    "keep-daily":   cfg.get("KEEP_DAILY",   "7"),
    "keep-weekly":  cfg.get("KEEP_WEEKLY",  "4"),
    "keep-monthly": cfg.get("KEEP_MONTHLY", "12"),
    "keep-yearly":  cfg.get("KEEP_YEARLY",  "2"),
}

# Server-Namen aus Konfiguration ermitteln (alle SRV_*_HOST Eintraege).
# <NAME> wird 1:1 als restic --host fuer die Snapshots dieses Servers
# verwendet - dadurch bekommt jeder Server eigene Snapshots und eigene
# Retention, unabhaengig von allen anderen Servern.
SERVERS = sorted(set(
    k[4:-5] for k in cfg if k.startswith("SRV_") and k.endswith("_HOST")
))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("moodle_backup")


def log_and_print(msg: str, level: str = "info") -> None:
    print(msg)
    getattr(logger, level)(msg)


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def send_mail(subject: str, body: str) -> None:
    try:
        msg = MIMEText(body)
        msg["Subject"] = f"[{MAIL_SERVER_NAME}] {subject}"
        msg["From"] = NOTIFY_MAIL_FROM
        msg["To"] = NOTIFY_MAIL_TO

        with smtplib.SMTP(SMTP_HOST) as smtp:
            smtp.send_message(msg)
    except Exception as exc:
        log_and_print(f"Mail konnte nicht versendet werden: {exc}", "error")


def run_streaming(cmd: list, cwd=None) -> subprocess.CompletedProcess:
    """Wie subprocess.run(capture_output=True), gibt die Ausgabe aber Zeile fuer
    Zeile live aus (print + Log) statt erst beim Prozessende. Wichtig bei
    Laeufen, die Stunden/Tage dauern koennen - sonst sieht man bis zum Ende
    keinen Fortschritt."""
    proc = subprocess.Popen(
        cmd, cwd=cwd,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    output_lines = []
    for line in proc.stdout:
        print(line, end="")
        logger.info(line.rstrip())
        output_lines.append(line)
    proc.wait()
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout="".join(output_lines), stderr="")


def run_restic(args: list, capture: bool = True, cwd=None, stream: bool = False) -> subprocess.CompletedProcess:
    cmd = [
        "restic",
        "--repo", RESTIC_REPO,
        "--password-file", RESTIC_PASSWORD_FILE,
    ] + args

    log_and_print(f"Restic Befehl: {' '.join(cmd)}" + (f" (cwd={cwd})" if cwd else ""))
    if stream:
        return run_streaming(cmd, cwd=cwd)
    return subprocess.run(cmd, capture_output=capture, text=True, cwd=cwd)


def apply_retention(host: str) -> bool:
    """Retention fuer genau einen Server, ueber --host isoliert von allen anderen."""
    args = ["forget", "--host", host, "--tag", RESTIC_TAG, "--prune"]
    for flag, value in RETENTION.items():
        args += [f"--{flag}", value]

    result = run_restic(args)

    if result.stdout:
        logger.info(result.stdout)
    if result.returncode != 0:
        log_and_print(f"Retention/Prune fuer {host} FEHLGESCHLAGEN", "error")
        if result.stderr:
            logger.error(result.stderr)
        return False

    return True


# ---------------------------------------------------------------------------
# rsync Pull
# ---------------------------------------------------------------------------

def rsync_pull(name: str, host: str, paths: list, ssh_key: str, ssh_port: str) -> bool:
    """Spiegelt alle Pfade eines Servers 1:1 (inkl. geloeschter Dateien, Zeitstempel,
    Rechte, Eigentuemer) nach tmp/moodle/<name>/<basename>/. Blockiert bis rsync fertig
    ist - erst danach wird ueberhaupt restic fuer diesen Server aufgerufen."""
    ssh_cmd = f"ssh -p {ssh_port} -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
    if ssh_key:
        ssh_cmd += f" -i {ssh_key}"

    server_staging = STAGING_DIR / name

    for remote_path in paths:
        local_target = server_staging / Path(remote_path).name
        local_target.mkdir(parents=True, exist_ok=True)

        cmd = [
            "rsync",
            "-a", "--delete", "--numeric-ids",
            "-e", ssh_cmd,
            f"{host}:{remote_path.rstrip('/')}/",
            f"{local_target}/",
        ]
        log_and_print(f"rsync ({name}): {' '.join(cmd)}")
        result = run_streaming(cmd)

        # rc 24 = "some files vanished before transfer" - bei einem laufenden
        # Server ueblich, kein echter Fehler
        if result.returncode not in (0, 24):
            log_and_print(
                f"rsync FEHLGESCHLAGEN fuer {name} ({remote_path}, rc={result.returncode})",
                "error",
            )
            return False

    return True


def fetch_latest_file(name: str, host: str, remote_dirs: list, ssh_key: str, ssh_port: str) -> bool:
    """Holt aus jedem angegebenen Remote-Verzeichnis NUR die zuletzt veraenderte
    Datei (z.B. den aktuellsten taeglichen SQL-Dump) nach tmp/moodle/<name>/<basename>/,
    statt wie rsync_pull() das komplette (taeglich wachsende) Verzeichnis zu spiegeln.
    Aeltere, bereits gesicherte Dump-Generationen werden bewusst NICHT jeden Tag erneut
    eingelagert - restic liefert ueber seine eigene Snapshot-Retention bereits
    Versionsgeschichte; ein zusaetzliches Mitfuehren alter Dumps wuerde den (oft
    grossen) Dump mehrfach redundant speichern.

    Die Datei wird dabei auf einen festen Namen ("dump<Endung>") umgeschrieben, damit
    ihr Pfad im Snapshot ueber alle Tage stabil bleibt (der Moodle-Server haengt selbst
    einen Zeitstempel an den Dateinamen an, z.B. "2026-10-17_080002_moodle.sql") - so
    kann ein Restore-Skript den Pfad fest verwenden statt ihn pro Snapshot per
    "restic ls" neu zu ermitteln. rsync -a uebertraegt dabei weiterhin die mtime der
    Originaldatei, der tatsaechliche Dump-Zeitpunkt bleibt also trotzdem ablesbar."""
    ssh_argv = ["ssh", "-p", ssh_port, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
    ssh_cmd  = f"ssh -p {ssh_port} -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
    if ssh_key:
        ssh_argv += ["-i", ssh_key]
        ssh_cmd  += f" -i {ssh_key}"

    server_staging = STAGING_DIR / name

    for remote_dir in remote_dirs:
        remote_dir = remote_dir.rstrip("/")
        find_cmd = (
            f"find {shlex.quote(remote_dir)} -maxdepth 1 -type f "
            "-printf '%T@ %f\\n' | sort -rn | head -n1"
        )
        result = subprocess.run(ssh_argv + [host, find_cmd], capture_output=True, text=True)
        latest_name = result.stdout.strip().split(" ", 1)[-1] if result.stdout.strip() else ""

        if result.returncode != 0 or not latest_name:
            log_and_print(
                f"Keine Datei in {remote_dir} auf {name} gefunden (rc={result.returncode})",
                "error",
            )
            return False

        local_target = server_staging / Path(remote_dir).name
        if local_target.exists():
            # Verzeichnis vor dem Pull leeren: es soll immer nur die aktuell
            # neueste Datei enthalten, keine Dumps frueherer Tage
            for old_file in local_target.iterdir():
                if old_file.is_file():
                    old_file.unlink()
        local_target.mkdir(parents=True, exist_ok=True)

        # Fester Zielname statt des zeitgestempelten Original-Dateinamens,
        # damit der Pfad im Snapshot stabil bleibt (siehe Docstring)
        fixed_name = "dump" + "".join(Path(latest_name).suffixes)

        cmd = [
            "rsync", "-a", "--numeric-ids",
            "-e", ssh_cmd,
            f"{host}:{remote_dir}/{latest_name}",
            str(local_target / fixed_name),
        ]
        log_and_print(f"rsync ({name}, neueste Datei {latest_name} -> {fixed_name}): {' '.join(cmd)}")
        rsync_result = run_streaming(cmd)

        if rsync_result.returncode != 0:
            log_and_print(
                f"rsync FEHLGESCHLAGEN fuer {name} ({remote_dir}/{latest_name}, rc={rsync_result.returncode})",
                "error",
            )
            return False

    return True


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def backup_server(name: str, dry_run: bool = False, site: str = "") -> bool:
    args = [
        "backup",
        "--host", name,
        "--tag", RESTIC_TAG,
    ]
    if site:
        # Zusaetzlicher Tag fuer den Standort - erlaubt z.B.
        # "restic snapshots --tag gs10", um alle Backups eines Standorts
        # uebergreifend zu sehen, unabhaengig vom einzelnen --host.
        args += ["--tag", site]
    args += [
        "--verbose",
        "--exclude", ".gitkeep",
        ".",
    ]

    if dry_run:
        args.append("--dry-run")

    # cwd=Staging-Verzeichnis dieses Servers, Quelle ".": Snapshot-Wurzel
    # enthaelt moodledata und das Dump-Verzeichnis als Geschwister-Ordner -
    # beide Pfade landen so in EINEM gemeinsamen Snapshot. Server werden
    # ausschliesslich ueber --host unterschieden (Retention laeuft ja
    # ohnehin schon pro --host).
    result = run_restic(args, cwd=STAGING_DIR / name, stream=True)

    if result.returncode != 0:
        log_and_print(f"Restic Backup fuer {name} FEHLGESCHLAGEN", "error")
        return False

    log_and_print(f"Backup fuer {name} erfolgreich." if not dry_run else f"DRY-RUN fuer {name} abgeschlossen.")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Moodle Backup via rsync + Restic")
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help=(
            "Testlauf – restic schreibt nichts, keine Snapshots, keine Retention "
            "(rsync synct trotzdem, damit die Vorschau den echten Stand zeigt)"
        ),
    )
    args = parser.parse_args()

    if not SERVERS:
        log_and_print(
            f"FEHLER: Keine Server in {CONFIG_FILE} gefunden "
            "(erwartet: SRV_<NAME>_HOST / SRV_<NAME>_PATHS) – Backup abgebrochen.",
            "error",
        )
        send_mail(
            "⚠️ Moodle Backup Fehler",
            f"Keine Server in {CONFIG_FILE} gefunden.\n"
            "Erwartetes Format: SRV_<NAME>_HOST / SRV_<NAME>_PATHS\n"
            "Backup wurde abgebrochen, es wurden keine Snapshots erstellt.",
        )
        return

    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

    lock_fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log_and_print("Ein anderer Moodle-Backup-Lauf ist bereits aktiv – breche ab.", "warning")
        lock_fp.close()
        sys.exit(0)

    try:
        start = datetime.now()
        log_and_print(f"=== Moodle Backup Start {start:%Y-%m-%d %H:%M:%S} ===")

        successful = []
        failures = {}

        for name in SERVERS:
            host        = cfg.get(f"SRV_{name}_HOST")
            paths_raw   = cfg.get(f"SRV_{name}_PATHS", "")
            latest_raw  = cfg.get(f"SRV_{name}_LATEST_PATHS", "")
            ssh_key     = cfg.get(f"SRV_{name}_SSH_KEY_FILE", SSH_KEY_FILE_DEFAULT)
            ssh_port    = cfg.get(f"SRV_{name}_SSH_PORT", SSH_PORT_DEFAULT)
            site        = cfg.get(f"SRV_{name}_SITE", "")

            if not host or not (paths_raw or latest_raw):
                log_and_print(
                    f"FEHLER: Konfiguration fuer Server {name} unvollstaendig "
                    "(HOST/PATHS/LATEST_PATHS)", "error",
                )
                failures[name] = "Konfiguration unvollstaendig"
                continue

            paths        = [p.strip() for p in paths_raw.split(",") if p.strip()]
            latest_paths = [p.strip() for p in latest_raw.split(",") if p.strip()]
            log_and_print(f"--- Server {name} ({host}) ---")

            if paths and not rsync_pull(name, host, paths, ssh_key, ssh_port):
                failures[name] = "rsync fehlgeschlagen"
                continue

            if latest_paths and not fetch_latest_file(name, host, latest_paths, ssh_key, ssh_port):
                failures[name] = "rsync (neueste Datei) fehlgeschlagen"
                continue

            if not backup_server(name, dry_run=args.dry_run, site=site):
                failures[name] = "restic backup fehlgeschlagen"
                continue

            if not args.dry_run and not apply_retention(name):
                failures[name] = "retention/prune fehlgeschlagen"
                continue

            successful.append(name)

        end = datetime.now()
        duration = end - start
        log_and_print(
            f"=== Moodle Backup Ende {end:%Y-%m-%d %H:%M:%S} (Dauer: {duration}) ==="
        )

        if failures:
            send_mail(
                "⚠️ Moodle Backup Fehler",
                f"{len(failures)} von {len(SERVERS)} Moodle-Servern konnten nicht "
                "vollstaendig gesichert werden:\n\n"
                + "\n".join(f"  - {n}: {r}" for n, r in failures.items())
                + f"\n\nLog: {LOG_FILE}",
            )

        if not failures and not args.dry_run:
            send_mail(
                "✅ Moodle Backup erfolgreich",
                (
                    f"Moodle Backup erfolgreich abgeschlossen.\n\n"
                    f"Start:    {start:%Y-%m-%d %H:%M:%S}\n"
                    f"Ende:     {end:%Y-%m-%d %H:%M:%S}\n"
                    f"Dauer:    {duration}\n"
                    f"Server gesichert ({len(successful)}/{len(SERVERS)}):\n"
                    f"  - " + "\n  - ".join(successful) + "\n\n"
                    f"Log: {LOG_FILE}"
                ),
            )
    finally:
        fcntl.flock(lock_fp, fcntl.LOCK_UN)
        lock_fp.close()


if __name__ == "__main__":
    main()
