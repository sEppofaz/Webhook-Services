#!/opt/rename-webhook/bin/python3
"""
kalender_report.py – Vereinskalender-Bericht per Telegram
Cron: täglich 00:10, 20:00 (Europe/Berlin)
Datenquelle: SQLite vk_accounts.db (verifiziert, ohne Crawler, mit GeoIP-DE)
"""

import json
import re
import sqlite3
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SECRETS_FILE        = Path("/etc/pka/secrets.env")
VEREINSTERMINE_FILE = Path("/opt/rename-webhook/vereinstermine.json")
LAST_IMPORT_FILE    = Path("/opt/rename-webhook/last_import.json")
DB_PATH             = Path("/opt/rename-webhook/vk_accounts.db")
TZ_LOCAL            = ZoneInfo("Europe/Berlin")


def load_secrets() -> dict:
    secrets = {}
    for line in SECRETS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^export\s+", "", line)
        if "=" in line:
            k, _, v = line.partition("=")
            secrets[k.strip()] = v.strip().strip('"').strip("'")
    return secrets


def send_telegram(token: str, chat_id: str, text: str) -> None:
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10)


def get_db_stats() -> dict:
    """Liest verifizierte Besucher-Daten (ohne Crawler) aus der SQLite-DB.
    Quelle: page_stats (gesamt) + page_stats_geo (nach Land, von stats_collector.py befüllt).
    """
    conn = sqlite3.connect(DB_PATH)

    letzter_tag = conn.execute("SELECT MAX(datum) FROM page_stats").fetchone()[0]
    cutoff_7d = (date.today() - timedelta(days=7)).isoformat()

    row = conn.execute(
        "SELECT COALESCE(views,0), COALESCE(unique_visitors,0) FROM page_stats WHERE datum=?",
        (letzter_tag,)
    ).fetchone() or (0, 0)

    row_7d = conn.execute(
        "SELECT COALESCE(SUM(views),0), COALESCE(SUM(unique_visitors),0) FROM page_stats WHERE datum>=?",
        (cutoff_7d,)
    ).fetchone() or (0, 0)

    de = conn.execute(
        "SELECT COALESCE(SUM(besucher),0) FROM page_stats_geo WHERE datum=? AND land='Deutschland'",
        (letzter_tag,)
    ).fetchone()[0]

    de_7d = conn.execute(
        "SELECT COALESCE(SUM(besucher),0) FROM page_stats_geo WHERE datum>=? AND land='Deutschland'",
        (cutoff_7d,)
    ).fetchone()[0]

    conn.close()

    try:
        datum_label = date.fromisoformat(letzter_tag).strftime("%d.%m.")
    except Exception:
        datum_label = letzter_tag

    return {
        "datum":    datum_label,
        "views":    row[0],
        "unique":   row[1],
        "views_7d": row_7d[0],
        "unique_7d":row_7d[1],
        "de":       de,
        "de_7d":    de_7d,
    }


def verein_stats() -> tuple[int, int]:
    if not VEREINSTERMINE_FILE.exists():
        return 0, 0
    try:
        data = json.loads(VEREINSTERMINE_FILE.read_text())
    except Exception:
        return 0, 0
    heute = datetime.now().strftime("%Y-%m-%d")
    gesamt = 0
    aktiv  = 0
    for key, items in data.items():
        if key.startswith("_") or not isinstance(items, list):
            continue
        gesamt += 1
        if any(t.get("datum", "") >= heute for t in items):
            aktiv += 1
    return gesamt, aktiv


def last_import_info() -> str:
    if not LAST_IMPORT_FILE.exists():
        return "–"
    try:
        li = json.loads(LAST_IMPORT_FILE.read_text())
        dt = datetime.strptime(li["datum"], "%Y-%m-%d %H:%M")
        return f"{dt.strftime('%d.%m.%Y, %H:%M')} ({li['termine']} Termine, {li['vereine']} Vereine)"
    except Exception:
        return "–"


def main():
    secrets        = load_secrets()
    stats          = get_db_stats()
    gesamt, aktiv  = verein_stats()
    letzter_import = last_import_info()
    jetzt          = datetime.now(TZ_LOCAL).strftime("%d.%m.%Y, %H:%M")

    text = (
        f"📊 <b>Vereinskalender</b> · {jetzt}\n\n"
        f"📅 <b>{stats['datum']}</b> · Aufrufe: <b>{stats['views']}</b> · Besucher: <b>{stats['unique']}</b>\n"
        f"🇩🇪 Deutschland verifiziert: <b>{stats['de']}</b>\n"
        f"📆 7 Tage: <b>{stats['views_7d']}</b> Aufrufe · <b>{stats['unique_7d']}</b> Besucher · <b>{stats['de_7d']}</b> DE\n"
        f"🏛 Vereine: <b>{gesamt} gesamt</b>, davon <b>{aktiv} mit künftigen Terminen</b>\n"
        f"📥 Letzter Import: <b>{letzter_import}</b>"
    )

    send_telegram(secrets["TOKEN"], secrets["CHAT_ID"], text)
    print(f"✅ Bericht gesendet ({jetzt})")


if __name__ == "__main__":
    main()
