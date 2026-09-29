#!/bin/bash
#
# Initialer rsync-Seed fuer den Samba-Staging-Ordner (tmp/samba/).
#
# Fuer den allerersten Sync einer Freigabe kann rsync je nach Datenmenge
# mehrere Tage laufen. samba-backup.py puffert die rsync-Ausgabe komplett
# und zeigt waehrend des Laufs keinen Fortschritt - dieses Skript laesst
# rsync stattdessen direkt mit --progress/--partial laufen (Live-Ausgabe,
# und bei einer Unterbrechung wird beim naechsten Lauf an der angefangenen
# Datei weitergemacht statt neu zu beginnen).
#
# Liest dieselbe config/samba.cfg wie samba-backup.py (SRV_<NAME>_HOST/
# _PATHS/_SSH_KEY_FILE/_SSH_PORT), macht aber NUR den rsync-Teil - kein
# restic, kein Lockfile. Nach einem (oder mehreren, ueber Nacht laufenden)
# Durchgaengen mit diesem Skript kann samba-backup.py regulaer starten und
# muss nur noch die seitdem geaenderten Dateien uebertragen.
#
# Fuer einen mehrtaegigen Lauf am besten in tmux/screen starten, z.B.:
#   tmux new -s samba-seed
#   scripts/samba-initial-sync.sh
#   # Strg+B, D zum Abhaengen; mit "tmux attach -t samba-seed" wieder rein
#
# Nutzung:
#   scripts/samba-initial-sync.sh                    # alle konfigurierten Server
#   scripts/samba-initial-sync.sh srv-gs10-01         # nur dieser Server
#   scripts/samba-initial-sync.sh --dry-run           # nur die rsync-Befehle anzeigen,
#   scripts/samba-initial-sync.sh --dry-run srv-gs10-01  # nichts wird uebertragen/angelegt
#
# WICHTIG: Dieses Skript braucht bash (mapfile, PIPESTATUS, pipefail - gibt
# es in Debians /bin/sh (dash) nicht). Bitte mit "./scripts/samba-initial-sync.sh"
# oder "bash scripts/samba-initial-sync.sh" aufrufen, NICHT mit "sh scripts/...".

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
CONFIG_FILE="$REPO_ROOT/config/samba.cfg"
STAGING_DIR="$REPO_ROOT/tmp/samba"
LOG_FILE="$REPO_ROOT/log/samba-initial-sync.log"

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
# wie samba-backup.py ermitteln, NUL/RS-getrennt fuer robustes Einlesen in
# bash (Werte koennen Sonderzeichen, aber keine NUL-Bytes enthalten).
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
        log "UEBERSPRINGE $name: HOST/PATHS unvollstaendig"
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

        # rc 24 = "some files vanished before transfer" - bei einer lebenden
        # Freigabe ueblich, kein echter Fehler (wie in samba-backup.py)
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
