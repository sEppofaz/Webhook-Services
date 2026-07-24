import json
import urllib.request

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
