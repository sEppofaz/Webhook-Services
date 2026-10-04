#!/opt/rename-webhook/bin/python3
"""
telegram_webhook_guard.py
Alle 30 Min (Cron): prüft ob die Telegram-Webhooks (Haupt-Bot, Kalender-Bot) noch
korrekt gesetzt sind – immer mit secret_token (Review 2026-10-04). `--force` setzt neu. Falls nicht (z.B. durch einen versehentlichen getUpdates-Call
gelöscht) -> automatisch neu setzen + Telegram-Alarm.

Hintergrund: Telegram-Webhooks laufen nicht ab, sie werden nur explizit oder
durch einen parallelen getUpdates-Call auf denselben Bot-Token gelöscht.
Vorfall 2026-08-12 bis 2026-08-27: Webhook war 2 Wochen unbemerkt weg, dadurch
blieben alle Bot-Befehle (u.a. /heimat-Kalenderimport) tot. Siehe CLAUDE.md.
"""
import json
import sys
import urllib.request

sys.path.insert(0, "/opt/rename-webhook")
from shared.secrets import load_secrets
from shared.telegram import send_telegram, webhook_secret

EXPECTED_URL = "https://vereinskalender.online/telegram"


def log(msg: str) -> None:
    from datetime import datetime
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _call(token: str, method: str, params: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(params).encode() if params else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if params else "GET",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def pruefe_bot(token: str, erwartete_url: str, secret: str, force: bool) -> str:
    """Webhook eines Bots prüfen und bei Bedarf MIT secret_token neu setzen.

    Neu gesetzt wird, wenn die URL fehlt/falsch ist, wenn Telegram zuletzt eine 403
    bekam (Webhook ohne bzw. mit altem Secret registriert – der Endpunkt lehnt
    dann alle Updates ab) oder mit --force. Ohne erwartete URL (Kalender-Bot) wird
    die aktuelle URL beibehalten. Gibt "ok", "neu" oder eine Fehlermeldung zurück."""
    info = _call(token, "getWebhookInfo").get("result", {})
    url = info.get("url", "")
    ziel = erwartete_url or url
    if not ziel:
        return "keine Webhook-URL registriert"
    fehler_403 = "403" in (info.get("last_error_message") or "")
    if url == ziel and not fehler_403 and not force:
        return "ok"
    result = _call(token, "setWebhook", {"url": ziel, "secret_token": secret})
    if result.get("ok"):
        return "neu"
    return f"setWebhook fehlgeschlagen: {result.get('description', result)}"


def main() -> None:
    secrets = load_secrets()
    token   = secrets["TOKEN"]
    chat_id = secrets["CHAT_ID"]
    force   = "--force" in sys.argv

    bots = [("Haupt-Bot", token, EXPECTED_URL,
             webhook_secret(token, secrets.get("TELEGRAM_WEBHOOK_SECRET", "")))]
    if secrets.get("KALENDER_BOT_TOKEN"):
        kt = secrets["KALENDER_BOT_TOKEN"]
        bots.append(("Kalender-Bot", kt, "", webhook_secret(kt)))

    for name, bot_token, url, secret in bots:
        try:
            ergebnis = pruefe_bot(bot_token, url, secret, force)
        except Exception as e:
            ergebnis = f"Fehler: {e}"
        if ergebnis == "ok":
            log(f"✅  {name}: Webhook ok")
        elif ergebnis == "neu":
            log(f"✅  {name}: Webhook (mit Secret) neu gesetzt")
            if not force:
                send_telegram(token, chat_id,
                              f"🔧 Telegram-Webhook {name} war weg oder abgewiesen – "
                              "automatisch neu registriert. Kein Handlungsbedarf.")
        else:
            log(f"❌  {name}: {ergebnis}")
            send_telegram(token, chat_id, f"❌ Telegram-Webhook-Guard ({name}): {ergebnis}")
            if name == "Haupt-Bot":
                sys.exit(1)


if __name__ == "__main__":
    main()
