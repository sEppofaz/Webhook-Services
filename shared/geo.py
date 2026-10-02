"""Geo-Zuordnung pro Termin: Ort, PLZ, Gemeinde, Landkreis, Bundesland.

Bis 2026-09-30 hingen alle Ortsfilter am **Verein** (`_ortOf[t.verein]` in
kalender.html). Ein Termin galt als „in Hölskofen", wenn der veranstaltende
Verein dort seinen Heimatort hat – nicht, wenn er dort stattfindet. Für eine
Pfarrei, die reihum in allen Kirchen des Verbands feiert, ist das falsch: Die
Hölskofener Messe lief unter „Postau", und ein Filter auf Hölskofen fand sie
nicht. Siehe ADR-014.

Dieses Modul bestimmt die Geo-Labels stattdessen aus dem Termin selbst.

**Bewusst ohne Abhängigkeiten ausser der Standardbibliothek.** `kalender_core`
erzwingt beim Import `os.environ["CLAUDE_API_KEY"]` und zieht Dropbox und PIL
mit; ein Resolver dort wäre ohne Secrets und ohne Server nicht ausführbar und
damit nicht offline testbar.

**Kill-Switch:** Fehlt `orte.json` oder ist es leer, liefert `geo_fuer_termin()`
`None`. Das Frontend fällt dann auf das alte, Verein-basierte Verhalten zurück.
Zurückrollen heisst also: Datei wegschieben, Service neu starten.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ORTE_FILE = Path(__file__).resolve().parent.parent / "orte.json"
# Orte im weiten Sinn (Lokale, Gebäude, falsche Schreibweisen) → amtliche Ortschaft.
# Getrennt von orte.json, damit dort nur amtliche Ortschaften stehen (ADR-014).
ORTE_FREI_FILE = Path(__file__).resolve().parent.parent / "orte_frei.json"

_register: list | None = None
_muster: list | None = None
_frei_muster: list | None = None


def _gem_norm(g: str) -> str:
    """Wie `_gemNormName()` in kalender.html – Präfix und „bei …" fallen weg."""
    s = re.sub(r"^(?:Gemeinde|Markt|Stadt)\s+", "", str(g or ""), flags=re.I)
    s = re.sub(r"\s+\(VGem\)$", "", s, flags=re.I)
    return re.sub(r"\s+(?:bei|b\.)\s+.+$", "", s, flags=re.I).strip()


def _lade() -> tuple[list, list]:
    """Register + vorkompilierte Suchmuster, einmal pro Prozess."""
    global _register, _muster, _frei_muster
    if _register is not None:
        return _register, _muster
    try:
        _register = json.loads(ORTE_FILE.read_text(encoding="utf-8"))
        if not isinstance(_register, list):
            _register = []
    except Exception:
        _register = []
    _muster = []
    for eintrag in _register:
        namen = [eintrag.get("ort", "")] + list(eintrag.get("alias") or [])
        # Längster Name zuerst: "Unterköllnbach" darf nicht an "Köllnbach" scheitern,
        # falls beide je im Register stehen.
        for n in sorted({n for n in namen if n}, key=len, reverse=True):
            _muster.append((
                re.compile(r"(?<![a-zäöüßA-ZÄÖÜ])" + re.escape(n) + r"(?![a-zäöüßA-ZÄÖÜ])", re.I),
                eintrag,
            ))
    _frei_muster = []
    try:
        frei = json.loads(ORTE_FREI_FILE.read_text(encoding="utf-8"))
    except Exception:
        frei = []
    for f in frei if isinstance(frei, list) else []:
        ziel = next((e for e in _register if e.get("ort") == f.get("ortschaft")), None)
        if not ziel:
            continue  # Verweis auf unbekannte Ortschaft: ignorieren statt falsch zuordnen
        for n in sorted({n for n in [f.get("name", "")] + list(f.get("alias") or []) if n},
                        key=len, reverse=True):
            _frei_muster.append((
                re.compile(r"(?<![a-zäöüßA-ZÄÖÜ])" + re.escape(n) + r"(?![a-zäöüßA-ZÄÖÜ])", re.I),
                ziel,
            ))
    return _register, _muster


def _ohne_adress_schwanz(text: str) -> str:
    """Schneidet ab der Postleitzahl ab.

    „Kläranlage Bayerbach, Penk 30 a, 84092 Bayerbach b. Ergoldsbach" nennt im
    Adress-Schwanz die Gemeinde Ergoldsbach, obwohl die Veranstaltung in
    Bayerbach/Penk liegt. Ohne den Schnitt zählte Ergoldsbach als dritter Ort.
    Bewusst nicht am ersten Komma geschnitten – das verlöre echte Treffer wie
    „Gasthaus Pritscher, Greilsberg" oder „Birish Pub, Bayerbach".
    """
    return re.split(r"\b\d{5}\b", str(text or ""))[0]


def treffer_im_text(text: str) -> list[dict]:
    """Alle Register-Einträge, die im Text als eigenständiges Wort vorkommen."""
    _lade()
    txt = _ohne_adress_schwanz(text)
    if not txt.strip():
        return []
    gefunden, gesehen = [], set()
    # Orte (orte_frei.json) zuerst, dann die amtlichen Ortschaften selbst
    for regex, eintrag in list(_frei_muster) + list(_muster):
        if eintrag["ort"] in gesehen:
            continue
        if regex.search(txt):
            gefunden.append(eintrag)
            gesehen.add(eintrag["ort"])
    return gefunden


def eintrag_fuer(name: str) -> dict | None:
    """Register-Eintrag zu einem exakten Ortsnamen (oder Alias)."""
    register, _ = _lade()
    n = (name or "").strip().casefold()
    if not n:
        return None
    for eintrag in register:
        if eintrag.get("ort", "").casefold() == n:
            return eintrag
        if any(a.casefold() == n for a in (eintrag.get("alias") or [])):
            return eintrag
    return None


def heimatort_of(meta_eintrag: dict | None, label: str = "") -> str:
    """Heimatort eines Vereins – wie `_ortNameOf()` in kalender.html.

    Fällt auf das letzte Wort des Labels zurück. Anders als dort werden
    Endungen ohne Buchstaben verworfen: „Landratsamt Landshut ( für Postau )"
    erzeugte sonst eine Ortschaft namens „)".
    """
    h = ((meta_eintrag or {}).get("heimatort") or "").strip()
    if h:
        return h
    if not label:
        return ""
    wort = label.strip().split()[-1].split("/")[0]
    return wort if len(wort) > 4 and wort[0].isalpha() else ""


def geo_fuer_termin(termin: dict, meta_eintrag: dict | None = None,
                    label: str = "") -> dict | None:
    """Geo-Labels eines Termins, oder None wenn kein Register vorhanden ist.

    Reihenfolge – die erste Stufe mit Ergebnis gewinnt:

    1. **Alle** Register-Treffer im Veranstaltungsort `ort`. Mehrere sind
       erlaubt und gewollt: „Kläranlage Bayerbach, Penk 30 a" liegt in beiden.
       Der Termin wird trotzdem nur einmal angezeigt, der Filter greift bei
       jedem seiner Orte.
    2. Das Feld `ortschaft`, sofern im Register.
    3. Der Heimatort des Vereins.

    **Spezifitäts-Regel:** Liefert Stufe 2 nur einen Hauptort und liegt der
    Heimatort des Vereins in derselben Gemeinde, gewinnt der Heimatort. Grund:
    `heimat_import.py` schreibt in `ortschaft` die Gemeinde, wenn es keinen Ort
    erkennt. Ohne diese Regel rutschte „Antoniusstüberl Mausham" vom Verein
    aus Feuchten auf die Gemeinde Bayerbach – also auf einen unspezifischeren
    Wert als vorher.
    """
    register, _ = _lade()
    if not register:
        return None

    eintraege = treffer_im_text(termin.get("ort", ""))

    if not eintraege:
        # Keine Ortschaft vor der PLZ: den Ort hinter der PLZ prüfen
        # („Rosemeyerstr. 1, 84061 Ergoldsbach"). Nur der erste Treffer – ein
        # Adress-Schwanz wie „84092 Bayerbach b. Ergoldsbach" nennt Nachbarn mit.
        rest = re.split(r"\b\d{5}\b", str(termin.get("ort", "")), maxsplit=1)
        if len(rest) > 1:
            treffer = []
            for regex, eintrag in _muster:
                m = regex.search(rest[1])
                if m:
                    treffer.append((m.start(), eintrag))
            if treffer:
                eintraege = [min(treffer, key=lambda x: x[0])[1]]

    if not eintraege:
        aus_feld = eintrag_fuer(termin.get("ortschaft", ""))
        heimat = eintrag_fuer(heimatort_of(meta_eintrag, label))
        if aus_feld and aus_feld.get("hauptort") and heimat \
                and heimat["gemeinde"] == aus_feld["gemeinde"] \
                and heimat["ort"] != aus_feld["ort"]:
            eintraege = [heimat]
        elif aus_feld:
            eintraege = [aus_feld]
        elif heimat:
            eintraege = [heimat]

    if not eintraege:
        return {"orte": [], "ortschaften": [], "plz": [], "gemeinden": [], "landkreise": [], "bundeslaender": []}

    def sammeln(feld):
        gesehen, out = set(), []
        for e in eintraege:
            v = e.get(feld) or ""
            if v and v not in gesehen:
                gesehen.add(v)
                out.append(v)
        return out

    return {
        "orte":          sammeln("ort"),
        # Ort mit seiner Gemeinde als Paar: die Chip-Label im Frontend heissen bei
        # Namensgleichheit "Weng (Postau)", dafuer reicht die Namensliste nicht.
        "ortschaften":   [{"ort": e["ort"], "gemeinde": e.get("gemeinde", ""),
                           "landkreis": e.get("landkreis", "")} for e in eintraege],
        "plz":           sammeln("plz"),
        "gemeinden":     sammeln("gemeinde"),
        "landkreise":    sammeln("landkreis"),
        "bundeslaender": sammeln("bundesland"),
    }
