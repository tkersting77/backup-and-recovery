# Firma Backup – Restic + OPNsense + WebUI

Backup-Lösung für den Firma-Samba-Server und eine beliebige Anzahl von OPNsense-Firewall-Instanzen
(Firewalls werden automatisch aus der Konfigurationsdatei ermittelt, kein Code-Change nötig).
Betrieben auf einem Debian-LXC-Container auf Proxmox, verbunden per OpenVPN.

---

## Projektstruktur

```
backup-and-recovery/
├── scripts/
│   ├── samba-backup.py         Samba-Freigaben per Restic Pull via SFTP
│   └── firewalls-backup.py     OPNsense-Configs per API + Restic
├── webui/
│   ├── main.py                 FastAPI Backend (LDAP/AD-Auth + Restic-CLI-Wrapper)
│   └── templates/
│       ├── login.html
│       └── index.html
├── config/
│   ├── samba.cfg.example       Vorlage – auf dem Server als samba.cfg ablegen
│   ├── firewalls.cfg.example   Vorlage – auf dem Server als firewalls.cfg ablegen
│   └── webui.cfg.example       Vorlage – auf dem Server als webui.cfg ablegen
├── systemd/                    Systemd Service- und Timer-Dateien
├── log/                        Log-Verzeichnis (Platzhalter, .gitkeep)
├── repositories/                Lokale Restic-Repositories (Platzhalter, .gitkeep)
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

### 3. Restic Repositories initialisieren

Ein Repository für die Samba-Daten, eines für die Firewall-Configs:

```bash
mkdir -p /backup/files/restic/firma
restic init --repo /backup/files/restic/firma

mkdir -p /backup/files/restic/opnsense
restic init --repo /backup/files/restic/opnsense

mkdir -p /etc/restic
echo "dein-passwort" > /etc/restic/password.txt
chmod 600 /etc/restic/password.txt
```

### 4. Konfigurationsdateien anlegen

```bash
cp config/samba.cfg.example config/samba.cfg
cp config/firewalls.cfg.example config/firewalls.cfg
cp config/webui.cfg.example config/webui.cfg
# Werte anpassen:
nano config/samba.cfg
nano config/firewalls.cfg
nano config/webui.cfg
```

Für `firewalls.cfg` gilt: Firewalls werden anhand aller `FW_<NAME>_IP`-Einträge automatisch erkannt –
für eine weitere Firewall einfach einen zusätzlichen `FW_<NAME>_IP/_KEY/_SECRET`-Block hinzufügen.

### 5. Python-Abhängigkeiten installieren

```bash
pip install -r requirements.txt --break-system-packages
```

(Deckt sowohl die Backup-Skripte als auch die WebUI ab: FastAPI, Uvicorn, Jinja2, ldap3, requests, urllib3.)

### 6. Systemd Timer aktivieren

```bash
cp systemd/*.service systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now samba-backup.timer
systemctl enable --now opnsense-backup.timer
```

> ⚠️ Vor dem Aktivieren prüfen, ob `ExecStart` in den `.service`-Dateien exakt auf die
> tatsächlichen Skriptnamen in `scripts/` zeigt (`samba-backup.py`, `firewalls-backup.py`).

### 7. WebUI starten (optional)

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
# Samba-Backup
python3 /opt/firma-backup/scripts/samba-backup.py

# Samba-Backup Dry-Run (kein Snapshot, keine Mail)
python3 /opt/firma-backup/scripts/samba-backup.py --dry-run

# OPNsense-Backup
python3 /opt/firma-backup/scripts/firewalls-backup.py
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
export RESTIC_REPOSITORY=/backup/files/restic/firma
export RESTIC_PASSWORD_FILE=/etc/restic/password.txt

restic snapshots
restic snapshots --tag samba
restic snapshots --tag opnsense
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
   restic -r /backup/files/restic/firma \
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
/var/log/restic-webui.log
```

---

## Konfigurationsdateien (Übersicht)

| Datei | Verwendet von | Zweck |
|---|---|---|
| `config/samba.cfg` | `scripts/samba-backup.py` | Restic-Repo, Samba-Host/Pfade, Mail, Retention |
| `config/firewalls.cfg` | `scripts/firewalls-backup.py` | Restic-Repo, OPNsense-API-Zugangsdaten je Firewall, Mail, Retention |
| `config/webui.cfg` | `webui/main.py` | Restic-Repositories für die WebUI, AD/LDAP-Auth, Session |

Alle drei Dateien enthalten Zugangsdaten und liegen **nicht** im Git-Repo (siehe `.gitignore`) –
nur die zugehörigen `*.cfg.example`-Vorlagen sind versioniert.
