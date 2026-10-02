#!/usr/bin/env python3
"""Offline-Abnahme für shared/geo.py – ohne Server, ohne Secrets, ohne pytest.

    python3 tests/test_geo.py            # Prüfungen
    python3 tests/test_geo.py --bericht  # zusätzlich die Differenzliste

Grundlage ist `tests/fixtures/termine.json`, ein Snapshot von /api/termine.
Die Erwartungswerte stammen aus der Messung vom 2026-09-30 (siehe ADR-014).
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.geo import (  # noqa: E402
    _ohne_adress_schwanz, eintrag_fuer, geo_fuer_termin, heimatort_of, treffer_im_text,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "termine.json"
STICHTAG = "2026-09-30"

_fehler: list[str] = []


def pruefe(bedingung, beschreibung, detail=""):
    if bedingung:
        print("  ok   %s" % beschreibung)
    else:
        print("  FEHL %s%s" % (beschreibung, ("  – " + str(detail)) if detail else ""))
        _fehler.append(beschreibung)


def laden():
    d = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return d["termine"], d.get("meta", {}), d.get("labels", {}), d.get("rubriken", {})


def geo(t, meta, labels):
    return geo_fuer_termin(t, meta.get(t.get("verein")), labels.get(t.get("verein"), ""))


def orte_von(t, meta, labels):
    g = geo(t, meta, labels)
    return g["orte"] if g else []


# ── Regeln einzeln ──────────────────────────────────────────────────────────
def test_regeln():
    print("\nRegeln")
    pruefe(_ohne_adress_schwanz("Kläranlage Bayerbach, Penk 30 a, 84092 Bayerbach b. Ergoldsbach")
           .strip() == "Kläranlage Bayerbach, Penk 30 a,",
           "PLZ-Schnitt entfernt den Adress-Schwanz")

    namen = {e["ort"] for e in treffer_im_text("Kläranlage Bayerbach, Penk 30 a, 84092 Bayerbach b. Ergoldsbach")}
    pruefe(namen == {"Bayerbach", "Penk"},
           "Adresse trifft Bayerbach und Penk, nicht Ergoldsbach", sorted(namen))

    pruefe({e["ort"] for e in treffer_im_text("Gasthaus Pritscher, Greilsberg")} == {"Greilsberg"},
           "Ort nach dem Komma wird gefunden")
    pruefe(treffer_im_text("Festplatz") == [],
           "reiner Gebäudename trifft nichts")
    pruefe({e["ort"] for e in treffer_im_text("Schloss Peuerbach")} == {"Bayerbach"},
           "Alias Schloss Peuerbach zeigt auf Bayerbach")
    pruefe(heimatort_of({}, "Landratsamt Landshut ( für Postau )") == "",
           "Label-Endung ohne Buchstaben erzeugt keine Ortschaft )")
    pruefe(heimatort_of({}, "FF Hölskofen") == "Hölskofen",
           "Label-Rückfall liefert den Ortsnamen")
    e = eintrag_fuer("Hölskofen")
    pruefe(e and e["gemeinde"] == "Bayerbach" and not e["hauptort"],
           "Hölskofen hängt an der Gemeinde Bayerbach und ist kein Hauptort")


# ── Abdeckung und Verteilung ────────────────────────────────────────────────
def test_abdeckung(termine, meta, labels):
    print("\nAbdeckung")
    kuenftig = [t for t in termine if t.get("datum", "") >= STICHTAG]
    ohne = [t for t in kuenftig if not orte_von(t, meta, labels)]
    pruefe(not ohne, "jeder künftige Termin bekommt mindestens einen Ort",
           [t.get("ort") for t in ohne[:5]])

    # Drei Termine nennen im Veranstaltungsort zwei Orte des Registers. Das ist
    # gewollt (Festlegung 1): der Termin wird einmal angezeigt, der Filter greift
    # bei jedem seiner Orte. Der Prototyp vom 2026-09-30 zählte nur zwei, weil
    # Jellenkofen damals noch nicht im Register stand.
    mehrfach = {frozenset(o) for t in kuenftig if len(o := orte_von(t, meta, labels)) > 1}
    pruefe(mehrfach == {frozenset({"Bayerbach", "Penk"}), frozenset({"Jellenkofen", "Prinkofen"})},
           "Mehrfachorte nur bei Adressen, die zwei Orte nennen",
           sorted(sorted(m) for m in mehrfach))

    # Ein Termin darf durch Mehrfachzuordnung nicht doppelt in der Liste stehen:
    # die Orte eines Termins sind ein Satz ohne Wiederholung.
    pruefe(all(len(o) == len(set(o)) for t in kuenftig if (o := orte_von(t, meta, labels))),
           "kein Ort erscheint doppelt am selben Termin")


def test_kernfaelle(termine, meta, labels, rubriken):
    print("\nKernfälle (Todo #414)")
    kuenftig = [t for t in termine if t.get("datum", "") >= STICHTAG]
    zahl = Counter(o for t in kuenftig for o in orte_von(t, meta, labels))

    pruefe(zahl["Paindlkofen"] == 21, "Paindlkofen: 21 Termine (vorher 0)", zahl["Paindlkofen"])
    pruefe(zahl["Oberköllnbach"] == 10, "Oberköllnbach: 10 Termine (vorher 0)", zahl["Oberköllnbach"])
    # 13 → 11: zwei Termine der Königstreuen Patrioten wandern nach Paindlkofen,
    # wo sie tatsächlich stattfinden (Gasthaus Pritscher). Die beiden
    # „Winklmoos"-Termine liegen in Hölskofen: Winklmoos ist ein Ort
    # (orte_frei.json) und verweist auf die Ortschaft Hölskofen.
    pruefe(zahl["Hölskofen"] == 11, "Hölskofen: 11 Termine (vorher 13)", zahl["Hölskofen"])

    pfarr = [t for t in kuenftig
             if rubriken.get(t.get("verein")) == "Pfarrei"
             and "Hölskofen" in orte_von(t, meta, labels)]
    pruefe(len(pfarr) == 3, "Hölskofen + Rubrik Pfarreien: die drei Gottesdienste", len(pfarr))

    # Der Auslöser: Termine der Pfarrgemeinde dürfen nicht pauschal unter Postau liegen
    postau = [t for t in kuenftig
              if labels.get(t.get("verein"), "").startswith("Pfarrgemeinde Postau")
              and "Postau" in orte_von(t, meta, labels)]
    pruefe(not postau, "keine Pfarr-Messe landet fälschlich in Postau", len(postau))


def test_orte_und_ortschaften():
    print("\nOrte vs. Ortschaften (ADR-014)")
    import shared.geo as g
    register, _ = g._lade()
    namen = [e["ort"] for e in register]
    pruefe(len(namen) == len(set(namen)), "keine doppelten Ortschaften")
    alle_alias = [a for e in register for a in (e.get("alias") or [])]
    pruefe(len(alle_alias) == len(set(alle_alias)) and not set(alle_alias) & set(namen),
           "Aliasse sind eindeutig und kein Ortschaftsname")
    fehlt = [e["ort"] for e in register
             if not (e.get("plz") and e.get("gemeinde") and e.get("landkreis"))]
    pruefe(not fehlt, "jede Ortschaft hat PLZ, Gemeinde und Landkreis", fehlt)

    frei = json.loads(g.ORTE_FREI_FILE.read_text(encoding="utf-8"))
    pruefe(all(f["ortschaft"] in namen for f in frei), "jeder Ort verweist auf eine Ortschaft")
    frei_namen = {f["name"].casefold() for f in frei}
    pruefe(not frei_namen & {n.casefold() for n in namen + alle_alias},
           "kein Ort steht zugleich als Ortschaft oder Alias im Register")

    # Winkelmoos ist amtliche Ortschaft (Bayerbach), Winklmoos nur ein Ort → Hölskofen
    pruefe(g.eintrag_fuer("Winkelmoos") and g.eintrag_fuer("Winkelmoos")["gemeinde"] == "Bayerbach",
           "Winkelmoos ist Ortschaft der Gemeinde Bayerbach")
    pruefe(g.eintrag_fuer("Winklmoos") is None, "Winklmoos ist keine Ortschaft")
    pruefe([e["ort"] for e in treffer_im_text("Weihnachtsmarkt Winklmoos")] == ["Hölskofen"],
           "Ort „Winklmoos“ führt zur Ortschaft Hölskofen")
    pruefe(geo_fuer_termin({"ort": "Winkelmoos 2"})["orte"] == ["Winkelmoos"],
           "Ortschaft Winkelmoos wird als Winkelmoos erkannt")

    pruefe(geo_fuer_termin({"ort": "Neufahrn"})["orte"] == ["Neufahrn"]
           and g.eintrag_fuer("Neufahrn")["plz"] == "84088",
           "Neufahrn (in Niederbayern) ist Ortschaft, PLZ 84088")

    # PLZ-Rückfall
    for text, soll in (("Rosemeyerstr. 1, 84061 Ergoldsbach", ["Ergoldsbach"]),
                       ("Goldbach Halle, Badstraße 20, 84061 Ergoldsbach", ["Ergoldsbach"])):
        ist = geo_fuer_termin({"ort": text})["orte"]
        pruefe(ist == soll, "PLZ-Rückfall: %s" % text, ist)
    pruefe(geo_fuer_termin({"ort": "Kläranlage Bayerbach, Penk 30 a, 84092 Bayerbach b. Ergoldsbach"})["orte"]
           == ["Bayerbach", "Penk"], "PLZ-Rückfall greift nicht, wenn vor der PLZ ein Treffer steht")

    # Kill-Switch für die Orte-Liste: Datei fehlt → ignorieren, Ortschaften gelten weiter
    alt = (g._register, g._muster, g._frei_muster, g.ORTE_FREI_FILE)
    g._register = g._muster = g._frei_muster = None
    g.ORTE_FREI_FILE = Path("/nicht/vorhanden/orte_frei.json")
    try:
        pruefe(geo_fuer_termin({"ort": "Hölskofen"})["orte"] == ["Hölskofen"]
               and not treffer_im_text("Winklmoos"),
               "ohne orte_frei.json gelten die Ortschaften weiter, Orte werden ignoriert")
    finally:
        g._register, g._muster, g._frei_muster, g.ORTE_FREI_FILE = alt


def test_killswitch():
    print("\nKill-Switch")
    import shared.geo as g
    alt_reg, alt_mus = g._register, g._muster
    g._register, g._muster = [], []
    try:
        pruefe(geo_fuer_termin({"ort": "Hölskofen"}) is None,
               "ohne Register liefert der Resolver None (Frontend fällt zurück)")
    finally:
        g._register, g._muster = alt_reg, alt_mus


# ── Registrierung: PLZ → Ortschaft (Todo #418) ──────────────────────────────
def test_plz():
    print("\nRegistrierung PLZ → Ortschaft")
    import shared.geo as g
    pruefe(g._lade_plz().get("plz"), "plz_gemeinden.json vorhanden und lesbar")
    gem = g.gemeinden_fuer_plz("84092")
    pruefe([(x["name"], x["landkreis"]) for x in gem] == [("Bayerbach", "Landkreis Landshut")],
           "84092 → Bayerbach, Landkreis Landshut", gem)
    pruefe([x["landkreis"] for x in g.gemeinden_fuer_plz("94137")] == ["Landkreis Rottal-Inn"],
           "94137 → das andere Bayerbach (Rottal-Inn)")
    pruefe({x["landkreis"] for x in g.gemeinden_fuer_plz("84036")} == {"Stadt Landshut", "Landkreis Landshut"},
           "84036 → zwei Gemeinden, Stadt und Landkreis getrennt")
    pruefe(g.gemeinden_fuer_plz("10115") and g.gemeinden_fuer_plz("20095"),
           "Stadtstaaten Berlin/Hamburg vorhanden")
    pruefe(g.gemeinden_fuer_plz("99999") == [], "unbekannte PLZ → leer")

    orte = g.orte_fuer_plz("84092")["orte"]
    pruefe("Hölskofen" in orte and "Winkelmoos" in orte and "Bayerbach" in orte,
           "Vorschläge 84092 enthalten Hölskofen, Winkelmoos, Bayerbach")
    pruefe("Paindlkofen" not in orte, "Vorschläge 84092 ohne Ortschaften anderer PLZ")
    pruefe("Bayerbach bei Ergoldsbach" not in orte, "Postort-Dublette „bei …“ entfällt")
    pruefe(g.orte_fuer_plz("8409")["orte"] == [], "ungültige PLZ → keine Vorschläge")

    a = g.ortschaft_aufloesen("Hölskofen", "84092")
    pruefe((a["gemeinde"], a["landkreis"], a["hinweise"]) == ("Bayerbach", "Landkreis Landshut", []),
           "Hölskofen/84092 → Bayerbach ohne Hinweis", a)
    a = g.ortschaft_aufloesen("Hölskofen", "84061")
    pruefe(a["gemeinde"] == "Ergoldsbach" and any("passt nicht" in h for h in a["hinweise"]),
           "falsche PLZ → Gemeinde aus der PLZ, Hinweis „passt nicht“", a)
    a = g.ortschaft_aufloesen("Bayerbach", "94137")
    pruefe(a["landkreis"] == "Landkreis Rottal-Inn" and not any("passt nicht" in h for h in a["hinweise"]),
           "gleichnamiger Ort anderswo → kein „passt nicht“", a)
    a = g.ortschaft_aufloesen("Musterdorf", "84092")
    pruefe(a["gemeinde"] == "Bayerbach" and any("nicht im Register" in h for h in a["hinweise"]),
           "unbekannte Ortschaft → Gemeinde der PLZ + Hinweis", a)
    a = g.ortschaft_aufloesen("Irgendwo", "84036")
    pruefe(a["gemeinde"] == "" and any("mehreren Gemeinden" in h for h in a["hinweise"]),
           "mehrdeutige PLZ → leer (Rückfall Nominatim) + Hinweis", a)

    # Städte: Stadtbezirke/-teile aus OSM
    orte = g.orte_fuer_plz("80807")["orte"]
    pruefe("Schwabing-Freimann" in orte and "Milbertshofen-Am Hart" in orte and "München" in orte,
           "80807 bietet Schwabing-Freimann und Milbertshofen-Am Hart an", orte)
    pruefe(not any(c.isdigit() for o in orte for c in o), "keine Münchner Stadtteilnummern („11.3“)", orte)
    pruefe("Ramersdorf-Perlach" not in g.orte_fuer_plz("81825")["orte"],
           "Ausreißer mit nur einer Straße entfällt (81825)")
    a = g.ortschaft_aufloesen("Schwabing-Freimann", "80807")
    pruefe((a["gemeinde"], a["landkreis"], a["hinweise"]) == ("München", "Stadt München", []),
           "Stadtteil → München, kein Register-Hinweis", a)
    pruefe(g.stadtteile_fuer_plz("84092") == [], "Land (84092): keine OSM-Stadtteile, Register zählt")

    alt = g._plz_daten
    g._plz_daten = {}
    try:
        pruefe(g.ortschaft_aufloesen("Musterdorf", "84092")["gemeinde"] == "",
               "Kill-Switch: ohne Schnappschuss leer → Rückfall Nominatim")
        pruefe(g.orte_fuer_plz("84092")["orte"][:1] == ["Bayerbach"],
               "Kill-Switch: Vorschläge weiter aus orte.json")
    finally:
        g._plz_daten = alt


# ── Bericht ─────────────────────────────────────────────────────────────────
def ohne_treffer(termine):
    """Veranstaltungsorte ohne Registertreffer – fehlende Orte werden sichtbar."""
    kuenftig = [t for t in termine if t.get("datum", "") >= STICHTAG and t.get("ort")]
    c = Counter(t["ort"] for t in kuenftig if not treffer_im_text(t["ort"])
                and not geo_fuer_termin({"ort": t["ort"]})["orte"])
    print("\nVeranstaltungsorte ohne Registertreffer (fallen auf den Heimatort des Vereins): %d Texte, %d Termine"
          % (len(c), sum(c.values())))
    for o, n in c.most_common():
        print("  %2d  %s" % (n, o))


def bericht(termine, meta, labels):
    kuenftig = [t for t in termine if t.get("datum", "") >= STICHTAG]
    alt = Counter(h for t in kuenftig
                  if (h := heimatort_of(meta.get(t.get("verein")), labels.get(t.get("verein"), ""))))
    neu = Counter(o for t in kuenftig for o in orte_von(t, meta, labels))
    print("\n\nDifferenz je Ortschaft (künftige Termine)")
    print("%-20s %6s %6s %8s" % ("Ortschaft", "heute", "neu", "Delta"))
    for n in sorted(set(alt) | set(neu)):
        a, b = alt.get(n, 0), neu.get(n, 0)
        if a != b:
            print("%-20s %6d %6d %+8d" % (n, a, b, b - a))
    print("\nTermine, die die Ortschaft wechseln")
    for t in kuenftig:
        h = heimatort_of(meta.get(t.get("verein")), labels.get(t.get("verein"), ""))
        o = orte_von(t, meta, labels)
        if h and h not in o:
            print("  %s  %-30s  %-14s -> %s" % (t["datum"], (t.get("ort") or "")[:30], h, ", ".join(o)))


def register_anzeigen(termine, meta, labels):
    """Das Register zur Durchsicht – mit Herkunft und Nutzung je Ort."""
    from shared.geo import _lade
    register, _ = _lade()
    kuenftig = [t for t in termine if t.get("datum", "") >= STICHTAG]
    nutzung = Counter(o for t in kuenftig for o in orte_von(t, meta, labels))
    heimat = Counter(h for k, m in meta.items()
                     if (h := heimatort_of(m, labels.get(k, ""))))
    print("\norte.json – %d Orte" % len(register))
    print("%-18s %-7s %-16s %-26s %-9s %-8s %s" %
          ("Ort", "PLZ", "Gemeinde", "Landkreis", "Hauptort", "Termine", "Alias / Beleg"))
    for e in sorted(register, key=lambda x: x["ort"]):
        if e.get("geprueft"):
            beleg = "von Josef bestätigt %s" % e["geprueft"]
        elif heimat.get(e["ort"]):
            beleg = "aus Vereinsdaten"
        else:
            beleg = "nur Nominatim – prüfen"
        alias = ", ".join(e.get("alias") or [])
        print("%-18s %-7s %-16s %-26s %-9s %-8s %s" % (
            e["ort"], e.get("plz") or "—", e.get("gemeinde", ""), e.get("landkreis", ""),
            "ja" if e.get("hauptort") else "", nutzung.get(e["ort"], 0) or "—",
            (alias + "  " if alias else "") + beleg))
    print("\nDatei: %s" % (Path(__file__).resolve().parent.parent / "orte.json"))


def main():
    termine, meta, labels, rubriken = laden()
    if "--register" in sys.argv:
        register_anzeigen(termine, meta, labels)
        return 0
    print("Fixture: %d Termine, %d Vereine" % (len(termine), len(labels)))
    test_regeln()
    test_abdeckung(termine, meta, labels)
    test_kernfaelle(termine, meta, labels, rubriken)
    test_orte_und_ortschaften()
    test_killswitch()
    test_plz()
    ohne_treffer(termine)
    if "--bericht" in sys.argv:
        bericht(termine, meta, labels)
    print("\n%s" % ("ALLE PRÜFUNGEN BESTANDEN" if not _fehler
                    else "%d FEHLGESCHLAGEN: %s" % (len(_fehler), "; ".join(_fehler))))
    return 1 if _fehler else 0


if __name__ == "__main__":
    sys.exit(main())
