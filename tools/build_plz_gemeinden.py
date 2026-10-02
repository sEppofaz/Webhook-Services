#!/usr/bin/env python3
"""Baut `plz_gemeinden.json`: PLZ → Gemeinde(n), Landkreis, Bundesland, Postorte.

Quelle (Josefs Wahl 2026-10-02, Gate A0 aus Todo #418): **OpenPLZ API**
(openplzapi.org, Daten unter ODbL-1.0, © OpenStreetMap-Mitwirkende).

- PLZ → Gemeindeschlüssel (AGS) und Postort: `streets.csv` aus
  github.com/openpotato/openplzapi.data (alle Straßen Deutschlands mit PLZ,
  Postort und Regionalschlüssel). Eine PLZ gehört zu jeder Gemeinde, in der
  Straßen mit dieser PLZ liegen.
- Gemeindename, Landkreis, Bundesland: `GET /de/FederalStates/{01..16}/Municipalities`
  (~11.000 Gemeinden, ~230 Seitenabrufe).

Ortsteile stehen **nicht** drin – die gibt es amtlich nicht bundesweit mit PLZ.
Die kommen aus der kuratierten `orte.json` (ADR-014). **Ausnahme Städte:** Die
OSM-Spalten `Borough` (Stadtbezirk) und `Suburb` (Stadtteil) werden je PLZ als
`t` übernommen – gepflegt nur in rund 1.100 PLZ (Berlin, Hamburg, München, Köln,
Stuttgart …), in Landshut/Regensburg/Nürnberg leer. Münchner Stadtteile sind dort
nur Nummern („11.3“) und werden verworfen.

Läuft lokal (Mac), nicht auf dem Server. Stdlib-only.

    python3 tools/build_plz_gemeinden.py                # lädt alles neu
    python3 tools/build_plz_gemeinden.py --streets X.csv  # vorhandene Straßendatei nutzen
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime
import json
import os
import re
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

STREETS_URL = "https://raw.githubusercontent.com/openpotato/openplzapi.data/main/src/de/osm/streets.csv"
API = "https://openplzapi.org/de/FederalStates/{land:02d}/Municipalities?page={page}&pageSize=50"
ZIEL = Path(__file__).resolve().parent.parent / "plz_gemeinden.json"
UA = {"User-Agent": "Vereinskalender-Schnappschuss/1.0 (vereinskalender.online)"}

# Streufehler: Eine Straße am Gemeinderand kann mit der PLZ der Nachbargemeinde
# getaggt sein. Eine Gemeinde zählt für eine PLZ nur, wenn dort mindestens so
# viele Straßen dieser PLZ liegen – oder wenn es die einzige Gemeinde der PLZ ist.
MIN_STRASSEN = 2


def _get_json(url: str):
    for versuch in range(4):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read()), r.headers
        except Exception as e:  # noqa: BLE001
            if versuch == 3:
                raise
            print(f"  Wiederhole ({e})", file=sys.stderr)
            time.sleep(3 * (versuch + 1))


def landkreis_label(district: dict | None) -> str:
    """Wie ADR-009: „Landkreis X" bzw. „Stadt X" (kreisfreie Städte)."""
    if not district:
        return ""
    name = (district.get("name") or "").split(",")[0].strip()
    typ = (district.get("type") or "").strip()
    if typ == "Landkreis":
        return f"Landkreis {name}"
    if typ in ("Kreisfreie Stadt", "Stadtkreis"):
        return f"Stadt {name}"
    if typ == "Kreis":
        return f"Kreis {name}"
    return name if not typ or typ in name else f"{typ} {name}"


def gemeinde_name(amtlich: str) -> str:
    """„Bayerbach b.Ergoldsbach" → „Bayerbach", „Ergoldsbach, M" → „Ergoldsbach".

    Bewusst nur die Zusätze, die Verwechslungen nicht auflösen: „a.d.Isar"
    bleibt, weil es Wörth a.d.Isar von Wörth a.d.Donau unterscheidet.
    """
    s = amtlich.split(",")[0].strip()
    s = re.sub(r"\s+b\.\s*.+$", "", s)          # „b.Ergoldsbach", „b. Landshut"
    s = re.sub(r"\s+bei\s+.+$", "", s)
    return s.strip()


def lade_gemeinden() -> dict:
    gemeinden = {}
    for land in range(1, 17):
        page, seiten = 1, 1
        while page <= seiten:
            daten, kopf = _get_json(API.format(land=land, page=page))
            seiten = int(kopf.get("x-total-pages") or 1)
            for g in daten:
                gemeinden[g["key"]] = {
                    "name": gemeinde_name(g["name"]),
                    "amtlich": g["name"],
                    "landkreis": landkreis_label(g.get("district")),
                    "bundesland": (g.get("federalState") or {}).get("name", ""),
                }
            page += 1
            time.sleep(0.3)
        print(f"Land {land:02d}: {seiten} Seiten, gesamt {len(gemeinden)} Gemeinden", file=sys.stderr)
    # Die Stadtstaaten liefert der Municipalities-Endpunkt nicht (leere Liste),
    # streets.csv kennt sie aber – ohne diese Zeilen fehlten ~290 PLZ.
    for ags, name in (("02000000", "Hamburg"), ("11000000", "Berlin")):
        gemeinden.setdefault(ags, {"name": name, "amtlich": name,
                                   "landkreis": f"Stadt {name}", "bundesland": name})
    return gemeinden


def lade_strassen(pfad: Path | None) -> Path:
    if pfad:
        return pfad
    tmp = Path(tempfile.gettempdir()) / "openplz_streets.csv"
    print(f"Lade {STREETS_URL} …", file=sys.stderr)
    req = urllib.request.Request(STREETS_URL, headers=UA)
    with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
        f.write(r.read())
    return tmp


def baue(strassen: Path, gemeinden: dict) -> dict:
    zaehler = collections.defaultdict(collections.Counter)   # plz → Counter(ags)
    postorte = collections.defaultdict(collections.Counter)  # plz → Counter(Postort)
    teile = collections.defaultdict(collections.Counter)     # plz → Counter((Stadtteil, ags))
    with open(strassen, encoding="utf-8", newline="") as f:
        for z in csv.DictReader(f):
            plz, ags = z.get("PostalCode", ""), z.get("RegionalKey", "")
            if len(plz) != 5 or not plz.isdigit() or len(ags) != 8:
                continue
            zaehler[plz][ags] += 1
            if z.get("Locality"):
                postorte[plz][z["Locality"].strip()] += 1
            for spalte in ("Borough", "Suburb"):
                name = (z.get(spalte) or "").strip()
                # Münchner Stadtteile sind Nummern („11.3“) – wertlos als Ortsangabe
                if name and re.search(r"[A-Za-zÄÖÜäöüß]", name):
                    teile[plz][(name, ags)] += 1

    plz_map, unbekannt = {}, set()
    for plz in sorted(zaehler):
        ags_liste = [a for a, n in zaehler[plz].most_common()
                     if n >= MIN_STRASSEN or len(zaehler[plz]) == 1]
        if not ags_liste:
            ags_liste = [zaehler[plz].most_common(1)[0][0]]
        bekannt = [a for a in ags_liste if a in gemeinden]
        unbekannt.update(a for a in ags_liste if a not in gemeinden)
        if not bekannt:
            continue
        plz_map[plz] = {
            "g": bekannt,
            "p": [p for p, n in postorte[plz].most_common() if n >= MIN_STRASSEN or len(postorte[plz]) == 1],
        }
        # Stadtbezirke/-teile: [Name, AGS], ohne Dubletten und ohne den Gemeindenamen selbst
        t, gesehen = [], set()
        for (name, ags), n in teile[plz].most_common():
            if n < MIN_STRASSEN or ags not in bekannt or name.casefold() in gesehen \
                    or name.casefold() == gemeinden[ags]["name"].casefold():
                continue
            gesehen.add(name.casefold())
            t.append([name, ags])
        if t:
            plz_map[plz]["t"] = t
    genutzt = {a for e in plz_map.values() for a in e["g"]}
    if unbekannt:
        print(f"Warnung: {len(unbekannt)} Gemeindeschlüssel ohne Gemeinde-Eintrag (verworfen)", file=sys.stderr)
    return {
        "_quelle": "OpenPLZ API (openplzapi.org), Daten © OpenStreetMap-Mitwirkende, ODbL-1.0",
        "_stand": datetime.date.today().isoformat(),
        "_hinweis": "Erzeugt mit tools/build_plz_gemeinden.py. g = Gemeindeschlüssel (AGS), p = Postorte, "
                    "t = Stadtbezirke/-teile aus OSM [Name, AGS] (nur wo gepflegt). "
                    "Ortsteile auf dem Land stehen in orte.json, nicht hier.",
        "plz": plz_map,
        "gemeinden": {a: gemeinden[a] for a in sorted(genutzt)},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--streets", type=Path, help="vorhandene streets.csv statt Download")
    ap.add_argument("--ziel", type=Path, default=ZIEL)
    a = ap.parse_args()

    gemeinden = lade_gemeinden()
    if len(gemeinden) < 10000:
        print(f"Abbruch: nur {len(gemeinden)} Gemeinden geladen (erwartet ~10.800)", file=sys.stderr)
        return 1
    daten = baue(lade_strassen(a.streets), gemeinden)
    if len(daten["plz"]) < 8000:
        print(f"Abbruch: nur {len(daten['plz'])} PLZ (erwartet ~8.200)", file=sys.stderr)
        return 1
    tmp = a.ziel.with_suffix(".tmp")
    tmp.write_text(json.dumps(daten, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, a.ziel)
    print(f"{a.ziel.name}: {len(daten['plz'])} PLZ, {len(daten['gemeinden'])} Gemeinden, "
          f"{a.ziel.stat().st_size // 1024} KB", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
