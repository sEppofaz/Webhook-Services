import html
import json
import os
import re
import secrets
import smtplib
import sys
import urllib.request
import uuid
from datetime import datetime, timedelta
from email import utils as email_utils
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

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
  .pitch{border-top:1px solid #e5e5ea;margin-top:20px;padding-top:14px}
  .pitch p{color:#6e6e73;font-size:13px;line-height:1.5;margin:0 0 8px}
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
    from shared.telegram import freigabe_chat_id
    token, chat = os.environ.get("TOKEN", ""), freigabe_chat_id()
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
    """„Hallo Maria Huber," – immer Vor- und Nachname, die Anrede bleibt ungenutzt (v1.69, Josef: kein
    „Herr/Frau" vor dem Du). Ohne Namen nur „Hallo,"."""
    vorname, nachname = (vorname or "").strip(), (nachname or "").strip()
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


# ── Mail-Texte: Standard im Code, Josefs Fassung in mail_texte.json (Admin → Texte, v1.68) ─────────
# Josef bearbeitet Betreff, Überschrift, Text, Knopf und Hinweis selbst – ohne HTML. Der Rahmen (Kopf,
# Fußzeile, Anrede, Link des Knopfs) bleibt im Code. Text: Leerzeile = Absatz, „- “ = Aufzählung,
# „1. “ = nummeriert, **fett**. Platzhalter nur aus der Liste der jeweiligen Mail.

MAIL_TEXTE_FILE = Path("/opt/rename-webhook/mail_texte.json")
FELDER = {"betreff": 150, "ueberschrift": 120, "text": 4000, "knopf": 60, "hinweis": 1000}   # Feld → Höchstlänge
VERLAUF_MAX = 20
KONTAKT = "Vereinskalender@icloud.com"

# Platzhalter → Beschreibung (für die Admin-Oberfläche). Links werden als anklickbarer Link eingesetzt.
PLATZHALTER = {
    "verein": "Name des Vereins",
    "link": "persönlicher Link dieser Mail",
    "login_link": "Link zur Anmeldung",
    "profil_link": "Link zum Vereinsprofil",
    "upload_link": "Link zum Terminplan-Upload",
    "kalender_link": "Link zum Kalender",
    "kontakt": KONTAKT,
    "neu": "neue Adresse (gekürzt, z. B. ma…@web.de)",
    "eingeladen_von": "Vor- und Nachname des Admins, der einlädt",
}
_LINKS = {"link", "login_link", "profil_link", "upload_link", "kalender_link"}

STANDARD = {
    # Kein eigener Versand: Kurzvorstellung am Ende JEDER Mail (v1.72), bearbeitbar wie ein Mail-Text.
    # `felder` begrenzt die Felder dieses Eintrags; Mails haben alle FELDER.
    "pitch": {
        "zweck": "Steht unten in allen E-Mails an Vereine",
        "titel": "Kurzvorstellung (alle E-Mails)",
        "felder": ["text"], "platzhalter": [], "knopf_link": None, "anrede": False,
        "betreff": "", "ueberschrift": "", "knopf": "", "hinweis": "",
        "text": ("Vereinskalender.online ist die Plattform, auf der man Termine und Veranstaltungen der Region "
                 "schnell und übersichtlich findet. Vereine und Veranstalter können ihre Termine dort selbst pflegen."),
    },
    "verify": {
        "zweck": "Nach der Registrierung – E-Mail-Adresse bestätigen (auch nach der Freigabe, wenn noch offen)",
        "platzhalter": ["link"], "knopf_link": "link", "anrede": True,
        "betreff": "Deine E-Mail-Adresse bestätigen – Vereinskalender",
        "ueberschrift": "E-Mail-Adresse bestätigen",
        "text": "Bitte bestätige deine E-Mail-Adresse, um deine Registrierung abzuschließen.",
        "knopf": "E-Mail bestätigen",
        "hinweis": "Der Link ist **24 Stunden** gültig.\nFalls du dich nicht registriert hast, kannst du diese E-Mail ignorieren.\n\nDirektlink: {link}",
    },
    "reset": {
        "zweck": "Passwort vergessen – Link zum neuen Passwort",
        "platzhalter": ["link"], "knopf_link": "link", "anrede": True,
        "betreff": "Passwort zurücksetzen – Vereinskalender",
        "ueberschrift": "Passwort zurücksetzen",
        "text": "Du hast eine Passwort-Zurücksetzung angefordert.",
        "knopf": "Neues Passwort setzen",
        "hinweis": "Der Link ist **1 Stunde** gültig.\nFalls du keine Zurücksetzung angefordert hast, ignoriere diese E-Mail.",
    },
    "invite": {
        "zweck": "Ein Vereinsadmin lädt ein weiteres Mitglied ein",
        "platzhalter": ["verein", "link", "eingeladen_von"], "knopf_link": "link", "anrede": True,
        "betreff": "Einladung: {verein} – Vereinskalender",
        "ueberschrift": "Einladung zur Mitarbeit",
        "text": ("**{eingeladen_von}** hat dich eingeladen, die Termine von **{verein}** im Vereinskalender "
                 "mitzuverwalten."),
        "knopf": "Einladung annehmen",
        "hinweis": "Der Link ist **48 Stunden** gültig.",
    },
    "welcome": {
        "zweck": "Nach der Freigabe – Konto ist freigeschaltet",
        "platzhalter": ["verein", "login_link", "profil_link", "upload_link", "kalender_link", "kontakt"],
        "knopf_link": "login_link", "anrede": True,
        "betreff": "Konto freigeschaltet – {verein}",
        "ueberschrift": "Willkommen beim Vereinskalender!",
        "text": ("Das Konto für **{verein}** ist freigeschaltet. In drei Schritten seid ihr dabei:\n\n"
                 "1. **Profil prüfen** – PLZ, Ortschaft, Rubrik und Ansprechpartner kontrollieren: {profil_link}\n"
                 "2. **Termine hochladen** – Jahresprogramm als PDF, Foto oder Excel: {upload_link}\n"
                 "3. **Kalender abonnieren** – Auf {kalender_link} den Button „Abonnieren“ antippen – dann habt ihr "
                 "alle Termine automatisch im iPhone-Kalender."),
        "knopf": "Jetzt einloggen",
        "hinweis": "Bei Fragen einfach auf diese E-Mail antworten oder schreiben an {kontakt}.",
    },
    "rejected": {
        "zweck": "Registrierung abgelehnt",
        "platzhalter": ["verein", "kontakt"], "knopf_link": "", "anrede": True,
        "betreff": "Registrierungsanfrage – Vereinskalender",
        "ueberschrift": "Registrierung nicht angenommen",
        "text": ("Die Registrierungsanfrage für **{verein}** konnte leider nicht bestätigt werden.\n\n"
                 "Bei Fragen wende dich direkt an den Kalender-Administrator."),
        "knopf": "",
        "hinweis": "",
    },
    "email_change_confirm": {
        "zweck": "Verein ändert seine Login-Adresse – Mail an die NEUE Adresse",
        "platzhalter": ["verein", "link"], "knopf_link": "link", "anrede": True,
        "betreff": "Neue E-Mail-Adresse bestätigen – Vereinskalender",
        "ueberschrift": "Neue E-Mail-Adresse bestätigen",
        "text": "Für **{verein}** soll diese Adresse künftig zum Einloggen und für Benachrichtigungen dienen.",
        "knopf": "Adresse bestätigen",
        "hinweis": ("Der Link ist **24 Stunden** gültig. Bis dahin gilt die bisherige Adresse.\n"
                    "Falls du das nicht veranlasst hast, ignoriere diese E-Mail.\n\nDirektlink: {link}"),
    },
    "email_change_notice": {
        "zweck": "Verein ändert seine Login-Adresse – Hinweis an die ALTE Adresse",
        "platzhalter": ["verein", "neu", "kontakt"], "knopf_link": "", "anrede": True,
        "betreff": "E-Mail-Adresse geändert – Vereinskalender",
        "ueberschrift": "Änderung der E-Mail-Adresse angefordert",
        "text": ("Für **{verein}** wurde eine neue Login-Adresse eingetragen: **{neu}**.\n"
                 "Sie gilt erst, wenn sie über den Link in der dortigen E-Mail bestätigt wird.\n\n"
                 "Warst du das nicht? Dann ändere bitte sofort dein Passwort und melde dich unter {kontakt}."),
        "knopf": "",
        "hinweis": "",
    },
}

# Beispielwerte für Vorschau und Testmail
BEISPIEL = {"verein": "FF Musterdorf e.V.", "link": f"{BASE_URL}/beispiel-link", "neu": "ma…@web.de",
            "eingeladen_von": "Maria Huber"}


def _lade_datei() -> dict:
    """Inhalt von mail_texte.json – fehlt die Datei: leer. Kaputte Datei → Standardtexte + Telegram-Hinweis."""
    try:
        if not MAIL_TEXTE_FILE.exists():
            return {}
        d = json.loads(MAIL_TEXTE_FILE.read_text())
        return d if isinstance(d, dict) else {}
    except Exception as e:
        print(f"MAIL-TEXTE nicht lesbar: {e}", file=sys.stderr)
        _fehler_melden("–", "mail_texte.json", f"Datei nicht lesbar, Standardtexte werden benutzt ({type(e).__name__})")
        return {}


def felder(art: str) -> list[str]:
    """Bearbeitbare Felder eines Eintrags – Mails alle, die Kurzvorstellung nur `text`."""
    return STANDARD[art].get("felder") or list(FELDER)


def standard_texte(art: str) -> dict:
    return {f: STANDARD[art][f] for f in felder(art)}


def texte(art: str) -> dict:
    """Aktuelle Texte einer Mail: Standard, überschrieben von Josefs Fassung."""
    eigen = (_lade_datei().get("texte") or {}).get(art) or {}
    return {f: (eigen[f] if isinstance(eigen.get(f), str) else STANDARD[art][f]) for f in felder(art)}


def pruefe_texte(art: str, werte: dict) -> str:
    """Fehlertext für die Admin-Oberfläche, leer = in Ordnung."""
    erlaubt = set(STANDARD[art]["platzhalter"])
    for feld in felder(art):
        maximal, wert = FELDER[feld], werte.get(feld, "")
        if not isinstance(wert, str):
            return f"„{feld}“ fehlt."
        if len(wert) > maximal:
            return f"„{feld}“ ist zu lang (höchstens {maximal} Zeichen)."
        for name in re.findall(r"\{([^{}]*)\}", wert):
            if name not in erlaubt:
                moeglich = ", ".join("{" + p + "}" for p in STANDARD[art]["platzhalter"]) or "keine"
                return f"Platzhalter {{{name}}} gibt es in dieser Mail nicht. Möglich: {moeglich}."
    if "betreff" in felder(art) and (not werte.get("betreff", "").strip() or not werte.get("ueberschrift", "").strip()):
        return "Betreff und Überschrift dürfen nicht leer sein."
    if STANDARD[art]["knopf_link"] and not werte.get("knopf", "").strip():
        return "Die Beschriftung des Knopfs darf nicht leer sein – sonst fehlt der Link."
    return ""


def _schreiben(d: dict) -> None:
    """Atomar (BKM Atomic-Write-Pattern): erst Temp-Datei, dann umbenennen."""
    tmp = MAIL_TEXTE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    os.replace(tmp, MAIL_TEXTE_FILE)


def speichere_texte(art: str, werte: dict) -> None:
    d = _lade_datei()
    alt = texte(art)
    verlauf = d.setdefault("verlauf", {}).setdefault(art, [])
    verlauf.insert(0, {"zeit": datetime.now().isoformat(timespec="seconds"), "texte": alt})
    del verlauf[VERLAUF_MAX:]
    neu = {f: werte[f] for f in felder(art)}
    if neu == standard_texte(art):
        d.setdefault("texte", {}).pop(art, None)          # gleich Standard → nichts überschreiben
    else:
        d.setdefault("texte", {})[art] = neu
    _schreiben(d)


def verlauf(art: str) -> list[dict]:
    return (_lade_datei().get("verlauf") or {}).get(art) or []


def _ersetzen(text: str, werte: dict, als_html: bool) -> str:
    """Escapen, dann Platzhalter einsetzen (Links anklickbar) und **fett**."""
    if not als_html:
        for k, v in werte.items():
            text = text.replace("{" + k + "}", str(v))
        return text
    out = html.escape(text)
    for k, v in werte.items():
        if k in _LINKS:
            wert = f'<a href="{html.escape(v)}" style="color:#6D28D9">{html.escape(v)}</a>'
        elif k == "kontakt":
            wert = f'<a href="mailto:{html.escape(v)}" style="color:#6D28D9">{html.escape(v)}</a>'
        else:
            wert = html.escape(str(v))
        out = out.replace("{" + k + "}", wert)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)


def _bloecke(text: str, werte: dict) -> str:
    """Leerzeile = Absatz, Zeilen mit „- “ bzw. „1. “ = Liste, sonst Zeilenumbruch."""
    teile = []
    for block in re.split(r"\n\s*\n", text.strip()):
        zeilen = [z for z in block.split("\n") if z.strip()]
        if not zeilen:
            continue
        if all(re.match(r"\s*-\s+", z) for z in zeilen):
            tag, rx = "ul", r"\s*-\s+"
        elif all(re.match(r"\s*\d+\.\s+", z) for z in zeilen):
            tag, rx = "ol", r"\s*\d+\.\s+"
        else:
            teile.append("<p>" + "<br>\n".join(_ersetzen(z, werte, True) for z in zeilen) + "</p>")
            continue
        punkte = "".join(f"<li>{_ersetzen(re.sub(rx, '', z, count=1), werte, True)}</li>" for z in zeilen)
        teile.append(f'<{tag} style="margin:12px 0 16px;padding-left:20px;color:#3c3c43;line-height:1.8;font-size:14px">'
                     f"{punkte}</{tag}>")
    return "\n".join(teile)


def _pitch_links(text: str) -> str:
    """„Vereinskalender.online" in der Kurzvorstellung wird zum Link (v1.73, jede Schreibweise). Ziel fest im Code;
    nicht in einer schon geschriebenen Adresse (https://…, …@…)."""
    return re.sub(r"(?<![/\w.@-])(vereinskalender\.online)(?![\w-])",
                  lambda m: f'<a href="{BASE_URL}" style="color:#6D28D9">{m.group(1)}</a>', text, flags=re.I)


def baue_mail(art: str, werte: dict, gruss: str = "", eigene: dict | None = None,
              pitch: str | None = None) -> tuple[str, str]:
    """(Betreff, HTML) einer Mail. `eigene` = Texte aus dem Formular (Vorschau vor dem Speichern),
    `pitch` = Entwurf der Kurzvorstellung (Vorschau), sonst die gespeicherte."""
    t = eigene or texte(art)
    werte = {"login_link": f"{BASE_URL}/verein/login", "profil_link": f"{BASE_URL}/verein/profil",
             "upload_link": f"{BASE_URL}/verein/upload", "kalender_link": BASE_URL, "kontakt": KONTAKT, **werte}
    werte = {k: v for k, v in werte.items() if k in STANDARD[art]["platzhalter"]}
    body = f"<h2>{_ersetzen(t['ueberschrift'], werte, True)}</h2>\n{_bloecke(t['text'], werte)}"
    ziel = STANDARD[art]["knopf_link"]
    if ziel and t["knopf"].strip():
        body += f'\n<a class="btn" href="{html.escape(werte[ziel])}">{html.escape(t["knopf"])}</a>'
    if t["hinweis"].strip():
        hinweis = "<br>\n".join(_ersetzen(z, werte, True) for z in t["hinweis"].strip().split("\n"))
        body += f'\n<p class="hint">{hinweis}</p>'
    if STANDARD[art]["anrede"]:
        body = _mit_gruss(body, gruss)
    pitch = texte("pitch")["text"] if pitch is None else pitch
    if pitch.strip():                                     # leer = Kurzvorstellung abgeschaltet
        body += f'\n<div class="pitch">{_pitch_links(_bloecke(pitch, {}))}</div>'
    titel = html.escape(_ersetzen(t["ueberschrift"], werte, False))
    return _ersetzen(t["betreff"], werte, False), _html_wrap(titel, body)


def _mail(art: str, to_email: str, werte: dict, gruss: str = "") -> bool:
    betreff, body = baue_mail(art, werte, gruss)
    return _send(to_email, betreff, body)


def send_verify_email(to_email: str, token: str, gruss: str = "") -> bool:
    return _mail("verify", to_email, {"link": f"{BASE_URL}/api/auth/verify?token={token}"}, gruss)


def send_reset_email(to_email: str, token: str, gruss: str = "") -> bool:
    return _mail("reset", to_email, {"link": f"{BASE_URL}/verein/passwort-reset?token={token}"}, gruss)


def send_invite_email(to_email: str, token: str, verein_name: str, eingeladen_von: str = "") -> bool:
    """Eingeladene kennen wir nur per Adresse → Begrüßung „Hallo,". Ohne Namen des Einladenden (alte
    Konten) steht „Ein Admin von <Verein>" (v1.71)."""
    von = (eingeladen_von or "").strip() or f"Ein Admin von {verein_name}"
    return _mail("invite", to_email, {"link": f"{BASE_URL}/verein/einladung?token={token}",
                                      "verein": verein_name, "eingeladen_von": von}, "Hallo,")


def send_welcome_email(to_email: str, verein_name: str, gruss: str = "") -> bool:
    return _mail("welcome", to_email, {"verein": verein_name}, gruss)


def send_rejected_email(to_email: str, verein_name: str, gruss: str = "") -> bool:
    return _mail("rejected", to_email, {"verein": verein_name}, gruss)


def send_email_change_confirm(to_email: str, token: str, verein_name: str, gruss: str = "") -> bool:
    """An die NEUE Adresse: erst der Klick macht sie zur Login-Adresse."""
    return _mail("email_change_confirm", to_email,
                 {"link": f"{BASE_URL}/verein/email-bestaetigen?token={token}", "verein": verein_name}, gruss)


def send_email_change_notice(to_email: str, neu: str, verein_name: str, gruss: str = "") -> bool:
    """An die ALTE Adresse: Hinweis, damit eine fremde Änderung auffällt."""
    teile = neu.split("@")
    maskiert = (teile[0][:2] + "…@" + teile[1]) if len(teile) == 2 else "…"
    return _mail("email_change_notice", to_email, {"verein": verein_name, "neu": maskiert}, gruss)


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
