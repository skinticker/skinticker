"""Inventarbewertung - Python-Port der inv()-Logik aus dem Apps Script.

Ablauf pro Durchlauf:
1. CSFloat-Inventar abrufen, Items nach market_hash_name gruppieren
   (Menge zaehlen, predicted_price in Cents mitteln).
2. Fuer Items ohne predicted_price: Fallback auf die oeffentliche
   CSFloat-Preisliste (min_price in Cents).
3. Steam-Preise aus dem SQLite-Cache; pro Durchlauf werden nur die
   aeltesten N Eintraege neu von Steam geholt (Rotationsprinzip wie der
   alte steamMinuteWorker, schont Steams informelles Rate-Limit).
4. Summen je Kategorie + Gesamt berechnen und als Snapshot speichern -
   gleiches Datenmodell wie die importierte Sheet-History, damit die
   Zeitreihe nahtlos weiterlaeuft.

Anders als das Apps Script zaehlt dieser Port NUR das CSFloat-Inventar
(keine zusaetzliche manuelle Liste), um Doppelzaehlungen zu vermeiden.
Skinport ist wie im Original deaktiviert (Werte bleiben 0).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone

import config
import db
import fx
from steam_client import get_steam_price_eur

# Wie lange ein ertradetes Item hoechstens als Provisorium gefuehrt wird, bis CSFloat
# es erfasst haben sollte (7-Tage-Trade-Hold + Puffer).
PENDING_TRADE_MAX_DAYS = 8

logger = logging.getLogger(__name__)

CATEGORIES = ["Skins", "Cases", "Sticker", "Agents", "Charms", "Andere"]


def detect_category(name: str) -> str:
    """Gleiche Regeln wie detectCategory_ im Apps Script."""
    if re.search(r"Case", name, re.IGNORECASE):
        return "Cases"
    if re.search(r"Sticker\s*\|", name, re.IGNORECASE):
        return "Sticker"
    if re.search(r"Agent\s*\|", name, re.IGNORECASE):
        return "Agents"
    if re.search(r"Charm\s*\|", name, re.IGNORECASE):
        return "Charms"
    return "Skins"


def extract_overlays(item: dict) -> list[dict]:
    """Angebrachte Sticker + Charms eines Items fuers Dashboard.

    Jeweils Name, Icon und Referenzpreis (USD-Cent). Reihenfolge = Slot, damit
    die Anzeige der tatsaechlichen Anbringung entspricht. Die Art richtet sich nach
    dem Quell-Array: 'stickers' = Sticker, 'keychains' = Charm (auch wenn ein
    Sticker in einen Charm gesteckt wurde - dann bleibt es ein Charm).
    """
    out: list[dict] = []
    for arr, kind in (("stickers", "sticker"), ("keychains", "charm")):
        for s in sorted(item.get(arr) or [], key=lambda x: x.get("slot", 0)):
            if s.get("name"):
                out.append({"name": s["name"], "icon": s.get("icon_url"),
                            "price": (s.get("reference") or {}).get("price"), "kind": kind})
    return out


def compose_hash(item: dict) -> str | None:
    """market_hash_name bestimmen, notfalls aus Name + Wear zusammensetzen."""
    if item.get("market_hash_name"):
        return item["market_hash_name"]
    base = item.get("item_name") or item.get("market_name") or item.get("name")
    if not base:
        return None
    wear = item.get("wear_name")
    return f"{base} ({wear})" if wear else base


def reduce_inventory(items: list[dict]) -> dict[str, dict]:
    """Gruppiert nach market_hash_name: Menge + Durchschnitt des predicted_price (USD).

    Merkt sich zusaetzlich Anzeige-Details (Bild, Float, Raritaet, Inspect-Link)
    des jeweils ersten Items fuer die Dashboard-Karten.
    """
    grouped: dict[str, dict] = {}
    for item in items:
        name = compose_hash(item)
        if not name:
            continue
        entry = grouped.setdefault(name, {
            "qty": 0, "pred_usd_sum": 0.0, "pred_count": 0,
            "float_value": item.get("float_value"),
            "icon_url": item.get("icon_url"),
            "inspect_link": item.get("inspect_link") or item.get("serialized_inspect"),
            "rarity": item.get("rarity"),
            "is_stattrak": int(bool(item.get("is_stattrak"))),
            "is_souvenir": int(bool(item.get("is_souvenir"))),
            "stickers": extract_overlays(item),
        })
        entry["qty"] += 1
        predicted_cents = (item.get("reference") or {}).get("predicted_price")
        if isinstance(predicted_cents, (int, float)):
            entry["pred_usd_sum"] += predicted_cents / 100
            entry["pred_count"] += 1
    return grouped


def refresh_steam_prices(conn, names: list[str]) -> None:
    """Aktualisiert die aeltesten veralteten Steam-Preise (Rotationsprinzip)."""
    per_run = int(os.getenv("STEAM_REFRESH_PER_RUN", "10"))
    stale = db.stale_steam_names(conn, names)
    todo = stale[:per_run]
    if not todo:
        logger.info("Steam-Cache: alle %s Eintraege frisch genug.", len(names))
        return
    logger.info("Steam-Cache: aktualisiere %s von %s veralteten Eintraegen.", len(todo), len(stale))
    for name in todo:
        price = get_steam_price_eur(name)
        db.upsert_steam_price(conn, name, price)


def add_pending_trade_items(grouped: dict, usd_to_eur: float) -> int:
    """Ergaenzt provisorisch ertradete Items, die noch im 7-Tage-Hold sind und von
    CSFloat noch nicht erfasst wurden (Name noch nicht in `grouped`). Sie werden mit
    dem CSFloat-Suggested-Price bewertet und `pending_trade=1` markiert. Sobald CSFloat
    das Item erfasst, ist der Name in `grouped` -> kein Provisorium mehr (ersetzt).

    Gibt die Anzahl ergaenzter Positionen zurueck.
    """
    now = datetime.now(timezone.utc)
    added = 0
    for t in config.load_steam_trades().get("trades", []):
        if not (t.get("given") and t.get("received")) or not t.get("time"):
            continue  # nur echte Item-fuer-Item-Trades
        if (now - datetime.fromtimestamp(t["time"], timezone.utc)).days > PENDING_TRADE_MAX_DAYS:
            continue  # zu alt - sollte laengst erfasst/verkauft sein
        for it in t["received"]:
            name = it["name"]
            if name in grouped:
                continue  # schon real im Inventar (CSFloat/manuell) -> nicht doppelt
            sug_eur = it.get("suggested_eur")
            if not sug_eur:
                continue
            qty = it.get("qty", 1)
            usd = (sug_eur / usd_to_eur) if usd_to_eur else 0.0
            entry = grouped.setdefault(name, {
                "qty": 0, "pred_usd_sum": 0.0, "pred_count": 0,
                "float_value": None, "icon_url": it.get("icon"), "inspect_link": None,
                "rarity": None, "is_stattrak": 0, "is_souvenir": 0, "stickers": [],
                "pending_trade": 1,
            })
            entry["qty"] += qty
            entry["pred_usd_sum"] += usd * qty
            entry["pred_count"] += qty
            entry["pending_trade"] = 1
            added += 1
    if added:
        logger.info("%s provisorische Trade-Items (im Hold) ergaenzt.", added)
    return added


def add_csfloat_hold_items(grouped: dict, usd_to_eur: float) -> int:
    """Ergaenzt auf CSFloat gekaufte Items, die noch im Escrow/Hold sind (noch nicht im
    Inventar). Wert = CSFloat-Suggested-Price (sonst Kaufpreis), `pending_trade=1`.
    Sobald CSFloat das Item ausliefert (Name in `grouped`), entfaellt das Provisorium.
    """
    added = 0
    for h in config.load_csfloat_hold().get("items", []):
        name = h.get("name")
        if not name or name in grouped:
            continue  # schon real im Inventar/manuell -> nicht doppelt
        val_eur = h.get("suggested_eur") or h.get("buy_eur")
        if not val_eur:
            continue
        qty = h.get("qty", 1)
        usd = (val_eur / usd_to_eur) if usd_to_eur else 0.0
        entry = grouped.setdefault(name, {
            "qty": 0, "pred_usd_sum": 0.0, "pred_count": 0,
            "float_value": h.get("float_value"), "icon_url": h.get("icon"), "inspect_link": None,
            "rarity": None, "is_stattrak": 0, "is_souvenir": 0, "stickers": [],
            "pending_trade": 1,
        })
        entry["qty"] += qty
        entry["pred_usd_sum"] += usd * qty
        entry["pred_count"] += qty
        entry["pending_trade"] = 1
        added += qty
    if added:
        logger.info("%s provisorische CSFloat-Hold-Items ergaenzt.", added)
    return added


def run_inventory_valuation(client, conn, manual_qty: dict[str, int] | None = None) -> dict:
    inventory = client.get_my_inventory()
    grouped = reduce_inventory(inventory)
    logger.info("CSFloat-Inventar: %s Items, %s eindeutige Namen.", len(inventory), len(grouped))

    csfloat_names = set(grouped)

    # Manuelle Liste (Lagereinheiten etc.) dazuzaehlen - wie im Apps Script.
    if manual_qty:
        manual_items = sum(manual_qty.values())
        for name, qty in manual_qty.items():
            entry = grouped.setdefault(name, {
                "qty": 0, "pred_usd_sum": 0.0, "pred_count": 0,
                "float_value": None, "icon_url": None, "inspect_link": None,
                "rarity": None, "is_stattrak": 0, "is_souvenir": 0, "stickers": [],
            })
            entry["qty"] += qty
        logger.info("Manuelle Liste: %s Items (%s Namen) dazugezaehlt.", manual_items, len(manual_qty))

    # Fallback-Preise nur laden, wenn wirklich etwas fehlt (grosser Download).
    missing = [n for n, e in grouped.items() if e["pred_count"] == 0]
    fallback_usd: dict[str, float] = {}
    if missing:
        logger.info("%s Items ohne predicted_price - lade Preisliste als Fallback.", len(missing))
        price_list = client.get_price_list()
        by_name = {p["market_hash_name"]: p for p in price_list}
        for name in missing:
            entry = by_name.get(name)
            if entry and entry.get("min_price"):
                fallback_usd[name] = entry["min_price"] / 100

    refresh_steam_prices(conn, list(grouped))
    steam_cache = db.load_steam_cache(conn)
    usd_to_eur = fx.get_usd_to_eur()

    # Ertradete (Steam) + auf CSFloat gekaufte (Escrow) Items im Hold provisorisch aufnehmen.
    try:
        add_pending_trade_items(grouped, usd_to_eur)
        add_csfloat_hold_items(grouped, usd_to_eur)
    except Exception:
        logger.exception("Provisorische Hold-Items konnten nicht ergaenzt werden.")

    category_sums = {
        cat: {"steam_eur": 0.0, "skinport_eur": 0.0, "csfloat_usd": 0.0, "csfloat_eur": 0.0, "items": 0}
        for cat in CATEGORIES
    }

    item_rows = []
    for name, entry in grouped.items():
        qty = entry["qty"]
        if entry["pred_count"] > 0:
            usd_each = entry["pred_usd_sum"] / entry["pred_count"]
        else:
            usd_each = fallback_usd.get(name, 0.0)

        steam_each = steam_cache.get(name, 0.0)
        category = detect_category(name)
        cat = category_sums[category]
        cat["csfloat_usd"] += usd_each * qty
        cat["csfloat_eur"] += usd_each * usd_to_eur * qty
        cat["steam_eur"] += steam_each * qty
        cat["items"] += qty

        item_rows.append({
            "market_hash_name": name,
            "category": category,
            "qty": qty,
            "usd_each": usd_each,
            "eur_each": usd_each * usd_to_eur,
            "steam_eur_each": steam_each or None,
            "float_value": entry.get("float_value"),
            "icon_url": entry.get("icon_url"),
            "inspect_link": entry.get("inspect_link"),
            "rarity": entry.get("rarity"),
            "is_stattrak": entry.get("is_stattrak", 0),
            "is_souvenir": entry.get("is_souvenir", 0),
            "in_csfloat": int(name in csfloat_names),
            "manual_qty": (manual_qty or {}).get(name, 0),
            "stickers": json.dumps(entry.get("stickers") or [], ensure_ascii=False) or None,
            "pending_trade": int(entry.get("pending_trade", 0)),
        })

    totals = {
        "fx_usd_eur": usd_to_eur,
        "sum_skinport_eur": 0.0,
        "sum_csfloat_usd": sum(c["csfloat_usd"] for c in category_sums.values()),
        "sum_csfloat_eur": sum(c["csfloat_eur"] for c in category_sums.values()),
        "total_items": sum(c["items"] for c in category_sums.values()),
    }

    ts = db.save_inventory_snapshot(conn, totals, category_sums)
    db.save_inventory_items(conn, ts, item_rows)
    logger.info(
        "Snapshot gespeichert: %s Items | CSFloat %.2f USD / %.2f EUR | Steam %.2f EUR",
        totals["total_items"], totals["sum_csfloat_usd"], totals["sum_csfloat_eur"],
        sum(c["steam_eur"] for c in category_sums.values()),
    )
    return totals
