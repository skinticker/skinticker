"""Wertet die CSFloat-Handelshistorie aus:

1. Automatische Kaufpreise (EK) fuer aktuell gehaltene Items (Matching ueber
   market_hash_name + float, da die Steam-asset_id sich beim Kauf aendert).
2. Realisierte Bilanz: verkaufte Items gegen ihren urspruenglichen Kauf matchen
   -> realisierter Gewinn/Verlust (z.B. Kauf 900, Verkauf 1800 = +900).

Nicht eindeutig zuordenbare Items bleiben leer (-> manuelle Eingabe fuer EK).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _fkey(float_value) -> str | None:
    if not isinstance(float_value, (int, float)):
        return None
    return f"{float_value:.6f}"


def _extract(trade: dict) -> tuple | None:
    """(name, floatkey, price_cents, date) aus einem Trade, oder None."""
    c = trade.get("contract") or {}
    it = c.get("item") or {}
    name = it.get("market_hash_name")
    price = c.get("price")
    if not name or not isinstance(price, (int, float)):
        return None
    date = (trade.get("accepted_at") or trade.get("created_at") or "")[:10]
    return name, _fkey(it.get("float_value")), price, date


def build_auto_buy_prices(inventory: list, buyer_trades: list, usd_to_eur: float) -> dict:
    """{market_hash_name: {price_eur, buy_date, source}} fuer eindeutig zuordenbare Items."""
    by_name_float, by_name_only = {}, {}
    for t in buyer_trades:
        ext = _extract(t)
        if not ext:
            continue
        name, fk, price, date = ext
        if fk is not None:
            by_name_float.setdefault((name, fk), (price, date))
        by_name_only.setdefault(name, []).append((price, date))

    inv_float, inv_name_floatless = {}, {}
    for item in inventory:
        name = item.get("market_hash_name")
        if not name:
            continue
        fk = _fkey(item.get("float_value"))
        if fk is not None:
            inv_float[(name, fk)] = inv_float.get((name, fk), 0) + 1
        else:
            inv_name_floatless[name] = inv_name_floatless.get(name, 0) + 1

    matched_prices, matched_date = {}, {}
    for (name, fk) in inv_float:
        hit = by_name_float.get((name, fk))
        if hit:
            matched_prices.setdefault(name, []).append(hit[0])
            matched_date[name] = hit[1]
    for name, owned in inv_name_floatless.items():
        buys = by_name_only.get(name, [])
        if owned == 1 and len(buys) == 1:
            matched_prices.setdefault(name, []).append(buys[0][0])
            matched_date[name] = buys[0][1]

    result = {}
    for name, prices in matched_prices.items():
        result[name] = {
            "price_eur": round(sum(prices) / len(prices) / 100 * usd_to_eur, 2),
            "buy_date": matched_date.get(name),
            "source": "csfloat",
        }
    return result


def build_realized(buyer_trades: list, seller_trades: list, usd_to_eur: float) -> dict:
    """Matcht Verkaeufe gegen Kaeufe (ueber name+float) und bildet die realisierte Bilanz."""
    # Kaeufe je (name, floatkey) sammeln; jeder Float ist praktisch eindeutig.
    buys: dict[tuple, list] = {}
    for t in buyer_trades:
        ext = _extract(t)
        if ext and ext[1] is not None:
            name, fk, price, date = ext
            buys.setdefault((name, fk), []).append((price, date))

    trades_out, used = [], {}
    for t in seller_trades:
        ext = _extract(t)
        if not ext or ext[1] is None:
            continue
        name, fk, sell_price, sell_date = ext
        key = (name, fk)
        candidates = buys.get(key)
        if not candidates:
            continue
        idx = used.get(key, 0)
        if idx >= len(candidates):
            continue  # jeder Kauf nur einmal gegen einen Verkauf matchen
        buy_price, buy_date = candidates[idx]
        used[key] = idx + 1
        profit_eur = round((sell_price - buy_price) / 100 * usd_to_eur, 2)
        buy_eur = round(buy_price / 100 * usd_to_eur, 2)
        sell_eur = round(sell_price / 100 * usd_to_eur, 2)
        icon = ((t.get("contract") or {}).get("item") or {}).get("icon_url")
        trades_out.append({
            "name": name, "buy_eur": buy_eur, "sell_eur": sell_eur,
            "profit_eur": profit_eur,
            "profit_pct": round((sell_price - buy_price) / buy_price * 100, 1) if buy_price else None,
            "buy_date": buy_date, "sell_date": sell_date,
            "icon_url": icon,
        })

    trades_out.sort(key=lambda x: x["sell_date"] or "", reverse=True)
    total = round(sum(t["profit_eur"] for t in trades_out), 2)
    return {
        "realized_total_eur": total,
        "trade_count": len(trades_out),
        "invested_eur": round(sum(t["buy_eur"] for t in trades_out), 2),
        "revenue_eur": round(sum(t["sell_eur"] for t in trades_out), 2),
        "best": max(trades_out, key=lambda x: x["profit_eur"], default=None),
        "worst": min(trades_out, key=lambda x: x["profit_eur"], default=None),
        "trades": trades_out[:100],
    }


def build_hold_items(pending_trades: list, usd_to_eur: float) -> list[dict]:
    """Aggregiert CSFloat-Pending-Buyer-Trades (Escrow/Hold) je Item: gekaufte Menge,
    Durchschnitts-EK (EUR), Kaufdatum, Hold-Ende, Icon, Float. Das sind gekaufte Items,
    die noch nicht ausgeliefert wurden - wie die Steam-Hold-Items provisorisch anzeigen.
    """
    agg: dict[str, dict] = {}
    for t in pending_trades:
        con = t.get("contract") or {}
        it = con.get("item") or {}
        name = it.get("market_hash_name")
        price = con.get("price")
        if not name or not isinstance(price, (int, float)):
            continue
        a = agg.setdefault(name, {"qty": 0, "cents": 0, "date": None, "hold": None,
                                  "icon": None, "float": None})
        a["qty"] += 1
        a["cents"] += price
        a["date"] = a["date"] or (t.get("accepted_at") or t.get("created_at") or "")[:10]
        a["hold"] = a["hold"] or t.get("trade_protection_ends_at")
        a["icon"] = a["icon"] or it.get("icon_url")
        if a["float"] is None:
            a["float"] = it.get("float_value")
    out = []
    for name, a in agg.items():
        out.append({
            "name": name, "qty": a["qty"],
            "buy_eur": round(a["cents"] / 100 * usd_to_eur / a["qty"], 2),
            "buy_date": a["date"], "hold_until": a["hold"],
            "icon": a["icon"], "float_value": a["float"],
        })
    return out


def sync_all(client, usd_to_eur: float) -> tuple[dict | None, dict]:
    """Holt Kauf- + Verkauf-Trades (und Inventar fuer Auto-EK) und liefert
    (auto_buy_prices, realized). auto ist None, wenn das Steam-Inventar gerade
    nicht abrufbar ist (z.B. privat) - die Bilanz wird davon nicht blockiert.
    """
    buyer_trades = client.get_my_buyer_trades()
    seller_trades = client.get_my_seller_trades()
    try:
        inventory = client.get_my_inventory()
        auto = build_auto_buy_prices(inventory, buyer_trades, usd_to_eur)
    except Exception as exc:
        logger.warning("Inventar fuer Auto-EK nicht abrufbar (%s) - behalte bisherige Auto-EK.", exc)
        auto = None
    realized = build_realized(buyer_trades, seller_trades, usd_to_eur)
    logger.info("Trade-Sync: %s Kaeufe, %s Verkaeufe, %s realisierte Trades (Bilanz %.2f EUR).",
                len(buyer_trades), len(seller_trades), realized["trade_count"],
                realized["realized_total_eur"])
    logger.info("Trade-Sync: %s Auto-EK, %s realisierte Trades (Bilanz %.2f EUR).",
                len(auto), realized["trade_count"], realized["realized_total_eur"])
    return auto, realized
