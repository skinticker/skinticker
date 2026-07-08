# NAS-Deployment (Synology DS225+, Container Manager)

Anleitung, um das komplette Projekt (Sniper + Inventar + Dashboard) auf dem NAS
in Betrieb zu nehmen. Ergebnis: alles laeuft dauerhaft, das Dashboard ist im
Browser erreichbar, Telegram-Alarme kommen automatisch.

Die drei Dienste teilen sich eine SQLite-Datenbank (`data/prices.db`):

| Dienst | Aufgabe | Intervall |
|---|---|---|
| csfloat-sniper | Watchlist-Preise, DB + Telegram-Alarm | alle 5 Min |
| csfloat-inventory | Inventarbewertung, Snapshot in DB | stuendlich |
| csfloat-dashboard | Web-Oberflaeche auf Port 8080 | dauerhaft |

---

## 1. Vorbereitung auf dem PC

1. Stelle sicher, dass die Werte stimmen:
   - `.env`: `CSFLOAT_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
     `STEAM_LOGIN_SECURE` (optional).
   - `data/settings.json`: Deal- und Alarm-Schwelle (Standard 3 / 10, im Dashboard editierbar).
   - `data/watchlist.json`: die gesuchten Skins.
2. Diese Dateien/Ordner brauchen wir auf dem NAS (alles **ausser** `venv/`):
   - Alle `*.py`-Dateien
   - `dashboard/` (Ordner mit `index.html`)
   - `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `requirements.txt`
   - `.env`  (Geheimnisse – separat behandeln, s.u.)
   - `data/`  ← **wichtig**: enthaelt die DB (`prices.db`, deine gesamte History) UND alle
     veraenderlichen Zustandsdateien (`settings.json`, `watchlist.json`, `buy_prices.json`,
     `auto_buy_prices.json`, `realized_pnl.json`, `steam_trades.json`, `csfloat_hold.json`,
     `excluded_trades.json`, `manual_items.json`, …). Alle drei Container teilen sich dieses
     eine rw-Volume.

> **Migration von einer aelteren Version:** Frueher lagen `settings.json`, `realized_pnl.json`
> usw. im Projekt-Root und wurden per Einzeldatei-Mount eingebunden (teils read-only). Ab
> jetzt gehoeren sie nach `data/`. Vor dem Rebuild auf dem NAS einmalig verschieben:
> `mv settings.json watchlist.json buy_prices.json auto_buy_prices.json realized_pnl.json steam_trades.json csfloat_hold.json excluded_trades.json data/ 2>/dev/null || true`
> (Der Code migriert vorhandene Root-Dateien beim Start zusaetzlich automatisch nach `data/`.)

---

## 2. Dateien auf die NAS kopieren

1. DSM → **File Station** → Ordner `docker/Sniper` (bzw. `/volume1/docker/Sniper`).
2. Die unter Punkt 1 genannten Dateien hochladen.
   - `.env` am besten **direkt auf dem NAS neu anlegen** (nicht ueber
     Cloud/Zwischenablage schicken), Inhalt aus der lokalen Datei kopieren.
   - `data/prices.db` mit hochladen, damit die History vorhanden ist. Wenn der
     Sniper auf dem NAS schon eine eigene `prices.db` angelegt hat: den Container
     vorher stoppen, die Datei ersetzen, dann neu starten.

---

## 3. Einmalige NAS-Konfiguration (falls noch nicht geschehen)

Diese Punkte waren beim ersten Sniper-Deployment noetig – falls schon erledigt,
ueberspringen:

### a) Container Manager installieren
DSM → **Paketzentrum** → „Container Manager" installieren.

### b) Docker-Container brauchen Internet (Firewall)
DSM → **Systemsteuerung → Sicherheit → Firewall**: eine **Zulassen**-Regel
anlegen für Quell-IP `172.17.0.0`, Maske `255.255.0.0`, Aktion „Zulassen",
und diese **ueber** der „Alle verweigern"-Regel platzieren.

### c) DNS für die Container
Synology-Container koennen sonst zeitweise keine Hostnamen aufloesen
(`Temporary failure in name resolution` → csfloat.com/steamcommunity.com nicht
erreichbar). Fix ist bereits in `docker-compose.yml` eingebaut (`dns: [8.8.8.8,
1.1.1.1]` je Dienst) – einfach mit hochladen und neu bauen.

Falls es trotzdem klemmt (z.B. schon beim `pip install` im Build): zusaetzlich
per SSH als root in `/var/packages/ContainerManager/etc/dockerd.json` den
Eintrag `"dns":["8.8.8.8","1.1.1.1"]` ergaenzen und Container Manager im
Paketzentrum neu starten (setzt DNS auch fuer die Build-Phase).

### d) Dashboard-Port im Heimnetz erreichbar
Damit du das Dashboard per Browser oeffnen kannst, muss dein Heimnetz-Subnetz
den Port **8080** erreichen duerfen. In derselben Firewall eine Zulassen-Regel
für Quell-IP `192.168.178.0`, Maske `255.255.255.0` (dein Router-Subnetz ggf.
anpassen), Port 8080 – oder allgemein dein LAN erlauben.

---

## 4. Projekt starten

1. Container Manager → **Projekt** → **Erstellen**.
2. Pfad auf `/volume1/docker/Sniper` zeigen lassen (erkennt `docker-compose.yml`).
3. **Ausführen**. Beim ersten Mal dauert der Build 1–2 Minuten (Python + Pakete).
4. Es sollten drei Container starten: `csfloat-sniper`, `csfloat-inventory`,
   `csfloat-dashboard`.
5. Logs pruefen: Container Manager → jeweiligen Container → **Protokoll**.
   Beim Sniper sollten Zeilen wie
   `... CSFloat X EUR | Steam Y EUR | -Z% | GUENSTIG` erscheinen.

---

## 5. Zugriff & Test

- **Dashboard im Heimnetz:** `http://<NAS-IP>:8080`
- **Unterwegs (Tailscale):** dieselbe URL über die Tailscale-IP des NAS –
  kein Port muss ins offene Internet.
- **Telegram testen:** im Dashboard unter „Steuerung" auf „Telegram testen".
- **Einstellungen/Watchlist:** direkt im Dashboard aendern – der Sniper liest
  `settings.json` und `watchlist.json` bei jedem Durchlauf neu.

---

## 6. Nicht vergessen

- **Telegram-Token** rotieren, falls er je geteilt wurde (BotFather → /revoke),
  neuen Wert in die `.env` auf dem NAS eintragen und den Sniper-Container neu
  starten.
- **Backups:** Das gesamte `data/`-Verzeichnis sollte gelegentlich gesichert
  werden (Hyper Backup oder einfache Kopie) – dort liegen die Preis-Historie
  (`prices.db`) sowie alle Zustandsdateien (Bilanz, Kaufpreise, manuelle Liste …).

---

## Updates einspielen

Wenn sich Code aendert:
1. Geaenderte Dateien nach `/volume1/docker/Sniper` kopieren (ueberschreiben).
2. Container Manager → Projekt → **Erstellen/Neu bauen** (Rebuild), dann starten.
Die Zustandsdateien in `data/` (`settings.json`, `watchlist.json`, `realized_pnl.json`, …)
werden **nicht** neu gebaut – sie liegen im `data/`-Volume und bleiben erhalten (inkl.
deiner Dashboard-Aenderungen, Kaufpreise und Trade-Ausschluesse).
