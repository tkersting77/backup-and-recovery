# Restic Backup – Firma Samba & OPNsense

Kurzreferenz der wichtigsten Restic-Befehle für das Backup-Setup.

## Konfiguration

```
Repository:      /backup/files/restic/firma
Passwort-Datei:  /etc/restic/password.txt
Tags:             samba | opnsense
```

Alle Befehle unten gehen davon aus, dass folgende Variablen gesetzt sind (optional, spart Tipparbeit):

```bash
export RESTIC_REPOSITORY=/backup/files/restic/firma
export RESTIC_PASSWORD_FILE=/etc/restic/password.txt
```

Ohne diese Variablen muss bei jedem Befehl `-r <repo> --password-file <datei>` ergänzt werden.

---

## Snapshots anzeigen

```bash
# Alle Snapshots
restic snapshots

# Nur Samba-Snapshots
restic snapshots --tag samba

# Nur OPNsense-Snapshots
restic snapshots --tag opnsense

# Nur die letzten 5 Snapshots
restic snapshots --latest 5
```

---

## In einem Snapshot suchen

```bash
# Datei/Ordner über alle Snapshots suchen
restic find "rechnung*.pdf"

# Suche auf einen Tag einschränken
restic find --tag samba "*.xlsx"

# Nur in einem bestimmten Snapshot suchen
restic find --snapshot <snapshot-id> "*.docx"

# Nach Pfad eingrenzen
restic find --path "/srv/samba/groups/buchhaltung" "*.pdf"

# Mit vollem Pfad + Snapshot-ID anzeigen (für direkten Restore)
restic find --long "rechnung*.pdf"
```

---

## Inhalt eines Snapshots durchsehen

```bash
# Wurzelverzeichnis eines Snapshots auflisten
restic ls <snapshot-id>

# Rekursiv mit vollständigen Pfaden
restic ls <snapshot-id> -l

# Bestimmten Unterordner auflisten
restic ls <snapshot-id> /srv/samba/users/max
```

---

## Wiederherstellen (Restore)

**Immer zuerst in ein separates Verzeichnis restoren, nie direkt auf Produktivpfade!**

```bash
# Letzten Snapshot komplett wiederherstellen
restic restore latest --target /tmp/restore

# Bestimmten Snapshot wiederherstellen
restic restore <snapshot-id> --target /tmp/restore

# Nur einen Ordner/Datei aus einem Snapshot wiederherstellen
restic restore <snapshot-id> --target /tmp/restore \
  --include "/srv/samba/groups/buchhaltung/rechnung-2026.pdf"

# Nur Samba-Snapshots berücksichtigen (z.B. bei "latest")
restic restore latest --tag samba --target /tmp/restore
```

Nach dem Restore: Inhalt prüfen (`ls -la /tmp/restore/...`), erst danach manuell an den
finalen Zielort kopieren (z.B. per `cp` oder `rsync` auf den Samba-Server).

---

## Retention / Aufräumen (Forget + Prune)

```bash
# Trockenlauf – zeigt was gelöscht würde, ohne etwas zu löschen
restic forget --tag samba \
  --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --keep-yearly 2 \
  --dry-run

# Tatsächlich anwenden + nicht mehr referenzierte Daten löschen
restic forget --tag samba \
  --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --keep-yearly 2 \
  --prune
```

| Policy | Bedeutung |
|---|---|
| `--keep-daily 7` | letzte 7 Tage, je 1 Snapshot/Tag |
| `--keep-weekly 4` | letzte 4 Wochen, je 1 Snapshot/Woche |
| `--keep-monthly 12` | letzte 12 Monate, je 1 Snapshot/Monat |
| `--keep-yearly 2` | letzte 2 Jahre, je 1 Snapshot/Jahr |

---

## Repository-Wartung

```bash
# Integrität des Repos prüfen
restic check

# Inklusive Lesetest aller Datenblöcke (dauert länger, gründlicher)
restic check --read-data

# Speicherplatz-Statistik
restic stats

# Speicherplatz-Statistik pro Snapshot (roh, ohne Dedup-Effekt)
restic stats --mode raw-data
```

---

## Manuelles Backup anstoßen (außerhalb der Skripte)

```bash
# Samba-Backup manuell auslösen
python3 /opt/firma-backup/samba_backup.py

# OPNsense-Backup manuell auslösen
python3 /opt/firma-backup/opnsense_backup.py
```

---

## Disaster Recovery – Kurzablauf

1. Restic auf neuem System installieren
2. VPN-Verbindung zum Heimserver/Repo sicherstellen
3. Passwort aus sicherem Speicherort holen (Passwort-Manager / Ausdruck)
4. Restore in temporäres Verzeichnis:
   ```bash
   restic restore latest --tag samba --target /tmp/restore
   ```
5. Daten per `rsync`/`scp` auf den (neuen) Samba-Server übertragen
6. ACL-Cronjob einmal manuell ausführen
7. Stichprobenartig Dateien/Berechtigungen prüfen

---

## Wichtige Logs

```
/var/log/samba-backup.log
/var/log/opnsense-backup.log
```
