"""SQLite der Planungsräume (Prototyp: `daten/planung.sqlite`, nicht im Git).

Schema so angelegt, dass es bei der Integration in `shared/vk_db.py` übernommen werden kann.
Entwürfe liegen bewusst **nicht** in `vereinstermine.json`: `/api/termine` gibt jedes Feld nach
außen, das nicht auf der Sperrliste steht – ein Entwurf dort wäre einen Fehler vom Leck entfernt.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from daten import DATEN_DIR

DB_FILE = DATEN_DIR / "planung.sqlite"

STATUS = {"vorschlag": "Vorschlag", "bestaetigt": "Bestätigt", "neu": "Neu", "verworfen": "Verworfen"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS planung_raum (
    id          INTEGER PRIMARY KEY,
    token       TEXT NOT NULL UNIQUE,      -- Link der Organisatorin
    gemeinde    TEXT NOT NULL,
    landkreis   TEXT NOT NULL,
    jahr        INTEGER NOT NULL,
    titel       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'offen',   -- offen | abgeschlossen
    erstellt_am TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS planung_verein (
    id          INTEGER PRIMARY KEY,
    raum_id     INTEGER NOT NULL REFERENCES planung_raum(id) ON DELETE CASCADE,
    verein_key  TEXT NOT NULL,
    verein_name TEXT NOT NULL,
    token       TEXT NOT NULL UNIQUE,      -- Link des Vereins (ohne Konto)
    fertig_am   TEXT,                      -- Verein hat „fertig“ gemeldet
    UNIQUE (raum_id, verein_key)
);
CREATE TABLE IF NOT EXISTS planung_termin (
    id            INTEGER PRIMARY KEY,
    raum_id       INTEGER NOT NULL REFERENCES planung_raum(id) ON DELETE CASCADE,
    verein_key    TEXT NOT NULL,
    datum         TEXT NOT NULL,
    uhrzeit       TEXT NOT NULL DEFAULT '',
    uhrzeit_bis   TEXT NOT NULL DEFAULT '',
    bezeichnung   TEXT NOT NULL,
    ort           TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'vorschlag',
    regel         TEXT NOT NULL DEFAULT '',  -- „wie 2026: 2. Samstag im Juli“
    datum_vorjahr TEXT NOT NULL DEFAULT '',
    geo_json      TEXT,                      -- _geo aus dem Vorjahr (Ort unverändert)
    geaendert_am  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_planung_termin_raum ON planung_termin(raum_id, datum);
"""


@contextmanager
def conn():
    DATEN_DIR.mkdir(exist_ok=True)
    c = sqlite3.connect(DB_FILE)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(_SCHEMA)


def jetzt() -> str:
    return datetime.now().isoformat(timespec="seconds")


def neuer_raum(gemeinde: str, landkreis: str, jahr: int, vereine: list[tuple[str, str]],
               vorschlaege: list[dict]) -> str:
    """Raum anlegen, Vereins-Links erzeugen, mit der Vorlage vorbefüllen. Gibt den Organisator-Token zurück."""
    token = secrets.token_urlsafe(16)
    with conn() as c:
        cur = c.execute("INSERT INTO planung_raum (token, gemeinde, landkreis, jahr, titel, erstellt_am) "
                        "VALUES (?,?,?,?,?,?)",
                        (token, gemeinde, landkreis, jahr, f"Jahresplanung {jahr} – {gemeinde}", jetzt()))
        raum_id = cur.lastrowid
        keys = {k for k, _ in vereine}
        for k, name in vereine:
            c.execute("INSERT INTO planung_verein (raum_id, verein_key, verein_name, token) VALUES (?,?,?,?)",
                      (raum_id, k, name, secrets.token_urlsafe(16)))
        for v in vorschlaege:
            if v.get("verein") in keys:
                c.execute("INSERT INTO planung_termin (raum_id, verein_key, datum, uhrzeit, uhrzeit_bis, bezeichnung, "
                          "ort, status, regel, datum_vorjahr, geo_json, geaendert_am) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (raum_id, v["verein"], v["datum"], v.get("uhrzeit", ""), v.get("uhrzeit_bis", ""),
                           v["bezeichnung"], v.get("ort", ""), "vorschlag", v.get("regel", ""),
                           v.get("datum_vorjahr", ""), json.dumps(v["_geo"], ensure_ascii=False) if v.get("_geo") else None,
                           jetzt()))
    return token


def raeume() -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT r.*, (SELECT COUNT(*) FROM planung_termin t WHERE t.raum_id = r.id "
                         "AND t.status != 'verworfen') AS termine FROM planung_raum r ORDER BY r.id DESC").fetchall()


def raum_per_token(token: str):
    with conn() as c:
        return c.execute("SELECT * FROM planung_raum WHERE token = ?", (token,)).fetchone()


def verein_per_token(token: str):
    with conn() as c:
        return c.execute("SELECT v.*, r.token AS raum_token FROM planung_verein v "
                         "JOIN planung_raum r ON r.id = v.raum_id WHERE v.token = ?", (token,)).fetchone()


def raum(raum_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM planung_raum WHERE id = ?", (raum_id,)).fetchone()


def vereine_im_raum(raum_id: int) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT v.*, "
            "(SELECT COUNT(*) FROM planung_termin t WHERE t.raum_id = v.raum_id AND t.verein_key = v.verein_key "
            " AND t.status != 'verworfen') AS termine, "
            "(SELECT COUNT(*) FROM planung_termin t WHERE t.raum_id = v.raum_id AND t.verein_key = v.verein_key "
            " AND t.status = 'vorschlag') AS offen "
            "FROM planung_verein v WHERE v.raum_id = ? ORDER BY lower(v.verein_name)", (raum_id,)).fetchall()


def termine_im_raum(raum_id: int, verein_key: str | None = None, ohne_verworfen: bool = False) -> list[dict]:
    sql = "SELECT * FROM planung_termin WHERE raum_id = ?"
    args: list = [raum_id]
    if verein_key:
        sql += " AND verein_key = ?"
        args.append(verein_key)
    if ohne_verworfen:
        sql += " AND status != 'verworfen'"
    with conn() as c:
        rows = c.execute(sql + " ORDER BY datum, uhrzeit, id", args).fetchall()
    out = []
    for r in rows:
        t = dict(r)
        t["verein"] = t["verein_key"]
        t["_pid"] = t["id"]
        t["id"] = f"p{t['id']}"   # nicht mit echten Termin-IDs verwechseln
        if t.get("geo_json"):
            t["_geo"] = json.loads(t["geo_json"])
        out.append(t)
    return out


def termin_aendern(raum_id: int, termin_id: int, verein_key: str | None, felder: dict) -> bool:
    """Felder eines Termins ändern. verein_key gesetzt = nur eigene Termine (Vereins-Link)."""
    erlaubt = {k: v for k, v in felder.items()
               if k in ("datum", "uhrzeit", "uhrzeit_bis", "bezeichnung", "ort", "status")}
    if not erlaubt:
        return False
    if "ort" in erlaubt:
        erlaubt["geo_json"] = None   # Ort geändert → Geo neu berechnen
    sql = "UPDATE planung_termin SET " + ", ".join(f"{k} = ?" for k in erlaubt) + ", geaendert_am = ? " \
          "WHERE id = ? AND raum_id = ?"
    args = list(erlaubt.values()) + [jetzt(), termin_id, raum_id]
    if verein_key:
        sql += " AND verein_key = ?"
        args.append(verein_key)
    with conn() as c:
        return c.execute(sql, args).rowcount == 1


def termin_neu(raum_id: int, verein_key: str, felder: dict) -> None:
    with conn() as c:
        c.execute("INSERT INTO planung_termin (raum_id, verein_key, datum, uhrzeit, uhrzeit_bis, bezeichnung, ort, "
                  "status, geaendert_am) VALUES (?,?,?,?,?,?,?,'neu',?)",
                  (raum_id, verein_key, felder["datum"], felder.get("uhrzeit", ""), felder.get("uhrzeit_bis", ""),
                   felder["bezeichnung"], felder.get("ort", ""), jetzt()))


def alle_vorschlaege_bestaetigen(raum_id: int, verein_key: str) -> int:
    with conn() as c:
        return c.execute("UPDATE planung_termin SET status = 'bestaetigt', geaendert_am = ? "
                         "WHERE raum_id = ? AND verein_key = ? AND status = 'vorschlag'",
                         (jetzt(), raum_id, verein_key)).rowcount


def fertig_melden(verein_id: int, fertig: bool) -> None:
    with conn() as c:
        c.execute("UPDATE planung_verein SET fertig_am = ? WHERE id = ?", (jetzt() if fertig else None, verein_id))


def raum_status(raum_id: int, status: str) -> None:
    with conn() as c:
        c.execute("UPDATE planung_raum SET status = ? WHERE id = ?", (status, raum_id))


def raum_loeschen(raum_id: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM planung_raum WHERE id = ?", (raum_id,))
