import html
import json
import os
import secrets
import smtplib
import sys
import urllib.request
import uuid
from datetime import datetime, timedelta
from email import utils as email_utils
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SMTP_HOST = "smtp-relay.brevo.com"
SMTP_PORT = 587
FROM_EMAIL = "noreply@vereinskalender.online"
FROM_NAME  = "Vereinskalender"
BASE_URL   = "https://vereinskalender.online"

_STYLE = """
<style>
  body{margin:0;padding:0;background:#f2f2f7;font-family:-apple-system,Helvetica,Arial,sans-serif}
  .wrap{padding:32px 16px}
  .card{background:#fff;border-radius:14px;padding:32px;max-width:480px;margin:0 auto;
        box-shadow:0 2px 12px rgba(0,0,0,.08)}
  .hdr{background:#6D28D9;border-radius:10px 10px 0 0;margin:-32px -32px 24px;
       padding:20px 32px;color:#fff}
  .hdr h1{margin:0;font-size:18px;font-weight:700}
  .hdr p{margin:4px 0 0;font-size:12px;opacity:.75}
  h2{color:#1c1c1e;margin:0 0 12px;font-size:17px}
  p{color:#3c3c43;line-height:1.6;margin:0 0 12px}
  .btn{display:inline-block;background:#6D28D9;color:#fff !important;text-decoration:none;
       padding:13px 28px;border-radius:10px;font-weight:600;font-size:15px;margin:8px 0 16px}
  .hint{color:#8e8e93;font-size:.82rem;line-height:1.5}
  .footer{text-align:center;margin-top:20px;color:#aeaeb2;font-size:.78rem}
</style>
"""

def _html_wrap(title: str, body: str) -> str:
    return f"""<!DOCTYPE html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>{_STYLE}</head>
<body><div class="wrap"><div class="card">
<div class="hdr"><h1>Vereinskalender</h1><p>Veranstaltungen in der Region</p></div>
{body}
</div>
<p class="footer">Vereinskalender &middot; vereinskalender.online<br>
Du erhältst diese E-Mail, weil eine Aktion auf unserem Portal durchgeführt wurde.</p>
</div></body></html>"""


def _fehler_melden(to_email: str, subject: str, grund: str) -> None:
    """Gescheiterte Mail sofort per Telegram an Josef (2026-10-07: Willkommens- und Bestätigungsmail für
    FF Hölskofen gingen still verloren, der Brevo-SMTP-Schlüssel war inaktiv). Wirft nie."""
    token, chat = os.environ.get("TOKEN", ""), os.environ.get("CHAT_ID", "")
    if not token or not chat:
        return
    text = (f"⚠️ Mail nicht verschickt\nAn: {to_email}\nBetreff: {subject}\nGrund: {grund[:300]}\n"
            "Bitte selbst Bescheid geben. Erneut senden: Admin → Accounts.")
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                     data=json.dumps({"chat_id": chat, "text": text}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"MAIL-ALARM nicht gesendet: {e}", file=sys.stderr)


def _send(to_email: str, subject: str, html_body: str) -> bool:
    """Schickt die Mail über Brevo. Jeder Fehlschlag (fehlender Zugang, SMTP-Fehler) meldet sich per Telegram."""
    # Betreff enthält teils den Vereinsnamen (Formulareingabe) – keine Zeilenumbrüche in Header
    subject = " ".join(str(subject).split())
    if any(c in to_email for c in "\r\n"):
        return False
    smtp_user = os.environ.get("BREVO_SMTP_USER", "")
    smtp_key  = os.environ.get("BREVO_SMTP_KEY", "")
    if not smtp_user or not smtp_key:
        print(f"MAIL ERROR to {to_email}: SMTP-Zugang fehlt", file=sys.stderr)
        _fehler_melden(to_email, subject, "SMTP-Zugang fehlt (BREVO_SMTP_USER/BREVO_SMTP_KEY nicht gesetzt)")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"]      = subject
    msg["From"]         = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"]           = to_email
    msg["Reply-To"]     = FROM_EMAIL
    msg["Message-ID"]   = f"<{uuid.uuid4()}@vereinskalender.online>"
    msg["Date"]         = email_utils.formatdate(localtime=False)
    msg["MIME-Version"] = "1.0"
    msg["Precedence"]   = "transactional"
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(smtp_user, smtp_key)
            smtp.sendmail(FROM_EMAIL, to_email, msg.as_string())
        return True
    except Exception as e:
        print(f"MAIL ERROR to {to_email}: {e}", file=sys.stderr)
        _fehler_melden(to_email, subject, f"{type(e).__name__}: {e}")
        return False


ANREDEN = ["Herr", "Frau", "keine Angabe"]


def begruessung(anrede: str | None = "", vorname: str | None = "", nachname: str | None = "") -> str:
    """„Hallo Herr Huber," – bei „keine Angabe" mit vollem Namen, ohne Namen nur „Hallo,"."""
    anrede, vorname, nachname = (anrede or "").strip(), (vorname or "").strip(), (nachname or "").strip()
    if anrede in ("Herr", "Frau") and nachname:
        return f"Hallo {anrede} {html.escape(nachname)},"
    name = " ".join(x for x in (vorname, nachname) if x)
    return f"Hallo {html.escape(name)}," if name else "Hallo,"


def gruss_aus(row) -> str:
    """Begrüßung aus einer vk_users-Zeile (sqlite3.Row oder dict), fehlende Spalten → „Hallo,"."""
    def feld(k):
        try:
            return row[k]
        except (KeyError, IndexError, TypeError):
            return ""
    return begruessung(feld("anrede"), feld("vorname"), feld("nachname"))


def _mit_gruss(body: str, gruss: str) -> str:
    """Setzt die Begrüßung direkt unter die Überschrift."""
    if not gruss:
        return body
    teile = body.split("</h2>", 1)
    return f"{teile[0]}</h2>\n<p>{gruss}</p>{teile[1]}" if len(teile) == 2 else f"<p>{gruss}</p>{body}"


def send_verify_email(to_email: str, token: str, gruss: str = "") -> bool:
    link = f"{BASE_URL}/api/auth/verify?token={token}"
    body = f"""<h2>E-Mail-Adresse bestätigen</h2>
<p>Bitte bestätige deine E-Mail-Adresse, um deine Registrierung abzuschließen.</p>
<a class="btn" href="{link}">E-Mail bestätigen</a>
<p class="hint">Der Link ist <strong>24 Stunden</strong> gültig.<br>
Falls du dich nicht registriert hast, kannst du diese E-Mail ignorieren.<br><br>
Direktlink: <a href="{link}" style="color:#6D28D9">{link}</a></p>"""
    return _send(to_email, "Deine E-Mail-Adresse bestätigen – Vereinskalender", _html_wrap("E-Mail bestätigen", _mit_gruss(body, gruss)))


def send_reset_email(to_email: str, token: str, gruss: str = "") -> bool:
    link = f"{BASE_URL}/verein/passwort-reset?token={token}"
    body = f"""<h2>Passwort zurücksetzen</h2>
<p>Du hast eine Passwort-Zurücksetzung angefordert.</p>
<a class="btn" href="{link}">Neues Passwort setzen</a>
<p class="hint">Der Link ist <strong>1 Stunde</strong> gültig.<br>
Falls du keine Zurücksetzung angefordert hast, ignoriere diese E-Mail.</p>"""
    return _send(to_email, "Passwort zurücksetzen – Vereinskalender", _html_wrap("Passwort zurücksetzen", _mit_gruss(body, gruss)))


def send_invite_email(to_email: str, token: str, verein_name: str) -> bool:
    link = f"{BASE_URL}/verein/einladung?token={token}"
    body = f"""<h2>Einladung zur Mitarbeit</h2>
<p>Du wurdest eingeladen, den Vereinskalender für <strong>{html.escape(verein_name)}</strong> mitzuverwalten.</p>
<a class="btn" href="{link}">Einladung annehmen</a>
<p class="hint">Der Link ist <strong>48 Stunden</strong> gültig.</p>"""
    return _send(to_email, f"Einladung: {verein_name} – Vereinskalender", _html_wrap("Einladung", body))


def send_welcome_email(to_email: str, verein_name: str, gruss: str = "") -> bool:
    login_link  = f"{BASE_URL}/verein/login"
    upload_link = f"{BASE_URL}/verein/upload"
    profil_link = f"{BASE_URL}/verein/profil"
    body = f"""<h2>Willkommen beim Vereinskalender!</h2>
<p>Das Konto für <strong>{html.escape(verein_name)}</strong> ist freigeschaltet. In drei Schritten seid ihr dabei:</p>
<ol style="margin:12px 0 16px;padding-left:20px;color:#3c3c43;line-height:2;font-size:14px">
  <li><strong>Profil prüfen</strong> – PLZ, Ortschaft, Rubrik und Ansprechpartner kontrollieren:<br>
      <a href="{profil_link}" style="color:#6D28D9">{profil_link}</a></li>
  <li><strong>Termine hochladen</strong> – Jahresprogramm als PDF, Foto oder Excel:<br>
      <a href="{upload_link}" style="color:#6D28D9">{upload_link}</a></li>
  <li><strong>Kalender abonnieren</strong> – Auf <a href="{BASE_URL}" style="color:#6D28D9">vereinskalender.online</a>
      den Button <em>„Abonnieren"</em> antippen – dann habt ihr alle Termine automatisch im iPhone-Kalender.</li>
</ol>
<a class="btn" href="{login_link}">Jetzt einloggen</a>
<p class="hint">Bei Fragen einfach auf diese E-Mail antworten oder schreiben an
<a href="mailto:Vereinskalender@icloud.com" style="color:#6D28D9">Vereinskalender@icloud.com</a>.</p>"""
    return _send(to_email, f"Konto freigeschaltet – {verein_name}", _html_wrap("Willkommen!", _mit_gruss(body, gruss)))


def send_rejected_email(to_email: str, verein_name: str, gruss: str = "") -> bool:
    body = f"""<h2>Registrierung nicht angenommen</h2>
<p>Die Registrierungsanfrage für <strong>{html.escape(verein_name)}</strong> konnte leider nicht bestätigt werden.</p>
<p>Bei Fragen wende dich direkt an den Kalender-Administrator.</p>"""
    return _send(to_email, f"Registrierungsanfrage – Vereinskalender", _html_wrap("Registrierung", _mit_gruss(body, gruss)))


def send_email_change_confirm(to_email: str, token: str, verein_name: str, gruss: str = "") -> bool:
    """An die NEUE Adresse: erst der Klick macht sie zur Login-Adresse."""
    link = f"{BASE_URL}/verein/email-bestaetigen?token={token}"
    body = f"""<h2>Neue E-Mail-Adresse bestätigen</h2>
<p>Für <strong>{html.escape(verein_name)}</strong> soll diese Adresse künftig zum Einloggen und für Benachrichtigungen dienen.</p>
<a class="btn" href="{link}">Adresse bestätigen</a>
<p class="hint">Der Link ist <strong>24 Stunden</strong> gültig. Bis dahin gilt die bisherige Adresse.<br>
Falls du das nicht veranlasst hast, ignoriere diese E-Mail.<br><br>
Direktlink: <a href="{link}" style="color:#6D28D9">{link}</a></p>"""
    return _send(to_email, "Neue E-Mail-Adresse bestätigen – Vereinskalender",
                 _html_wrap("E-Mail bestätigen", _mit_gruss(body, gruss)))


def send_email_change_notice(to_email: str, neu: str, verein_name: str, gruss: str = "") -> bool:
    """An die ALTE Adresse: Hinweis, damit eine fremde Änderung auffällt."""
    teile = neu.split("@")
    maskiert = (teile[0][:2] + "…@" + teile[1]) if len(teile) == 2 else "…"
    body = f"""<h2>Änderung der E-Mail-Adresse angefordert</h2>
<p>Für <strong>{html.escape(verein_name)}</strong> wurde eine neue Login-Adresse eingetragen: <strong>{html.escape(maskiert)}</strong>.
Sie gilt erst, wenn sie über den Link in der dortigen E-Mail bestätigt wird.</p>
<p>Warst du das nicht? Dann ändere bitte sofort dein Passwort und melde dich unter
<a href="mailto:Vereinskalender@icloud.com" style="color:#6D28D9">Vereinskalender@icloud.com</a>.</p>"""
    return _send(to_email, "E-Mail-Adresse geändert – Vereinskalender",
                 _html_wrap("Hinweis", _mit_gruss(body, gruss)))


def konto_mail(conn, user_id: int, to_email: str, verein_name: str, email_verified, gruss: str = "") -> tuple[str, bool]:
    """Die eine Mail, die ein freigegebener Verein braucht (Freigabe, Admin „Mail erneut senden“):
    E-Mail noch nicht bestätigt → neuer Bestätigungslink (24 h; danach kann er sich direkt anmelden),
    sonst die Willkommens-Mail. Gibt (art, verschickt) zurück, art = "bestaetigung" | "willkommen"."""
    if not email_verified:
        token = secrets.token_urlsafe(32)
        conn.execute("UPDATE vk_users SET verify_token=?, verify_token_expires=? WHERE id=?",
                     (token, (datetime.utcnow() + timedelta(hours=24)).isoformat(), user_id))
        return "bestaetigung", send_verify_email(to_email, token, gruss=gruss)
    return "willkommen", send_welcome_email(to_email, verein_name, gruss=gruss)


def konto_mail_text(art: str, verschickt: bool) -> str:
    """Eine Zeile für Telegram und Admin: was rausging bzw. was zu tun ist."""
    if not verschickt:
        return "⚠️ Mail NICHT verschickt – bitte selbst Bescheid geben. Erneut senden: Admin → Accounts."
    if art == "bestaetigung":
        return ("📧 Bestätigungslink verschickt – die E-Mail-Adresse ist noch nicht bestätigt; "
                "nach dem Klick kann sich der Verein anmelden.")
    return "📧 Willkommens-Mail verschickt."
