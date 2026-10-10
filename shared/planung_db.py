"""Entwürfe, Planungsrunden und Prüfkreis der Vereine (ADR-026) – in `vk_accounts.db`.

- **Jeder Verein plant in seinem eigenen Bereich:** Entwürfe gehören dem Verein. Der Vereinsadmin legt sie an
  (auch aus der Vorjahres-Vorlage), ändert, bestätigt (nur in einer Planungsrunde: „steht so“) und
  **veröffentlicht selbst** – einzeln oder alle. Niemand sonst veröffentlicht.
- **Planungsrunde:** Jeder Vereinsadmin kann eine starten (**Organisator**, dokumentiert) und andere per **Link oder
  Code** einladen. Beitreten nur eingeloggt und durch aktiven Klick. Live gibt es nur freigegebene Konten im
  Vereinsbereich – `require_verein_login` lässt wartende Konten gar nicht hinein (Josef gibt jedes selbst frei).
- **Dokumentation:** Jede Runde führt einen Verlauf (`runde_protokoll`, nur Vereinsnamen und Termine). Beim
  Abschließen wird das Ergebnis eingefroren (`runde_ergebnis`, versioniert); das PDF entsteht bei jedem Abruf neu.

Entwürfe liegen bewusst **nicht** in `vereinstermine.json`: `/api/termine` gibt jedes Feld nach außen, das nicht
auf der Sperrliste steht – ein vergessener Filter, und Unveröffentlichtes wäre öffentlich.
Veröffentlichen schreibt den Termin in den Kalender und merkt sich dessen ID (`termin_id`); der Entwurf bleibt als
„Im Kalender“ für die Runde stehen, die Werte kommen dann aus dem Kalender.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime

from shared.vk_db import db_conn

# Code zum Beitreten: ohne leicht verwechselbare Zeichen (0/O, 1/I/L), angezeigt als „K7M-4QX“
CODE_ZEICHEN = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LAENGE = 6

STATUS = {"entwurf": "Entwurf", "bestaetigt": "Bestätigt", "veroeffentlicht": "Im Kalender",
          "kalender": "Im Kalender"}   # kalender = Termin aus dem Kalender (nicht aus einem Entwurf)

SCHEMA = """
CREATE TABLE IF NOT EXISTS entwurf (
    id                 INTEGER PRIMARY KEY,
    verein_key         TEXT NOT NULL,
    datum              TEXT NOT NULL,
    uhrzeit            TEXT NOT NULL DEFAULT '',
    uhrzeit_bis        TEXT NOT NULL DEFAULT '',
    bezeichnung        TEXT NOT NULL,
    ort                TEXT NOT NULL DEFAULT '',
    beschreibung       TEXT NOT NULL DEFAULT '',
    regel              TEXT NOT NULL DEFAULT '',          -- „wie 2026: 2. Samstag im Juli“
    datum_vorjahr      TEXT NOT NULL DEFAULT '',
    bestaetigt_am      TEXT,                              -- in der Runde: „steht so“ (Änderung setzt zurück)
    veroeffentlicht_am TEXT,                              -- gesetzt = steht im Kalender
    termin_id          TEXT,                              -- ID des Kalender-Termins nach dem Veröffentlichen
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
CREATE TABLE IF NOT EXISTS pruefkreis (         -- „Überschneidungen prüfen mit“: Gemeinde automatisch + Abweichungen
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
    id                INTEGER PRIMARY KEY,
    runde_id          INTEGER NOT NULL REFERENCES runde(id) ON DELETE CASCADE,
    version           INTEGER NOT NULL,
    abgeschlossen_am  TEXT NOT NULL,
    abgeschlossen_von TEXT NOT NULL,
    daten_json        TEXT NOT NULL,             -- Teilnehmer, Termine, Konflikte, Verlauf
    UNIQUE (runde_id, version)
);
"""


def jetzt() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ── Entwürfe ─────────────────────────────────────────────────────────────────

_FELDER = ("datum", "uhrzeit", "uhrzeit_bis", "bezeichnung", "ort", "beschreibung")


def _als_termin(r) -> dict:
    t = dict(r)
    t["verein"] = t["verein_key"]
    t["_eid"] = t["id"]
    t["id"] = f"e{t['id']}"   # nicht mit echten Termin-IDs verwechseln
    t["status"] = "veroeffentlicht" if t["veroeffentlicht_am"] else ("bestaetigt" if t["bestaetigt_am"] else "entwurf")
    return t


def entwuerfe(verein_key: str | None = None, jahr: int | None = None, vereine: set | None = None,
              offen: bool = False) -> list[dict]:
    """Entwürfe eines Vereins oder mehrerer (vereine), optional eines Jahres. offen=True: nur unveröffentlichte."""
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
    if offen:
        sql += " AND veroeffentlicht_am IS NULL"
    with db_conn() as c:
        return [_als_termin(r) for r in c.execute(sql + " ORDER BY datum, uhrzeit, id", args)]


def entwurf(eid: int) -> dict | None:
    with db_conn() as c:
        r = c.execute("SELECT * FROM entwurf WHERE id = ?", (eid,)).fetchone()
    return _als_termin(r) if r else None


def entwurf_neu(verein_key: str, felder: dict, regel: str = "", datum_vorjahr: str = "") -> int:
    with db_conn() as c:
        return c.execute(
            "INSERT INTO entwurf (verein_key, datum, uhrzeit, uhrzeit_bis, bezeichnung, ort, beschreibung, regel, "
            "datum_vorjahr, geaendert_am) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (verein_key, felder["datum"], felder.get("uhrzeit", ""), felder.get("uhrzeit_bis", ""),
             felder["bezeichnung"], felder.get("ort", ""), felder.get("beschreibung", ""), regel, datum_vorjahr,
             jetzt())).lastrowid


def _vorlage_schluessel(t: dict) -> tuple:
    # Mit Uhrzeit: zwei gleichnamige Termine am selben Vorjahrestag (z. B. 10:00 und 10:30) sind zwei
    return (t.get("datum_vorjahr", ""), t.get("uhrzeit", ""), t.get("bezeichnung", ""))


def offene_vorlage(verein_key: str, vorschlaege: list[dict], kalender: list[dict] = ()) -> list[dict]:
    """Vorjahres-Vorschläge des Vereins, die weder als Entwurf übernommen sind noch schon im Kalender stehen
    (gleicher Tag + Titel), ohne ergänzte Serien-Monate."""
    vorhanden = {_vorlage_schluessel(t) for t in entwuerfe(verein_key)}
    im_kalender = {(t.get("datum"), t.get("bezeichnung")) for t in kalender if t.get("verein") == verein_key}
    return [v for v in vorschlaege if v.get("verein") == verein_key and not v.get("ergaenzt")
            and _vorlage_schluessel(v) not in vorhanden and (v.get("datum"), v.get("bezeichnung")) not in im_kalender]


def aus_vorlage(verein_key: str, vorschlaege: list[dict], kalender: list[dict] = ()) -> int:
    offen = offene_vorlage(verein_key, vorschlaege, kalender)
    for v in offen:
        entwurf_neu(verein_key, v, v.get("regel", ""), v.get("datum_vorjahr", ""))
    return len(offen)


def entwurf_aendern(eid: int, verein_key: str, felder: dict) -> bool:
    """Nur der eigene Verein, nur unveröffentlichte. Eine Änderung hebt die Bestätigung auf."""
    werte = {k: felder[k] for k in _FELDER if k in felder}
    werte["bestaetigt_am"] = None
    sql = "UPDATE entwurf SET " + ", ".join(f"{k} = ?" for k in werte) + ", geaendert_am = ? " \
          "WHERE id = ? AND verein_key = ? AND veroeffentlicht_am IS NULL"
    with db_conn() as c:
        return c.execute(sql, list(werte.values()) + [jetzt(), eid, verein_key]).rowcount == 1


def entwurf_loeschen(eid: int, verein_key: str) -> bool:
    with db_conn() as c:
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
    with db_conn() as c:
        return c.execute(sql, [jetzt() if ja else None, jetzt(), verein_key] + list(eids or [])).rowcount


def reservieren(verein_key: str, eids: list[int]) -> list[dict]:
    """Erster Schritt beim Veröffentlichen: Entwürfe als veröffentlicht markieren und zurückgeben – nur die, die
    noch offen waren. So kann ein Doppelklick oder ein zweites Gerät denselben Entwurf nicht zweimal eintragen."""
    if not eids:
        return []
    zeit = jetzt()
    with db_conn() as c:
        c.execute("UPDATE entwurf SET veroeffentlicht_am = ?, geaendert_am = ? WHERE verein_key = ? "
                  "AND veroeffentlicht_am IS NULL" + _ids_sql(eids), [zeit, zeit, verein_key] + list(eids))
        rows = c.execute("SELECT * FROM entwurf WHERE verein_key = ? AND veroeffentlicht_am = ? AND termin_id IS NULL"
                         + _ids_sql(eids) + " ORDER BY datum, uhrzeit, id", [verein_key, zeit] + list(eids)).fetchall()
    return [_als_termin(r) for r in rows]


def reservierung_aufheben(eids: list[int]) -> None:
    """Schreiben in den Kalender ist gescheitert → Entwürfe wieder offen."""
    if not eids:
        return
    with db_conn() as c:
        c.execute("UPDATE entwurf SET veroeffentlicht_am = NULL WHERE termin_id IS NULL" + _ids_sql(eids), list(eids))


def termin_id_setzen(eid: int, termin_id: str) -> None:
    with db_conn() as c:
        c.execute("UPDATE entwurf SET termin_id = ? WHERE id = ?", (termin_id, eid))


def veroeffentlichte_termin_ids(vereine: set) -> set:
    """Kalender-IDs der aus Entwürfen veröffentlichten Termine (in der Runde nicht doppelt zählen)."""
    if not vereine:
        return set()
    with db_conn() as c:
        return {r[0] for r in c.execute(
            f"SELECT termin_id FROM entwurf WHERE termin_id IS NOT NULL AND verein_key IN ({','.join('?' * len(vereine))})",
            sorted(vereine))}


def kalender_termin_geloescht(verein_key: str, termin_id: str) -> list[dict]:
    """Ein veröffentlichter Termin wurde im Kalender gelöscht → auch der Entwurf dazu verschwindet aus den Runden."""
    with db_conn() as c:
        rows = c.execute("SELECT * FROM entwurf WHERE verein_key = ? AND termin_id = ?", (verein_key, termin_id)).fetchall()
        c.execute("DELETE FROM entwurf WHERE verein_key = ? AND termin_id = ?", (verein_key, termin_id))
    return [_als_termin(r) for r in rows]


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
    with db_conn() as c:
        rid = c.execute("INSERT INTO runde (name, jahr, organisator, link, code, erstellt_am) VALUES (?,?,?,?,?,?)",
                        (name, jahr, organisator, secrets.token_urlsafe(16), _neuer_code(c), jetzt())).lastrowid
        c.execute("INSERT INTO runde_teilnehmer (runde_id, verein_key, beigetreten_am) VALUES (?,?,?)",
                  (rid, organisator, jetzt()))
        _protokoll(c, rid, organisator, "Runde gestartet (Organisator)", f"{name} {jahr}")
        return rid


def runde(runde_id: int):
    with db_conn() as c:
        return c.execute("SELECT * FROM runde WHERE id = ?", (runde_id,)).fetchone()


def runde_per_link(link: str):
    with db_conn() as c:
        return c.execute("SELECT * FROM runde WHERE link = ?", (link,)).fetchone()


def runde_per_code(code: str):
    code = code_normal(code)
    if len(code) != CODE_LAENGE:
        return None
    with db_conn() as c:
        return c.execute("SELECT * FROM runde WHERE code = ?", (code,)).fetchone()


def einladung_erneuern(runde_id: int) -> None:
    """Neuer Link + neuer Code – die alten gelten nicht mehr. Wer schon drin ist, bleibt drin."""
    with db_conn() as c:
        c.execute("UPDATE runde SET link = ?, code = ? WHERE id = ?", (secrets.token_urlsafe(16), _neuer_code(c), runde_id))


def beitreten(runde_id: int, verein_key: str) -> str:
    """'neu' | 'schon' | 'entfernt' (vom Organisator entfernt → nur der Organisator nimmt wieder auf)."""
    with db_conn() as c:
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


def verlassen(runde_id: int, verein_key: str) -> None:
    with db_conn() as c:
        c.execute("UPDATE runde_teilnehmer SET aktiv = 2 WHERE runde_id = ? AND verein_key = ?", (runde_id, verein_key))


def teilnehmer_setzen(runde_id: int, verein_key: str, aktiv: bool) -> None:
    """Organisator: entfernen (aktiv = 0, Link/Code helfen dann nicht mehr) oder wieder aufnehmen."""
    with db_conn() as c:
        c.execute("UPDATE runde_teilnehmer SET aktiv = ? WHERE runde_id = ? AND verein_key = ?",
                  (1 if aktiv else 0, runde_id, verein_key))


def teilnehmer(runde_id: int) -> list[dict]:
    """Alle Teilnehmer (auch entfernte/ausgetretene), aktive zuerst. Namen ergänzt die Route."""
    with db_conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runde_teilnehmer WHERE runde_id = ? "
                                           "ORDER BY aktiv = 1 DESC, beigetreten_am", (runde_id,))]


def aktive_teilnehmer(runde_id: int) -> set:
    return {t["verein_key"] for t in teilnehmer(runde_id) if t["aktiv"] == 1}


def runden_des_vereins(verein_key: str) -> list[dict]:
    with db_conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT r.*, (SELECT COUNT(*) FROM runde_teilnehmer x WHERE x.runde_id = r.id AND x.aktiv = 1) AS anzahl "
            "FROM runde r JOIN runde_teilnehmer t ON t.runde_id = r.id "
            "WHERE t.verein_key = ? AND t.aktiv = 1 ORDER BY r.jahr DESC, r.id DESC", (verein_key,))]


def runde_status(runde_id: int, status: str) -> None:
    with db_conn() as c:
        c.execute("UPDATE runde SET status = ? WHERE id = ?", (status, runde_id))


# ── Verlauf und Ergebnis ─────────────────────────────────────────────────────

def _protokoll(c, runde_id: int, verein_key: str, aktion: str, details: str = "") -> None:
    c.execute("INSERT INTO runde_protokoll (runde_id, zeit, verein_key, aktion, details) VALUES (?,?,?,?,?)",
              (runde_id, jetzt(), verein_key, aktion, details))


def protokoll(runde_id: int, verein_key: str, aktion: str, details: str = "") -> None:
    with db_conn() as c:
        _protokoll(c, runde_id, verein_key, aktion, details)


def protokoll_liste(runde_id: int) -> list[dict]:
    with db_conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runde_protokoll WHERE runde_id = ? ORDER BY id", (runde_id,))]


def offene_runden(verein_key: str, jahr: int) -> list[int]:
    """Offene Runden, in denen der Verein dabei ist und die dieses Jahr planen – dort wird eine Terminänderung protokolliert."""
    return [r["id"] for r in runden_des_vereins(verein_key) if r["status"] == "offen" and r["jahr"] == jahr]


def naechste_version(runde_id: int) -> int:
    with db_conn() as c:
        return (c.execute("SELECT MAX(version) FROM runde_ergebnis WHERE runde_id = ?", (runde_id,)).fetchone()[0] or 0) + 1


def ergebnis_speichern(runde_id: int, verein_key: str, daten: dict) -> int:
    with db_conn() as c:
        version = (c.execute("SELECT MAX(version) FROM runde_ergebnis WHERE runde_id = ?", (runde_id,)).fetchone()[0] or 0) + 1
        c.execute("INSERT INTO runde_ergebnis (runde_id, version, abgeschlossen_am, abgeschlossen_von, daten_json) "
                  "VALUES (?,?,?,?,?)", (runde_id, version, daten["abgeschlossen_am"], verein_key,
                                         json.dumps({**daten, "version": version}, ensure_ascii=False)))
        _protokoll(c, runde_id, verein_key, "Runde abgeschlossen", f"Ergebnis Version {version}")
        return version


def _ergebnis_aus(r) -> dict:
    e = dict(r)
    e["daten"] = json.loads(e.pop("daten_json"))
    return e


def ergebnisse(runde_id: int | None = None) -> list[dict]:
    sql, args = "SELECT * FROM runde_ergebnis", []
    if runde_id:
        sql += " WHERE runde_id = ?"
        args.append(runde_id)
    with db_conn() as c:
        return [_ergebnis_aus(r) for r in c.execute(sql + " ORDER BY abgeschlossen_am DESC, version DESC", args)]


def ergebnisse_des_vereins(verein_key: str) -> list[dict]:
    """Ergebnisse aller Runden, an denen der Verein beim Abschluss beteiligt war."""
    return [e for e in ergebnisse() if verein_key in {t["verein"] for t in e["daten"]["teilnehmer"]}]


def ergebnis(ergebnis_id: int) -> dict | None:
    with db_conn() as c:
        r = c.execute("SELECT * FROM runde_ergebnis WHERE id = ?", (ergebnis_id,)).fetchone()
    return _ergebnis_aus(r) if r else None


def stand_version(runde_id: int, jahr: int, kalender_stand: str = "") -> str:
    """Kennung des aktuellen Stands einer Runde – Prüfsumme über alle Entwürfe der Teilnehmer, die Teilnehmer, den
    Status und (kalender_stand) den Kalender. Nicht nur der letzte Änderungszeitpunkt: zwei Änderungen in derselben
    Sekunde (Treffen!) fielen sonst nicht auf."""
    with db_conn() as c:
        zeilen = c.execute("SELECT id, datum, uhrzeit, uhrzeit_bis, bezeichnung, ort, bestaetigt_am, veroeffentlicht_am "
                           "FROM entwurf WHERE substr(datum, 1, 4) = ? AND verein_key IN (SELECT verein_key FROM "
                           "runde_teilnehmer WHERE runde_id = ? AND aktiv = 1) ORDER BY id", (str(jahr), runde_id)).fetchall()
        keys = [r[0] for r in c.execute("SELECT verein_key FROM runde_teilnehmer WHERE runde_id = ? AND aktiv = 1 "
                                        "ORDER BY verein_key", (runde_id,))]
        st = c.execute("SELECT status FROM runde WHERE id = ?", (runde_id,)).fetchone()
    h = hashlib.sha1()
    for z in zeilen:
        h.update(repr(tuple(z)).encode())
    h.update(f"|{','.join(keys)}|{st['status'] if st else ''}|{kalender_stand}".encode())
    return h.hexdigest()[:16]


# ── Prüfkreis für Überschneidungen ───────────────────────────────────────────

def pruefkreis(verein_key: str) -> tuple[set, set]:
    """(dazu, ohne) – zusätzlich geprüfte und ausgeschlossene Vereine. Die eigene Gemeinde gilt automatisch."""
    with db_conn() as c:
        rows = c.execute("SELECT ziel_key, art FROM pruefkreis WHERE verein_key = ?", (verein_key,)).fetchall()
    return {r["ziel_key"] for r in rows if r["art"] == "dazu"}, {r["ziel_key"] for r in rows if r["art"] == "ohne"}


def pruefkreis_setzen(verein_key: str, dazu: set, ohne: set) -> None:
    """Ersetzt die Einstellungen des Vereins komplett. Ein Verein kann nicht zugleich dazu und ohne sein (ohne gewinnt)."""
    dazu = set(dazu) - set(ohne) - {verein_key}
    ohne = set(ohne) - {verein_key}
    with db_conn() as c:
        c.execute("DELETE FROM pruefkreis WHERE verein_key = ?", (verein_key,))
        c.executemany("INSERT INTO pruefkreis (verein_key, ziel_key, art) VALUES (?,?,?)",
                      [(verein_key, k, "dazu") for k in sorted(dazu)] + [(verein_key, k, "ohne") for k in sorted(ohne)])
