"""Zentrale Einstellungen + Watchlist, editierbar zur Laufzeit (auch ueber das Dashboard).

Werte liegen in settings.json / watchlist.json und werden bei jedem Zugriff frisch
gelesen, damit Aenderungen aus dem Dashboard sofort vom Sniper uebernommen werden.
Fehlt settings.json, greifen die Defaults (bzw. .env als Uebergangsloesung).
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

# Alle zur Laufzeit veraenderlichen Zustandsdateien liegen im data/-Verzeichnis. Das ist
# das einzige als Docker-Volume beschreibbare, von ALLEN Containern (Dashboard, Inventar,
# Sniper) gemeinsam genutzte Verzeichnis. Frueher lagen einige Dateien im App-Root und
# wurden per Einzeldatei-Mount eingebunden - teils read-only, teils gar nicht -> das fuehrte
# zu "Read-only file system"-Fehlern (Bilanz-Sync) und fehlenden Hold-Items im Dashboard.
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))


def _state_path(name: str) -> Path:
    return DATA_DIR / name


SETTINGS_PATH = _state_path("settings.json")
WATCHLIST_PATH = _state_path("watchlist.json")
BUY_PRICES_PATH = _state_path("buy_prices.json")
AUTO_BUY_PRICES_PATH = _state_path("auto_buy_prices.json")
REALIZED_PNL_PATH = _state_path("realized_pnl.json")
RATE_LIMITS_PATH = _state_path("rate_limits.json")
STEAM_TRADES_PATH = _state_path("steam_trades.json")
CSFLOAT_HOLD_PATH = _state_path("csfloat_hold.json")
CSFLOAT_BALANCE_PATH = _state_path("balance.json")
EXCLUDED_TRADES_PATH = _state_path("excluded_trades.json")
MANUAL_ITEMS_PATH = _state_path("manual_items.json")

# Alle Zustandsdateien, fuer die eine Migration aus dem alten App-Root sinnvoll ist.
_STATE_PATHS = (SETTINGS_PATH, WATCHLIST_PATH, BUY_PRICES_PATH, AUTO_BUY_PRICES_PATH,
                REALIZED_PNL_PATH, RATE_LIMITS_PATH, STEAM_TRADES_PATH, CSFLOAT_HOLD_PATH,
                CSFLOAT_BALANCE_PATH, EXCLUDED_TRADES_PATH)


def _migrate_legacy_state() -> None:
    """Einmalige, sanfte Migration vom alten Layout (Datei im App-Root) ins data/-Verzeichnis.

    Liegt eine Zustandsdatei noch im Root und noch nicht in data/, wird sie nach data/
    kopiert - so gehen bestehende Daten beim Umstieg auf das gemeinsame Volume nicht
    verloren. Fehler werden bewusst verschluckt (best effort, darf den Start nie kippen).
    """
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for target in _STATE_PATHS:
        legacy = Path(target.name)  # gleiche Datei im App-Root
        if legacy.resolve() == target.resolve():
            continue
        if legacy.exists() and not target.exists():
            try:
                shutil.copy2(legacy, target)
            except OSError:
                pass


_migrate_legacy_state()

# Nur diese Schluessel sind gueltig; alles andere wird beim Speichern ignoriert.
# Vergleichsmassstab ist der Steam-Marktpreis (kein Buff mehr).
DEFAULTS = {
    "snipe_threshold_percent": 3.0,   # ab hier im Dashboard als "guenstig" markiert (unter Steam)
    "alert_threshold_percent": 10.0,  # ab hier zusaetzlich Telegram-Alarm
}

# Grenzen zur Validierung von Eingaben aus dem Dashboard.
BOUNDS = {
    "snipe_threshold_percent": (0.0, 90.0),
    "alert_threshold_percent": (0.0, 90.0),
}


def load_settings() -> dict:
    data = dict(DEFAULTS)
    # .env als Uebergangsloesung (falls jemand die alten Variablen gesetzt hat)
    for key, env in [("snipe_threshold_percent", "SNIPE_THRESHOLD_PERCENT"),
                     ("alert_threshold_percent", "ALERT_THRESHOLD_PERCENT")]:
        if os.getenv(env):
            try:
                data[key] = float(os.getenv(env))
            except ValueError:
                pass
    # settings.json hat Vorrang (das ist die im Dashboard editierbare Quelle)
    if SETTINGS_PATH.exists():
        try:
            stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            data.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except (json.JSONDecodeError, OSError):
            pass
    return data


def save_settings(updates: dict) -> dict:
    current = load_settings()
    for key, value in updates.items():
        if key not in DEFAULTS:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        lo, hi = BOUNDS[key]
        current[key] = max(lo, min(hi, value))
    SETTINGS_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


def load_watchlist() -> list[dict]:
    if not WATCHLIST_PATH.exists():
        return []
    try:
        return json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def save_watchlist(items: list[dict]) -> list[dict]:
    # Doppelte Namen entfernen, Reihenfolge beibehalten.
    seen, cleaned = set(), []
    for it in items:
        name = (it.get("market_hash_name") or "").strip()
        if name and name not in seen:
            seen.add(name)
            cleaned.append({"market_hash_name": name})
    WATCHLIST_PATH.write_text(json.dumps(cleaned, indent=2, ensure_ascii=False), encoding="utf-8")
    return cleaned


def load_buy_prices() -> dict:
    """market_hash_name -> {"price_eur": float, "buy_date": "YYYY-MM-DD" | None}."""
    if not BUY_PRICES_PATH.exists():
        return {}
    try:
        return json.loads(BUY_PRICES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def set_buy_price(name: str, price_eur, buy_date: str | None = None) -> dict:
    """Setzt/aktualisiert den Kaufpreis (pro Stueck) eines Items. price_eur=None loescht ihn."""
    data = load_buy_prices()
    name = (name or "").strip()
    if not name:
        return data
    if price_eur in (None, "", 0):
        data.pop(name, None)
    else:
        try:
            price = float(price_eur)
        except (TypeError, ValueError):
            return data
        prev = data.get(name, {})
        data[name] = {"price_eur": price, "buy_date": buy_date or prev.get("buy_date")}
    BUY_PRICES_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return data


def load_auto_buy_prices() -> dict:
    """{"synced_at": iso|None, "prices": {name: {price_eur, buy_date, source}}}."""
    if not AUTO_BUY_PRICES_PATH.exists():
        return {"synced_at": None, "prices": {}}
    try:
        data = json.loads(AUTO_BUY_PRICES_PATH.read_text(encoding="utf-8"))
        return {"synced_at": data.get("synced_at"), "prices": data.get("prices", {})}
    except (json.JSONDecodeError, OSError):
        return {"synced_at": None, "prices": {}}


def save_auto_buy_prices(prices: dict, synced_at: str) -> None:
    AUTO_BUY_PRICES_PATH.write_text(
        json.dumps({"synced_at": synced_at, "prices": prices}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def load_steam_trades() -> dict:
    """Rohe Steam-Trade-History (vom Inventar-Sync geschrieben).
    Format: {"synced_at": iso|None, "trades": [{trade_id, time, given, received}]}."""
    if not STEAM_TRADES_PATH.exists():
        return {"synced_at": None, "trades": []}
    try:
        data = json.loads(STEAM_TRADES_PATH.read_text(encoding="utf-8"))
        return {"synced_at": data.get("synced_at"), "trades": data.get("trades", [])}
    except (json.JSONDecodeError, OSError):
        return {"synced_at": None, "trades": []}


def save_steam_trades(trades: list, synced_at: str) -> None:
    STEAM_TRADES_PATH.write_text(
        json.dumps({"synced_at": synced_at, "trades": trades}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def load_csfloat_hold() -> dict:
    """Auf CSFloat gekaufte Items im Escrow/Hold (noch nicht ausgeliefert).
    Format: {"synced_at": iso|None, "items": [{name, qty, buy_eur, buy_date, hold_until, ...}]}."""
    if not CSFLOAT_HOLD_PATH.exists():
        return {"synced_at": None, "items": []}
    try:
        data = json.loads(CSFLOAT_HOLD_PATH.read_text(encoding="utf-8"))
        return {"synced_at": data.get("synced_at"), "items": data.get("items", [])}
    except (json.JSONDecodeError, OSError):
        return {"synced_at": None, "items": []}


def save_csfloat_hold(items: list, synced_at: str) -> None:
    CSFLOAT_HOLD_PATH.write_text(
        json.dumps({"synced_at": synced_at, "items": items}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _empty_balance() -> dict:
    return {"balance_usd": None, "pending_usd": None, "balance_cents": None,
            "pending_cents": None, "fetched_at": None}


def load_csfloat_balance() -> dict:
    """Gecachter CSFloat-Kontostand (aktiv + ausstehend).

    Betraege liegen sowohl in US-Cent (roh, wie von der API) als auch bereits in
    USD vor. Format: {balance_usd, pending_usd, balance_cents, pending_cents,
    fetched_at}. Wird von csfloat_balance.refresh() geschrieben, damit das
    Dashboard den Stand ohne eigenen API-Call anzeigen kann."""
    if not CSFLOAT_BALANCE_PATH.exists():
        return _empty_balance()
    try:
        data = json.loads(CSFLOAT_BALANCE_PATH.read_text(encoding="utf-8"))
        return {**_empty_balance(), **data}
    except (json.JSONDecodeError, OSError):
        return _empty_balance()


def save_csfloat_balance(balance_cents, pending_cents) -> dict:
    """Speichert den Kontostand. balance_cents/pending_cents in US-Cent (API-Roh);
    fehlende/ungueltige Werte werden zu None. Gibt den gespeicherten Datensatz zurueck."""
    def to_usd(cents):
        return round(cents / 100, 2) if isinstance(cents, (int, float)) else None

    data = {
        "balance_cents": balance_cents if isinstance(balance_cents, (int, float)) else None,
        "pending_cents": pending_cents if isinstance(pending_cents, (int, float)) else None,
        "balance_usd": to_usd(balance_cents),
        "pending_usd": to_usd(pending_cents),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    CSFLOAT_BALANCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CSFLOAT_BALANCE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def load_rate_limits() -> dict:
    """Zuletzt gesehener CSFloat-Rate-Limit-Stand je Endpoint (vom Sniper/Inventory
    geschrieben). Format: {path: {limit, remaining, reset_at, updated_at}}."""
    if not RATE_LIMITS_PATH.exists():
        return {}
    try:
        return json.loads(RATE_LIMITS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def load_manual_items() -> dict:
    """Manuelle Liste = Items ausserhalb des CSFloat-Inventars (v.a. Inhalt der
    CS2-Lagereinheiten). Format: {market_hash_name: menge}. Lokale, editierbare Datei
    (data/manual_items.json) - ersetzt die fruehere Google-Sheets-Anbindung."""
    if not MANUAL_ITEMS_PATH.exists():
        return {}
    try:
        data = json.loads(MANUAL_ITEMS_PATH.read_text(encoding="utf-8"))
        return {str(k): int(v) for k, v in data.items() if v}
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        return {}


def save_manual_items(items: dict) -> dict:
    """Speichert die manuelle Liste. Leere/ungueltige Mengen werden verworfen."""
    clean = {str(k): int(v) for k, v in (items or {}).items() if v}
    MANUAL_ITEMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANUAL_ITEMS_PATH.write_text(json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")
    return clean


def load_excluded_trades() -> list:
    """Schluessel der Trades, die aus der realisierten Bilanz-Statistik ausgeschlossen
    sind (z.B. geliehene Items). Format: {"keys": [...]}. Schluessel werden im
    Dashboard/Server aus stabilen Trade-Feldern gebildet (name|buy_date|sell_date|pct)."""
    if not EXCLUDED_TRADES_PATH.exists():
        return []
    try:
        data = json.loads(EXCLUDED_TRADES_PATH.read_text(encoding="utf-8"))
        return data.get("keys", []) if isinstance(data, dict) else []
    except (json.JSONDecodeError, OSError):
        return []


def set_trade_excluded(key: str, excluded: bool) -> list:
    """Schaltet einen Trade in der Bilanz-Statistik aus (excluded=True) oder wieder ein."""
    keys = set(load_excluded_trades())
    key = (key or "").strip()
    if not key:
        return sorted(keys)
    if excluded:
        keys.add(key)
    else:
        keys.discard(key)
    EXCLUDED_TRADES_PATH.write_text(
        json.dumps({"keys": sorted(keys)}, indent=2, ensure_ascii=False), encoding="utf-8")
    return sorted(keys)


def load_realized_pnl() -> dict:
    """Gecachte realisierte Bilanz (Kauf-/Verkauf-Abgleich). Wird 1x/Tag erneuert."""
    if not REALIZED_PNL_PATH.exists():
        return {}
    try:
        return json.loads(REALIZED_PNL_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_realized_pnl(data: dict, synced_at: str) -> None:
    REALIZED_PNL_PATH.write_text(
        json.dumps({"synced_at": synced_at, **data}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def add_watchlist_item(name: str) -> list[dict]:
    items = load_watchlist()
    items.append({"market_hash_name": name})
    return save_watchlist(items)


def remove_watchlist_item(name: str) -> list[dict]:
    items = [it for it in load_watchlist() if it.get("market_hash_name") != name]
    return save_watchlist(items)
