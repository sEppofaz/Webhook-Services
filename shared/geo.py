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


def ortschaft_aus_name(name: str, gemeinde: str) -> str:
    """Ortschaft, die im Vereinsnamen steht – nur wenn sie zur Gemeinde gehört und eindeutig ist.

    „Freiwillige Feuerwehr Oberlindhart" → Oberlindhart, „Oberlindharther Theaterbrettl"
    → Oberlindhart (Adjektiv auf -er/-her zählt), „Eltern-Kind-Gruppen
    Mallersdorf-Pfaffenberg" → "" (zwei Ortschaften). Für den Heimatort neuer Vereine
    beim Import; sonst stünde dort der Gemeindename, der keine Ortschaft ist.
    """
    register, _ = _lade()
    gem = _gem_norm(gemeinde).casefold()
    if not name or not gem:
        return ""
    treffer = set()
    for e in register:
        if _gem_norm(e.get("gemeinde", "")).casefold() != gem:
            continue
        for n in [e.get("ort", "")] + list(e.get("alias") or []):
            if n and re.search(r"(?<![a-zäöüßA-ZÄÖÜ])" + re.escape(n) + r"(?:h?er)?(?![a-zäöüßA-ZÄÖÜ])",
                               name, re.I):
                treffer.add(e["ort"])
    return treffer.pop() if len(treffer) == 1 else ""


def _ort_norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def zuordnung_treffer(ort: str, verein: str, gemeinde: str, zuordnung: list | None) -> dict | None:
    """Passende Admin-Zuordnung (`_orte_zuordnung` in vereinstermine.json, v1.42) oder None.

    Gilt für den exakten Ortstext (Groß/Klein und Leerzeichen egal). Eine Zuordnung nur für
    einen Verein schlägt die für die ganze Gemeinde. `ausflug: true` (v1.43) markiert ein
    Ausflugsziel ohne eigene Ortschaft.
    """
    n = _ort_norm(ort)
    if not n or not zuordnung:
        return None
    passend = [z for z in zuordnung if isinstance(z, dict) and _ort_norm(z.get("ort")) == n]
    for z in passend:
        if z.get("verein") and z["verein"] == verein:
            return z
    gem = _gem_norm(gemeinde).casefold()
    for z in passend:
        if not z.get("verein") and gem and _gem_norm(z.get("gemeinde", "")).casefold() == gem:
            return z
    return None


def zuordnung_fuer(ort: str, verein: str, gemeinde: str, zuordnung: list | None) -> dict | None:
    """Register-Eintrag der passenden Admin-Zuordnung (None auch bei Ausflugszielen)."""
    z = zuordnung_treffer(ort, verein, gemeinde, zuordnung)
    return eintrag_fuer(z.get("ortschaft", "")) if z and not z.get("ausflug") else None


def geo_fuer_termin(termin: dict, meta_eintrag: dict | None = None,
                    label: str = "", zuordnung: list | None = None) -> dict | None:
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
    quelle = "ort" if eintraege else ""

    ausflug = False
    if not eintraege:
        z = zuordnung_treffer(termin.get("ort", ""), termin.get("verein") or termin.get("_vkey") or "",
                              (meta_eintrag or {}).get("gemeinde", ""), zuordnung)
        if z and z.get("ausflug"):
            ausflug = True      # Ausflugsziel: Ortschaft wie bisher aus Feld/Heimatort (Treffpunkt)
        elif z and eintrag_fuer(z.get("ortschaft", "")):
            eintraege, quelle = [eintrag_fuer(z["ortschaft"])], "zuordnung"

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
                quelle = "ort"

    # Landkreis-Abgleich (v1.40): Das Register sucht Namen ohne Regionsbezug, und
    # Allerweltsnamen gibt es in mehreren Landkreisen („Klause" ist eine Einöde in
    # Mallersdorf-Pfaffenberg). Trifft der Text Orte aus mehreren Landkreisen und ist
    # der Landkreis des Vereins darunter, bleiben nur dessen Treffer.
    verein_lk = str((meta_eintrag or {}).get("landkreis") or "").strip()
    if verein_lk and len(eintraege) > 1:
        eigene = [e for e in eintraege if e.get("landkreis") == verein_lk]
        if eigene:
            eintraege = eigene

    if not eintraege:
        aus_feld = eintrag_fuer(termin.get("ortschaft", ""))
        heimat = eintrag_fuer(heimatort_of(meta_eintrag, label))
        if aus_feld and aus_feld.get("hauptort") and heimat \
                and heimat["gemeinde"] == aus_feld["gemeinde"] \
                and heimat["ort"] != aus_feld["ort"]:
            eintraege, quelle = [heimat], "heimat"
        elif aus_feld:
            eintraege, quelle = [aus_feld], "ortschaft"
        elif heimat:
            eintraege, quelle = [heimat], "heimat"

    if ausflug and quelle != "ort":
        quelle = "ausflug"
    if not eintraege:
        return {"orte": [], "ortschaften": [], "plz": [], "gemeinden": [], "landkreise": [],
                "bundeslaender": [], "quelle": quelle if ausflug else ""}

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
        # Herkunft: "ort" (Name im Ortstext), "zuordnung" (Admin-Tab „Orte“),
        # "ortschaft" (Feld ortschaft), "heimat" (Heimatort des Vereins – nur geraten) oder
        # "ausflug" (Admin: Ausflugsziel, Ortschaft bleibt die aus Feld/Heimatort)
        "quelle":        quelle,
    }


# ── Registrierung: PLZ → Gemeinde → Ortschaft (Todo #418, Baustein A) ─────────
#
# Zwei Schichten: `plz_gemeinden.json` (amtlich, bundesweit, aus OpenPLZ, ODbL)
# kennt PLZ → Gemeinde(n), Landkreis, Bundesland und Postorte – aber keine
# Ortsteile. Die kommen aus `orte.json`. Ohne Schnappschuss-Datei (Kill-Switch)
# fällt die Registrierung auf `lookup_plz()` (Nominatim) zurück wie bisher.

PLZ_FILE = Path(__file__).resolve().parent.parent / "plz_gemeinden.json"
_plz_daten: dict | None = None


def _lade_plz() -> dict:
    global _plz_daten
    if _plz_daten is None:
        try:
            _plz_daten = json.loads(PLZ_FILE.read_text(encoding="utf-8"))
            if not isinstance(_plz_daten, dict):
                _plz_daten = {}
        except Exception:
            _plz_daten = {}
    return _plz_daten


def plz_gueltig(plz: str) -> bool:
    return bool(re.fullmatch(r"\d{5}", plz or ""))


def gemeinden_fuer_plz(plz: str) -> list[dict]:
    """Gemeinden einer PLZ: [{name, landkreis, bundesland}], leer wenn unbekannt."""
    d = _lade_plz()
    eintrag = (d.get("plz") or {}).get(plz or "")
    if not eintrag:
        return []
    gem = d.get("gemeinden") or {}
    return [dict(gem[a]) for a in eintrag.get("g", []) if a in gem]


def postorte_fuer_plz(plz: str) -> list[str]:
    return list(((_lade_plz().get("plz") or {}).get(plz or "") or {}).get("p", []))


def stadtteile_fuer_plz(plz: str) -> list[dict]:
    """Stadtbezirke/-teile aus OSM (nur wo gepflegt): [{name, gemeinde, landkreis}]."""
    d = _lade_plz()
    gem = d.get("gemeinden") or {}
    out = []
    for name, ags in ((d.get("plz") or {}).get(plz or "") or {}).get("t", []):
        if ags in gem:
            out.append({"name": name, "gemeinde": gem[ags]["name"], "landkreis": gem[ags]["landkreis"]})
    return out


def _register_mit_name(name: str) -> list[dict]:
    """Alle Register-Einträge mit diesem Namen oder Alias (Namensgleichheit möglich)."""
    register, _ = _lade()
    n = (name or "").strip().casefold()
    if not n:
        return []
    return [e for e in register
            if e.get("ort", "").casefold() == n
            or any(a.casefold() == n for a in (e.get("alias") or []))]


def orte_fuer_plz(plz: str) -> dict:
    """Vorschläge fürs Formular: Ortschaften (Register) + Gemeinde- und Postortnamen.

    Aus dem Register nur Einträge mit genau dieser PLZ – eine Gemeinde mit
    mehreren PLZ bietet so nur die Ortschaften an, die zur eingegebenen gehören.
    """
    if not plz_gueltig(plz):
        return {"plz": plz, "bekannt": False, "gemeinden": [], "orte": []}
    register, _ = _lade()
    gemeinden = gemeinden_fuer_plz(plz)
    namen: dict[str, str] = {}
    for e in register:
        if e.get("plz") == plz and e.get("ort"):
            namen.setdefault(e["ort"].casefold(), e["ort"])
    for g in gemeinden:
        namen.setdefault(g["name"].casefold(), g["name"])
    for t in stadtteile_fuer_plz(plz):
        namen.setdefault(t["name"].casefold(), t["name"])
    for p in postorte_fuer_plz(plz):
        # „Bayerbach bei Ergoldsbach" ist derselbe Ort wie „Bayerbach"
        if _gem_norm(p).casefold() not in namen:
            namen.setdefault(p.casefold(), p)
    return {
        "plz": plz,
        "bekannt": bool(gemeinden) or bool(namen),
        "gemeinden": [{"name": g["name"], "landkreis": g["landkreis"]} for g in gemeinden],
        "orte": sorted(namen.values(), key=str.casefold),
    }


def ortschaft_aufloesen(name: str, plz: str) -> dict:
    """Gemeinde/Landkreis zu Ortschaft + PLZ, dazu Hinweise für Josef.

    Rückgabe `{gemeinde, landkreis, hinweise: [..]}`. Leere `gemeinde` heisst:
    nicht eindeutig bestimmbar → Aufrufer fällt auf `lookup_plz()` zurück.
    Lehnt nie ab – die Hinweise gehen in die Telegram-Freigabemeldung.
    """
    hinweise: list[str] = []
    gemeinden = gemeinden_fuer_plz(plz)
    if not gemeinden and _lade_plz():
        hinweise.append(f"PLZ {plz} ist im PLZ-Verzeichnis unbekannt.")

    treffer = _register_mit_name(name)
    passend = [e for e in treffer if e.get("plz") == plz]
    if passend:
        e = passend[0]
        return {"gemeinde": e.get("gemeinde", ""), "landkreis": e.get("landkreis", ""),
                "hinweise": hinweise}
    # Stadtbezirk/-teil dieser PLZ (OSM)? Gilt als bekannt – kein Register-Hinweis,
    # sonst meldete jede Registrierung aus einer Großstadt „nicht im Register".
    if not treffer:
        teil = next((t for t in stadtteile_fuer_plz(plz)
                     if t["name"].casefold() == (name or "").strip().casefold()), None)
        if teil:
            return {"gemeinde": teil["gemeinde"], "landkreis": teil["landkreis"], "hinweise": hinweise}

    # Ortschaft = Gemeindename dieser PLZ?
    n = _gem_norm(name).casefold()
    gleich = [g for g in gemeinden if g["name"].casefold() == n]
    if treffer and not gleich:
        hinweise.append(f"PLZ {plz} passt nicht zur Ortschaft „{name}“ "
                        f"(Register: {treffer[0].get('plz')} {treffer[0].get('gemeinde')}).")
    else:
        # auch bei Namensgleichheit mit einem Register-Ort anderswo
        # (Bayerbach 94137, Landkreis Rottal-Inn ≠ Bayerbach 84092)
        hinweise.append(f"Ortschaft „{name}“ ({plz}) steht nicht im Register (orte.json).")

    wahl = gleich[0] if gleich else (gemeinden[0] if len(gemeinden) == 1 else None)
    if wahl:
        return {"gemeinde": wahl["name"], "landkreis": wahl["landkreis"], "hinweise": hinweise}
    if len(gemeinden) > 1:
        hinweise.append("PLZ gehört zu mehreren Gemeinden ("
                        + ", ".join(g["name"] for g in gemeinden) + ") – Gemeinde bitte prüfen.")
    return {"gemeinde": "", "landkreis": "", "hinweise": hinweise}
