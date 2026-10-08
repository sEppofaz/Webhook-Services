"""SQLite des Prototyps (`daten/planung.sqlite`, nicht im Git).

Modell (Josef 2026-10-05, ADR-026):
- **Jeder Verein plant in seinem eigenen Bereich:** Entwürfe gehören dem Verein. Der Vereinsadmin legt sie an
  (auch aus der Vorjahres-Vorlage), ändert, **bestätigt** (= „steht so“, für die anderen sichtbar) und
  **veröffentlicht selbst** – einzeln oder alle. Niemand sonst veröffentlicht.
- **Planungsrunde:** Jeder freigegebene Vereinsadmin kann eine starten (**Organisator**, dokumentiert) und andere per
  **Link oder Code** einladen – ohne Josef. Beitreten nur eingeloggt, mit freigegebenem Konto, durch aktiven Klick. In der Runde sieht jeder die
  Entwürfe aller Teilnehmer und ändert/bestätigt seine eigenen – am Mac oder Handy.
- **Dokumentation:** Jede Runde führt einen Verlauf (`runde_protokoll`). Beim Abschließen wird das Ergebnis
  eingefroren (`runde_ergebnis`, versioniert) – daraus entsteht für jeden beteiligten Verein das Ergebnis-PDF im
  Vereins-Archiv. Gespeichert wird der Stand, nicht die Datei: das PDF ist bei jedem Abruf identisch.
- **Jedes neue Konto gibt Josef persönlich frei** (App/Telegram). Vorher: eigene Entwürfe ja, Runden nein,
  Veröffentlichen nein.

Entwürfe liegen bewusst **nicht** in `vereinstermine.json`: `/api/termine` gibt jedes Feld nach außen, das nicht
auf der Sperrliste steht. Schema so angelegt, dass es in `shared/vk_db.py` übernommen werden kann.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from daten import DATEN_DIR

DB_FILE = DATEN_DIR / "planung.sqlite"

STATUS = {"entwurf": "Entwurf", "bestaetigt": "Bestätigt", "veroeffentlicht": "Im Kalender",
          "kalender": "Im Kalender"}   # kalender = steht schon live im Kalender (Momentaufnahme, nur lesend)
FREIGABE = {"vko": "freigegeben", "ausstehend": "wartet auf Kontofreigabe"}
# Code zum Beitreten: ohne leicht verwechselbare Zeichen (0/O, 1/I/L), angezeigt als „K7M-4QX“
CODE_ZEICHEN = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LAENGE = 6

_SCHEMA = """
CREATE TABLE IF NOT EXISTS konto (            -- im Prototyp: simulierte Vereinskonten (live: vereine_accounts)
    verein_key   TEXT PRIMARY KEY,
    verein_name  TEXT NOT NULL,
    freigabe     TEXT NOT NULL,                -- ausstehend | vko (Freigabe nur durch Josef)
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
    regel              TEXT NOT NULL DEFAULT '',          -- „wie 2026: 2. Samstag im Juli“
    datum_vorjahr      TEXT NOT NULL DEFAULT '',
    geo_json           TEXT,                              -- _geo aus dem Vorjahr (Ort unverändert)
    bestaetigt_am      TEXT,                              -- Verein: „steht so“ (Änderung setzt zurück)
    veroeffentlicht_am TEXT,                              -- gesetzt = veröffentlicht
    geaendert_am       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_entwurf_verein ON entwurf(verein_key, datum);
CREATE TABLE IF NOT EXISTS runde (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    jahr        INTEGER NOT NULL,
    organisator TEXT NOT NULL,              -- verein_key des Vereins, der die Runde gestartet hat
    link        TEXT NOT NULL UNIQUE,       -- Einladungslink-Token
    code        TEXT NOT NULL UNIQUE,       -- Beitrittscode (ohne Bindestrich, Großbuchstaben)
    status      TEXT NOT NULL DEFAULT 'offen',   -- offen | abgeschlossen
    erstellt_am TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runde_teilnehmer (
    runde_id       INTEGER NOT NULL REFERENCES runde(id) ON DELETE CASCADE,
    verein_key     TEXT NOT NULL,
    beigetreten_am TEXT NOT NULL,
    aktiv          INTEGER NOT NULL DEFAULT 1,      -- 1 dabei · 0 vom Organisator entfernt · 2 selbst verlassen
    PRIMARY KEY (runde_id, verein_key)
);
CREATE TABLE IF NOT EXISTS einladung (         -- Link/Code geöffnet, bevor das Konto freigegeben war
    verein_key TEXT NOT NULL,
    runde_id   INTEGER NOT NULL REFERENCES runde(id) ON DELETE CASCADE,
    nachweis   TEXT NOT NULL,                   -- genutzter Link oder Code (nach „Erneuern“ ungültig)
    angelegt_am TEXT NOT NULL,
    PRIMARY KEY (verein_key, runde_id)
);
CREATE TABLE IF NOT EXISTS verein_einstellung ( -- live: _meta[key].crawler_aus in vereinstermine.json (v1.66, ADR-027)
    verein_key  TEXT PRIMARY KEY,
    crawler_aus INTEGER NOT NULL DEFAULT 0      -- 1 = nur der Verein trägt ein, keine Termine von Gemeinde-Webseiten
);
CREATE TABLE IF NOT EXISTS pruefkreis (         -- „Überschneidungen prüfen mit“: Gemeinde automatisch + Anpassungen
    verein_key TEXT NOT NULL,
    ziel_key   TEXT NOT NULL,
    art        TEXT NOT NULL,                   -- dazu (immer prüfen) | ohne (nie prüfen)
    PRIMARY KEY (verein_key, ziel_key)
);
CREATE TABLE IF NOT EXISTS runde_protokoll (    -- Verlauf: nur Vereinsnamen und Termine, keine Personendaten
    id         INTEGER PRIMARY KEY,
    runde_id   INTEGER NOT NULL REFERENCES runde(id) ON DELETE CASCADE,
    zeit       TEXT NOT NULL,
    verein_key TEXT NOT NULL,
    aktion     TEXT NOT NULL,
    details    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS runde_ergebnis (     -- eingefrorener Stand beim Abschließen, je Abschluss eine Version
    id               INTEGER PRIMARY KEY,
    runde_id         INTEGER NOT NULL REFERENCES runde(id) ON DELETE CASCADE,
    version          INTEGER NOT NULL,
    abgeschlossen_am TEXT NOT NULL,
    abgeschlossen_von TEXT NOT NULL,
    daten_json       TEXT NOT NULL,              -- Teilnehmer, Termine, Konflikte, Verlauf
    UNIQUE (runde_id, version)
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
        # Ältere Prototyp-Stände (Organisator-Raum, Verschiebe-Vorschläge): Räume verwerfen,
        # Konten und Entwürfe ins neue Schema übernehmen.
        for alt in ("planung_termin", "planung_verein", "planung_raum"):
            c.execute(f"DROP TABLE IF EXISTS {alt}")
        if "vorschlag_datum" in {r["name"] for r in c.execute("PRAGMA table_info(entwurf)")}:
            c.execute("ALTER TABLE entwurf RENAME TO entwurf_alt")
        if "eingeladen_raum_id" in {r["name"] for r in c.execute("PRAGMA table_info(konto)")}:
            c.execute("ALTER TABLE konto RENAME TO konto_alt")
        if "gastgeber" in {r["name"] for r in c.execute("PRAGMA table_info(runde)")}:
            c.execute("ALTER TABLE runde RENAME COLUMN gastgeber TO organisator")
        c.executescript(_SCHEMA)
        if c.execute("SELECT 1 FROM sqlite_master WHERE name = 'entwurf_alt'").fetchone():
            c.execute("INSERT INTO entwurf (id, verein_key, datum, uhrzeit, uhrzeit_bis, bezeichnung, ort, regel, "
                      "datum_vorjahr, geo_json, veroeffentlicht_am, geaendert_am) SELECT id, verein_key, datum, uhrzeit, "
                      "uhrzeit_bis, bezeichnung, ort, regel, datum_vorjahr, geo_json, veroeffentlicht_am, geaendert_am "
                      "FROM entwurf_alt")
            c.execute("DROP TABLE entwurf_alt")
        if c.execute("SELECT 1 FROM sqlite_master WHERE name = 'konto_alt'").fetchone():
            c.execute("INSERT INTO konto SELECT verein_key, verein_name, "
                      "CASE freigabe WHEN 'einladung' THEN 'vko' ELSE freigabe END, angelegt_am FROM konto_alt")
            c.execute("DROP TABLE konto_alt")


def jetzt() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ── Konten ───────────────────────────────────────────────────────────────────

def konto(verein_key: str):
    with conn() as c:
        return c.execute("SELECT * FROM konto WHERE verein_key = ?", (verein_key,)).fetchone()


def konten() -> dict:
    with conn() as c:
        return {r["verein_key"]: dict(r) for r in c.execute("SELECT * FROM konto")}


def konto_anlegen(verein_key: str, verein_name: str) -> None:
    """Neues Konto wartet immer auf Josefs Freigabe."""
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO konto (verein_key, verein_name, freigabe, angelegt_am) VALUES (?,?,'ausstehend',?)",
                  (verein_key, verein_name, jetzt()))


def konto_freigeben(verein_key: str) -> None:
    with conn() as c:
        c.execute("UPDATE konto SET freigabe = 'vko' WHERE verein_key = ? AND freigabe = 'ausstehend'", (verein_key,))


def freigegeben(verein_key: str) -> bool:
    k = konto(verein_key)
    return bool(k) and k["freigabe"] == "vko"


# ── Entwürfe ─────────────────────────────────────────────────────────────────

_FELDER = ("datum", "uhrzeit", "uhrzeit_bis", "bezeichnung", "ort")


def _als_termin(r) -> dict:
    t = dict(r)
    t["verein"] = t["verein_key"]
    t["_eid"] = t["id"]
    t["id"] = f"e{t['id']}"   # nicht mit echten Termin-IDs verwechseln
    t["status"] = "veroeffentlicht" if t["veroeffentlicht_am"] else ("bestaetigt" if t["bestaetigt_am"] else "entwurf")
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
    with conn() as c:
        out = [_als_termin(r) for r in c.execute(sql + " ORDER BY datum, uhrzeit, id", args)]
    return [t for t in out if t["status"] == status] if status else out


def entwurf(eid: int):
    with conn() as c:
        r = c.execute("SELECT * FROM entwurf WHERE id = ?", (eid,)).fetchone()
    return _als_termin(r) if r else None


def entwurf_neu(verein_key: str, felder: dict, regel: str = "", datum_vorjahr: str = "", geo=None) -> int:
    with conn() as c:
        return c.execute("INSERT INTO entwurf (verein_key, datum, uhrzeit, uhrzeit_bis, bezeichnung, ort, regel, datum_vorjahr, "
                  "geo_json, geaendert_am) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (verein_key, felder["datum"], felder.get("uhrzeit", ""), felder.get("uhrzeit_bis", ""),
                   felder["bezeichnung"], felder.get("ort", ""), regel, datum_vorjahr,
                   json.dumps(geo, ensure_ascii=False) if geo else None, jetzt())).lastrowid


def _vorlage_schluessel(t: dict) -> tuple:
    # Mit Uhrzeit: zwei gleichnamige Termine am selben Vorjahrestag (z. B. 10:00 und 10:30) sind zwei
    return (t.get("datum_vorjahr", ""), t.get("uhrzeit", ""), t.get("bezeichnung", ""))


def offene_vorlage(verein_key: str, vorschlaege: list[dict]) -> list[dict]:
    """Vorjahres-Vorschläge des Vereins, die noch nicht als Entwurf übernommen sind (ohne ergänzte Serien-Monate)."""
    vorhanden = {_vorlage_schluessel(t) for t in entwuerfe(verein_key)}
    return [v for v in vorschlaege if v.get("verein") == verein_key and not v.get("ergaenzt")
            and _vorlage_schluessel(v) not in vorhanden]


def aus_vorlage(verein_key: str, vorschlaege: list[dict]) -> int:
    offen = offene_vorlage(verein_key, vorschlaege)
    for v in offen:
        entwurf_neu(verein_key, v, v.get("regel", ""), v.get("datum_vorjahr", ""), v.get("_geo"))
    return len(offen)


def entwurf_aendern(eid: int, verein_key: str, felder: dict) -> bool:
    """Nur der eigene Verein, nur unveröffentlichte. Eine Änderung hebt die Bestätigung auf."""
    werte = {k: felder[k] for k in _FELDER if k in felder}
    if "ort" in werte:
        werte["geo_json"] = None   # Ort geändert → Geo neu berechnen
    werte["bestaetigt_am"] = None
    sql = "UPDATE entwurf SET " + ", ".join(f"{k} = ?" for k in werte) + ", geaendert_am = ? " \
          "WHERE id = ? AND verein_key = ? AND veroeffentlicht_am IS NULL"
    with conn() as c:
        return c.execute(sql, list(werte.values()) + [jetzt(), eid, verein_key]).rowcount == 1


def entwurf_loeschen(eid: int, verein_key: str) -> bool:
    with conn() as c:
        return c.execute("DELETE FROM entwurf WHERE id = ? AND verein_key = ? AND veroeffentlicht_am IS NULL",
                         (eid, verein_key)).rowcount == 1


def _ids_sql(eids) -> str:
    return f" AND id IN ({','.join('?' * len(eids))})" if eids is not None else ""


def bestaetigen(verein_key: str, eids: list[int] | None = None, ja: bool = True) -> int:
    """Einzeln (eids) oder alle eigenen unveröffentlichten Entwürfe bestätigen bzw. zurücknehmen."""
    if eids is not None and not eids:
        return 0
    sql = "UPDATE entwurf SET bestaetigt_am = ?, geaendert_am = ? WHERE verein_key = ? AND veroeffentlicht_am IS NULL" \
          + _ids_sql(eids)
    with conn() as c:
        return c.execute(sql, [jetzt() if ja else None, jetzt(), verein_key] + list(eids or [])).rowcount


def veroeffentlichen(verein_key: str, eids: list[int] | None = None) -> int:
    """Einzeln (eids) oder alle. Prototyp: nur Zeitstempel – live: in den Kalender schreiben."""
    if eids is not None and not eids:
        return 0
    sql = "UPDATE entwurf SET veroeffentlicht_am = ?, geaendert_am = ? WHERE verein_key = ? AND veroeffentlicht_am IS NULL" \
          + _ids_sql(eids)
    with conn() as c:
        return c.execute(sql, [jetzt(), jetzt(), verein_key] + list(eids or [])).rowcount


def zurueckziehen(eid: int, verein_key: str) -> bool:
    """Prototyp-Hilfe: Veröffentlichung zurücknehmen (live: Termin löschen = Soft-Delete)."""
    with conn() as c:
        return c.execute("UPDATE entwurf SET veroeffentlicht_am = NULL, geaendert_am = ? WHERE id = ? AND verein_key = ?",
                         (jetzt(), eid, verein_key)).rowcount == 1


# ── Planungsrunden ───────────────────────────────────────────────────────────

def _neuer_code(c) -> str:
    while True:
        code = "".join(secrets.choice(CODE_ZEICHEN) for _ in range(CODE_LAENGE))
        if not c.execute("SELECT 1 FROM runde WHERE code = ?", (code,)).fetchone():
            return code


def code_normal(eingabe: str) -> str:
    """Eingabe → gespeicherte Form: Großbuchstaben, ohne Bindestrich/Leerzeichen."""
    return "".join(ch for ch in (eingabe or "").upper() if ch.isalnum())


def code_anzeige(code: str) -> str:
    return f"{code[:3]}-{code[3:]}" if len(code) == CODE_LAENGE else code


def runde_starten(name: str, jahr: int, organisator: str) -> int:
    with conn() as c:
        cur = c.execute("INSERT INTO runde (name, jahr, organisator, link, code, erstellt_am) VALUES (?,?,?,?,?,?)",
                        (name, jahr, organisator, secrets.token_urlsafe(16), _neuer_code(c), jetzt()))
        c.execute("INSERT INTO runde_teilnehmer (runde_id, verein_key, beigetreten_am) VALUES (?,?,?)",
                  (cur.lastrowid, organisator, jetzt()))
        _protokoll(c, cur.lastrowid, organisator, "Runde gestartet (Organisator)", f"{name} {jahr}")
        return cur.lastrowid


def runde(runde_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM runde WHERE id = ?", (runde_id,)).fetchone()


def runde_per_link(link: str):
    with conn() as c:
        return c.execute("SELECT * FROM runde WHERE link = ?", (link,)).fetchone()


def runde_per_code(code: str):
    code = code_normal(code)
    if len(code) != CODE_LAENGE:
        return None
    with conn() as c:
        return c.execute("SELECT * FROM runde WHERE code = ?", (code,)).fetchone()


def einladung_erneuern(runde_id: int) -> None:
    """Neuer Link + neuer Code – die alten gelten nicht mehr. Wer schon drin ist, bleibt drin."""
    with conn() as c:
        c.execute("UPDATE runde SET link = ?, code = ? WHERE id = ?", (secrets.token_urlsafe(16), _neuer_code(c), runde_id))


def beitreten(runde_id: int, verein_key: str) -> str:
    """'neu' | 'schon' | 'entfernt' (vom Organisator entfernt → nur der Organisator nimmt wieder auf)."""
    with conn() as c:
        r = c.execute("SELECT aktiv FROM runde_teilnehmer WHERE runde_id = ? AND verein_key = ?",
                      (runde_id, verein_key)).fetchone()
        if r and r["aktiv"] == 1:
            return "schon"
        if r and r["aktiv"] == 0:
            return "entfernt"
        if r:   # selbst verlassen → darf wieder beitreten
            c.execute("UPDATE runde_teilnehmer SET aktiv = 1 WHERE runde_id = ? AND verein_key = ?", (runde_id, verein_key))
        else:
            c.execute("INSERT INTO runde_teilnehmer (runde_id, verein_key, beigetreten_am) VALUES (?,?,?)",
                      (runde_id, verein_key, jetzt()))
        return "neu"


def einladung_merken(verein_key: str, runde_id: int, nachweis: str) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO einladung VALUES (?,?,?,?)", (verein_key, runde_id, nachweis, jetzt()))


def offene_einladungen(verein_key: str) -> list[dict]:
    """Gemerkte Einladungen zu offenen Runden, solange Link/Code noch gilt und der Verein nie beigetreten ist."""
    with conn() as c:
        rows = c.execute(
            "SELECT r.id, r.name, r.jahr, e.nachweis FROM einladung e JOIN runde r ON r.id = e.runde_id "
            "WHERE e.verein_key = ? AND r.status = 'offen' AND e.nachweis IN (r.link, r.code) "
            "AND NOT EXISTS (SELECT 1 FROM runde_teilnehmer t WHERE t.runde_id = r.id AND t.verein_key = e.verein_key) "
            "ORDER BY r.jahr, r.name", (verein_key,)).fetchall()
        return [dict(r) for r in rows]


def verlassen(runde_id: int, verein_key: str) -> None:
    with conn() as c:
        c.execute("UPDATE runde_teilnehmer SET aktiv = 2 WHERE runde_id = ? AND verein_key = ?", (runde_id, verein_key))


def teilnehmer_setzen(runde_id: int, verein_key: str, aktiv: bool) -> None:
    """Organisator: entfernen (aktiv = 0, Link/Code helfen dann nicht mehr) oder wieder aufnehmen."""
    with conn() as c:
        c.execute("UPDATE runde_teilnehmer SET aktiv = ? WHERE runde_id = ? AND verein_key = ?",
                  (1 if aktiv else 0, runde_id, verein_key))


def teilnehmer(runde_id: int) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT t.*, k.verein_name FROM runde_teilnehmer t LEFT JOIN konto k USING (verein_key) "
                         "WHERE t.runde_id = ? ORDER BY t.aktiv = 1 DESC, lower(k.verein_name)", (runde_id,)).fetchall()


def aktive_teilnehmer(runde_id: int) -> set:
    return {t["verein_key"] for t in teilnehmer(runde_id) if t["aktiv"] == 1}


def runden_des_vereins(verein_key: str) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT r.*, (SELECT COUNT(*) FROM runde_teilnehmer x WHERE x.runde_id = r.id AND x.aktiv = 1) "
                         "AS anzahl FROM runde r JOIN runde_teilnehmer t ON t.runde_id = r.id "
                         "WHERE t.verein_key = ? AND t.aktiv = 1 ORDER BY r.jahr DESC, r.id DESC", (verein_key,)).fetchall()


def runde_status(runde_id: int, status: str) -> None:
    with conn() as c:
        c.execute("UPDATE runde SET status = ? WHERE id = ?", (status, runde_id))


# ── Verlauf und Ergebnis ─────────────────────────────────────────────────────

def _protokoll(c, runde_id: int, verein_key: str, aktion: str, details: str = "") -> None:
    c.execute("INSERT INTO runde_protokoll (runde_id, zeit, verein_key, aktion, details) VALUES (?,?,?,?,?)",
              (runde_id, jetzt(), verein_key, aktion, details))


def protokoll(runde_id: int, verein_key: str, aktion: str, details: str = "") -> None:
    with conn() as c:
        _protokoll(c, runde_id, verein_key, aktion, details)


def protokoll_liste(runde_id: int) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runde_protokoll WHERE runde_id = ? ORDER BY id", (runde_id,))]


def offene_runden(verein_key: str, jahr: int) -> list[int]:
    """Offene Runden, in denen der Verein dabei ist und die dieses Jahr planen – dort wird eine Terminänderung protokolliert."""
    return [r["id"] for r in runden_des_vereins(verein_key) if r["status"] == "offen" and r["jahr"] == jahr]


def ergebnis_speichern(runde_id: int, verein_key: str, daten: dict) -> int:
    with conn() as c:
        version = (c.execute("SELECT MAX(version) FROM runde_ergebnis WHERE runde_id = ?", (runde_id,)).fetchone()[0] or 0) + 1
        c.execute("INSERT INTO runde_ergebnis (runde_id, version, abgeschlossen_am, abgeschlossen_von, daten_json) "
                  "VALUES (?,?,?,?,?)", (runde_id, version, daten["abgeschlossen_am"], verein_key,
                                         json.dumps(daten, ensure_ascii=False)))
        _protokoll(c, runde_id, verein_key, "Runde abgeschlossen", f"Ergebnis Version {version}")
        return version


def ergebnisse(runde_id: int | None = None) -> list[dict]:
    sql, args = "SELECT * FROM runde_ergebnis", []
    if runde_id:
        sql += " WHERE runde_id = ?"
        args.append(runde_id)
    with conn() as c:
        out = []
        for r in c.execute(sql + " ORDER BY abgeschlossen_am DESC, version DESC", args):
            e = dict(r)
            e["daten"] = json.loads(e.pop("daten_json"))
            out.append(e)
        return out


def ergebnisse_des_vereins(verein_key: str) -> list[dict]:
    """Ergebnisse aller Runden, an denen der Verein beim Abschluss beteiligt war – sein Archiv."""
    return [e for e in ergebnisse() if verein_key in {t["verein"] for t in e["daten"]["teilnehmer"]}]


def ergebnis(ergebnis_id: int) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM runde_ergebnis WHERE id = ?", (ergebnis_id,)).fetchone()
    if not r:
        return None
    e = dict(r)
    e["daten"] = json.loads(e.pop("daten_json"))
    return e


def stand_version(runde_id: int, jahr: int) -> str:
    """Kennung des aktuellen Stands einer Runde – Prüfsumme über alle Termine der Teilnehmer, die Teilnehmer und
    den Status. Nicht nur der letzte Änderungszeitpunkt: zwei Änderungen in derselben Sekunde (Treffen!) fielen
    sonst nicht auf."""
    import hashlib
    keys = aktive_teilnehmer(runde_id)
    with conn() as c:
        zeilen = c.execute("SELECT id, datum, uhrzeit, uhrzeit_bis, bezeichnung, ort, bestaetigt_am, veroeffentlicht_am "
                           "FROM entwurf WHERE substr(datum, 1, 4) = ? AND verein_key IN (SELECT verein_key FROM "
                           "runde_teilnehmer WHERE runde_id = ? AND aktiv = 1) ORDER BY id", (str(jahr), runde_id)).fetchall()
        st = c.execute("SELECT status FROM runde WHERE id = ?", (runde_id,)).fetchone()
    h = hashlib.sha1()
    for z in zeilen:
        h.update(repr(tuple(z)).encode())
    h.update(f"|{','.join(sorted(keys))}|{st['status'] if st else ''}".encode())
    return h.hexdigest()[:16]


# ── Prüfkreis für Überschneidungen ───────────────────────────────────────────

def pruefkreis(verein_key: str) -> tuple[set, set]:
    """(dazu, ohne) – zusätzlich geprüfte und ausgeschlossene Vereine. Die eigene Gemeinde gilt automatisch."""
    with conn() as c:
        rows = c.execute("SELECT ziel_key, art FROM pruefkreis WHERE verein_key = ?", (verein_key,)).fetchall()
    return {r["ziel_key"] for r in rows if r["art"] == "dazu"}, {r["ziel_key"] for r in rows if r["art"] == "ohne"}


def crawler_aus(verein_key: str) -> bool:
    with conn() as c:
        r = c.execute("SELECT crawler_aus FROM verein_einstellung WHERE verein_key = ?", (verein_key,)).fetchone()
    return bool(r and r["crawler_aus"])


def crawler_setzen(verein_key: str, aus: bool) -> None:
    with conn() as c:
        c.execute("INSERT INTO verein_einstellung (verein_key, crawler_aus) VALUES (?,?) "
                  "ON CONFLICT(verein_key) DO UPDATE SET crawler_aus = excluded.crawler_aus", (verein_key, int(aus)))


def pruefkreis_setzen(verein_key: str, dazu: set, ohne: set) -> None:
    """Ersetzt die Einstellungen des Vereins komplett. Ein Verein kann nicht zugleich dazu und ohne sein (ohne gewinnt)."""
    dazu = set(dazu) - set(ohne) - {verein_key}
    ohne = set(ohne) - {verein_key}
    with conn() as c:
        c.execute("DELETE FROM pruefkreis WHERE verein_key = ?", (verein_key,))
        c.executemany("INSERT INTO pruefkreis (verein_key, ziel_key, art) VALUES (?,?,?)",
                      [(verein_key, k, "dazu") for k in sorted(dazu)] + [(verein_key, k, "ohne") for k in sorted(ohne)])
