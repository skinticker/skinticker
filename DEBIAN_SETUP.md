# Deployment auf Debian mit Docker – Schritt für Schritt

Anleitung, um das komplette Projekt (Sniper + Inventar + Dashboard) auf einem
Debian-Server mit Docker zum Laufen zu bringen. Ergebnis: alles läuft dauerhaft
im Hintergrund, das Dashboard ist im Browser unter Port **8080** erreichbar.

Es starten drei Container, die sich eine SQLite-DB (`data/prices.db`) teilen:

| Container | Aufgabe | Intervall |
|---|---|---|
| csfloat-sniper | Watchlist-Preise, DB + Telegram-Alarm | alle 5 Min |
| csfloat-inventory | Inventarbewertung | stündlich |
| csfloat-dashboard | Web-Oberfläche auf Port 8080 | dauerhaft |

---

## 0. Voraussetzungen prüfen (einmalig)

Auf dem Debian-Server einloggen (SSH) und prüfen, ob Docker + Compose da sind:

```bash
docker --version
docker compose version
```

Beide sollten eine Version ausgeben. Falls Docker fehlt, einmalig installieren:

```bash
# Offizielle Docker-Installation (empfohlen)
curl -fsSL https://get.docker.com | sudo sh

# Eigenen Benutzer zur docker-Gruppe hinzufügen (dann kein sudo nötig)
sudo usermod -aG docker $USER
# Danach einmal aus- und wieder einloggen, damit die Gruppe greift.
```

> Hinweis: Das `compose`-Plugin (`docker compose`, mit Leerzeichen) ist bei
> der offiziellen Installation dabei. Das alte `docker-compose` (mit Bindestrich)
> funktioniert auch, ist aber veraltet.

---

## 1. Projekt auf den Server bringen

Den geteilten Ordner auf den Server kopieren. Zwei gängige Wege:

**A) Per SCP vom eigenen PC aus** (Ordner heißt hier `share`):

```bash
scp -r ./share benutzer@SERVER-IP:~/csfloat
```

**B) Als ZIP hochladen und entpacken** (auf dem Server):

```bash
mkdir -p ~/csfloat && cd ~/csfloat
unzip ~/share.zip        # ggf. Pfad anpassen
```

Danach auf dem Server in den Ordner wechseln:

```bash
cd ~/csfloat
ls    # Dockerfile, docker-compose.yml, *.py, dashboard/, data/ ... sollten da sein
```

---

## 2. Eigene `.env` anlegen (Zugangsdaten)

Die App braucht eigene API-Schlüssel. Vorlage kopieren und ausfüllen:

```bash
cp .env.example .env
nano .env
```

In der `.env` eintragen:

- `CSFLOAT_API_KEY` – **eigener** Key aus den CSFloat-Account-Einstellungen (csfloat.com).
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` – optional, für Alarme. Leer lassen = keine Alarme.
- `STEAM_LOGIN_SECURE` – optional, nur für das Steam-Trade-Feature. Leer lassen = Feature aus.

Speichern in `nano`: `Strg+O`, `Enter`, dann `Strg+X`.

> **Wichtig:** Es müssen **eigene** Zugangsdaten sein – nicht die aus einer fremden
> `.env`. Der Steam-Cookie ist praktisch ein Account-Login.

---

## 3. Bauen und starten

```bash
docker compose up -d --build
```

- `--build` baut das Image (beim ersten Mal 1–2 Min: Python + Pakete).
- `-d` startet im Hintergrund (detached).

Prüfen, ob alle drei Container laufen:

```bash
docker compose ps
```

Es sollten `csfloat-sniper`, `csfloat-inventory` und `csfloat-dashboard` mit
Status `Up` erscheinen.

---

## 4. Dashboard öffnen

Im Browser aufrufen:

```
http://SERVER-IP:8080
```

Falls auf dem Server eine Firewall (ufw) aktiv ist, Port 8080 im lokalen Netz
freigeben:

```bash
sudo ufw allow 8080/tcp
```

> Port 8080 **nicht** ungeschützt ins offene Internet stellen. Für Zugriff von
> unterwegs eignet sich z.B. Tailscale oder ein Reverse-Proxy mit Passwort.

---

## 5. Logs & Betrieb

```bash
# Live-Logs aller Container
docker compose logs -f

# Nur den Sniper (zeigt Zeilen wie "CSFloat X EUR | Steam Y EUR | -Z% | GUENSTIG")
docker compose logs -f csfloat-sniper

# Stoppen
docker compose down

# Wieder starten
docker compose up -d
```

Die Container haben `restart: unless-stopped` – sie starten nach einem
Server-Neustart automatisch wieder.

---

## 6. Daten & Updates

- **Alle veränderlichen Daten** (Preis-DB, Bilanz, Kaufpreise, Watchlist,
  Einstellungen …) liegen im Ordner `data/`. Der wird als Volume gemountet und
  bleibt bei Updates erhalten.
- **Backup:** einfach den `data/`-Ordner sichern.
- **Watchlist / Einstellungen:** direkt im Dashboard änderbar – der Sniper liest
  sie bei jedem Durchlauf neu.

**Code-Update einspielen** (neue Dateien in den Ordner kopiert):

```bash
docker compose up -d --build
```

Das baut nur das Image neu; die Daten in `data/` bleiben unangetastet.

---

## Fehlersuche

| Problem | Ursache / Lösung |
|---|---|
| `docker: permission denied` | Benutzer nicht in `docker`-Gruppe → `sudo usermod -aG docker $USER`, neu einloggen. Oder Befehle mit `sudo`. |
| `Temporary failure in name resolution` in den Logs | DNS-Problem im Container. Ist per `dns: [8.8.8.8, 1.1.1.1]` in der `docker-compose.yml` schon abgefangen. Falls es beim `build` klemmt: `/etc/docker/daemon.json` um `{"dns":["8.8.8.8","1.1.1.1"]}` ergänzen, dann `sudo systemctl restart docker`. |
| Dashboard nicht erreichbar | Firewall (`sudo ufw allow 8080/tcp`) und richtige Server-IP prüfen; `docker compose ps` zeigt, ob der Container läuft. |
| Container startet und stoppt sofort | `docker compose logs csfloat-sniper` ansehen – meist fehlt/falsch ist der `CSFLOAT_API_KEY` in der `.env`. |
