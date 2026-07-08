"""Dashboard-Server: liefert dashboard/index.html und generiert /data.json
live aus der SQLite-Datenbank. Nur Python-Standardbibliothek, kein Framework.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv

import config
import csfloat_balance
import db
import steam_trades
import telegram_notify

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

DB_PATH = Path("data/prices.db")
INDEX_PATH = Path("dashboard/index.html")
CACHE_SECONDS = 60

_cache: dict = {"at": 0.0, "payload": None}

# Manueller Sniper-Lauf aus dem Dashboard: nur einer zur Zeit, Client wiederverwenden
# (haelt Session + Rate-Limit-Stand warm und aktualisiert data/rate_limits.json mit).
_sniper_lock = threading.Lock()
_sniper_client = None


def _get_sniper_client():
    global _sniper_client
    if _sniper_client is None:
        api_key = os.getenv("CSFLOAT_API_KEY")
        if not api_key:
            return None
        from csfloat_client import CSFloatClient
        _sniper_client = CSFloatClient(api_key=api_key)
    return _sniper_client


def _daily_series(conn, query: str, params=()) -> list:
    """Fuehrt eine Query aus, die pro Tag den letzten Wert liefert."""
    return [list(r) for r in conn.execute(query, params)]


PERF_PERIODS = {"24h": 1, "7d": 7, "30d": 30}


def _pct_from_series(series: list, cutoff_iso: str) -> float | None:
    """series: [(ts, value), ...] aufsteigend. %-Aenderung letzter Wert vs. Wert bei/vor cutoff."""
    if len(series) < 2:
        return None
    latest_v = series[-1][1]
    past_v = None
    for ts, v in series:
        if ts <= cutoff_iso:
            past_v = v
        else:
            break
    if not past_v or past_v <= 0:
        return None
    return (latest_v - past_v) / past_v * 100


def _build_performers(conn) -> dict:
    """Top-5-Items je Zeitraum nach %-Wertentwicklung, plus Mini-Sparkline.

    Basis ist der per-Item-Verlauf (inventory_items). Der fuellt sich erst seit
    Einfuehrung der Python-Bewertung - fuer 7d/30d braucht es entsprechend Historie.
    """
    now = datetime.now(timezone.utc)
    cutoffs = {k: (now - timedelta(days=d)).isoformat() for k, d in PERF_PERIODS.items()}

    by_item: dict[str, dict] = {}
    for name, ts, eur, cat, icon in conn.execute(
        "SELECT market_hash_name, ts, eur_each, category, icon_url FROM inventory_items "
        "WHERE eur_each IS NOT NULL ORDER BY ts"
    ):
        d = by_item.setdefault(name, {"series": [], "category": cat, "icon": icon})
        d["series"].append((ts, eur))
        d["category"], d["icon"] = cat, icon  # jeweils juengsten Stand behalten

    performers: dict[str, list] = {}
    for pk, cutoff in cutoffs.items():
        rows = []
        for name, d in by_item.items():
            pct = _pct_from_series(d["series"], cutoff)
            if pct is None or abs(pct) < 0.05:
                continue
            rows.append({
                "name": name, "category": d["category"], "pct": round(pct, 2),
                "current_eur": round(d["series"][-1][1], 2), "icon_url": d["icon"],
                "spark": [round(v, 2) for _, v in d["series"][-12:]],
            })
        rows.sort(key=lambda x: x["pct"], reverse=True)
        # Alle bewegten Items senden (nicht nur Top 5) - das Dashboard filtert clientseitig
        # nach Preis-Kategorie und zeigt dann die Top 5 der jeweiligen Auswahl.
        performers[pk] = rows
    return performers


HOLD_DISPLAY_DAYS = 7  # Steam-Trade-Hold; CSFloat liefert hold_until direkt mit.


def _hold_expired(hold_until, now: datetime) -> bool:
    """True, wenn das Hold-Ende in der Vergangenheit liegt. Ohne Datum: nicht abgelaufen."""
    if not hold_until:
        return False
    try:
        return datetime.fromisoformat(str(hold_until).replace("Z", "+00:00")) < now
    except ValueError:
        return False


def _build_hold_arrivals(steam_trade_data: dict, price_map: dict) -> list:
    """Items, die aktuell im Trade-Hold stecken: auf CSFloat gekauft (Escrow) oder via
    Steam ertradet (7-Tage-Hold). Wert = aktueller Inventarpreis, sonst Suggested/EK.
    Abgelaufene Holds werden weggelassen - die Items sind dann normal handelbar und
    gehoeren nicht mehr in die Neuzugangs-Anzeige.
    """
    from inventory import detect_category

    now = datetime.now(timezone.utc)
    out = []

    # 1) Auf CSFloat gekaufte Items im Escrow/Hold (mit hold_until von CSFloat).
    for h in config.load_csfloat_hold().get("items", []):
        name = h.get("name")
        if not name or _hold_expired(h.get("hold_until"), now):
            continue
        eur_each = price_map.get(name) or h.get("suggested_eur") or h.get("buy_eur")
        out.append({
            "market_hash_name": name, "category": detect_category(name),
            "icon_url": h.get("icon"), "eur_each": eur_each, "qty": h.get("qty", 1),
            "ek_each": h.get("buy_eur"),  # CSFloat-Kaufpreis (EK)
            "hold_until": h.get("hold_until"), "source": "csfloat", "since": h.get("buy_date"),
        })

    # 2) Via Steam ertradete Items im 7-Tage-Hold (Hold-Ende = Trade-Zeit + 7 Tage).
    for t in steam_trade_data.get("trades", []):
        ttime = t.get("time")
        if not (t.get("received") and ttime):
            continue
        trade_dt = datetime.fromtimestamp(ttime, timezone.utc)
        hold_until = (trade_dt + timedelta(days=HOLD_DISPLAY_DAYS)).isoformat()
        if _hold_expired(hold_until, now):
            continue
        for it in t["received"]:
            name = it.get("name")
            if not name:
                continue
            eur_each = price_map.get(name)
            if eur_each is None:
                eur_each = it.get("suggested_eur")
            out.append({
                "market_hash_name": name, "category": detect_category(name),
                "icon_url": it.get("icon"), "eur_each": eur_each, "qty": it.get("qty", 1),
                "ek_each": None,  # bei Tausch keine direkte Kostenbasis -> aus buy_prices ergaenzen
                "hold_until": hold_until, "source": "trade", "since": trade_dt.isoformat(),
            })

    out.sort(key=lambda x: x.get("hold_until") or "")  # naechster Hold-Ablauf zuerst
    return out


def _trade_key(t: dict, occ: int) -> str:
    """Stabiler Schluessel je realisiertem Trade - unabhaengig vom FX-Kurs (nutzt
    Name, Kauf-/Verkaufsdatum und die fx-unabhaengige Rendite%). occ nummeriert
    identische Trades durch, damit Duplikate getrennt schaltbar bleiben."""
    return f"{t.get('name')}|{t.get('buy_date')}|{t.get('sell_date')}|{t.get('profit_pct')}#{occ}"


def _recompute_realized(pnl_cache: dict, excluded_set: set) -> dict:
    """Annotiert jeden Trade mit key + excluded und berechnet die Kennzahlen NUR aus
    den nicht ausgeschlossenen Trades neu (z.B. geliehene Items zaehlen nicht mit)."""
    trades = [dict(t) for t in pnl_cache.get("trades", [])]
    seen: dict[str, int] = {}
    for t in trades:
        base = f"{t.get('name')}|{t.get('buy_date')}|{t.get('sell_date')}|{t.get('profit_pct')}"
        occ = seen.get(base, 0)
        seen[base] = occ + 1
        t["key"] = f"{base}#{occ}"
        t["excluded"] = t["key"] in excluded_set
    incl = [t for t in trades if not t["excluded"]]
    return {
        **pnl_cache,
        "trades": trades,  # alle (annotiert), damit das Dashboard sie ein-/ausschalten kann
        "trade_count": len(incl),
        "realized_total_eur": round(sum(t.get("profit_eur") or 0 for t in incl), 2),
        "invested_eur": round(sum(t.get("buy_eur") or 0 for t in incl), 2),
        "revenue_eur": round(sum(t.get("sell_eur") or 0 for t in incl), 2),
        "best": max(incl, key=lambda x: x.get("profit_eur") or 0, default=None),
        "worst": min(incl, key=lambda x: x.get("profit_eur") or 0, default=None),
        "excluded_count": len(trades) - len(incl),
    }


def _change_since(history: list, hours: float) -> float | None:
    """Prozentuale Aenderung des letzten Werts gegenueber dem Wert vor X Stunden."""
    if len(history) < 2:
        return None
    latest_ts, latest_val = history[-1][0], history[-1][1]
    cutoff = (datetime.fromisoformat(latest_ts.replace("Z", "+00:00")).replace(tzinfo=None)
              - timedelta(hours=hours)).isoformat()
    past_val = None
    for ts, val, *_ in history:
        if ts[:19] <= cutoff[:19]:
            past_val = val
        else:
            break
    if not past_val:
        return None
    return (latest_val - past_val) / past_val * 100


def _auth_required() -> bool:
    """True, wenn DASHBOARD_USER + DASHBOARD_PASSWORD in der .env gesetzt sind."""
    return bool(os.getenv("DASHBOARD_USER") and os.getenv("DASHBOARD_PASSWORD"))


def _check_basic_auth(header: str | None) -> bool:
    """Prueft einen 'Authorization: Basic ...'-Header gegen die .env-Zugangsdaten.
    Vergleich mit compare_digest (konstante Zeit) gegen Timing-Angriffe."""
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        user, _, password = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return (hmac.compare_digest(user, os.getenv("DASHBOARD_USER", ""))
            and hmac.compare_digest(password, os.getenv("DASHBOARD_PASSWORD", "")))


def build_data() -> dict:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        # Gesamtwert-Verlauf: ein Punkt pro Tag (letzter Wert des Tages), EUR + USD + Items
        history = _daily_series(conn, """
            SELECT ts, sum_csfloat_eur, sum_csfloat_usd, total_items
            FROM inventory_history
            WHERE ts IN (SELECT MAX(ts) FROM inventory_history GROUP BY date(ts))
              AND sum_csfloat_eur IS NOT NULL
            ORDER BY ts
        """)

        category_history: dict[str, list] = {}
        for cat in ["Skins", "Cases", "Sticker", "Agents", "Charms", "Andere"]:
            rows = _daily_series(conn, """
                SELECT ts, csfloat_eur
                FROM inventory_history_category
                WHERE category = ?
                  AND ts IN (SELECT MAX(ts) FROM inventory_history_category WHERE category = ? GROUP BY date(ts))
                  AND csfloat_eur IS NOT NULL
                ORDER BY ts
            """, (cat, cat))
            if any(v for _, v in rows):
                category_history[cat] = rows

        latest_item_ts = conn.execute("SELECT MAX(ts) FROM inventory_items").fetchone()[0]
        items = []
        if latest_item_ts:
            cols = ["market_hash_name", "category", "qty", "usd_each", "eur_each", "steam_eur_each",
                    "float_value", "icon_url", "inspect_link", "rarity", "is_stattrak", "is_souvenir",
                    "in_csfloat", "manual_qty", "stickers", "pending_trade"]
            for row in conn.execute(
                f"SELECT {', '.join(cols)} FROM inventory_items WHERE ts = ? ORDER BY eur_each * qty DESC",
                (latest_item_ts,),
            ):
                it = dict(zip(cols, row))
                try:
                    it["stickers"] = json.loads(it["stickers"]) if it.get("stickers") else []
                except (json.JSONDecodeError, TypeError):
                    it["stickers"] = []
                items.append(it)

        # Wertaenderung je Item (aktuell vs. aeltester Snapshot der letzten 7 Tage)
        item_changes: dict[str, float] = {}
        if latest_item_ts:
            week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            for name, first_usd, last_usd in conn.execute("""
                SELECT market_hash_name,
                       (SELECT usd_each FROM inventory_items i2
                         WHERE i2.market_hash_name = i1.market_hash_name AND i2.ts >= ?
                         ORDER BY ts LIMIT 1),
                       usd_each
                FROM inventory_items i1
                WHERE ts = ?
            """, (week_ago, latest_item_ts)):
                if first_usd and last_usd and first_usd > 0:
                    change = (last_usd - first_usd) / first_usd * 100
                    if abs(change) > 0.05:
                        item_changes[name] = round(change, 2)

        # A7: Top-Performer je Zeitraum (aus dem per-Item-Verlauf, keine neuen API-Calls)
        performers = _build_performers(conn)

        # Steam-Trades bewerten - liefern die Kostenbasis ertradeter Items, die Info,
        # welche Abgaenge Tausch (kein Cash-Out) waren, UND den Netto-Saldo der
        # Tauschgeschaefte fuer die Investment-Bilanz. Preis-Map = zuletzt bekannter
        # EUR-Preis je Item (deckt aktuelle wie abgegangene Items ab).
        price_map = {}
        for pname, peur in conn.execute("""
            SELECT market_hash_name, eur_each FROM inventory_items i1
            WHERE eur_each IS NOT NULL
              AND ts = (SELECT MAX(ts) FROM inventory_items i2 WHERE i2.market_hash_name = i1.market_hash_name)
        """):
            price_map[pname] = peur
        steam_trade_data = config.load_steam_trades()
        steam_trades_valued = steam_trades.value_trades(steam_trade_data.get("trades", []), price_map)

        # Aus der Statistik ausgeschlossene Trades (z.B. geliehene Items). Ein Set fuer
        # realisierte Trades (Key = name|kauf|verkauf|pct#n) UND Tausch-Trades (barter:<id>).
        excluded_set = set(config.load_excluded_trades())
        for t in steam_trades_valued:
            t["key"] = f"barter:{t.get('trade_id')}"
            t["excluded"] = t["key"] in excluded_set
        # Items, die in ausgeschlossenen Tausch-Trades erhalten wurden - ihre synthetische
        # Kostenbasis (EK) wird unten neutralisiert, damit sie Unrealisiert/EK nicht verzerrt.
        excluded_barter_received = {it["name"] for t in steam_trades_valued
                                    if t.get("excluded") for it in t["received"]}

        # Neuzugaenge = NUR Items im aktuellen Trade-Hold (auf CSFloat gekauft & noch im
        # Escrow ODER via Steam ertradet & noch im 7-Tage-Hold). Nach Ablauf des Holds
        # sind sie normal handelbar und verschwinden hier automatisch.
        new_arrivals = _build_hold_arrivals(steam_trade_data, price_map)

        # C1: Kaufpreise. Automatik-Datei enthaelt CSFloat-Kaeufe UND die persistierte
        # Trade-Kostenbasis (source je Eintrag); manuell hat immer Vorrang.
        auto_bp = config.load_auto_buy_prices().get("prices", {})
        manual_bp = config.load_buy_prices()
        buy_prices = {}
        for name, d in auto_bp.items():
            buy_prices[name] = {"price_eur": d.get("price_eur"), "buy_date": d.get("buy_date"),
                                "source": d.get("source", "csfloat")}
        for name, d in manual_bp.items():
            buy_prices[name] = {"price_eur": d.get("price_eur"), "buy_date": d.get("buy_date"), "source": "manual"}

        # Kostenbasis aus ausgeschlossenen Tausch-Trades entfernen (z.B. geliehenes Item
        # weitergegeben) - sonst verzerrt die synthetische Trade-EK Unrealisiert & Neuzugang-EK.
        for name in excluded_barter_received:
            bp = buy_prices.get(name)
            if bp and bp.get("source") == "trade":
                buy_prices.pop(name, None)

        # Neuzugaenge (Trade-Hold) um EK + Gewinn/Verlust ergaenzen. EK aus dem Hold-Kaufpreis
        # (CSFloat) bzw. der persistierten Kostenbasis (Tausch) via buy_prices.
        for a in new_arrivals:
            if a.get("ek_each") is None:
                bp = buy_prices.get(a["market_hash_name"])
                if bp and bp.get("price_eur"):
                    a["ek_each"] = bp["price_eur"]
            ek, cur = a.get("ek_each"), a.get("eur_each")
            if ek and cur is not None:
                a["profit_each"] = round(cur - ek, 2)
                a["profit_pct"] = round((cur - ek) / ek * 100, 1)
            else:
                a["profit_each"] = None
                a["profit_pct"] = None

        # Investment-Bilanz ("wie ich investiert habe") = Gesamtergebnis aus drei Quellen:
        #   1) realisierte Gewinne aus der CSFloat-Trade-History (Kauf/Verkauf),
        #   2) Netto-Saldo der Steam-Tauschgeschaefte,
        #   3) unrealisiertes Delta (aktueller Wert - EK) ueber alle gehaltenen Items mit EK.
        realized_view = _recompute_realized(config.load_realized_pnl(), excluded_set)
        realized_pnl_eur = round(realized_view.get("realized_total_eur") or 0.0, 2)
        barter_eur = round(sum(t.get("balance_eur") or 0
                               for t in steam_trades_valued if not t.get("excluded")), 2)
        ek_sum = cur_sum = 0.0
        items_with_ek = 0
        for it in items:
            bp = buy_prices.get(it["market_hash_name"])
            if bp and bp.get("price_eur"):
                ek_sum += bp["price_eur"] * it["qty"]
                cur_sum += (it["eur_each"] or 0) * it["qty"]
                items_with_ek += 1
        unrealized_eur = round(cur_sum - ek_sum, 2)
        investment = {
            "realized_eur": realized_pnl_eur,
            "barter_eur": barter_eur,
            "unrealized_eur": unrealized_eur,
            "ek_total_eur": round(ek_sum, 2),
            "current_of_ek_eur": round(cur_sum, 2),
            "items_with_ek": items_with_ek,
            "total_eur": round(realized_pnl_eur + barter_eur + unrealized_eur, 2),
        }

        # Gesamtergebnis einmal taeglich festhalten (letzter Wert des Tages gewinnt)
        # und die Tages-Historie fuer den Verlaufs-Chart mitliefern. Best effort -
        # ein Schreibfehler darf data.json nicht kippen.
        try:
            db.save_investment_snapshot(conn, investment)
        except Exception:
            logger.exception("Investment-Snapshot konnte nicht gespeichert werden")
        try:
            investment_history = db.load_investment_history(conn)
        except Exception:
            investment_history = []

        # Sniping-Watchlist: letzter Stand + Verlauf je Item
        watchlist: dict[str, dict] = {}
        for name, ts, cents, steam_eur, buff_eur, pct, good, flt, listing_id, icon in conn.execute("""
            SELECT market_hash_name, checked_at, csfloat_price_usd_cents, steam_price_eur,
                   estimated_buff_eur, percent_vs_buff, is_good_deal,
                   csfloat_float_value, csfloat_listing_id, csfloat_icon_url
            FROM price_analysis ORDER BY checked_at
        """):
            entry = watchlist.setdefault(name, {"series": []})
            entry["series"].append([ts, round(cents / 100, 2) if cents else None,
                                     steam_eur, buff_eur, round(pct, 2) if pct is not None else None])
            # icon_url erst seit einem spaeteren Sniper-Lauf gefuellt -> letzten
            # bekannten Wert behalten, falls die aktuelle Zeile noch keinen hat.
            prev_icon = entry.get("latest", {}).get("icon_url")
            entry["latest"] = {"ts": ts, "csfloat_usd": round(cents / 100, 2) if cents else None,
                                "steam_eur": steam_eur, "est_buff_eur": buff_eur,
                                "percent_vs_buff": round(pct, 2) if pct is not None else None,
                                "is_good_deal": bool(good),
                                "float_value": flt,
                                "listing_id": listing_id,
                                "icon_url": icon or prev_icon,
                                "listing_url": f"https://csfloat.com/item/{listing_id}" if listing_id else None}
        for entry in watchlist.values():
            entry["series"] = entry["series"][-500:]

        latest = history[-1] if history else [None, 0, 0, 0]
        fx_row = conn.execute(
            "SELECT fx_usd_eur FROM inventory_history WHERE fx_usd_eur IS NOT NULL ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        fx_usd_eur = fx_row[0] if fx_row else 0.92

        last_snipe_at = conn.execute("SELECT MAX(checked_at) FROM price_analysis").fetchone()[0]
        last_inventory_at = conn.execute(
            "SELECT MAX(ts) FROM inventory_history WHERE source='python'"
        ).fetchone()[0]
        categories_latest = {}
        cat_ts = conn.execute(
            "SELECT MAX(ts) FROM inventory_history_category WHERE source='python'"
        ).fetchone()[0]
        if cat_ts:
            for cat, eur, usd, n in conn.execute("""
                SELECT category, csfloat_eur, csfloat_usd, items
                FROM inventory_history_category WHERE ts = ?
            """, (cat_ts,)):
                if n:
                    categories_latest[cat] = {"eur": eur, "usd": usd, "items": n}

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "fx_usd_eur": fx_usd_eur,
            "settings": config.load_settings(),
            "watchlist_names": [it["market_hash_name"] for it in config.load_watchlist()],
            "telegram_configured": telegram_notify.is_configured(),
            "last_snipe_at": last_snipe_at,
            "last_inventory_at": last_inventory_at,
            "poll_interval_seconds": int(os.getenv("POLL_INTERVAL_SECONDS", "300")),
            "hero": {
                "total_eur": latest[1],
                "total_usd": latest[2],
                "total_items": latest[3],
                "change_24h": _change_since(history, 24),
                "change_7d": _change_since(history, 24 * 7),
                "change_30d": _change_since(history, 24 * 30),
                "history_days": len(history),
            },
            "history": history,
            "category_history": category_history,
            "categories_latest": categories_latest,
            "items": items,
            "item_changes": item_changes,
            "performers": performers,
            "new_arrivals": new_arrivals,
            "buy_prices": buy_prices,
            "investment": investment,
            "investment_history": investment_history,
            "realized_pnl": realized_view,
            "rate_limits": config.load_rate_limits(),
            "csfloat_balance": config.load_csfloat_balance(),
            "csfloat_balance_budget": csfloat_balance.current_budget(),
            "steam_trades": steam_trades_valued,
            "steam_trades_synced_at": steam_trade_data.get("synced_at"),
            "steam_trades_configured": steam_trades.is_configured(),
            "watchlist": watchlist,
        }
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    def _authorize(self) -> bool:
        """Basic-Auth pruefen (falls konfiguriert). Bei Fehlschlag: 401 senden, False zurueck."""
        if not _auth_required():
            return True
        if _check_basic_auth(self.headers.get("Authorization")):
            return True
        body = b"unauthorized"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="SkinTicker", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def do_GET(self):
        # /healthz ist bewusst ohne Auth: nur Liveness fuer den Docker-Healthcheck,
        # gibt keine Daten preis.
        if self.path == "/healthz":
            self._respond(200, "text/plain", b"ok")
            return
        if not self._authorize():
            return
        if self.path in ("/", "/index.html"):
            body = INDEX_PATH.read_bytes()
            self._respond(200, "text/html; charset=utf-8", body)
        elif self.path == "/data.json":
            now = time.time()
            if _cache["payload"] is None or now - _cache["at"] > CACHE_SECONDS:
                try:
                    _cache["payload"] = json.dumps(build_data()).encode("utf-8")
                    _cache["at"] = now
                except Exception:
                    logger.exception("data.json konnte nicht erzeugt werden")
                    self._respond(500, "application/json", b'{"error":"data unavailable"}')
                    return
            self._respond(200, "application/json", _cache["payload"])
        else:
            self._respond(404, "text/plain", b"not found")

    def do_POST(self):
        if not self._authorize():
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or "{}")
        except (ValueError, json.JSONDecodeError):
            self._respond(400, "application/json", b'{"error":"invalid json"}')
            return

        if self.path == "/api/settings":
            saved = config.save_settings(body)
            self._invalidate()
            self._respond(200, "application/json", json.dumps(saved).encode("utf-8"))

        elif self.path == "/api/watchlist":
            action = body.get("action")
            name = (body.get("market_hash_name") or "").strip()
            if action == "add" and name:
                names = config.add_watchlist_item(name)
            elif action == "remove" and name:
                names = config.remove_watchlist_item(name)
            else:
                self._respond(400, "application/json", b'{"error":"action/name fehlt"}')
                return
            self._invalidate()
            payload = {"watchlist_names": [it["market_hash_name"] for it in names]}
            self._respond(200, "application/json", json.dumps(payload).encode("utf-8"))

        elif self.path == "/api/buyprice":
            name = (body.get("market_hash_name") or "").strip()
            if not name:
                self._respond(400, "application/json", b'{"error":"name fehlt"}')
                return
            saved = config.set_buy_price(name, body.get("price_eur"), body.get("buy_date"))
            self._invalidate()
            self._respond(200, "application/json", json.dumps({"buy_prices": saved}).encode("utf-8"))

        elif self.path == "/api/test-telegram":
            ok = telegram_notify.send_message("✅ SkinTicker: Test-Nachricht – Telegram-Anbindung funktioniert.")
            self._respond(200, "application/json", json.dumps({"ok": ok}).encode("utf-8"))

        elif self.path == "/api/run-sniper":
            self._run_sniper()

        elif self.path == "/api/refresh-balance":
            self._refresh_balance(force=bool(body.get("force")))

        elif self.path == "/api/sync-trades":
            self._sync_trades()

        elif self.path == "/api/exclude-trade":
            key = (body.get("key") or "").strip()
            if not key:
                self._respond(400, "application/json", b'{"error":"key fehlt"}')
                return
            keys = config.set_trade_excluded(key, bool(body.get("excluded")))
            self._invalidate()
            self._respond(200, "application/json",
                          json.dumps({"ok": True, "excluded_keys": keys}).encode("utf-8"))

        else:
            self._respond(404, "application/json", b'{"error":"not found"}')

    def _sync_trades(self):
        """Gleicht EK (Kaufpreise) + realisierte Bilanz + Hold sofort mit der CSFloat-
        Handelshistorie ab - erzwungen (ohne 20h-Drossel). Nutzt den warmen Sniper-Client
        und den Sniper-Lock, damit nicht parallel zu einem Preislauf gefeuert wird.
        Antwortet immer mit HTTP 200 + 'ok'-Flag."""
        client = _get_sniper_client()
        if client is None:
            self._respond(200, "application/json",
                          json.dumps({"ok": False, "message": "CSFLOAT_API_KEY fehlt in der .env."}).encode("utf-8"))
            return
        if not _sniper_lock.acquire(blocking=False):
            self._respond(200, "application/json",
                          json.dumps({"ok": False, "message": "Ein Lauf läuft bereits – bitte kurz warten."}).encode("utf-8"))
            return
        try:
            import inventory_main
            summary = inventory_main.sync_trade_prices(client, force=True)
            self._invalidate()
            summary["ok"] = True
            self._respond(200, "application/json", json.dumps(summary).encode("utf-8"))
        except Exception as exc:
            logger.exception("Manueller Trade-Sync fehlgeschlagen")
            self._respond(200, "application/json",
                          json.dumps({"ok": False, "message": str(exc) or type(exc).__name__}).encode("utf-8"))
        finally:
            _sniper_lock.release()

    def _refresh_balance(self, force: bool = False):
        """Ruft den CSFloat-Kontostand ab - aber nur, wenn das Rate-Limit-Budget es
        zulaesst (csfloat_balance.refresh entscheidet anhand der /me-Header). Bei zu
        wenig Kontingent oder laufendem Sniper-Lauf kommt der gecachte Stand zurueck.
        Antwortet immer mit HTTP 200 + 'ok'-Flag, damit das Frontend es anzeigen kann."""
        client = _get_sniper_client()
        if client is None:
            self._respond(200, "application/json",
                          json.dumps({"ok": False, "message": "CSFLOAT_API_KEY fehlt in der .env."}).encode("utf-8"))
            return
        # Teilt sich den Sniper-Client (warme Session + Rate-Limit-Stand). Non-blocking:
        # laeuft gerade ein Sniper-Durchlauf, nicht warten und /me nicht zusaetzlich feuern.
        if not _sniper_lock.acquire(blocking=False):
            cached = config.load_csfloat_balance()
            payload = {"ok": True, "refreshed": False,
                       "message": "Sniper-Lauf aktiv – Kontostand nicht abgefragt.",
                       "balance": cached, "budget": csfloat_balance.current_budget()}
            self._respond(200, "application/json", json.dumps(payload).encode("utf-8"))
            return
        try:
            result = csfloat_balance.refresh(client, force=force)
            result["ok"] = True
            self._invalidate()
            self._respond(200, "application/json", json.dumps(result).encode("utf-8"))
        except Exception as exc:
            logger.exception("Balance-Refresh fehlgeschlagen")
            self._respond(200, "application/json",
                          json.dumps({"ok": False, "message": str(exc) or type(exc).__name__}).encode("utf-8"))
        finally:
            _sniper_lock.release()

    def _run_sniper(self):
        """Fuehrt einen Watchlist-Preislauf (CSFloat + Steam + DB, ohne Sheets) aus und
        liefert eine Zusammenfassung inkl. API-/Item-Meldungen zurueck. Antwortet immer
        mit HTTP 200 und einem 'ok'-Flag, damit das Frontend die Meldung anzeigen kann."""
        if not _sniper_lock.acquire(blocking=False):
            self._respond(200, "application/json",
                          json.dumps({"ok": False, "message": "Ein Lauf läuft bereits – bitte kurz warten."}).encode("utf-8"))
            return
        try:
            client = _get_sniper_client()
            if client is None:
                self._respond(200, "application/json",
                              json.dumps({"ok": False, "message": "CSFLOAT_API_KEY fehlt in der .env."}).encode("utf-8"))
                return
            import db
            import main
            conn = db.get_connection()
            try:
                summary = main.analyze_watchlist(client, conn)
            finally:
                conn.close()
            self._invalidate()
            summary["ok"] = True
            self._respond(200, "application/json", json.dumps(summary).encode("utf-8"))
        except Exception as exc:
            logger.exception("Manueller Sniper-Lauf fehlgeschlagen")
            self._respond(200, "application/json",
                          json.dumps({"ok": False, "message": str(exc) or type(exc).__name__}).encode("utf-8"))
        finally:
            _sniper_lock.release()

    def _invalidate(self):
        _cache["payload"] = None
        _cache["at"] = 0.0

    def _respond(self, status: int, content_type: str, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        logger.info("%s %s", self.address_string(), fmt % args)


def main():
    port = int(os.getenv("DASHBOARD_PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    if _auth_required():
        logger.info("Basic-Auth aktiv (DASHBOARD_USER gesetzt).")
    else:
        logger.warning("Dashboard laeuft OHNE Login-Schutz - nur im vertrauenswuerdigen "
                       "Netz betreiben! Zum Absichern DASHBOARD_USER + DASHBOARD_PASSWORD "
                       "in der .env setzen.")
    logger.info("Dashboard laeuft auf http://0.0.0.0:%s", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
