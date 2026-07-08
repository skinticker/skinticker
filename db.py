"""SQLite-Speicherung fuer CSFloat-Preisabfragen."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("data/prices.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS csfloat_listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_hash_name TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    rank INTEGER NOT NULL,
    listing_id TEXT NOT NULL,
    price_usd_cents INTEGER NOT NULL,
    float_value REAL,
    seller_username TEXT
);
CREATE INDEX IF NOT EXISTS idx_csfloat_listings_name_time
    ON csfloat_listings(market_hash_name, checked_at);

CREATE TABLE IF NOT EXISTS inventory_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    fx_usd_eur REAL,
    sum_skinport_eur REAL,
    sum_csfloat_usd REAL,
    sum_csfloat_eur REAL,
    total_items INTEGER,
    source TEXT NOT NULL DEFAULT 'python'
);
CREATE INDEX IF NOT EXISTS idx_inventory_history_ts ON inventory_history(ts);

CREATE TABLE IF NOT EXISTS inventory_history_category (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    category TEXT NOT NULL,
    steam_eur REAL,
    skinport_eur REAL,
    csfloat_usd REAL,
    csfloat_eur REAL,
    items INTEGER,
    source TEXT NOT NULL DEFAULT 'python'
);
CREATE INDEX IF NOT EXISTS idx_inventory_history_category_ts
    ON inventory_history_category(ts, category);

CREATE TABLE IF NOT EXISTS inventory_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    market_hash_name TEXT NOT NULL,
    category TEXT NOT NULL,
    qty INTEGER NOT NULL,
    usd_each REAL,
    eur_each REAL,
    steam_eur_each REAL,
    float_value REAL,
    icon_url TEXT,
    inspect_link TEXT,
    rarity INTEGER,
    is_stattrak INTEGER,
    is_souvenir INTEGER,
    in_csfloat INTEGER,
    manual_qty INTEGER
);
CREATE INDEX IF NOT EXISTS idx_inventory_items_ts ON inventory_items(ts);
CREATE INDEX IF NOT EXISTS idx_inventory_items_name ON inventory_items(market_hash_name, ts);

CREATE TABLE IF NOT EXISTS deal_alerts (
    market_hash_name TEXT PRIMARY KEY,
    last_alerted_at TEXT NOT NULL,
    last_listing_id TEXT,
    last_price_cents INTEGER
);

CREATE TABLE IF NOT EXISTS steam_cache (
    market_hash_name TEXT PRIMARY KEY,
    price_eur REAL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS investment_history (
    day TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    realized_eur REAL,
    barter_eur REAL,
    unrealized_eur REAL,
    total_eur REAL
);

CREATE TABLE IF NOT EXISTS price_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_hash_name TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    csfloat_price_usd_cents INTEGER,
    csfloat_listing_id TEXT,
    csfloat_float_value REAL,
    steam_price_eur REAL,
    estimated_buff_eur REAL,
    percent_vs_buff REAL,
    is_good_deal INTEGER,
    csfloat_icon_url TEXT
);
CREATE INDEX IF NOT EXISTS idx_price_analysis_name_time
    ON price_analysis(market_hash_name, checked_at);
"""

# Spalten, die spaeter dazukamen: bei bestehenden DBs per ALTER TABLE nachruesten.
MIGRATIONS = [
    ("price_analysis", "csfloat_icon_url", "TEXT"),
    ("deal_alerts", "last_price_cents", "INTEGER"),
    ("inventory_items", "stickers", "TEXT"),  # JSON: angebrachte Sticker + Charms
    ("inventory_items", "pending_trade", "INTEGER"),  # 1 = provisorisch aus Trade (im 7-Tage-Hold)
]


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    # WAL: Leser blockieren Schreiber nicht (3 Container teilen sich die DB).
    # busy_timeout: bei kurzzeitig gesperrter DB warten statt sofort "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    for table, column, coltype in MIGRATIONS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    conn.commit()
    return conn


def save_listings(conn: sqlite3.Connection, market_hash_name: str, listings: list[dict]) -> None:
    """Speichert eine Momentaufnahme der (bereits sortierten) Listings fuer ein Item."""
    checked_at = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            market_hash_name,
            checked_at,
            rank,
            listing["id"],
            listing["price"],
            listing["item"].get("float_value"),
            listing.get("seller", {}).get("username"),
        )
        for rank, listing in enumerate(listings, start=1)
    ]
    conn.executemany(
        """
        INSERT INTO csfloat_listings
            (market_hash_name, checked_at, rank, listing_id, price_usd_cents, float_value, seller_username)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()


def load_steam_cache(conn: sqlite3.Connection) -> dict[str, float]:
    """market_hash_name -> Preis in EUR (nur Eintraege mit vorhandenem Preis)."""
    rows = conn.execute("SELECT market_hash_name, price_eur FROM steam_cache WHERE price_eur IS NOT NULL")
    return {name: price for name, price in rows}


def last_manual_quantities(conn: sqlite3.Connection) -> dict[str, int]:
    """Manuelle Mengen (Lagereinheiten etc.) aus dem letzten Snapshot rekonstruieren.

    Dient zum einmaligen Seeden von data/manual_items.json (Migration von der frueheren
    Google-Sheets-Anbindung), damit die manuellen Items nicht verloren gehen.
    """
    row = conn.execute("SELECT MAX(ts) FROM inventory_items").fetchone()
    if not row or not row[0]:
        return {}
    return {
        name: qty
        for name, qty in conn.execute(
            "SELECT market_hash_name, manual_qty FROM inventory_items WHERE ts = ? AND manual_qty > 0",
            (row[0],),
        )
    }


def stale_steam_names(conn: sqlite3.Connection, names: list[str], max_age_hours: int = 24) -> list[str]:
    """Welche Namen haben keinen oder einen veralteten Cache-Eintrag? Aelteste zuerst."""
    if not names:
        return []
    placeholders = ",".join("?" * len(names))
    cached = dict(
        conn.execute(
            f"SELECT market_hash_name, fetched_at FROM steam_cache WHERE market_hash_name IN ({placeholders})",
            names,
        )
    )
    cutoff = datetime.now(timezone.utc).timestamp() - max_age_hours * 3600

    def age_key(name: str) -> float:
        fetched = cached.get(name)
        return datetime.fromisoformat(fetched).timestamp() if fetched else 0.0

    stale = [n for n in names if n not in cached or age_key(n) < cutoff]
    stale.sort(key=age_key)
    return stale


def upsert_steam_price(conn: sqlite3.Connection, name: str, price_eur: float | None) -> None:
    conn.execute(
        """
        INSERT INTO steam_cache (market_hash_name, price_eur, fetched_at)
        VALUES (?, ?, ?)
        ON CONFLICT(market_hash_name) DO UPDATE SET price_eur=excluded.price_eur, fetched_at=excluded.fetched_at
        """,
        (name, price_eur, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def save_inventory_items(conn: sqlite3.Connection, ts: str, rows: list[dict]) -> None:
    """Speichert die Einzel-Items eines Bewertungslaufs (fuer Dashboard-Karten und
    per-Item-Wertverlauf). Haengt an - aeltere Snapshots bleiben als Historie erhalten."""
    conn.executemany(
        """
        INSERT INTO inventory_items
            (ts, market_hash_name, category, qty, usd_each, eur_each, steam_eur_each,
             float_value, icon_url, inspect_link, rarity, is_stattrak, is_souvenir,
             in_csfloat, manual_qty, stickers, pending_trade)
        VALUES (:ts, :market_hash_name, :category, :qty, :usd_each, :eur_each, :steam_eur_each,
                :float_value, :icon_url, :inspect_link, :rarity, :is_stattrak, :is_souvenir,
                :in_csfloat, :manual_qty, :stickers, :pending_trade)
        """,
        [{**r, "ts": ts} for r in rows],
    )
    conn.commit()


def save_inventory_snapshot(conn: sqlite3.Connection, totals: dict, category_sums: dict) -> str:
    """Speichert einen Bewertungs-Snapshot (Gesamt + je Kategorie) mit source='python'.

    Gibt den Zeitstempel zurueck, damit Einzel-Items denselben ts bekommen.
    """
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO inventory_history
            (ts, fx_usd_eur, sum_skinport_eur, sum_csfloat_usd, sum_csfloat_eur, total_items, source)
        VALUES (?, ?, ?, ?, ?, ?, 'python')
        """,
        (
            ts,
            totals["fx_usd_eur"],
            totals["sum_skinport_eur"],
            totals["sum_csfloat_usd"],
            totals["sum_csfloat_eur"],
            totals["total_items"],
        ),
    )
    for category, s in category_sums.items():
        conn.execute(
            """
            INSERT INTO inventory_history_category
                (ts, category, steam_eur, skinport_eur, csfloat_usd, csfloat_eur, items, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'python')
            """,
            (ts, category, s["steam_eur"], s["skinport_eur"], s["csfloat_usd"], s["csfloat_eur"], s["items"]),
        )
    conn.commit()
    return ts


def should_alert(conn: sqlite3.Connection, name: str, price_cents: int | None) -> bool:
    """True nur, wenn dieser Durchlauf einen GUENSTIGEREN Preis findet als beim
    letzten Alarm (oder wenn fuer dieses Item noch nie alarmiert wurde).

    So wird nicht bei jedem Durchlauf fuer denselben Deal gepingt - erst wenn
    der Preis weiter faellt, kommt eine neue Nachricht.
    """
    if price_cents is None:
        return False
    row = conn.execute(
        "SELECT last_price_cents FROM deal_alerts WHERE market_hash_name = ?",
        (name,),
    ).fetchone()
    if row is None or row[0] is None:
        return True
    return price_cents < row[0]


def record_alert(conn: sqlite3.Connection, name: str, listing_id: str | None,
                 price_cents: int | None) -> None:
    conn.execute(
        """
        INSERT INTO deal_alerts (market_hash_name, last_alerted_at, last_listing_id, last_price_cents)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(market_hash_name) DO UPDATE SET
            last_alerted_at = excluded.last_alerted_at,
            last_listing_id = excluded.last_listing_id,
            last_price_cents = excluded.last_price_cents
        """,
        (name, datetime.now(timezone.utc).isoformat(), listing_id, price_cents),
    )
    conn.commit()


def save_investment_snapshot(conn: sqlite3.Connection, parts: dict) -> None:
    """Haelt das Investment-Gesamtergebnis einmal pro Tag fest (letzter Wert des
    Tages gewinnt). Wird vom Dashboard bei jeder Daten-Erzeugung aktualisiert -
    so entsteht nebenbei eine Tages-Historie fuer den Verlaufs-Chart."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS investment_history (
            day TEXT PRIMARY KEY, ts TEXT NOT NULL,
            realized_eur REAL, barter_eur REAL, unrealized_eur REAL, total_eur REAL)
    """)
    now = datetime.now(timezone.utc)
    conn.execute(
        """
        INSERT INTO investment_history (day, ts, realized_eur, barter_eur, unrealized_eur, total_eur)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(day) DO UPDATE SET
            ts = excluded.ts,
            realized_eur = excluded.realized_eur,
            barter_eur = excluded.barter_eur,
            unrealized_eur = excluded.unrealized_eur,
            total_eur = excluded.total_eur
        """,
        (now.date().isoformat(), now.isoformat(), parts.get("realized_eur"),
         parts.get("barter_eur"), parts.get("unrealized_eur"), parts.get("total_eur")),
    )
    conn.commit()


def load_investment_history(conn: sqlite3.Connection) -> list:
    """Tages-Historie des Gesamtergebnisses: [[day, realized, barter, unrealized, total], ...]."""
    try:
        return [list(r) for r in conn.execute(
            "SELECT day, realized_eur, barter_eur, unrealized_eur, total_eur "
            "FROM investment_history ORDER BY day"
        )]
    except sqlite3.OperationalError:  # Tabelle existiert noch nicht (alte DB, nie geschrieben)
        return []


def save_analysis(conn: sqlite3.Connection, analysis: dict) -> None:
    """Speichert das Ergebnis eines Preisvergleichs (CSFloat vs. Steam-Marktpreis).

    Hinweis: estimated_buff_eur/percent_vs_buff sind historische Spaltennamen -
    verglichen wird seit der Umstellung direkt gegen den Steam-Preis."""
    conn.execute(
        """
        INSERT INTO price_analysis
            (market_hash_name, checked_at, csfloat_price_usd_cents, csfloat_listing_id,
             csfloat_float_value, steam_price_eur, estimated_buff_eur, percent_vs_buff, is_good_deal,
             csfloat_icon_url)
        VALUES (:market_hash_name, :checked_at, :csfloat_price_usd_cents, :csfloat_listing_id,
                :csfloat_float_value, :steam_price_eur, :estimated_buff_eur, :percent_vs_buff, :is_good_deal,
                :csfloat_icon_url)
        """,
        analysis,
    )
    conn.commit()
