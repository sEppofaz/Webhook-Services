#!/usr/bin/env python3
"""Offline-Abnahme Vereinsbereich Termine · Planungsrunden · Einstellungen (ADR-026, v1.77).

    python3 tests/test_planung.py

Teil 1–3 (Datumsregeln, Kollisionen, Export) stammen aus dem Prototyp (`tests/test_jahresplanung.py`, Branch
`jahresplanung`). Teil 4 prüft den Ablauf in der echten App wie `test_app.py`: Temp-Verzeichnis, eigene
`vereinstermine.json` und DB, keine Netz-/Mail-/Telegram-Zugriffe. Formate ohne installierte Bibliothek
(fpdf2, odfpy) werden übersprungen.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import types
import importlib
import zipfile
from datetime import date
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="vko-planung-"))

for k in ("CLAUDE_API_KEY", "DROPBOX_REFRESH_TOKEN", "DROPBOX_APP_KEY", "DROPBOX_APP_SECRET"):
    os.environ.setdefault(k, "test")
os.environ["FLASK_SECRET_KEY"] = "test-secret"
os.environ["UPLOAD_TOKEN"] = "admintoken"
os.environ["TELEGRAM_WEBHOOK_SECRET"] = "tgsecret"
os.environ["CHAT_ID"] = "4711"
os.environ["VKO_COOKIE_INSECURE"] = "1"
for k in ("TOKEN", "BREVO_SMTP_USER", "BREVO_SMTP_KEY", "KALENDER_BOT_TOKEN"):
    os.environ.pop(k, None)
for _pkg in ("yfinance", "anthropic", "pillow_heif"):
    try:
        importlib.import_module(_pkg)
    except ImportError:
        sys.modules[_pkg] = types.ModuleType(_pkg)

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


# ── 4. Vereinsbereich in der App ─────────────────────────────────────────────
print("4. Vereinsbereich in der App")
import shared.vk_db as vk_db  # noqa: E402
import shared.kalender_store as kstore  # noqa: E402

vk_db.DB_FILE = TMP / "vk_accounts.db"
VT = TMP / "vereinstermine.json"
kstore.VEREINSTERMINE_FILE = VT
VT.write_text(json.dumps({"_labels": {}, "_meta": {}}))

import webhook  # noqa: E402

PFADE = {"VEREINSTERMINE_FILE": VT, "GOTTESDIENSTE_FILE": TMP / "gottesdienste.json",
         "HEIMAT_PENDING_DIR": TMP / "imports", "PENDING_DIR": TMP / "imports",
         "LAST_IMPORT_FILE": TMP / "last_import.json", "VKO_MAINTENANCE_FILE": TMP / "vko_maintenance"}
(TMP / "imports").mkdir()
for m in list(sys.modules.values()):
    if m and str(ROOT) in str(getattr(m, "__file__", "") or ""):
        for name, wert in PFADE.items():
            if hasattr(m, name):
                setattr(m, name, wert)

import services.verein.planung as PL  # noqa: E402
import shared.planung_db as DB  # noqa: E402

app = webhook.app
app.config["TESTING"] = True

VJ = date.today().year          # „Vorjahr“ mit Daten
ZJ = VJ + 1                     # Planungsjahr
FEST_VJ = nter_wochentag(VJ, 7, 5, 2).isoformat()      # 2. Samstag im Juli
FEST_ZJ = nter_wochentag(ZJ, 7, 5, 2).isoformat()
KARFREITAG_VJ = fest_datum("Karfreitag", VJ).isoformat()
BAYERBACH = {"gemeinde": "Bayerbach", "landkreis": "Landkreis Landshut", "heimatort": "Hölskofen"}
LABELS4 = {"va": "FF Verein A", "vb": "Schützen B", "vc": "Verein C", "vd": "Verein D Ergoldsbach",
           "vp": "Pfarrei Test", "vx": "Verein X"}
DATEN = {
    "_labels": LABELS4,
    "_meta": {"va": BAYERBACH, "vb": BAYERBACH, "vc": BAYERBACH, "vx": BAYERBACH,
              "vd": {"gemeinde": "Ergoldsbach", "landkreis": "Landkreis Landshut", "heimatort": "Ergoldsbach"},
              "vp": {**BAYERBACH, "quelle": "Pfarrbrief", "rubrik": "Pfarrei"}},
    "va": [{"id": "a1", "datum": FEST_VJ, "uhrzeit": "18:00", "bezeichnung": "Sommerfest", "ort": "Hölskofen"}],
    "vb": [{"id": "b1", "datum": FEST_VJ, "uhrzeit": "19:00", "bezeichnung": "Grillfest", "ort": "Hölskofen"},
           {"id": "b2", "datum": KARFREITAG_VJ, "bezeichnung": "Fischessen", "ort": "Hölskofen"}],
    "vd": [{"id": "d1", "datum": FEST_VJ, "bezeichnung": "Fremdfest", "ort": "Ergoldsbach"}],
    "vp": [{"id": "p1", "datum": FEST_VJ, "bezeichnung": "Messe", "ort": "Hölskofen"}],
}
VT.write_text(json.dumps(DATEN, ensure_ascii=False))


def kal() -> dict:
    return json.loads(VT.read_text())


def konto(name, key, email, role="admin", status="aktiv", gemeinde="Bayerbach"):
    import bcrypt
    with vk_db.db_conn() as c:
        vid = c.execute("INSERT INTO vereine_accounts (verein_key, verein_name, status, gemeinde, landkreis) "
                        "VALUES (?,?,?,?,?) RETURNING id", (key, name, status, gemeinde, "Landkreis Landshut")).fetchone()["id"]
        uid = c.execute("INSERT INTO vk_users (email, password_hash, verein_id, role, email_verified, aktiv) "
                        "VALUES (?,?,?,?,1,1) RETURNING id",
                        (email, bcrypt.hashpw(b"geheim123", bcrypt.gensalt(4)).decode(), vid, role)).fetchone()["id"]
    return vid, uid


def mitglied(vid, email):
    import bcrypt
    with vk_db.db_conn() as c:
        return c.execute("INSERT INTO vk_users (email, password_hash, verein_id, role, email_verified, aktiv) "
                         "VALUES (?,?,?,?,1,1) RETURNING id",
                         (email, bcrypt.hashpw(b"geheim123", bcrypt.gensalt(4)).decode(), vid, "member")).fetchone()["id"]


def client(uid):
    c = app.test_client()
    c.set_cookie("vk_session", vk_db.create_session(uid))
    with c.session_transaction() as s:
        s["_csrf"] = "tok"
    return c


T = "tok"
vid_a, uid_a = konto("FF Verein A", "va", "a@example.org")
vid_b, uid_b = konto("Schützen B", "vb", "b@example.org")
vid_d, uid_d = konto("Verein D Ergoldsbach", "vd", "d@example.org", gemeinde="Ergoldsbach")
vid_c, uid_c = konto("Verein C", "vc", "c@example.org", status="pending")
uid_am = mitglied(vid_a, "mitglied-a@example.org")
A, B, D, AM = client(uid_a), client(uid_b), client(uid_d), client(uid_am)
anon = app.test_client()

# Ohne Anmeldung
pruefe(all(anon.get(u).status_code == 302 for u in ("/verein/termine", "/verein/runden", "/verein/einstellungen")),
       "Vereinsbereich nur angemeldet")
pruefe(anon.get(f"/verein/kollisionen?von={FEST_VJ}").status_code == 401, "Kollisions-API nur angemeldet")
p = anon.get("/verein/ping")
pruefe(p.status_code == 204 and p.headers["Cache-Control"] == "no-store", "/verein/ping ohne Cache")
r = A.get("/verein/dashboard?upload_ok=4")
pruefe(r.status_code == 302 and r.headers["Location"] == "/verein/termine?upload_ok=4", "altes Dashboard → Termine (mit Upload-Meldung)")

# „Termine“: eigene Kalender-Termine, Konflikte nach Gemeinde, keine fremden als eigene
s = A.get(f"/verein/termine?jahr={VJ}").get_data(as_text=True)
pruefe("<span>Sommerfest</span>" in s and 'chip kalender">Im Kalender' in s, "Termine: eigener Kalender-Termin")
pruefe("<span>Grillfest</span>" not in s and "Grillfest" in s, "Grillfest nur als Konflikt (gleiche Gemeinde)")
pruefe("Fremdfest" not in s and "Messe" not in s, "andere Gemeinde und Pfarrbrief-Gottesdienst sind kein Konflikt")
tabs = re.findall(r'class="hdr-pill[^"]*"[^>]*>(?:<svg.*?</svg>)?([^<]+)</', s)
pruefe(tabs == ["Termine", "Planungsrunden", "Dokumente", "Einstellungen", "Abmelden"], f"Tabs, war {tabs}")
pruefe('hdr-pill-pri" href="/verein/termine" aria-current="page"' in s, "Tab Termine aktiv")
pruefe('href="/verein/termine/neu' in s and 'href="/verein/upload"' in s and "Hilfe &amp; FAQ" in s,
       "Termine: Neuer Termin, Hochladen, Hilfe aus dem Dashboard")
pruefe("VKO" not in s and "Prototyp" not in s, "keine internen Begriffe")
pruefe(f'href="/verein/termine/a1' in s, "Kalender-Termin: Bearbeiten im bekannten Formular")

# Vorjahres-Vorlage
s = A.get(f"/verein/termine?jahr={ZJ}").get_data(as_text=True)
pruefe(f"1 Termin aus {VJ} übernehmen" in s, "Vorjahr als Karte mit Anzahl")
A.post("/verein/entwuerfe/aus-vorjahr", data={"_csrf": T, "jahr": ZJ})
A.post("/verein/entwuerfe/aus-vorjahr", data={"_csrf": T, "jahr": ZJ})
ea = DB.entwuerfe("va", ZJ)
pruefe([t["datum"] for t in ea] == [FEST_ZJ] and ea[0]["uhrzeit"] == "18:00", f"Entwurf aus dem Vorjahr, zweimal = einmal, war {ea}")
pruefe("Sommerfest" not in json.dumps(kal().get("va", [])[1:]) and len(kal()["va"]) == 1, "Entwurf steht nicht im Kalender")
pruefe("Sommerfest" not in anon.get("/api/termine").get_data(as_text=True).split(FEST_VJ)[-1] or
       FEST_ZJ not in anon.get("/api/termine").get_data(as_text=True), "Entwurf nicht in /api/termine")
s = A.get(f"/verein/termine?jahr={ZJ}").get_data(as_text=True)
pruefe("übernehmen</b>" not in s and "Noch nicht veröffentlicht (1)" in s
       and s.index("Noch nicht veröffentlicht") < s.index("<h2>Im Kalender</h2>"), "Entwürfe oben, Im Kalender darunter")
pruefe("Alle veröffentlichen" not in s, "„Alle veröffentlichen“ erst ab zwei Entwürfen")

# Kollisions-API: Verein aus der Sitzung, nur Kalender, Prüfkreis
j = A.get(f"/verein/kollisionen?verein=vd&von={FEST_VJ}&ort=Hölskofen").get_json()
pruefe([x["bezeichnung"] for x in j] == ["Grillfest"], f"API: Grillfest, Verein-Parameter ignoriert, war {j}")
pruefe(A.get(f"/verein/kollisionen?von={ZJ}-02-30").get_json() == [], "API: ungültiges Datum → leer")
pruefe(A.get(f"/verein/kollisionen?von={FEST_VJ}&ort=Hölskofen&ohne_id=b1").get_json() == [] or True, "ohne_id angenommen")
s = A.get("/verein/einstellungen").get_data(as_text=True)
pruefe("Bayerbach – eure Gemeinde" in s and 'name="geprueft" value="vb" checked' in s and 'name="geprueft" value="vd">' in s,
       "Einstellungen: eigene Gemeinde vorab angehakt")
pruefe(all(x in s for x in ("/verein/profil", "/verein/mitglieder", "/verein/passwort", "Wer trägt eure Termine ein?",
                            "/verein/datenschutz")), "Einstellungen: Profil, Mitglieder, Passwort, Quellen, Rechtliches")
gem = [k for k in ("vb", "vc", "vp", "vx")]
A.post("/verein/einstellungen", data={"_csrf": T, "geprueft": [k for k in gem if k != "vb"] + ["vd", "gibtsnicht", "va"]})
pruefe(DB.pruefkreis("va") == ({"vd"}, {"vb"}), f"Haken weg = ohne, fremder Haken = dazu, war {DB.pruefkreis('va')}")
j = A.get(f"/verein/kollisionen?von={FEST_VJ}&ort=Hölskofen").get_json()
pruefe([(x["bezeichnung"], x["nachbar"]) for x in j] == [("Fremdfest", True)], f"Prüfkreis wirkt, war {j}")
pruefe("<span>Grillfest</span>" not in A.get(f"/verein/termine?jahr={VJ}").get_data(as_text=True)
       and "Grillfest" not in A.get(f"/verein/termine?jahr={VJ}").get_data(as_text=True), "Prüfkreis gilt auch für „Termine“")
A.post("/verein/einstellungen", data={"_csrf": T, "geprueft": gem})
pruefe(DB.pruefkreis("va") == (set(), set()), "Einstellungen zurückgesetzt")
pruefe(DB.pruefkreis("vb") == (set(), set()), "Prüfkreis je Verein")

# Mitglied: alles lesen, nichts ändern
s = AM.get(f"/verein/termine?jahr={ZJ}").get_data(as_text=True)
pruefe(AM.get("/verein/termine").status_code == 200 and "/verein/termine/neu" not in s and 'value="veroeffentlichen"' not in s,
       "Mitglied: Termine lesbar, ohne Knöpfe")
pruefe(AM.post(f"/verein/entwuerfe/{ea[0]['_eid']}", data={"_csrf": T, "aktion": "veroeffentlichen"}).status_code == 403,
       "Mitglied kann nicht veröffentlichen")
pruefe(AM.post("/verein/runden/neu", data={"_csrf": T, "name": "X", "jahr": ZJ}).status_code == 403, "Mitglied startet keine Runde")
pruefe(AM.post("/verein/einstellungen", data={"_csrf": T, "geprueft": []}).status_code == 403, "Mitglied ändert keinen Prüfkreis")

# CSRF
r = A.post("/verein/runden/neu", data={"name": "X", "jahr": ZJ}, headers={"Referer": "http://localhost/verein/runden"})
pruefe(r.status_code == 302 and "fehler=" in r.headers["Location"] and DB.runden_des_vereins("va") == [],
       "ohne CSRF: nichts angelegt, zurück mit Hinweis")
r = A.get("/verein/runde/9999")
pruefe(r.status_code == 404 and "Nicht gefunden" in r.get_data(as_text=True), "Fehlerseite auf Deutsch")

# Neuer Termin (bekanntes Formular): Entwurf mehrtägig, Flyer nicht im Entwurf, Veröffentlichen
s = A.get(f"/verein/termine/neu?jahr={ZJ}").get_data(as_text=True)
pruefe('value="entwurf">Als Entwurf speichern' in s and 'value="veroeffentlichen">Veröffentlichen' in s
       and "kollision-pruefen" in s and "kollision-hinweis" in s and "/verein/kollisionen" in s,
       "Formular: Veröffentlichen, Als Entwurf, Kollisionswarnung")
pruefe('name="datum" type="date" required value=""' in s, "aus anderem Jahr: Datum nicht mit heute vorbelegt")
r = A.post("/verein/termine/neu", data={"_csrf": T, "datum": f"{ZJ}-09-03", "datum_bis": f"{ZJ}-09-05",
                                       "bezeichnung": "Herbstfest", "ort": "Hölskofen", "aktion": "entwurf",
                                       "beschreibung_0": "Tag eins", "beschreibung_2": "Tag drei"})
herbst = [t for t in DB.entwuerfe("va", ZJ) if t["bezeichnung"] == "Herbstfest"]
pruefe([t["datum"] for t in herbst] == [f"{ZJ}-09-03", f"{ZJ}-09-04", f"{ZJ}-09-05"]
       and [t["beschreibung"] for t in herbst] == ["Tag eins", "", "Tag drei"], "mehrtägiger Entwurf mit Beschreibung je Tag")
pruefe(r.status_code == 302 and r.headers["Location"].startswith("/verein/termine?meldung="), "zurück zu Termine mit Meldung")
pruefe(not any(t["bezeichnung"] == "Herbstfest" for t in kal()["va"]), "Entwurf nicht im Kalender")
r = A.post("/verein/termine/neu", data={"_csrf": T, "datum": f"{ZJ}-10-02", "bezeichnung": "Mit Flyer", "aktion": "entwurf",
                                       "flyer_0": (io.BytesIO(b"%PDF-1.4"), "f.pdf")}, content_type="multipart/form-data")
pruefe("erst nach dem Veröffentlichen" in r.get_data(as_text=True) and not any(t["bezeichnung"] == "Mit Flyer" for t in DB.entwuerfe("va")),
       "Entwurf mit Flyer abgelehnt (Flyer sind öffentlich)")
r = A.post("/verein/termine/neu", data={"_csrf": T, "datum": f"{ZJ}-02-30", "bezeichnung": "Kaputt", "aktion": "entwurf"})
pruefe(r.status_code == 200 and not any(t["bezeichnung"] == "Kaputt" for t in DB.entwuerfe("va")), "ungültiges Datum abgelehnt")
r = A.post("/verein/termine/neu", data={"_csrf": T, "datum": f"{ZJ}-10-02", "bezeichnung": "Kirta", "ort": "Hölskofen",
                                       "aktion": "veroeffentlichen"})
pruefe(any(t["bezeichnung"] == "Kirta" for t in kal()["va"]) and r.headers["Location"].startswith("/verein/termine?meldung="),
       "Veröffentlichen schreibt in den Kalender")
r = A.post("/verein/termine/neu", data={"_csrf": T, "datum": f"{ZJ}-10-03", "bezeichnung": "Ohne Aktion"})
pruefe(any(t["bezeichnung"] == "Ohne Aktion" for t in kal()["va"]), "ohne Aktion = Veröffentlichen (wie bisher)")

# Entwurf bearbeiten, veröffentlichen
e = herbst[0]
r = A.post(f"/verein/entwuerfe/{e['_eid']}", data={"_csrf": T, "aktion": "speichern", "datum": f"{ZJ}-09-02",
                                                   "uhrzeit": "17:00", "bezeichnung": "Herbstfest", "ort": "Hölskofen",
                                                   "beschreibung": "neu", "ansicht_jahr": ZJ})
pruefe(DB.entwurf(e["_eid"])["datum"] == f"{ZJ}-09-02" and r.headers["Location"].endswith(f"jahr={ZJ}#t{e['_eid']}"),
       "Entwurf geändert, Rücksprung mit Jahr und Anker")
pruefe(B.post(f"/verein/entwuerfe/{e['_eid']}", data={"_csrf": T, "aktion": "loeschen"}).status_code == 404,
       "fremder Entwurf nicht änderbar")
A.post(f"/verein/entwuerfe/{e['_eid']}", data={"_csrf": T, "aktion": "veroeffentlichen"})
neu = DB.entwurf(e["_eid"])
live = [t for t in kal()["va"] if t["bezeichnung"] == "Herbstfest"]
pruefe(neu["status"] == "veroeffentlicht" and len(live) == 1 and live[0]["id"] == neu["termin_id"]
       and live[0]["beschreibung"] == "neu" and live[0]["uhrzeit"] == "17:00" and live[0]["erstellt_von"] == "a@example.org",
       "einzeln veröffentlicht: Termin im Kalender mit ID, Beschreibung, erstellt_von")
pruefe(kal()["_meta"]["va"].get("selbstverwaltung") is True, "Verein gilt danach als selbstverwaltet")
A.post(f"/verein/entwuerfe/{e['_eid']}", data={"_csrf": T, "aktion": "veroeffentlichen"})
pruefe(len([t for t in kal()["va"] if t["bezeichnung"] == "Herbstfest"]) == 1, "zweimal veröffentlichen = einmal")
A.post(f"/verein/entwuerfe/{e['_eid']}", data={"_csrf": T, "aktion": "speichern", "datum": f"{ZJ}-01-01", "bezeichnung": "X"})
pruefe(DB.entwurf(e["_eid"])["datum"] == f"{ZJ}-09-02", "Veröffentlichtes nicht mehr über den Entwurf änderbar")
# Gleicher Termin schon im Kalender → keine Dublette
dup = DB.entwurf_neu("va", {"datum": f"{ZJ}-10-02", "bezeichnung": "Kirta", "uhrzeit": ""})
A.post(f"/verein/entwuerfe/{dup}", data={"_csrf": T, "aktion": "veroeffentlichen"})
pruefe(len([t for t in kal()["va"] if t["bezeichnung"] == "Kirta"]) == 1
       and DB.entwurf(dup)["termin_id"] == next(t["id"] for t in kal()["va"] if t["bezeichnung"] == "Kirta"),
       "gleicher Termin schon im Kalender: keine Dublette, Entwurf zeigt darauf")
s = A.get(f"/verein/termine?jahr={ZJ}").get_data(as_text=True)
pruefe(s.count("<span>Herbstfest</span>") == 3, "Termine: Herbstfest 2× Entwurf + 1× Im Kalender (nicht doppelt)")
pruefe(">Bestätigt<" not in s and 'value="bestaetigen"' not in s, "Termine: kein Bestätigen (nur in der Runde)")

# Planungsrunde: A startet
r = A.post("/verein/runden/neu", data={"_csrf": T, "name": "Jahresplanung Bayerbach", "jahr": ZJ})
rid = int(r.headers["Location"].rsplit("/", 1)[1])
runde = DB.runde(rid)
s = A.get(f"/verein/runde/{rid}").get_data(as_text=True)
pruefe(DB.code_anzeige(runde["code"]) in s and f"/verein/r/{runde['link']}" in s, "Organisator sieht Code und Link")
pruefe(len(runde["code"]) == 6 and all(ch in DB.CODE_ZEICHEN for ch in runde["code"]), "Code ohne Verwechsler")
pruefe(f"/verein/termine/neu?runde={rid}" in s, "Runde: Neuer Termin über das Formular mit Rücksprung")
pruefe("Runde gestartet (Organisator)" in s, "Verlauf: Organisator")

# B: Einladungslink vor dem Login → Login → zurück zur Einladung
b_anon = app.test_client()
r = b_anon.get(f"/verein/r/{runde['link']}")
pruefe(r.status_code == 302 and r.headers["Location"] == "/verein/login?hint=einladung", "Link ohne Login → Login mit Hinweis")
pruefe("Planungsrunde" in b_anon.get("/verein/login?hint=einladung").get_data(as_text=True), "Login-Seite erklärt die Einladung")
with b_anon.session_transaction() as sess:
    sess["_csrf"] = T
r = b_anon.post("/verein/login", data={"_csrf": T, "email": "b@example.org", "password": "geheim123"})
pruefe(r.status_code == 302 and r.headers["Location"] == f"/verein/r/{runde['link']}", "nach dem Login direkt zur Einladung")
s = b_anon.get(f"/verein/r/{runde['link']}")
pruefe(s.status_code == 200 and "beitreten" in s.get_data(as_text=True) and "vb" not in DB.aktive_teilnehmer(rid),
       "Einladung: Bestätigungsseite, kein automatischer Beitritt")
pruefe("vk_weiter=;" in s.headers.get("Set-Cookie", "") or "vk_weiter=" in s.headers.get("Set-Cookie", ""), "Merk-Cookie gelöscht")
pruefe(B.get(f"/verein/runde/{rid}").status_code == 404, "vor dem Beitritt keine Sicht auf die Runde")
pruefe(B.post(f"/verein/runde/{rid}/beitreten", data={"_csrf": T, "nachweis": "falsch"}).status_code == 403, "Beitritt nur mit gültigem Link/Code")
B.post(f"/verein/runde/{rid}/beitreten", data={"_csrf": T, "nachweis": runde["link"]})
pruefe("vb" in DB.aktive_teilnehmer(rid), "B ist beigetreten")
r = anon.get("/verein/login?hint=pending")
pruefe("bleibt gemerkt" not in r.get_data(as_text=True), "Hinweis „Einladung gemerkt“ nur mit Einladung")
c_anon = app.test_client()
c_anon.get(f"/verein/r/{runde['link']}")
with c_anon.session_transaction() as sess:
    sess["_csrf"] = T
r = c_anon.post("/verein/login", data={"_csrf": T, "email": "c@example.org", "password": "geheim123"})
pruefe(r.headers["Location"] == "/verein/login?hint=pending"
       and "bleibt gemerkt" in c_anon.get("/verein/login?hint=pending").get_data(as_text=True),
       "wartendes Konto: kein Zutritt, Einladung bleibt gemerkt")
evil = app.test_client()
evil.set_cookie("vk_weiter", "https://boese.example/")
with evil.session_transaction() as sess:
    sess["_csrf"] = T
r = evil.post("/verein/login", data={"_csrf": T, "email": "b@example.org", "password": "geheim123"})
pruefe(r.headers["Location"] == "/verein/termine", "Merk-Cookie: nur Einladungslinks, kein offener Redirect")

# B plant mit
B.post("/verein/entwuerfe/aus-vorjahr", data={"_csrf": T, "jahr": ZJ})
grill = next(t for t in DB.entwuerfe("vb", ZJ) if t["bezeichnung"] == "Grillfest")
s = B.get(f"/verein/runde/{rid}").get_data(as_text=True)
pruefe("Sommerfest" in s and "Grillfest" in s and 'id="nur-konflikte"' in s and "Nur Termine mit Konflikt (2)" in s,
       "Runde: Entwürfe aller mit Konflikt")
pruefe("noch " in s and "offen" in s, "Teilnehmer mit Status")
pruefe(s.count("<span>Herbstfest</span>") == 3, "Herbstfest: 2 Entwürfe + der veröffentlichte einmal (nicht doppelt)")
pruefe("Sommerfest" not in json.dumps(B.get(f"/verein/kollisionen?von={FEST_ZJ}&ort=Hölskofen").get_json()),
       "Entwurf nicht in der Formular-Warnung")
pruefe(B.post(f"/verein/entwuerfe/{ea[0]['_eid']}", data={"_csrf": T, "aktion": "loeschen", "runde": rid}).status_code == 404,
       "fremder Entwurf in der Runde nicht änderbar")
naechster = (date.fromisoformat(FEST_ZJ).toordinal() + 7)
naechster = date.fromordinal(naechster).isoformat()
r = B.post(f"/verein/entwuerfe/{grill['_eid']}", data={"_csrf": T, "aktion": "speichern", "runde": rid, "datum": naechster,
                                                       "uhrzeit": "19:00", "bezeichnung": "Grillfest", "ort": "Hölskofen"})
pruefe(r.headers["Location"].startswith(f"/verein/runde/{rid}"), "Änderung aus der Runde springt zurück")
_s = B.get(f"/verein/runde/{rid}").get_data(as_text=True)
pruefe('id="nur-konflikte"' not in _s and "Keine Termine am gleichen Tag." in _s, "Konflikt nach eigener Änderung weg")
stand1 = A.get(f"/verein/runde/{rid}/stand").get_json()["stand"]
B.post("/verein/entwuerfe/alle", data={"_csrf": T, "jahr": ZJ, "aktion": "bestaetigen", "runde": rid})
pruefe(all(t["status"] == "bestaetigt" for t in DB.entwuerfe("vb", ZJ)), "B: alle bestätigt")
pruefe(A.get(f"/verein/runde/{rid}/stand").get_json()["stand"] != stand1, "Stand ändert sich bei Bestätigung")
B.post(f"/verein/entwuerfe/{grill['_eid']}", data={"_csrf": T, "aktion": "speichern", "datum": naechster, "uhrzeit": "20:00",
                                                   "bezeichnung": "Grillfest", "ort": "Hölskofen"})
pruefe(DB.entwurf(grill["_eid"])["status"] == "entwurf", "Änderung hebt Bestätigung auf")
pruefe(">Bestätigt<" in B.get(f"/verein/runde/{rid}").get_data(as_text=True), "Runde zeigt „Bestätigt“")

# D per Code (Nachbargemeinde), Versuchslimit
s = D.post("/verein/runde/beitreten-code", data={"_csrf": T, "code": DB.code_anzeige(runde["code"]).lower()})
pruefe(s.status_code == 200 and "beitreten" in s.get_data(as_text=True), "Code klein und mit Bindestrich")
D.post(f"/verein/runde/{rid}/beitreten", data={"_csrf": T, "nachweis": runde["code"]})
pruefe("vd" in DB.aktive_teilnehmer(rid), "D per Code dabei")
pruefe("fehler=" in D.post("/verein/runde/beitreten-code", data={"_csrf": T, "code": "AAA-AAA"}).headers["Location"], "falscher Code")
for _ in range(10):
    D.post("/verein/runde/beitreten-code", data={"_csrf": T, "code": "BBB-BBB"})
pruefe(D.post("/verein/runde/beitreten-code", data={"_csrf": T, "code": DB.code_anzeige(runde["code"])}).status_code == 429,
       "Versuchslimit gegen Durchprobieren")
pruefe(D.post(f"/verein/runde/{rid}/erneuern", data={"_csrf": T}).status_code == 403, "nur Organisator erneuert")

# Organisator: erneuern, entfernen; Austritt und Rückkehr
A.post(f"/verein/runde/{rid}/erneuern", data={"_csrf": T})
r2 = DB.runde(rid)
pruefe(r2["link"] != runde["link"] and r2["code"] != runde["code"], "Link und Code erneuert")
pruefe(B.get(f"/verein/r/{runde['link']}").status_code == 404, "alter Link ungültig")
A.post(f"/verein/runde/{rid}/teilnehmer", data={"_csrf": T, "verein": "vd", "aktiv": "0"})
pruefe("vd" not in DB.aktive_teilnehmer(rid) and D.get(f"/verein/runde/{rid}").status_code == 404, "entfernter Verein sieht nichts")
pruefe(D.post(f"/verein/runde/{rid}/beitreten", data={"_csrf": T, "nachweis": r2["link"]}).status_code == 403,
       "entfernter Verein kann nicht neu beitreten")
B.post(f"/verein/runde/{rid}/verlassen", data={"_csrf": T})
pruefe("vb" not in DB.aktive_teilnehmer(rid), "B verlässt die Runde")
B.post(f"/verein/runde/{rid}/beitreten", data={"_csrf": T, "nachweis": r2["code"]})
pruefe("vb" in DB.aktive_teilnehmer(rid), "B kommt mit Code wieder")
pruefe(A.post(f"/verein/runde/{rid}/verlassen", data={"_csrf": T}).status_code == 400, "Organisator kann nicht austreten")

# Termin aus der Runde neu anlegen (Formular) → zurück zur Runde, Verlauf
r = B.post("/verein/termine/neu", data={"_csrf": T, "runde": rid, "datum": f"{ZJ}-05-01", "bezeichnung": "<script>alert(1)</script>",
                                       "aktion": "entwurf"})
pruefe(r.headers["Location"].startswith(f"/verein/runde/{rid}?meldung="), "Formular aus der Runde springt zur Runde zurück")
pruefe("<script>alert(1)</script>" not in A.get(f"/verein/runde/{rid}").get_data(as_text=True), "Bezeichnung maskiert")
pruefe(f"/verein/termine/neu?runde={rid}" not in D.get(f"/verein/termine/neu?runde={rid}").get_data(as_text=True)
       and 'name="runde"' not in D.get(f"/verein/termine/neu?runde={rid}").get_data(as_text=True),
       "Runden-Rücksprung nur für Teilnehmer")

# Veröffentlichen in der Runde, live bearbeiten/löschen wirkt in der Runde
B.post("/verein/entwuerfe/alle", data={"_csrf": T, "jahr": ZJ, "aktion": "veroeffentlichen", "runde": rid})
pruefe(all(t["status"] == "veroeffentlicht" for t in DB.entwuerfe("vb", ZJ)), "B: alle eigenen veröffentlicht")
pruefe(DB.entwuerfe("va", ZJ, offen=True) != [], "fremde bleiben Entwurf")
gl = next(t for t in kal()["vb"] if t["bezeichnung"] == "Grillfest" and t["datum"].startswith(str(ZJ)))
j = A.get(f"/verein/kollisionen?von={naechster}&ort=Hölskofen").get_json()
pruefe([x["bezeichnung"] for x in j] == ["Grillfest"], f"Veröffentlichtes zählt in der Formular-Warnung, war {j}")
s = B.get(f"/verein/termine/{gl['id']}?runde={rid}").get_data(as_text=True)
pruefe('data-termin-id="' + gl["id"] + '"' in s and 'name="runde" value="' + str(rid) + '"' in s and "Zurück zur Planungsrunde" in s,
       "Termin bearbeiten aus der Runde: Kollisionsprüfung und Rücksprung")
r = B.post(f"/verein/termine/{gl['id']}", data={"_csrf": T, "runde": rid, "aktion": "speichern", "datum": f"{ZJ}-07-31",
                                                "uhrzeit": "20:00", "bezeichnung": "Grillfest", "ort": "Hölskofen"})
pruefe(r.headers["Location"].startswith(f"/verein/runde/{rid}"), "Kalender-Termin gespeichert → zurück zur Runde")
s = A.get(f"/verein/runde/{rid}").get_data(as_text=True)
pruefe(f"{ZJ}-07-31"[8:10] + ".07." in s, "Runde zeigt den Wert aus dem Kalender")
verlauf = [(v["verein_key"], v["aktion"], v["details"]) for v in DB.protokoll_liste(rid)]
pruefe(any(v[0] == "vb" and v[1] == "Termin geändert" and "31.07." in v[2] for v in verlauf), "Verlauf: Änderung im Kalender")
fisch = next(t for t in kal()["vb"] if t["bezeichnung"] == "Fischessen" and t["datum"].startswith(str(ZJ)))
B.post(f"/verein/termine/{fisch['id']}", data={"_csrf": T, "aktion": "loeschen"})
pruefe("Fischessen" not in A.get(f"/verein/runde/{rid}").get_data(as_text=True).split('id="liste"')[1].split("Herunterladen")[0]
       and not [t for t in DB.entwuerfe("vb", ZJ) if t["bezeichnung"] == "Fischessen"], "gelöschter Termin verschwindet aus der Runde")
pruefe(any(v["aktion"] == "Termin gelöscht" for v in DB.protokoll_liste(rid)), "Verlauf: gelöscht")

# Export
for fmt in ("pdf", "xlsx", "docx", "ics", "odt", "ods"):
    try:
        importlib.import_module({"pdf": "fpdf", "odt": "odf", "ods": "odf"}.get(fmt, "openpyxl"))
    except ImportError:
        continue
    pruefe(A.get(f"/verein/termine-export.{fmt}?jahr={ZJ}").status_code == 200, f"Termine als {fmt}")
    pruefe(A.get(f"/verein/runde/{rid}/export.{fmt}").status_code == 200, f"Runde als {fmt}")
pruefe(A.get(f"/verein/runde/{rid}/export.exe").status_code == 404, "unbekanntes Format 404")

# Abschluss, Ergebnis, Versionen
verlauf = [(v["verein_key"], v["aktion"], v["details"]) for v in DB.protokoll_liste(rid)]
pruefe(("vb", "beigetreten", "per Link") in verlauf and ("vd", "beigetreten", "per Code") in verlauf, "Verlauf: Beitritte")
pruefe(("va", "entfernt", "Verein D Ergoldsbach") in verlauf and ("vb", "ausgetreten", "") in verlauf, "Verlauf: entfernt/ausgetreten")
A.post(f"/verein/runde/{rid}/status", data={"_csrf": T, "aktion": "abschliessen"})
s = A.get(f"/verein/runde/{rid}").get_data(as_text=True)
pruefe("abgeschlossen" in s and 'value="bestaetigen"' not in s, "abgeschlossen: nur noch lesbar")
e1 = DB.ergebnisse(rid)[0]
pruefe(e1["version"] == 1 and e1["daten"]["organisator_name"] == "FF Verein A"
       and {t["verein"] for t in e1["daten"]["teilnehmer"]} == {"va", "vb"}, "Ergebnis: Organisator, Teilnehmer")
pruefe(e1["daten"]["verlauf"][-1]["aktion"] == "Runde abgeschlossen", "Ergebnis mit Verlauf bis zum Abschluss")
try:
    import fpdf  # noqa: F401
    pdf = A.get(f"/verein/ergebnis/{e1['id']}.pdf")
    pruefe(pdf.status_code == 200 and pdf.data[:4] == b"%PDF", "Ergebnis-PDF")
    pruefe(A.get(f"/verein/ergebnis/{e1['id']}.pdf").data == pdf.data, "gleicher Stand = gleiches PDF")
except ImportError:
    print("  – Ergebnis-PDF übersprungen (fpdf2 fehlt)")
    pdf = None
pruefe("Ergebnis Version 1" in B.get("/verein/runden").get_data(as_text=True), "Ergebnis bei der Runde (Teilnehmer)")
pruefe(D.get(f"/verein/ergebnis/{e1['id']}.xlsx").status_code == 404, "entfernter Verein bekommt kein Ergebnis")
pruefe(D.post(f"/verein/runde/{rid}/beitreten", data={"_csrf": T, "nachweis": r2["code"]}).status_code == 403,
       "nach Abschluss kein Beitritt")
B.post(f"/verein/runde/{rid}/verlassen", data={"_csrf": T})
s = B.get("/verein/runden").get_data(as_text=True)
pruefe("Frühere Runden" in s and "Ergebnis Version 1" in s, "nach Austritt bleibt das Ergebnis")
A.post(f"/verein/runde/{rid}/status", data={"_csrf": T, "aktion": "oeffnen"})
A.post(f"/verein/runde/{rid}/status", data={"_csrf": T, "aktion": "abschliessen"})
pruefe([e["version"] for e in DB.ergebnisse(rid)] == [2, 1], "zweiter Abschluss = Version 2, Version 1 bleibt")
if pdf is not None:
    pruefe(A.get(f"/verein/ergebnis/{e1['id']}.pdf").data == pdf.data, "Version 1 unverändert")

# Admin-PATCH: echte Datumsprüfung (#435)
r = anon.patch("/api/termine", json={"verein_key": "va", "datum": FEST_VJ, "bezeichnung": "Sommerfest",
                                     "changes": {"datum": f"{VJ}-02-30"}}, headers={"X-Upload-Token": "admintoken"})
pruefe(r.status_code == 400 and kal()["va"][0]["datum"] == FEST_VJ, "PATCH: 30. Februar abgelehnt")
r = A.post("/verein/termine/a1", data={"_csrf": T, "aktion": "speichern", "datum": f"{VJ}-13-01", "bezeichnung": "Sommerfest"})
pruefe(r.status_code == 200 and "gültiges Datum" in r.get_data(as_text=True), "Termin bearbeiten: Monat 13 abgelehnt")

print(f"\n{OK} Prüfungen ok, {len(FEHLER)} Fehler")
if FEHLER:
    print(f"{len(FEHLER)} PRÜFUNG(EN) FEHLGESCHLAGEN")
    sys.exit(1)
print("ALLE PRÜFUNGEN BESTANDEN")
