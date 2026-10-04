#!/opt/rename-webhook/bin/python3
"""
kalender_erinnerung.py
Täglich 18:00: Sendet Telegram-Erinnerungen für morgige Termine an alle Abonnenten.
"""

import html
import json
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "/opt/rename-webhook")
from shared.secrets import load_secrets
from shared.vk_db import tg_get_all_subscriptions

VEREINSTERMINE_FILE = Path("/opt/rename-webhook/vereinstermine.json")


def load_kalender_bot_token() -> str:
    # Gemeinsamer Parser statt eigener Zeilenlogik (vorher lstrip("export") – Zeichen- statt
    # Präfix-Entfernung, Review 2026-10-04)
    return load_secrets().get("KALENDER_BOT_TOKEN", "")


def split_telegram_message(text: str, limit: int = 4096) -> list[str]:
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


def send(token: str, chat_id: str, text: str):
    for part in split_telegram_message(text):
        payload = json.dumps({"chat_id": chat_id, "text": part, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"  ❌ Fehler für {chat_id}: {e}")


def main():
    token = load_kalender_bot_token()
    if not token:
        print("❌ KALENDER_BOT_TOKEN nicht gefunden – abgebrochen.")
        return

    if not VEREINSTERMINE_FILE.exists():
        print("❌ vereinstermine.json nicht gefunden – abgebrochen.")
        return

    data   = json.loads(VEREINSTERMINE_FILE.read_text())
    labels = data.get("_labels", {})

    berlin = ZoneInfo("Europe/Berlin")
    morgen_dt = datetime.now(berlin) + timedelta(days=1)
    morgen    = morgen_dt.strftime("%Y-%m-%d")
    morgen_de = morgen_dt.strftime("%d.%m.%Y")
    wochentage = ["Montag","Dienstag","Mittwoch","Donnerstag","Freitag","Samstag","Sonntag"]
    morgen_wt  = wochentage[morgen_dt.weekday()]

    morgen_termine: dict[str, list] = {}
    for key, termine in data.items():
        if key.startswith("_") or not isinstance(termine, list):
            continue
        # Gelöschte (auch verworfene heimat-Importe, die als geloescht gespeichert sind) nie erinnern
        treffer = [t for t in termine if isinstance(t, dict) and t.get("datum") == morgen
                   and not t.get("geloescht") and not t.get("deleted")]
        if treffer:
            morgen_termine[key] = treffer

    if not morgen_termine:
        print(f"Keine Termine morgen ({morgen}) – nichts gesendet.")
        return

    alle_abos = tg_get_all_subscriptions()
    abos_per_chat: dict[str, list] = {}
    for abo in alle_abos:
        abos_per_chat.setdefault(abo["chat_id"], []).append(abo["verein_key"])

    gesendet = 0
    for chat_id, abonnierte_vereine in abos_per_chat.items():
        relevante = [k for k in abonnierte_vereine if k in morgen_termine]
        if not relevante:
            continue

        zeilen = [f"🔔 <b>Morgen, {morgen_wt} {morgen_de}:</b>\n"]
        for key in relevante:
            # parse_mode HTML: alle Inhalte escapen, sonst lehnt Telegram die ganze Nachricht ab
            e = html.escape
            verein_name = labels.get(key, key)
            zeilen.append(f"🏘️ <b>{e(verein_name)}</b>")
            for t in morgen_termine[key]:
                uhrzeit = f"⏰ {e(t['uhrzeit'])} Uhr\n" if t.get("uhrzeit") else ""
                ort     = f"📍 {e(t['ort'])}\n"         if t.get("ort")     else ""
                zeilen.append(f"📋 {e(t.get('bezeichnung',''))}\n{uhrzeit}{ort}")

        zeilen.append("─────────────────")
        zeilen.append("Abos ändern: /abo · Abmelden: /stop")
        send(token, chat_id, "\n".join(zeilen))
        gesendet += 1

    print(f"✅ Erinnerungen gesendet: {gesendet} Nutzer, {len(morgen_termine)} Vereine mit Terminen")


if __name__ == "__main__":
    main()
