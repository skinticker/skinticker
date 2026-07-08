"""Steam-Tauschgeschaefte aus der Inventory-History (eingeloggte Session).

Steams Web-API (GetTradeHistory) liefert fuer normale Keys KEINE Item-Inhalte
mehr - nur Metadaten. Deshalb lesen wir die Inventory-History-Seite selbst
(ajax=1), die die tatsaechlich getauschten Items enthaelt. Dafuer braucht es den
eingeloggten `steamLoginSecure`-Cookie (STEAM_LOGIN_SECURE in der .env).

Jede abgeschlossene Tausch-Zeile ("Traded") hat eine +-Gruppe (erhalten) und ggf.
eine --Gruppe (gegeben). Zusammen mit unseren predicted prices ergibt das die
Bilanz je Trade und die Kostenbasis ertradeter Items.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

CS2_APPID = "730"
UA = "Mozilla/5.0 (CSFloat-Sniper Python-Skript)"


def is_configured() -> bool:
    return bool(os.getenv("STEAM_LOGIN_SECURE"))


def _steamid_from_cookie(cookie: str) -> str | None:
    """steamLoginSecure beginnt mit der 17-stelligen SteamID64."""
    m = re.match(r"(\d{17})", cookie or "")
    return m.group(1) if m else None


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _parse_time(date_text: str, time_text: str) -> int:
    """'5 Jul, 2026' + '11:57am' -> Unix-Zeit (als UTC genaehert)."""
    try:
        dt = datetime.strptime(f"{date_text.strip()} {time_text.strip()}", "%d %b, %Y %I:%M%p")
        return int(dt.replace(tzinfo=timezone.utc).timestamp())
    except (ValueError, AttributeError):
        return 0


def _parse_items(group_html: str, descriptions: dict) -> list[dict]:
    """Items einer +/- Gruppe: Name (voller market_hash_name aus descriptions) + Menge."""
    bucket: dict[str, dict] = {}
    for a in re.split(r"(?=<a )", group_html):
        appid = re.search(r'data-appid="(\d+)"', a)
        cid = re.search(r'data-classid="(\d+)"', a)
        iid = re.search(r'data-instanceid="(\d+)"', a)
        if not (appid and cid and iid) or appid.group(1) != CS2_APPID:
            continue
        amount = re.search(r'data-amount="(\d+)"', a)
        qty = int(amount.group(1)) if amount else 1
        key = f"{cid.group(1)}_{iid.group(1)}"
        name = (descriptions.get(key, {}) or {}).get("market_hash_name")
        if not name:  # Fallback auf den Anzeigenamen (ohne Wear) im Span
            span = re.search(r'history_item_name[^>]*>(.*?)</span>', a, re.S)
            name = _strip_tags(span.group(1)) if span else None
        if not name:
            continue
        # Item-Bild-Hash (zwischen /economy/image/ und der Groessenangabe) fuer Hold-Items
        img = re.search(r'/economy/image/([^/"]+)', a)
        b = bucket.setdefault(name, {"qty": 0, "icon": img.group(1) if img else None})
        b["qty"] += qty
    return [{"name": n, "qty": b["qty"], "icon": b["icon"]} for n, b in bucket.items()]


def parse_inventory_history(html: str, descriptions: dict) -> list[dict]:
    """Parst die "Traded"-Zeilen (vollstaendige Tauschgeschaefte) aus dem History-HTML.

    Die "You traded with X"-Zeilen sind received-only Duplikate der jeweils folgenden
    "Traded"-Zeile und werden bewusst uebersprungen.
    """
    trades = []
    for row in html.split('<div class="tradehistoryrow">')[1:]:
        ev = re.search(r'tradehistory_event_description">(.*?)</div>', row, re.S)
        event = _strip_tags(ev.group(1)) if ev else ""
        if event != "Traded":
            continue

        given, received = [], []
        for sign, grp in re.findall(
            r'tradehistory_items_plusminus">([^<]*)</div>\s*<div class="tradehistory_items_group">(.*?)</div>',
            row, re.S,
        ):
            items = _parse_items(grp, descriptions)
            (received if sign.strip() == "+" else given).extend(items)
        # Nur echte Item-fuer-Item-Trades: beide Seiten muessen Items haben.
        # Einseitige Eintraege (Geschenke, Markt, unvollstaendig erfasst) fallen raus.
        if not given or not received:
            continue

        dm = re.search(r'tradehistory_date">\s*(.*?)<div class="tradehistory_timestamp">(.*?)</div>', row, re.S)
        ts = _parse_time(_strip_tags(dm.group(1)), _strip_tags(dm.group(2))) if dm else 0
        hid = re.search(r'id="history([0-9a-f]+)_', row)
        trades.append({
            "trade_id": hid.group(1) if hid else f"t{ts}",
            "time": ts,
            "given": given,
            "received": received,
        })
    return trades


def fetch_trades(cookie: str | None = None, max_pages: int = 8) -> list[dict]:
    """Laedt die Inventory-History (paginiert) und liefert die Tausch-Trades.

    Leere Liste bei fehlendem Cookie oder Fehler (z.B. Cookie abgelaufen -> 401/HTML).
    """
    cookie = cookie or os.getenv("STEAM_LOGIN_SECURE")
    if not cookie:
        return []
    sid = _steamid_from_cookie(cookie)
    if not sid:
        logger.error("STEAM_LOGIN_SECURE hat kein erkennbares SteamID64-Praefix - Cookie ungueltig?")
        return []

    url = f"https://steamcommunity.com/profiles/{sid}/inventoryhistory/"
    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    session.cookies.set("steamLoginSecure", cookie, domain="steamcommunity.com")

    all_trades: dict[str, dict] = {}
    cursor = None
    for page in range(max_pages):
        params = {"ajax": 1, "app[]": 730, "l": "english"}
        if cursor:
            params["cursor[time]"] = cursor.get("time")
            params["cursor[time_frac]"] = cursor.get("time_frac", 0)
            params["cursor[s]"] = cursor.get("s")
        try:
            r = session.get(url, params=params, timeout=25)
        except requests.RequestException as exc:
            logger.warning("Steam-InventoryHistory Netzwerkfehler (Seite %s): %s", page, exc)
            break
        ctype = r.headers.get("content-type", "")
        if r.status_code != 200 or "json" not in ctype:
            # Nicht-JSON: entweder ausgeloggt (Login-Seite) oder Rate-Limit.
            # Steam setzt im Seiten-JS "g_steamID = false;" wenn NICHT eingeloggt.
            body = r.text or ""
            if "g_steamID = false" in body or "<title>Sign In</title>" in body:
                logger.error("Steam-InventoryHistory: nicht eingeloggt (HTTP %s) - "
                             "STEAM_LOGIN_SECURE abgelaufen/ungueltig, bitte neuen Cookie eintragen.", r.status_code)
            else:
                logger.warning("Steam-InventoryHistory: kein JSON (HTTP %s) - vermutlich Steam-Rate-Limit, "
                               "spaeter erneut versuchen.", r.status_code)
            break
        data = r.json()
        if not data.get("success"):
            logger.warning("Steam-InventoryHistory: success=false (Seite %s).", page)
            break
        descriptions = (data.get("descriptions") or {}).get(CS2_APPID, {}) or {}
        for t in parse_inventory_history(data.get("html", "") or "", descriptions):
            all_trades[t["trade_id"]] = t  # dedupe ueber Trade-Hash
        cursor = data.get("cursor")
        if not cursor:
            break
        time.sleep(1.5)  # Steam schont, nicht hammern

    trades = sorted(all_trades.values(), key=lambda x: x["time"] or 0, reverse=True)
    logger.info("Steam-InventoryHistory: %s Tausch-Trades geladen (%s Seiten).", len(trades), page + 1)
    return trades


def merge_trades(existing: list[dict], fetched: list[dict]) -> list[dict]:
    """Vereinigt bereits gespeicherte und frisch geladene Trades ueber die trade_id.

    Trades sind unveraenderliche Historie - einmal erkannt, gehen sie nicht mehr verloren.
    Das schuetzt vor Datenverlust, wenn ein Abruf leer/unvollstaendig zurueckkommt (z.B.
    abgelaufener Cookie oder Steam-Rate-Limit): der gespeicherte Bestand bleibt erhalten,
    neue Trades kommen dazu. Bei Duplikaten gewinnt die frisch geladene Version.
    """
    by_id: dict[str, dict] = {}
    for t in existing or []:
        tid = t.get("trade_id")
        if tid:
            by_id[tid] = t
    for t in fetched or []:
        tid = t.get("trade_id")
        if tid:
            by_id[tid] = t
    return sorted(by_id.values(), key=lambda x: x.get("time") or 0, reverse=True)


def enrich_with_prices(trades: list[dict], price_eur_by_name: dict[str, float]) -> list[dict]:
    """Setzt je Item den 'suggested_eur' aus der CSFloat-Preisliste (min_price).
    So sind auch frisch ertradete Items (7-Tage-Hold, noch nicht im Inventar) bewertet."""
    for t in trades:
        for it in t.get("given", []) + t.get("received", []):
            p = price_eur_by_name.get(it["name"])
            if p is not None:
                it["suggested_eur"] = round(p, 2)
    return trades


def compute_cost_basis(valued_trades: list[dict]) -> dict[str, dict]:
    """Verteilt je Item-fuer-Item-Trade den gegebenen Wert anteilig (nach Wert) auf die
    erhaltenen Items -> EK pro Stueck. Ergebnis: {name: {price_eur, buy_date}}.

    Nur bei vollstaendig bepreisten Trades (sonst faelsche Verteilung). Damit ist die
    Bilanz gegengerechnet: Summe der EK = gegebener Wert, Differenz zum aktuellen Wert
    = Trade-Gewinn/-Verlust.
    """
    from datetime import datetime, timezone
    out: dict[str, dict] = {}
    for t in valued_trades:
        if not t.get("priced") or t.get("given_eur", 0) <= 0 or t.get("received_eur", 0) <= 0:
            continue
        day = (datetime.fromtimestamp(t["time"], timezone.utc).date().isoformat()
               if t.get("time") else None)
        for it in t["received"]:
            if not it.get("value_eur") or it.get("qty", 0) <= 0:
                continue
            per_unit = t["given_eur"] * (it["value_eur"] / t["received_eur"]) / it["qty"]
            out[it["name"]] = {"price_eur": round(per_unit, 2), "buy_date": day}
    return out


def value_trades(trades: list[dict], price_eur_by_name: dict[str, float] | None = None) -> list[dict]:
    """Bewertet jede Trade-Seite und berechnet die Bilanz = erhalten - gegeben.

    Preis je Item: bevorzugt der zum Sync gespeicherte CSFloat-'suggested_eur'
    (min_price aus der Preisliste), sonst der zuletzt bekannte Inventar-Preis.
    """
    price_eur_by_name = price_eur_by_name or {}

    def _side(items: list[dict]) -> tuple[float, list[dict], bool]:
        total = 0.0
        enriched = []
        complete = True
        for it in items:
            price = it.get("suggested_eur")
            if price is None:
                price = price_eur_by_name.get(it["name"])
            if price is None:
                complete = False
            qty = it.get("qty", 1)
            value = (price or 0) * qty
            total += value
            enriched.append({"name": it["name"], "qty": qty,
                             "eur_each": round(price, 2) if price is not None else None,
                             "value_eur": round(value, 2) if price is not None else None})
        return round(total, 2), enriched, complete

    out = []
    for t in trades:
        g_val, given, g_ok = _side(t["given"])
        r_val, received, r_ok = _side(t["received"])
        out.append({
            "trade_id": t["trade_id"],
            "time": t["time"],
            "given": given,
            "received": received,
            "given_eur": g_val,
            "received_eur": r_val,
            "balance_eur": round(r_val - g_val, 2),
            # priced=False, wenn fuer mind. ein Item kein Preis bekannt war (Bilanz unvollstaendig)
            "priced": g_ok and r_ok,
        })
    out.sort(key=lambda x: x["time"] or 0, reverse=True)
    return out
