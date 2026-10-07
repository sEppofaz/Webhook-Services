#!/usr/bin/env python3
"""Offline-Abnahme der Flask-App (Vereinsbereich, Admin-API, Telegram, Erinnerungen).

    python3 tests/test_app.py

Ohne Server, ohne Netz, ohne Mails, ohne Telegram: alle Dateien liegen in einem
Temp-Verzeichnis, Netzwerk-Funktionen werden durch Attrappen ersetzt.
Entstanden mit dem Komplett-Review 2026-10-04 (v1.44) – je Befund eine Prüfung.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="vko-test-"))

for k in ("CLAUDE_API_KEY", "DROPBOX_REFRESH_TOKEN", "DROPBOX_APP_KEY", "DROPBOX_APP_SECRET"):
    os.environ.setdefault(k, "test")
os.environ["FLASK_SECRET_KEY"] = "test-secret"
os.environ["UPLOAD_TOKEN"] = "admintoken"
os.environ["TELEGRAM_WEBHOOK_SECRET"] = "tgsecret"
os.environ["CHAT_ID"] = "4711"
os.environ["VKO_COOKIE_INSECURE"] = "1"   # Testclient spricht http
for k in ("TOKEN", "BREVO_SMTP_USER", "BREVO_SMTP_KEY", "KALENDER_BOT_TOKEN"):
    os.environ.pop(k, None)

# Fremdpakete, die nur Nicht-VKO-Blueprints brauchen, lokal oft fehlen → Attrappe
import importlib  # noqa: E402
import types  # noqa: E402
for _pkg in ("yfinance", "anthropic", "pillow_heif"):
    try:
        importlib.import_module(_pkg)
    except ImportError:
        sys.modules[_pkg] = types.ModuleType(_pkg)

import shared.vk_db as vk_db  # noqa: E402
import shared.kalender_store as kstore  # noqa: E402

vk_db.DB_FILE = TMP / "vk_accounts.db"
VT = TMP / "vereinstermine.json"
kstore.VEREINSTERMINE_FILE = VT
VT.write_text(json.dumps({"_labels": {}, "_meta": {}}))

import webhook  # noqa: E402

PFADE = {
    "VEREINSTERMINE_FILE": VT,
    "GOTTESDIENSTE_FILE": TMP / "gottesdienste.json",
    "HEIMAT_PENDING_DIR": TMP / "imports",
    "PENDING_DIR": TMP / "imports",
    "LAST_IMPORT_FILE": TMP / "last_import.json",
    "VKO_MAINTENANCE_FILE": TMP / "vko_maintenance",
}
(TMP / "imports").mkdir()


def patch_module(mod):
    for name, wert in PFADE.items():
        if hasattr(mod, name):
            setattr(mod, name, wert)


for m in list(sys.modules.values()):
    if m and getattr(m, "__file__", "") and str(ROOT) in str(getattr(m, "__file__", "")):
        patch_module(m)


def fake_plz(plz):
    return {"plz": plz, "gemeinde": "Bayerbach", "landkreis": "Landkreis Landshut"}


for m in list(sys.modules.values()):
    if m and hasattr(m, "lookup_plz") and str(ROOT) in str(getattr(m, "__file__", "")):
        m.lookup_plz = fake_plz

app = webhook.app
app.config["TESTING"] = True
ADMIN = {"X-Upload-Token": "admintoken"}

_fehler: list[str] = []


def pruefe(bedingung, beschreibung, detail=""):
    if bedingung:
        print("  ok   %s" % beschreibung)
    else:
        print("  FEHL %s%s" % (beschreibung, ("  – " + str(detail)[:300]) if detail else ""))
        _fehler.append(beschreibung)


def daten() -> dict:
    return json.loads(VT.read_text())


def schreibe(d: dict) -> None:
    VT.write_text(json.dumps(d, ensure_ascii=False))


def verein_anlegen(name: str, key: str, email: str, role: str = "admin", **verein) -> tuple[int, int]:
    import bcrypt
    with vk_db.db_conn() as c:
        vid = c.execute(
            "INSERT INTO vereine_accounts (verein_key, verein_name, status, rubrik, heimatort, plz, gemeinde, landkreis)"
            " VALUES (?,?,?,?,?,?,?,?) RETURNING id",
            (key, name, "aktiv", verein.get("rubrik", "Verein"), verein.get("heimatort"), verein.get("plz"),
             verein.get("gemeinde"), verein.get("landkreis"))).fetchone()["id"]
        uid = c.execute(
            "INSERT INTO vk_users (email, password_hash, verein_id, role, email_verified, aktiv)"
            " VALUES (?,?,?,?,1,1) RETURNING id",
            (email, bcrypt.hashpw(b"geheim123", bcrypt.gensalt(4)).decode(), vid, role)).fetchone()["id"]
    return vid, uid


def user_anlegen(verein_id: int, email: str, role: str = "member") -> int:
    import bcrypt
    with vk_db.db_conn() as c:
        return c.execute(
            "INSERT INTO vk_users (email, password_hash, verein_id, role, email_verified, aktiv)"
            " VALUES (?,?,?,?,1,1) RETURNING id",
            (email, bcrypt.hashpw(b"geheim123", bcrypt.gensalt(4)).decode(), verein_id, role)).fetchone()["id"]


def client_fuer(user_id: int):
    cl = app.test_client()
    tok = vk_db.create_session(user_id)
    cl.set_cookie("vk_session", tok)
    return cl, tok


def csrf(cl) -> str:
    with cl.session_transaction() as s:
        s["_csrf"] = "csrftest"
    return "csrftest"


XSS = '<img src=x onerror=alert(1)>'


# ── 1: XSS ───────────────────────────────────────────────────────────────────
def test_xss():
    print("\n1 · XSS")
    vid, uid = verein_anlegen(XSS, "xss_verein", "xss@example.org")
    cl, _ = client_fuer(uid)
    r = cl.get("/verein/dashboard")
    t = r.get_data(as_text=True)
    pruefe(r.status_code == 200, "Dashboard lädt", r.status_code)
    pruefe(XSS not in t and "&lt;img src=x" in t, "Vereinsname im Dashboard (Titel, Kopf) escapt")
    tok = csrf(cl)
    r = cl.post("/verein/mitglieder", data={"_csrf": tok, "aktion": "einladen", "email": '"><svg/onload=1>@x.de'})
    t = r.get_data(as_text=True)
    pruefe("gültige E-Mail" in t and "<svg/onload" not in t, "Einladung mit HTML-Adresse abgelehnt")
    with vk_db.db_conn() as c:
        n = c.execute("SELECT COUNT(*) FROM vk_users WHERE verein_id=?", (vid,)).fetchone()[0]
    pruefe(n == 1, "kein Mitglied mit ungültiger Adresse angelegt", n)
    user_anlegen(vid, "<b>fett</b>@x.de")
    t = cl.get("/verein/mitglieder").get_data(as_text=True)
    pruefe("<b>fett</b>" not in t and "&lt;b&gt;fett" in t, "Mitglieder-E-Mail escapt")
    from shared import vk_mail
    gesendet = {}
    alt = vk_mail._send
    vk_mail._send = lambda to, sub, body: gesendet.update(sub=sub, body=body) or True
    try:
        vk_mail.send_invite_email("a@b.de", "tok", XSS + "\nBcc: x@y.de")
    finally:
        vk_mail._send = alt
    pruefe(XSS not in gesendet["body"], "Einladungsmail escapt Vereinsnamen")
    gesendet.clear()
    pruefe(vk_mail._send("a@b.de\nBcc: x@y.de", "s", "b") is False, "Mail an Adresse mit Zeilenumbruch verweigert")
    html = (ROOT / "kalender.html").read_text(encoding="utf-8")
    pruefe("${r.label}" not in html and "${c.label}" not in html, "Suchliste + Chips escapen Labels")
    pruefe("replace(/'/g,\"\\\\'\")" not in html, "kein Namens-Escaping per \\' in onclick mehr")
    pruefe("&#39;" in re.search(r"function escHtml\(s\)\{[^\n]+", html).group(0), "escHtml escapt auch '")


# ── 2: Telegram-Webhooks nur mit Secret ─────────────────────────────────────
def test_telegram_secret():
    print("\n2 · Telegram-Webhook-Secret")
    cl = app.test_client()
    body = {"callback_query": {"id": "1", "data": "verein_approve:1:x", "from": {"id": 4711}}}
    pruefe(cl.post("/telegram", json=body).status_code == 403, "gefälschter Callback ohne Secret → 403")
    pruefe(cl.post("/telegram", json=body, headers={"X-Telegram-Bot-Api-Secret-Token": "falsch"}).status_code == 403,
           "falsches Secret → 403")
    pruefe(cl.post("/telegram", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "tgsecret"}).status_code == 200,
           "richtiges Secret → 200")
    pruefe(cl.post("/kalender-bot", json={"message": {"chat": {"id": 1}, "text": "/start"}}).status_code == 403,
           "Kalender-Bot ohne Secret → 403")
    from shared.telegram import webhook_secret
    a, b = webhook_secret("123:abc"), webhook_secret("123:abd")
    pruefe(len(a) == 64 and a != b and re.fullmatch(r"[0-9a-f]+", a), "Secret aus Token abgeleitet, Telegram-tauglich")
    pruefe(webhook_secret("123:abc", "eigen") == "eigen" and webhook_secret("") == "", "explizites Secret hat Vorrang")
    import telegram_webhook_guard as g
    aufrufe = []

    def fake(info):
        def _call(token, method, params=None):
            aufrufe.append((method, params))
            return {"result": info} if method == "getWebhookInfo" else {"ok": True}
        return _call
    url = "https://vereinskalender.online/telegram"
    g._call = fake({"url": url})
    pruefe(g.pruefe_bot("t", url, "s", False) == "ok" and len(aufrufe) == 1, "Webhook ok → nichts gesetzt")
    aufrufe.clear(); g._call = fake({"url": url, "last_error_message": "Wrong response from the webhook: 403 Forbidden"})
    pruefe(g.pruefe_bot("t", url, "s", False) == "neu" and aufrufe[-1][1] == {"url": url, "secret_token": "s"},
           "403 von Telegram → mit secret_token neu gesetzt")
    aufrufe.clear(); g._call = fake({"url": ""})
    pruefe(g.pruefe_bot("t", url, "s", False) == "neu", "fehlender Webhook → neu gesetzt")
    aufrufe.clear(); g._call = fake({"url": "https://x/kalender-bot"})
    pruefe(g.pruefe_bot("t", "", "s", True) == "neu" and aufrufe[-1][1]["url"] == "https://x/kalender-bot",
           "Kalender-Bot: --force behält bestehende URL")


# ── 3: Admin-Vereins-API ohne Lost Update ───────────────────────────────────
def test_vereine_api_lock():
    print("\n3 · /api/vereine ohne verlorene Schreibzugriffe")
    import services.kalender.routes as kr
    d = daten()
    d["_labels"]["ffw_test"] = "FFW Test"
    d["_meta"]["ffw_test"] = {"heimatort": "Testdorf"}
    d["ffw_test"] = []
    schreibe(d)

    def langsamer_lookup(plz):
        # währenddessen trägt ein Verein einen Termin ein
        th = threading.Thread(target=lambda: kstore.KalenderStore.update(
            lambda x: x.setdefault("ffw_test", []).append({"datum": "2099-01-01", "bezeichnung": "parallel"})))
        th.start(); th.join()
        return fake_plz(plz)
    alt = kr.lookup_plz
    kr.lookup_plz = langsamer_lookup
    try:
        r = app.test_client().post("/api/vereine", headers=ADMIN,
                                   json={"key": "ffw_test", "name": "FFW Test e.V.", "plz": "84092", "rubrik": "Verein"})
    finally:
        kr.lookup_plz = alt
    j = r.get_json()
    d = daten()
    pruefe(r.status_code == 200 and j["name"] == "FFW Test e.V.", "POST speichert Name", r.get_data(as_text=True))
    pruefe(any(t.get("bezeichnung") == "parallel" for t in d["ffw_test"]), "paralleler Termin bleibt erhalten")
    pruefe(d["_meta"]["ffw_test"].get("heimatort") == "Testdorf" and d["_meta"]["ffw_test"].get("gemeinde") == "Bayerbach",
           "PLZ-Daten ergänzt, Heimatort behalten", d["_meta"]["ffw_test"])
    pruefe(app.test_client().post("/api/vereine", headers=ADMIN, json={"key": "gibtsnicht"}).status_code == 404,
           "unbekannter Verein → 404")
    r = app.test_client().delete("/api/vereine/ffw_test", headers=ADMIN)
    d = daten()
    pruefe(r.status_code == 200 and r.get_json()["geloescht"] == 1 and "ffw_test" not in d
           and "ffw_test" not in d["_labels"], "DELETE entfernt Verein samt Terminen", r.get_data(as_text=True))
    pruefe(app.test_client().delete("/api/vereine/ffw_test", headers=ADMIN).status_code == 404, "zweites DELETE → 404")
    src = (ROOT / "services" / "kalender" / "routes.py").read_text()
    pruefe("d.clear() or d.update" not in src, "kein Snapshot-Zurückschreiben mehr in kalender/routes.py")


# ── 4: Abo-Erinnerungen ohne gelöschte Termine, HTML-sicher ─────────────────
def test_erinnerung():
    print("\n4 · kalender_erinnerung.py")
    import kalender_erinnerung as ke
    from zoneinfo import ZoneInfo
    morgen = (datetime.now(ZoneInfo("Europe/Berlin")) + timedelta(days=1)).strftime("%Y-%m-%d")
    d = daten()
    d["_labels"]["musik"] = "Musik & <Tanz>"
    d["musik"] = [
        {"datum": morgen, "bezeichnung": "Kaffee & Kuchen <drinnen>", "ort": "Saal", "uhrzeit": "14:00"},
        {"datum": morgen, "bezeichnung": "Verworfen", "geloescht": True, "geloescht_von": "admin_reject"},
        {"datum": morgen, "bezeichnung": "Alt gelöscht", "deleted": True},
    ]
    schreibe(d)
    gesendet = []
    ke.VEREINSTERMINE_FILE = VT
    ke.load_kalender_bot_token = lambda: "tok"
    ke.tg_get_all_subscriptions = lambda: [{"chat_id": "99", "verein_key": "musik"}]
    ke.send = lambda token, chat_id, text: gesendet.append(text)
    ke.main()
    text = "\n".join(gesendet)
    pruefe(len(gesendet) == 1 and "Kaffee &amp; Kuchen &lt;drinnen&gt;" in text, "Termin escapt gesendet", text)
    pruefe("Verworfen" not in text and "Alt gelöscht" not in text, "gelöschte/verworfene Termine nicht erinnert")
    pruefe("<b>Musik &amp; &lt;Tanz&gt;</b>" in text, "Vereinsname escapt")
    d = daten(); d.pop("musik"); d["_labels"].pop("musik"); schreibe(d)


# ── 5 + 6: Mitglied entfernen, Sessions ─────────────────────────────────────
def test_sessions():
    print("\n5/6 · Mitglied entfernen + Sessions")
    vid, admin = verein_anlegen("Session-Verein", "session_verein", "admin@sv.de")
    mitglied = user_anlegen(vid, "mitglied@sv.de")
    mcl, mtok = client_fuer(mitglied)
    vk_db.log_audit("test", "x", "session_verein", mitglied)
    pruefe(mcl.get("/verein/dashboard").status_code == 200, "Mitglied eingeloggt")
    acl, atok = client_fuer(admin)
    tok = csrf(acl)
    r = acl.post("/verein/mitglieder", data={"_csrf": tok, "aktion": "entfernen", "member_id": str(mitglied)})
    pruefe(r.status_code == 200 and "Mitglied entfernt" in r.get_data(as_text=True), "Entfernen trotz Session/Audit", r.status_code)
    with vk_db.db_conn() as c:
        weg = c.execute("SELECT COUNT(*) FROM vk_users WHERE id=?", (mitglied,)).fetchone()[0] == 0
        audit = c.execute("SELECT COUNT(*) FROM vk_audit WHERE aktion='test' AND user_id IS NULL").fetchone()[0]
    pruefe(weg and audit == 1, "Benutzer gelöscht, Audit-Zeile bleibt ohne Benutzerbezug")
    pruefe(vk_db.get_session_user(mtok) is None, "Session des Entfernten ungültig")
    # Admin eines anderen Vereins kann fremde Mitglieder nicht löschen
    vid2, admin2 = verein_anlegen("Anderer", "anderer", "admin@anderer.de")
    m2 = user_anlegen(vid2, "m2@anderer.de")
    acl.post("/verein/mitglieder", data={"_csrf": tok, "aktion": "entfernen", "member_id": str(m2)})
    with vk_db.db_conn() as c:
        pruefe(c.execute("SELECT COUNT(*) FROM vk_users WHERE id=?", (m2,)).fetchone()[0] == 1, "fremdes Mitglied bleibt")
    # deaktivierter Benutzer → keine Session
    with vk_db.db_conn() as c:
        c.execute("UPDATE vk_users SET aktiv=0 WHERE id=?", (m2,))
    _, t2 = client_fuer(m2)
    pruefe(vk_db.get_session_user(t2) is None, "deaktivierter Benutzer hat keine gültige Session")
    # absolute Laufzeit
    _, alt = client_fuer(admin2)
    with vk_db.db_conn() as c:
        c.execute("UPDATE vk_sessions SET created_at=datetime('now','-9 hours'), last_active=CURRENT_TIMESTAMP WHERE id=?", (alt,))
    pruefe(vk_db.get_session_user(alt) is None, "Session nach 8 h ab Login abgelaufen, trotz Aktivität")
    # Passwort ändern beendet andere Sessions, behält die eigene
    _, anderes_geraet = client_fuer(admin)
    r = acl.post("/verein/passwort", data={"_csrf": tok, "password_alt": "geheim123",
                                         "password_neu": "neuesPasswort1", "password_neu2": "neuesPasswort1"})
    pruefe("erfolgreich" in r.get_data(as_text=True), "Passwort geändert")
    pruefe(vk_db.get_session_user(anderes_geraet) is None and vk_db.get_session_user(atok) is not None,
           "andere Sessions beendet, aktuelle bleibt")
    # Passwort-Reset beendet alle Sessions
    with vk_db.db_conn() as c:
        c.execute("UPDATE vk_users SET reset_token='rt', reset_token_expires=? WHERE id=?",
                  ((datetime.utcnow() + timedelta(hours=1)).isoformat(), admin))
    cl = app.test_client(); t = csrf(cl)
    r = cl.post("/verein/passwort-reset", data={"_csrf": t, "token": "rt", "password": "nochNeuer12", "password2": "nochNeuer12"})
    pruefe(r.status_code == 302 and vk_db.get_session_user(atok) is None, "Reset beendet alle Sessions", r.status_code)


# ── 7: Admin-Löschen als Soft-Delete ────────────────────────────────────────
def test_admin_loeschen():
    print("\n7 · Admin-Löschen")
    import services.kalender.routes as kr
    import services.verein.routes as vr
    import heimat_import as hi
    d = daten()
    d["_labels"]["gem"] = "Veranstaltungen Gem"
    d["gem"] = [{"id": "aaaa1111", "datum": "2099-05-01", "uhrzeit": "19:00", "bezeichnung": "Maifest am Dorfplatz",
                 "flyer_url": "https://x", "flyer_path": "/flyer/a.pdf"}]
    schreibe(d)
    geloescht = []
    alt_k, alt_v = kr.delete_flyer, vr.delete_flyer
    kr.delete_flyer = vr.delete_flyer = geloescht.append
    try:
        r = app.test_client().delete("/api/termine", headers=ADMIN,
                                     json={"verein_key": "gem", "datum": "2099-05-01", "bezeichnung": "Maifest am Dorfplatz", "id": "aaaa1111"})
        t = daten()["gem"][0]
        pruefe(r.status_code == 200 and t.get("geloescht") and "flyer_url" not in t, "Termin soft-gelöscht, Flyer-Felder weg")
        pruefe(geloescht == ["/flyer/a.pdf"], "Flyer in Dropbox gelöscht", geloescht)
        oeff = app.test_client().get("/api/termine").get_json()
        pruefe(not any(x.get("id") == "aaaa1111" for x in oeff["termine"]), "nicht mehr in /api/termine")
        pruefe(hi._is_duplicate("2099-05-01", "19:00", "Maifest am Dorfplatz", hi._existing_from_data(daten())),
               "Gemeinde-Import erkennt ihn als Duplikat (kommt nicht wieder)")
        pruefe(app.test_client().delete("/api/termine", headers=ADMIN, json={"verein_key": "gem", "datum": "2099-05-01",
               "bezeichnung": "Maifest am Dorfplatz", "id": "aaaa1111"}).status_code == 404, "zweites Löschen → 404")
        # Vereinsformular: Löschen entfernt den Flyer ebenfalls
        vid, uid = verein_anlegen("Flyer-Verein", "flyer_verein", "f@fv.de")
        d = daten(); d["_labels"]["flyer_verein"] = "Flyer-Verein"
        d["flyer_verein"] = [{"id": "bbbb2222", "datum": "2099-06-01", "bezeichnung": "Fest", "flyer_url": "u", "flyer_path": "/flyer/b.pdf"}]
        schreibe(d)
        cl, _ = client_fuer(uid); tok = csrf(cl)
        cl.post("/verein/termine/bbbb2222", data={"_csrf": tok, "aktion": "loeschen"})
        t = daten()["flyer_verein"][0]
        pruefe(t.get("geloescht") and "flyer_path" not in t and "/flyer/b.pdf" in geloescht, "Vereins-Löschen entfernt Flyer")
    finally:
        kr.delete_flyer, vr.delete_flyer = alt_k, alt_v


# ── 8: Admin-Accounts-Fenster wirkt auf den Kalender ────────────────────────
def test_admin_verein_meta():
    print("\n8 · Admin ändert Verein → _meta")
    vid, uid = verein_anlegen("Meta-Verein", "meta_verein", "m@mv.de", heimatort="Altdorf", plz="84032",
                              gemeinde="Altdorf", landkreis="Landkreis Landshut")
    d = daten(); d["_labels"]["meta_verein"] = "Meta-Verein"
    d["_meta"]["meta_verein"] = {"heimatort": "Altdorf", "plz": "84032", "gemeinde": "Altdorf",
                                 "landkreis": "Landkreis Landshut", "selbstverwaltung": True}
    schreibe(d)
    r = app.test_client().patch(f"/api/admin/verein/{vid}", headers=ADMIN,
                                json={"verein_name": "Meta-Verein e.V.", "rubrik": "Sonstiges", "heimatort": "Hölskofen", "plz": "84092"})
    m = daten()["_meta"]["meta_verein"]
    pruefe(r.status_code == 200 and m.get("heimatort") == "Hölskofen" and m.get("plz") == "84092" and m.get("rubrik") == "Sonstiges",
           "Heimatort/PLZ/Rubrik in _meta", m)
    pruefe(m.get("gemeinde") == "Bayerbach" and m.get("selbstverwaltung") is True, "Gemeinde neu bestimmt, andere _meta-Felder bleiben", m)
    pruefe(daten()["_labels"]["meta_verein"] == "Meta-Verein e.V.", "Label umbenannt")
    with vk_db.db_conn() as c:
        row = c.execute("SELECT heimatort, gemeinde FROM vereine_accounts WHERE id=?", (vid,)).fetchone()
    pruefe(row["heimatort"] == "Hölskofen" and row["gemeinde"] == "Bayerbach", "DB ebenfalls aktualisiert", dict(row))
    api = app.test_client().get("/api/termine").get_json()
    pruefe(api["meta"]["meta_verein"]["heimatort"] == "Hölskofen", "öffentliche API zeigt neuen Heimatort")
    app.test_client().patch(f"/api/admin/verein/{vid}", headers=ADMIN, json={"verein_name": "", "heimatort": ""})
    pruefe(daten()["_labels"]["meta_verein"] == "Meta-Verein e.V." and "heimatort" not in daten()["_meta"]["meta_verein"],
           "leerer Name wird ignoriert, geleerter Heimatort entfernt")


# ── 9: Verknüpfen setzt Alias (Admin + Telegram gleich) ─────────────────────
def test_verknuepfen():
    print("\n9 · Key-Übertragung")
    vid, uid = verein_anlegen("Schützen Hölskofen", "schuetzen_hk", "s@hk.de")
    d = daten()
    d["_labels"].update({"schuetzenverein_hoelskofen": "Schützenverein Hölskofen", "schuetzen_hk": "Schützen Hölskofen"})
    d["schuetzenverein_hoelskofen"] = [{"datum": "2099-03-01", "bezeichnung": "Preisschießen"}]
    d["schuetzen_hk"] = []
    d["_meta"]["schuetzenverein_hoelskofen"] = {"heimatort": "Hölskofen"}
    d.setdefault("_heimat_aliases", {})["alt_schuetzen"] = "schuetzenverein_hoelskofen"
    schreibe(d)
    with vk_db.db_conn() as c:
        c.execute("INSERT INTO tg_subscriptions (chat_id, verein_key) VALUES ('1','schuetzenverein_hoelskofen'),"
                  "('1','schuetzen_hk'),('2','schuetzenverein_hoelskofen')")
    body = {"callback_query": {"id": "1", "data": f"vk_link:{vid}:schuetzenverein_hoelskofen", "from": {"id": 4711}}}
    r = app.test_client().post("/telegram", json=body, headers={"X-Telegram-Bot-Api-Secret-Token": "tgsecret"})
    d = daten()
    pruefe(r.status_code == 200 and len(d["schuetzen_hk"]) == 1 and "schuetzenverein_hoelskofen" not in d,
           "Telegram-Verknüpfen überträgt Termine")
    pruefe(d["_heimat_aliases"].get("schuetzenverein_hoelskofen") == "schuetzen_hk"
           and d["_heimat_aliases"].get("alt_schuetzen") == "schuetzen_hk", "Alias gesetzt, Alias-Kette umgebogen")
    pruefe(d["_meta"]["schuetzen_hk"].get("heimatort") == "Hölskofen", "_meta übernommen")
    with vk_db.db_conn() as c:
        abos = sorted((r["chat_id"], r["verein_key"]) for r in c.execute("SELECT * FROM tg_subscriptions"))
    pruefe(abos == [("1", "schuetzen_hk"), ("2", "schuetzen_hk")], "Abos umgezogen ohne Duplikat-Fehler", abos)
    r = app.test_client().post(f"/api/admin/verein/{vid}/transfer-key", headers=ADMIN, json={"source_key": "_meta"})
    pruefe(r.status_code == 400 and "_meta" in daten(), "interne Keys (_meta …) nicht übertragbar")


# ── 10: iCal-Feed-UIDs eindeutig ────────────────────────────────────────────
def test_ical_uids():
    print("\n10 · iCal-UIDs")
    import services.kalender.routes as kr
    (TMP / "gottesdienste.json").write_text(json.dumps({"hk": [
        {"datum": "2099-04-05", "uhrzeit": "08:30", "ort": "Hölskofen", "art": "Hl. Messe"},
        {"datum": "2099-04-05", "uhrzeit": "10:00", "ort": "Postau", "art": "Hl. Messe"}]}))
    import shared.kalender_core as kc
    kc.GOTTESDIENSTE_FILE = TMP / "gottesdienste.json"
    ics = app.test_client().get("/api/ical/feed?v=pfarrgemeinde").get_data(as_text=True)
    uids = re.findall(r"^UID:(.+)$", ics, re.M)
    pruefe(len(uids) == 2 and len(set(uids)) == 2, "zwei Messen am selben Tag → zwei UIDs", uids)
    pruefe(uids[0].strip() == "2099-04-05-hlmesse-pfarrgemeinde@vereinskalender", "erste behält bisherige UID", uids[0])
    ics2 = app.test_client().get("/api/ical/feed?v=pfarrgemeinde").get_data(as_text=True)
    pruefe(re.findall(r"^UID:(.+)$", ics2, re.M) == uids, "UIDs stabil zwischen Abrufen")
    (TMP / "gottesdienste.json").unlink()


# ── 11: Registrierung übernimmt keinen bestehenden Kalender-Key ─────────────
def test_registrierung_key():
    print("\n11 · Registrierung ohne Key-Übernahme")
    d = daten(); d["_labels"]["kljb_postau"] = "KLJB Postau"; d["kljb_postau"] = [{"datum": "2099-01-01", "bezeichnung": "x"}]
    schreibe(d)
    import services.auth.routes as ar
    with vk_db.db_conn() as c:
        k = ar._unique_verein_key(c, "KLJB Postau")
    pruefe(k == "kljb_postau_1", "freier Key statt kljb_postau", k)


# ── 12: Bezahlte Fremd-Endpunkte nicht über VKO-Domains ─────────────────────
def test_fremde_endpunkte():
    print("\n12 · Verkehr/Aktien/Autoquartett über VKO-Domain gesperrt")
    import services.verkehr.routes as vr
    aufrufe = []
    alt = vr.get_route
    vr.get_route = lambda o, d: aufrufe.append((o, d)) or {"normal_sek": 600, "traffic_sek": 600, "dist_m": 1000,
        "overview_polyline": "", "start_name": o, "end_name": d, "traffic": []}
    try:
        cl = app.test_client()
        for host in ("vereinskalender.online", "www.veranstaltungen.website"):
            r = cl.get("/api/verkehr?origin=a&destination=b", headers={"Host": host})
            pruefe(r.status_code == 404, f"/api/verkehr über {host} → 404")
        pruefe(cl.post("/aktien-lookup", json={"ticker": "X"}, headers={"Host": "vereinskalender.online"}).status_code == 404,
               "/aktien-lookup über VKO → 404")
        pruefe(cl.post("/autoquartett/car-lookup", json={"name": "X"}, headers={"Host": "vereinskalender.online"}).status_code == 404,
               "/autoquartett über VKO → 404")
        pruefe(not aufrufe, "kein TomTom-Aufruf ausgelöst")
        r = cl.get("/api/verkehr?origin=a&destination=b", headers={"Host": "umbenennen.duckdns.org"})
        pruefe(r.status_code == 200 and aufrufe, "über umbenennen.duckdns.org unverändert erreichbar")
        pruefe(cl.get("/api/termine", headers={"Host": "vereinskalender.online"}).status_code == 200, "VKO-API unberührt")
    finally:
        vr.get_route = alt


# ── 13: Pending-Datei atomar (auch wenn root sie angelegt hat) ──────────────
def test_pending_atomar():
    print("\n13 · Pending-Datei bei Teilbestätigung")
    import heimat_import as hi
    hi.PENDING_DIR = TMP / "imports"
    hi.LOG_FILE = str(TMP / "heimat.log")
    hi.LAST_IMPORT_FILE = TMP / "last_import.json"
    ev = lambda k, b: {"_verein_key": k, "_label": k, "_gemeinde": "Testgem", "datum": "2099-07-0" + str(len(b) % 9 + 1),
                       "uhrzeit": "", "bezeichnung": b, "ort": "", "_neu": True}
    pf = hi.PENDING_DIR / "heimat_pending_abc123.json"
    pf.write_text(json.dumps({"uid": "abc123", "events": [ev("verein_a", "Sommerfest A"), ev("verein_b", "Herbstfest B")]}))
    pf.chmod(0o444)   # wie eine fremde (root-)Datei: nicht direkt beschreibbar
    try:
        hi.do_import("abc123", ["verein_a"])
        rest = json.loads(pf.read_text())["events"]
        pruefe([e["_verein_key"] for e in rest] == ["verein_b"], "Rest-Pending trotz schreibgeschützter Datei aktualisiert", rest)
        pruefe(not list(hi.PENDING_DIR.glob("*.tmp")), "keine .tmp-Reste")
        hi.do_reject("abc123", ["verein_b"])
        pruefe(not pf.exists(), "nach Verwerfen des Rests gelöscht")
    finally:
        if pf.exists():
            pf.chmod(0o644)
    hi.LOG_FILE = "/nicht/vorhanden/x.log"
    hi._log("geht nicht in Datei")
    pruefe(True, "_log bricht bei nicht schreibbarer Log-Datei nicht ab")


# ── Rest: Import behält vergangene Termine ──────────────────────────────────
def test_import_vergangenheit():
    print("\nR1 · Import behält vergangene Termine")
    from shared.kalender_core import _do_save_import
    d = daten(); d["_labels"]["hist"] = "Hist"
    d["hist"] = [{"id": "h1", "datum": "2020-01-01", "bezeichnung": "Alt"},
                 {"id": "h2", "datum": "2099-01-01", "bezeichnung": "Zukunft", "geloescht": True}]
    schreibe(d)
    _do_save_import([{"verein": "Hist", "datum": "2099-01-01", "bezeichnung": "Zukunft"},
                     {"verein": "Hist", "datum": "2099-02-01", "bezeichnung": "Neu"},
                     {"verein": "Hist", "datum": "2019-01-01", "bezeichnung": "Uralt"}], "", "", verein_key="hist")
    b = [t["bezeichnung"] for t in daten()["hist"]]
    pruefe(b == ["Alt", "Zukunft", "Neu"], "Vergangenes bleibt, Gelöschtes sperrt Duplikat, alte neue nicht übernommen", b)


# ── Rest: Statistik-Zeitzone ────────────────────────────────────────────────
def test_stats_zeit():
    print("\nR2 · Statistik liest Log-Offset")
    import services.kalender.routes as kr
    dt = kr._stats_parse_dt('1.2.3.4 - - [04/Oct/2026:23:30:00 +0200] "GET / HTTP/1.1"')
    pruefe(dt is not None and dt.utcoffset() == timedelta(hours=2) and dt.hour == 23, "Offset +0200 übernommen", dt)
    from zoneinfo import ZoneInfo
    pruefe(dt.astimezone(ZoneInfo("Europe/Berlin")).date().isoformat() == "2026-10-04", "23:30 Ortszeit bleibt am selben Tag")


# ── Rest: Cookies ───────────────────────────────────────────────────────────
def test_cookies():
    print("\nR3 · Cookie-Flags")
    import services.auth.routes as ar
    src = (ROOT / "services" / "auth" / "routes.py").read_text()
    pruefe(src.count("secure=_COOKIE_SECURE") == 3, "vk_session/vk_preauth mit Secure-Flag (3 Stellen)")
    pruefe(os.environ.get("VKO_COOKIE_INSECURE") == "1" and not ar._COOKIE_SECURE, "Testmodus ohne Secure (nur per Umgebungsvariable)")
    pruefe(app.config["SESSION_COOKIE_SAMESITE"] == "Lax", "Flask-Session SameSite=Lax")


# ── Rest: Freigabe-Nachricht + Callback ─────────────────────────────────────
def test_freigabe_nachricht():
    print("\nR4 · Freigabe per Telegram")
    import services.auth.routes as ar
    gesendet = {}
    alt = ar.send_telegram_inline
    ar.send_telegram_inline = lambda chat, text, kb, parse_mode=None: gesendet.update(text=text, kb=kb, pm=parse_mode)
    try:
        name = "Förderverein <Grundschule> Hölskofen & Postau e.V."
        ar._telegram_approve_msg(77, name, "a@b.de", rubrik="Verein")
    finally:
        ar.send_telegram_inline = alt
    cbs = [b["callback_data"] for b in gesendet["kb"][0]]
    pruefe(gesendet["pm"] == "HTML" and "&lt;Grundschule&gt;" in gesendet["text"] and "&amp;" in gesendet["text"],
           "HTML-Modus mit escaptem Namen")
    pruefe(all(len(c.encode()) <= 64 for c in cbs), "callback_data ≤ 64 Byte", cbs)
    # Callback mit neuem und altem Namensformat
    from shared.telegram import cb_name
    for i, fmt in enumerate((cb_name, lambda n: n[:30].replace(":", "_"))):
        with vk_db.db_conn() as c:
            vid = c.execute("INSERT INTO vereine_accounts (verein_key, verein_name, status) VALUES (?,?,'pending') RETURNING id",
                            (f"frei_{i}", name)).fetchone()["id"]
            c.execute("INSERT INTO vk_users (email, password_hash, verein_id, role) VALUES (?,?,?, 'admin')",
                      (f"f{i}@x.de", "x", vid))
        body = {"callback_query": {"id": "1", "data": f"verein_approve:{vid}:{fmt(name)}", "from": {"id": 4711}}}
        app.test_client().post("/telegram", json=body, headers={"X-Telegram-Bot-Api-Secret-Token": "tgsecret"})
        with vk_db.db_conn() as c:
            st = c.execute("SELECT status FROM vereine_accounts WHERE id=?", (vid,)).fetchone()["status"]
        pruefe(st == "aktiv", f"Freigabe-Callback ({'neues' if i == 0 else 'altes'} Format)", st)


# ── Rest: Store-Lock über Prozessgrenzen ────────────────────────────────────
def test_store_mehrprozess():
    print("\nR5 · KalenderStore mit zwei Prozessen")
    import subprocess
    datei = TMP / "mp.json"
    datei.write_text(json.dumps({"_labels": {}, "mp": []}))
    code = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); from pathlib import Path\n"
            f"import shared.kalender_store as k; k.VEREINSTERMINE_FILE = Path({str(datei)!r})\n"
            "for i in range(40):\n"
            "    k.KalenderStore.update(lambda d: d['mp'].append({'datum': '2099-01-01', 'bezeichnung': sys.argv[1] + str(i)}))\n")
    procs = [subprocess.Popen([sys.executable, "-c", code, n]) for n in ("a", "b")]
    for p in procs:
        p.wait()
    n = len(json.loads(datei.read_text())["mp"])
    pruefe(n == 80 and all(p.returncode == 0 for p in procs), "80 von 80 Einträgen, kein Lost Update", n)


# ── Rest: Kalender-Bot-Tastatur seitenweise ─────────────────────────────────
def test_bot_tastatur():
    print("\nR6 · Kalender-Bot /abo seitenweise")
    import services.kalender_bot.routes as kb
    labels = {f"v{i:03d}": f"Verein {i:03d}" for i in range(138)}
    alt = kb.tg_get_subscriptions
    kb.tg_get_subscriptions = lambda c: ["v000"]
    try:
        s0 = kb._verein_auswahl_keyboard("1", labels)
        s3 = kb._verein_auswahl_keyboard("1", labels, 3)
    finally:
        kb.tg_get_subscriptions = alt
    knoepfe = lambda kbd: sum(len(r) for r in kbd)
    pruefe(knoepfe(s0) <= 45 and knoepfe(s3) <= 45, "höchstens 45 Knöpfe je Nachricht", (knoepfe(s0), knoepfe(s3)))
    vereine = lambda kbd: [b["callback_data"] for r in kbd for b in r if b["callback_data"].startswith("vk_abo:")]
    alle = set()
    for n in range(4):
        kb.tg_get_subscriptions = lambda c: []
        alle |= set(vereine(kb._verein_auswahl_keyboard("1", labels, n)))
    kb.tg_get_subscriptions = alt
    pruefe(len(alle) == 138, "alle 138 Vereine über 4 Seiten erreichbar", len(alle))
    pruefe(s0[0][0]["text"].startswith("✅") and any(b["callback_data"] == "vk_seite:1" for r in s0 for b in r),
           "Abo markiert, Weiter-Knopf vorhanden")


# ── 19: Favoriten-Abo nach der Mischregel (ADR-025) ─────────────────────────
def test_abo_mischregel():
    print("\n19 · Favoriten-Abo: Ort des Termins + Vereinssitz, Pfarreien nur am Ort")
    import shared.kalender_core as kc
    d = daten()
    d["_labels"]["kp"] = "Königstreue Patrioten Hölskofen"
    d["_meta"]["kp"] = {"heimatort": "Hölskofen", "gemeinde": "Bayerbach", "landkreis": "Landkreis Landshut"}
    d["kp"] = [{"datum": "2099-06-06", "uhrzeit": "19:30", "bezeichnung": "Monatsversammlung Test",
                "ort": "Gasthaus Pritscher Paindlkofen"}]
    schreibe(d)
    (TMP / "gottesdienste.json").write_text(json.dumps({"hk": [
        {"datum": "2099-06-07", "uhrzeit": "08:30", "ort": "Hölskofen", "art": "Messe Hoelskofen Test"}]}))
    kc.GOTTESDIENSTE_FILE = TMP / "gottesdienste.json"

    def titel(q):
        ics = app.test_client().get("/api/ical/feed?" + q).get_data(as_text=True)
        return set(re.findall(r"^SUMMARY:(.+?)\r?$", ics, re.M))
    mv, messe = "Monatsversammlung Test", "Messe Hoelskofen Test"
    t = titel("o=H%C3%B6lskofen%7CBayerbach")
    pruefe(any(mv in x for x in t), "Ortschaft Hölskofen: Versammlung in Paindlkofen (Vereinssitz)", t)
    pruefe(any(messe in x for x in t), "Ortschaft Hölskofen: Messe vor Ort", t)
    t = titel("o=Postau%7CPostau")
    pruefe(not any(messe in x for x in t), "Ortschaft Postau: keine Messe aus Hölskofen (Pfarrei nur am Ort)", t)
    t = titel("o=Paindlkofen%7CErgoldsbach")
    pruefe(any(mv in x for x in t), "Ortschaft Paindlkofen: Versammlung (Ort des Termins)", t)
    t = titel("g=Ergoldsbach%7CLandkreis%20Landshut")
    pruefe(any(mv in x for x in t) and not any(messe in x for x in t), "Gemeinde Ergoldsbach: Versammlung, keine Messe", t)
    t = titel("r=Straubing-Bogen")
    pruefe(not any(mv in x or messe in x for x in t), "Region Straubing-Bogen: nichts davon", t)
    t = titel("r=Landkreis%20Landshut")
    pruefe(any(mv in x for x in t), "Region im alten Format „Landkreis Landshut“ (Favorit vor v1.61)", t)
    t = titel("v=kp")
    pruefe(any(mv in x for x in t) and not any(messe in x for x in t), "nur Verein kp: unverändert Verein-basiert", t)
    t = titel("ort=H%C3%B6lskofen")
    pruefe(any(mv in x for x in t) and any(messe in x for x in t), "altes ?ort= folgt derselben Regel", t)
    t = titel("v=kp&o=Postau%7CPostau")
    pruefe(any(mv in x for x in t), "Verein ODER Ortschaft kombiniert", t)
    (TMP / "gottesdienste.json").unlink()


def test_chips_ohne_onclick():
    print("\n19c · Favoriten-/Rubrik-Chips ohne Daten im onclick (v1.63)")
    html_ = (BASIS / "kalender.html").read_text() if "BASIS" in globals() else Path(__file__).resolve().parent.parent.joinpath("kalender.html").read_text()
    pruefe("removeFavorite('${" not in html_, "kein removeFavorite('${…}') im Markup")
    pruefe("setRubrik('${" not in html_, "kein setRubrik('${…}') im Markup")


def test_rename_relevanz():
    print("\n19b · Rename: Rechnungs-Relevanz verträgt Nicht-Text aus der KI-Antwort")
    from services.rename.routes import _ist_rechnungsrelevant as rel
    pruefe(rel("2026-10-05_Rechnung_Firma.pdf", 17) is True, "Zahl statt Kategorie bricht nicht ab")
    pruefe(rel("2026-10-05_Kontakt_X.pdf", None) is False, "Kategorie aus Dateiname: Kontakt")


# ── 20: heimat-Import schreibt keine Gemeinde als Ortschaft (Todo #417) ─────
def test_import_ortschaft():
    print("\n20 · heimat-Import: Ortschaft nicht aus dem Seitennamen")
    import heimat_import as hi
    hi.PENDING_DIR = TMP / "imports"
    hi.LOG_FILE = str(TMP / "heimat.log")
    hi.LAST_IMPORT_FILE = TMP / "last_import.json"
    ev = {"_verein_key": "ff_greils", "_label": "FF Greilsberg", "_gemeinde": "Bayerbach",
          "_gemeinde_amtlich": "Bayerbach", "datum": "2099-08-01", "uhrzeit": "", "bezeichnung": "Gartenfest Test",
          "ort": "GH FF Greilsberg", "_neu": True}
    (hi.PENDING_DIR / "heimat_pending_ort417.json").write_text(json.dumps({"uid": "ort417", "events": [ev]}))
    hi.do_import("ort417")
    d = daten()
    t = [x for x in d.get("ff_greils", []) if x["bezeichnung"] == "Gartenfest Test"]
    pruefe(t and not t[0].get("ortschaft"), "Feld ortschaft leer statt „Bayerbach“", t)
    pruefe(d.get("_ortschaften", {}).get("gemeinde_map", {}).get("Bayerbach") == "Bayerbach",
           "gemeinde_map lernt weiter über den Seitennamen", d.get("_ortschaften"))


# ── 21: Register prüfen + offene Admin-Aufgaben (Todo #417) ─────────────────
def test_register_pruefen():
    print("\n21 · Register prüfen, offene Admin-Aufgaben für den 20-Uhr-Bericht")
    from shared.admin_aufgaben import register_pruefung, offene_aufgaben, aufgaben_text
    c = app.test_client()
    pruefe(c.get("/api/admin/register").status_code == 401, "ohne Token 401")
    vorher = c.get("/api/admin/register", headers=ADMIN).get_json()
    pruefe(len(vorher["offen"]) > 0, "unbestätigte Register-Einträge gelistet", len(vorher["offen"]))
    e = vorher["offen"][0]
    r = c.post("/api/admin/register", headers=ADMIN, json={"ort": e["ort"], "gemeinde": e["gemeinde"], "ok": True})
    nachher = c.get("/api/admin/register", headers=ADMIN).get_json()
    pruefe(r.status_code == 200 and len(nachher["offen"]) == len(vorher["offen"]) - 1 and nachher["bestaetigt"] == 1,
           "„Stimmt“ nimmt den Eintrag aus der Liste", nachher["bestaetigt"])
    pruefe("_orte_geprueft" in daten(), "Urteil steht in vereinstermine.json, nicht in orte.json")
    e2 = nachher["offen"][0]
    r = c.post("/api/admin/register", headers=ADMIN, json={"ort": e2["ort"], "gemeinde": e2["gemeinde"], "ok": False})
    pruefe(r.status_code == 400, "„Falsch“ ohne Notiz abgelehnt")
    c.post("/api/admin/register", headers=ADMIN, json={"ort": e2["ort"], "gemeinde": e2["gemeinde"], "ok": False,
                                                      "notiz": "gehört zu Postau"})
    d = c.get("/api/admin/register", headers=ADMIN).get_json()
    pruefe(len(d["falsch"]) == 1 and d["falsch"][0]["notiz"] == "gehört zu Postau", "„Falsch“ mit Notiz gemeldet", d["falsch"])
    r = c.post("/api/admin/register", headers=ADMIN, json={"ort": "Gibtsnicht", "gemeinde": "Nirgends", "ok": True})
    pruefe(r.status_code == 400, "unbekannter Eintrag abgelehnt")
    c.delete("/api/admin/register", headers=ADMIN, json={"ort": e2["ort"], "gemeinde": e2["gemeinde"]})
    pruefe(not c.get("/api/admin/register", headers=ADMIN).get_json()["falsch"], "Zurücknehmen löscht das Urteil")
    a = offene_aufgaben(daten(), TMP / "imports", vk_db.DB_FILE)
    pruefe(a["register"] == len(d["offen"]) + 1 and isinstance(a["vereine"], int), "Zähler für den Bericht", a)
    pruefe(aufgaben_text({"importe": 0, "vereine": 0, "orte": 0, "register": 0, "register_falsch": 3}) == "",
           "nichts für Josef offen → kein Abschnitt")
    pruefe(aufgaben_text({"importe": 1, "vereine": 2, "orte": 0, "register": 19}) ==
           "1 Import bestätigen · 2 Vereine freigeben · 19 Register-Einträge prüfen", "Text im Bericht")


# ── 22: PLZ-Wächter (Todo #419) ─────────────────────────────────────────────
def test_plz_check():
    print("\n22 · PLZ-Wächter: nur bei neuem Export rechnen, Vergleich, Meldung")
    import plz_check as pc
    alt = {"_stand": "2026-10-02", "plz": {"84092": {"g": ["A"]}, "84061": {"g": ["B"]}, "99999": {"g": ["C"]}},
           "gemeinden": {"A": {"name": "Bayerbach"}, "B": {"name": "Ergoldsbach"}, "C": {"name": "Weg"}}}
    neu = {"plz": {"84092": {"g": ["A", "B"]}, "84061": {"g": ["B"]}, "11111": {"g": ["A"]}},
           "gemeinden": {"A": {"name": "Bayerbach"}, "B": {"name": "Ergoldsbach"}}}
    d = pc.vergleiche(alt, neu)
    pruefe(d["geaendert"] == {"84092": (["Bayerbach"], ["Bayerbach", "Ergoldsbach"])}
           and d["neu"] == ["11111"] and d["weg"] == ["99999"], "Vergleich: geändert/neu/weg", d)
    with vk_db.db_conn() as c:
        c.execute("INSERT INTO vereine_accounts (verein_name, status, plz) VALUES ('PLZ-Testverein', 'aktiv', '84092')")
    alt_db = pc.DB_PATH; pc.DB_PATH = vk_db.DB_FILE
    b = pc.betroffene(d)
    pc.DB_PATH = alt_db
    pruefe(("PLZ-Testverein", "84092") in b["konten"], "betroffenes Vereinskonto erkannt", b["konten"])
    pruefe(any(p == "84092" for _, p in b["register"]), "betroffene Register-Einträge (orte.json) erkannt", b["register"][:3])
    t = pc.meldung({"datum": "2026-11-01", "sha": "x"}, "2026-10-02", d, b)
    pruefe("84092: Bayerbach → Bayerbach, Ergoldsbach" in t and "PLZ-Testverein (84092)" in t and "<b>" not in t,
           "Meldung nennt Änderung und Konto, ohne HTML", t[:200])
    # Kein neuer Export → kein Download, keine Meldung
    snap, stand, export = pc.SNAPSHOT, pc.STAND_FILE, pc.letzter_export
    pc.SNAPSHOT = TMP / "plz_snap.json"; pc.SNAPSHOT.write_text(json.dumps(alt))
    pc.STAND_FILE = TMP / "plz_stand.json"
    pc.letzter_export = lambda: {"sha": "abc", "datum": "2025-11-22"}
    sys.argv = ["plz_check.py"]
    gebaut = []
    import types as _t
    sys.modules["build_plz_gemeinden"] = _t.SimpleNamespace(lade_gemeinden=lambda: gebaut.append(1) or {})
    pruefe(pc.main() == 0 and not gebaut, "Export älter als Schnappschuss → nichts geladen")
    pc.STAND_FILE.write_text(json.dumps({"sha": "abc", "gemeldet_am": "2026-11-01"}))
    pc.letzter_export = lambda: {"sha": "abc", "datum": "2026-11-01"}
    pruefe(pc.main() == 0 and not gebaut, "schon gemeldeter Export → nichts geladen")
    pc.SNAPSHOT, pc.STAND_FILE, pc.letzter_export = snap, stand, export
    del sys.modules["build_plz_gemeinden"]


# ── 23: Quelle „Pfarrbrief“ (Josef 2026-10-05) ──────────────────────────────
def test_quelle_pfarrbrief():
    print("\n23 · Quelle „Pfarrbrief“: Gottesdienste und Standard-Quelle aus _meta")
    import shared.kalender_core as kc
    d = daten()
    d["_labels"]["pfarrgemeinde_test"] = "Pfarrgemeinde Test"
    d["_meta"]["pfarrgemeinde_test"] = {"quelle": "Pfarrbrief"}
    d["pfarrgemeinde_test"] = [{"datum": "2099-09-01", "uhrzeit": "19:00", "ort": "Postau", "bezeichnung": "Messe A"},
                               {"datum": "2099-09-02", "uhrzeit": "19:00", "ort": "Postau", "bezeichnung": "Messe B",
                                "quelle": "eigene"}]
    schreibe(d)
    (TMP / "gottesdienste.json").write_text(json.dumps({"hk": [
        {"datum": "2099-09-03", "uhrzeit": "08:30", "ort": "Hölskofen", "art": "Messe C"}]}))
    kc.GOTTESDIENSTE_FILE = TMP / "gottesdienste.json"
    T = {t["bezeichnung"]: t.get("quelle", "") for t in app.test_client().get("/api/termine").get_json()["termine"]}
    pruefe(T.get("Messe A") == "Pfarrbrief", "Standard-Quelle aus _meta eingesetzt", T.get("Messe A"))
    pruefe(T.get("Messe B") == "eigene", "eigene Quelle bleibt", T.get("Messe B"))
    pruefe(T.get("Messe C") == "Pfarrbrief", "Gottesdienste aus gottesdienste.json: Pfarrbrief", T.get("Messe C"))
    (TMP / "gottesdienste.json").unlink()


# ── 24: Verdacht auf Doppeleintrag + Abruf-Schalter je Verein (ADR-027) ──────
def test_verdacht_und_schalter():
    print("\n24 · Verdacht auf Doppeleintrag, Abruf-Schalter, Admin-Übersicht")
    import heimat_import as hi
    hi.PENDING_DIR = TMP / "imports"
    hi.LOG_FILE = str(TMP / "heimat.log")
    hi.LAST_IMPORT_FILE = TMP / "last_import.json"
    d = daten()
    d["_labels"].update({"ffw_sv": "FFW SV", "tsv_nur": "TSV Nur", "ffw_aus": "FFW Aus"})
    d["_meta"].update({"ffw_sv": {"selbstverwaltung": True}, "tsv_nur": {}, "ffw_aus": {"selbstverwaltung": True}})
    d["ffw_sv"] = [{"id": "s1", "datum": "2099-05-01", "uhrzeit": "18:00", "bezeichnung": "Weinfest der FFW",
                    "erstellt_von": "a@b.de"}]
    d["tsv_nur"] = [{"datum": "2099-05-01", "uhrzeit": "14:00", "bezeichnung": "Turnier", "quelle": "heimat-info.de",
                     "quelle_url": "https://x"}]
    schreibe(d)
    g = {"name": "Testgem", "verein_key": "gemeinde_test", "label": "Veranstaltungen Testgem", "url": "https://x"}
    def roh(verein, datum, uhr, titel):
        return {"_verein_name": verein, "datum": datum, "uhrzeit": uhr, "bezeichnung": titel, "ort": ""}
    tage = hi._tage_je_verein(d)
    existing = hi._existing_from_data(d)
    e1 = roh("FFW SV", "2099-05-01", "19:00", "Weinfest")              # umbenannt + verschoben → Verdacht
    e2 = roh("FFW SV", "2099-05-02", "19:00", "Kirchweih")             # anderer Tag → eindeutig neu
    e3 = roh("TSV Nur", "2099-05-01", "16:00", "Sommerfest")           # pflegt nicht selbst → kein Verdacht
    e4 = roh("FFW SV", "2099-05-01", "18:00", "Weinfest der FFW")      # exakt vorhanden → Duplikat
    for e in (e1, e2, e3, e4):
        hi._zuordnen(e, g, {}, d["_meta"], existing, tage)
    pruefe(e1["_neu"] and e1["_verdacht"] == "Weinfest der FFW 18:00", "Verdacht: selbst pflegend + selber Tag", e1)
    pruefe(e2["_neu"] and not e2["_verdacht"], "anderer Tag: kein Verdacht")
    pruefe(e3["_neu"] and not e3["_verdacht"], "Verein ohne Selbstpflege: kein Verdacht")
    pruefe(not e4["_neu"] and not e4["_verdacht"], "exaktes Duplikat bleibt Duplikat, kein Verdacht")

    # Telegram-Weg: Verdacht bleibt liegen, Rest wird übernommen
    pf = hi.PENDING_DIR / "heimat_pending_verd01.json"
    pf.write_text(json.dumps({"uid": "verd01", "events": [e1, e2, e3, e4]}))
    gesendet = []
    alt = hi.send_telegram_inline
    hi.send_telegram_inline = lambda tok, chat, msg, kb: gesendet.append((msg, kb))
    try:
        hi._sende_vorschau({"uid": "verd01", "neu": 3, "duplikate": 1, "sv": 3, "gesamt": 4,
                            "fehler": [], "hinweise": [], "ki": []}, {"TOKEN": "t", "CHAT_ID": "1"})
    finally:
        hi.send_telegram_inline = alt
    msg, kb = gesendet[0]
    pruefe("mögliche Doppelte" in msg and "Weinfest der FFW 18:00" in msg, "Telegram-Vorschau nennt den Verdacht", msg[-300:])
    pruefe(kb[0][0]["text"].startswith("✅ 2 importieren"), "Knopf zählt nur eindeutige Termine", kb[0][0]["text"])
    res = hi.do_import("verd01", mit_verdacht=False)
    T = {t["bezeichnung"] for k in ("ffw_sv", "tsv_nur") for t in daten()[k]}
    pruefe("Kirchweih" in T and "Sommerfest" in T and "Weinfest" not in T, "Telegram-Import ohne Verdachtsfall", T)
    rest = json.loads(pf.read_text())["events"] if pf.exists() else []
    pruefe([e["bezeichnung"] for e in rest] == ["Weinfest"], "Verdachtsfall bleibt im Pending für den Admin", rest)
    pruefe("1 mögliche Doppelte bleiben offen" in res, "Rückmeldung nennt offene Verdachtsfälle", res)
    # Admin-Ansicht: Verdacht wird markiert geliefert
    imp = app.test_client().get("/api/admin/importe", headers=ADMIN).get_json()
    eintrag = next(i for i in imp if i["uid"] == "verd01")
    pruefe(eintrag["verdacht"] == 1 and eintrag["vereine"][0]["termine"][0]["verdacht"] == "Weinfest der FFW 18:00",
           "Admin-Importe liefern den Verdacht je Termin", eintrag)
    hi.do_import("verd01")      # Admin hakt an → wird übernommen
    pruefe(any(t["bezeichnung"] == "Weinfest" for t in daten()["ffw_sv"]) and not pf.exists(),
           "Admin-Bestätigung übernimmt den Verdachtsfall")

    # Schalter im Dashboard
    vid, uid = verein_anlegen("FFW Aus", "ffw_aus", "aus@example.org")
    mid = user_anlegen(vid, "mitglied-aus@example.org")
    cl, _ = client_fuer(uid)
    t = cl.get("/verein/dashboard").get_data(as_text=True)
    pruefe("Termine von Gemeinde-Webseiten" in t and "Abruf ausschalten" in t, "Dashboard zeigt Schalter (Status an)")
    r = cl.post("/verein/crawler", data={"aus": "1"})
    pruefe(r.status_code == 403 and not daten()["_meta"]["ffw_aus"].get("crawler_aus"), "ohne CSRF abgelehnt")
    mcl, _ = client_fuer(mid)
    r = mcl.post("/verein/crawler", data={"_csrf": csrf(mcl), "aus": "1"})
    pruefe(r.status_code == 403, "Mitglied ohne Adminrolle darf nicht schalten")
    pruefe("Abruf ausschalten" not in mcl.get("/verein/dashboard").get_data(as_text=True), "Mitglied sieht keinen Knopf")
    r = cl.post("/verein/crawler", data={"_csrf": csrf(cl), "aus": "1"})
    pruefe(r.status_code == 302 and daten()["_meta"]["ffw_aus"].get("crawler_aus") is True, "Admin schaltet aus")
    pruefe(daten()["_meta"]["ffw_aus"].get("selbstverwaltung") is True, "andere _meta-Felder bleiben")
    pruefe("Abruf wieder einschalten" in cl.get("/verein/dashboard").get_data(as_text=True), "Dashboard zeigt Aus")

    # Abruf aus: Termine fallen beim Bestätigen weg (Schalter nach dem Abruf umgelegt)
    e5 = roh("FFW Aus", "2099-06-01", "10:00", "Maibaum")
    hi._zuordnen(e5, g, {}, daten()["_meta"], set(), {})
    pruefe(e5["_aus"], "_zuordnen erkennt abgeschalteten Abruf")
    e5["_aus"] = False   # wie ein Pending von vor dem Umschalten
    pf2 = hi.PENDING_DIR / "heimat_pending_aus001.json"
    pf2.write_text(json.dumps({"uid": "aus001", "events": [e5]}))
    res = hi.do_import("aus001")
    pruefe(not daten().get("ffw_aus") and "abgeschaltetem Abruf" in res, "do_import übernimmt nichts bei Abruf aus", res)

    # Admin-Übersicht
    vid2, _ = verein_anlegen("FFW SV", "ffw_sv", "sv@example.org")
    L = {v["verein_key"]: v["pflege"] for v in app.test_client().get("/api/admin/users", headers=ADMIN).get_json()}
    pruefe(L["ffw_aus"]["crawler_aus"] and L["ffw_aus"]["selbst"], "Übersicht: Abruf aus + pflegt selbst", L["ffw_aus"])
    pruefe(L["ffw_sv"] == {"eigene": 1, "crawler": 2, "selbst": True, "crawler_aus": False},
           "Übersicht zählt eigene und Crawler-Termine", L["ffw_sv"])

    r = cl.post("/verein/crawler", data={"_csrf": csrf(cl), "aus": "0"})
    pruefe("crawler_aus" not in daten()["_meta"]["ffw_aus"], "wieder einschalten entfernt das Feld")


# ── Mailversand: Fehler melden, Freigabe-Rückmeldung, „Mail erneut senden“ (2026-10-07) ──
def test_mail_rueckmeldung():
    print("\nM1 · Mailversand: Fehler melden, Freigabe, erneut senden")
    from shared import vk_mail
    import services.telegram.routes as tr
    gemeldet, gesendet, telegram = [], [], []
    alt_melden, alt_send = vk_mail._fehler_melden, vk_mail._send
    vk_mail._fehler_melden = lambda to, sub, grund: gemeldet.append((to, grund))
    try:
        pruefe(vk_mail._send("a@b.de", "Test", "x") is False and gemeldet and "SMTP-Zugang fehlt" in gemeldet[-1][1],
               "fehlender SMTP-Zugang → Telegram-Meldung", gemeldet)
        gemeldet.clear()
        pruefe(vk_mail._send("a@b.de\nBcc: x@y.de", "s", "b") is False and not gemeldet,
               "Header-Injection: abgelehnt, ohne Alarm")
        os.environ.update(BREVO_SMTP_USER="u", BREVO_SMTP_KEY="k")
        import smtplib
        alt_smtp = smtplib.SMTP
        class Kaputt:
            def __init__(self, *a, **k):
                raise smtplib.SMTPAuthenticationError(535, b"Authentication failed")
        smtplib.SMTP = Kaputt
        try:
            pruefe(vk_mail._send("a@b.de", "Test", "x") is False and "SMTPAuthenticationError" in gemeldet[-1][1],
                   "SMTP-Fehler (z. B. inaktiver Schlüssel) → Telegram-Meldung", gemeldet)
        finally:
            smtplib.SMTP = alt_smtp
            os.environ.pop("BREVO_SMTP_USER"); os.environ.pop("BREVO_SMTP_KEY")
    finally:
        vk_mail._fehler_melden = alt_melden

    # Ab hier _send nachgestellt: gelingt oder scheitert je nach Schalter
    klappt = {"ja": True}
    vk_mail._send = lambda to, sub, body: gesendet.append((to, sub, body)) or klappt["ja"]
    alt_tg, alt_cb = tr.send_telegram, tr.answer_telegram_callback
    tr.send_telegram = lambda chat, text: telegram.append(text)
    tr.answer_telegram_callback = lambda cb, text=None: telegram.append("CB:" + str(text))
    from shared.telegram import cb_name
    try:
        def pending(name, mail, bestaetigt):
            with vk_db.db_conn() as c:
                vid = c.execute("INSERT INTO vereine_accounts (verein_key, verein_name, status) VALUES (?,?,'pending') RETURNING id",
                                (name.lower().replace(" ", "_"), name)).fetchone()["id"]
                uid = c.execute("INSERT INTO vk_users (email, password_hash, verein_id, role, email_verified, aktiv)"
                                " VALUES (?,?,?, 'admin', ?, 1) RETURNING id", (mail, "x", vid, bestaetigt)).fetchone()["id"]
            return vid, uid

        def freigeben_tg(vid, name):
            body = {"callback_query": {"id": "1", "data": f"verein_approve:{vid}:{cb_name(name)}", "from": {"id": 4711}}}
            app.test_client().post("/telegram", json=body, headers={"X-Telegram-Bot-Api-Secret-Token": "tgsecret"})

        # Freigabe per Telegram, E-Mail noch unbestätigt → Bestätigungslink statt Willkommens-Mail
        vid, uid = pending("FF Mailtest", "mt@x.de", 0)
        freigeben_tg(vid, "FF Mailtest")
        with vk_db.db_conn() as c:
            tok = c.execute("SELECT verify_token FROM vk_users WHERE id=?", (uid,)).fetchone()["verify_token"]
        pruefe(bool(tok) and "bestätigen" in gesendet[-1][1],
               "unbestätigt: Freigabe schickt neuen Bestätigungslink", gesendet[-1][1])
        pruefe(any("Bestätigungslink verschickt" in t for t in telegram), "Telegram nennt den Bestätigungslink", telegram)
        r = app.test_client().get(f"/api/auth/verify?token={tok}")
        pruefe(r.status_code == 200 and "jetzt anmelden" in r.get_data(as_text=True),
               "Bestätigung nach der Freigabe → „Jetzt anmelden“")

        # Freigabe per Telegram, Mail scheitert → Warnung statt ✅ allein
        telegram.clear(); klappt["ja"] = False
        vid2, uid2 = pending("FF Mailfehler", "mf@x.de", 1)
        freigeben_tg(vid2, "FF Mailfehler")
        pruefe(any("NICHT verschickt" in t for t in telegram) and any("Mail fehlgeschlagen" in t for t in telegram),
               "Mail gescheitert → Telegram warnt", telegram)
        klappt["ja"] = True

        # Freigabe über die Admin-App: Antwort enthält, was mit der Mail passiert ist
        vid3, uid3 = pending("FF Adminfrei", "af@x.de", 1)
        r = app.test_client().post(f"/api/admin/vereine/{vid3}/approve", headers=ADMIN).get_json()
        pruefe(r["mail"] == "willkommen" and r["mail_ok"] and "Willkommens-Mail verschickt" in r["mail_text"],
               "Admin-Freigabe meldet die Willkommens-Mail", r)

        # Admin → Accounts → „Mail erneut senden“
        cl = app.test_client()
        pruefe(cl.post(f"/api/admin/users/{uid}/mail").status_code == 401, "erneut senden nur mit Admin-Token")
        with vk_db.db_conn() as c:
            c.execute("UPDATE vk_users SET email_verified=0 WHERE id=?", (uid,))
        r = cl.post(f"/api/admin/users/{uid}/mail", headers=ADMIN)
        with vk_db.db_conn() as c:
            tok2 = c.execute("SELECT verify_token FROM vk_users WHERE id=?", (uid,)).fetchone()["verify_token"]
        pruefe(r.status_code == 200 and r.get_json()["mail"] == "bestaetigung" and tok2 and tok2 != tok,
               "unbestätigt → neuer Bestätigungslink", r.get_json())
        r = cl.post(f"/api/admin/users/{uid3}/mail", headers=ADMIN).get_json()
        pruefe(r["mail"] == "willkommen" and r["ok"], "bestätigt + freigegeben → Willkommens-Mail", r)
        vid4, uid4 = pending("FF Nochnicht", "nn@x.de", 1)
        pruefe(cl.post(f"/api/admin/users/{uid4}/mail", headers=ADMIN).status_code == 409,
               "bestätigt, aber nicht freigegeben → keine Willkommens-Mail")
        klappt["ja"] = False
        r = cl.post(f"/api/admin/users/{uid3}/mail", headers=ADMIN).get_json()
        pruefe(r["ok"] is False and "NICHT verschickt" in r["mail_text"], "Fehlschlag wird angezeigt", r)
        src = (ROOT / "kalender.html").read_text()
        pruefe('class="acc-mail-btn" data-uid="${u.id}"' in src and "Bestätigungslink senden" in src,
               "Admin: Knopf je Benutzer über data-uid")
    finally:
        vk_mail._send = alt_send
        tr.send_telegram, tr.answer_telegram_callback = alt_tg, alt_cb


def test_mail_lebenszeichen():
    print("\nM2 · Lebenszeichen-Mail")
    reg = json.loads((ROOT / "cron_registry.json").read_text())
    job = [j for j in reg["jobs"] if j["name"] == "mail_lebenszeichen"]
    cron = (ROOT / "deploy" / "cron.d" / "pka-mail-lebenszeichen").read_text()
    pruefe(job and job[0]["days"] == [0] and job[0]["at"] == ["08:00"], "Registry: montags 08:00", job)
    pruefe("0 8 * * 1 root" in cron and "cronwrap.py mail_lebenszeichen --" in cron, "cron.d: Mo 08:00 über cronwrap")


def test_freigabe_gruppe():
    print("\nM3 · Eigene Telegram-Gruppe für VKO-Freigaben")
    import shared.telegram as st
    import services.auth.routes as ar
    import services.telegram.routes as tr
    pruefe(st.freigabe_chat_id() == "4711", "ohne Eintrag: Hauptchat (Rückfall)")
    st.FREIGABE_CHAT_ID = "-1009"
    an, haupt = [], []
    alt_inline, alt_tg = ar.send_telegram_inline, tr.send_telegram
    ar.send_telegram_inline = lambda chat, text, kb, parse_mode=None: an.append(chat)
    tr.send_telegram = lambda chat, text: haupt.append((chat, text))
    try:
        ar._telegram_approve_msg(1, "FF Gruppe", "g@x.de")
        pruefe(an == ["-1009"], "Freigabe-Anfrage geht in die Gruppe", an)
        cl = app.test_client()
        H = {"X-Telegram-Bot-Api-Secret-Token": "tgsecret"}
        def beitritt(von):
            return {"my_chat_member": {"chat": {"id": -1005, "title": "VKO Freigaben", "type": "group"},
                                       "from": {"id": von}, "new_chat_member": {"status": "member"}}}
        cl.post("/telegram", json=beitritt(4711), headers=H)
        pruefe(haupt and haupt[-1][0] == "4711" and "-1005" in haupt[-1][1] and "VKO Freigaben" in haupt[-1][1],
               "Bot meldet Chat-ID der neuen Gruppe im Hauptchat", haupt)
        haupt.clear()
        cl.post("/telegram", json=beitritt(999), headers=H)
        pruefe(not haupt, "von Fremden hinzugefügt: keine Meldung")
        cl.post("/telegram", json={"message": {"chat": {"id": -1009}, "migrate_to_chat_id": -1001234}}, headers=H)
        pruefe(haupt and "-1001234" in haupt[-1][1], "neue ID nach Umwandlung zur Supergruppe wird gemeldet", haupt)
        src = (ROOT / "services" / "auth" / "routes.py").read_text()
        pruefe('os.environ.get("CHAT_ID"' not in src, "Konto-Meldungen nutzen alle freigabe_chat_id()")
    finally:
        st.FREIGABE_CHAT_ID = ""
        ar.send_telegram_inline, tr.send_telegram = alt_inline, alt_tg


TESTS = [test_xss, test_telegram_secret, test_vereine_api_lock, test_erinnerung, test_sessions,
         test_admin_loeschen, test_admin_verein_meta, test_verknuepfen,
         test_ical_uids, test_registrierung_key, test_fremde_endpunkte,
         test_pending_atomar, test_import_vergangenheit, test_stats_zeit, test_cookies,
         test_freigabe_nachricht, test_store_mehrprozess, test_bot_tastatur, test_abo_mischregel, test_chips_ohne_onclick, test_rename_relevanz, test_import_ortschaft, test_register_pruefen, test_plz_check, test_quelle_pfarrbrief,
         test_verdacht_und_schalter, test_mail_rueckmeldung, test_mail_lebenszeichen, test_freigabe_gruppe]

if __name__ == "__main__":
    for t in TESTS:
        t()
    print()
    if _fehler:
        print(f"{len(_fehler)} PRÜFUNG(EN) FEHLGESCHLAGEN")
        sys.exit(1)
    print("ALLE PRÜFUNGEN BESTANDEN")
