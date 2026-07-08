"""Telegram-Benachrichtigungen. Token/Chat-ID kommen aus der .env (nie im Code)."""

from __future__ import annotations

import html
import logging
import os

import requests

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def send_message(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.info("Telegram nicht konfiguriert (TELEGRAM_BOT_TOKEN/CHAT_ID fehlen) - Nachricht uebersprungen.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            logger.warning("Telegram HTTP %s: %s", resp.status_code, resp.text[:200])
            return False
        return True
    except requests.RequestException as exc:
        logger.warning("Telegram-Sendefehler: %s", exc)
        return False


def format_deal(name: str, csfloat_eur: float, steam_eur: float, percent_vs_steam: float,
                float_value: float | None, listing_url: str | None) -> str:
    saving = -percent_vs_steam
    lines = [
        "🔥 <b>CS2 Snipe-Alarm</b>",
        f"<b>{html.escape(name)}</b>",
        f"CSFloat: <b>{csfloat_eur:.2f} €</b>",
        f"Steam: {steam_eur:.2f} €",
        f"Ersparnis: <b>{saving:.1f}%</b> unter Steam",
    ]
    if float_value is not None:
        lines.append(f"Float: {float_value:.6f}")
    if listing_url:
        lines.append(f'<a href="{html.escape(listing_url)}">→ Zum Angebot auf CSFloat</a>')
    return "\n".join(lines)
