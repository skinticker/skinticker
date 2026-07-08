"""Einstiegspunkt fuer die Inventarbewertung.

Dauerbetrieb:  python inventory_main.py          (Intervall: INVENTORY_INTERVAL_SECONDS)
Einmaliger Lauf: python inventory_main.py --once  (zum Testen)
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

import config
import db
import fx
import steam_trades
import trade_prices
from csfloat_client import CSFloatClient
from inventory import run_inventory_valuation

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

load_dotenv()

# Heartbeat fuer den Docker-Healthcheck: wird nach jedem Bewertungslauf beruehrt.
HEARTBEAT_PATH = Path("data/heartbeat_inventory")


def _touch_heartbeat() -> None:
    try:
        HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_PATH.touch()
    except OSError:
        pass  # Telemetrie darf den Lauf nie kippen


def sync_trade_prices(client, min_hours: float = 20.0, force: bool = False) -> dict:
    """Gleicht Kaufpreise (EK) + realisierte Bilanz mit der CSFloat-Handelshistorie ab.

    Standardmaessig hoechstens ~1x/Tag (min_hours), da /me/trades ein niedriges
    Rate-Limit (100/Fenster) hat. force=True erzwingt den Lauf (z.B. wenn manuell
    ueber das Dashboard ausgeloest). Gibt eine Zusammenfassung zurueck.
    """
    stored = config.load_auto_buy_prices()
    synced_at = stored.get("synced_at")
    if not force and synced_at:
        try:
            age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(synced_at)).total_seconds() / 3600
            if age_h < min_hours:
                logger.info("Trade-Sync uebersprungen (vor %.1fh synchronisiert).", age_h)
                return {"skipped": True, "message": f"Zuletzt vor {age_h:.1f}h abgeglichen.",
                        "synced_at": synced_at}
        except ValueError:
            pass
    logger.info("Trade-Sync: gleiche Kaufpreise + realisierte Bilanz mit der CSFloat-Historie ab ...")
    now_iso = datetime.now(timezone.utc).isoformat()
    rate = fx.get_usd_to_eur()
    auto, realized = trade_prices.sync_all(client, rate)
    config.save_realized_pnl(realized, now_iso)
    hold_count = 0

    # CSFloat-Preisliste einmal laden (Suggested Price = min_price) - fuer Steam-Trades
    # UND CSFloat-Hold-Items.
    pmap = {}
    try:
        pmap = {p["market_hash_name"]: p["min_price"] / 100 * rate
                for p in client.get_price_list() if p.get("min_price")}
    except Exception:
        logger.exception("CSFloat-Preisliste nicht abrufbar - Trade-/Hold-Items ohne suggested prices.")

    # Steam-Handelshistorie (Item-fuer-Item-Trades) - nur wenn STEAM_LOGIN_SECURE gesetzt.
    if steam_trades.is_configured():
        try:
            fetched = steam_trades.fetch_trades()
            existing = config.load_steam_trades().get("trades", [])
            # Nie ueberschreiben: gespeicherte + neue Trades vereinigen. So gehen bei
            # abgelaufenem Cookie / Steam-Rate-Limit (leerer Abruf) keine Trades verloren.
            if not fetched and existing:
                logger.warning("Steam-Abruf lieferte keine Trades (Cookie abgelaufen/Rate-Limit?) - "
                               "behalte %s gespeicherte Trades.", len(existing))
            trades = steam_trades.merge_trades(existing, fetched)
            if trades and pmap:
                steam_trades.enrich_with_prices(trades, pmap)
            config.save_steam_trades(trades, now_iso)
            # Trade-Kostenbasis (EK) persistent in die Kaufpreise (source='trade').
            cost = steam_trades.compute_cost_basis(steam_trades.value_trades(trades))
            if cost and auto is not None:
                for name, c in cost.items():
                    if name not in auto:
                        auto[name] = {"price_eur": c["price_eur"], "buy_date": c["buy_date"], "source": "trade"}
        except Exception:
            logger.exception("Steam-Trade-History-Sync fehlgeschlagen - wird beim naechsten Lauf erneut versucht.")
    else:
        logger.info("Steam-Trade-History uebersprungen (STEAM_LOGIN_SECURE nicht gesetzt).")

    # CSFloat-Hold: auf CSFloat gekaufte, noch nicht ausgelieferte Items (Escrow) -
    # provisorisch anzeigen, EK = Kaufpreis.
    try:
        hold_items = trade_prices.build_hold_items(client.get_pending_buyer_trades(), rate)
        for h in hold_items:
            h["suggested_eur"] = pmap.get(h["name"])
        config.save_csfloat_hold(hold_items, now_iso)
        hold_count = len(hold_items)
        if auto is not None:
            for h in hold_items:  # EK (Kaufpreis) persistieren
                auto[h["name"]] = {"price_eur": h["buy_eur"], "buy_date": h["buy_date"], "source": "csfloat"}
        logger.info("CSFloat-Hold: %s Item-Typen im Escrow.", len(hold_items))
    except Exception:
        logger.exception("CSFloat-Hold-Sync fehlgeschlagen - wird beim naechsten Lauf erneut versucht.")

    if auto is not None:
        config.save_auto_buy_prices(auto, now_iso)

    return {
        "skipped": False,
        "auto_ek": len(auto) if auto is not None else None,
        "realized_trades": realized.get("trade_count"),
        "realized_total_eur": realized.get("realized_total_eur"),
        "hold_items": hold_count,
        "synced_at": now_iso,
        "message": ("EK aus Handelshistorie abgeglichen"
                    + (f" · {len(auto)} Auto-EK" if auto is not None else " (Inventar nicht abrufbar – EK unverändert)")
                    + f" · {realized.get('trade_count', 0)} realisierte Trades"),
    }


def _sync_trade_prices_if_due(client, min_hours: float = 20.0) -> dict:
    """Rueckwaertskompatibler Wrapper (throttled)."""
    return sync_trade_prices(client, min_hours=min_hours, force=False)


def main():
    once = "--once" in sys.argv
    interval = int(os.getenv("INVENTORY_INTERVAL_SECONDS", "3600"))

    client = CSFloatClient(api_key=os.getenv("CSFLOAT_API_KEY"))
    conn = db.get_connection()

    # Manuelle Liste (Lagereinheiten etc.): lokale, editierbare data/manual_items.json.
    # Fehlt sie noch, einmalig aus dem letzten Snapshot seeden (Migration von der
    # frueheren Google-Sheets-Anbindung), damit die Items nicht verloren gehen.
    if not config.MANUAL_ITEMS_PATH.exists():
        seed = db.last_manual_quantities(conn)
        if seed:
            config.save_manual_items(seed)
            logger.info("manual_items.json initial aus letztem Snapshot erstellt (%s Positionen).", len(seed))

    try:
        while True:
            logger.info("--- Inventarbewertung startet ---")
            try:
                manual_qty = config.load_manual_items()
                run_inventory_valuation(client, conn, manual_qty=manual_qty)
            except Exception:
                logger.exception("Inventarbewertung fehlgeschlagen - naechster Versuch im naechsten Intervall.")

            try:
                _sync_trade_prices_if_due(client)
            except Exception:
                logger.exception("Trade-Sync fehlgeschlagen - wird beim naechsten faelligen Lauf erneut versucht.")
            _touch_heartbeat()
            if once:
                break
            logger.info("Fertig. Naechste Bewertung in %s Sekunden.", interval)
            time.sleep(interval)
    except KeyboardInterrupt:
        logger.info("Beende auf Wunsch (Strg+C).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
