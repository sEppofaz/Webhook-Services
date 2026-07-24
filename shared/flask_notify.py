import json
import os
import urllib.request

from shared.kalender_core import log

TELEGRAM_TOKEN   = os.environ.get("TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("CHAT_ID", "")

TELEGRAM_MSG_LIMIT = 4096


def split_telegram_message(text: str, limit: int = TELEGRAM_MSG_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts


def send_telegram(chat_id: str | int, text: str) -> None:
    if not TELEGRAM_TOKEN:
        log("⚠️  TELEGRAM_TOKEN nicht gesetzt – Nachricht nicht gesendet")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for part in split_telegram_message(text):
        payload = json.dumps({"chat_id": chat_id, "text": part}).encode()
        req     = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log(f"❌  Telegram-Sendefehler: {e}")


def send_telegram_inline(chat_id: str | int, text: str, keyboard: list, parse_mode: str | None = None) -> int | None:
    """Sendet Nachricht mit Inline-Keyboard, gibt message_id der letzten (Keyboard-)Nachricht zurück."""
    if not TELEGRAM_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    parts = split_telegram_message(text)
    message_id = None
    for i, part in enumerate(parts):
        msg: dict = {"chat_id": chat_id, "text": part}
        if i == len(parts) - 1:
            msg["reply_markup"] = {"inline_keyboard": keyboard}
        if parse_mode:
            msg["parse_mode"] = parse_mode
        payload = json.dumps(msg).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            message_id = data.get("result", {}).get("message_id")
        except Exception as e:
            log(f"❌  Telegram-Inline-Sendefehler: {e}")
            return None
    return message_id


def answer_telegram_callback(callback_query_id: str, text: str = "") -> None:
    """Beantwortet einen Inline-Keyboard-Callback (entfernt Lade-Spinner)."""
    if not TELEGRAM_TOKEN:
        return
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
    payload = json.dumps({"callback_query_id": callback_query_id, "text": text}).encode()
    req     = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"❌  Telegram-Callback-Fehler: {e}")
