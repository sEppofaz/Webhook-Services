"""SQLite des Prototyps (`daten/planung.sqlite`, nicht im Git).

Modell (Josef 2026-10-05):
- **Entwürfe gehören dem Verein.** Der Vereinsadmin legt sie an (auch aus der Vorjahres-Vorlage),
  ändert sie und veröffentlicht einzeln oder alle auf einmal. Niemand sonst veröffentlicht.
- **Ein Planungsraum ist nur eine Sicht** auf die Entwürfe seiner aktiven Vereine im Planungsjahr.
  Die Organisatorin verschiebt nicht selbst, sie macht Verschiebe-Vorschläge; der Verein übernimmt sie.
- **Jeder Verein braucht ein Konto, und Josef gibt jedes neue Konto persönlich frei** (App oder Telegram,
  wie heute). Die Einladung aus einem Planungsraum führt nur zur Registrierung und wird als Hinweis
  mitgegeben – sie ersetzt die Prüfung nicht (Josef 2026-10-05). Bis zur Freigabe: eigene Entwürfe ja,
  Entwürfe anderer Vereine sehen nein, Veröffentlichen nein.

Entwürfe liegen bewusst **nicht** in `vereinstermine.json`: `/api/termine` gibt jedes Feld nach außen,
das nicht auf der Sperrliste steht. Schema so angelegt, dass es in `shared/vk_db.py` übernommen werden kann.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

from daten import DATEN_DIR

DB_FILE = DATEN_DIR / "planung.sqlite"
EINLADUNG_TAGE = 60

STATUS = {"entwurf": "Entwurf", "veroeffentlicht": "Veröffentlicht"}
FREIGABE = {"vko": "freigegeben", "ausstehend": "wartet auf Freigabe durch VKO"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS konto (            -- im Prototyp: simulierte Vereinskonten (live: vereine_accounts)
    verein_key   TEXT PRIMARY KEY,
    verein_name  TEXT NOT NULL,
    freigabe     TEXT NOT NULL,                -- ausstehend | vko (Freigabe nur durch Josef)
    eingeladen_raum_id INTEGER,                -- Hinweis für die Prüfung: kam über diese Einladung
    angelegt_am  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entwurf (
    id                 INTEGER PRIMARY KEY,
    verein_key         TEXT NOT NULL,
    datum              TEXT NOT NULL,
    uhrzeit            TEXT NOT NULL DEFAULT '',
    uhrzeit_bis        TEXT NOT NULL DEFAULT '',
    bezeichnung        TEXT NOT NULL,
    ort                TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'entwurf',   -- entwurf | veroeffentlicht
    regel              TEXT NOT NULL DEFAULT '',          -- „wie 2026: 2. Samstag im Juli“
    datum_vorjahr      TEXT NOT NULL DEFAULT '',
    geo_json           TEXT,                              -- _geo aus dem Vorjahr (Ort unverändert)
    vorschlag_datum    TEXT,                              -- Verschiebe-Vorschlag der Organisatorin
    vorschlag_raum_id  INTEGER,
    veroeffentlicht_am TEXT,
    geaendert_am       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_entwurf_verein ON entwurf(verein_key, datum);
CREATE TABLE IF NOT EXISTS planung_raum (
    id          INTEGER PRIMARY KEY,
    token       TEXT NOT NULL UNIQUE,       -- Link der Organisatorin
    gemeinde    TEXT NOT NULL,
    landkreis   TEXT NOT NULL,
    jahr        INTEGER NOT NULL,
    titel       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'offen',   -- offen | abgeschlossen
    erstellt_am TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS planung_verein (
    id             INTEGER PRIMARY KEY,
    raum_id        INTEGER NOT NULL REFERENCES planung_raum(id) ON DELETE CASCADE,
    verein_key     TEXT NOT NULL,
    verein_name    TEXT NOT NULL,
    einladung      TEXT NOT NULL UNIQUE,    -- Einladungs-Token (führt zu Login/Registrierung)
    einladung_bis  TEXT NOT NULL,
    angenommen_am  TEXT,                    -- Konto über diese Einladung angelegt
    aktiv          INTEGER NOT NULL DEFAULT 1,
    fertig_am      TEXT,
    UNIQUE (raum_id, verein_key)
);
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
        if c.execute("SELECT 1 FROM sqlite_master WHERE name = 'planung_termin'").fetchone():
            # Altes Modell (Termine im Raum) – Prototyp, keine Migration: neu anlegen
            c.executescript("DROP TABLE planung_termin; DROP TABLE IF EXISTS planung_verein; DROP TABLE IF EXISTS planung_raum;")
        c.executescript(_SCHEMA)
        # Prototyp-Stand mit automatischer Freigabe (bis 2026-10-05 abends) nachziehen
        if "eingeladen_raum_id" not in {r["name"] for r in c.execute("PRAGMA table_info(konto)")}:
            c.execute("ALTER TABLE konto ADD COLUMN eingeladen_raum_id INTEGER")
        c.execute("UPDATE konto SET freigabe = 'vko' WHERE freigabe = 'einladung'")


def jetzt() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ── Konten ───────────────────────────────────────────────────────────────────

def konto(verein_key: str):
    with conn() as c:
        return c.execute("SELECT * FROM konto WHERE verein_key = ?", (verein_key,)).fetchone()


def konten() -> dict:
    with conn() as c:
        return {r["verein_key"]: dict(r) for r in c.execute("SELECT * FROM konto")}


def konto_anlegen(verein_key: str, verein_name: str, raum_id: int | None = None) -> None:
    """Neues Konto wartet immer auf Josefs Freigabe – auch über eine Einladung (dann mit Hinweis auf den Raum)."""
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO konto (verein_key, verein_name, freigabe, eingeladen_raum_id, angelegt_am) "
                  "VALUES (?,?,'ausstehend',?,?)", (verein_key, verein_name, raum_id, jetzt()))


def konto_freigeben(verein_key: str) -> None:
    with conn() as c:
        c.execute("UPDATE konto SET freigabe = 'vko' WHERE verein_key = ? AND freigabe = 'ausstehend'", (verein_key,))


def freigegeben(verein_key: str) -> bool:
    k = konto(verein_key)
    return bool(k) and k["freigabe"] == "vko"


darf_veroeffentlichen = freigegeben


# ── Entwürfe ─────────────────────────────────────────────────────────────────

_FELDER = ("datum", "uhrzeit", "uhrzeit_bis", "bezeichnung", "ort")


def _als_termin(r) -> dict:
    t = dict(r)
    t["verein"] = t["verein_key"]
    t["_eid"] = t["id"]
    t["id"] = f"e{t['id']}"   # nicht mit echten Termin-IDs verwechseln
    if t.get("geo_json"):
        t["_geo"] = json.loads(t["geo_json"])
    return t


def entwuerfe(verein_key: str | None = None, jahr: int | None = None, vereine: set | None = None,
              status: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM entwurf WHERE 1=1", []
    if verein_key:
        sql += " AND verein_key = ?"
        args.append(verein_key)
    if vereine is not None:
        if not vereine:
            return []
        sql += f" AND verein_key IN ({','.join('?' * len(vereine))})"
        args += sorted(vereine)
    if jahr:
        sql += " AND substr(datum, 1, 4) = ?"
        args.append(str(jahr))
    if status:
        sql += " AND status = ?"
        args.append(status)
    with conn() as c:
        return [_als_termin(r) for r in c.execute(sql + " ORDER BY datum, uhrzeit, id", args)]


def entwurf(eid: int):
    with conn() as c:
        r = c.execute("SELECT * FROM entwurf WHERE id = ?", (eid,)).fetchone()
    return _als_termin(r) if r else None


def entwurf_neu(verein_key: str, felder: dict, regel: str = "", datum_vorjahr: str = "", geo=None) -> None:
    with conn() as c:
        c.execute("INSERT INTO entwurf (verein_key, datum, uhrzeit, uhrzeit_bis, bezeichnung, ort, regel, datum_vorjahr, "
                  "geo_json, geaendert_am) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (verein_key, felder["datum"], felder.get("uhrzeit", ""), felder.get("uhrzeit_bis", ""),
                   felder["bezeichnung"], felder.get("ort", ""), regel, datum_vorjahr,
                   json.dumps(geo, ensure_ascii=False) if geo else None, jetzt()))


def _vorlage_schluessel(t: dict) -> tuple:
    # Mit Uhrzeit: zwei gleichnamige Termine am selben Vorjahrestag (z. B. 9:00 und 14:00) sind zwei
    return (t.get("datum_vorjahr", ""), t.get("uhrzeit", ""), t.get("bezeichnung", ""))


def offene_vorlage(verein_key: str, vorschlaege: list[dict]) -> list[dict]:
    """Vorjahres-Vorschläge des Vereins, die noch nicht als Entwurf übernommen sind (ohne ergänzte Serien-Monate)."""
    vorhanden = {_vorlage_schluessel(t) for t in entwuerfe(verein_key)}
    return [v for v in vorschlaege if v.get("verein") == verein_key and not v.get("ergaenzt")
            and _vorlage_schluessel(v) not in vorhanden]


def aus_vorlage(verein_key: str, vorschlaege: list[dict]) -> int:
    """Vorjahres-Vorschläge als Entwürfe übernehmen – ohne Doppel. Ergänzte Serien-Monate nur anzeigen."""
    offen = offene_vorlage(verein_key, vorschlaege)
    for v in offen:
        entwurf_neu(verein_key, v, v.get("regel", ""), v.get("datum_vorjahr", ""), v.get("_geo"))
    return len(offen)


def entwurf_aendern(eid: int, verein_key: str, felder: dict) -> bool:
    """Nur der eigene Verein, nur unveröffentlichte Entwürfe. Ein offener Verschiebe-Vorschlag erledigt sich."""
    werte = {k: felder[k] for k in _FELDER if k in felder}
    if "ort" in werte:
        werte["geo_json"] = None   # Ort geändert → Geo neu berechnen
    werte.update(vorschlag_datum=None, vorschlag_raum_id=None)
    sql = "UPDATE entwurf SET " + ", ".join(f"{k} = ?" for k in werte) + ", geaendert_am = ? " \
          "WHERE id = ? AND verein_key = ? AND status = 'entwurf'"
    with conn() as c:
        return c.execute(sql, list(werte.values()) + [jetzt(), eid, verein_key]).rowcount == 1


def entwurf_loeschen(eid: int, verein_key: str) -> bool:
    with conn() as c:
        return c.execute("DELETE FROM entwurf WHERE id = ? AND verein_key = ? AND status = 'entwurf'",
                         (eid, verein_key)).rowcount == 1


def vorschlag_uebernehmen(eid: int, verein_key: str, annehmen: bool) -> bool:
    with conn() as c:
        if annehmen:
            n = c.execute("UPDATE entwurf SET datum = vorschlag_datum, vorschlag_datum = NULL, vorschlag_raum_id = NULL, "
                          "geaendert_am = ? WHERE id = ? AND verein_key = ? AND status = 'entwurf' "
                          "AND vorschlag_datum IS NOT NULL", (jetzt(), eid, verein_key)).rowcount
        else:
            n = c.execute("UPDATE entwurf SET vorschlag_datum = NULL, vorschlag_raum_id = NULL WHERE id = ? "
                          "AND verein_key = ?", (eid, verein_key)).rowcount
        return n == 1


def veroeffentlichen(verein_key: str, eids: list[int] | None = None) -> int:
    """Einzeln (eids) oder alle Entwürfe des Vereins. Prototyp: nur Status – live: in den Kalender schreiben."""
    sql = "UPDATE entwurf SET status = 'veroeffentlicht', veroeffentlicht_am = ?, vorschlag_datum = NULL, " \
          "vorschlag_raum_id = NULL WHERE verein_key = ? AND status = 'entwurf'"
    args: list = [jetzt(), verein_key]
    if eids is not None:
        if not eids:
            return 0
        sql += f" AND id IN ({','.join('?' * len(eids))})"
        args += eids
    with conn() as c:
        return c.execute(sql, args).rowcount


def zurueckziehen(eid: int, verein_key: str) -> bool:
    """Prototyp-Hilfe: Veröffentlichung zurücknehmen (live: Termin löschen = Soft-Delete)."""
    with conn() as c:
        return c.execute("UPDATE entwurf SET status = 'entwurf', veroeffentlicht_am = NULL WHERE id = ? "
                         "AND verein_key = ? AND status = 'veroeffentlicht'", (eid, verein_key)).rowcount == 1


# ── Planungsräume ────────────────────────────────────────────────────────────

def neuer_raum(gemeinde: str, landkreis: str, jahr: int, vereine: list[tuple[str, str]]) -> str:
    token = secrets.token_urlsafe(16)
    with conn() as c:
        cur = c.execute("INSERT INTO planung_raum (token, gemeinde, landkreis, jahr, titel, erstellt_am) "
                        "VALUES (?,?,?,?,?,?)",
                        (token, gemeinde, landkreis, jahr, f"Jahresplanung {jahr} – {gemeinde}", jetzt()))
        for k, name in vereine:
            _verein_einfuegen(c, cur.lastrowid, k, name)
    return token


def _verein_einfuegen(c, raum_id: int, key: str, name: str) -> None:
    bis = (datetime.now() + timedelta(days=EINLADUNG_TAGE)).isoformat(timespec="seconds")
    c.execute("INSERT INTO planung_verein (raum_id, verein_key, verein_name, einladung, einladung_bis) VALUES (?,?,?,?,?)",
              (raum_id, key, name, secrets.token_urlsafe(16), bis))


def verein_dazuholen(raum_id: int, key: str, name: str) -> None:
    """Abgewählt → wieder aktiv (gleiche Einladung). Neu → mit eigener Einladung."""
    with conn() as c:
        if c.execute("SELECT 1 FROM planung_verein WHERE raum_id = ? AND verein_key = ?", (raum_id, key)).fetchone():
            c.execute("UPDATE planung_verein SET aktiv = 1 WHERE raum_id = ? AND verein_key = ?", (raum_id, key))
        else:
            _verein_einfuegen(c, raum_id, key, name)


def verein_aktiv(raum_id: int, verein_id: int, aktiv: bool) -> None:
    with conn() as c:
        c.execute("UPDATE planung_verein SET aktiv = ? WHERE id = ? AND raum_id = ?", (1 if aktiv else 0, verein_id, raum_id))


def raeume() -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT r.*, (SELECT COUNT(*) FROM planung_verein v WHERE v.raum_id = r.id AND v.aktiv = 1) "
                         "AS vereine FROM planung_raum r ORDER BY r.id DESC").fetchall()


def raum_per_token(token: str):
    with conn() as c:
        return c.execute("SELECT * FROM planung_raum WHERE token = ?", (token,)).fetchone()


def raum(raum_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM planung_raum WHERE id = ?", (raum_id,)).fetchone()


def einladung(token: str):
    with conn() as c:
        return c.execute("SELECT v.*, r.status AS raum_status, r.titel FROM planung_verein v "
                         "JOIN planung_raum r ON r.id = v.raum_id WHERE v.einladung = ?", (token,)).fetchone()


def einladung_angenommen(verein_id: int) -> None:
    with conn() as c:
        c.execute("UPDATE planung_verein SET angenommen_am = ? WHERE id = ? AND angenommen_am IS NULL", (jetzt(), verein_id))


def vereine_im_raum(raum_id: int) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT * FROM planung_verein WHERE raum_id = ? ORDER BY aktiv DESC, lower(verein_name)",
                         (raum_id,)).fetchall()


def aktive_keys(raum_id: int) -> set:
    return {v["verein_key"] for v in vereine_im_raum(raum_id) if v["aktiv"]}


def raeume_des_vereins(verein_key: str) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT r.*, v.fertig_am, v.id AS pv_id FROM planung_raum r JOIN planung_verein v "
                         "ON v.raum_id = r.id WHERE v.verein_key = ? AND v.aktiv = 1 ORDER BY r.jahr DESC",
                         (verein_key,)).fetchall()


def vorschlag_machen(raum_id: int, eid: int, datum: str | None) -> bool:
    """Organisatorin: Verschiebe-Vorschlag setzen (None = zurücknehmen) – nur für Entwürfe aktiver Vereine."""
    keys = aktive_keys(raum_id)
    with conn() as c:
        r = c.execute("SELECT verein_key FROM entwurf WHERE id = ? AND status = 'entwurf'", (eid,)).fetchone()
        if not r or r["verein_key"] not in keys:
            return False
        c.execute("UPDATE entwurf SET vorschlag_datum = ?, vorschlag_raum_id = ? WHERE id = ?",
                  (datum, raum_id if datum else None, eid))
        return True


def fertig_melden(raum_id: int, verein_key: str, fertig: bool) -> None:
    with conn() as c:
        c.execute("UPDATE planung_verein SET fertig_am = ? WHERE raum_id = ? AND verein_key = ?",
                  (jetzt() if fertig else None, raum_id, verein_key))


def raum_status(raum_id: int, status: str) -> None:
    with conn() as c:
        c.execute("UPDATE planung_raum SET status = ? WHERE id = ?", (status, raum_id))


def raum_loeschen(raum_id: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM planung_raum WHERE id = ?", (raum_id,))
