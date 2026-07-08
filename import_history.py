"""Importiert die History aus dem CSV-Backup des Inventar-Sheets nach SQLite.

Rein lesend gegenueber dem Backup; das Google Sheet wird nicht angefasst.
Mehrfaches Ausfuehren ist sicher: vorherige Sheet-Importe werden erst
entfernt und dann frisch importiert (erkennbar an source='sheet_import').

Die History ist ueber die Zeit gewachsen, deshalb muss der Parser mit
mehreren Formaten umgehen:
- alte Zeilen: 6-9 Spalten (nur Gesamtwerte, keine Kategorien)
- neue Zeilen: 36 Spalten (plus 6 Kategorien x 5 Werte)
- Zahlen mal nackt mit deutschem Komma ('4258,1'), mal formatiert
  ('EUR 4.296,78' / '$ 4.916,22' mit Tausenderpunkten)
"""

import csv
import logging
import re
import sys
from datetime import datetime

import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

CATEGORIES = ["Skins", "Cases", "Sticker", "Agents", "Charms", "Andere"]

# Spaltenlayout (0-basiert): 0=Timestamp, 1=FX, 2=Sum Skinport EUR,
# 3=Sum CSFloat USD, 4=Sum CSFloat EUR, 5=Items, danach je Kategorie 5 Spalten.
CATEGORY_BLOCK_START = 6
CATEGORY_BLOCK_SIZE = 5

TS_FORMATS = ["%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"]


def parse_number(raw: str) -> float | None:
    """'EUR 4.296,78' / '$ 4.916,22' / '4258,1' / '373' -> float. Leer -> None."""
    if raw is None:
        return None
    s = re.sub(r"[^\d,.\-]", "", str(raw))
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_timestamp(raw: str) -> str | None:
    raw = (raw or "").strip()
    for fmt in TS_FORMATS:
        try:
            return datetime.strptime(raw, fmt).isoformat()
        except ValueError:
            continue
    return None


def import_file(csv_path: str) -> None:
    with open(csv_path, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))

    if not rows or rows[0][0] != "Timestamp":
        raise SystemExit(f"Unerwartetes Format: erste Zelle ist {rows[0][0]!r}, nicht 'Timestamp'.")

    conn = db.get_connection()
    try:
        removed = conn.execute("DELETE FROM inventory_history WHERE source='sheet_import'").rowcount
        removed_cat = conn.execute("DELETE FROM inventory_history_category WHERE source='sheet_import'").rowcount
        if removed or removed_cat:
            logger.info("Vorherigen Import entfernt (%s Gesamt-, %s Kategorie-Zeilen).", removed, removed_cat)

        imported, skipped = 0, 0
        for line_no, row in enumerate(rows[1:], start=2):
            ts = parse_timestamp(row[0] if row else "")
            if ts is None:
                skipped += 1
                continue

            def cell(i):
                return row[i] if i < len(row) else None

            conn.execute(
                """
                INSERT INTO inventory_history
                    (ts, fx_usd_eur, sum_skinport_eur, sum_csfloat_usd, sum_csfloat_eur, total_items, source)
                VALUES (?, ?, ?, ?, ?, ?, 'sheet_import')
                """,
                (
                    ts,
                    parse_number(cell(1)),
                    parse_number(cell(2)),
                    parse_number(cell(3)),
                    parse_number(cell(4)),
                    parse_number(cell(5)),
                ),
            )

            for ci, cat in enumerate(CATEGORIES):
                base = CATEGORY_BLOCK_START + ci * CATEGORY_BLOCK_SIZE
                if base >= len(row):
                    break
                values = [parse_number(cell(base + k)) for k in range(CATEGORY_BLOCK_SIZE)]
                if all(v is None for v in values):
                    continue
                conn.execute(
                    """
                    INSERT INTO inventory_history_category
                        (ts, category, steam_eur, skinport_eur, csfloat_usd, csfloat_eur, items, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'sheet_import')
                    """,
                    (ts, cat, *values),
                )

            imported += 1

        conn.commit()
        logger.info("Import fertig: %s Zeilen importiert, %s ohne Timestamp uebersprungen.", imported, skipped)
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Aufruf: python import_history.py <pfad/zur/History.csv>")
    import_file(sys.argv[1])
