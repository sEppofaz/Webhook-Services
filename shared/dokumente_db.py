"""Dokumente der Vereine (ADR-029, v1.78): Protokolle, Satzung, Sonstiges – in `vk_accounts.db`.

- **Nur für den eigenen Verein:** Jede Abfrage läuft über `verein_key`. Fremde IDs gibt es nicht (→ 404).
  Nichts davon ist öffentlich, auch Josefs Admin-Oberfläche sieht nur Zahlen (`statistik()`).
- **Zwei Arten:** `datei` (hochgeladen, liegt unter `shared/dokumente_store.ORDNER`) oder `formular`
  (direkt geschrieben, Inhalt als JSON in `inhalt`; PDF/Word entstehen bei jedem Abruf neu).
- Ersteller/Bearbeiter als User-ID, keine E-Mail-Adressen. Löschen ist endgültig (Zeile + Datei).
"""
from __future__ import annotations

import json
from datetime import datetime

from shared.vk_db import db_conn

KATEGORIEN = {"protokoll": "Protokolle", "satzung": "Satzung", "sonstiges": "Sonstiges"}
KATEGORIE_EINZAHL = {"protokoll": "Protokoll", "satzung": "Satzung", "sonstiges": "Dokument"}
SITZUNGSARTEN = ("Mitgliederversammlung", "Vorstandssitzung", "Ausschusssitzung", "Sonstige Sitzung")

SCHEMA = """
CREATE TABLE IF NOT EXISTS dokument (
    id           INTEGER PRIMARY KEY,
    verein_key   TEXT NOT NULL,
    kategorie    TEXT NOT NULL,                     -- protokoll | satzung | sonstiges
    titel        TEXT NOT NULL,
    datum        TEXT NOT NULL DEFAULT '',          -- Sitzungs- bzw. Dokumentdatum (YYYY-MM-DD), optional
    art          TEXT NOT NULL,                     -- datei | formular
    datei_name   TEXT NOT NULL DEFAULT '',          -- Originalname (nur Anzeige/Download)
    datei_pfad   TEXT NOT NULL DEFAULT '',          -- Name unter ORDNER (UUID + Endung)
    datei_typ    TEXT NOT NULL DEFAULT '',          -- Endung aus der Inhaltsprüfung
    groesse      INTEGER NOT NULL DEFAULT 0,
    inhalt       TEXT NOT NULL DEFAULT '',          -- JSON, nur art=formular
    erstellt_von INTEGER,
    erstellt_am  TEXT NOT NULL,
    geaendert_von INTEGER,
    geaendert_am TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_dokument_verein ON dokument(verein_key, kategorie, datum);
"""


def jetzt() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _aus(r) -> dict:
    d = dict(r)
    try:
        d["inhalt"] = json.loads(d["inhalt"]) if d["inhalt"] else {}
    except ValueError:
        d["inhalt"] = {}
    return d


def liste(verein_key: str, suche: str = "") -> list[dict]:
    """Alle Dokumente eines Vereins, neueste zuerst (ohne Datum nach dem Anlegen)."""
    sql, args = "SELECT * FROM dokument WHERE verein_key = ?", [verein_key]
    if suche:
        sql += " AND (titel LIKE ? ESCAPE '\\' OR datei_name LIKE ? ESCAPE '\\')"
        muster = "%" + suche.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        args += [muster, muster]
    with db_conn() as c:
        return [_aus(r) for r in c.execute(sql + " ORDER BY COALESCE(NULLIF(datum, ''), substr(erstellt_am, 1, 10)) DESC, id DESC", args)]


def hole(did: int, verein_key: str) -> dict | None:
    with db_conn() as c:
        r = c.execute("SELECT * FROM dokument WHERE id = ? AND verein_key = ?", (did, verein_key)).fetchone()
    return _aus(r) if r else None


def neu(verein_key: str, user_id: int, kategorie: str, titel: str, datum: str, art: str,
        inhalt: dict | None = None, datei: dict | None = None) -> int:
    """datei = {name, pfad, typ, groesse} bei art=datei."""
    datei = datei or {}
    z = jetzt()
    with db_conn() as c:
        return c.execute(
            "INSERT INTO dokument (verein_key, kategorie, titel, datum, art, datei_name, datei_pfad, datei_typ, groesse,"
            " inhalt, erstellt_von, erstellt_am, geaendert_von, geaendert_am) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " RETURNING id",
            (verein_key, kategorie, titel, datum, art, datei.get("name", ""), datei.get("pfad", ""),
             datei.get("typ", ""), datei.get("groesse", 0),
             json.dumps(inhalt, ensure_ascii=False) if inhalt else "", user_id, z, user_id, z)).fetchone()["id"]


def aendern(did: int, verein_key: str, user_id: int, kategorie: str, titel: str, datum: str,
            inhalt: dict | None = None, datei: dict | None = None) -> bool:
    """Ändert Kopfdaten und (formular) den Inhalt bzw. (datei) ersetzt die Datei, wenn `datei` gesetzt ist."""
    sql = "UPDATE dokument SET kategorie = ?, titel = ?, datum = ?, geaendert_von = ?, geaendert_am = ?"
    args: list = [kategorie, titel, datum, user_id, jetzt()]
    if inhalt is not None:
        sql += ", inhalt = ?"
        args.append(json.dumps(inhalt, ensure_ascii=False))
    if datei:
        sql += ", datei_name = ?, datei_pfad = ?, datei_typ = ?, groesse = ?"
        args += [datei["name"], datei["pfad"], datei["typ"], datei["groesse"]]
    with db_conn() as c:
        return c.execute(sql + " WHERE id = ? AND verein_key = ?", args + [did, verein_key]).rowcount == 1


def loeschen(did: int, verein_key: str) -> dict | None:
    """Löscht die Zeile und gibt sie zurück (die Datei entfernt der Aufrufer)."""
    with db_conn() as c:
        r = c.execute("DELETE FROM dokument WHERE id = ? AND verein_key = ? RETURNING *", (did, verein_key)).fetchone()
    return _aus(r) if r else None


def belegung(verein_key: str) -> int:
    with db_conn() as c:
        return c.execute("SELECT COALESCE(SUM(groesse), 0) AS n FROM dokument WHERE verein_key = ?",
                         (verein_key,)).fetchone()["n"]


def verein_loeschen(verein_key: str) -> tuple[int, list[str]]:
    """Alle Dokumente eines Vereins löschen (Konto gelöscht). Gibt (Anzahl, Dateinamen zum Entfernen) zurück."""
    with db_conn() as c:
        rows = c.execute("DELETE FROM dokument WHERE verein_key = ? RETURNING datei_pfad", (verein_key,)).fetchall()
    return len(rows), [r["datei_pfad"] for r in rows if r["datei_pfad"]]


def key_umziehen(quelle: str, ziel: str, c=None) -> int:
    """Key-Übertragung (`uebertrage_key`): Dokumente wandern mit. Dateien liegen flach, nur die Zeile ändert sich."""
    if c is not None:
        return c.execute("UPDATE dokument SET verein_key = ? WHERE verein_key = ?", (ziel, quelle)).rowcount
    with db_conn() as c2:
        return c2.execute("UPDATE dokument SET verein_key = ? WHERE verein_key = ?", (ziel, quelle)).rowcount


def statistik() -> dict:
    """Für Josefs Admin-Statistik – nur Zahlen, keine Inhalte."""
    with db_conn() as c:
        r = c.execute("SELECT COUNT(*) AS n, COUNT(DISTINCT verein_key) AS v, COALESCE(SUM(groesse), 0) AS b"
                      " FROM dokument").fetchone()
    return {"anzahl": r["n"], "vereine": r["v"], "mb": round(r["b"] / 1024 / 1024, 1)}
