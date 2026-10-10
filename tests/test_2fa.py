#!/usr/bin/env python3
"""Offline-Abnahme Anmeldung mit Authenticator-App (2FA, v1.84): Einrichten, Login mit Code, Ersatzcodes,
Sperre, mehrere Vereine, Abschalten, Zurücksetzen durch Josef. Braucht pyotp + segno.

    python3 tests/test_2fa.py
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
TMP = Path(tempfile.mkdtemp(prefix="vko-2fa-"))

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




import re as _re  # noqa: E402
import pyotp  # noqa: E402

PW = "geheim123"


def konto(name, key, email, role="admin", vid=None):
    import bcrypt
    with vk_db.db_conn() as c:
        if vid is None:
            vid = c.execute("INSERT INTO vereine_accounts (verein_key, verein_name, status) VALUES (?,?, 'aktiv') RETURNING id",
                            (key, name)).fetchone()["id"]
        uid = c.execute("INSERT INTO vk_users (email, password_hash, verein_id, role, email_verified, aktiv, ds_fassung)"
                        " VALUES (?,?,?,?, 1, 1, ?) RETURNING id",
                        (email, bcrypt.hashpw(PW.encode(), bcrypt.gensalt(4)).decode(), vid, role, vk_db.DS_FASSUNG)).fetchone()["id"]
    return vid, uid


def neu_client():
    c = app.test_client()
    with c.session_transaction() as s:
        s["_csrf"] = "tok"
    return c


def cookie(r, name):
    return any(h.startswith(name + "=") and not h.startswith(name + "=;") for h in r.headers.getlist("Set-Cookie"))


def login(c, email):
    return c.post("/verein/login", data={"_csrf": T, "email": email, "password": PW})


T = "tok"
vid_a, uid_a = konto("FF Verein A", "va", "a@example.org")
_, uid_m = konto("", "", "m@example.org", role="member", vid=vid_a)

print("Ohne 2FA")
c = neu_client()
r = login(c, "a@example.org")
pruefe(r.status_code == 302 and r.headers["Location"] == "/verein/termine" and cookie(r, "vk_session"), "Login wie bisher")
s = c.get("/verein/einstellungen").get_data(as_text=True)
pruefe("Anmeldung mit Authenticator-App" in s and "/verein/2fa" in s, "Einstellungen bieten 2FA an")
m = neu_client(); login(m, "m@example.org")
pruefe(m.get("/verein/2fa").headers.get("Location") == "/verein/einstellungen", "Mitglied: keine 2FA-Seite")

print("Einrichten")
s = c.get("/verein/2fa").get_data(as_text=True)
with c.session_transaction() as sess:
    secret = sess["totp_neu"]
pruefe("<svg" in s and "otpauth://totp/" in s and "Einschalten" in s, "QR-Code, Link und Schlüssel")
c.get("/verein/2fa")
with c.session_transaction() as sess:
    pruefe(sess["totp_neu"] == secret, "Schlüssel bleibt beim Neuladen gleich")
r = c.post("/verein/2fa", data={"_csrf": T, "aktion": "einrichten", "code": "000000"})
pruefe("stimmt nicht" in r.get_data(as_text=True), "falscher Code beim Einrichten abgelehnt")
with vk_db.db_conn() as conn:
    pruefe(conn.execute("SELECT totp_secret FROM vk_users WHERE id = ?", (uid_a,)).fetchone()[0] is None, "… nichts gespeichert")
r = c.post("/verein/2fa", data={"_csrf": T, "aktion": "einrichten", "code": pyotp.TOTP(secret).now()})
s = r.get_data(as_text=True)
codes = _re.findall(r"<code[^>]*>([a-z0-9]{4}-[a-z0-9]{4})</code>", s)
pruefe(len(codes) == 10 and len(set(codes)) == 10, f"10 Ersatzcodes angezeigt, waren {len(codes)}")
with vk_db.db_conn() as conn:
    row = conn.execute("SELECT totp_secret, totp_recovery_hashes FROM vk_users WHERE id = ?", (uid_a,)).fetchone()
pruefe(row[0] == secret and not any(cd in (row[1] or "") for cd in codes), "Schlüssel gespeichert, Ersatzcodes nur als Hash")
with c.session_transaction() as sess:
    pruefe("totp_neu" not in sess, "Schlüssel aus der Sitzung entfernt")
pruefe("Eingeschaltet" in c.get("/verein/einstellungen").get_data(as_text=True), "Einstellungen: eingeschaltet")

print("Login mit Code")
d = neu_client()
r = login(d, "a@example.org")
pruefe(r.headers["Location"] == "/verein/login/2fa" and not cookie(r, "vk_session") and cookie(r, "vk_2fa"),
       "nach Passwort: Code-Abfrage, keine Sitzung")
pruefe(d.get("/verein/termine").headers["Location"] == "/verein/login", "ohne Code kein Vereinsbereich")
r = d.post("/verein/login/2fa", data={"_csrf": T, "code": "123456" if pyotp.TOTP(secret).now() != "123456" else "654321"})
pruefe("stimmt nicht" in r.get_data(as_text=True) and not cookie(r, "vk_session"), "falscher Code abgelehnt")
r = d.post("/verein/login/2fa", data={"_csrf": T, "code": pyotp.TOTP(secret).now()})
pruefe(r.status_code == 302 and r.headers["Location"] == "/verein/termine" and cookie(r, "vk_session"), "richtiger Code → Sitzung")
pruefe(d.get("/verein/termine").status_code == 200, "Vereinsbereich erreichbar")
with vk_db.db_conn() as conn:
    pruefe(conn.execute("SELECT login_attempts FROM vk_users WHERE id = ?", (uid_a,)).fetchone()[0] == 0, "Fehlversuche zurückgesetzt")
pruefe(neu_client().get("/verein/login/2fa").headers["Location"] == "/verein/login", "Code-Seite ohne Pre-Auth → Login")

print("Ersatzcodes")
e = neu_client(); login(e, "a@example.org")
r = e.post("/verein/login/2fa", data={"_csrf": T, "code": codes[0].upper().replace("-", " ")})
pruefe(cookie(r, "vk_session"), "Ersatzcode (Großbuchstaben, Leerzeichen) funktioniert")
e2 = neu_client(); login(e2, "a@example.org")
r = e2.post("/verein/login/2fa", data={"_csrf": T, "code": codes[0]})
pruefe(not cookie(r, "vk_session"), "derselbe Ersatzcode gilt nur einmal")
s = c.get("/verein/2fa").get_data(as_text=True)
pruefe("Noch 9 von 10" in s, "Restzahl der Ersatzcodes")

print("Sperre")
with vk_db.db_conn() as conn:
    conn.execute("UPDATE vk_users SET login_attempts = 0, locked_until = NULL WHERE id = ?", (uid_a,))
f = neu_client(); login(f, "a@example.org")
for _ in range(5):
    f.post("/verein/login/2fa", data={"_csrf": T, "code": "000000"})
r = f.post("/verein/login/2fa", data={"_csrf": T, "code": pyotp.TOTP(secret).now()})
pruefe("Zu viele Fehlversuche" in r.get_data(as_text=True) and not cookie(r, "vk_session"), "nach 5 Fehlversuchen gesperrt")
pruefe("Zu viele Fehlversuche" in login(neu_client(), "a@example.org").get_data(as_text=True), "Sperre gilt auch fürs Passwort")
with vk_db.db_conn() as conn:
    conn.execute("UPDATE vk_users SET login_attempts = 0, locked_until = NULL WHERE id = ?", (uid_a,))

print("Mehrere Vereine")
vid_b, uid_b = konto("Verein B", "vb", "a@example.org")      # gleiche E-Mail, ohne 2FA
g_ = neu_client()
r = login(g_, "a@example.org")
pruefe(r.headers["Location"] == "/verein/login/verein-waehlen", "Auswahl der Vereine")
r = g_.post("/verein/login/verein-waehlen", data={"_csrf": T, "user_id": uid_b})
pruefe(cookie(r, "vk_session") and r.headers["Location"] == "/verein/termine", "Verein ohne 2FA: direkt angemeldet")
h = neu_client(); login(h, "a@example.org")
r = h.post("/verein/login/verein-waehlen", data={"_csrf": T, "user_id": uid_a})
pruefe(r.headers["Location"] == "/verein/login/2fa" and not cookie(r, "vk_session"), "Verein mit 2FA: Code-Abfrage")
r = h.post("/verein/login/2fa", data={"_csrf": T, "code": pyotp.TOTP(secret).now()})
pruefe(cookie(r, "vk_session"), "… mit Code angemeldet")

print("Josef setzt zurück, Abschalten")
pruefe(neu_client().post(f"/api/admin/users/{uid_a}/2fa-reset").status_code == 401, "Reset ohne Token 401")
liste = neu_client().get("/api/admin/users", headers={"X-Upload-Token": "admintoken"}).get_json()
pruefe(any(u["id"] == uid_a and u["totp_aktiv"] for v in liste for u in v["users"]), "Admin-Liste zeigt 2FA aktiv")
r = neu_client().post(f"/api/admin/users/{uid_a}/2fa-reset", headers={"X-Upload-Token": "admintoken"})
pruefe(r.status_code == 200, "Reset mit Token")
i = neu_client()
r = i.post("/verein/login", data={"_csrf": T, "email": "a@example.org", "password": PW})
r = i.post("/verein/login/verein-waehlen", data={"_csrf": T, "user_id": uid_a})
pruefe(cookie(r, "vk_session"), "nach Reset wieder nur Passwort")
pruefe(neu_client().post(f"/api/admin/users/{uid_a}/2fa-reset", headers={"X-Upload-Token": "admintoken"}).status_code == 404,
       "Reset ohne aktive 2FA → 404")
# erneut einrichten und selbst abschalten
c.get("/verein/2fa")
with c.session_transaction() as sess:
    secret2 = sess["totp_neu"]
c.post("/verein/2fa", data={"_csrf": T, "aktion": "einrichten", "code": pyotp.TOTP(secret2).now()})
r = c.post("/verein/2fa", data={"_csrf": T, "aktion": "aus", "code": "000000"})
pruefe("stimmt nicht" in r.get_data(as_text=True), "Abschalten mit falschem Code abgelehnt")
r = c.post("/verein/2fa", data={"_csrf": T, "aktion": "aus", "code": pyotp.TOTP(secret2).now()})
with vk_db.db_conn() as conn:
    pruefe(conn.execute("SELECT totp_secret FROM vk_users WHERE id = ?", (uid_a,)).fetchone()[0] is None
           and "abgeschaltet" in r.get_data(as_text=True), "Abschalten mit Code")
    au = [x[0] for x in conn.execute("SELECT aktion FROM vk_audit WHERE aktion LIKE '2fa_%'")]
pruefe(au.count("2fa_eingerichtet") == 2 and au.count("2fa_abgeschaltet") == 1, f"Audit 2FA, war {au}")
html_k = (ROOT / "kalender.html").read_text()
pruefe("acc-2fa-btn" in html_k and "/2fa-reset" in html_k, "Admin-Knopf im Frontend")

print(f"\n{OK} Prüfungen ok, {len(FEHLER)} Fehler")
if FEHLER:
    print(f"{len(FEHLER)} PRÜFUNG(EN) FEHLGESCHLAGEN")
    sys.exit(1)
print("ALLE PRÜFUNGEN BESTANDEN")
