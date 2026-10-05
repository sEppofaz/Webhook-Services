#!/usr/bin/env python3
"""Offline-Tests Prototyp Jahresplanung (Branch `jahresplanung`).

    ~/.venvs/vko-jahresplanung/bin/python tests/test_jahresplanung.py

Prüft Datumsregeln (Ostern, n-ter Wochentag, Serien, Blöcke), Kollisionen (Gemeinde,
Gottesdienste, Wochenende), alle Exportformate und den Ablauf im Prototyp (Planungsraum,
Vereins-Link, CSRF, fremde Termine). Eigene Testdaten in einem Temp-Verzeichnis – kein Netz,
keine Live-Daten. Formate ohne installierte Bibliothek (fpdf2, odfpy) werden übersprungen.
"""
from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import zipfile
from datetime import date
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "prototyp" / "jahresplanung"))

from shared.wiederholung import (erster_advent, fest_datum, nter_wochentag, ostern,  # noqa: E402
                                 regel_fuer, uebertrage, vorlage)
from shared.kollision import STUFE_TAG, STUFE_WOCHENENDE, kollisionen  # noqa: E402
from shared import export as E  # noqa: E402

FEHLER: list[str] = []
OK = 0


def pruefe(bedingung, text):
    global OK
    if bedingung:
        OK += 1
    else:
        FEHLER.append(text)
        print("  ✗", text)


# ── 1. Datumsregeln ──────────────────────────────────────────────────────────
print("1. Datumsregeln")
pruefe(ostern(2026) == date(2026, 4, 5), "Ostern 2026")
pruefe(ostern(2027) == date(2027, 3, 28), "Ostern 2027")
pruefe(ostern(2025) == date(2025, 4, 20), "Ostern 2025")
pruefe(ostern(2038) == date(2038, 4, 25), "Ostern 2038 (spätester Termin)")
pruefe(fest_datum("Karfreitag", 2027) == date(2027, 3, 26), "Karfreitag 2027")
pruefe(fest_datum("Fronleichnam", 2027) == date(2027, 5, 27), "Fronleichnam 2027")
pruefe(fest_datum("Pfingstsonntag", 2027) == date(2027, 5, 16), "Pfingsten 2027")
pruefe(erster_advent(2026) == date(2026, 11, 29), "1. Advent 2026")
pruefe(erster_advent(2027) == date(2027, 11, 28), "1. Advent 2027")
pruefe(fest_datum("Volkstrauertag", 2027) == date(2027, 11, 14), "Volkstrauertag 2027")
pruefe(nter_wochentag(2027, 7, 5, 2) == date(2027, 7, 10), "2. Samstag Juli 2027")
pruefe(nter_wochentag(2027, 2, 5, 5) is None, "5. Samstag Februar 2027 gibt es nicht")
pruefe(nter_wochentag(2027, 5, 2, -1) == date(2027, 5, 26), "letzter Mittwoch Mai 2027")

r = regel_fuer(date(2026, 4, 3))
pruefe(r["fest"] == "Karfreitag" and uebertrage(r, date(2026, 4, 3), 2027) == date(2027, 3, 26), "Karfreitag → Karfreitag")
r = regel_fuer(date(2026, 5, 23), "Wattturnier am Pfingstsamstag")
pruefe(uebertrage(r, date(2026, 5, 23), 2027) == date(2027, 5, 15) and "vor Pfingstsonntag" in r["text"],
       "Pfingstsamstag per Stichwort")
r = regel_fuer(date(2026, 6, 7), "Frohleichnamsprozession")
pruefe(r["typ"] == "fest" and uebertrage(r, date(2026, 6, 7), 2027) == date(2027, 5, 30),
       "Schreibweise Frohleichnam, Sonntag danach")
r = regel_fuer(date(2026, 5, 1), "Maibaum")
pruefe(r["typ"] == "datum" and uebertrage(r, date(2026, 5, 1), 2027) == date(2027, 5, 1), "1. Mai bleibt 1. Mai")
r = regel_fuer(date(2026, 10, 18), "Kirchweih")
pruefe(uebertrage(r, date(2026, 10, 18), 2027) == date(2027, 10, 17), "Kirchweih = 3. Sonntag Oktober")
r = regel_fuer(date(2026, 8, 29), "Sommerfest")   # 5. Samstag
pruefe(r["n"] == -1 and uebertrage(r, date(2026, 8, 29), 2027) == date(2027, 8, 28), "5. Samstag → letzter Samstag")
r = regel_fuer(date(2026, 7, 11), "Oster-Grillfest im Juli")   # Stichwort, aber zu weit weg
pruefe(r["typ"] == "monat", "Stichwort ohne Nähe zum Fest wird ignoriert")

# Serie: letzter Mittwoch (Woche 4 und 5 gemischt) + Ergänzung der nicht erfassten Monate
serie = [{"verein": "a", "bezeichnung": "Seniorentreff", "datum": x}
         for x in ("2026-05-27", "2026-06-24", "2026-07-29", "2026-09-30")]
v = vorlage(serie, 2027, daten_ab=date(2026, 5, 1))
pruefe(all(x["regel_typ"] == "serie" for x in v), "Serie erkannt")
pruefe({x["datum"] for x in v if not x.get("ergaenzt")} == {"2027-05-26", "2027-06-30", "2027-07-28", "2027-09-29"},
       "Serie letzter Mittwoch übertragen")
pruefe(sorted(x["datum"][5:7] for x in v if x.get("ergaenzt")) == ["01", "02", "03", "04"], "Jan–Apr ergänzt")
# Keine Serie bei uneinheitlicher Woche
v = vorlage([{"verein": "a", "bezeichnung": "Sitzung", "datum": x} for x in ("2026-10-06", "2026-11-10", "2026-12-08")], 2027)
pruefe(all(x["regel_typ"] == "monat" for x in v), "1./2./2. Dienstag ist keine Serie")
# Block: Volksfest Fr–So wandert zusammen
v = vorlage([{"verein": "a", "bezeichnung": "Volksfest", "datum": x} for x in ("2026-07-24", "2026-07-25", "2026-07-26")], 2027)
pruefe([x["datum"] for x in v] == ["2027-07-23", "2027-07-24", "2027-07-25"], "Block bleibt Fr–So")
# Ausschluss + Quelljahr
v = vorlage([{"verein": "p", "bezeichnung": "Messe", "datum": "2026-06-07", "quelle": "Pfarrbrief"},
             {"verein": "a", "bezeichnung": "Alt", "datum": "2025-06-07"}], 2027,
            ausschliessen=lambda t: t.get("quelle") == "Pfarrbrief")
pruefe(v == [], "Pfarrbrief und falsches Jahr ausgeschlossen")

# ── 2. Kollisionen ───────────────────────────────────────────────────────────
print("2. Kollisionen")


def geo(gemeinde, lk="Landkreis Landshut"):
    return {"ortschaften": [{"ort": gemeinde, "gemeinde": gemeinde, "landkreis": lk}]}


META = {
    "a": {"gemeinde": "Gemeinde Testdorf", "landkreis": "Landkreis Landshut", "heimatort": "Testdorf"},
    "b": {"gemeinde": "Testdorf", "landkreis": "Landkreis Landshut", "heimatort": "Testdorf"},
    "c": {"gemeinde": "Testdorf", "landkreis": "Landkreis Landshut", "heimatort": "Testdorf"},
    "d": {"gemeinde": "Andersort", "landkreis": "Landkreis Landshut", "heimatort": "Andersort"},
    "p": {"gemeinde": "Testdorf", "landkreis": "Landkreis Landshut", "heimatort": "Testdorf", "quelle": "Pfarrbrief"},
}
LABELS = {"a": "Verein A", "b": "Verein B", "c": "Verein C", "d": "Verein D", "p": "Pfarrei"}
RUBRIKEN = {"p": "Pfarrei"}
TERMINE = [
    {"id": "1", "verein": "b", "datum": "2026-07-11", "uhrzeit": "19:00", "bezeichnung": "Grillfest", "_geo": geo("Testdorf")},
    {"id": "2", "verein": "d", "datum": "2026-07-11", "bezeichnung": "Fremdfest", "_geo": geo("Andersort")},
    {"id": "3", "verein": "p", "datum": "2026-07-11", "bezeichnung": "Messe", "_geo": geo("Testdorf")},
    {"id": "4", "verein": "c", "datum": "2026-07-12", "bezeichnung": "Frühschoppen", "_geo": geo("Testdorf")},
    {"id": "5", "verein": "a", "datum": "2026-07-11", "bezeichnung": "Eigener", "_geo": geo("Testdorf")},
    {"id": "6", "verein": "c", "datum": "2026-07-11", "bezeichnung": "Intern", "intern": True, "_geo": geo("Testdorf")},
    {"id": "7", "verein": "c", "datum": "2026-07-11", "bezeichnung": "Gelöscht", "geloescht": True, "_geo": geo("Testdorf")},
]
k = kollisionen(TERMINE, META, LABELS, {"verein": "a", "datum": "2026-07-11", "_geo": geo("Testdorf")}, RUBRIKEN)
pruefe([x["id"] for x in k] == ["1"], f"nur Grillfest kollidiert, war {[x['id'] for x in k]}")
k = kollisionen(TERMINE, META, LABELS, {"verein": "a", "datum": "2026-07-11", "_geo": geo("Testdorf")}, RUBRIKEN,
                wochenende=True)
pruefe([(x["id"], x["stufe"]) for x in k] == [("1", STUFE_TAG), ("4", STUFE_WOCHENENDE)], "Wochenende als zweite Stufe")
k = kollisionen(TERMINE, META, LABELS, {"verein": "a", "tage": ["2026-07-10", "2026-07-11"], "_geo": geo("Testdorf")}, RUBRIKEN)
pruefe([x["entwurf_datum"] for x in k] == ["2026-07-11"], "mehrtägiger Entwurf")
k = kollisionen(TERMINE, META, LABELS, {"verein": "a", "datum": "2026-07-11", "_geo": geo("Testdorf")}, RUBRIKEN,
                ausser_ids={"1"})
pruefe(k == [], "eigener Termin beim Bearbeiten ausgenommen")
k = kollisionen(TERMINE, META, LABELS, {"verein": "d", "datum": "2026-07-11", "_geo": geo("Andersort")}, RUBRIKEN)
pruefe(k == [], "andere Gemeinde kollidiert nicht")
pruefe(kollisionen(TERMINE, META, LABELS, {"verein": "a", "datum": "kaputt"}, RUBRIKEN) == [], "kaputtes Datum")
k = kollisionen(TERMINE, META, LABELS, {"verein": "a", "datum": "2026-07-11", "_geo": geo("Testdorf")}, RUBRIKEN,
                zusatz_vereine={"d"})
pruefe(sorted((x["id"], x["nachbar"]) for x in k) == [("1", False), ("2", True)], "Nachbarverein aus anderer Gemeinde zählt")
k = kollisionen(TERMINE, META, LABELS, {"verein": "a", "datum": "2026-07-11", "_geo": geo("Testdorf")}, RUBRIKEN,
                zusatz_vereine={"b"})
pruefe([x["nachbar"] for x in k] == [False], "Zusatzverein aus eigener Gemeinde ist kein Nachbar")
k = kollisionen(TERMINE, META, LABELS, {"verein": "x", "datum": "2026-07-11"}, RUBRIKEN, zusatz_vereine={"d"})
pruefe([x["id"] for x in k] == ["2"], "ohne bekannte Gemeinde nur die gewählten Vereine")

# ── 3. Export ────────────────────────────────────────────────────────────────
print("3. Export")
ZEILEN = [{"datum": "2027-07-10", "uhrzeit": "19:00", "uhrzeit_bis": "23:00", "bezeichnung": "Sommerfest „Am Weiher“",
           "ort": "Festplatz; Testdorf", "verein": "a", "verein_name": "Verein A"},
          {"datum": "2027-03-26", "bezeichnung": "Karfreitag\nBEGIN:VEVENT", "verein": "b", "verein_name": "Verein B"}]
for fmt in E.FORMATE:
    try:
        inhalt, mime, endung = E.exportiere(fmt, "Jahresprogramm 2027 – Testdorf", ZEILEN)
    except ImportError as e:
        print(f"  – {fmt} übersprungen ({e.name} fehlt)")
        continue
    pruefe(len(inhalt) > 200 and endung == fmt, f"{fmt} erzeugt")
    if fmt in ("docx", "xlsx", "odt", "ods"):
        z = zipfile.ZipFile(io.BytesIO(inhalt))
        text = " ".join(z.read(n).decode("utf-8", "ignore") for n in z.namelist() if n.endswith(".xml"))
        pruefe("Sommerfest" in text and "Verein A" in text, f"{fmt} enthält Termin und Verein")
    if fmt == "pdf":
        pruefe(inhalt[:4] == b"%PDF", "pdf beginnt mit %PDF")
    if fmt == "ics":
        s = inhalt.decode()
        pruefe(s.split("\r\n").count("BEGIN:VEVENT") == 2, "ics: genau zwei Termine (kein eingeschleuster)")
        pruefe("DTSTART;VALUE=DATE:20270326" in s and "DTSTART:20270710T190000" in s, "ics: Zeiten")
        pruefe("Festplatz\\; Testdorf" in s, "ics: Escaping")
pruefe(E.dateiname("Termine 2027 – Verein/A", "pdf") == "Termine_2027_Verein_A.pdf", "Dateiname bereinigt")

# ── 4. Prototyp-App ──────────────────────────────────────────────────────────
print("4. Prototyp-App")
try:
    import flask  # noqa: F401
except ImportError:
    print("  – Flask fehlt, übersprungen")
else:
    tmp = Path(tempfile.mkdtemp(prefix="vko-jp-"))
    import daten as D
    import db as DB
    D.DATEN_DIR = tmp
    D.TERMINE_FILE = tmp / "termine.json"
    DB.DB_FILE = tmp / "planung.sqlite"
    VORJAHR = [
        {"id": "a1", "verein": "a", "datum": "2026-07-11", "uhrzeit": "18:00", "bezeichnung": "Sommerfest", "_geo": geo("Testdorf")},
        {"id": "b1", "verein": "b", "datum": "2026-07-11", "uhrzeit": "19:00", "bezeichnung": "Grillfest", "_geo": geo("Testdorf")},
        {"id": "b2", "verein": "b", "datum": "2026-04-03", "bezeichnung": "Fischessen", "_geo": geo("Testdorf")},
        {"id": "d1", "verein": "d", "datum": "2026-07-11", "bezeichnung": "Fremdfest", "_geo": geo("Andersort")},
        {"id": "p1", "verein": "p", "datum": "2026-07-11", "bezeichnung": "Messe", "quelle": "Pfarrbrief", "_geo": geo("Testdorf")},
    ]
    D.TERMINE_FILE.write_text(json.dumps({"labels": LABELS, "termine": VORJAHR, "meta": META, "rubriken": RUBRIKEN}))
    DB.init()
    import app as A
    A.app.config["TESTING"] = True
    c = A.app.test_client()

    def csrf_von(seite: bytes) -> str:
        m = re.search(rb'name="_csrf" value="([0-9a-f]+)"', seite)
        return m.group(1).decode() if m else ""

    s = c.get("/")
    pruefe(s.status_code == 200 and b"Jahresplanung" in s.data, "Startseite")
    tok = csrf_von(s.data)
    pruefe(c.post("/planung/neu", data={"gemeinde": "Testdorf|Landkreis Landshut", "jahr": "2027"}).status_code == 403,
           "POST ohne CSRF abgelehnt")
    j = c.get("/api/kollisionen?verein=a&von=2026-07-11&ort=Testdorf").get_json()
    pruefe([x["bezeichnung"] for x in j] == ["Grillfest"], f"API-Kollisionen, war {j}")
    j = c.get("/api/kollisionen?verein=a&von=2026-07-11&mit=d,gibtsnicht").get_json()
    pruefe(sorted(x["bezeichnung"] for x in j) == ["Fremdfest", "Grillfest"], f"API mit Nachbarn, war {j}")
    pruefe(b"Weitere Vereine einbeziehen" in c.get("/kollision").data, "Nachbar-Auswahl auf der Seite")
    s = c.get("/vorlage?verein=b&jahr=2027")
    pruefe(b"Karfreitag" in s.data and b"Sommerfest" in s.data, "Vorlage zeigt Regel und Konflikt")

    # Ohne Anmeldung keine Entwürfe, keine Runden
    pruefe(c.get("/entwuerfe").status_code == 302 and c.get("/runden").status_code == 302, "nur angemeldet")

    def als(key, neu=False):
        c.post("/abmelden", data={"_csrf": tok})
        c.post("/anmelden", data={"_csrf": tok, "verein": key, **({"aktion": "konto"} if neu else {})})

    # Konten: jede Registrierung wartet auf Josef
    for v in ("a", "b", "c", "d"):
        als(v, neu=True)
    pruefe(all(DB.konto(v)["freigabe"] == "ausstehend" for v in "abcd"), "neue Konten warten auf Freigabe")
    als("a")
    pruefe(c.post("/runden/neu", data={"_csrf": tok, "name": "X", "jahr": "2027"}).status_code == 403,
           "ohne Freigabe keine Runde starten")
    c.post("/entwuerfe/aus-vorjahr", data={"_csrf": tok, "jahr": "2027"})
    pruefe([t["datum"] for t in DB.entwuerfe("a", 2027)] == ["2027-07-10"], "Entwürfe auch vor der Freigabe")
    pruefe(c.post(f"/entwuerfe/{DB.entwuerfe('a', 2027)[0]['_eid']}", data={"_csrf": tok, "aktion": "veroeffentlichen"}).status_code == 403,
           "ohne Freigabe kein Veröffentlichen")
    for v in ("a", "b", "d"):
        c.post("/demo/freigeben", data={"_csrf": tok, "verein": v})
    pruefe(DB.freigegeben("a") and not DB.freigegeben("c"), "Freigabe durch VKO (c bleibt ausstehend)")
    c.post("/entwuerfe/aus-vorjahr", data={"_csrf": tok, "jahr": "2027"})
    pruefe(len(DB.entwuerfe("a", 2027)) == 1, "Vorjahr zweimal erzeugen legt keine Doppel an")
    pruefe(b"aus dem Vorjahr erzeugen" not in c.get("/entwuerfe?jahr=2027").data, "Vorjahr-Knopf weg, wenn alles übernommen")
    gleich = [{"verein": "a", "bezeichnung": "Gartenfest", "datum_vorjahr": "2026-08-15", "uhrzeit": u} for u in ("10:00", "10:30")]
    pruefe(len(DB.offene_vorlage("a", gleich)) == 2, "gleicher Titel/Tag, andere Uhrzeit: zwei Vorschläge")

    # A startet eine Runde
    r = c.post("/runden/neu", data={"_csrf": tok, "name": "Jahresplanung Testdorf", "jahr": "2027"})
    rid = int(r.headers["Location"].rsplit("/", 1)[1])
    runde = DB.runde(rid)
    s = c.get(f"/runde/{rid}")
    pruefe(s.status_code == 200 and DB.code_anzeige(runde["code"]).encode() in s.data, "Gastgeber sieht Code")
    pruefe(all(ch in DB.CODE_ZEICHEN for ch in runde["code"]) and len(runde["code"]) == 6, "Code ohne Verwechsler")

    # B tritt per Link bei – erst Bestätigungsseite, dann aktiver Klick
    als("b")
    c.post("/entwuerfe/aus-vorjahr", data={"_csrf": tok, "jahr": "2027"})
    pruefe(c.get(f"/runde/{rid}").status_code == 404, "vor dem Beitritt keine Sicht auf die Runde")
    s = c.get(f"/r/{runde['link']}")
    pruefe(b"beitreten" in s.data and "b" not in DB.aktive_teilnehmer(rid), "Link zeigt Beitritt, tritt nicht automatisch bei")
    pruefe(c.post(f"/runde/{rid}/beitreten", data={"_csrf": tok, "nachweis": "falsch"}).status_code == 403,
           "Beitritt nur mit gültigem Link/Code")
    c.post(f"/runde/{rid}/beitreten", data={"_csrf": tok, "nachweis": runde["link"]})
    pruefe("b" in DB.aktive_teilnehmer(rid), "B ist beigetreten")
    s = c.get(f"/runde/{rid}")
    pruefe(b"Sommerfest" in s.data and b"Grillfest" in s.data and b"Konflikte am gleichen Tag (1)" in s.data,
           "Runde zeigt Entwürfe aller mit Konflikt")
    pruefe(b"betrifft euch" in s.data, "Konflikt als eigener markiert")
    pruefe(b"Sommerfest" not in c.get("/api/kollisionen?verein=b&von=2027-07-10").data, "Entwurf nicht in der Formular-Warnung")

    # B kann A's Termin nicht ändern; ändert und bestätigt seinen eigenen in der Runde
    eid_a = DB.entwuerfe("a", 2027)[0]["_eid"]
    pruefe(c.post(f"/entwuerfe/{eid_a}", data={"_csrf": tok, "aktion": "loeschen", "runde": rid}).status_code == 404,
           "fremder Termin nicht änderbar")
    eid_b = next(t["_eid"] for t in DB.entwuerfe("b", 2027) if t["bezeichnung"] == "Grillfest")
    r = c.post(f"/entwuerfe/{eid_b}", data={"_csrf": tok, "aktion": "speichern", "runde": rid, "datum": "2027-07-17",
                                            "uhrzeit": "19:00", "bezeichnung": "Grillfest", "ort": ""})
    pruefe(r.headers["Location"].startswith(f"/runde/{rid}"), "Änderung aus der Runde springt zur Runde zurück")
    pruefe(b"Konflikte am gleichen Tag (0)" in c.get(f"/runde/{rid}").data, "Konflikt nach eigener Änderung weg")
    c.post("/entwuerfe/alle", data={"_csrf": tok, "jahr": "2027", "aktion": "bestaetigen", "runde": rid})
    pruefe(all(t["status"] == "bestaetigt" for t in DB.entwuerfe("b", 2027)), "alle eigenen bestätigt")
    c.post(f"/entwuerfe/{eid_b}", data={"_csrf": tok, "aktion": "speichern", "datum": "2027-07-18", "uhrzeit": "19:00",
                                        "bezeichnung": "Grillfest", "ort": ""})
    pruefe(DB.entwurf(eid_b)["status"] == "entwurf", "Änderung hebt Bestätigung auf")

    # C (nicht freigegeben) und D per Code
    als("c")
    pruefe(c.post(f"/runde/{rid}/beitreten", data={"_csrf": tok, "nachweis": runde["code"]}).status_code == 403,
           "ohne Freigabe kein Beitritt")
    als("d")
    s = c.post("/runde/beitreten-code", data={"_csrf": tok, "code": DB.code_anzeige(runde["code"]).lower()})
    pruefe(s.status_code == 200 and b"beitreten" in s.data, "Code: klein und mit Bindestrich geht")
    c.post(f"/runde/{rid}/beitreten", data={"_csrf": tok, "nachweis": runde["code"]})
    pruefe("d" in DB.aktive_teilnehmer(rid), "D per Code beigetreten (Nachbargemeinde)")
    pruefe(b"Fremdfest" in c.get(f"/runde/{rid}").data or DB.entwuerfe("d", 2027) == [], "Runde sichtbar für D")
    r = c.post("/runde/beitreten-code", data={"_csrf": tok, "code": "AAA-AAA"})
    pruefe("fehler=" in r.headers["Location"], "falscher Code")
    for _ in range(10):
        c.post("/runde/beitreten-code", data={"_csrf": tok, "code": "BBB-BBB"})
    pruefe(c.post("/runde/beitreten-code", data={"_csrf": tok, "code": DB.code_anzeige(runde["code"])}).status_code == 429,
           "Versuchslimit gegen Durchprobieren")
    pruefe(c.post(f"/runde/{rid}/erneuern", data={"_csrf": tok}).status_code == 403, "nur Organisator erneuert")

    # Netz: Lebenszeichen und Stand für „andere haben geändert“
    pruefe(c.get("/ping").status_code == 204 and c.get("/ping").headers["Cache-Control"] == "no-store", "/ping ohne Cache")
    stand1 = c.get(f"/runde/{rid}/stand").get_json()["stand"]
    als("a")
    c.post(f"/entwuerfe/{eid_a}", data={"_csrf": tok, "aktion": "bestaetigen"})
    als("d")
    pruefe(c.get(f"/runde/{rid}/stand").get_json()["stand"] != stand1, "Stand ändert sich, wenn ein anderer Verein bestätigt")
    pruefe(b"vkoAbruf" in c.get(f"/runde/{rid}").data and b'id="netz"' in c.get("/").data, "Netz-Hinweis auf den Seiten")

    # Organisator: erneuern, entfernen, abschließen
    als("a")
    c.post(f"/runde/{rid}/erneuern", data={"_csrf": tok})
    neu_r = DB.runde(rid)
    pruefe(neu_r["link"] != runde["link"] and neu_r["code"] != runde["code"], "Link und Code erneuert")
    pruefe(c.get(f"/r/{runde['link']}").status_code == 404, "alter Link ungültig")
    pruefe("d" in DB.aktive_teilnehmer(rid), "wer drin ist, bleibt drin")
    c.post(f"/runde/{rid}/teilnehmer", data={"_csrf": tok, "verein": "d", "aktiv": "0"})
    pruefe("d" not in DB.aktive_teilnehmer(rid), "Gastgeber entfernt D")
    als("d")
    pruefe(c.get(f"/runde/{rid}").status_code == 404, "entfernter Verein sieht die Runde nicht")
    pruefe(c.post(f"/runde/{rid}/beitreten", data={"_csrf": tok, "nachweis": neu_r["link"]}).status_code == 403,
           "entfernter Verein kann nicht neu beitreten")
    als("b")
    c.post(f"/runde/{rid}/verlassen", data={"_csrf": tok})
    pruefe("b" not in DB.aktive_teilnehmer(rid), "B verlässt die Runde")
    c.post(f"/runde/{rid}/beitreten", data={"_csrf": tok, "nachweis": neu_r["code"]})
    pruefe("b" in DB.aktive_teilnehmer(rid), "B kommt mit Code wieder")

    # Ungültige Eingaben, Maskierung
    r = c.post("/entwuerfe/neu", data={"_csrf": tok, "datum": "2027-02-30", "bezeichnung": "X"})
    pruefe("fehler=" in r.headers["Location"], "ungültiges Datum abgelehnt")
    c.post("/entwuerfe/neu", data={"_csrf": tok, "datum": "2027-05-01", "bezeichnung": "<script>alert(1)</script>"})
    pruefe(b"<script>alert(1)</script>" not in c.get(f"/runde/{rid}").data, "Bezeichnung wird maskiert")

    # Veröffentlichen: einzeln, dann alle – nur eigene
    eid_mai = next(t["_eid"] for t in DB.entwuerfe("b", 2027) if t["datum"] == "2027-05-01")
    c.post(f"/entwuerfe/{eid_mai}", data={"_csrf": tok, "aktion": "veroeffentlichen"})
    pruefe(DB.entwurf(eid_mai)["status"] == "veroeffentlicht", "einzeln veröffentlicht")
    c.post(f"/entwuerfe/{eid_mai}", data={"_csrf": tok, "aktion": "speichern", "datum": "2027-05-02", "bezeichnung": "X"})
    pruefe(DB.entwurf(eid_mai)["datum"] == "2027-05-01", "Veröffentlichtes nicht über Entwurf änderbar")
    c.post("/entwuerfe/alle", data={"_csrf": tok, "jahr": "2027", "aktion": "veroeffentlichen"})
    pruefe(all(t["status"] == "veroeffentlicht" for t in DB.entwuerfe("b", 2027)), "alle eigenen veröffentlicht")
    pruefe(DB.entwuerfe("a", 2027)[0]["status"] != "veroeffentlicht", "fremde bleiben unveröffentlicht")
    j = c.get("/api/kollisionen?verein=a&von=2027-07-18&ort=Testdorf").get_json()
    pruefe([x["bezeichnung"] for x in j] == ["Grillfest"], "Veröffentlichtes zählt in der Formular-Warnung")

    # Exporte, Abschluss
    pruefe(c.get("/entwuerfe/export.xlsx?jahr=2027").status_code == 200, "Vereins-Export")
    pruefe(c.get(f"/runde/{rid}/export.docx").status_code == 200, "Runden-Export")
    pruefe(c.get(f"/runde/{rid}/export.exe").status_code == 404, "unbekanntes Format 404")
    pruefe(c.post("/anmelden", data={"_csrf": tok, "verein": "p"}).status_code == 403, "ohne Konto keine Anmeldung")
    als("a")
    # Verlauf: Organisator, Beitritte, Terminänderungen
    verlauf = [(v["verein_key"], v["aktion"], v["details"]) for v in DB.protokoll_liste(rid)]
    pruefe(verlauf[0][:2] == ("a", "Runde gestartet (Organisator)"), "Verlauf: Organisator dokumentiert")
    pruefe(("b", "beigetreten", "per Link") in verlauf and ("d", "beigetreten", "per Code") in verlauf, "Verlauf: Beitritte")
    pruefe(any(v[0] == "b" and v[1] == "Termin geändert" and "Sa 10.07. → Sa 17.07." in v[2] for v in verlauf),
           f"Verlauf: Verschiebung mit altem und neuem Datum")
    pruefe(("a", "entfernt", "Verein D") in verlauf and ("b", "ausgetreten", "") in verlauf, "Verlauf: entfernt/ausgetreten")
    pruefe(any(v[1] == "veröffentlicht" for v in verlauf), "Verlauf: veröffentlicht")

    pruefe(c.get("/archiv").status_code == 302 and DB.ergebnisse_des_vereins("a") == [], "alte Archiv-Adresse leitet um, vor Abschluss leer")
    r = c.post(f"/runde/{rid}/status", data={"_csrf": tok, "aktion": "abschliessen"})
    pruefe("Archiv" in r.headers["Location"] or "meldung=" in r.headers["Location"], "Abschluss meldet Archiv")
    s = c.get(f"/runde/{rid}")
    pruefe(b"abgeschlossen" in s.data and b'value="bestaetigen"' not in s.data, "abgeschlossen: Runde nur noch lesbar")
    e1 = DB.ergebnisse(rid)[0]
    d1 = e1["daten"]
    pruefe(e1["version"] == 1 and d1["organisator_name"] == "Verein A"
           and {t["verein"] for t in d1["teilnehmer"]} == {"a", "b"}, "Ergebnis: Organisator und Teilnehmer beim Abschluss")
    pruefe(len(d1["termine"]) == len(DB.entwuerfe(jahr=2027, vereine={"a", "b"})), "Ergebnis: alle Termine der Teilnehmer")
    pruefe(d1["verlauf"][-1]["aktion"] == "Runde abgeschlossen", "Ergebnis enthält den Verlauf bis zum Abschluss")
    pdf1 = c.get(f"/archiv/ergebnis/{e1['id']}.pdf")
    pruefe(pdf1.status_code == 200 and pdf1.data[:4] == b"%PDF", "Ergebnis-PDF")
    pruefe(c.get(f"/archiv/ergebnis/{e1['id']}.pdf").data == pdf1.data, "gleicher Stand = gleiches PDF")
    pruefe(c.get(f"/archiv/ergebnis/{e1['id']}.xlsx").status_code == 200, "Ergebnis auch als Excel")
    pruefe(b"Ergebnis Version 1" in c.get("/runden").data, "Ergebnis bei der Runde (Organisator)")
    als("b")
    pruefe(b"Ergebnis Version 1" in c.get("/runden").data and c.get(f"/archiv/ergebnis/{e1['id']}.pdf").status_code == 200,
           "Ergebnis bei der Runde (Teilnehmer)")
    c.post(f"/runde/{rid}/verlassen", data={"_csrf": tok})
    s = c.get("/runden")
    pruefe(b"Fr\xc3\xbchere Runden" in s.data and b"Ergebnis Version 1" in s.data, "nach Austritt bleibt das Ergebnis")
    c.post(f"/runde/{rid}/beitreten", data={"_csrf": tok, "nachweis": neu_r["code"]})
    als("d")
    pruefe(c.get(f"/archiv/ergebnis/{e1['id']}.pdf").status_code == 404 and b"Ergebnis Version" not in c.get("/runden").data,
           "entfernter Verein bekommt das Ergebnis nicht")
    # Wieder öffnen und erneut abschließen → Version 2, Version 1 bleibt
    als("a")
    c.post(f"/runde/{rid}/status", data={"_csrf": tok, "aktion": "oeffnen"})
    c.post(f"/runde/{rid}/status", data={"_csrf": tok, "aktion": "abschliessen"})
    pruefe([e["version"] for e in DB.ergebnisse(rid)] == [2, 1], "zweiter Abschluss = Version 2, Version 1 bleibt")
    pruefe(c.get(f"/archiv/ergebnis/{e1['id']}.pdf").data == pdf1.data, "Version 1 unverändert")
    als("c")
    c.post("/demo/freigeben", data={"_csrf": tok, "verein": "c"})
    pruefe(c.post(f"/runde/{rid}/beitreten", data={"_csrf": tok, "nachweis": neu_r["link"]}).status_code == 403,
           "nach Abschluss kein Beitritt")
    pruefe(c.get("/anmelden").status_code == 200 and c.get("/runden").status_code == 200, "Übersichtsseiten")
    pruefe(c.get("/planung").status_code == 302, "alte Adresse leitet um")

print(f"\n{OK} Prüfungen ok, {len(FEHLER)} Fehler")
sys.exit(1 if FEHLER else 0)
