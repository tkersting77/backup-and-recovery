#!/bin/bash
#
# Initialer rsync-Seed fuer den Moodle-Staging-Ordner (tmp/moodle/),
# ausschliesslich fuer moodledata (SRV_<NAME>_PATHS) - NICHT fuer die
# SQL-Dumps (SRV_<NAME>_LATEST_PATHS). Die Dumps bestehen pro Lauf nur aus
# einer einzelnen Datei (siehe fetch_latest_file() in moodle-backup.py) und
# brauchen deshalb keinen gesonderten, lang laufenden Seed-Sync.
#
# Fuer den allerersten Sync von moodledata kann rsync je nach Datenmenge
# mehrere Stunden/Tage laufen. moodle-backup.py puffert die rsync-Ausgabe
# komplett und zeigt waehrend des Laufs keinen Fortschritt - dieses Skript
# laesst rsync stattdessen direkt mit --progress/--partial laufen (Live-
# Ausgabe, und bei einer Unterbrechung wird beim naechsten Lauf an der
# angefangenen Datei weitergemacht statt neu zu beginnen).
#
# Liest dieselbe config/moodle.cfg wie moodle-backup.py (SRV_<NAME>_HOST/
# _PATHS/_SSH_KEY_FILE/_SSH_PORT), macht aber NUR den rsync-Teil fuer
# SRV_<NAME>_PATHS - kein restic, kein Lockfile. Nach einem (oder mehreren,
# ueber Nacht laufenden) Durchgaengen mit diesem Skript kann moodle-backup.py
# regulaer starten und muss fuer moodledata nur noch die seitdem geaenderten
# Dateien uebertragen.
#
# Fuer einen mehrtaegigen Lauf am besten in tmux/screen starten, z.B.:
#   tmux new -s moodle-seed
#   scripts/moodle-initial-sync.sh
#   # Strg+B, D zum Abhaengen; mit "tmux attach -t moodle-seed" wieder rein
#
# Nutzung:
#   scripts/moodle-initial-sync.sh                 # alle konfigurierten Server
#   scripts/moodle-initial-sync.sh moodle           # nur dieser Server
#   scripts/moodle-initial-sync.sh --dry-run        # nur die rsync-Befehle anzeigen,
#   scripts/moodle-initial-sync.sh --dry-run moodle # nichts wird uebertragen/angelegt
#
# WICHTIG: Dieses Skript braucht bash (mapfile, PIPESTATUS, pipefail - gibt
# es in Debians /bin/sh (dash) nicht). Bitte mit "./scripts/moodle-initial-sync.sh"
# oder "bash scripts/moodle-initial-sync.sh" aufrufen, NICHT mit "sh scripts/...".

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
CONFIG_FILE="$REPO_ROOT/config/moodle.cfg"
STAGING_DIR="$REPO_ROOT/tmp/moodle"
LOG_FILE="$REPO_ROOT/log/moodle-initial-sync.log"

DRY_RUN=0
ONLY_SERVER=""
for arg in "$@"; do
    case "$arg" in
        --dry-run|-n)
            DRY_RUN=1
            ;;
        *)
            ONLY_SERVER="$arg"
            ;;
    esac
done

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG_FILE"
}

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "FEHLER: $CONFIG_FILE nicht gefunden" >&2
    exit 1
fi

mkdir -p "$STAGING_DIR" "$(dirname "$LOG_FILE")"

# Server-Liste (+ HOST/PATHS/SSH_KEY_FILE/SSH_PORT) mit demselben Parsing
# wie moodle-backup.py ermitteln, NUL/RS-getrennt fuer robustes Einlesen in
# bash (Werte koennen Sonderzeichen, aber keine NUL-Bytes enthalten).
# SRV_<NAME>_LATEST_PATHS (SQL-Dumps) wird hier bewusst nicht gelesen.
mapfile -d '' -t SERVER_RECORDS < <(python3 - "$CONFIG_FILE" "$ONLY_SERVER" <<'PYEOF'
import sys

config_file, only_server = sys.argv[1], sys.argv[2]

cfg = {}
with open(config_file) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        cfg[key.strip()] = value.strip().strip('"').strip("'")

servers = sorted(set(
    k[4:-5] for k in cfg if k.startswith("SRV_") and k.endswith("_HOST")
))
if only_server:
    servers = [s for s in servers if s == only_server]

ssh_key_default = cfg.get("SSH_KEY_FILE", "")
ssh_port_default = cfg.get("SSH_PORT", "22")

for name in servers:
    host = cfg.get(f"SRV_{name}_HOST", "")
    paths = cfg.get(f"SRV_{name}_PATHS", "")
    ssh_key = cfg.get(f"SRV_{name}_SSH_KEY_FILE", ssh_key_default)
    ssh_port = cfg.get(f"SRV_{name}_SSH_PORT", ssh_port_default)
    sys.stdout.write("\x1e".join([name, host, paths, ssh_key, ssh_port]))
    sys.stdout.write("\x00")
PYEOF
)

if [[ "${#SERVER_RECORDS[@]}" -eq 0 ]]; then
    if [[ -n "$ONLY_SERVER" ]]; then
        echo "FEHLER: Server '$ONLY_SERVER' nicht in $CONFIG_FILE gefunden" >&2
    else
        echo "FEHLER: Keine Server (SRV_<NAME>_HOST) in $CONFIG_FILE gefunden" >&2
    fi
    exit 1
fi

for record in "${SERVER_RECORDS[@]}"; do
    IFS=$'\x1e' read -r name host paths_raw ssh_key ssh_port <<< "$record"

    if [[ -z "$host" || -z "$paths_raw" ]]; then
        log "UEBERSPRINGE $name: HOST/PATHS unvollstaendig (ggf. nur LATEST_PATHS konfiguriert)"
        continue
    fi

    ssh_cmd="ssh -p $ssh_port -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
    if [[ -n "$ssh_key" ]]; then
        ssh_cmd="$ssh_cmd -i $ssh_key"
    fi

    IFS=',' read -ra paths <<< "$paths_raw"
    for remote_path in "${paths[@]}"; do
        remote_path="$(echo "$remote_path" | xargs)"
        [[ -z "$remote_path" ]] && continue

        base_name="$(basename "$remote_path")"
        local_target="$STAGING_DIR/$name/$base_name"

        rsync_cmd=(rsync -a --delete --numeric-ids --partial --progress
            -e "$ssh_cmd"
            "$host:${remote_path%/}/"
            "$local_target/")

        if [[ "$DRY_RUN" -eq 1 ]]; then
            printf -v cmd_str '%q ' "${rsync_cmd[@]}"
            log "DRY-RUN ($name): $cmd_str"
            continue
        fi

        mkdir -p "$local_target"
        log "=== Start $name:$remote_path -> $local_target ==="
        "${rsync_cmd[@]}" 2>&1 | tee -a "$LOG_FILE"
        rc=${PIPESTATUS[0]}

        # rc 24 = "some files vanished before transfer" - bei einem laufenden
        # Server ueblich, kein echter Fehler (wie in moodle-backup.py)
        if [[ "$rc" -ne 0 && "$rc" -ne 24 ]]; then
            log "FEHLER: rsync fuer $name ($remote_path) fehlgeschlagen (rc=$rc)"
            exit 1
        fi
        log "=== Ende $name:$remote_path ==="
    done
done

if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY-RUN abgeschlossen – es wurde nichts uebertragen oder angelegt."
else
    log "Initialer Sync abgeschlossen."
fi
