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

from stats_collector import collect_day

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


def send_telegram(token: str, chat_id: str, text: str) -> None:
    for part in split_telegram_message(text):
        payload = json.dumps({"chat_id": chat_id, "text": part, "parse_mode": "HTML"}).encode()
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


def get_live_today_stats() -> dict:
    """Live-Auswertung des laufenden Tages (00:00 bis jetzt) direkt aus dem nginx-Log –
    für den 20-Uhr-Bericht. Nutzt dieselbe Zähl-/Crawler-Logik wie
    stats_collector.collect_day(), schreibt aber nichts in die DB (sonst würde die
    7-Tage-Summe unvollständige mit vollständigen Tagen mischen).
    """
    heute = datetime.now(TZ_LOCAL).date()
    views, unique, _hourly, geo = collect_day(heute, max_files=1)
    de = sum(besucher for (land, _stadt), besucher in geo.items() if land == "Deutschland")
    return {
        "datum":  heute.strftime("%d.%m."),
        "views":  views,
        "unique": unique,
        "de":     de,
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


_NEU_AKTIONEN = ("erstellt", "upload", "upload_confirmed")


def verein_activity_stats() -> dict:
    """Zählt Neuanlagen und Änderungen durch Vereine aus vk_audit (UTC-Timestamps).
    'neu' umfasst sowohl Einzel-Anlagen (aktion='erstellt') als auch Massen-Uploads
    (aktion='upload'/'upload_confirmed') – letztere über die Spalte 'anzahl' gewichtet,
    da eine Audit-Zeile dort einen ganzen Upload-Batch (mehrere Termine) repräsentiert.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        from datetime import timezone
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        cutoff_24h = (now_utc - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        cutoff_7d  = (now_utc - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

        placeholders = ",".join("?" * len(_NEU_AKTIONEN))

        def _sum_neu(cutoff: str) -> int:
            return conn.execute(
                f"SELECT COALESCE(SUM(anzahl),0) FROM vk_audit WHERE aktion IN ({placeholders}) AND timestamp>=?",
                (*_NEU_AKTIONEN, cutoff)
            ).fetchone()[0]

        def _count_geaendert(cutoff: str) -> int:
            return conn.execute(
                "SELECT COUNT(*) FROM vk_audit WHERE aktion='geaendert' AND timestamp>=?",
                (cutoff,)
            ).fetchone()[0]

        result = {
            "neu_24h":       _sum_neu(cutoff_24h),
            "geaendert_24h": _count_geaendert(cutoff_24h),
            "neu_7d":        _sum_neu(cutoff_7d),
            "geaendert_7d":  _count_geaendert(cutoff_7d),
        }
        conn.close()
        return result
    except Exception:
        return {"neu_24h": 0, "geaendert_24h": 0, "neu_7d": 0, "geaendert_7d": 0}


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
    act            = verein_activity_stats()
    jetzt_dt       = datetime.now(TZ_LOCAL)
    jetzt          = jetzt_dt.strftime("%d.%m.%Y, %H:%M")

    # 00:10-Lauf: vollständiger, abgeschlossener Vortag aus der DB (von stats_collector 00:05 befüllt).
    # 20:00-Lauf: laufender Tag (00:00 bis jetzt) live aus dem nginx-Log – nicht identisch mit dem
    # DB-Wert, der bis zum nächsten 00:05-Lauf noch den davor liegenden Vortag zeigt.
    if jetzt_dt.hour < 12:
        tag_label = f"{stats['datum']} (Vortag, vollständig)"
        tag_views, tag_de = stats["views"], stats["de"]
    else:
        live = get_live_today_stats()
        tag_label = f"{live['datum']} (heute bis {jetzt_dt.strftime('%H:%M')} Uhr)"
        tag_views, tag_de = live["views"], live["de"]

    text = (
        f"📊 <b>Vereinskalender</b> · {jetzt}\n\n"
        f"📅 <b>{tag_label}</b> · Aufrufe: <b>{tag_views}</b> · 🇩🇪 <b>{tag_de}</b> Besucher\n"
        f"📆 7 Tage: <b>{stats['views_7d']}</b> Aufrufe · 🇩🇪 <b>{stats['de_7d']}</b> Besucher\n"
        f"🏛 Vereine: <b>{gesamt} gesamt</b>, davon <b>{aktiv} mit künftigen Terminen</b>\n"
        f"✏️ Vereinstermine 24h: <b>{act['neu_24h']} neu</b> · <b>{act['geaendert_24h']} geändert</b>"
        f" | 7 Tage: <b>{act['neu_7d']} neu</b> · <b>{act['geaendert_7d']} geändert</b>\n"
        f"📥 Letzter Import: <b>{letzter_import}</b>"
    )

    send_telegram(secrets["TOKEN"], secrets["CHAT_ID"], text)
    print(f"✅ Bericht gesendet ({jetzt})")


if __name__ == "__main__":
    main()
