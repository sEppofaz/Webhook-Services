#!/opt/rename-webhook/bin/python3
"""plz_check.py – Wächter für den PLZ-Schnappschuss (Todo #419, schlanke Variante, Josef 2026-10-05)

Cron: monatlich am 1. um 05:00 über `cronwrap.py plz_check` (/etc/cron.d/pka-plz-check).

Die Quelldaten pflegt das fremde Projekt OpenPLZ (GitHub `openpotato/openplzapi.data`, Export aus
OpenStreetMap, unregelmäßig – zuletzt 2025-11). Der Job fragt nur, ob es dort eine neuere
`streets.csv` gibt als die, aus der unser `plz_gemeinden.json` gebaut ist. Nur dann:
Schnappschuss in ein Temp-Verzeichnis neu berechnen (tools/build_plz_gemeinden.py), mit dem
aktuellen vergleichen und Josef per Telegram melden, welche PLZ sich ändern und ob Vereinskonten
oder Register-Einträge (orte.json) betroffen sind.

Schreibt **nie** `plz_gemeinden.json` – die Datei liegt im Git, ein Schreibzugriff des Servers ließe
den nächsten `git pull` scheitern. Den Tausch macht eine Claude-Session lokal mit dem Bauwerkzeug.
Keine Mails an Vereine (2 Konten, 2026-10-05) – erst ab ca. 20 Konten überdenken.

    plz_check.py              # normaler Lauf
    plz_check.py --dry-run    # nichts senden, keinen Stand speichern, Meldung ausgeben
    plz_check.py --force      # auch rechnen, wenn der Export schon gemeldet wurde
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
import urllib.request
from pathlib import Path

BASIS = Path(__file__).resolve().parent
sys.path.insert(0, str(BASIS))
sys.path.insert(0, str(BASIS / "tools"))

SNAPSHOT   = BASIS / "plz_gemeinden.json"
ORTE       = BASIS / "orte.json"
DB_PATH    = BASIS / "vk_accounts.db"
STAND_FILE = Path("/var/lib/pka-plz/stand.json")
COMMITS    = ("https://api.github.com/repos/openpotato/openplzapi.data/commits"
              "?path=src/de/osm/streets.csv&per_page=1")
UA = {"User-Agent": "Vereinskalender-PLZ-Check/1.0 (vereinskalender.online)",
      "Accept": "application/vnd.github+json"}


def letzter_export() -> dict:
    """Neuester Commit der streets.csv im OpenPLZ-Repo: {sha, datum (YYYY-MM-DD)}."""
    req = urllib.request.Request(COMMITS, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        c = json.loads(r.read())[0]
    return {"sha": c["sha"], "datum": c["commit"]["committer"]["date"][:10]}


def vergleiche(alt: dict, neu: dict) -> dict:
    """PLZ, deren Gemeinden sich ändern, die neu sind oder wegfallen (Gemeinden als Namen)."""
    def gems(d, plz):
        e = d["plz"].get(plz)
        return sorted(d["gemeinden"].get(a, {}).get("name", a) for a in e["g"]) if e else []
    a, n = set(alt["plz"]), set(neu["plz"])
    geaendert = {p: (gems(alt, p), gems(neu, p)) for p in sorted(a & n) if gems(alt, p) != gems(neu, p)}
    return {"geaendert": geaendert, "neu": sorted(n - a), "weg": sorted(a - n)}


def betroffene(diff: dict) -> dict:
    plz_set = set(diff["geaendert"]) | set(diff["weg"])
    konten = []
    try:
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as c:   # nur lesen, nie eine leere DB anlegen
            konten = [r for r in c.execute(
                "SELECT verein_name, plz FROM vereine_accounts WHERE plz IS NOT NULL AND plz <> ''") if r[1] in plz_set]
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  Vereinskonten nicht lesbar: {e}", file=sys.stderr)
    register = []
    try:
        r = json.loads(ORTE.read_text())
        r = r if isinstance(r, list) else r.get("orte", [])
        register = [(e["ort"], e.get("plz", "")) for e in r if e.get("plz") in plz_set]
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  orte.json nicht lesbar: {e}", file=sys.stderr)
    return {"konten": konten, "register": register}


def meldung(export: dict, stand_alt: str, diff: dict, betr: dict) -> str:
    g, neu, weg = diff["geaendert"], diff["neu"], diff["weg"]
    zeilen = [f"📮 PLZ-Daten: neuer OpenPLZ-Export vom {export['datum']}",
              f"Unser Schnappschuss: Stand {stand_alt}.",
              f"Geänderte Gemeinden bei {len(g)} PLZ · neu {len(neu)} · weggefallen {len(weg)}"]
    if betr["konten"]:
        zeilen.append("⚠️ Vereinskonten betroffen: " +
                      ", ".join(f"{n} ({p})" for n, p in betr["konten"]))
    else:
        zeilen.append("Vereinskonten: nicht betroffen")
    if betr["register"]:
        zeilen.append("⚠️ Register-Einträge prüfen: " +
                      ", ".join(f"{o} ({p})" for o, p in betr["register"][:20]))
    for p, (vorher, nachher) in list(g.items())[:15]:
        zeilen.append(f"  {p}: {', '.join(vorher)} → {', '.join(nachher)}")
    if len(g) > 15:
        zeilen.append(f"  … und {len(g) - 15} weitere")
    zeilen.append("→ In einer Claude-Session: „PLZ-Schnappschuss neu bauen“ "
                  "(tools/build_plz_gemeinden.py, dann commit + deploy).")
    return "\n".join(zeilen)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    alt = json.loads(SNAPSHOT.read_text())
    stand_alt = alt.get("_stand", "")
    export = letzter_export()
    gemeldet = {}
    if STAND_FILE.exists():
        try:
            gemeldet = json.loads(STAND_FILE.read_text())
        except Exception:  # noqa: BLE001
            gemeldet = {}
    if not a.force and export["datum"] <= stand_alt:
        print(f"✅ Kein neuer Export (OpenPLZ {export['datum']}, Schnappschuss {stand_alt})")
        return 0
    if not a.force and gemeldet.get("sha") == export["sha"]:
        print(f"✅ Export {export['datum']} wurde am {gemeldet.get('gemeldet_am', '?')} schon gemeldet")
        return 0

    import build_plz_gemeinden as b   # erst hier: lädt nur bei neuem Export
    gemeinden = b.lade_gemeinden()
    if len(gemeinden) < 10000:
        raise SystemExit(f"Abbruch: nur {len(gemeinden)} Gemeinden geladen")
    with tempfile.TemporaryDirectory() as tmp:
        b.ZIEL = Path(tmp) / "plz_gemeinden.json"
        strassen = Path(tmp) / "streets.csv"
        req = urllib.request.Request(b.STREETS_URL, headers=b.UA)
        with urllib.request.urlopen(req, timeout=300) as r, open(strassen, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)   # streamen statt die ganze CSV in den Speicher
        neu = b.baue(strassen, gemeinden)
    if len(neu["plz"]) < 8000:
        raise SystemExit(f"Abbruch: nur {len(neu['plz'])} PLZ – Export unvollständig?")

    diff = vergleiche(alt, neu)
    text = meldung(export, stand_alt, diff, betroffene(diff))
    if a.dry_run:
        print(text)
        return 0
    from shared.secrets import load_secrets
    from shared.telegram import send_telegram
    cfg = load_secrets()
    send_telegram(cfg["TOKEN"], cfg["CHAT_ID"], text)
    STAND_FILE.parent.mkdir(parents=True, exist_ok=True)
    from datetime import date
    STAND_FILE.write_text(json.dumps({**export, "gemeldet_am": date.today().isoformat()}))
    print(f"📮 Gemeldet: Export {export['datum']}, {len(diff['geaendert'])} PLZ geändert")
    return 0


if __name__ == "__main__":
    sys.exit(main())
