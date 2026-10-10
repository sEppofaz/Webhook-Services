import hmac
import html
import os
import secrets
import re
import threading
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from functools import wraps
from urllib.parse import quote

import bcrypt
from flask import Blueprint, jsonify, make_response, redirect, request, session

from shared.vk_db import (
    DS_FASSUNG, SESSION_TIMEOUT_HOURS, create_session, db_conn, delete_session, delete_user_sessions,
    get_session_user, init_db, log_audit,
)
from shared.kalender_core import lookup_plz, _make_verein_key
from shared.rubriken import RUBRIKEN
from shared.csrf import csrf_field, get_csrf_token, validate_csrf
from shared.vk_mail import (
    ANREDEN,
    gruss_aus,
    konto_mail,
    konto_mail_text,
    send_rejected_email,
    send_reset_email,
    send_verify_email,
)
from shared import vk_mail
from shared.flask_notify import send_telegram, send_telegram_inline
from shared.telegram import freigabe_chat_id
from shared.geo import orte_fuer_plz, ortschaft_aufloesen, plz_gueltig

auth_bp = Blueprint("auth", __name__)

UPLOAD_TOKEN = os.environ.get("UPLOAD_TOKEN", "")
MAX_LOGIN_ATTEMPTS = 5
# Cookies nur über HTTPS (Seite läuft ausschließlich per HTTPS/HSTS); Tests setzen False
_COOKIE_SECURE = os.environ.get("VKO_COOKIE_INSECURE", "") != "1"
LOCKOUT_MINUTES = 15

# Kurzlebiger Pre-Auth-Store für Vereinsauswahl bei mehreren Accounts pro E-Mail
# token → ([(user_id, verein_name)], expires)
_preauth: dict[str, tuple[list[tuple[int, str]], datetime]] = {}
_preauth_lock = threading.Lock()


def _make_preauth(choices: list[tuple[int, str]]) -> str:
    token = secrets.token_urlsafe(16)
    with _preauth_lock:
        _preauth[token] = (choices, datetime.utcnow() + timedelta(minutes=5))
    return token


def _pop_preauth(token: str) -> list[tuple[int, str]] | None:
    with _preauth_lock:
        entry = _preauth.pop(token, None)
    if not entry:
        return None
    choices, expires = entry
    return choices if datetime.utcnow() < expires else None


def _peek_preauth(token: str) -> list[tuple[int, str]] | None:
    """Wie _pop_preauth, aber verbraucht den Token nicht (für die GET-Anzeige)."""
    with _preauth_lock:
        entry = _preauth.get(token)
    if not entry:
        return None
    choices, expires = entry
    return choices if datetime.utcnow() < expires else None

_CSS = """
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{background:#1c1c1e;color:#f2f2f7;font-family:-apple-system,sans-serif;
     max-width:480px;margin:0 auto;padding:1.5rem 1rem}
h1{font-size:1.4rem;margin:0 0 1.5rem}
label{display:block;font-size:.85rem;color:#aeaeb2;margin:.75rem 0 .25rem}
input,select,textarea{width:100%;padding:.75rem;border-radius:.625rem;
  border:1px solid #3a3a3c;background:#2c2c2e;color:#f2f2f7;font-size:1rem}
/* Gleiche Höhe für Textfelder und Auswahlfelder: Safari/iOS zeichnet <select> sonst
   im Systemstil und ignoriert Padding/Höhe. Eigener Pfeil = Lucide chevron-down. */
input:not([type=checkbox]):not([type=file]),select{line-height:1.25;height:calc(2.75rem + 2px)}
select{-webkit-appearance:none;appearance:none;padding-right:2.5rem;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24' fill='none' stroke='%238e8e93' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right .75rem center;background-size:1.1rem}
input:focus,select:focus,textarea:focus{outline:2px solid #0a84ff;border-color:transparent}
textarea{font-family:inherit;line-height:1.4;resize:vertical}
.btn{display:block;width:100%;padding:.875rem;margin-top:1rem;border:none;
     border-radius:.625rem;background:#0a84ff;color:#fff;font-size:1rem;
     font-weight:600;cursor:pointer;text-align:center;text-decoration:none}
.btn-sec{background:#2c2c2e;color:#0a84ff}
.btn-danger{background:#ff3b30}
.err{color:#ff453a;margin:.5rem 0;font-size:.9rem}
.ok{color:#34c759;margin:.5rem 0;font-size:.9rem}
.hint{color:#8e8e93;font-size:.82rem;margin:.5rem 0}
a{color:#0a84ff}
.card{background:#2c2c2e;border-radius:.875rem;padding:1rem;margin:.75rem 0}
.spam-hint{background:#2c2c2e;border-radius:.625rem;padding:.75rem 1rem;
           color:#aeaeb2;font-size:.85rem;margin-top:1rem}
hr{border:none;border-top:1px solid #3a3a3c;margin:1.5rem 0}
.chk{display:flex;gap:.75rem;align-items:flex-start;margin:.75rem 0}
.chk input{width:1.2rem;height:1.2rem;flex-shrink:0;margin-top:.15rem}
.chk label{margin:0;font-size:.9rem;color:#f2f2f7}
.field-err{border-color:#ff453a!important;outline:none!important}
.chk.field-err{outline:1.5px solid #ff453a!important;border-radius:.5rem;padding:.5rem}
.pw-wrap{position:relative}
.pw-wrap input{padding-right:2.8rem}
.pw-toggle{position:absolute;right:.6rem;top:50%;transform:translateY(-50%);background:none;border:none;color:#8e8e93;cursor:pointer;padding:.25rem;display:flex;align-items:center;line-height:1}
</style>
"""

_BACK = '<a class="btn btn-sec" href="/verein/login" style="margin-top:.75rem">← Zurück zum Login</a>'


_PW_TOGGLE_JS = """<script>
/* Auge-Symbol an jedem Passwortfeld (Progressive Enhancement). Felder, die schon in .pw-wrap
   stehen (Registrierung), bleiben unberührt. */
(function(){
  var EYE='<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg>',OFF='<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
  function setze(inp,b,zeigen){inp.type=zeigen?'text':'password';b.innerHTML=zeigen?OFF:EYE;
    b.setAttribute('aria-pressed',zeigen?'true':'false');b.setAttribute('aria-label',zeigen?'Passwort verbergen':'Passwort anzeigen');}
  var paare=[];
  document.querySelectorAll('input[type=password]').forEach(function(inp){
    if(inp.closest('.pw-wrap'))return;
    var w=document.createElement('div');w.className='pw-wrap';
    inp.parentNode.insertBefore(w,inp);w.appendChild(inp);
    var b=document.createElement('button');b.type='button';b.className='pw-toggle';b.tabIndex=-1;
    w.appendChild(b);setze(inp,b,false);paare.push([inp,b]);
    b.addEventListener('click',function(){setze(inp,b,inp.type==='password');});
  });
  /* Beim Zurück-Navigieren (Bfcache) nie offen im Klartext stehen lassen */
  window.addEventListener('pageshow',function(){paare.forEach(function(p){setze(p[0],p[1],false);});});
})();
</script>"""

def _page(title: str, body: str) -> str:
    """Titel wird hier escapt (enthält teils den frei wählbaren Vereinsnamen), `body` ist fertiges HTML."""
    title = html.escape(title)
    return f"""<!doctype html><html lang="de"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} – Vereinskalender</title>{_CSS}</head>
<body><h1>{title}</h1>{body}{_PW_TOGGLE_JS}</body></html>"""


_WEITER_RE = re.compile(r"^/verein/r/[A-Za-z0-9_-]{8,64}$")


def _weiter_ziel() -> str:
    """Einladungslink zu einer Planungsrunde, der vor dem Login geöffnet wurde (Cookie `vk_weiter`, gesetzt in
    `services/verein/planung.py`). Nur dieser eine Pfadtyp – kein offener Redirect."""
    ziel = request.cookies.get("vk_weiter", "")
    return ziel if _WEITER_RE.match(ziel) else ""


def _session_token() -> str:
    return request.cookies.get("vk_session", "")


# Kenntnisnahme Datenschutz/Nutzungsbedingungen (v1.79) – dasselbe Kästchen bei Registrierung, Einladung und
# `/verein/bestaetigen`. Bewusst keine Einwilligung (Art. 6 Abs. 1 a), Grundlage ist der Nutzungsvertrag.
DS_FELD = "datenschutz_gelesen"
DS_KAESTCHEN = f"""<div class="chk">
    <input type="checkbox" name="{DS_FELD}" id="dsg" required>
    <label for="dsg">Ich habe die <a href="/verein/datenschutz" target="_blank" rel="noopener">Datenschutzerklärung</a> gelesen und akzeptiere die <a href="/verein/nutzungsbedingungen" target="_blank" rel="noopener">Nutzungsbedingungen</a>.</label>
  </div>"""
DS_FEHLER = "Bitte bestätigen, dass du die Datenschutzerklärung gelesen hast und die Nutzungsbedingungen akzeptierst."
_ZIEL_RE = re.compile(r"^/verein/[A-Za-z0-9/_.\-]*(\?[A-Za-z0-9=&_.%+\-]*)?$")


def _ziel_ok(ziel: str) -> str:
    """Rücksprung nach der Bestätigung – nur Pfade im Vereinsbereich, kein offener Redirect."""
    return ziel if ziel and "//" not in ziel and _ZIEL_RE.match(ziel) and not ziel.startswith("/verein/bestaetigen") else ""


def require_verein_login(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_session_user(_session_token())
        if not user:
            return redirect("/verein/login")
        if not user["email_verified"]:
            return redirect("/verein/login?hint=verify")
        if user["verein_status"] not in ("aktiv",):
            return redirect("/verein/login?hint=pending")
        if user.get("ds_fassung") != DS_FASSUNG:
            ziel = request.path + ("?" + request.query_string.decode("latin-1") if request.query_string else "")
            ziel = _ziel_ok(ziel) if request.method == "GET" else ""
            return redirect("/verein/bestaetigen" + (f"?ziel={quote(ziel, safe='/')}" if ziel else ""))
        return f(*args, user=user, **kwargs)
    return decorated


def _unique_verein_key(conn, verein_name: str) -> str:
    """Key, der weder in der DB noch im Kalender (Termin-Keys, Labels, Aliase) vergeben ist.
    Sonst übernähme ein neuer Account ohne Rückfrage fremde, importierte Termine – die
    Zuordnung läuft bewusst über den „Verknüpfen“-Vorschlag an Josef (Review 2026-10-04)."""
    from shared.kalender_store import KalenderStore
    try:
        data = KalenderStore.read()
    except Exception:
        data = {}
    belegt = set(data) | set(data.get("_labels", {})) | set(data.get("_heimat_aliases", {}))
    base = _make_verein_key(verein_name)
    key = base
    for i in range(1, 20):
        exists = key in belegt or conn.execute(
            "SELECT 1 FROM vereine_accounts WHERE verein_key = ?", (key,)
        ).fetchone()
        if not exists:
            return key
        key = f"{base}_{i}"
    return f"{base}_{secrets.token_hex(3)}"


def _hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def _check_pw(pw: str, hashed: str) -> bool:
    return bcrypt.checkpw(pw.encode(), hashed.encode())


def _valid_email(email: str) -> bool:
    # Keine HTML-/Header-Sonderzeichen: Adresse landet in Seiten, Mail-Headern und Admin-Listen
    return bool(re.match(r"^[^@\s<>\"'(),;:\\\[\]]+@[^@\s<>\"'(),;:\\\[\]]+\.[^@\s<>\"'(),;:\\\[\]]+$", email))


def _telegram_approve_msg(verein_id: int, verein_name: str, email: str,
                           rubrik: str = "", heimatort: str = "", telefon: str = "",
                           plz: str = "", gemeinde: str = "", landkreis: str = "",
                           hinweise: list[str] | None = None, ansprechpartner: str = "") -> None:
    from shared.telegram import cb_name
    e = html.escape   # parse_mode HTML: alle Formularwerte escapen
    lines = [f"🏛 Neuer Verein wartet auf Freigabe:\n<b>{e(verein_name)}</b>"]
    if rubrik:
        lines.append(f"Rubrik: {e(rubrik)}")
    if heimatort:
        lines.append(e(f"Ort: {plz} {heimatort}".replace("  ", " ").strip()
                       + (f" ({gemeinde}, {landkreis})" if gemeinde else "")))
    for h in hinweise or []:
        lines.append(f"⚠️ {e(h)}")
    if ansprechpartner:
        lines.append(f"Ansprechpartner: {e(ansprechpartner)}")
    lines.append(f"E-Mail: {e(email)}")
    if telefon:
        lines.append(f"Telefon: {e(telefon)}")
    try:
        send_telegram_inline(
            freigabe_chat_id(),
            "\n".join(lines),
            [
                [
                    {"text": "✅ Freigeben", "callback_data": f"verein_approve:{verein_id}:{cb_name(verein_name)}"},
                    {"text": "❌ Ablehnen", "callback_data": f"verein_reject:{verein_id}:{cb_name(verein_name)}"},
                ]
            ],
            parse_mode="HTML",
        )
    except Exception:
        pass


def _telegram_suggest_links(verein_id: int, verein_name: str, verein_key: str) -> None:
    """Schickt für jeden fuzzy-gematchten JSON-Key eine eigene Telegram-Nachricht."""
    try:
        from shared.kalender_store import KalenderStore
        data = KalenderStore.read()
        with db_conn() as conn:
            registered_keys = {
                r["verein_key"] for r in conn.execute(
                    "SELECT verein_key FROM vereine_accounts WHERE verein_key IS NOT NULL"
                ).fetchall()
            }
        labels = data.get("_labels", {})
        matches = []
        for key, termine in data.items():
            if key.startswith("_") or key in registered_keys or not isinstance(termine, list):
                continue
            label = labels.get(key, key)
            score = max(
                SequenceMatcher(None, verein_name.lower(), label.lower()).ratio(),
                SequenceMatcher(None, verein_key, key).ratio(),
            )
            if score >= 0.6:
                matches.append((score, key, label, len(termine)))
        matches.sort(reverse=True)
        for score, src_key, src_label, n in matches[:3]:
            text = (
                f"🔗 Mögliche Verknüpfung für {verein_name}:\n"
                f"Key {src_key} ({src_label}) – {n} Termine\n"
                f"Ähnlichkeit: {score:.0%}"
            )
            send_telegram_inline(
                freigabe_chat_id(),
                text,
                [[
                    {"text": "✅ Verknüpfen", "callback_data": f"vk_link:{verein_id}:{src_key}"},
                    {"text": "❌ Ignorieren", "callback_data": f"vk_ignore:{verein_id}:{src_key}"},
                ]],
            )
    except Exception:
        pass


# ── Register ────────────────────────────────────────────────────────────────

# ── PLZ + Ortschaft (Todo #418, ADR-016) ─────────────────────────────────────
# Erst die PLZ, dann die Ortschaft mit Vorschlagsliste (<datalist>) aus
# /api/orte. Freitext bleibt erlaubt; unbekannte Ortschaften gehen als Hinweis
# an Josef (Telegram), abgelehnt wird nie. Genutzt von Registrierung und Profil.
_ORTSCHAFT_JS = """<script>
(function(){
  var plz=document.querySelector('input[name=plz]'),ort=document.querySelector('input[name=heimatort]');
  var liste=document.getElementById('ort-liste'),info=document.getElementById('plz-info');
  if(!plz||!ort||!liste||!info)return;
  var zuletzt='',ctl=null;
  function zeige(t,warn){info.textContent=t;info.style.color=warn?'#ff9f0a':'#8e8e93';}
  function laden(){
    var v=plz.value.trim();
    if(!/^\\d{5}$/.test(v)){if(v!==zuletzt){liste.innerHTML='';zeige('',false);}zuletzt=v;return;}
    if(v===zuletzt)return;zuletzt=v;
    if(ctl)ctl.abort();ctl=('AbortController' in window)?new AbortController():null;
    fetch('/api/orte?plz='+v,ctl?{signal:ctl.signal}:{}).then(function(r){return r.json();}).then(function(d){
      if(d.plz!==plz.value.trim())return;
      liste.innerHTML='';
      (d.orte||[]).forEach(function(o){var op=document.createElement('option');op.value=o;liste.appendChild(op);});
      if(d.gemeinden&&d.gemeinden.length){
        zeige(d.gemeinden.map(function(g){return g.name+' · '+g.landkreis;}).join(' / '),false);
      }else{zeige('Diese PLZ kennen wir nicht. Bitte prüfen – speichern geht trotzdem.',true);}
    }).catch(function(){});
  }
  plz.addEventListener('input',laden);plz.addEventListener('change',laden);laden();
})();
</script>"""


def _telegram_ortschaft_hinweis(verein_name: str, plz: str, ort: str, hinweise: list[str]) -> None:
    """Profiländerung mit unbekannter/unpassender Ortschaft → kurze Meldung an Josef."""
    text = (f"📍 Vereinsprofil geändert: {verein_name}\n"
            f"Ort: {plz} {ort}\n"
            + "\n".join(f"⚠️ {h}" for h in hinweise))
    try:
        send_telegram(freigabe_chat_id(), text)
    except Exception:
        pass


def _ansprechpartner_felder(anrede: str, vorname: str, nachname: str) -> str:
    """Anrede (freiwillig, seit v1.69 in keiner Mail genutzt), Vor- und Nachname (Pflicht, Begrüßung in den Mails)."""
    opts = "".join(
        f'<option value="{a}"{" selected" if anrede == a else ""}>{a}</option>' for a in ANREDEN
    )
    return f"""
  <label>Anrede <span class="hint">(freiwillig)</span></label>
  <select name="anrede"><option value="">– bitte wählen –</option>{opts}</select>
  <label>Vorname Ansprechpartner</label>
  <input name="vorname" type="text" required autocomplete="given-name" placeholder="z.B. Maria" value="{html.escape(vorname)}">
  <label>Nachname Ansprechpartner</label>
  <input name="nachname" type="text" required autocomplete="family-name" placeholder="z.B. Huber" value="{html.escape(nachname)}">"""


def ansprechpartner_fehler(anrede: str, vorname: str, nachname: str) -> str:
    if anrede and anrede not in ANREDEN:
        return "Bitte eine gültige Anrede wählen."
    if not vorname or not nachname:
        return "Bitte Vor- und Nachname des Ansprechpartners angeben."
    if len(vorname) > 60 or len(nachname) > 60:
        return "Vor- oder Nachname ist zu lang (max. 60 Zeichen)."
    return ""


def _ortschaft_felder(plz: str, heimatort: str) -> str:
    """PLZ- und Ortschaftsfeld, beide Pflicht. DB-Feld bleibt `heimatort`."""
    return f"""
  <label>PLZ</label>
  <input name="plz" type="text" inputmode="numeric" pattern="[0-9]{{5}}" maxlength="5" required autocomplete="postal-code" placeholder="z.B. 84092" value="{html.escape(plz)}">
  <p class="hint" id="plz-info" aria-live="polite" style="margin:.35rem 0 0"></p>
  <label>Ortschaft <span class="hint">(Heimatort des Vereins)</span></label>
  <input name="heimatort" type="text" required list="ort-liste" autocomplete="off" placeholder="erst PLZ eingeben, dann auswählen" value="{html.escape(heimatort)}">
  <datalist id="ort-liste"></datalist>
  <p class="hint" style="margin:.35rem 0 0">Deine Ortschaft steht nicht in der Liste? Einfach eintippen.</p>"""


_PLZ_QUELLE = ('<p class="hint">PLZ-Verzeichnis: <a href="https://www.openplzapi.org/">OpenPLZ API</a>, '
               '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap-Mitwirkende</a>, ODbL.</p>')


def ortschaft_geo(heimatort: str, plz: str) -> tuple[str, str, list[str]]:
    """Gemeinde, Landkreis, Hinweise – Register/Schnappschuss, sonst Nominatim."""
    r = ortschaft_aufloesen(heimatort, plz)
    gemeinde, landkreis = r["gemeinde"], r["landkreis"]
    if not gemeinde:
        geo = lookup_plz(plz)
        gemeinde, landkreis = geo.get("gemeinde", ""), geo.get("landkreis", "")
    return gemeinde, landkreis, r["hinweise"]


@auth_bp.route("/api/orte", methods=["GET"])
def api_orte():
    """Öffentlich, nur lesend: Gemeinden + Ortschafts-Vorschläge zu einer PLZ."""
    plz = (request.args.get("plz") or "").strip()
    if not plz_gueltig(plz):
        return jsonify({"plz": plz, "bekannt": False, "gemeinden": [], "orte": []}), 400
    resp = jsonify(orte_fuer_plz(plz))
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@auth_bp.route("/verein/register", methods=["GET", "POST"])
def register():
    error = ""
    form_data: dict = {}
    if request.method == "POST":
        if not validate_csrf():
            return _page("Fehler", '<p class="err">Ungültige Anfrage. Bitte Seite neu laden.</p>'), 403
        verein_name = request.form.get("verein_name", "").strip()
        email       = request.form.get("email", "").strip().lower()
        pw          = request.form.get("password", "")
        pw2         = request.form.get("password2", "")
        sv          = request.form.get("selbstverpflichtung", "")
        zn          = request.form.get("zugangsdaten_notiert", "")
        rubrik      = request.form.get("rubrik", "").strip()
        heimatort   = request.form.get("heimatort", "").strip()
        plz         = request.form.get("plz", "").strip()
        telefon     = request.form.get("telefon", "").strip()
        anrede      = request.form.get("anrede", "").strip()
        vorname     = request.form.get("vorname", "").strip()
        nachname    = request.form.get("nachname", "").strip()
        form_data   = dict(verein_name=verein_name, email=email, rubrik=rubrik,
                           heimatort=heimatort, plz=plz, telefon=telefon,
                           anrede=anrede, vorname=vorname, nachname=nachname)

        if not verein_name or len(verein_name) < 3:
            error = "Bitte einen Vereinsnamen mit mindestens 3 Zeichen eingeben."
        elif rubrik not in RUBRIKEN:
            error = "Bitte eine gültige Rubrik auswählen."
        elif not plz_gueltig(plz):
            error = "PLZ muss 5 Ziffern haben (z.B. 84092)."
        elif not heimatort or len(heimatort) < 2:
            error = "Bitte die Ortschaft des Vereins angeben."
        elif ansprechpartner_fehler(anrede, vorname, nachname):
            error = ansprechpartner_fehler(anrede, vorname, nachname)
        elif not _valid_email(email):
            error = "Bitte eine gültige E-Mail-Adresse eingeben."
        elif not telefon:
            error = "Bitte eine Telefonnummer für Rückfragen angeben."
        elif len(pw) < 8:
            error = "Passwort muss mindestens 8 Zeichen haben."
        elif pw != pw2:
            error = "Passwörter stimmen nicht überein."
        elif not sv:
            error = "Bitte die Selbstverpflichtungserklärung bestätigen."
        elif not zn:
            error = "Bitte bestätigen, dass die Zugangsdaten notiert wurden."
        elif not request.form.get(DS_FELD):
            error = DS_FEHLER
        else:
            # Duplikat-Check: ähnlicher Vereinsname schon vorhanden?
            with db_conn() as conn:
                existing = conn.execute(
                    "SELECT verein_name FROM vereine_accounts WHERE status != 'abgelehnt'"
                ).fetchall()
            for ex in existing:
                if SequenceMatcher(None, verein_name.lower(), ex["verein_name"].lower()).ratio() >= 0.85:
                    error = (f'Ein Verein mit ähnlichem Namen ist bereits registriert: '
                             f'„{html.escape(ex["verein_name"])}". '
                             f'Falls du bereits einen Account hast, bitte einloggen. '
                             f'Bei Problemen: <a href="mailto:info@vereinskalender.online">info@vereinskalender.online</a>')
                    break

        if not error:
            gemeinde, landkreis, hinweise = ortschaft_geo(heimatort, plz)
            with db_conn() as conn:
                verein_row = conn.execute(
                    """INSERT INTO vereine_accounts
                       (verein_name, selbstverpflichtung, rubrik, heimatort, plz, gemeinde, landkreis)
                       VALUES (?,?,?,?,?,?,?) RETURNING id""",
                    (verein_name, 1, rubrik, heimatort, plz or None, gemeinde or None, landkreis or None),
                ).fetchone()
                verein_id = verein_row["id"]
                verein_key = _unique_verein_key(conn, verein_name)
                conn.execute(
                    "UPDATE vereine_accounts SET verein_key = ? WHERE id = ?",
                    (verein_key, verein_id),
                )
                token   = secrets.token_urlsafe(32)
                expires = (datetime.utcnow() + timedelta(hours=24)).isoformat()
                conn.execute(
                    """INSERT INTO vk_users
                       (email, password_hash, verein_id, role, telefon, verify_token, verify_token_expires,
                        anrede, vorname, nachname, name, ds_fassung, ds_bestaetigt_am)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                    (email, _hash_pw(pw), verein_id, "admin", telefon or None, token, expires,
                     anrede, vorname, nachname, f"{vorname} {nachname}", DS_FASSUNG),
                )
            send_verify_email(email, token, gruss=gruss_aus(form_data))
            _telegram_approve_msg(verein_id, verein_name, email,
                                  rubrik=rubrik, heimatort=heimatort, telefon=telefon,
                                  plz=plz, gemeinde=gemeinde, landkreis=landkreis,
                                  hinweise=hinweise,
                                  ansprechpartner=f"{anrede} {vorname} {nachname}".replace("keine Angabe ", "").strip())
            threading.Thread(
                target=_telegram_suggest_links,
                args=(verein_id, verein_name, verein_key),
                daemon=True,
            ).start()
            body = f"""
<p class="ok">Registrierung eingegangen!</p>
<p style="color:#aeaeb2;font-size:.9rem;margin-bottom:1.25rem">So geht es weiter:</p>
<div style="display:flex;flex-direction:column;gap:.75rem;margin-bottom:1.25rem">
  <div style="display:flex;gap:.75rem;align-items:flex-start">
    <span style="background:#0a84ff;color:#fff;border-radius:50%;width:22px;height:22px;display:flex;align-items:center;justify-content:center;font-size:.75rem;font-weight:700;flex-shrink:0;margin-top:.1rem">1</span>
    <div>
      <div style="font-weight:600;font-size:.9rem">E-Mail bestätigen</div>
      <div style="color:#aeaeb2;font-size:.85rem">Wir haben eine E-Mail an <strong style="color:#f2f2f7">{html.escape(email)}</strong> geschickt. Bitte auf den Bestätigungslink klicken.</div>
    </div>
  </div>
  <div style="display:flex;gap:.75rem;align-items:flex-start">
    <span style="background:#636366;color:#fff;border-radius:50%;width:22px;height:22px;display:flex;align-items:center;justify-content:center;font-size:.75rem;font-weight:700;flex-shrink:0;margin-top:.1rem">2</span>
    <div>
      <div style="font-weight:600;font-size:.9rem">Administrator prüft die Anfrage</div>
      <div style="color:#aeaeb2;font-size:.85rem">In der Regel innerhalb eines Tages. Du erhältst danach eine Bestätigungsmail. Falls dein Verein bereits Termine im Kalender hat, ordnen wir sie bei der Prüfung deinem Account zu.</div>
    </div>
  </div>
  <div style="display:flex;gap:.75rem;align-items:flex-start">
    <span style="background:#636366;color:#fff;border-radius:50%;width:22px;height:22px;display:flex;align-items:center;justify-content:center;font-size:.75rem;font-weight:700;flex-shrink:0;margin-top:.1rem">3</span>
    <div>
      <div style="font-weight:600;font-size:.9rem">Einloggen und Termine verwalten</div>
      <div style="color:#aeaeb2;font-size:.85rem">Nach der Freigabe kannst du dich unter <a href="/verein/login" style="color:#0a84ff">/verein/login</a> einloggen und Termine eintragen oder hochladen.</div>
    </div>
  </div>
</div>
<div class="spam-hint">Keine E-Mail erhalten? Bitte auch im <strong>Spam-Ordner</strong> nachsehen. Der Bestätigungslink ist 24 Stunden gültig.</div>
<a class="btn btn-sec" href="/" style="margin-top:1rem">← Zurück zum Kalender</a>"""
            return _page("Registrierung eingegangen", body)

    rubrik_opts = "".join(
        f'<option value="{r}"{" selected" if form_data.get("rubrik") == r else ""}>{r}</option>'
        for r in RUBRIKEN
    )
    tok = get_csrf_token()
    form = f"""
<p style="color:#aeaeb2;font-size:.9rem">Trage deinen Verein im Vereinskalender ein.</p>
{'<p class="err">'+error+'</p>' if error else ''}
<form method="post" autocomplete="on">
  {csrf_field(tok)}
  <label>Vereinsname</label>
  <input name="verein_name" type="text" required autocomplete="organization" placeholder="z.B. FF Musterdorf" value="{html.escape(form_data.get('verein_name', ''))}">
  <label>Rubrik</label>
  <select name="rubrik" required>
    <option value="">– bitte wählen –</option>
    {rubrik_opts}
  </select>
{_ortschaft_felder(form_data.get('plz', ''), form_data.get('heimatort', ''))}
{_ansprechpartner_felder(form_data.get('anrede', ''), form_data.get('vorname', ''), form_data.get('nachname', ''))}
  <label>E-Mail Ansprechpartner <span class="hint">(damit loggt ihr euch ein)</span></label>
  <input name="email" type="text" inputmode="email" autocorrect="off" autocapitalize="none" required autocomplete="email" placeholder="vorstand@beispiel.de" value="{html.escape(form_data.get('email', ''))}">
  <label>Telefon Ansprechpartner</label>
  <input name="telefon" type="tel" required autocomplete="tel" placeholder="z.B. 0172 1234567" value="{html.escape(form_data.get('telefon', ''))}">
  <label>Passwort <span class="hint">(mind. 8 Zeichen)</span></label>
  <div class="pw-wrap">
    <input name="password" type="password" required autocomplete="new-password">
    <button type="button" class="pw-toggle" onclick="togglePw(this)" tabindex="-1" aria-label="Passwort anzeigen"><svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg></button>
  </div>
  <label>Passwort wiederholen</label>
  <div class="pw-wrap">
    <input name="password2" type="password" required autocomplete="new-password">
    <button type="button" class="pw-toggle" onclick="togglePw(this)" tabindex="-1" aria-label="Passwort anzeigen"><svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg></button>
  </div>
  <div class="chk">
    <input type="checkbox" name="selbstverpflichtung" id="sv">
    <label for="sv">Ich bestätige, dass ich befugter Vertreter des genannten Vereins bin (gewählter Vorstand oder schriftlich bevollmächtigtes Mitglied) und berechtigt bin, im Namen des Vereins Termine zu veröffentlichen. Ich übernehme die Verantwortung für die Richtigkeit der eingetragenen Daten.</label>
  </div>
  <div class="chk">
    <input type="checkbox" name="zugangsdaten_notiert" id="zn" required>
    <label for="zn">Ich habe die Zugangsdaten (E-Mail-Adresse + Passwort) notiert.</label>
  </div>
  {DS_KAESTCHEN}
  <button class="btn" type="submit" id="reg-btn">Registrieren</button>
</form>
<a class="btn btn-sec" href="/verein/login" style="margin-top:.5rem">← Abbrechen</a>
<hr>
<p class="hint">Bereits registriert? <a href="/verein/login">Zum Login</a></p>
<p class="hint"><a href="/verein/datenschutz">Datenschutzerklärung</a> · <a href="/verein/nutzungsbedingungen">Nutzungsbedingungen</a></p>
{_PLZ_QUELLE}
{_ORTSCHAFT_JS}
<script>
document.querySelector('form').addEventListener('submit',function(e){{
  this.querySelectorAll('.field-err').forEach(f=>f.classList.remove('field-err'));
  let first=null;
  const btn=document.getElementById('reg-btn');
  this.querySelectorAll('input[required]:not([type=checkbox]),select[required]').forEach(f=>{{
    let bad=!f.value.trim();
    if(!bad&&f.name==='plz')bad=!/^\\d{{5}}$/.test(f.value.trim());
    if(!bad&&f.name==='email')bad=!/^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/.test(f.value.trim());
    if(!bad&&f.name==='password2'){{const p=this.querySelector('[name=password]');if(p)bad=f.value!==p.value;}}
    if(bad){{f.classList.add('field-err');if(!first)first=f;}}
  }});
  const sv=this.querySelector('[name=selbstverpflichtung]');
  if(sv&&!sv.checked){{sv.closest('.chk').classList.add('field-err');if(!first)first=sv;}}
  const zn=this.querySelector('[name=zugangsdaten_notiert]');
  if(zn&&!zn.checked){{zn.closest('.chk').classList.add('field-err');if(!first)first=zn;}}
  const ds=this.querySelector('[name={DS_FELD}]');
  if(ds&&!ds.checked){{ds.closest('.chk').classList.add('field-err');if(!first)first=ds;}}
  if(first){{e.preventDefault();first.scrollIntoView({{behavior:'smooth',block:'center'}});first.focus();}}
  else if(btn){{btn.disabled=true;btn.textContent='Wird registriert…';}}
}});
function togglePw(btn){{
  const inp=btn.closest('.pw-wrap').querySelector('input');
  const show=inp.type==='password';
  inp.type=show?'text':'password';
  const eye='<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg>';
  const eyeOff='<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
  btn.innerHTML=show?eyeOff:eye;
}}
</script>"""
    return _page("Verein registrieren", form)


# ── E-Mail verifizieren ──────────────────────────────────────────────────────

@auth_bp.route("/api/auth/verify")
def verify_email():
    token = request.args.get("token", "")
    with db_conn() as conn:
        row = conn.execute(
            """SELECT u.id, u.verify_token_expires, v.status AS verein_status
               FROM vk_users u LEFT JOIN vereine_accounts v ON v.id = u.verein_id WHERE u.verify_token = ?""",
            (token,),
        ).fetchone()
        if not row:
            body = f'<p class="err">Ungültiger Bestätigungslink.</p><a href="/api/auth/resend-verify">Neuen Link anfordern</a>{_BACK}'
            return _page("Fehler", body), 400
        if datetime.fromisoformat(row["verify_token_expires"]) < datetime.utcnow():
            body = f'<p class="err">Der Bestätigungslink ist abgelaufen.</p><a class="btn" href="/api/auth/resend-verify">Neuen Bestätigungslink anfordern</a>'
            return _page("Link abgelaufen", body), 400
        conn.execute(
            "UPDATE vk_users SET email_verified=1, verify_token=NULL, verify_token_expires=NULL WHERE id=?",
            (row["id"],),
        )
    if row["verein_status"] == "aktiv":   # schon freigegeben (Bestätigungslink nach der Freigabe) → direkt anmelden
        body = '<p class="ok">E-Mail-Adresse bestätigt!</p><p>Euer Konto ist freigeschaltet – ihr könnt euch jetzt anmelden.</p><a class="btn" href="/verein/login" style="margin-top:.5rem">Jetzt anmelden</a>'
    else:
        body = f'<p class="ok">E-Mail-Adresse bestätigt!</p><p>Dein Konto wird nun vom Administrator geprüft. Du erhältst eine E-Mail sobald es freigeschaltet wurde.</p><a class="btn btn-sec" href="/" style="margin-top:.5rem">← Zurück zum Kalender</a>'
    return _page("Bestätigt", body)


@auth_bp.route("/api/auth/resend-verify", methods=["GET", "POST"])
def resend_verify():
    if request.method == "POST":
        if not validate_csrf():
            return _page("Fehler", '<p class="err">Ungültige Anfrage. Bitte Seite neu laden.</p>'), 403
        email = request.form.get("email", "").strip().lower()
        tokens_to_send = []
        with db_conn() as conn:
            rows = conn.execute(
                "SELECT id, anrede, vorname, nachname FROM vk_users WHERE email=? AND email_verified=0", (email,)
            ).fetchall()
            for row in rows:
                token = secrets.token_urlsafe(32)
                expires = (datetime.utcnow() + timedelta(hours=24)).isoformat()
                conn.execute(
                    "UPDATE vk_users SET verify_token=?, verify_token_expires=? WHERE id=?",
                    (token, expires, row["id"]),
                )
                tokens_to_send.append((token, gruss_aus(row)))
        for token, gruss in tokens_to_send:
            send_verify_email(email, token, gruss=gruss)
        body = '<p class="ok">Falls die E-Mail existiert und noch nicht bestätigt ist, wurde ein neuer Link verschickt.</p><div class="spam-hint">Bitte auch im <strong>Spam-Ordner</strong> nachsehen.</div>' + _BACK
        return _page("Link verschickt", body)
    tok = get_csrf_token()
    form = f'<form method="post">{csrf_field(tok)}<label>E-Mail-Adresse</label><input name="email" type="email" required><button class="btn" type="submit">Neuen Link anfordern</button></form>{_BACK}'
    return _page("Bestätigungslink anfordern", form)


# ── Login ────────────────────────────────────────────────────────────────────

@auth_bp.route("/verein/login", methods=["GET", "POST"])
def login():
    hint = request.args.get("hint", "")
    hint_msg = {
        "verify":  '<p class="hint">Bitte bestätige zuerst deine E-Mail-Adresse.</p>',
        "pending": '<p class="hint">Dein Konto wartet noch auf Freigabe durch den Administrator.</p>',
        "reset":   '<p class="ok">Passwort wurde geändert. Bitte jetzt einloggen.</p>',
        "einladung": '<p class="hint">Ihr seid zu einer Planungsrunde eingeladen. Bitte meldet euch an – danach geht es direkt weiter.</p>',
    }.get(hint, "")
    if hint == "pending" and _weiter_ziel():
        hint_msg += '<p class="hint">Eure Einladung zur Planungsrunde bleibt gemerkt – nach der Freigabe einfach anmelden.</p>'

    error = ""
    login_user_id = None

    if request.method == "POST":
        if not validate_csrf():
            return _page("Fehler", '<p class="err">Ungültige Anfrage. Bitte Seite neu laden.</p>'), 403
        email = request.form.get("email", "").strip().lower()
        pw    = request.form.get("password", "")
        now   = datetime.utcnow()

        with db_conn() as conn:
            rows = conn.execute(
                """SELECT u.id, u.password_hash, u.aktiv, u.email_verified,
                          u.login_attempts, u.locked_until, u.role,
                          v.status as verein_status, v.verein_name
                   FROM vk_users u
                   JOIN vereine_accounts v ON v.id = u.verein_id
                   WHERE u.email = ?""",
                (email,),
            ).fetchall()

            if not rows:
                error = "E-Mail oder Passwort falsch."
            else:
                # Wenn irgendein Account für diese E-Mail gesperrt ist → Lockout
                if any(r["locked_until"] and datetime.fromisoformat(r["locked_until"]) > now for r in rows):
                    error = f"Zu viele Fehlversuche. Bitte {LOCKOUT_MINUTES} Minuten warten."
                else:
                    matched = [r for r in rows if _check_pw(pw, r["password_hash"])]

                    if not matched:
                        # Fehlversuch: alle Rows dieser E-Mail hochzählen und ggf. sperren.
                        # Eine abgelaufene Sperre setzt den Zähler zurück (sonst sperrt
                        # der nächste Fehlversuch sofort wieder, statt neu bis
                        # MAX_LOGIN_ATTEMPTS zu zählen).
                        def _eff_attempts(r):
                            if r["locked_until"] and datetime.fromisoformat(r["locked_until"]) <= now:
                                return 0
                            return r["login_attempts"]
                        new_attempts = max(_eff_attempts(r) for r in rows) + 1
                        locked = (now + timedelta(minutes=LOCKOUT_MINUTES)).isoformat() if new_attempts >= MAX_LOGIN_ATTEMPTS else None
                        ids = [r["id"] for r in rows]
                        conn.execute(
                            f"UPDATE vk_users SET login_attempts=?, locked_until=? WHERE id IN ({','.join('?'*len(ids))})",
                            [new_attempts, locked] + ids,
                        )
                        error = "E-Mail oder Passwort falsch."
                    else:
                        # Erfolg: Zähler aller gematchten Rows zurücksetzen
                        matched_ids = [r["id"] for r in matched]
                        conn.execute(
                            f"UPDATE vk_users SET login_attempts=0, locked_until=NULL WHERE id IN ({','.join('?'*len(matched_ids))})",
                            matched_ids,
                        )
                        # Nur vollständig nutzbare Accounts weiter beachten
                        usable = [r for r in matched if r["aktiv"] and r["email_verified"] and r["verein_status"] == "aktiv"]

                        if not usable:
                            if any(not r["aktiv"] for r in matched):
                                error = "Dein Konto ist deaktiviert."
                            elif any(not r["email_verified"] for r in matched):
                                return redirect("/verein/login?hint=verify")
                            else:
                                return redirect("/verein/login?hint=pending")
                        elif len(usable) == 1:
                            login_user_id = usable[0]["id"]
                        else:
                            # Mehrere Vereine für diese E-Mail → Auswahl anbieten
                            choices = [(r["id"], r["verein_name"]) for r in usable]
                            preauth_token = _make_preauth(choices)
                            resp = make_response(redirect("/verein/login/verein-waehlen"))
                            resp.set_cookie("vk_preauth", preauth_token, httponly=True, secure=_COOKIE_SECURE, samesite="Lax", max_age=300)
                            return resp

        if login_user_id is not None:
            return _anmelden(login_user_id)

    tok = get_csrf_token()
    form = f"""
{hint_msg}
{'<p class="err">'+error+'</p>' if error else ''}
<form method="post" autocomplete="on">
  {csrf_field(tok)}
  <label>E-Mail</label>
  <input name="email" type="email" required autocomplete="email">
  <label>Passwort</label>
  <input name="password" type="password" required autocomplete="current-password">
  <button class="btn" type="submit">Einloggen</button>
</form>
<hr>
<p class="hint"><a href="/verein/passwort-vergessen">Passwort vergessen?</a></p>
<p class="hint">Noch kein Konto? <a href="/verein/register">Verein registrieren</a></p>
<a class="btn btn-sec" href="/" style="margin-top:.5rem">← Zurück zum Kalender</a>
<p class="hint" style="margin-top:1rem"><a href="/verein/datenschutz">Datenschutzerklärung</a> · <a href="/verein/nutzungsbedingungen">Nutzungsbedingungen</a></p>"""
    return _page("Login", form)


@auth_bp.route("/verein/login/verein-waehlen", methods=["GET", "POST"])
def login_verein_waehlen():
    preauth_token = request.cookies.get("vk_preauth", "")

    if request.method == "POST":
        if not validate_csrf():
            return redirect("/verein/login")
        choices = _pop_preauth(preauth_token)
        if not choices:
            return redirect("/verein/login")
        try:
            chosen_id = int(request.form.get("user_id", ""))
        except ValueError:
            return redirect("/verein/login")
        valid_ids = [uid for uid, _ in choices]
        if chosen_id not in valid_ids:
            return redirect("/verein/login")
        resp = _anmelden(chosen_id)
        resp.delete_cookie("vk_preauth")
        return resp

    # GET: Auswahl-Seite anzeigen (Token wird hier nur gelesen, nicht verbraucht)
    choices = _peek_preauth(preauth_token)
    if not choices:
        return redirect("/verein/login")
    tok = get_csrf_token()
    options = "".join(
        f"""<form method="post" style="margin:.5rem 0">
  {csrf_field(tok)}
  <input type="hidden" name="user_id" value="{uid}">
  <button class="btn" type="submit">{html.escape(verein_name)}</button>
</form>"""
        for uid, verein_name in choices
    )
    body = f"""
<p style="color:#aeaeb2;font-size:.9rem">Deine E-Mail-Adresse ist für mehrere Vereine registriert. Für welchen möchtest du dich einloggen?</p>
{options}
<hr>
<a class="btn btn-sec" href="/verein/login">← Zurück</a>"""
    return _page("Verein wählen", body)


# ── Datenschutz bestätigen (v1.79) ───────────────────────────────────────────

@auth_bp.route("/verein/bestaetigen", methods=["GET", "POST"])
def datenschutz_bestaetigen():
    """Kenntnisnahme je Fassung (`DS_FASSUNG`). Bewusst ohne `require_verein_login` – der leitet hierher um."""
    user = get_session_user(_session_token())
    if not user:
        return redirect("/verein/login")
    if not user["email_verified"]:
        return redirect("/verein/login?hint=verify")
    if user["verein_status"] != "aktiv":
        return redirect("/verein/login?hint=pending")
    ziel = _ziel_ok(request.values.get("ziel", ""))
    weiter = ziel or _weiter_ziel() or "/verein/termine"
    if user.get("ds_fassung") == DS_FASSUNG:
        return redirect(weiter)
    fehler = ""
    if request.method == "POST":
        if not validate_csrf():
            return _page("Fehler", '<p class="err">Ungültige Anfrage. Bitte Seite neu laden.</p>'), 403
        if request.form.get(DS_FELD):
            with db_conn() as conn:
                conn.execute("UPDATE vk_users SET ds_fassung = ?, ds_bestaetigt_am = CURRENT_TIMESTAMP WHERE id = ?",
                             (DS_FASSUNG, user["id"]))
            log_audit("datenschutz_bestaetigt", DS_FASSUNG, user["verein_key"] or "", user["id"])
            return redirect(weiter)
        fehler = DS_FEHLER
    tok = get_csrf_token()
    neu = "Die Datenschutzerklärung und die Nutzungsbedingungen haben sich geändert." if user.get("ds_fassung") \
        else "Bevor es weitergeht, bitte einmal bestätigen."
    body = f"""
<p style="color:#aeaeb2;font-size:.9rem">{neu} Wir speichern nur, wann du welche Fassung bestätigt hast.</p>
{'<p class="err">' + fehler + '</p>' if fehler else ''}
<form method="post">
  {csrf_field(tok)}
  <input type="hidden" name="ziel" value="{html.escape(ziel)}">
  {DS_KAESTCHEN}
  <button class="btn" type="submit">Weiter</button>
</form>
<form method="post" action="/verein/logout">
  {csrf_field(tok)}
  <button class="btn btn-sec" type="submit" style="margin-top:.5rem">Abmelden</button>
</form>
<p class="hint" style="margin-top:1rem">Angemeldet als {html.escape(user["email"])} · {html.escape(user["verein_name"])}</p>"""
    return _page("Datenschutz", body)


# ── Anmeldung mit Authenticator-App (2FA, v1.84, freiwillig für Vereinsadmins) ──
# TOTP (pyotp, RFC 6238) + 10 Ersatzcodes (bcrypt in `totp_recovery_hashes`, je einmal gültig). Nach richtigem
# Passwort entsteht die Sitzung erst nach dem Code (`/verein/login/2fa`, Pre-Auth-Token im Cookie `vk_2fa`).
# Fehlversuche zählen in dieselbe Sperre wie beim Passwort. Josef setzt bei Geräteverlust zurück
# (`POST /api/admin/users/<id>/2fa-reset`).

TOTP_ISSUER = "Vereinskalender"
ERSATZ_ANZAHL = 10


def _sitzung_starten(uid: int):
    resp = make_response(redirect(_weiter_ziel() or "/verein/termine"))
    resp.set_cookie("vk_session", create_session(uid), httponly=True, secure=_COOKIE_SECURE, samesite="Lax",
                    max_age=SESSION_TIMEOUT_HOURS * 3600)
    return resp


def _anmelden(uid: int):
    """Nach Passwort (und ggf. Vereinswahl): mit 2FA erst zur Code-Abfrage, sonst Sitzung."""
    with db_conn() as conn:
        row = conn.execute("SELECT totp_secret FROM vk_users WHERE id = ?", (uid,)).fetchone()
    if row and row["totp_secret"]:
        resp = make_response(redirect("/verein/login/2fa"))
        resp.set_cookie("vk_2fa", _make_preauth([(uid, "")]), httponly=True, secure=_COOKIE_SECURE,
                        samesite="Lax", max_age=300)
        return resp
    return _sitzung_starten(uid)


def _ersatzcodes() -> tuple[list[str], str]:
    """10 Codes wie `k7m2-9xqp` (ohne verwechselbare Zeichen) + JSON der bcrypt-Hashes."""
    zeichen = "abcdefghjkmnpqrstuvwxyz23456789"
    codes = ["".join(secrets.choice(zeichen) for _ in range(4)) + "-" + "".join(secrets.choice(zeichen) for _ in range(4))
             for _ in range(ERSATZ_ANZAHL)]
    import json as _json
    return codes, _json.dumps([bcrypt.hashpw(c.encode(), bcrypt.gensalt(10)).decode() for c in codes])


def _code_pruefen(conn, uid: int, secret: str, hashes_json: str | None, code: str) -> bool:
    """TOTP (±30 s) oder ein Ersatzcode – der wird dabei verbraucht."""
    import json as _json
    import pyotp
    code = "".join(code.split()).lower()
    if code.isdigit() and len(code) == 6:
        return bool(secret) and pyotp.TOTP(secret).verify(code, valid_window=1)
    if len(code) == 8 and "-" not in code:
        code = code[:4] + "-" + code[4:]
    hashes = _json.loads(hashes_json or "[]")
    for h in hashes:
        if bcrypt.checkpw(code.encode(), h.encode()):
            hashes.remove(h)
            conn.execute("UPDATE vk_users SET totp_recovery_hashes = ? WHERE id = ?", (_json.dumps(hashes), uid))
            return True
    return False


@auth_bp.route("/verein/login/2fa", methods=["GET", "POST"])
def login_2fa():
    token = request.cookies.get("vk_2fa", "")
    choices = _peek_preauth(token)
    if not choices:
        return redirect("/verein/login")
    uid = choices[0][0]
    error = ""
    if request.method == "POST":
        if not validate_csrf():
            return redirect("/verein/login")
        now = datetime.utcnow()
        with db_conn() as conn:
            row = conn.execute("SELECT totp_secret, totp_recovery_hashes, login_attempts, locked_until FROM vk_users"
                               " WHERE id = ? AND aktiv = 1", (uid,)).fetchone()
            if not row:
                return redirect("/verein/login")
            if row["locked_until"] and datetime.fromisoformat(row["locked_until"]) > now:
                error = f"Zu viele Fehlversuche. Bitte {LOCKOUT_MINUTES} Minuten warten."
            elif _code_pruefen(conn, uid, row["totp_secret"], row["totp_recovery_hashes"], request.form.get("code", "")):
                conn.execute("UPDATE vk_users SET login_attempts = 0, locked_until = NULL WHERE id = ?", (uid,))
            else:
                versuche = (row["login_attempts"] or 0) + 1
                gesperrt = (now + timedelta(minutes=LOCKOUT_MINUTES)).isoformat() if versuche >= MAX_LOGIN_ATTEMPTS else None
                conn.execute("UPDATE vk_users SET login_attempts = ?, locked_until = ? WHERE id = ?", (versuche, gesperrt, uid))
                error = "Der Code stimmt nicht. Bitte den aktuellen Code aus der App eingeben."
        if not error:
            _pop_preauth(token)
            resp = _sitzung_starten(uid)
            resp.delete_cookie("vk_2fa")
            return resp
    tok = get_csrf_token()
    body = f"""
<p style="color:#aeaeb2;font-size:.9rem">Bitte den 6-stelligen Code aus deiner Authenticator-App eingeben.</p>
{'<p class="err">' + error + '</p>' if error else ''}
<form method="post" autocomplete="off">
  {csrf_field(tok)}
  <label>Code</label>
  <input name="code" inputmode="numeric" autocomplete="one-time-code" required autofocus maxlength="12" placeholder="123456">
  <button class="btn" type="submit">Anmelden</button>
</form>
<p class="hint">Handy nicht zur Hand? Einen deiner Ersatzcodes eingeben (z.&nbsp;B. k7m2-9xqp). Alles verloren? Schreib an <a href="mailto:{vk_mail.KONTAKT}">{vk_mail.KONTAKT}</a>.</p>
<a class="btn btn-sec" href="/verein/login" style="margin-top:.5rem">← Zurück zum Login</a>"""
    return _page("Bestätigungscode", body)


@auth_bp.route("/verein/2fa", methods=["GET", "POST"])
@require_verein_login
def zwei_faktor(user):
    """Einrichten, Ersatzcodes neu erzeugen, Abschalten – nur Vereinsadmins (Mitglieder schreiben nichts)."""
    import pyotp
    import segno
    if user["role"] != "admin":
        return redirect("/verein/einstellungen")
    zurueck = '<a class="btn btn-sec" href="/verein/einstellungen" style="margin-top:.75rem">← Zurück zu den Einstellungen</a>'
    tok = get_csrf_token()
    fehler = ""
    with db_conn() as conn:
        row = conn.execute("SELECT totp_secret, totp_recovery_hashes FROM vk_users WHERE id = ?", (user["id"],)).fetchone()
    aktiv = bool(row["totp_secret"])

    def codes_seite(codes, titel):
        liste = "".join(f'<li><code style="white-space:nowrap">{c}</code></li>' for c in codes)
        return _page(titel, f"""
<p class="ok">Ab jetzt fragt die Anmeldung nach dem Code aus der App.</p>
<div class="card"><b>Deine Ersatzcodes</b>
<p class="hint">Jeder Code gilt einmal – falls das Handy fehlt. Jetzt ausdrucken oder sicher notieren; sie werden nur dieses eine Mal angezeigt.</p>
<ol style="columns:2 9rem;font-size:1rem;line-height:1.8;padding-left:1.6rem">{liste}</ol>
<button class="btn btn-sec" onclick="window.print()">Drucken</button></div>
{zurueck}""")

    if request.method == "POST":
        if not validate_csrf():
            return _page("Fehler", '<p class="err">Ungültige Anfrage. Bitte Seite neu laden.</p>'), 403
        aktion, code = request.form.get("aktion", ""), request.form.get("code", "")
        if aktion == "einrichten" and not aktiv:
            secret = session.get("totp_neu", "")
            if secret and pyotp.TOTP(secret).verify("".join(code.split()), valid_window=1):
                codes, hashes = _ersatzcodes()
                with db_conn() as conn:
                    conn.execute("UPDATE vk_users SET totp_secret = ?, totp_recovery_hashes = ? WHERE id = ?",
                                 (secret, hashes, user["id"]))
                session.pop("totp_neu", None)
                log_audit("2fa_eingerichtet", f"user_{user['id']}", user["verein_key"] or "", user["id"])
                return codes_seite(codes, "Authenticator-App eingerichtet")
            fehler = "Der Code stimmt nicht. Bitte den aktuellen Code aus der App eingeben."
        elif aktion in ("ersatz", "aus") and aktiv:
            with db_conn() as conn:
                ok = _code_pruefen(conn, user["id"], row["totp_secret"], row["totp_recovery_hashes"], code)
                if ok and aktion == "aus":
                    conn.execute("UPDATE vk_users SET totp_secret = NULL, totp_recovery_hashes = NULL WHERE id = ?",
                                 (user["id"],))
            if ok and aktion == "aus":
                log_audit("2fa_abgeschaltet", f"user_{user['id']}", user["verein_key"] or "", user["id"])
                return _page("Authenticator-App abgeschaltet", '<p class="ok">Die Anmeldung fragt nicht mehr nach einem Code.</p>' + zurueck)
            if ok:
                codes, hashes = _ersatzcodes()
                with db_conn() as conn:
                    conn.execute("UPDATE vk_users SET totp_recovery_hashes = ? WHERE id = ?", (hashes, user["id"]))
                return codes_seite(codes, "Neue Ersatzcodes")
            fehler = "Der Code stimmt nicht."

    if aktiv:
        import json as _json
        rest = len(_json.loads(row["totp_recovery_hashes"] or "[]"))
        body = f"""
<p class="ok">Die Anmeldung mit Authenticator-App ist eingeschaltet. Noch {rest} von {ERSATZ_ANZAHL} Ersatzcodes übrig.</p>
{'<p class="err">' + fehler + '</p>' if fehler else ''}
<form method="post" class="card">{csrf_field(tok)}
  <label>Code aus der App oder ein Ersatzcode</label>
  <input name="code" inputmode="numeric" autocomplete="one-time-code" required maxlength="12">
  <button class="btn" name="aktion" value="ersatz" type="submit">Neue Ersatzcodes erzeugen</button>
  <button class="btn btn-sec" name="aktion" value="aus" type="submit" style="margin-top:.5rem">Abschalten</button>
</form>{zurueck}"""
        return _page("Anmeldung mit Authenticator-App", body)

    secret = session.get("totp_neu") or pyotp.random_base32()
    session["totp_neu"] = secret
    uri = pyotp.TOTP(secret).provisioning_uri(name=user["email"], issuer_name=f"{TOTP_ISSUER} {user['verein_name']}"[:60])
    qr = segno.make(uri, error="m").svg_inline(scale=5, border=2, dark="#1c1c1e", light="#ffffff")
    gruppiert = " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))
    body = f"""
<p style="color:#aeaeb2;font-size:.9rem">Zusätzlich zum Passwort fragt die Anmeldung dann nach einem 6-stelligen Code aus einer App (z.&nbsp;B. Passwörter auf dem iPhone, Google Authenticator, Microsoft Authenticator). Freiwillig – schützt den Vereinsbereich, falls jemand euer Passwort kennt.</p>
{'<p class="err">' + fehler + '</p>' if fehler else ''}
<div class="card"><b>1. In der App hinzufügen</b>
  <div style="background:#fff;border-radius:10px;padding:8px;width:max-content;max-width:100%;margin:.75rem 0">{qr}</div>
  <p class="hint">Am Handy direkt: <a href="{html.escape(uri)}">In Authenticator-App öffnen</a> · oder Schlüssel abtippen: <code style="user-select:all">{gruppiert}</code></p>
</div>
<form method="post" class="card">{csrf_field(tok)}
  <b>2. Code aus der App eingeben</b>
  <input name="code" inputmode="numeric" autocomplete="one-time-code" required maxlength="8" placeholder="123456" style="margin-top:.5rem">
  <button class="btn" name="aktion" value="einrichten" type="submit">Einschalten</button>
</form>{zurueck}"""
    return _page("Anmeldung mit Authenticator-App", body)


@auth_bp.route("/api/admin/users/<int:user_id>/2fa-reset", methods=["POST"])
def admin_2fa_reset(user_id: int):
    """Josef: Authenticator-App eines Kontos zurücksetzen (Handy verloren)."""
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return {"error": "Unauthorized"}, 401
    with db_conn() as conn:
        n = conn.execute("UPDATE vk_users SET totp_secret = NULL, totp_recovery_hashes = NULL, login_attempts = 0,"
                         " locked_until = NULL WHERE id = ? AND totp_secret IS NOT NULL", (user_id,)).rowcount
    return ({"ok": True}, 200) if n else ({"error": "Für dieses Konto ist keine Authenticator-App eingerichtet."}, 404)


# ── Logout ───────────────────────────────────────────────────────────────────

@auth_bp.route("/verein/logout", methods=["POST"])
def logout():
    token = _session_token()
    if token:
        delete_session(token)
    resp = make_response(redirect("/"))
    resp.delete_cookie("vk_session")
    return resp


# ── Passwort vergessen / Reset ───────────────────────────────────────────────

@auth_bp.route("/verein/passwort-vergessen", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        if not validate_csrf():
            return _page("Fehler", '<p class="err">Ungültige Anfrage. Bitte Seite neu laden.</p>'), 403
        email = request.form.get("email", "").strip().lower()
        tokens_to_send = []
        with db_conn() as conn:
            rows = conn.execute(
                "SELECT id, anrede, vorname, nachname FROM vk_users WHERE email = ?", (email,)
            ).fetchall()
            for row in rows:
                token = secrets.token_urlsafe(32)
                expires = (datetime.utcnow() + timedelta(hours=1)).isoformat()
                conn.execute(
                    "UPDATE vk_users SET reset_token=?, reset_token_expires=? WHERE id=?",
                    (token, expires, row["id"]),
                )
                tokens_to_send.append((token, gruss_aus(row)))
        for token, gruss in tokens_to_send:
            send_reset_email(email, token, gruss=gruss)
        body = '<p class="ok">Falls diese E-Mail registriert ist, wurde ein Reset-Link verschickt.</p><div class="spam-hint">Bitte auch im <strong>Spam-Ordner</strong> nachsehen.</div>' + _BACK
        return _page("Link verschickt", body)
    tok = get_csrf_token()
    form = f'<p style="color:#aeaeb2">Gib deine E-Mail-Adresse ein. Du erhältst einen Link zum Passwort-Zurücksetzen.</p><form method="post">{csrf_field(tok)}<label>E-Mail</label><input name="email" type="email" required><button class="btn" type="submit">Reset-Link anfordern</button></form>{_BACK}'
    return _page("Passwort vergessen", form)


@auth_bp.route("/verein/passwort-reset", methods=["GET", "POST"])
def reset_password():
    token = request.args.get("token", "") or request.form.get("token", "")
    with db_conn() as conn:
        row = conn.execute(
            "SELECT id, reset_token_expires FROM vk_users WHERE reset_token = ?",
            (token,),
        ).fetchone()
        if not row:
            body = f'<p class="err">Ungültiger oder bereits verwendeter Reset-Link.</p>{_BACK}'
            return _page("Fehler", body), 400
        if datetime.fromisoformat(row["reset_token_expires"]) < datetime.utcnow():
            body = f'<p class="err">Der Reset-Link ist abgelaufen.</p><a class="btn" href="/verein/passwort-vergessen">Neuen Link anfordern</a>'
            return _page("Link abgelaufen", body), 400

        pw_error = ""
        if request.method == "POST":
            if not validate_csrf():
                return _page("Fehler", '<p class="err">Ungültige Anfrage. Bitte Seite neu laden.</p>'), 403
            pw = request.form.get("password", "")
            pw2 = request.form.get("password2", "")
            if len(pw) < 8:
                pw_error = "Passwort muss mindestens 8 Zeichen haben."
            elif pw != pw2:
                pw_error = "Passwörter stimmen nicht überein."
            else:
                conn.execute(
                    "UPDATE vk_users SET password_hash=?, reset_token=NULL, reset_token_expires=NULL, login_attempts=0, locked_until=NULL WHERE id=?",
                    (_hash_pw(pw), row["id"]),
                )
                delete_user_sessions(row["id"], conn=conn)  # ggf. gekaperte Sessions beenden
                return redirect("/verein/login?hint=reset")

    tok = get_csrf_token()
    form = f"""{'<p class="err">'+pw_error+'</p>' if pw_error else ''}
<form method="post">
{csrf_field(tok)}
<input type="hidden" name="token" value="{html.escape(token)}">
<label>Neues Passwort <span class="hint">(mind. 8 Zeichen)</span></label>
<input name="password" type="password" required autocomplete="new-password">
<label>Passwort wiederholen</label>
<input name="password2" type="password" required autocomplete="new-password">
<button class="btn" type="submit">Passwort speichern</button>
</form>"""
    return _page("Neues Passwort", form)


# ── Superadmin: Verein freigeben/ablehnen ────────────────────────────────────

@auth_bp.route("/api/admin/vereine/<int:verein_id>/approve", methods=["POST"])
def approve_verein(verein_id: int):
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return {"error": "Unauthorized"}, 401
    with db_conn() as conn:
        row = conn.execute(
            """SELECT v.verein_name, v.verein_key, v.plz, v.gemeinde, v.landkreis,
                      v.heimatort, v.rubrik, u.email, u.anrede, u.vorname, u.nachname,
                      u.id AS uid, u.email_verified
               FROM vereine_accounts v
               JOIN vk_users u ON u.verein_id = v.id AND u.role = 'admin'
               WHERE v.id = ? AND v.status = 'pending'""",
            (verein_id,),
        ).fetchone()
        if not row:
            return {"error": "Nicht gefunden oder bereits bearbeitet"}, 404
        conn.execute(
            "UPDATE vereine_accounts SET status='aktiv', freigegeben_at=CURRENT_TIMESTAMP WHERE id=?",
            (verein_id,),
        )
        art, ok = konto_mail(conn, row["uid"], row["email"], row["verein_name"], row["email_verified"],
                             gruss=gruss_aus(row))

    from shared.kalender_store import register_verein
    register_verein(row["verein_key"], row["verein_name"], row)

    return {"ok": True, "mail": art, "mail_ok": ok, "mail_text": konto_mail_text(art, ok)}


@auth_bp.route("/api/admin/vereine/<int:verein_id>/reject", methods=["POST"])
def reject_verein(verein_id: int):
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return {"error": "Unauthorized"}, 401
    with db_conn() as conn:
        row = conn.execute(
            """SELECT v.verein_name, u.email, u.anrede, u.vorname, u.nachname FROM vereine_accounts v
               JOIN vk_users u ON u.verein_id = v.id AND u.role = 'admin'
               WHERE v.id = ? AND v.status = 'pending'""",
            (verein_id,),
        ).fetchone()
        if not row:
            return {"error": "Nicht gefunden oder bereits bearbeitet"}, 404
        conn.execute(
            "UPDATE vereine_accounts SET status='abgelehnt' WHERE id=?",
            (verein_id,),
        )
        ok = send_rejected_email(row["email"], row["verein_name"], gruss=gruss_aus(row))
    return {"ok": True, "mail_ok": ok}


@auth_bp.route("/api/admin/vereine-pending")
def pending_vereine():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return {"error": "Unauthorized"}, 401
    with db_conn() as conn:
        rows = conn.execute(
            """SELECT v.id, v.verein_name, v.created_at, u.email
               FROM vereine_accounts v
               JOIN vk_users u ON u.verein_id = v.id AND u.role='admin'
               WHERE v.status='pending' ORDER BY v.created_at""",
        ).fetchall()
    return [dict(r) for r in rows]


@auth_bp.route("/api/admin/users")
def admin_users():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return {"error": "Unauthorized"}, 401
    with db_conn() as conn:
        vereine = conn.execute(
            """SELECT v.id, v.verein_key, v.verein_name, v.status,
                      v.rubrik, v.heimatort, v.plz, v.gemeinde, v.landkreis,
                      v.created_at, v.avv_fassung, v.avv_am
               FROM vereine_accounts v
               ORDER BY v.verein_name""",
        ).fetchall()
        users = conn.execute(
            """SELECT u.id, u.email, u.name, u.telefon, u.aktiv,
                      u.email_verified, u.created_at, u.role, u.verein_id,
                      u.ds_fassung, u.ds_bestaetigt_am, (u.totp_secret IS NOT NULL) AS totp_aktiv
               FROM vk_users u""",
        ).fetchall()
    users_by_verein: dict = {}
    for u in users:
        ud = dict(u)
        ud["ds_aktuell"] = u["ds_fassung"] == DS_FASSUNG   # Datenschutz-Bestätigung der geltenden Fassung (v1.79)
        users_by_verein.setdefault(u["verein_id"], []).append(ud)
    pflege = _pflege_je_verein()
    result = []
    for v in vereine:
        vd = dict(v)
        vd["users"] = users_by_verein.get(v["id"], [])
        vd["pflege"] = pflege.get(v["verein_key"] or "", {"selbst": False, "crawler_aus": False,
                                                         "eigene": 0, "crawler": 0})
        result.append(vd)
    return result


def _pflege_je_verein() -> dict:
    """Übersicht im Admin (ADR-027): pflegt der Verein selbst, ist der Abruf aus, wie viele kommende
    Termine stammen vom Verein (Formular/Upload) bzw. vom Crawler (quelle_url)."""
    from shared.kalender_store import KalenderStore
    try:
        data = KalenderStore.read()
    except Exception:
        return {}
    heute = datetime.now().strftime("%Y-%m-%d")
    meta, labels = data.get("_meta", {}), data.get("_labels", {})
    out: dict = {}
    for key, items in data.items():
        if key.startswith("_") or not isinstance(items, list):
            continue
        eigene = crawler = 0
        for t in items:
            if not isinstance(t, dict) or t.get("geloescht") or t.get("deleted") or t.get("datum", "") < heute:
                continue
            if t.get("erstellt_von") or (t.get("quelle") and t.get("quelle") == labels.get(key)):
                eigene += 1
            elif "quelle_url" in t:
                crawler += 1
        out[key] = {"eigene": eigene, "crawler": crawler}
    for key, m in meta.items():
        e = out.setdefault(key, {"eigene": 0, "crawler": 0})
        e["selbst"] = bool(m.get("selbstverwaltung"))
        e["crawler_aus"] = bool(m.get("crawler_aus"))
    for e in out.values():
        e.setdefault("selbst", False)
        e.setdefault("crawler_aus", False)
    return out


@auth_bp.route("/api/admin/users/<int:user_id>", methods=["PATCH"])
def admin_update_user(user_id: int):
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return {"error": "Unauthorized"}, 401
    body = request.get_json(silent=True) or {}
    name    = body.get("name",    "").strip() or None
    telefon = body.get("telefon", "").strip() or None
    with db_conn() as conn:
        row = conn.execute("SELECT id FROM vk_users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return {"error": "Nicht gefunden"}, 404
        conn.execute(
            "UPDATE vk_users SET name = ?, telefon = ? WHERE id = ?",
            (name, telefon, user_id),
        )
    return {"ok": True}


@auth_bp.route("/api/admin/users/<int:user_id>/mail", methods=["POST"])
def admin_konto_mail(user_id: int):
    """Admin → Accounts → „Mail erneut senden“: Bestätigungslink (E-Mail unbestätigt) bzw. Willkommens-Mail
    (freigegebener Verein). Für Fälle, in denen die Mail bei Registrierung/Freigabe nicht ankam."""
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return {"error": "Unauthorized"}, 401
    with db_conn() as conn:
        row = conn.execute(
            """SELECT u.id, u.email, u.email_verified, u.aktiv, u.anrede, u.vorname, u.nachname,
                      v.verein_name, v.status
               FROM vk_users u JOIN vereine_accounts v ON v.id = u.verein_id WHERE u.id = ?""",
            (user_id,),
        ).fetchone()
        if not row:
            return {"error": "Nicht gefunden"}, 404
        if not row["aktiv"]:
            return {"error": "Benutzer ist deaktiviert"}, 409
        if row["email_verified"] and row["status"] != "aktiv":
            return {"error": "Verein ist noch nicht freigegeben – erst freigeben, dann geht die Willkommens-Mail raus"}, 409
        art, ok = konto_mail(conn, row["id"], row["email"], row["verein_name"], row["email_verified"],
                             gruss=gruss_aus(row))
    return {"ok": ok, "mail": art, "mail_text": konto_mail_text(art, ok)}


# ── Admin → Texte: E-Mail-Texte selbst bearbeiten (v1.68) ────────────────────

def _admin_ok() -> bool:
    token = request.headers.get("X-Upload-Token", "")
    return bool(UPLOAD_TOKEN) and hmac.compare_digest(token, UPLOAD_TOKEN)


PITCH, PITCH_BEISPIEL = "pitch", "invite"      # Kurzvorstellung (v1.72): kein eigener Versand, gezeigt an der Einladung


def _beispiel_gruss(art: str) -> str:
    """Begrüßung in Vorschau/Testmail wie im echten Versand: Eingeladene kennen wir nur per Adresse (v1.74)."""
    return "Hallo," if art in (PITCH, "invite") else "Hallo Erika Muster,"


def _mailtext_eintrag(art: str) -> dict:
    s = vk_mail.STANDARD[art]
    aktuell = vk_mail.texte(art)
    return {"art": art, "zweck": s["zweck"], "titel": s.get("titel") or aktuell["betreff"], "texte": aktuell,
            "felder": vk_mail.felder(art), "standard": vk_mail.standard_texte(art),
            "geaendert": aktuell != vk_mail.standard_texte(art),
            "platzhalter": [{"name": p, "info": vk_mail.PLATZHALTER[p]} for p in s["platzhalter"]],
            "knopf": bool(s["knopf_link"]), "verlauf": len(vk_mail.verlauf(art))}


def _formular_texte(art: str):
    body = request.get_json(silent=True) or {}
    werte = {f: body.get(f, "") for f in vk_mail.felder(art)}
    return werte, vk_mail.pruefe_texte(art, werte)


@auth_bp.route("/api/admin/mailtexte")
def admin_mailtexte():
    if not _admin_ok():
        return {"error": "Unauthorized"}, 401
    return jsonify([_mailtext_eintrag(a) for a in vk_mail.STANDARD])


@auth_bp.route("/api/admin/mailtexte/<art>", methods=["PUT"])
def admin_mailtext_speichern(art: str):
    if not _admin_ok():
        return {"error": "Unauthorized"}, 401
    if art not in vk_mail.STANDARD:
        return {"error": "Unbekannte Mail"}, 404
    werte, fehler = _formular_texte(art)
    if fehler:
        return {"error": fehler}, 400
    vk_mail.speichere_texte(art, werte)
    return _mailtext_eintrag(art)


@auth_bp.route("/api/admin/mailtexte/<art>/vorschau", methods=["POST"])
def admin_mailtext_vorschau(art: str):
    """Vorschau mit Beispielwerten – auch für noch nicht gespeicherte Texte aus dem Formular."""
    if not _admin_ok():
        return {"error": "Unauthorized"}, 401
    if art not in vk_mail.STANDARD:
        return {"error": "Unbekannte Mail"}, 404
    werte, fehler = _formular_texte(art)
    if fehler:
        return {"error": fehler}, 400
    if art == PITCH:                                      # Kurzvorstellung: Vorschau an der Einladungsmail
        betreff, inhalt = vk_mail.baue_mail(PITCH_BEISPIEL, vk_mail.BEISPIEL, "Hallo,", pitch=werte["text"])
    else:
        betreff, inhalt = vk_mail.baue_mail(art, vk_mail.BEISPIEL, _beispiel_gruss(art), eigene=werte)
    return {"betreff": betreff, "html": inhalt}


@auth_bp.route("/api/admin/mailtexte/<art>/test", methods=["POST"])
def admin_mailtext_test(art: str):
    """Testmail mit den GESPEICHERTEN Texten und Beispielwerten an das Vereinskalender-Postfach."""
    if not _admin_ok():
        return {"error": "Unauthorized"}, 401
    if art not in vk_mail.STANDARD:
        return {"error": "Unbekannte Mail"}, 404
    betreff, inhalt = vk_mail.baue_mail(PITCH_BEISPIEL if art == PITCH else art, vk_mail.BEISPIEL, _beispiel_gruss(art))
    ok = vk_mail._send(vk_mail.KONTAKT, "[Test] " + betreff, inhalt)
    return {"ok": ok, "an": vk_mail.KONTAKT}


@auth_bp.route("/api/admin/mailtexte/<art>/zuruecksetzen", methods=["POST"])
def admin_mailtext_zuruecksetzen(art: str):
    """{"auf": "standard"} oder {"auf": "vorige"} (letzte gespeicherte Fassung vor der aktuellen)."""
    if not _admin_ok():
        return {"error": "Unauthorized"}, 401
    if art not in vk_mail.STANDARD:
        return {"error": "Unbekannte Mail"}, 404
    auf = (request.get_json(silent=True) or {}).get("auf", "standard")
    if auf == "vorige":
        v = vk_mail.verlauf(art)
        if not v:
            return {"error": "Es gibt keine vorige Fassung."}, 409
        werte = v[0]["texte"]
    else:
        werte = vk_mail.standard_texte(art)
    vk_mail.speichere_texte(art, werte)
    return _mailtext_eintrag(art)


@auth_bp.route("/api/admin/verein/<int:verein_id>", methods=["PATCH"])
def admin_update_verein(verein_id: int):
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return {"error": "Unauthorized"}, 401
    body = request.get_json(silent=True) or {}
    with db_conn() as conn:
        row = conn.execute(
            "SELECT id, verein_key, verein_name, heimatort, plz FROM vereine_accounts WHERE id = ?", (verein_id,)
        ).fetchone()
    if not row:
        return {"error": "Nicht gefunden"}, 404
    verein_key = row["verein_key"]
    fields = {}
    for f in ("verein_name", "rubrik", "heimatort", "plz", "gemeinde", "landkreis"):
        if f in body:
            fields[f] = (body[f] or "").strip() or None
    if fields.get("plz") and not re.match(r"^\d{5}$", fields["plz"]):
        fields.pop("plz")
    if "verein_name" in fields and not fields["verein_name"]:
        fields.pop("verein_name")  # Name nie leeren
    # Ort geändert → Gemeinde/Landkreis neu bestimmen (wie im Vereinsprofil), außer der
    # Admin gibt sie selbst mit (Netzwerk-Fallback vor allen Schreibzugriffen)
    neuer_ort = fields.get("heimatort", row["heimatort"])
    neue_plz = fields.get("plz", row["plz"])
    if (("heimatort" in fields and fields["heimatort"] != row["heimatort"])
            or ("plz" in fields and fields["plz"] != row["plz"])) and neuer_ort and neue_plz \
            and "gemeinde" not in fields and "landkreis" not in fields:
        gem, lk, _ = ortschaft_geo(neuer_ort, neue_plz)
        fields["gemeinde"], fields["landkreis"] = gem or None, lk or None
    if fields:
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        with db_conn() as conn:
            conn.execute(
                f"UPDATE vereine_accounts SET {set_clause} WHERE id = ?",
                list(fields.values()) + [verein_id],
            )
    if fields and verein_key:
        # Kalender liest `_labels`/`_meta` aus vereinstermine.json, und `_meta` hat dort Vorrang
        # vor der DB – ohne diesen Abgleich wirkte die Änderung nicht (Review 2026-10-04, Punkt 8)
        from shared.kalender_store import KalenderStore

        def _sync(d, vk=verein_key, felder=dict(fields)):
            if felder.get("verein_name"):
                d.setdefault("_labels", {})[vk] = felder["verein_name"]
            if vk not in d.get("_labels", {}):
                return  # (noch) nicht im Kalender – register_verein übernimmt bei Freigabe
            m = d.setdefault("_meta", {}).setdefault(vk, {})
            for k in ("rubrik", "heimatort", "plz", "gemeinde", "landkreis"):
                if k in felder:
                    if felder[k]:
                        m[k] = felder[k]
                    else:
                        m.pop(k, None)
        KalenderStore.update(_sync)
    return {"ok": True}


@auth_bp.route("/api/admin/verein/<int:verein_id>", methods=["DELETE"])
def admin_delete_verein(verein_id: int):
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return {"error": "Unauthorized"}, 401
    body = request.get_json(silent=True) or {}
    delete_termine = bool(body.get("delete_termine", False))
    with db_conn() as conn:
        row = conn.execute(
            "SELECT verein_key, verein_name FROM vereine_accounts WHERE id = ?",
            (verein_id,),
        ).fetchone()
        if not row:
            return {"error": "Nicht gefunden"}, 404
        verein_key  = row["verein_key"]
        verein_name = row["verein_name"]
        user_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM vk_users WHERE verein_id = ?", (verein_id,)
            ).fetchall()
        ]
        for uid in user_ids:
            conn.execute("DELETE FROM vk_sessions WHERE user_id = ?", (uid,))
            conn.execute("DELETE FROM vk_audit WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM vk_users WHERE verein_id = ?", (verein_id,))
        conn.execute("DELETE FROM upload_quota WHERE verein_id = ?", (verein_id,))
        if verein_key:
            conn.execute(
                "DELETE FROM tg_subscriptions WHERE verein_key = ?", (verein_key,)
            )
        conn.execute("DELETE FROM vereine_accounts WHERE id = ?", (verein_id,))
    # Dokumente gehören nur dem Verein – mit dem Konto immer weg, auch wenn die Termine bleiben (ADR-029)
    geloescht_dokumente = 0
    if verein_key:
        from shared import dokumente_db, dokumente_store
        geloescht_dokumente, dateien = dokumente_db.verein_loeschen(verein_key)
        for name in dateien:
            dokumente_store.entfernen(name)
    geloescht_termine = 0
    if delete_termine and verein_key:
        try:
            from shared.kalender_store import KalenderStore
            def _rm(data):
                nonlocal geloescht_termine
                geloescht_termine = len(data.pop(verein_key, []))
                data.get("_labels", {}).pop(verein_key, None)
                data.get("_meta",   {}).pop(verein_key, None)
            KalenderStore.update(_rm)
        except Exception:
            pass
    return {"ok": True, "geloescht_termine": geloescht_termine, "geloescht_dokumente": geloescht_dokumente}




@auth_bp.route("/api/admin/unregistered-keys")
def admin_unregistered_keys():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return {"error": "Unauthorized"}, 401
    from shared.kalender_store import KalenderStore
    data = KalenderStore.read()
    with db_conn() as conn:
        registered = {
            r["verein_key"] for r in conn.execute(
                "SELECT verein_key FROM vereine_accounts WHERE verein_key IS NOT NULL"
            ).fetchall()
        }
    labels = data.get("_labels", {})
    result = []
    for key, termine in data.items():
        if key.startswith("_") or key in registered or not isinstance(termine, list):
            continue
        result.append({"key": key, "label": labels.get(key, key), "n_termine": len(termine)})
    result.sort(key=lambda x: x["label"])
    return result


@auth_bp.route("/api/admin/verein/<int:verein_id>/transfer-key", methods=["POST"])
def admin_transfer_key(verein_id: int):
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return {"error": "Unauthorized"}, 401
    body = request.get_json(silent=True) or {}
    source_key = (body.get("source_key") or "").strip()
    if not source_key:
        return {"error": "source_key fehlt"}, 400
    with db_conn() as conn:
        row = conn.execute(
            "SELECT verein_key FROM vereine_accounts WHERE id = ?", (verein_id,)
        ).fetchone()
        if not row:
            return {"error": "Verein nicht gefunden"}, 404
        target_key = row["verein_key"]
        if not target_key:
            return {"error": "Account hat noch keinen verein_key"}, 400
    from shared.kalender_store import uebertrage_key
    try:
        transferred = uebertrage_key(source_key, target_key)
    except ValueError as e:
        return {"error": str(e)}, 400
    return {"ok": True, "transferred": transferred}


# ── Session-Status (für Client-JS) ──────────────────────────────────────────

@auth_bp.route("/api/auth/me")
def auth_me():
    user = get_session_user(_session_token())
    if not user or not user["email_verified"] or user["verein_status"] != "aktiv":
        return {"loggedin": False}, 200
    return {
        "loggedin": True,
        "role": user["role"],
        "verein_name": user["verein_name"],
        "email": user["email"],
    }, 200

# ── DB init on import ────────────────────────────────────────────────────────

init_db()
