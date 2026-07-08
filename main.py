"""Sniper: fragt die Watchlist bei CSFloat + Steam ab, vergleicht CSFloat gegen den
Steam-Marktpreis, speichert alles lokal in SQLite und alarmiert bei guten Deals via
Telegram. (Die fruehere Google-Sheets-Ausgabe wurde entfernt.)
"""

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

import config
import db
import fx
import telegram_notify
from csfloat_client import CSFloatClient
from steam_client import get_steam_price_eur

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

load_dotenv()

# Heartbeat fuer den Docker-Healthcheck: wird nach jedem Durchlauf beruehrt.
# Bleibt die Datei zu lange unveraendert, meldet der Healthcheck "unhealthy".
HEARTBEAT_PATH = Path("data/heartbeat_sniper")


def _touch_heartbeat() -> None:
    try:
        HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_PATH.touch()
    except OSError:
        pass  # Telemetrie darf den Lauf nie kippen


def main():
    client = CSFloatClient(api_key=os.getenv("CSFLOAT_API_KEY"))
    conn = db.get_connection()
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))

    try:
        while True:
            logger.info("--- Neuer Durchlauf ---")
            # Den ganzen Durchlauf abschirmen: ein transienter Fehler (z.B. Netzwerk,
            # Wechselkurs-Abruf) darf die Schleife NICHT beenden - sonst geht der
            # Container in eine Crash-Restart-Schleife. Naechster Versuch im naechsten
            # Intervall (wie bei der Inventarbewertung).
            try:
                summary = analyze_watchlist(client, conn)
                logger.info(
                    "Durchlauf fertig: %s/%s aktualisiert, %s Deal(s). Naechster in %ss.",
                    summary["ok_count"], summary["checked"], summary["deals"], poll_interval,
                )
            except Exception:
                logger.exception("Durchlauf fehlgeschlagen - naechster Versuch im naechsten Intervall.")
            _touch_heartbeat()
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        logger.info("Beende auf Wunsch (Strg+C).")
    finally:
        conn.close()


def analyze_watchlist(client, conn) -> dict:
    """Ein Sniper-Durchlauf (CSFloat + Steam + DB): Watchlist frisch lesen, je Item
    analysieren, in die DB schreiben und bei guten Deals per Telegram alarmieren.
    Liefert je Item ok/Meldung zurueck (auch fuer den manuellen Dashboard-Trigger)."""
    settings = config.load_settings()
    watchlist = config.load_watchlist()
    usd_to_eur = fx.get_usd_to_eur()
    results = []
    for item in watchlist:
        name = item["market_hash_name"]
        try:
            results.append(_analyze_item(client, conn, name, usd_to_eur, settings))
        except Exception as exc:
            logger.exception("Fehler bei '%s' im manuellen Lauf.", name)
            results.append({"name": name, "ok": False, "message": str(exc) or type(exc).__name__})
    return {
        "checked": len(results),
        "ok_count": sum(1 for r in results if r.get("ok")),
        "deals": sum(1 for r in results if r.get("is_good_deal")),
        "results": results,
    }


def _analyze_item(client, conn, name, usd_to_eur, settings) -> dict:
    """CSFloat + Steam abfragen, in die DB schreiben und ggf. einen Telegram-Alarm
    senden. Liefert ein Ergebnis-Dict fuer die Zusammenfassung im Dashboard."""
    listings = client.get_cheapest_listings(name, limit=3)
    if not listings:
        logger.warning("Keine kaufbaren Listings gefunden fuer: %s", name)
        return {"name": name, "ok": False, "message": "Kein Listing gefunden"}

    db.save_listings(conn, name, listings)
    cheapest = listings[0]
    csfloat_price_eur = cheapest["price"] / 100 * usd_to_eur

    steam_price_eur = get_steam_price_eur(name)

    analysis = {
        "market_hash_name": name,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "csfloat_price_usd_cents": cheapest["price"],
        "csfloat_listing_id": cheapest["id"],
        "csfloat_float_value": cheapest["item"].get("float_value"),
        "csfloat_icon_url": cheapest["item"].get("icon_url"),
        "steam_price_eur": steam_price_eur,
        "estimated_buff_eur": None,
        "percent_vs_buff": None,
        "is_good_deal": 0,
    }

    percent_vs_steam = None
    is_good_deal = False
    if steam_price_eur is not None:
        # Direkter Vergleich CSFloat vs. Steam-Marktpreis. Negativ = guenstiger als Steam.
        percent_vs_steam = (csfloat_price_eur - steam_price_eur) / steam_price_eur * 100
        is_good_deal = percent_vs_steam <= -settings["snipe_threshold_percent"]

        analysis["percent_vs_buff"] = percent_vs_steam  # Spaltenname historisch, Wert = vs. Steam
        analysis["is_good_deal"] = int(is_good_deal)

        flag = "GUENSTIG" if is_good_deal else "-"
        logger.info(
            "%s: CSFloat %.2f EUR | Steam %.2f EUR | %+.1f%% | %s",
            name, csfloat_price_eur, steam_price_eur, percent_vs_steam, flag,
        )

        # Telegram-Alarm nur bei richtig guten Deals (strengere Schwelle) UND nur,
        # wenn dieser Durchlauf guenstiger ist als der letzte Alarm.
        if percent_vs_steam <= -settings["alert_threshold_percent"]:
            if db.should_alert(conn, name, cheapest["price"]):
                msg = telegram_notify.format_deal(
                    name, csfloat_price_eur, steam_price_eur, percent_vs_steam,
                    cheapest["item"].get("float_value"), f"https://csfloat.com/item/{cheapest['id']}",
                )
                if telegram_notify.send_message(msg):
                    db.record_alert(conn, name, cheapest["id"], cheapest["price"])
                    logger.info("Telegram-Alarm gesendet fuer %s (%.1f%%, %.2f EUR).",
                                name, percent_vs_steam, csfloat_price_eur)
    else:
        logger.warning("%s: kein Steam-Preis verfuegbar, Vergleich uebersprungen.", name)

    db.save_analysis(conn, analysis)  # Analyse-Zeile fuer Dashboard-Historie

    return {
        "name": name, "ok": True,
        "message": "OK" if steam_price_eur is not None else "OK (kein Steam-Preis)",
        "csfloat_eur": round(csfloat_price_eur, 2),
        "steam_eur": round(steam_price_eur, 2) if steam_price_eur is not None else None,
        "percent_vs_steam": round(percent_vs_steam, 1) if percent_vs_steam is not None else None,
        "is_good_deal": is_good_deal,
    }


if __name__ == "__main__":
    main()
