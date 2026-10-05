"""Datenzugriff des Prototyps: Momentaufnahme der öffentlichen /api/termine + Hilfen.

Die Momentaufnahme liegt in `daten/termine.json` (nicht im Git). Sie enthält auch vergangene
Termine, aber keine E-Mails oder internen Felder – `/api/termine` filtert die (DSGVO).
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date
from pathlib import Path

HIER = Path(__file__).resolve().parent
sys.path.insert(0, str(HIER.parent.parent))   # src/ – für shared.*

from shared.geo import _gem_norm  # noqa: E402
from shared.kollision import ist_regelgottesdienst  # noqa: E402
from shared.wiederholung import vorlage  # noqa: E402

DATEN_DIR = HIER / "daten"
TERMINE_FILE = DATEN_DIR / "termine.json"
QUELLE_URL = "https://vereinskalender.online/api/termine"

_cache: dict = {"mtime": 0.0, "daten": None, "vorschlaege": {}}


def hole_daten() -> int:
    """Neue Momentaufnahme laden (ein öffentlicher GET, keine Kosten). Gibt die Terminzahl zurück."""
    DATEN_DIR.mkdir(exist_ok=True)
    req = urllib.request.Request(QUELLE_URL, headers={"User-Agent": "VKO-Jahresplanung-Prototyp"})
    with urllib.request.urlopen(req, timeout=30) as r:
        roh = r.read()
    daten = json.loads(roh)
    tmp = TERMINE_FILE.with_suffix(".tmp")
    tmp.write_bytes(roh)
    tmp.replace(TERMINE_FILE)
    return len(daten.get("termine", []))


def daten() -> dict:
    """{labels, termine, meta, rubriken} – neu geladen, wenn sich die Datei geändert hat."""
    if not TERMINE_FILE.exists():
        return {"labels": {}, "termine": [], "meta": {}, "rubriken": {}}
    mtime = TERMINE_FILE.stat().st_mtime
    if _cache["daten"] is None or mtime != _cache["mtime"]:
        _cache.update(mtime=mtime, daten=json.loads(TERMINE_FILE.read_text()), vorschlaege={})
    return _cache["daten"]


def stand() -> dict:
    d = daten()
    if not TERMINE_FILE.exists():
        return {"vorhanden": False}
    tage = sorted(t.get("datum", "") for t in d["termine"] if t.get("datum"))
    return {"vorhanden": True, "anzahl": len(d["termine"]), "vereine": len(d["labels"]),
            "von": tage[0] if tage else "", "bis": tage[-1] if tage else "",
            "geholt": date.fromtimestamp(TERMINE_FILE.stat().st_mtime).strftime("%d.%m.%Y")}


def daten_ab() -> date | None:
    """Erster erfasster Termin (2026: Mai) – Serien bekommen die Monate davor ergänzt."""
    tage = [t.get("datum", "") for t in daten()["termine"] if t.get("datum")]
    try:
        return date.fromisoformat(min(tage)) if tage else None
    except ValueError:
        return None


def ausschliessen(t: dict) -> bool:
    """Für die Vorlage: Pfarrbrief-Gottesdienste (mit Messintentionen) nie übernehmen."""
    return ist_regelgottesdienst(t, daten()["meta"].get(t.get("verein", "")))


def vorschlaege(zieljahr: int) -> list[dict]:
    """Vorlage für alle Vereine (zwischengespeichert je Jahr und Datenstand)."""
    d = daten()
    if zieljahr not in _cache["vorschlaege"]:
        _cache["vorschlaege"][zieljahr] = vorlage(d["termine"], zieljahr, ausschliessen=ausschliessen,
                                                 daten_ab=daten_ab())
    return _cache["vorschlaege"][zieljahr]


def sitz(verein_key: str) -> tuple[str, str]:
    """(Gemeinde ohne Präfix, Landkreis) des Vereinssitzes."""
    m = daten()["meta"].get(verein_key) or {}
    return _gem_norm(m.get("ortschaft_gemeinde") or m.get("gemeinde") or ""), m.get("landkreis") or "Landkreis Landshut"


def gemeinden() -> list[dict]:
    """Gemeinden mit Vereinen: [{gemeinde, landkreis, anzahl}] nach Anzahl."""
    zaehler: dict = {}
    for k in daten()["labels"]:
        g, lk = sitz(k)
        if g:
            zaehler[(g, lk)] = zaehler.get((g, lk), 0) + 1
    return [{"gemeinde": g, "landkreis": lk, "anzahl": n}
            for (g, lk), n in sorted(zaehler.items(), key=lambda x: (-x[1], x[0]))]


def vereine_der_gemeinde(gemeinde: str, landkreis: str) -> list[tuple[str, str]]:
    """[(key, Name)] der Vereine mit Sitz in der Gemeinde. Pfarreien sind dabei (Pfarrfest!) –
    ihre Pfarrbrief-Gottesdienste filtert schon die Vorlage."""
    out = [(k, name) for k, name in daten()["labels"].items() if sitz(k) == (gemeinde, landkreis)]
    return sorted(out, key=lambda x: x[1].lower())


def vereine() -> list[tuple[str, str]]:
    return sorted(daten()["labels"].items(), key=lambda x: x[1].lower())
