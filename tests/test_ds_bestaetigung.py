#!/usr/bin/env python3
"""Offline-Abnahme Datenschutz-Bestätigung je Fassung (v1.79, `/verein/bestaetigen`).

    python3 tests/test_ds_bestaetigung.py

Aufbau wie `test_dokumente.py`: Temp-Verzeichnis, eigene DB, keine Netz-/Mail-/Telegram-Zugriffe.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
import tempfile
import types
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="vko-ds-"))

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

app = webhook.app
app.config["TESTING"] = True


def konto(name, key, email):
    import bcrypt
    with vk_db.db_conn() as c:
        vid = c.execute("INSERT INTO vereine_accounts (verein_key, verein_name, status) VALUES (?,?, 'aktiv') RETURNING id",
                        (key, name)).fetchone()["id"]
        uid = c.execute("INSERT INTO vk_users (email, password_hash, verein_id, role, email_verified, aktiv, ds_fassung) "
                        f"VALUES (?,?,?, 'admin', 1, 1, '{vk_db.DS_FASSUNG}') RETURNING id",
                        (email, bcrypt.hashpw(b"geheim123", bcrypt.gensalt(4)).decode(), vid)).fetchone()["id"]
    return vid, uid


def mitglied(vid, email):
    import bcrypt
    with vk_db.db_conn() as c:
        return c.execute("INSERT INTO vk_users (email, password_hash, verein_id, role, email_verified, aktiv, ds_fassung) "
                         f"VALUES (?,?,?, 'member', 1, 1, '{vk_db.DS_FASSUNG}') RETURNING id",
                         (email, bcrypt.hashpw(b"geheim123", bcrypt.gensalt(4)).decode(), vid)).fetchone()["id"]


import services.auth.routes as AR  # noqa: E402


def konto(name, key, email, fassung=None, role="admin"):
    import bcrypt
    with vk_db.db_conn() as c:
        vid = c.execute("INSERT INTO vereine_accounts (verein_key, verein_name, status) VALUES (?,?, 'aktiv') RETURNING id",
                        (key, name)).fetchone()["id"]
        uid = c.execute("INSERT INTO vk_users (email, password_hash, verein_id, role, email_verified, aktiv, ds_fassung)"
                        " VALUES (?,?,?,?, 1, 1, ?) RETURNING id",
                        (email, bcrypt.hashpw(b"geheim123", bcrypt.gensalt(4)).decode(), vid, role, fassung)
                        ).fetchone()["id"]
    return vid, uid


def client(uid=None):
    c = app.test_client()
    if uid:
        c.set_cookie("vk_session", vk_db.create_session(uid))
    with c.session_transaction() as s:
        s["_csrf"] = "tok"
    return c


def zeile(uid):
    with vk_db.db_conn() as c:
        return dict(c.execute("SELECT ds_fassung, ds_bestaetigt_am FROM vk_users WHERE id = ?", (uid,)).fetchone())


T = "tok"
F = vk_db.DS_FASSUNG
vid_a, uid_a = konto("FF Verein A", "va", "a@example.org")              # Altkonto, nie bestätigt
vid_b, uid_b = konto("Schützen B", "vb", "b@example.org", fassung=F)    # aktuelle Fassung

print("Umleitung für Altkonten")
A = client(uid_a)
for pfad in ("/verein/termine", "/verein/dokumente", "/verein/einstellungen"):
    r = A.get(pfad)
    pruefe(r.status_code == 302 and r.headers["Location"].startswith("/verein/bestaetigen"), f"{pfad} → Bestätigung")
r = A.get("/verein/dokumente?q=Satzung")
pruefe(unquote(r.headers["Location"]) == "/verein/bestaetigen?ziel=/verein/dokumente?q=Satzung", "Ziel samt Suche gemerkt")
r = A.post("/verein/dokumente", data={"_csrf": T})
pruefe(r.status_code == 302 and r.headers["Location"] == "/verein/bestaetigen", "POST ohne Ziel umgeleitet, nichts gespeichert")
B = client(uid_b)
pruefe(B.get("/verein/termine").status_code == 200, "bestätigtes Konto kommt direkt rein")

print("Seite /verein/bestaetigen")
s = A.get("/verein/bestaetigen?ziel=/verein/dokumente").get_data(as_text=True)
pruefe('name="datenschutz_gelesen"' in s and "required" in s, "Pflicht-Kästchen")
pruefe('href="/verein/datenschutz" target="_blank"' in s and 'href="/verein/nutzungsbedingungen" target="_blank"' in s,
       "Links öffnen im neuen Tab")
pruefe('action="/verein/logout"' in s, "Abmelden möglich")
pruefe('value="/verein/dokumente"' in s, "Ziel im Formular")
pruefe("Bevor es weitergeht" in s, "Text für Erstbestätigung")
r = A.post("/verein/bestaetigen", data={"ziel": "/verein/dokumente"})
pruefe(r.status_code == 403 and zeile(uid_a)["ds_fassung"] is None, "ohne CSRF abgelehnt")
r = A.post("/verein/bestaetigen", data={"_csrf": T, "ziel": "/verein/dokumente"})
pruefe(r.status_code == 200 and "Bitte bestätigen" in r.get_data(as_text=True) and zeile(uid_a)["ds_fassung"] is None,
       "ohne Häkchen: Hinweis, nichts gespeichert")
r = A.post("/verein/bestaetigen", data={"_csrf": T, "ziel": "/verein/dokumente", "datenschutz_gelesen": "on"})
pruefe(r.status_code == 302 and r.headers["Location"] == "/verein/dokumente", "mit Häkchen weiter zum Ziel")
z = zeile(uid_a)
pruefe(z["ds_fassung"] == F and z["ds_bestaetigt_am"], "Fassung und Zeitpunkt gespeichert")
with vk_db.db_conn() as c:
    au = c.execute("SELECT * FROM vk_audit WHERE aktion = 'datenschutz_bestaetigt' AND user_id = ?", (uid_a,)).fetchall()
pruefe(len(au) == 1 and au[0]["termin_id"] == F and au[0]["verein_key"] == "va", "Audit-Eintrag mit Fassung")
pruefe(A.get("/verein/dokumente").status_code == 200, "danach nicht mehr gefragt")
r = A.get("/verein/bestaetigen")
pruefe(r.status_code == 302 and r.headers["Location"] == "/verein/termine", "Seite bei bestätigtem Konto → weiter")

print("Ziel nur im Vereinsbereich")
for ziel in ("//evil.example", "https://evil.example/verein/", "/verein//evil", "/kalender", "/verein/bestaetigen",
             "/verein/x\r\nSet-Cookie:a"):
    pruefe(AR._ziel_ok(ziel) == "", f"Ziel {ziel!r} verworfen")
pruefe(AR._ziel_ok("/verein/r/Abc_123-xy") == "/verein/r/Abc_123-xy", "Einladungslink Planungsrunde erlaubt")
vid_c, uid_c = konto("Musik C", "vc", "c@example.org")
C = client(uid_c)
r = C.post("/verein/bestaetigen", data={"_csrf": T, "ziel": "//evil.example", "datenschutz_gelesen": "on"})
pruefe(r.headers["Location"] == "/verein/termine", "fremdes Ziel → /verein/termine")
vid_d, uid_d = konto("Chor D", "vd", "d@example.org")
r = client(uid_d).get("/verein/r/Abc_123-xy")
pruefe(unquote(r.headers["Location"]) == "/verein/bestaetigen?ziel=/verein/r/Abc_123-xy",
       "Einladungslink Planungsrunde: erst bestätigen, dann weiter")

print("Ohne Login / nicht freigegeben")
r = client().get("/verein/bestaetigen")
pruefe(r.status_code == 302 and r.headers["Location"] == "/verein/login", "ohne Login → Login")
with vk_db.db_conn() as c:
    c.execute("UPDATE vereine_accounts SET status = 'pending' WHERE id = ?", (vid_d,))
r = client(uid_d).get("/verein/bestaetigen")
pruefe(r.headers["Location"] == "/verein/login?hint=pending", "Verein nicht freigegeben → Login-Hinweis")
with vk_db.db_conn() as c:
    c.execute("UPDATE vereine_accounts SET status = 'aktiv' WHERE id = ?", (vid_d,))

print("Neue Fassung")
alt = vk_db.DS_FASSUNG
AR.DS_FASSUNG = "2099-01"
r = A.get("/verein/termine")
pruefe(r.headers.get("Location", "").startswith("/verein/bestaetigen"), "neue Fassung → erneut gefragt")
s = A.get("/verein/bestaetigen").get_data(as_text=True)
pruefe("haben sich geändert" in s, "Text für geänderte Fassung")
A.post("/verein/bestaetigen", data={"_csrf": T, "datenschutz_gelesen": "on"})
pruefe(zeile(uid_a)["ds_fassung"] == "2099-01", "neue Fassung gespeichert")
with vk_db.db_conn() as c:
    n = c.execute("SELECT COUNT(*) FROM vk_audit WHERE aktion = 'datenschutz_bestaetigt' AND user_id = ?",
                  (uid_a,)).fetchone()[0]
pruefe(n == 2, "Verlauf: beide Fassungen im Audit")
AR.DS_FASSUNG = alt
with vk_db.db_conn() as c:
    c.execute("UPDATE vk_users SET ds_fassung = ? WHERE id = ?", (alt, uid_a))

print("Einladung annehmen")
import bcrypt  # noqa: E402
with vk_db.db_conn() as c:
    uid_m = c.execute("INSERT INTO vk_users (email, password_hash, verein_id, role, email_verified, aktiv,"
                      " einladungs_token, einladungs_expires) VALUES (?,?,?, 'member', 1, 0, 'inv123', '2099-01-01T00:00:00')"
                      " RETURNING id", ("m@example.org", "x", vid_b)).fetchone()["id"]
M = client()
s = M.get("/verein/einladung?token=inv123").get_data(as_text=True)
pruefe('name="datenschutz_gelesen"' in s, "Einladung: Kästchen vorhanden")
s = M.post("/verein/einladung", data={"_csrf": T, "token": "inv123", "password": "geheim123",
                                      "password2": "geheim123"}).get_data(as_text=True)
pruefe("Bitte bestätigen" in s, "Einladung ohne Häkchen abgelehnt")
r = M.post("/verein/einladung", data={"_csrf": T, "token": "inv123", "password": "geheim123",
                                      "password2": "geheim123", "datenschutz_gelesen": "on"})
pruefe(r.status_code == 302 and zeile(uid_m)["ds_fassung"] == F, "Einladung angenommen, Fassung gespeichert")
pruefe(client(uid_m).get("/verein/termine").status_code == 200, "Mitglied nach Einladung nicht nochmal gefragt")

print("Registrierung")
REG = {"_csrf": T, "verein_name": "Trachtenverein Hölskofen", "rubrik": "Verein", "plz": "84092",
       "heimatort": "Hölskofen", "anrede": "Herr", "vorname": "Max", "nachname": "Muster", "email": "r@example.org",
       "telefon": "0172 1234567", "password": "geheim123", "password2": "geheim123", "selbstverpflichtung": "on",
       "zugangsdaten_notiert": "on"}
R = client()
s = R.get("/verein/register").get_data(as_text=True)
pruefe('name="datenschutz_gelesen"' in s and "name=datenschutz_gelesen" in s, "Registrierung: Kästchen + JS-Prüfung")
s = R.post("/verein/register", data=REG).get_data(as_text=True)
with vk_db.db_conn() as c:
    pruefe(c.execute("SELECT 1 FROM vk_users WHERE email = 'r@example.org'").fetchone() is None
           and "Bitte bestätigen" in s, "Registrierung ohne Häkchen abgelehnt")
R.post("/verein/register", data={**REG, "datenschutz_gelesen": "on"})
with vk_db.db_conn() as c:
    u = c.execute("SELECT ds_fassung, ds_bestaetigt_am FROM vk_users WHERE email = 'r@example.org'").fetchone()
pruefe(u is not None and u["ds_fassung"] == F and u["ds_bestaetigt_am"], "Registrierung speichert Fassung")

print("Datenschutzerklärung")
s = client().get("/verein/datenschutz").get_data(as_text=True)
pruefe("Art. 6 Abs. 1 lit. b" in s and "Rechtsgrundlage" in s, "Abschnitt Rechtsgrundlage")
pruefe("Stand: Oktober 2026" in s, "Stand aus DS_FASSUNG")

print("Admin-Ansicht")
pruefe(client().get("/api/admin/users").status_code == 401, "Kontenliste ohne Token 401")
liste = client().get("/api/admin/users", headers={"X-Upload-Token": "admintoken"}).get_json()
us = {u["email"]: u for v in liste for u in v["users"]}
pruefe(us["a@example.org"]["ds_aktuell"] and us["a@example.org"]["ds_fassung"] == F
       and us["a@example.org"]["ds_bestaetigt_am"], "Konto A: bestätigt mit Zeitpunkt")
pruefe(not us["d@example.org"]["ds_aktuell"] and us["d@example.org"]["ds_fassung"] is None, "Konto D: offen")
st = client().get("/api/admin/stats", headers={"X-Upload-Token": "admintoken"}).get_json()
pruefe(st.get("datenschutz", {}).get("fassung") == F and st["datenschutz"]["konten"] >= 5
       and st["datenschutz"]["bestaetigt"] < st["datenschutz"]["konten"], "Statistik: bestätigt von Konten")
html = (ROOT / "kalender.html").read_text()
pruefe("Datenschutz bestätigt am" in html and "data.datenschutz" in html, "Admin-Liste zeigt die Bestätigung")

print(f"\n{OK} Prüfungen ok, {len(FEHLER)} Fehler")
if FEHLER:
    print(f"{len(FEHLER)} PRÜFUNG(EN) FEHLGESCHLAGEN")
    sys.exit(1)
print("ALLE PRÜFUNGEN BESTANDEN")
