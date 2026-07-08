"""CSFloat-Kontostand (aktiv + ausstehend) abrufen, cachen und rate-limit-schonend
aktualisieren.

Der Kontostand kommt vom inoffiziellen /me-Endpoint (per Browser reverse-engineered,
kann sich jederzeit aendern -> wird defensiv geparst). CSFloat liefert alle Betraege
in US-Cent; die Umrechnung in USD (/100) uebernimmt config.save_csfloat_balance().

Kern ist refresh_budget(): Anhand der zuletzt gesehenen Rate-Limit-Header des
/me-Endpoints wird entschieden, OB ueberhaupt abgefragt werden darf - so bleiben
immer genug Calls fuer die eigentlichen /me-Aufgaben (Inventar, Trades) uebrig -
und WIE oft (ein sicheres Mindestintervall, das das freie Budget ueber das
Rate-Limit-Fenster streckt).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import config

logger = logging.getLogger(__name__)

# Endpoint, dessen Kontingent der Kontostand-Abruf verbraucht.
ME_PATH = "/me"

# So viele Calls im aktuellen Fenster fuer andere /me-Aufgaben (Inventar-Sync,
# Trade-Historie) reserviert lassen - der Balance-Abruf fasst sie nicht an.
RESERVE_CALLS = 10

# Nie haeufiger als so oft neu abrufen, egal wie viel Budget frei ist (Sekunden).
MIN_REFRESH_SECONDS = 60


def _seconds_since(iso_ts: str | None, now: float) -> float | None:
    if not iso_ts:
        return None
    try:
        return now - datetime.fromisoformat(iso_ts).timestamp()
    except (ValueError, TypeError):
        return None


def refresh_budget(rl_entry: dict | None, last_fetched_at: str | None = None,
                   now: float | None = None) -> dict:
    """Entscheidet anhand des /me-Rate-Limit-Stands, ob (erneut) abgefragt werden darf.

    rl_entry ist der persistierte Stand aus data/rate_limits.json fuer /me:
    {limit, remaining, reset_at, updated_at} - oder None, solange fuer /me noch
    kein Call abgesetzt wurde.

    Gibt ein Dict fuer Frontend + Server zurueck (allowed, reason, spendable,
    recommended_interval_seconds, ...). "spendable" = frei verwendbare Calls nach
    Abzug der Reserve.
    """
    now = now if now is not None else time.time()

    limit = eff_remaining = reset_in = spendable = None
    if rl_entry:
        limit = rl_entry.get("limit")
        remaining = rl_entry.get("remaining")
        reset_at = rl_entry.get("reset_at")
        reset_in = max(0.0, reset_at - now) if reset_at else None
        if reset_at and reset_at < now and limit is not None:
            # Fenster ist durch -> Kontingent wieder voll.
            eff_remaining, reset_in = limit, 0.0
        else:
            eff_remaining = remaining
        if eff_remaining is not None:
            spendable = eff_remaining - RESERVE_CALLS

    # Empfohlenes Mindestintervall: das freie Budget so ueber die Zeit bis zum
    # Reset strecken, dass es reicht - aber nie schneller als MIN_REFRESH_SECONDS.
    interval = float(MIN_REFRESH_SECONDS)
    if spendable and spendable > 0 and reset_in:
        interval = max(interval, reset_in / spendable)

    since_last = _seconds_since(last_fetched_at, now)

    if rl_entry is None:
        allowed, reason = True, "Erster Abruf - Kontingent noch unbekannt."
    elif spendable is not None and spendable <= 0:
        allowed = False
        reason = (f"Kontingent-Reserve geschuetzt: nur {eff_remaining}/{limit} frei, "
                  f"{RESERVE_CALLS} bleiben fuer Inventar/Trades reserviert.")
    elif since_last is not None and since_last < interval:
        wait = int(interval - since_last)
        allowed = False
        reason = f"Zuletzt vor {int(since_last)}s abgerufen - naechster Abruf in {wait}s frei."
    else:
        allowed, reason = True, "Genug Kontingent frei."

    return {
        "allowed": allowed,
        "reason": reason,
        "limit": limit,
        "remaining": eff_remaining,
        "reserve": RESERVE_CALLS,
        "spendable": spendable,
        "reset_in_seconds": round(reset_in) if reset_in is not None else None,
        "recommended_interval_seconds": round(interval),
        "seconds_since_last": round(since_last) if since_last is not None else None,
    }


def current_budget() -> dict:
    """Budget-Stand ohne API-Call - fuer das Dashboard (aus den Cache-Dateien)."""
    cached = config.load_csfloat_balance()
    return refresh_budget(config.load_rate_limits().get(ME_PATH), cached.get("fetched_at"))


def _parse_balance(data) -> tuple:
    """Zieht (balance_cents, pending_cents) aus der /me-Antwort. Der Endpoint ist
    inoffiziell -> Werte koennen top-level oder unter 'user' liegen."""
    if not isinstance(data, dict):
        return None, None
    user = data.get("user") if isinstance(data.get("user"), dict) else data
    return user.get("balance"), user.get("pending_balance")


def refresh(client, force: bool = False) -> dict:
    """Aktualisiert den Kontostand, sofern das Rate-Limit-Budget es zulaesst.

    Verbraucht nur dann einen echten /me-Call, wenn refresh_budget() gruenes Licht
    gibt (oder force=True). Ansonsten wird der gecachte Stand unveraendert
    zurueckgegeben. Ergebnis: {refreshed, message, balance, budget}.
    """
    cached = config.load_csfloat_balance()
    budget = refresh_budget(config.load_rate_limits().get(ME_PATH), cached.get("fetched_at"))

    if not force and not budget["allowed"]:
        return {"refreshed": False, "message": budget["reason"],
                "balance": cached, "budget": budget}

    try:
        data = client.get_account()
    except Exception as exc:  # Netzwerk/HTTP/429-Ausschoepfung -> Cache behalten
        logger.warning("Kontostand-Abruf fehlgeschlagen: %s", exc)
        return {"refreshed": False, "message": f"Abruf fehlgeschlagen: {exc}",
                "balance": cached, "budget": budget}

    balance_cents, pending_cents = _parse_balance(data)
    if balance_cents is None and pending_cents is None:
        return {"refreshed": False,
                "message": "Antwort ohne balance/pending_balance (Endpoint evtl. geaendert).",
                "balance": cached, "budget": budget}

    saved = config.save_csfloat_balance(balance_cents, pending_cents)
    # Nach dem Call liegen frische Header vor -> Budget/naechstes Intervall neu berechnen.
    budget = refresh_budget(config.load_rate_limits().get(ME_PATH), saved.get("fetched_at"))
    logger.info("Kontostand aktualisiert: %s USD aktiv, %s USD pending.",
                saved.get("balance_usd"), saved.get("pending_usd"))
    return {"refreshed": True, "message": "Kontostand aktualisiert.",
            "balance": saved, "budget": budget}
