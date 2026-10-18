# Firma Backup – Restic + OPNsense + WebUI

Backup-Lösung für den Firma-Samba-Server, eine beliebige Anzahl von OPNsense-Firewall-Instanzen
und den Moodle-Server (moodledata + tägliche SQL-Dumps)
(Firewalls/Server werden automatisch aus der jeweiligen Konfigurationsdatei ermittelt, kein Code-Change nötig).
Betrieben auf einem Debian-LXC-Container auf Proxmox, verbunden per OpenVPN.

---

## Projektstruktur

```
backup-and-recovery/
├── scripts/
│   ├── samba-backup.py         Samba-Freigaben per rsync/SSH spiegeln + Restic sichern
│   ├── firewalls-backup.py     OPNsense-Configs per API + Restic
│   └── moodle-backup.py        moodledata + SQL-Dumps per rsync/SSH spiegeln + Restic sichern
├── webui/
│   ├── main.py                 FastAPI Backend (LDAP/AD-Auth + Restic-CLI-Wrapper)
│   └── templates/
│       ├── login.html
│       └── index.html
├── config/
│   ├── samba.cfg.example       Vorlage – auf dem Server als samba.cfg ablegen
│   ├── firewalls.cfg.example   Vorlage – auf dem Server als firewalls.cfg ablegen
│   ├── moodle.cfg.example      Vorlage – auf dem Server als moodle.cfg ablegen
│   └── webui.cfg.example       Vorlage – auf dem Server als webui.cfg ablegen
├── systemd/                    Systemd Service- und Timer-Dateien
├── log/                        Log-Verzeichnis (Platzhalter, .gitkeep)
├── repositories/                Lokale Restic-Repositories (Platzhalter, .gitkeep)
├── tmp/firewalls/                Staging fuer heruntergeladene OPNsense-Configs (vor dem Restic-Backup)
├── tmp/samba/                    rsync-Spiegel der Samba-Freigaben (vor dem Restic-Backup)
├── tmp/moodle/                   rsync-Spiegel von moodledata + SQL-Dumps (vor dem Restic-Backup)
├── requirements.txt             Python-Abhängigkeiten für Backup-Skripte + WebUI
├── README.md
└── .gitignore
```

---

## Installation

### 1. Repo klonen

```bash
git clone <repo-url> /opt/firma-backup
cd /opt/firma-backup
```

### 2. Restic installieren und aktualisieren

```bash
apt update && apt install -y restic
restic self-update
```

### 3. rsync/SSH für den Samba-Pull einrichten

```bash
apt install -y rsync openssh-client
ssh-keygen -t ed25519 -f /etc/restic/samba_id_ed25519 -N ""
# Public Key (/etc/restic/samba_id_ed25519.pub) auf dem Samba-Server für
# den SSH-User aus SAMBA_HOST in ~/.ssh/authorized_keys eintragen
```

Der Key-Pfad kommt später als `SSH_KEY_FILE` in `config/samba.cfg` (siehe Schritt 5).

Für den Moodle-Pull analog ein eigener Key:

```bash
ssh-keygen -t ed25519 -f /etc/restic/moodle_id_ed25519 -N ""
# Public Key (/etc/restic/moodle_id_ed25519.pub) auf dem Moodle-Server für
# den SSH-User aus SRV_moodle_HOST in ~/.ssh/authorized_keys eintragen
# (Lesezugriff auf moodledata + das SQL-Dump-Verzeichnis genügt)
```

### 4. Restic Repositories initialisieren

Ein Repository für die Samba-Daten, eines für die Firewall-Configs, eines für Moodle:

```bash
mkdir -p /backup/files/restic/samba
restic init --repo /backup/files/restic/samba

mkdir -p /backup/files/restic/firewalls
restic init --repo /backup/files/restic/firewalls

mkdir -p /backup/files/restic/moodle
restic init --repo /backup/files/restic/moodle

mkdir -p /etc/restic
echo "dein-passwort" > /etc/restic/password.txt
chmod 600 /etc/restic/password.txt

echo "dein-moodle-passwort" > /etc/restic/password-moodle.txt
chmod 600 /etc/restic/password-moodle.txt
```

### 5. Konfigurationsdateien anlegen

```bash
cp config/samba.cfg.example config/samba.cfg
cp config/firewalls.cfg.example config/firewalls.cfg
cp config/moodle.cfg.example config/moodle.cfg
cp config/webui.cfg.example config/webui.cfg
# Werte anpassen:
nano config/samba.cfg
nano config/firewalls.cfg
nano config/moodle.cfg
nano config/webui.cfg
```

Für `firewalls.cfg` gilt: Firewalls werden anhand aller `FRW_<NAME>_IP`-Einträge automatisch erkannt –
für eine weitere Firewall einfach einen zusätzlichen `FRW_<NAME>_IP/_KEY/_SECRET`-Block hinzufügen.

Für `samba.cfg` gilt: beliebig viele Samba-Server werden anhand aller `SRV_<NAME>_HOST`-Einträge
automatisch erkannt – für einen weiteren Server einfach einen zusätzlichen
`SRV_<NAME>_HOST`/`_PATHS`-Block hinzufügen (optional mit eigenem `_SSH_KEY_FILE`/`_SSH_PORT`,
sonst gelten die globalen `SSH_KEY_FILE`/`SSH_PORT`-Defaults). `<NAME>` wird 1:1 als restic
`--host` verwendet – jeder Server bekommt dadurch eigene Snapshots und eigene, unabhängige
Retention. `SRV_<NAME>_PATHS` sind kommagetrennte Pfade auf dem jeweiligen Server – jeder wird
per rsync 1:1 (inkl. gelöschter Dateien, Zeitstempel, Rechte) nach `tmp/samba/<NAME>/<basename>/`
gespiegelt, bevor restic den Stand dieses Servers sichert.

Für `moodle.cfg` gilt dasselbe `SRV_<NAME>_HOST`-Schema wie bei Samba, mit zwei Arten von Pfaden,
die beide in **einem** gemeinsamen restic-Snapshot landen:
- `SRV_<NAME>_PATHS` – normales 1:1-rsync-Mirroring (wie bei Samba), hier für das
  moodledata-Verzeichnis (Datei-Uploads).
- `SRV_<NAME>_LATEST_PATHS` – für das SQL-Dump-Verzeichnis. Der Moodle-Server legt dort täglich
  eine neue, zeitgestempelte Dump-Datei an, ohne alte selbst zu löschen. Damit diese (oft großen)
  Dumps nicht jeden Tag erneut und mehrfach im Repo landen, holt `moodle-backup.py` aus diesem
  Verzeichnis bewusst **nur die jeweils neueste Datei** – ältere Dump-Generationen bleiben auf dem
  Moodle-Server liegen (dessen eigene Sache), die Versionsgeschichte übernimmt stattdessen restics
  eigene Snapshot-Retention (`KEEP_DAILY`/`WEEKLY`/`MONTHLY`/`YEARLY`).

### 6. Initialen Samba-Sync durchführen (empfohlen bei großen Freigaben)

Der allererste rsync-Durchlauf kann je nach Datenmenge mehrere Tage dauern. Damit der reguläre
`samba-backup.py`-Lauf (per Timer) dabei nicht im Weg steht bzw. keine Chance hat fertig zu
werden, vorher einmalig mit `scripts/samba-initial-sync.sh` vorsynchronisieren – das macht nur
den rsync-Teil (kein restic, kein Lockfile) und zeigt den Fortschritt live an, statt ihn wie
`samba-backup.py` erst am Ende gepuffert auszugeben.

```bash
# Erst anschauen, welche Befehle laufen wuerden (nichts wird uebertragen/angelegt):
scripts/samba-initial-sync.sh --dry-run

# Einzelnen Server pruefen:
scripts/samba-initial-sync.sh --dry-run srv-gs10-01

# Echten Lauf am besten in tmux/screen starten, da er lange dauern kann:
tmux new -s samba-seed
scripts/samba-initial-sync.sh
# Strg+B, D zum Abhaengen; mit "tmux attach -t samba-seed" wieder rein

# Optional nur ein Server (z.B. um Server zeitlich zu staffeln):
scripts/samba-initial-sync.sh srv-gs10-01
```

Log landet zusätzlich unter `log/samba-initial-sync.log`. Ist der Staging-Ordner einmal
vollständig, muss `samba-backup.py` beim ersten regulären Lauf nur noch die seitdem geänderten
Dateien übertragen.

Analog gibt es `scripts/moodle-initial-sync.sh` für den initialen Sync von moodledata
(ausschließlich `SRV_<NAME>_PATHS` – die SQL-Dumps aus `SRV_<NAME>_LATEST_PATHS` bestehen pro
Lauf nur aus einer einzelnen Datei und brauchen keinen gesonderten Seed-Sync):

```bash
scripts/moodle-initial-sync.sh --dry-run

tmux new -s moodle-seed
scripts/moodle-initial-sync.sh
# Strg+B, D zum Abhaengen; mit "tmux attach -t moodle-seed" wieder rein
```

Log landet unter `log/moodle-initial-sync.log`.

### 7. Python-Abhängigkeiten installieren

```bash
pip install -r requirements.txt --break-system-packages
```

(Deckt sowohl die Backup-Skripte als auch die WebUI ab: FastAPI, Uvicorn, Jinja2, ldap3, requests, urllib3.)

### 8. Systemd Timer aktivieren

```bash
cp systemd/*.service systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now samba-backup.timer
systemctl enable --now firewalls-backup.timer
systemctl enable --now moodle-backup.timer
```

Zeitplan der drei Timer (gestaffelt, um I/O-Überlappung zu vermeiden): Samba 02:00 Uhr →
Firewalls 03:00 Uhr → Moodle 06:00 Uhr (mit Sicherheitsabstand nach dem Ende des
Moodle-eigenen MySQL-Dump-Jobs, der laut Betrieb bis ca. 05:00 Uhr fertig ist).

> ⚠️ Vor dem Aktivieren prüfen, ob `ExecStart` in den `.service`-Dateien exakt auf die
> tatsächlichen Skriptnamen in `scripts/` zeigt (`samba-backup.py`, `firewalls-backup.py`,
> `moodle-backup.py`).

### 9. WebUI starten (optional)

```bash
cp systemd/restic-webui.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now restic-webui
# Erreichbar unter http://<server-ip>:8080
```

`WorkingDirectory` und `ExecStart` in `restic-webui.service` müssen auf das `webui/`-Verzeichnis
dieses Repos zeigen (dort liegt `main.py`).

---

## Manuell ausführen

```bash
# Samba-Backup (alle konfigurierten Server)
python3 /opt/firma-backup/scripts/samba-backup.py

# Samba-Backup Dry-Run (kein Snapshot, keine Retention, keine Mail)
python3 /opt/firma-backup/scripts/samba-backup.py --dry-run

# OPNsense-Backup
python3 /opt/firma-backup/scripts/firewalls-backup.py

# Moodle-Backup (moodledata + SQL-Dumps)
python3 /opt/firma-backup/scripts/moodle-backup.py

# Moodle-Backup Dry-Run (kein Snapshot, keine Retention, keine Mail)
python3 /opt/firma-backup/scripts/moodle-backup.py --dry-run

# Initialer Samba-Sync (nur rsync, kein restic – siehe Installation Schritt 6)
scripts/samba-initial-sync.sh --dry-run          # nur Befehle anzeigen
scripts/samba-initial-sync.sh                    # alle Server
scripts/samba-initial-sync.sh srv-gs10-01         # nur ein Server

# Initialer Moodle-Sync (nur moodledata, nur rsync, kein restic – siehe Installation Schritt 6)
scripts/moodle-initial-sync.sh --dry-run         # nur Befehle anzeigen
scripts/moodle-initial-sync.sh                   # alle Server
scripts/moodle-initial-sync.sh moodle            # nur ein Server
```

---

## WebUI

FastAPI-Anwendung zum Durchsuchen und Wiederherstellen von Restic-Snapshots im Browser.

- **Login** gegen Samba/Active Directory per LDAP (`webui.cfg`: `AD_SERVER`, `AD_DOMAIN`,
  `AD_BASE_DN`, `AD_GROUP`) – nur Mitglieder der konfigurierten Gruppe erhalten Zugriff.
- **Mehrere Restic-Repositories** gleichzeitig nutzbar, je Eintrag `REPO_<NAME>` in `webui.cfg`
  (eigenes Passwort pro Repo möglich, sonst Fallback `RESTIC_PASSWORD_FILE_DEFAULT`).
- **Snapshots auflisten**, optional gefiltert nach Tag (`/api/{repo}/snapshots`).
- **Dateibaum ansehen** (`/api/{repo}/ls/{snapshot_id}`) und **Volltextsuche** über Dateinamen
  (`/api/{repo}/find`).
- **Download** einzelner Dateien oder ganzer Ordner als ZIP direkt aus dem Snapshot
  (`/api/{repo}/download/{snapshot_id}`), Restore erfolgt serverseitig in ein temporäres
  Verzeichnis und wird danach wieder aufgeräumt.
- Sessions werden in-memory verwaltet (Cookie `session_token`, Ablauf über
  `SESSION_EXPIRE_MINUTES`).

---

## Wichtige Restic-Befehle

### Snapshots anzeigen

```bash
export RESTIC_REPOSITORY=/backup/files/restic/samba
export RESTIC_PASSWORD_FILE=/etc/restic/password.txt

restic snapshots
restic snapshots --tag samba
restic snapshots --tag opnsense
restic snapshots --tag moodle
```

### Dateien suchen

```bash
restic find "rechnung*.pdf"
restic find --tag samba "*.xlsx"
restic find --snapshot <id> "*.docx"
restic find --path "/srv/samba/groups/buchhaltung" "*.pdf"
```

### Inhalt eines Snapshots anzeigen

```bash
restic ls <snapshot-id>
restic ls <snapshot-id> /srv/samba/users/max
```

### Wiederherstellen

```bash
# Immer erst in temporaeres Verzeichnis!
restic restore latest --tag samba --target /tmp/restore

# Einzelne Datei
restic restore <id> --target /tmp/restore \
  --include "/srv/samba/groups/buchhaltung/rechnung.pdf"

# Danach auf Samba-Server uebertragen
rsync -av /tmp/restore/srv/samba/ samba-user@10.8.0.x:/srv/samba/
```

### Retention manuell (Dry-Run)

```bash
restic forget --tag samba \
  --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --keep-yearly 2 \
  --dry-run
```

### Repository prüfen

```bash
restic check
restic stats
```

---

## Retention Policy

| Policy | Bedeutung |
|---|---|
| `keep-daily 7` | Letzte 7 Tage, je 1 Snapshot/Tag |
| `keep-weekly 4` | Letzte 4 Wochen, je 1 Snapshot/Woche |
| `keep-monthly 12` | Letzte 12 Monate, je 1 Snapshot/Monat |
| `keep-yearly 2` | Letzte 2 Jahre, je 1 Snapshot/Jahr |

Wird nach jedem erfolgreichen Backup automatisch angewendet (`forget --prune`), konfigurierbar
über `KEEP_DAILY` / `KEEP_WEEKLY` / `KEEP_MONTHLY` / `KEEP_YEARLY` in der jeweiligen `.cfg`.

---

## Disaster Recovery

1. Neuen Server aufsetzen, Restic installieren
2. VPN-Verbindung herstellen
3. Passwort aus sicherem Speicher holen
4. Restore durchführen:
   ```bash
   restic -r /backup/files/restic/samba \
     --password-file /etc/restic/password.txt \
     restore latest --tag samba --target /tmp/restore
   ```
5. Daten auf Samba-Server übertragen:
   ```bash
   rsync -av /tmp/restore/srv/samba/ samba-user@10.8.0.x:/srv/samba/
   ```
6. ACL-Cronjob auf dem Samba-Server manuell ausführen

---

## Logs

Standardmäßig schreiben die Skripte und die WebUI nach `/var/log/` (Pfad je `LOG_FILE` in der
jeweiligen `.cfg` konfigurierbar):

```
/var/log/samba-backup.log
/var/log/opnsense-backup.log
/var/log/moodle-backup.log
/var/log/restic-webui.log
```

---

## Konfigurationsdateien (Übersicht)

| Datei | Verwendet von | Zweck |
|---|---|---|
| `config/samba.cfg` | `scripts/samba-backup.py` | Restic-Repo, Samba-Host/Pfade, Mail, Retention |
| `config/firewalls.cfg` | `scripts/firewalls-backup.py` | Restic-Repo, OPNsense-API-Zugangsdaten je Firewall, Mail, Retention |
| `config/moodle.cfg` | `scripts/moodle-backup.py` | Restic-Repo, Moodle-Host/Pfade (moodledata + SQL-Dumps), Mail, Retention |
| `config/webui.cfg` | `webui/main.py` | Restic-Repositories für die WebUI, AD/LDAP-Auth, Session |

Alle vier Dateien enthalten Zugangsdaten und liegen **nicht** im Git-Repo (siehe `.gitignore`) –
nur die zugehörigen `*.cfg.example`-Vorlagen sind versioniert.
