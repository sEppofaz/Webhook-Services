#!/opt/rename-webhook/bin/python3
"""
telegram_webhook_guard.py
Alle 30 Min (Cron): prüft ob der Telegram-Webhook des Haupt-Bots noch korrekt
gesetzt ist. Falls nicht (z.B. durch einen versehentlichen getUpdates-Call
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
from shared.telegram import send_telegram

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


def main() -> None:
    secrets = load_secrets()
    token   = secrets["TOKEN"]
    chat_id = secrets["CHAT_ID"]

    info = _call(token, "getWebhookInfo")
    current_url = info.get("result", {}).get("url", "")

    if current_url == EXPECTED_URL:
        log(f"✅  Webhook ok ({current_url})")
        return

    log(f"⚠️  Webhook falsch/leer (aktuell: {current_url!r}) – setze neu…")
    result = _call(token, "setWebhook", {"url": EXPECTED_URL})

    if result.get("ok"):
        log(f"✅  Webhook neu gesetzt: {EXPECTED_URL}")
        send_telegram(
            token, chat_id,
            "🔧 Telegram-Webhook war weg (Bot-Befehle wären tot gewesen) – "
            "automatisch neu registriert. Kein Handlungsbedarf."
        )
    else:
        log(f"❌  setWebhook fehlgeschlagen: {result}")
        send_telegram(
            token, chat_id,
            f"❌ Telegram-Webhook-Guard: setWebhook fehlgeschlagen!\n{result.get('description', result)}"
        )


if __name__ == "__main__":
    main()
