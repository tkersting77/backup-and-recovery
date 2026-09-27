# Firma Backup – Restic + OPNsense + WebUI

Backup-Lösung für den Firma-Samba-Server und fünf OPNsense-Firewall-Instanzen.
Betrieben auf einem Debian-LXC-Container auf Proxmox, verbunden per OpenVPN.

---

## Projektstruktur

```
firma-backup/
├── scripts/
│   ├── samba_backup.py        Samba-Freigaben per Restic Pull via SFTP
│   └── opnsense_backup.py     OPNsense-Configs per API + Restic
├── webui/
│   ├── main.py                FastAPI Backend
│   ├── requirements.txt
│   └── templates/
│       ├── login.html
│       └── index.html
├── config/
│   ├── samba.cfg.example      Vorlage – auf dem Server als samba.cfg ablegen
│   ├── opnsense.cfg.example   Vorlage – auf dem Server als opnsense.cfg ablegen
│   └── webui.cfg.example      Vorlage – auf dem Server als webui.cfg ablegen
├── systemd/                   Systemd Service- und Timer-Dateien
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

### 3. Restic Repository initialisieren

```bash
mkdir -p /backup/files/restic/firma
restic init --repo /backup/files/restic/firma
mkdir -p /etc/restic
echo "dein-passwort" > /etc/restic/password.txt
chmod 600 /etc/restic/password.txt
```

### 4. Konfigurationsdateien anlegen

```bash
cp config/samba.cfg.example config/samba.cfg
cp config/opnsense.cfg.example config/opnsense.cfg
cp config/webui.cfg.example config/webui.cfg
# Werte anpassen:
nano config/samba.cfg
nano config/opnsense.cfg
nano config/webui.cfg
```

### 5. Python-Abhängigkeiten installieren

```bash
# Fuer Backup-Skripte
apt install -y python3-requests

# Fuer WebUI
pip install -r webui/requirements.txt --break-system-packages
```

### 6. Systemd Timer aktivieren

```bash
cp systemd/*.service systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now samba-backup.timer
systemctl enable --now opnsense-backup.timer
```

### 7. WebUI starten (optional)

```bash
cp systemd/restic-webui.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now restic-webui
# Erreichbar unter http://<server-ip>:8080
```

---

## Manuell ausführen

```bash
# Samba-Backup
python3 /opt/firma-backup/scripts/samba_backup.py

# Samba-Backup Dry-Run (kein Snapshot, keine Mail)
python3 /opt/firma-backup/scripts/samba_backup.py --dry-run

# OPNsense-Backup
python3 /opt/firma-backup/scripts/opnsense_backup.py
```

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

```
/var/log/samba-backup.log
/var/log/opnsense-backup.log
```