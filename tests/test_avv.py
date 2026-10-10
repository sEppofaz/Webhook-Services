#!/usr/bin/env python3
"""Offline-Abnahme Freischaltung des Dokumentenbereichs per AV-Vertrag, Export, Zugriffsprotokoll, Vorstand
(ADR-032, v1.83). Aufbau wie `test_dokumente.py`.

    python3 tests/test_avv.py
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
TMP = Path(tempfile.mkdtemp(prefix="vko-avv-"))

for k in ("CLAUDE_API_KEY", "DROPBOX_REFRESH_TOKEN", "DROPBOX_APP_KEY", "DROPBOX_APP_SECRET"):
    os.environ.setdefault(k, "test")
os.environ["FLASK_SECRET_KEY"] = "test-secret"
os.environ["UPLOAD_TOKEN"] = "admintoken"
os.environ["TELEGRAM_WEBHOOK_SECRET"] = "tgsecret"
os.environ["CHAT_ID"] = "4711"
os.environ["VKO_COOKIE_INSECURE"] = "1"
for k in ("TOKEN", "BREVO_SMTP_USER", "BREVO_SMTP_KEY", "MAILJET_API_KEY", "MAILJET_SECRET_KEY", "KALENDER_BOT_TOKEN"):
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




def konto(name, key, email, avv=None):
    import bcrypt
    with vk_db.db_conn() as c:
        vid = c.execute("INSERT INTO vereine_accounts (verein_key, verein_name, status, avv_fassung) VALUES (?,?, 'aktiv', ?)"
                        " RETURNING id", (key, name, avv)).fetchone()["id"]
        uid = c.execute("INSERT INTO vk_users (email, password_hash, verein_id, role, email_verified, aktiv, ds_fassung)"
                        " VALUES (?,?,?, 'admin', 1, 1, ?) RETURNING id",
                        (email, bcrypt.hashpw(b"geheim123", bcrypt.gensalt(4)).decode(), vid, vk_db.DS_FASSUNG)).fetchone()["id"]
    return vid, uid


def mitglied(vid, email):
    with vk_db.db_conn() as c:
        return c.execute("INSERT INTO vk_users (email, password_hash, verein_id, role, email_verified, aktiv, ds_fassung)"
                         " VALUES (?, 'x', ?, 'member', 1, 1, ?) RETURNING id", (email, vid, vk_db.DS_FASSUNG)).fetchone()["id"]


def client(uid=None):
    c = app.test_client()
    if uid:
        c.set_cookie("vk_session", vk_db.create_session(uid))
    with c.session_transaction() as s:
        s["_csrf"] = "tok"
    return c


T = "tok"
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
vid_a, uid_a = konto("FF Verein A", "va", "a@example.org")                 # nicht freigeschaltet
uid_am = mitglied(vid_a, "m@example.org")
A, AM = client(uid_a), client(uid_am)

print("Ohne Freischaltung")
s = A.get("/verein/dokumente").get_data(as_text=True)
pruefe("Dokumentenbereich freischalten" in s and 'href="/verein/avv"' in s, "Admin sieht Freischalt-Seite mit AVV-Link")
s = AM.get("/verein/dokumente").get_data(as_text=True)
pruefe("Freischalten kann nur ein Vereinsadmin" in s and "Dokumentenbereich freischalten" not in s, "Mitglied: Hinweis, kein Knopf")
for url in ("/verein/dokumente/neu", "/verein/dokumente/listen"):
    r = A.get(url)
    pruefe(r.status_code == 302 and r.headers["Location"].endswith("/verein/dokumente/freischalten"), f"{url} → Freischalten")
r = A.post("/verein/dokumente", data={"_csrf": T, "kategorie": "protokoll", "datum": "", "titel": "x",
                                      "datei": (io.BytesIO(PDF), "a.pdf")}, content_type="multipart/form-data")
pruefe(r.status_code == 302 and r.headers["Location"].endswith("/freischalten") and not D.liste("va"), "Upload gesperrt")
s = A.get("/verein/termine").get_data(as_text=True)
pruefe("Neu: Dokumentenbereich" in s, "Hinweis auf Termine für Admin")
pruefe("Neu: Dokumentenbereich" not in AM.get("/verein/termine").get_data(as_text=True), "kein Hinweis für Mitglieder")
A.post("/verein/hinweis/dokumente", data={"_csrf": T})
pruefe("Neu: Dokumentenbereich" not in A.get("/verein/termine").get_data(as_text=True), "Hinweis weggeklickt")
s = A.get("/verein/einstellungen").get_data(as_text=True)
pruefe("Nicht freigeschaltet" in s, "Einstellungen zeigen Status")
s = client().get("/verein/avv").get_data(as_text=True)
pruefe("Art. 28" in s and "Anlage 1" in s and "Anlage 2" in s and "Hetzner" in s and "Mailjet" in s
       and "nicht zusätzlich verschlüsselt" in s and vk_db.AVV_FASSUNG in s, "AVV-Seite mit TOMs und Unterauftragnehmern")

print("Freischalten")
r = AM.post("/verein/dokumente/freischalten", data={"_csrf": T, "avv": "on"})
pruefe(r.status_code == 403, "Mitglied kann nicht freischalten")
r = A.post("/verein/dokumente/freischalten", data={"_csrf": T})
pruefe(r.status_code == 400 and "Bitte bestätigen" in r.get_data(as_text=True), "ohne Häkchen abgelehnt")
r = A.post("/verein/dokumente/freischalten", data={"_csrf": T, "avv": "on"})
with vk_db.db_conn() as c:
    v = dict(c.execute("SELECT avv_fassung, avv_am, avv_user FROM vereine_accounts WHERE id = ?", (vid_a,)).fetchone())
    au = c.execute("SELECT termin_id FROM vk_audit WHERE aktion = 'avv_abgeschlossen' AND verein_key = 'va'").fetchall()
pruefe(r.status_code == 302 and v["avv_fassung"] == vk_db.AVV_FASSUNG and v["avv_am"] and v["avv_user"] == uid_a,
       f"Fassung, Zeit und Konto gespeichert, war {v}")
pruefe(len(au) == 1 and au[0]["termin_id"] == vk_db.AVV_FASSUNG, "Audit avv_abgeschlossen")
s = A.get("/verein/dokumente").get_data(as_text=True)
pruefe("Protokoll schreiben" in s and "nicht freigeschaltet" not in s, "freigeschaltet: Liste mit Aktionen")
pruefe("Freigeschaltet" in A.get("/verein/einstellungen").get_data(as_text=True), "Einstellungen: freigeschaltet")

print("Vorstand und Sichtbarkeit")
r = A.post("/verein/dokumente", data={"_csrf": T, "kategorie": "protokoll", "datum": "2026-05-01", "titel": "Vorstand intern",
                                      "sichtbar": "vorstand", "datei": (io.BytesIO(PDF), "v.pdf")},
           content_type="multipart/form-data")
A.post("/verein/dokumente/neu", data={"_csrf": T, "kategorie": "sonstiges", "titel": "Für alle", "datum": "", "text": "Hallo",
                                      "sichtbar": "alle"})
dv = [d for d in D.liste("va") if d["titel"] == "Vorstand intern"][0]
pruefe(dv["sichtbar"] == "vorstand", "Sichtbarkeit gespeichert")
s = AM.get("/verein/dokumente").get_data(as_text=True)
pruefe("Für alle" in s and "Vorstand intern" not in s, "Mitglied sieht Dokument „nur Vorstand“ nicht in der Liste")
for url in (f"/verein/dokumente/{dv['id']}", f"/verein/dokumente/{dv['id']}/datei"):
    pruefe(AM.get(url).status_code == 404, f"Mitglied: {url} → 404")
pruefe(AM.get("/verein/dokumente?q=intern").get_data(as_text=True).count("Vorstand intern") == 0, "… auch nicht per Suche")
pruefe(AM.post("/verein/mitglieder", data={"_csrf": T, "aktion": "vorstand", "member_id": uid_am, "wert": "1"}).status_code == 302,
       "Mitglied ruft Mitgliederseite auf → Umleitung")
with vk_db.db_conn() as c:
    pruefe(c.execute("SELECT vorstand FROM vk_users WHERE id = ?", (uid_am,)).fetchone()[0] == 0,
           "Mitglied kann sich nicht selbst zum Vorstand machen")
r = A.post("/verein/mitglieder", data={"_csrf": T, "aktion": "vorstand", "member_id": uid_am, "wert": "1"})
s = A.get("/verein/mitglieder").get_data(as_text=True)
pruefe("Vorstand entfernen" in s and "Vorstand ·" in s, "Admin markiert Vorstand, Seite zeigt es")
s = AM.get("/verein/dokumente").get_data(as_text=True)
pruefe("Vorstand intern" in s and "nur Vorstand" in s, "Vorstand sieht das Dokument sofort (ohne neu anmelden)")
pruefe(AM.get(f"/verein/dokumente/{dv['id']}/datei").status_code == 200, "Vorstand kann die Datei öffnen")
pruefe(AM.get("/verein/dokumente/neu").status_code == 403, "Vorstand schreibt nicht")
A.post("/verein/mitglieder", data={"_csrf": T, "aktion": "vorstand", "member_id": uid_am, "wert": "0"})
pruefe(AM.get(f"/verein/dokumente/{dv['id']}").status_code == 404, "Markierung entfernt → wieder 404")
vid_b, uid_b = konto("Verein B", "vb", "b@example.org", avv=vk_db.AVV_FASSUNG)
uid_bm = mitglied(vid_b, "bm@example.org")
client(uid_b).post("/verein/mitglieder", data={"_csrf": T, "aktion": "vorstand", "member_id": uid_am, "wert": "1"})
with vk_db.db_conn() as c:
    pruefe(c.execute("SELECT vorstand FROM vk_users WHERE id = ?", (uid_am,)).fetchone()[0] == 0,
           "fremder Verein kann keine Vorstands-Markierung setzen")
    pruefe(c.execute("SELECT COUNT(*) FROM vk_audit WHERE aktion IN ('vorstand_gesetzt','vorstand_entfernt')").fetchone()[0] == 2,
           "Audit vorstand_gesetzt/entfernt")

print("Zugriffsprotokoll")
AM.get("/verein/dokumente")
fa = [d for d in D.liste("va") if d["titel"] == "Für alle"][0]
AM.get(f"/verein/dokumente/{fa['id']}")
AM.get(f"/verein/dokumente/{dv['id']}")          # 404 – kein Eintrag
pruefe(AM.get("/verein/dokumente/verlauf").status_code == 403, "Mitglied: kein Protokoll")
s = A.get("/verein/dokumente/verlauf").get_data(as_text=True)
pruefe("m@example.org" in s and "angesehen" in s and "Für alle" in s and "AV-Vertrag abgeschlossen" in s, "Protokoll zeigt Ansicht und AVV")
with vk_db.db_conn() as c:
    n = c.execute("SELECT COUNT(*) FROM vk_audit WHERE aktion = 'dokument_angesehen' AND termin_id = ?",
                  (f"dok_{dv['id']}",)).fetchone()[0]
    pruefe(n == 1, f"Fremd-404 erzeugt keinen Eintrag (nur der Vorstands-Abruf), war {n}")
    c.execute("INSERT INTO vk_audit (aktion, termin_id, verein_key, user_id, timestamp) VALUES"
              " ('dokument_angesehen', 'dok_1', 'va', ?, '2024-01-01 10:00:00')", (uid_a,))
    c.execute("INSERT INTO vk_audit (aktion, termin_id, verein_key, user_id, timestamp) VALUES"
              " ('dokument_angesehen', 'dok_1', 'vb', ?, '2024-01-01 10:00:00')", (uid_b,))
    c.execute("INSERT INTO vk_audit (aktion, termin_id, verein_key, user_id, timestamp) VALUES"
              " ('avv_abgeschlossen', '2024-01', 'vb', ?, '2024-01-01 10:00:00')", (uid_b,))
import services.verein.dokumente as VD0
VD0._aufgeraeumt["tag"] = ""
client().get("/verein/datenschutz")                  # irgendein Aufruf → tägliches Aufräumen für alle Vereine
with vk_db.db_conn() as c:
    alt = [tuple(r) for r in c.execute("SELECT verein_key, aktion FROM vk_audit WHERE timestamp < '2025-01-01'")]
pruefe(alt == [("vb", "avv_abgeschlossen")], f"alte Einträge aller Vereine gelöscht, AVV-Nachweis bleibt, war {alt}")
# Ansicht lässt sich nicht per URL-Parameter verstecken
with vk_db.db_conn() as c:
    vorher = c.execute("SELECT COUNT(*) FROM vk_audit WHERE aktion = 'dokument_angesehen' AND user_id = ?", (uid_am,)).fetchone()[0]
AM.get(f"/verein/dokumente/{fa['id']}?meldung=x")
with vk_db.db_conn() as c:
    nachher = c.execute("SELECT COUNT(*) FROM vk_audit WHERE aktion = 'dokument_angesehen' AND user_id = ?", (uid_am,)).fetchone()[0]
pruefe(nachher == vorher + 1, "Ansicht mit ?meldung= wird trotzdem protokolliert")
r = A.post(f"/verein/dokumente/{fa['id']}", data={"_csrf": T, "kategorie": "sonstiges", "titel": "Für alle", "datum": "",
                                               "text": "Hallo neu", "sichtbar": "alle"})
A.get(r.headers["Location"])
with vk_db.db_conn() as c:
    letzte = c.execute("SELECT aktion FROM vk_audit WHERE termin_id = ? ORDER BY id DESC LIMIT 1", (f"dok_{fa['id']}",)).fetchone()[0]
pruefe(letzte == "dokument_geaendert", f"eigene Ansicht direkt nach dem Speichern zählt nicht, war {letzte}")

print("Export ZIP")
pruefe(AM.get("/verein/dokumente/export.zip").status_code == 403, "Mitglied: kein Export")
r = A.get("/verein/dokumente/export.zip")
z = zipfile.ZipFile(io.BytesIO(r.data))
namen = z.namelist()
pruefe(r.status_code == 200 and r.headers["Cache-Control"] == "private, no-store" and "uebersicht.csv" in namen,
       f"ZIP mit Übersicht, war {namen}")
pruefe(any(n.startswith("Protokolle/") and n.endswith(".pdf") for n in namen)
       and any(n.startswith("Sonstiges/") for n in namen), "Dateien nach Kategorie, geschriebenes Dokument dabei")
pruefe("Vorstand intern" in z.read("uebersicht.csv").decode("utf-8-sig"), "Export enthält auch „nur Vorstand“")
with vk_db.db_conn() as c:
    pruefe(c.execute("SELECT 1 FROM vk_audit WHERE aktion = 'dokumente_export' AND verein_key = 'va'").fetchone(), "Audit Export")

print("Bestand und neue Fassung")
import shared.vk_db as V
import services.verein.dokumente as VD
import services.verein.planung as VP
alt_f = V.AVV_FASSUNG
VD.AVV_FASSUNG = VP.AVV_FASSUNG = "2099-01"
s = A.get("/verein/dokumente").get_data(as_text=True)
pruefe("Für alle" in s and "nicht freigeschaltet" in s and "Protokoll schreiben" not in s, "neue Fassung: Bestand lesbar, Anlegen weg")
pruefe(A.get(f"/verein/dokumente/{fa['id']}.pdf").status_code in (200, 404), "Bestand weiter abrufbar")
pruefe(A.get("/verein/dokumente/neu").headers["Location"].endswith("/freischalten"), "neue Fassung: Anlegen gesperrt")
pruefe("neue Fassung" in A.get("/verein/dokumente/freischalten").get_data(as_text=True), "Freischalt-Seite nennt die neue Fassung")
r = A.post(f"/verein/dokumente/{fa['id']}/loeschen", data={"_csrf": T})
pruefe(r.status_code == 302 and not D.hole(fa["id"], "va"), "Löschen geht auch ohne neue Fassung")
VD.AVV_FASSUNG = VP.AVV_FASSUNG = alt_f

print("Datenschutz, Admin")
s = client().get("/verein/datenschutz").get_data(as_text=True)
pruefe("Server-Protokolle" in s and "14 Tagen" in s and "Anthropic" in s and "Telegram" in s and "12 Monaten" in s
       and "/verein/avv" in s, "Datenschutzerklärung ergänzt")
pruefe("Entwürfe und Planungsrunden" in client().get("/verein/nutzungsbedingungen").get_data(as_text=True),
       "Nutzungsbedingungen: keine Personendaten in Entwürfe")
liste = client().get("/api/admin/users", headers={"X-Upload-Token": "admintoken"}).get_json()
va = [v for v in liste if v["verein_key"] == "va"][0]
pruefe(va["avv_fassung"] == alt_f and va["avv_am"], "Admin-Liste: Freischaltung je Verein")
st = client().get("/api/admin/stats", headers={"X-Upload-Token": "admintoken"}).get_json()
pruefe(st["datenschutz"]["avv_vereine"] == 2, f"Statistik: freigeschaltete Vereine, war {st['datenschutz']}")
html_k = (ROOT / "kalender.html").read_text()
pruefe("Dokumente freigeschaltet am" in html_k and "avv_vereine" in html_k, "Admin-Ansicht im Frontend")

print(f"\n{OK} Prüfungen ok, {len(FEHLER)} Fehler")
if FEHLER:
    print(f"{len(FEHLER)} PRÜFUNG(EN) FEHLGESCHLAGEN")
    sys.exit(1)
print("ALLE PRÜFUNGEN BESTANDEN")
