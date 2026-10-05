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

    # Ohne Anmeldung keine Entwürfe
    pruefe(c.get("/entwuerfe").status_code == 302, "Entwürfe nur angemeldet")

    # Planungsraum: Vereine der Gemeinde mit Einladung, noch keine Entwürfe
    r = c.post("/planung/neu", data={"gemeinde": "Testdorf|Landkreis Landshut", "jahr": "2027", "_csrf": tok})
    orga = r.headers["Location"]
    s = c.get(orga)
    einl = re.findall(rb'/einladung/([\w-]+)', s.data)
    pruefe(len(set(einl)) == 4, f"vier Einladungen (A, B, C, Pfarrei), waren {len(set(einl))}")
    pruefe(b"Noch keine Entw" in s.data, "Raum ohne Entwürfe")
    raum_id = 1

    def einladung_fuer(key):
        return next(v["einladung"] for v in DB.vereine_im_raum(raum_id) if v["verein_key"] == key)

    def als(key):
        c.post("/anmelden", data={"_csrf": tok, "verein": key})

    # Verein A über Einladung: Konto automatisch freigegeben, Entwürfe aus dem Vorjahr
    r = c.post(f"/einladung/{einladung_fuer('a')}", data={"_csrf": tok})
    pruefe(r.status_code == 302 and DB.konto("a")["freigabe"] == "einladung", "Einladung legt freigegebenes Konto an")
    c.post("/entwuerfe/aus-vorjahr?jahr=2027", data={"_csrf": tok, "jahr": "2027"})
    ea = DB.entwuerfe("a", 2027)
    pruefe([t["datum"] for t in ea] == ["2027-07-10"], f"Entwurf aus Vorjahr, war {[t['datum'] for t in ea]}")
    c.post("/entwuerfe/aus-vorjahr", data={"_csrf": tok, "jahr": "2027"})
    pruefe(len(DB.entwuerfe("a", 2027)) == 1, "Vorjahr zweimal erzeugen legt keine Doppel an")
    pruefe(b"aus dem Vorjahr erzeugen" not in c.get("/entwuerfe?jahr=2027").data, "Vorjahr-Knopf weg, wenn alles übernommen")
    gleich = [{"verein": "a", "bezeichnung": "Gartenfest", "datum_vorjahr": "2026-08-15", "uhrzeit": u} for u in ("10:00", "10:30")]
    pruefe(len(DB.offene_vorlage("a", gleich)) == 2, "gleicher Titel/Tag, andere Uhrzeit: zwei Vorschläge")
    s = c.get("/entwuerfe?jahr=2027")
    pruefe(b"Sommerfest" in s.data and b"Grillfest" not in s.data, "Entwurfsseite: eigene, keine fremden Entwürfe")
    pruefe(b"Sommerfest" not in c.get("/api/kollisionen?verein=b&von=2027-07-10").data, "Entwurf nicht in der Formular-Warnung")

    # Verein B ohne Einladung registriert: Entwürfe ja, veröffentlichen nein
    c.post("/abmelden", data={"_csrf": tok})
    c.post("/anmelden", data={"_csrf": tok, "verein": "b", "aktion": "konto"})
    pruefe(DB.konto("b")["freigabe"] == "ausstehend", "Registrierung ohne Einladung wartet auf Freigabe")
    c.post("/entwuerfe/aus-vorjahr", data={"_csrf": tok, "jahr": "2027"})
    eb = {t["bezeichnung"]: t for t in DB.entwuerfe("b", 2027)}
    pruefe(set(eb) == {"Grillfest", "Fischessen"} and eb["Fischessen"]["datum"] == "2027-03-26", "B: Vorjahr inkl. Karfreitag")
    pruefe(c.post(f"/entwuerfe/{eb['Grillfest']['_eid']}", data={"_csrf": tok, "aktion": "veroeffentlichen"}).status_code == 403,
           "ohne Freigabe kein Veröffentlichen")
    # Fremden Entwurf anfassen → 404
    pruefe(c.post(f"/entwuerfe/{ea[0]['_eid']}", data={"_csrf": tok, "aktion": "loeschen"}).status_code == 404,
           "fremder Entwurf nicht änderbar")
    # Einladung gibt B nachträglich frei
    c.post(f"/einladung/{einladung_fuer('b')}", data={"_csrf": tok})
    pruefe(DB.konto("b")["freigabe"] == "vko", "Einladung ersetzt ausstehende Freigabe")

    # Treffen: Konflikt Sommerfest A / Grillfest B, Organisatorin schlägt Verschiebung vor
    s = c.get(orga)
    pruefe(b"Konflikte am gleichen Tag (1)" in s.data, "Konflikt zwischen Entwürfen im Treffen")
    pruefe(c.post(f"{orga}/vorschlag/{ea[0]['_eid']}", data={"_csrf": tok, "datum": "2027-13-01"}).status_code == 400,
           "ungültiges Vorschlagsdatum")
    c.post(f"{orga}/vorschlag/{ea[0]['_eid']}", data={"_csrf": tok, "datum": "2027-07-17"})
    pruefe(DB.entwurf(ea[0]["_eid"])["datum"] == "2027-07-10", "Vorschlag ändert den Entwurf nicht selbst")
    pruefe(b"vorgeschlagen" in c.get(orga).data, "Vorschlag in der Organisator-Ansicht")
    # B sieht im Treffen beide, A übernimmt den Vorschlag
    s = c.get(f"/treffen/{raum_id}")
    pruefe(b"Sommerfest" in s.data and b"betrifft euch" in s.data, "Vereinssicht im Treffen")
    als("a")
    s = c.get("/entwuerfe?jahr=2027")
    pruefe(b"Vorschlag aus dem Planungstreffen" in s.data, "Verein sieht Vorschlag")
    c.post(f"/entwuerfe/{ea[0]['_eid']}", data={"_csrf": tok, "aktion": "vorschlag_annehmen"})
    pruefe(DB.entwurf(ea[0]["_eid"])["datum"] == "2027-07-17" and not DB.entwurf(ea[0]["_eid"])["vorschlag_datum"],
           "Vorschlag übernommen")
    pruefe(b"Konflikte am gleichen Tag (0)" in c.get(orga).data, "Konflikt nach Übernahme weg")

    # Ungültige Eingaben, Maskierung
    r = c.post("/entwuerfe/neu", data={"_csrf": tok, "datum": "2027-02-30", "bezeichnung": "X"})
    pruefe("fehler=" in r.headers["Location"], "ungültiges Datum abgelehnt")
    c.post("/entwuerfe/neu", data={"_csrf": tok, "datum": "2027-05-01", "bezeichnung": "<script>alert(1)</script>"})
    pruefe(b"<script>alert(1)</script>" not in c.get("/entwuerfe?jahr=2027").data, "Bezeichnung wird maskiert")

    # Veröffentlichen: einzeln, dann alle
    eid_mai = next(t["_eid"] for t in DB.entwuerfe("a", 2027) if t["datum"] == "2027-05-01")
    c.post(f"/entwuerfe/{eid_mai}", data={"_csrf": tok, "aktion": "veroeffentlichen"})
    pruefe([t["status"] for t in DB.entwuerfe("a", 2027)] == ["veroeffentlicht", "entwurf"], "einzeln veröffentlicht")
    pruefe(c.post(f"/entwuerfe/{eid_mai}", data={"_csrf": tok, "aktion": "speichern", "datum": "2027-05-02",
                                                "bezeichnung": "X"}).status_code == 302
           and DB.entwurf(eid_mai)["datum"] == "2027-05-01", "Veröffentlichtes nicht über Entwurf änderbar")
    c.post("/entwuerfe/alle-veroeffentlichen", data={"_csrf": tok, "jahr": "2027"})
    pruefe(all(t["status"] == "veroeffentlicht" for t in DB.entwuerfe("a", 2027)), "alle veröffentlicht")
    j = c.get("/api/kollisionen?verein=b&von=2027-07-17&ort=Testdorf").get_json()
    pruefe([x["bezeichnung"] for x in j] == ["Sommerfest"], "Veröffentlichtes zählt in der Formular-Warnung")

    # Abwählen / dazuholen
    vid_b = next(v["id"] for v in DB.vereine_im_raum(raum_id) if v["verein_key"] == "b")
    c.post(f"{orga}/verein/{vid_b}", data={"_csrf": tok, "aktiv": "0"})
    pruefe(all(t["verein"] != "b" for t in DB.entwuerfe(jahr=2027, vereine=DB.aktive_keys(raum_id))), "abgewählt zählt nicht")
    als("b")
    pruefe(c.get(f"/treffen/{raum_id}").status_code == 404, "abgewählter Verein sieht das Treffen nicht")
    pruefe(DB.entwuerfe("b", 2027) != [], "Entwürfe des abgewählten Vereins bleiben")
    c.post(f"{orga}/dazuholen", data={"_csrf": tok, "verein": ["b", "d", "gibtsnicht"]})
    pruefe(c.get(f"/treffen/{raum_id}").status_code == 200, "wieder aufgenommen")
    pruefe(b"Nachbar</span>" in c.get(orga).data, "Nachbarverein dazugeholt")

    # Exporte, falsche Tokens, abgeschlossene Planung
    pruefe(c.get("/entwuerfe/export.xlsx?jahr=2027").status_code == 200, "Vereins-Export")
    pruefe(c.get(f"{orga}/export.docx").status_code == 200, "Raum-Export")
    pruefe(c.get(f"{orga}/export.exe").status_code == 404, "unbekanntes Format 404")
    pruefe(c.get("/p/falsch").status_code == 404 and c.get("/einladung/falsch").status_code == 404, "falscher Token 404")
    pruefe(c.post("/anmelden", data={"_csrf": tok, "verein": "c"}).status_code == 403, "ohne Konto keine Anmeldung")
    c.post(f"{orga}/status", data={"_csrf": tok, "aktion": "abschliessen"})
    eid_b = DB.entwuerfe("b", 2027)[0]["_eid"]
    pruefe(c.post(f"{orga}/vorschlag/{eid_b}", data={"_csrf": tok, "datum": "2027-08-01"}).status_code == 403,
           "nach Abschluss keine Vorschläge")
    pruefe(c.post(f"/einladung/{einladung_fuer('c')}", data={"_csrf": tok}).status_code == 403,
           "nach Abschluss keine Registrierung per Einladung")
    pruefe(c.get("/anmelden").status_code == 200 and c.get("/planung").status_code == 200, "Übersichtsseiten")

print(f"\n{OK} Prüfungen ok, {len(FEHLER)} Fehler")
sys.exit(1 if FEHLER else 0)
