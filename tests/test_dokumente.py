#!/usr/bin/env python3
"""Offline-Abnahme Tab „Dokumente“ im Vereinsbereich (ADR-029, v1.78).

    python3 tests/test_dokumente.py

Wie `test_planung.py`: Temp-Verzeichnis, eigene DB und `vereinstermine.json`, Ablage in TMP, keine Netz-/Mail-/
Telegram-Zugriffe. PDF/Word werden übersprungen, wenn fpdf2/python-docx fehlen (volle Abdeckung mit
`~/.venvs/vko-jahresplanung/bin/python`).
"""
from __future__ import annotations

import importlib
import io
import json
import os
import re
import sys
import tempfile
import types
import zipfile
from pathlib import Path
from urllib.parse import unquote_plus as unquote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="vko-dokumente-"))

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

FEHLER: list[str] = []
OK = 0


def pruefe(bedingung, text):
    global OK
    if bedingung:
        OK += 1
    else:
        FEHLER.append(text)
        print("  ✗", text)


import shared.vk_db as vk_db  # noqa: E402
import shared.kalender_store as kstore  # noqa: E402
import shared.dokumente_store as S  # noqa: E402

vk_db.DB_FILE = TMP / "vk_accounts.db"
VT = TMP / "vereinstermine.json"
kstore.VEREINSTERMINE_FILE = VT
S.ORDNER = TMP / "vereinsdokumente"
VT.write_text(json.dumps({"_labels": {"va": "FF Verein A", "vb": "Schützen B", "alt": "Alter Key"}, "_meta": {}}))

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

import shared.dokumente_db as D  # noqa: E402

app = webhook.app
app.config["TESTING"] = True


def konto(name, key, email):
    import bcrypt
    with vk_db.db_conn() as c:
        vid = c.execute("INSERT INTO vereine_accounts (verein_key, verein_name, status) VALUES (?,?, 'aktiv') RETURNING id",
                        (key, name)).fetchone()["id"]
        uid = c.execute("INSERT INTO vk_users (email, password_hash, verein_id, role, email_verified, aktiv) "
                        "VALUES (?,?,?, 'admin', 1, 1) RETURNING id",
                        (email, bcrypt.hashpw(b"geheim123", bcrypt.gensalt(4)).decode(), vid)).fetchone()["id"]
    return vid, uid


def mitglied(vid, email):
    import bcrypt
    with vk_db.db_conn() as c:
        return c.execute("INSERT INTO vk_users (email, password_hash, verein_id, role, email_verified, aktiv) "
                         "VALUES (?,?,?, 'member', 1, 1) RETURNING id",
                         (email, bcrypt.hashpw(b"geheim123", bcrypt.gensalt(4)).decode(), vid)).fetchone()["id"]


def client(uid):
    c = app.test_client()
    c.set_cookie("vk_session", vk_db.create_session(uid))
    with c.session_transaction() as s:
        s["_csrf"] = "tok"
    return c


def docx_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document/>")
    return buf.getvalue()


def odt_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr("content.xml", "<office:document-content/>")
    return buf.getvalue()


PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
T = "tok"
vid_a, uid_a = konto("FF Verein A", "va", "a@example.org")
vid_b, uid_b = konto("Schützen B", "vb", "b@example.org")
uid_am = mitglied(vid_a, "mitglied-a@example.org")
A, B, AM = client(uid_a), client(uid_b), client(uid_am)
anon = app.test_client()


def hochladen(c, data, name="Protokoll JHV.pdf", **felder):
    return c.post("/verein/dokumente", data={"_csrf": T, "kategorie": "protokoll", "datum": "2026-03-14",
                                             "titel": "", **felder, "datei": (io.BytesIO(data), name)},
                  content_type="multipart/form-data")


# ── 1. Formaterkennung ───────────────────────────────────────────────────────
print("1. Formaterkennung")
pruefe(S.erkenne(PDF) == "pdf", "PDF")
pruefe(S.erkenne(b"\xff\xd8\xff\xe0" + b"0" * 20) == "jpg", "JPG")
pruefe(S.erkenne(b"\x89PNG\r\n\x1a\n" + b"0" * 20) == "png", "PNG")
pruefe(S.erkenne(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp", "WebP")
pruefe(S.erkenne(docx_bytes()) == "docx", "Word")
pruefe(S.erkenne(odt_bytes()) == "odt", "LibreOffice Text")
pruefe(S.erkenne(b"MZ\x90\x00" + b"0" * 50) is None, "Programmdatei (.exe) abgelehnt")
pruefe(S.erkenne(b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>") is None, "SVG abgelehnt")
pruefe(S.erkenne(b"<html><script>alert(1)</script>") is None, "HTML abgelehnt")
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("x.js", "alert(1)")
pruefe(S.erkenne(buf.getvalue()) is None, "beliebiges ZIP abgelehnt")
pruefe(S.sicherer_name("../../etc/passwd", "pdf") == "passwd.pdf", "Pfad aus dem Dateinamen entfernt")
pruefe(S.sicherer_name("Protokoll.exe", "pdf") == "Protokoll.pdf", "echte Endung statt angegebener")
pruefe(S.pfad("../vk_accounts.db") is None and S.pfad("") is None, "pfad(): nur selbst erzeugte Namen")

# ── 2. Anmeldung und Tab ─────────────────────────────────────────────────────
print("2. Anmeldung und Tab")
pruefe(all(anon.get(u).status_code == 302 for u in ("/verein/dokumente", "/verein/dokumente/neu",
                                                    "/verein/dokumente/1", "/verein/dokumente/1/datei",
                                                    "/verein/dokumente/1.pdf")), "alles nur angemeldet")
s = A.get("/verein/dokumente").get_data(as_text=True)
tabs = re.findall(r'class="hdr-pill[^"]*"[^>]*>(?:<svg.*?</svg>)?([^<]+)</', s)
pruefe(tabs == ["Termine", "Planungsrunden", "Dokumente", "Einstellungen", "Abmelden"], f"Tabs, war {tabs}")
pruefe('href="/verein/dokumente" aria-current="page"' in s, "Tab Dokumente aktiv")
pruefe("Nur für euren Verein sichtbar" in s and "Noch keine." in s, "Hinweis + leere Liste")
pruefe("Protokoll schreiben" in s and 'data-klappe="hochladen"' in s, "Admin: Schreiben + Hochladen")
s = AM.get("/verein/dokumente").get_data(as_text=True)
pruefe("Protokoll schreiben" not in s and "legen die Vereinsadmins an" in s, "Mitglied: keine Knöpfe")

# ── 3. Hochladen ─────────────────────────────────────────────────────────────
print("3. Hochladen")
r = hochladen(A, PDF)
pruefe(r.status_code == 302 and "abgelegt" in unquote(r.headers["Location"]), "PDF hochgeladen")
docs = D.liste("va")
pruefe(len(docs) == 1 and docs[0]["titel"] == "Protokoll JHV" and docs[0]["datei_typ"] == "pdf"
       and docs[0]["groesse"] == len(PDF), f"Zeile angelegt, Titel aus Dateiname, war {docs}")
d1 = docs[0]
pruefe((S.ORDNER / d1["datei_pfad"]).read_bytes() == PDF and re.fullmatch(r"[0-9a-f]{32}\.pdf", d1["datei_pfad"]),
       "Datei unter UUID-Namen abgelegt")
pruefe(oct((S.ORDNER / d1["datei_pfad"]).stat().st_mode)[-3:] == "640", "Datei nur für Dienstbenutzer/Gruppe lesbar")
r = hochladen(A, b"MZ\x90\x00" + b"0" * 50, "virus.pdf")
pruefe(r.status_code == 302 and "Format" in unquote(r.headers["Location"]) and len(D.liste("va")) == 1,
       "getarnte .exe abgelehnt")
r = hochladen(A, PDF, kategorie="geheim")
pruefe("Kategorie" in unquote(r.headers["Location"]) and len(D.liste("va")) == 1, "unbekannte Kategorie abgelehnt")
r = hochladen(A, PDF, datum="2026-02-30")
pruefe("gültiges Datum" in unquote(r.headers["Location"]), "30. Februar abgelehnt")
r = hochladen(AM, PDF)
pruefe(r.status_code == 403 and len(D.liste("va")) == 1, "Mitglied darf nicht hochladen")
r = hochladen(A, docx_bytes(), "Satzung.docx", kategorie="satzung", titel="Satzung 2024", datum="")
pruefe(r.status_code == 302 and [d["titel"] for d in D.liste("va", "Satzung")] == ["Satzung 2024"], "Word als Satzung")
alt_max = S.MAX_BYTES
S.MAX_BYTES = 100
r = hochladen(A, PDF + b"0" * 200)
pruefe("zu groß" in unquote(r.headers["Location"]), "Datei zu groß")
S.MAX_BYTES = alt_max
alt_verein = S.MAX_VEREIN
S.MAX_VEREIN = D.belegung("va") + 10
r = hochladen(A, PDF)
pruefe("Speicher ist voll" in unquote(r.headers["Location"]), "Speichergrenze je Verein")
S.MAX_VEREIN = alt_verein

# Liste, Ansicht, Download
s = A.get("/verein/dokumente").get_data(as_text=True)
pruefe("Protokoll JHV" in s and "Satzung 2024" in s and "14.03.2026" in s, "Liste zeigt beide")
s = A.get("/verein/dokumente?q=jhv").get_data(as_text=True)
pruefe("Protokoll JHV" in s and "Satzung 2024" not in s, "Suche im Titel")
s = A.get("/verein/dokumente?q=%25").get_data(as_text=True)
pruefe("Keine Treffer" in s, "Suche: % ist kein Platzhalter")
r = A.get(f"/verein/dokumente/{d1['id']}/datei")
pruefe(r.status_code == 200 and r.data == PDF and r.mimetype == "application/pdf"
       and r.headers["Cache-Control"] == "private, no-store" and r.headers["X-Content-Type-Options"] == "nosniff"
       and "inline" in r.headers.get("Content-Disposition", "inline"), "PDF öffnen (im Browser, ohne Cache)")
r.close()
r = AM.get(f"/verein/dokumente/{d1['id']}/datei?laden=1")
pruefe(r.status_code == 200 and "attachment" in r.headers["Content-Disposition"], "Mitglied: herunterladen")
r.close()
satzung = D.liste("va", "Satzung")[0]
r = A.get(f"/verein/dokumente/{satzung['id']}/datei")
pruefe("attachment" in r.headers["Content-Disposition"] and "Satzung.docx" in r.headers["Content-Disposition"],
       "Word wird immer heruntergeladen, mit Originalname")
r.close()

# Fremder Verein
pruefe(B.get(f"/verein/dokumente/{d1['id']}").status_code == 404, "fremder Verein: Ansicht 404")
pruefe(B.get(f"/verein/dokumente/{d1['id']}/datei").status_code == 404, "fremder Verein: Datei 404")
pruefe(B.post(f"/verein/dokumente/{d1['id']}/loeschen", data={"_csrf": T}).status_code == 404
       and D.hole(d1["id"], "va"), "fremder Verein: Löschen 404")
pruefe("Protokoll JHV" not in B.get("/verein/dokumente").get_data(as_text=True), "fremder Verein: nicht in der Liste")
pruefe("Protokoll JHV" not in anon.get("/api/termine").get_data(as_text=True), "nicht in /api/termine")

# Ändern + Datei ersetzen
pdf2 = PDF + b"% neu\n"
r = A.post(f"/verein/dokumente/{d1['id']}", data={"_csrf": T, "kategorie": "protokoll", "datum": "2026-03-15",
                                                  "titel": "JHV 2026", "datei": (io.BytesIO(pdf2), "neu.pdf")},
           content_type="multipart/form-data")
d1n = D.hole(d1["id"], "va")
pruefe(r.status_code == 302 and d1n["titel"] == "JHV 2026" and d1n["datum"] == "2026-03-15"
       and d1n["groesse"] == len(pdf2) and d1n["datei_name"] == "neu.pdf", "Ändern mit neuer Datei")
pruefe(not (S.ORDNER / d1["datei_pfad"]).exists() and (S.ORDNER / d1n["datei_pfad"]).read_bytes() == pdf2,
       "alte Datei entfernt, neue abgelegt")
r = AM.post(f"/verein/dokumente/{d1['id']}", data={"_csrf": T, "kategorie": "protokoll", "titel": "x"})
pruefe(r.status_code == 403 and D.hole(d1["id"], "va")["titel"] == "JHV 2026", "Mitglied darf nicht ändern")

# ── 4. Schreiben (Protokoll-Formular) ────────────────────────────────────────
print("4. Protokoll schreiben")
s = A.get("/verein/dokumente/neu?kat=protokoll").get_data(as_text=True)
pruefe("Tagesordnung" in s and 'name="top_titel"' in s and "Mitgliederversammlung" in s, "Formular mit Tagesordnung")
pruefe(AM.get("/verein/dokumente/neu").status_code == 403, "Mitglied: kein Formular")
FORM = {"_csrf": T, "kategorie": "protokoll", "datum": "2026-04-02", "titel": "", "sitzungsart": "Vorstandssitzung",
        "ort": "Gasthaus Huber", "beginn": "19:30", "ende": "21:45", "leitung": "Anna Maier", "protokoll": "Jörg Ößer",
        "anwesende": "7 von 9", "entschuldigt": "Max Huber",
        "top_titel": ["Begrüßung", "", "Kasse – Prüfung"], "top_text": ["Alle da.", "", "Prüfung ohne Beanstandung"],
        "top_beschluss": ["", "", "Entlastung des Kassiers"], "top_ja": ["", "", "6"], "top_nein": ["", "", "0"],
        "top_enthaltung": ["", "", "1"], "text": "Nächste Sitzung im Mai."}
r = A.post("/verein/dokumente/neu", data=FORM)
pid = int(re.search(r"/verein/dokumente/(\d+)", r.headers["Location"]).group(1))
p = D.hole(pid, "va")
pruefe(r.status_code == 302 and p["art"] == "formular" and p["titel"] == "Protokoll Vorstandssitzung 02.04.2026",
       f"Protokoll gespeichert, Titel gebildet, war {p and p['titel']}")
pruefe([t["titel"] for t in p["inhalt"]["tops"]] == ["Begrüßung", "Kasse – Prüfung"], "leerer TOP übersprungen")
pruefe(p["inhalt"]["tops"][1]["ja"] == "6" and p["inhalt"]["leitung"] == "Anna Maier", "Felder übernommen")
s = A.get(f"/verein/dokumente/{pid}").get_data(as_text=True)
pruefe("TOP 2: Kasse – Prüfung" in s and "Ja 6 · Nein 0 · Enthaltung 1" in s and "Gasthaus Huber" in s
       and "19:30 – 21:45 Uhr" in s, "Ansicht mit Kopf, TOPs, Abstimmung")
pruefe("Bearbeiten" in s and "Endgültig löschen" in s, "Admin: Bearbeiten/Löschen")
s = AM.get(f"/verein/dokumente/{pid}").get_data(as_text=True)
pruefe("TOP 2" in s and "bearbeiten=1" not in s and "Endgültig löschen" not in s, "Mitglied: nur lesen")
s = AM.get(f"/verein/dokumente/{pid}?bearbeiten=1").get_data(as_text=True)
pruefe('name="top_titel"' not in s, "Mitglied: kein Bearbeiten-Formular per URL")
r = A.post("/verein/dokumente/neu", data={**FORM, "datum": ""})
pruefe(r.status_code == 400 and "Datum der Sitzung" in r.get_data(as_text=True)
       and 'value="Gasthaus Huber"' in r.get_data(as_text=True), "Protokoll ohne Datum: Fehler, Eingaben bleiben")
r = A.post("/verein/dokumente/neu", data={**FORM, "top_ja": ["", "", "viele"]})
pruefe(r.status_code == 400 and "nur Zahlen" in r.get_data(as_text=True), "Abstimmung nur Zahlen")
r = A.post("/verein/dokumente/neu", data={**FORM, "titel": "<script>alert(1)</script>"})
s = A.get(r.headers["Location"]).get_data(as_text=True)
pruefe("<script>alert(1)" not in s and "&lt;script&gt;" in s, "Titel wird escapt")
D.loeschen(int(re.search(r"/(\d+)", r.headers["Location"]).group(1)), "va")

# Bearbeiten
s = A.get(f"/verein/dokumente/{pid}?bearbeiten=1").get_data(as_text=True)
pruefe('value="Kasse – Prüfung"' in s and "Zurück ohne Speichern" in s, "Bearbeiten-Formular vorbefüllt")
r = A.post(f"/verein/dokumente/{pid}", data={**FORM, "titel": "Vorstand April", "top_titel": ["Nur einer"],
                                              "top_text": [""], "top_beschluss": [""], "top_ja": [""], "top_nein": [""],
                                              "top_enthaltung": [""]})
p = D.hole(pid, "va")
pruefe(r.status_code == 302 and p["titel"] == "Vorstand April" and len(p["inhalt"]["tops"]) == 1, "Protokoll geändert")

# Satzung als Text
r = A.post("/verein/dokumente/neu", data={"_csrf": T, "kategorie": "satzung", "datum": "", "titel": "",
                                          "text": "§ 1 Name\nDer Verein heißt …", "ort": "ignoriert"})
sid = int(re.search(r"/(\d+)", r.headers["Location"]).group(1))
sz = D.hole(sid, "va")
pruefe(sz["titel"] == "Satzung" and sz["inhalt"] == {"text": "§ 1 Name\nDer Verein heißt …"}, f"Satzung als Text, war {sz}")

# Export
try:
    import fpdf  # noqa: F401
    r = A.get(f"/verein/dokumente/{pid}.pdf")
    pruefe(r.status_code == 200 and r.data[:4] == b"%PDF" and r.headers["Cache-Control"] == "private, no-store"
           and "Vorstand_April.pdf" in r.headers["Content-Disposition"], "Protokoll als PDF")
    pruefe(A.get(f"/verein/dokumente/{pid}.pdf").data == r.data, "gleicher Stand = gleiches PDF")
    pruefe(AM.get(f"/verein/dokumente/{pid}.pdf").status_code == 200, "Mitglied: PDF")
    pruefe(A.get(f"/verein/dokumente/{sid}.pdf").status_code == 200, "Satzung als PDF")
except ImportError:
    print("  (fpdf2 fehlt – PDF übersprungen)")
try:
    import docx  # noqa: F401
    r = A.get(f"/verein/dokumente/{pid}.docx")
    with zipfile.ZipFile(io.BytesIO(r.data)) as z:
        xml = z.read("word/document.xml").decode()
    pruefe(r.status_code == 200 and "Jörg Ößer" in xml and "TOP 1: Nur einer" in xml, "Protokoll als Word mit Umlauten")
except ImportError:
    print("  (python-docx fehlt – Word übersprungen)")
pruefe(A.get(f"/verein/dokumente/{d1['id']}.pdf").status_code == 404, "Datei-Dokument hat keinen PDF-Export")
pruefe(A.get(f"/verein/dokumente/{pid}.exe").status_code == 404, "unbekanntes Format 404")
pruefe(B.get(f"/verein/dokumente/{pid}.pdf").status_code == 404, "fremder Verein: kein Export")

# ── 5. Löschen, Audit, Statistik ─────────────────────────────────────────────
print("5. Löschen, Audit, Statistik")
with vk_db.db_conn() as c:
    aktionen = [r["aktion"] for r in c.execute("SELECT aktion FROM vk_audit WHERE termin_id LIKE 'dok_%'")]
pruefe("dokument_neu" in aktionen and "dokument_geaendert" in aktionen, f"Audit-Einträge, war {set(aktionen)}")
pfad_satz = satzung["datei_pfad"]
r = AM.post(f"/verein/dokumente/{satzung['id']}/loeschen", data={"_csrf": T})
pruefe(r.status_code == 403 and D.hole(satzung["id"], "va"), "Mitglied darf nicht löschen")
r = A.post(f"/verein/dokumente/{satzung['id']}/loeschen", data={"_csrf": T})
pruefe(r.status_code == 302 and not D.hole(satzung["id"], "va") and not (S.ORDNER / pfad_satz).exists(),
       "Löschen entfernt Zeile und Datei")
pruefe(A.get(f"/verein/dokumente/{satzung['id']}").status_code == 404, "gelöschtes Dokument 404")
st = anon.get("/api/admin/stats", headers={"X-Upload-Token": "admintoken"}).get_json()
pruefe(st and st["dokumente"]["anzahl"] == len(D.liste("va")) and st["dokumente"]["vereine"] == 1,
       f"Admin-Statistik: nur Zahlen, war {st and st.get('dokumente')}")
pruefe("JHV" not in json.dumps(st), "Admin-Statistik ohne Titel")

# Key-Übertragung: Dokumente wandern mit
D.neu("alt", uid_a, "sonstiges", "Altlast", "", "formular", inhalt={"text": "x"})
kstore.uebertrage_key("alt", "vb")
pruefe([d["titel"] for d in D.liste("vb")] == ["Altlast"] and not D.liste("alt"), "Key-Übertragung nimmt Dokumente mit")

# Konto löschen: Dokumente und Dateien weg, auch wenn die Termine bleiben
hochladen(A, PDF, "noch.pdf")
dateien = [d["datei_pfad"] for d in D.liste("va") if d["datei_pfad"]]
r = anon.delete(f"/api/admin/verein/{vid_a}", json={"delete_termine": False}, headers={"X-Upload-Token": "admintoken"})
pruefe(r.status_code == 200 and r.get_json()["geloescht_dokumente"] == 4 and not D.liste("va"),
       f"Konto gelöscht → Dokumente weg, war {r.get_json()}")
pruefe(dateien and not any((S.ORDNER / n).exists() for n in dateien), "… und ihre Dateien")
pruefe([d["titel"] for d in D.liste("vb")] == ["Altlast"], "anderer Verein unberührt")

# Rechtstexte
s = anon.get("/verein/datenschutz").get_data(as_text=True)
pruefe("Vereinsdokumente" in s and "nicht öffentlich" in s, "Datenschutzerklärung nennt die Dokumente")
s = anon.get("/verein/nutzungsbedingungen").get_data(as_text=True)
pruefe("Vereinsdokumente" in s and "in seinem Auftrag" in s, "Nutzungsbedingungen nennen die Dokumente")

print(f"\n{OK} Prüfungen ok, {len(FEHLER)} Fehler")
if FEHLER:
    print(f"{len(FEHLER)} PRÜFUNG(EN) FEHLGESCHLAGEN")
    sys.exit(1)
print("ALLE PRÜFUNGEN BESTANDEN")
