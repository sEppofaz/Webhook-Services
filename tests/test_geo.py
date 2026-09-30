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
    # „Winklmoos"-Termine bleiben in Hölskofen: Winklmoos steht nicht im
    # Register, `ortschaft` liefert nur den Hauptort Bayerbach, und die
    # Spezifitäts-Regel zieht deshalb den Heimatort des Vereins vor.
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


# ── Bericht ─────────────────────────────────────────────────────────────────
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


def main():
    termine, meta, labels, rubriken = laden()
    print("Fixture: %d Termine, %d Vereine" % (len(termine), len(labels)))
    test_regeln()
    test_abdeckung(termine, meta, labels)
    test_kernfaelle(termine, meta, labels, rubriken)
    test_killswitch()
    if "--bericht" in sys.argv:
        bericht(termine, meta, labels)
    print("\n%s" % ("ALLE PRÜFUNGEN BESTANDEN" if not _fehler
                    else "%d FEHLGESCHLAGEN: %s" % (len(_fehler), "; ".join(_fehler))))
    return 1 if _fehler else 0


if __name__ == "__main__":
    sys.exit(main())
