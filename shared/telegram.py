import json
import os
import urllib.request

# Eigene Telegram-Gruppe „VKO Freigaben“ (Josef 2026-10-07): alles rund um Vereinskonten – Freigabe-Anfragen
# mit Knöpfen, Ergebnis, Verknüpfungsvorschläge, Ortschaft-Hinweise, gescheiterte Mails. Keine Geheimnis-Info
# (Chat-ID), deshalb im Repo. Leer = alles in den Hauptchat (CHAT_ID). Die ID meldet der Bot selbst, sobald er
# zur Gruppe hinzugefügt wird. Pitfall: Wird die Gruppe zur Supergruppe, ändert sich die ID (der Bot meldet
# auch das) – dann hier neu eintragen.
FREIGABE_CHAT_ID = ""


def freigabe_chat_id() -> str:
    return FREIGABE_CHAT_ID or os.environ.get("CHAT_ID", "")

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


def _post(token: str, payload: dict) -> None:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10)


def send_telegram(token: str, chat_id: str | int, text: str) -> None:
    for part in split_telegram_message(text):
        _post(token, {"chat_id": chat_id, "text": part})


def send_telegram_inline(token: str, chat_id: str | int, text: str, keyboard: list) -> None:
    parts = split_telegram_message(text)
    for i, part in enumerate(parts):
        payload = {"chat_id": chat_id, "text": part}
        if i == len(parts) - 1:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        _post(token, payload)


def webhook_secret(bot_token: str, explizit: str = "") -> str:
    """Secret für `X-Telegram-Bot-Api-Secret-Token` (Review 2026-10-04, Punkt 2).

    Ohne eigenes Secret in secrets.env aus dem Bot-Token abgeleitet – so kennen
    Flask (Token aus EnvironmentFile) und telegram_webhook_guard.py (setzt es per
    setWebhook) denselben Wert, ohne dass ein weiteres Secret gepflegt werden muss.
    Leerer Token → leeres Secret (Endpunkt lehnt dann alles ab)."""
    if explizit:
        return explizit
    if not bot_token:
        return ""
    import hashlib
    import hmac
    return hmac.new(bot_token.encode(), b"vko-telegram-webhook", hashlib.sha256).hexdigest()


def secret_ok(header_wert: str | None, erwartet: str) -> bool:
    import hmac
    return bool(erwartet) and hmac.compare_digest((header_wert or "").encode(), erwartet.encode())


def cb_name(name: str, max_bytes: int = 30) -> str:
    """Vereinsname für callback_data (Telegram-Limit 64 Byte gesamt): ohne ':' und auf
    max_bytes UTF-8-Bytes gekürzt, ohne ein Zeichen zu zerschneiden."""
    roh = (name or "").replace(":", "_").encode("utf-8")[:max_bytes]
    return roh.decode("utf-8", errors="ignore")
