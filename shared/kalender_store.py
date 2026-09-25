import fcntl
import json
import threading
from pathlib import Path
from typing import Callable

VEREINSTERMINE_FILE = Path("/opt/rename-webhook/vereinstermine.json")
_lock = threading.Lock()
_cache_data: dict | None = None
_cache_mtime: float = 0.0

# Aufräumen falls voriger Lauf zwischen write_text und replace abgestürzt ist
_tmp = VEREINSTERMINE_FILE.with_suffix(".json.tmp")
if _tmp.exists():
    _tmp.unlink()


class KalenderStore:
    @staticmethod
    def read() -> dict:
        global _cache_data, _cache_mtime
        try:
            mtime = VEREINSTERMINE_FILE.stat().st_mtime
        except OSError:
            return {}
        if _cache_data is not None and mtime == _cache_mtime:
            return _cache_data
        data = json.loads(VEREINSTERMINE_FILE.read_text())
        _cache_data = data
        _cache_mtime = mtime
        return data

    @staticmethod
    def update(mutator: Callable[[dict], None]) -> dict:
        global _cache_data, _cache_mtime
        with _lock:
            with open(VEREINSTERMINE_FILE, "r+") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    data = json.load(fh)
                    mutator(data)
                    tmp = VEREINSTERMINE_FILE.with_suffix(".json.tmp")
                    tmp.write_text(
                        json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    tmp.replace(VEREINSTERMINE_FILE)
                    _cache_data = None
                    _cache_mtime = 0.0
                    return data
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)


# Felder, die aus vereine_accounts in _meta[key] übernommen werden. Ohne sie steht
# der Verein in der Übersicht ohne Ort und fällt aus der Regionsfilterung heraus.
_META_FELDER = ("plz", "gemeinde", "landkreis", "heimatort", "rubrik")


def register_verein(verein_key: str, verein_name: str, row=None) -> None:
    """Freigegebenen Verein in vereinstermine.json bekannt machen.

    Ohne Eintrag in `_labels` ist ein freigegebener Verein in der Vereinsübersicht
    unsichtbar, bis er seinen ersten Termin anlegt oder importiert – der Kalender
    liest den Anzeigenamen ausschließlich aus `_labels[verein_key]`, nicht aus
    `vereine_accounts.verein_name` (Vorfall 2026-06-18: FFW Paindlkofen).

    `setdefault` durchgehend: ein Verein, der schon Termine hat, behält Label und
    `_meta` unverändert. Idempotent, mehrfacher Aufruf ändert nichts.

    Zentral hier, weil es zwei Freigabe-Wege gibt (API-Endpunkt und Telegram-Button)
    – siehe BKM/Atomic-Write-Pattern.md: eine Schreibstelle, nicht zwei.

    `row` ist die sqlite3.Row aus vereine_accounts, falls vorhanden – daraus werden
    PLZ, Gemeinde, Landkreis, Heimatort und Rubrik übernommen. Fehlen sie, ist der
    Verein in der Übersicht zwar sichtbar, aber ohne Ort und damit nicht über den
    Regionsfilter zu finden.
    """
    if not verein_key:
        return

    zusatz = {}
    if row is not None:
        for feld in _META_FELDER:
            try:
                wert = row[feld]
            except (KeyError, IndexError):
                continue
            if wert:
                zusatz[feld] = wert

    def _mutate(data: dict) -> None:
        data.setdefault("_labels", {}).setdefault(verein_key, verein_name)
        meta = data.setdefault("_meta", {}).setdefault(verein_key, {})
        meta.setdefault("selbstverwaltung", True)
        for feld, wert in zusatz.items():
            meta.setdefault(feld, wert)

    KalenderStore.update(_mutate)
