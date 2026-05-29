import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("/opt/rename-webhook/rechnungen.db")

STEUER_KATEGORIEN = [
    "Allgemeines",
    "Forst- und Landwirtschaft",
    "Gehaltsabrechnungen",
    "Haus und Hof",
    "Medikamente-Arzt",
    "Photovoltaik",
    "Private Rechnung",
    "Steuer und Beratung",
    "Vermietung",
    "Verpachtung",
    "Versicherungen",
    "Werbungskosten",
]


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rechnungen (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                datum            TEXT,
                firma            TEXT,
                betrag_raw       TEXT,
                betrag           REAL,
                kategorie_rename TEXT,
                schlagwort       TEXT,
                roga_kuerzel     TEXT,
                steuer_kategorie TEXT,
                dateiname        TEXT,
                erstellt_am      TEXT,
                dropbox_pfad     TEXT
            )
        """)
        try:
            conn.execute("ALTER TABLE rechnungen ADD COLUMN dropbox_pfad TEXT")
        except sqlite3.OperationalError:
            pass
        conn.commit()


def _parse_betrag(raw: str) -> float | None:
    if not raw:
        return None
    m = re.search(r"([+-]?\d+[.,]\d+)", raw)
    if not m:
        return None
    return float(m.group(1).replace(",", "."))


def parse_filename(filename: str) -> dict:
    """Extrahiert Felder aus dem Rename-Schema YYYY-MM-DD_Kategorie_Firma_Schlagwort[_RoGaXX][_Betrag]"""
    stem = Path(filename).stem
    parts = stem.split("_")

    datum = parts[0] if parts else ""
    kategorie = parts[1] if len(parts) > 1 else ""
    firma = parts[2] if len(parts) > 2 else ""

    roga_kuerzel = ""
    betrag_raw = ""
    schlagwort_parts = []

    for part in parts[3:]:
        if re.match(r"RoGa\d+[A-Za-z]?$", part):
            roga_kuerzel = part
        elif re.match(r"[+-]\d+[.,]\d+€$", part):
            betrag_raw = part
        else:
            schlagwort_parts.append(part)

    return {
        "datum": datum,
        "firma": firma,
        "betrag_raw": betrag_raw,
        "betrag": _parse_betrag(betrag_raw),
        "kategorie_rename": kategorie,
        "schlagwort": "_".join(schlagwort_parts),
        "roga_kuerzel": roga_kuerzel,
    }


def insert_rechnung(dateiname: str, steuer_kategorie: str | None = None) -> int | None:
    fields = parse_filename(dateiname)
    if fields["kategorie_rename"].lower() != "rechnung":
        return None
    if not steuer_kategorie and fields["roga_kuerzel"]:
        steuer_kategorie = "Vermietung"
    if not steuer_kategorie:
        steuer_kategorie = "Allgemeines"

    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """INSERT INTO rechnungen
               (datum, firma, betrag_raw, betrag, kategorie_rename, schlagwort,
                roga_kuerzel, steuer_kategorie, dateiname, erstellt_am)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                fields["datum"], fields["firma"], fields["betrag_raw"],
                fields["betrag"], fields["kategorie_rename"], fields["schlagwort"],
                fields["roga_kuerzel"], steuer_kategorie, dateiname,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return cur.lastrowid


def dateiname_exists(dateiname: str) -> bool:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM rechnungen WHERE dateiname = ?", (dateiname,)
        ).fetchone()
    return row is not None


def insert_raw(datum: str, firma: str, betrag_raw: str, betrag: float | None,
               kategorie_rename: str, schlagwort: str, roga_kuerzel: str,
               steuer_kategorie: str, dateiname: str) -> int:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """INSERT INTO rechnungen
               (datum, firma, betrag_raw, betrag, kategorie_rename, schlagwort,
                roga_kuerzel, steuer_kategorie, dateiname, erstellt_am)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (datum, firma, betrag_raw, betrag, kategorie_rename, schlagwort,
             roga_kuerzel, steuer_kategorie, dateiname,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def get_all(limit: int = 200, offset: int = 0,
            steuer_kategorie: str | None = None,
            datum_von: str | None = None,
            datum_bis: str | None = None) -> list[dict]:
    init_db()
    where, params = [], []
    if steuer_kategorie:
        where.append("steuer_kategorie = ?"); params.append(steuer_kategorie)
    if datum_von:
        where.append("datum >= ?"); params.append(datum_von)
    if datum_bis:
        where.append("datum <= ?"); params.append(datum_bis)
    sql = "SELECT * FROM rechnungen"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY datum DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_one(row_id: int) -> dict | None:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM rechnungen WHERE id = ?", (row_id,)).fetchone()
    return dict(row) if row else None


def update_rechnung(row_id: int, **fields) -> bool:
    allowed = {"datum", "firma", "betrag_raw", "betrag", "schlagwort",
               "steuer_kategorie", "dateiname", "dropbox_pfad"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    init_db()
    sets = ", ".join(f"{k} = ?" for k in updates)
    params = list(updates.values()) + [row_id]
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(f"UPDATE rechnungen SET {sets} WHERE id = ?", params)
        conn.commit()
    return True
