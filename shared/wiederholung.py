"""Vorjahres-Vorlage: Termine eines Jahres nach ihrem Schema ins Folgejahr übertragen.

Prototyp auf Branch `jahresplanung` (Josef 2026-10-05, Idee 1b) – noch nicht in der Live-App.

Die meisten Vereinstermine folgen einem festen Schema: „jedes Jahr am Karfreitag“, „2. Samstag im
Juli“, „jeden 3. Sonntag im Monat“. Dieses Modul bestimmt für jeden Termin des Vorjahres eine Regel
und rechnet daraus das Datum im Zieljahr. Ergebnis sind **Vorschläge** – der Verein bestätigt,
verschiebt oder verwirft sie; nichts wird automatisch veröffentlicht.

Reihenfolge der Regeln (die erste passende gewinnt):
1. Feste Feiertage (gleiches Datum): Neujahr, 1. Mai, 15.8., 3.10., 1.11., Weihnachten, Silvester …
2. Bewegliche Feste relativ zu Ostern bzw. zum 1. Advent – wenn der Termin genau darauf fällt
   oder der Titel das Fest nennt („Pfingstfest“ am Samstag davor → „Samstag vor Pfingstsonntag“).
3. Serien: gleicher Titel in ≥ 3 Monaten mit derselben Monatsregel → „jeden 3. Sonntag im Monat“.
4. Standard: gleicher Wochentag, gleiche Woche im Monat („2. Samstag im Juli“), in der 5. Woche
   „letzter Samstag im Juli“ (eine 5. Woche gibt es nicht in jedem Monat).
Mehrtägige Termine (gleicher Titel an aufeinanderfolgenden Tagen) wandern als Block: die Regel
bestimmt der erste Samstag des Blocks (sonst der erste Tag), alle Tage verschieben sich gleich.

Nur Standardbibliothek – offline testbar wie `shared/geo.py`.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta

WOCHENTAGE = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
MONATE = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
          "September", "Oktober", "November", "Dezember")

# Feste Feiertage/Brauchtumstage: (Monat, Tag) → Name
FESTE_DATEN = {
    (1, 1): "Neujahr", (1, 6): "Heilige Drei Könige", (4, 30): "Vorabend 1. Mai",
    (5, 1): "1. Mai", (8, 15): "Mariä Himmelfahrt", (10, 3): "Tag der Deutschen Einheit",
    (11, 1): "Allerheiligen", (11, 11): "Martinstag", (12, 6): "Nikolaus",
    (12, 24): "Heiligabend", (12, 25): "1. Weihnachtsfeiertag", (12, 26): "2. Weihnachtsfeiertag",
    (12, 31): "Silvester",
}

# Bewegliche Feste: Name → (Bezug, Tage). Bezug „ostern“ = Ostersonntag, „advent“ = 1. Advent.
BEWEGLICH = {
    "Unsinniger Donnerstag": ("ostern", -52),
    "Faschingssamstag": ("ostern", -50),
    "Faschingssonntag": ("ostern", -49),
    "Rosenmontag": ("ostern", -48),
    "Faschingsdienstag": ("ostern", -47),
    "Aschermittwoch": ("ostern", -46),
    "Palmsonntag": ("ostern", -7),
    "Gründonnerstag": ("ostern", -3),
    "Karfreitag": ("ostern", -2),
    "Karsamstag": ("ostern", -1),
    "Ostersonntag": ("ostern", 0),
    "Ostermontag": ("ostern", 1),
    "Weißer Sonntag": ("ostern", 7),
    "Christi Himmelfahrt": ("ostern", 39),
    "Pfingstsonntag": ("ostern", 49),
    "Pfingstmontag": ("ostern", 50),
    "Fronleichnam": ("ostern", 60),
    "Volkstrauertag": ("advent", -14),
    "Buß- und Bettag": ("advent", -11),
    "Totensonntag": ("advent", -7),
    "1. Advent": ("advent", 0),
    "2. Advent": ("advent", 7),
    "3. Advent": ("advent", 14),
    "4. Advent": ("advent", 21),
}

# Stichwörter im Titel → Fest, an dem sich der Termin orientiert (auch wenn er Tage davor/danach liegt)
STICHWORTE = (
    (r"fasching|fasnet|fastnacht|kinderball|weiberfasching", "Faschingsdienstag"),
    (r"aschermittwoch|fischessen", "Aschermittwoch"),
    (r"palm", "Palmsonntag"),
    (r"karfreitag", "Karfreitag"),
    (r"oster|eiersuche|eierpecken", "Ostersonntag"),
    (r"himmelfahrt|vatertag", "Christi Himmelfahrt"),
    (r"pfingst", "Pfingstsonntag"),
    (r"fro(h|n|hn)leichnam", "Fronleichnam"),
    (r"volkstrauer", "Volkstrauertag"),
    (r"advent", "1. Advent"),
)

# Wie weit ein Stichwort-Termin vom Fest entfernt sein darf (z. B. Osterfeuer am Karsamstag,
# Kinderfasching zwei Wochen vor Faschingsdienstag). Weiter weg = Zufallstreffer im Titel.
_STICHWORT_MAX_TAGE = 21


def ostern(jahr: int) -> date:
    """Ostersonntag (gregorianisch, anonymer Algorithmus nach Meeus/Jones/Butcher)."""
    a = jahr % 19
    b, c = divmod(jahr, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    monat, tag = divmod(h + l - 7 * m + 114, 31)
    return date(jahr, monat, tag + 1)


def erster_advent(jahr: int) -> date:
    """1. Advent = vierter Sonntag vor dem 25.12."""
    weihnachten = date(jahr, 12, 25)
    return weihnachten - timedelta(days=weihnachten.weekday() + 1 + 21)


def fest_datum(name: str, jahr: int) -> date:
    bezug, tage = BEWEGLICH[name]
    basis = ostern(jahr) if bezug == "ostern" else erster_advent(jahr)
    return basis + timedelta(days=tage)


def nter_wochentag(jahr: int, monat: int, wochentag: int, n: int) -> date | None:
    """n-ter Wochentag im Monat (n=1..5), n=-1 = letzter. None, wenn es ihn nicht gibt."""
    if n == -1:
        naechster = date(jahr + (monat == 12), monat % 12 + 1, 1)
        d = naechster - timedelta(days=1)
        return d - timedelta(days=(d.weekday() - wochentag) % 7)
    erster = date(jahr, monat, 1)
    d = erster + timedelta(days=(wochentag - erster.weekday()) % 7 + 7 * (n - 1))
    return d if d.month == monat else None


def _ist_letzter(d: date) -> bool:
    return (d + timedelta(days=7)).month != d.month


def _monatsregel(d: date) -> tuple[int, int]:
    """(Wochentag, n) für ein Datum; n=-1 in der 5. Woche („letzter“)."""
    n = (d.day - 1) // 7 + 1
    return d.weekday(), (-1 if n == 5 else n)


def _monatsregel_text(wochentag: int, n: int, akkusativ: bool = False) -> str:
    letzter = "letzten" if akkusativ else "letzter"
    return f"{letzter if n == -1 else f'{n}.'} {WOCHENTAGE[wochentag]}"


def _norm_titel(titel: str) -> str:
    return re.sub(r"\W+", " ", (titel or "").lower()).strip()


def regel_fuer(d: date, titel: str = "") -> dict:
    """Regel für einen einzelnen Termin. Rückgabe: {typ, text, sicher, …} – mit `uebertrage()` anwenden."""
    for name, (bezug, tage) in BEWEGLICH.items():
        if fest_datum(name, d.year) == d:
            return {"typ": "fest", "fest": name, "abstand": 0, "text": name, "sicher": True}
    if (d.month, d.day) in FESTE_DATEN:
        return {"typ": "datum", "monat": d.month, "tag": d.day,
                "text": f"{FESTE_DATEN[(d.month, d.day)]} ({d.day}.{d.month}.)", "sicher": True}
    t = (titel or "").lower()
    for muster, name in STICHWORTE:
        if re.search(muster, t):
            abstand = (d - fest_datum(name, d.year)).days
            if abs(abstand) <= _STICHWORT_MAX_TAGE:
                return {"typ": "fest", "fest": name, "abstand": abstand,
                        "text": _abstand_text(d, name, abstand), "sicher": True}
    wt, n = _monatsregel(d)
    return {"typ": "monat", "wochentag": wt, "n": n,
            "text": f"{_monatsregel_text(wt, n)} im {MONATE[d.month - 1]}",
            "sicher": wt >= 4}   # Fr/Sa/So: Vereinsfeste – unter der Woche eher Zufall


def _abstand_text(d: date, fest: str, abstand: int) -> str:
    if abstand == 0:
        return fest
    wt = WOCHENTAGE[d.weekday()]
    if abs(abstand) <= 6:
        return f"{wt} {'vor' if abstand < 0 else 'nach'} {fest}"
    return f"{abs(abstand)} Tage {'vor' if abstand < 0 else 'nach'} {fest} ({wt})"


def uebertrage(regel: dict, alt: date, jahr: int) -> date | None:
    """Datum der Regel im Zieljahr. Für Monatsregeln der Monat von `alt`."""
    if regel["typ"] == "fest":
        return fest_datum(regel["fest"], jahr) + timedelta(days=regel["abstand"])
    if regel["typ"] == "datum":
        try:
            return date(jahr, regel["monat"], regel["tag"])
        except ValueError:   # 29.02.
            return date(jahr, 3, 1)
    if regel["typ"] == "monat":
        return nter_wochentag(jahr, alt.month, regel["wochentag"], regel["n"])
    return None


# ── Serien und Blöcke ────────────────────────────────────────────────────────

def _serienregel(daten: list[date]) -> tuple[int, int] | None:
    """Gemeinsame Monatsregel für Termine in ≥ 3 verschiedenen Monaten, sonst None.
    Prüft „n-ter“ und „letzter“ – der Seniorentreff am letzten Mittwoch liegt mal in Woche 4, mal 5."""
    monate = {(d.year, d.month) for d in daten}
    if len(monate) < 3 or len(monate) != len(daten):
        return None
    wochentage = {d.weekday() for d in daten}
    if len(wochentage) != 1:
        return None
    wt = wochentage.pop()
    ns = {(d.day - 1) // 7 + 1 for d in daten}
    if len(ns) == 1 and ns != {5}:
        return wt, ns.pop()
    if all(_ist_letzter(d) for d in daten):
        return wt, -1
    return None


def _bloecke(daten: list[date]) -> list[list[date]]:
    """Aufeinanderfolgende Tage zu Blöcken zusammenfassen (Volksfest Fr–So)."""
    bloecke: list[list[date]] = []
    for d in sorted(set(daten)):
        if bloecke and (d - bloecke[-1][-1]).days == 1:
            bloecke[-1].append(d)
        else:
            bloecke.append([d])
    return bloecke


def vorlage(termine: list[dict], zieljahr: int, quelljahr: int | None = None,
            ausschliessen=None, daten_ab: date | None = None) -> list[dict]:
    """Vorschläge fürs Zieljahr aus den Terminen des Quelljahres (Standard: Vorjahr).

    termine: Dicts mit `datum` (YYYY-MM-DD), `bezeichnung`, optional `uhrzeit`, `uhrzeit_bis`, `ort`,
    `verein`. ausschliessen: optionale Funktion(termin) → True = nicht übernehmen (z. B. Gottesdienste).
    Rückgabe sortiert nach neuem Datum; jeder Vorschlag hat `datum` (neu), `datum_vorjahr`,
    `regel` (Klartext), `regel_typ`, `sicher`, optional `serie`/`block` (gemeinsame Kennung).
    daten_ab: erster erfasster Tag des Datenbestands. Serien bekommen die Monate davor ergänzt
    (`ergaenzt: True`), weil die Lücke an der Erfassung liegt, nicht am Verein.
    """
    quelljahr = quelljahr or zieljahr - 1
    gruppen: dict[tuple, list[dict]] = defaultdict(list)
    for t in termine:
        try:
            d = date.fromisoformat(t.get("datum", ""))
        except ValueError:
            continue
        if d.year != quelljahr or (ausschliessen and ausschliessen(t)):
            continue
        gruppen[(t.get("verein", ""), _norm_titel(t.get("bezeichnung", "")))].append({**t, "_d": d})

    vorschlaege: list[dict] = []
    for (verein, _), liste in gruppen.items():
        # Gleicher Termin doppelt (gleiches Datum + Uhrzeit, z. B. aus zwei Importen) nur einmal
        eindeutig = {}
        for t in sorted(liste, key=lambda x: (x["_d"], x.get("uhrzeit", ""))):
            eindeutig.setdefault((t["_d"], t.get("uhrzeit", "")), t)
        liste = list(eindeutig.values())
        daten = sorted({t["_d"] for t in liste})
        kennung = f"{verein}:{_norm_titel(liste[0].get('bezeichnung', ''))}"

        serie = _serienregel(daten)
        if serie:
            wt, n = serie
            text = f"jeden {_monatsregel_text(wt, n, akkusativ=True)} im Monat"
            for t in liste:
                neu = nter_wochentag(zieljahr, t["_d"].month, wt, n)
                if neu:
                    vorschlaege.append(_vorschlag(t, neu, text, "serie", True, serie=kennung))
            # Monate vor Beginn der Datenerfassung (2026: erst ab Mai) ergänzen – als solche markiert,
            # der Verein verwirft sie, wenn die Serie dort pausiert.
            if daten_ab and daten_ab.year == quelljahr:
                erster = liste[0]
                for monat in range(1, daten_ab.month):
                    neu = nter_wochentag(zieljahr, monat, wt, n)
                    if neu:
                        v = _vorschlag(erster, neu, text, "serie", False, serie=kennung)
                        v["datum_vorjahr"] = ""
                        v["regel"] += f" (ergänzt: {MONATE[monat - 1]} {quelljahr} nicht erfasst)"
                        v["ergaenzt"] = True
                        vorschlaege.append(v)
            continue

        for i, block in enumerate(_bloecke(daten)):
            anker = next((d for d in block if d.weekday() == 5), block[0])
            regel = regel_fuer(anker, liste[0].get("bezeichnung", ""))
            neu_anker = uebertrage(regel, anker, zieljahr)
            if not neu_anker:
                continue
            versatz = neu_anker - anker
            text = regel["text"] + (f", {len(block)} Tage" if len(block) > 1 else "")
            for t in liste:
                if t["_d"] in block:
                    vorschlaege.append(_vorschlag(t, t["_d"] + versatz, text, regel["typ"], regel["sicher"],
                                                  block=f"{kennung}:{i}" if len(block) > 1 else None))
    vorschlaege.sort(key=lambda v: (v["datum"], v.get("uhrzeit", ""), v.get("verein", "")))
    return vorschlaege


def _vorschlag(t: dict, neu: date, text: str, typ: str, sicher: bool, serie=None, block=None) -> dict:
    # `_geo` mitnehmen: der Ort bleibt gleich, die Kollisionsprüfung spart sich die Neuberechnung
    v = {k: t[k] for k in ("verein", "bezeichnung", "uhrzeit", "uhrzeit_bis", "ort", "_geo") if t.get(k)}
    v.update({"datum": neu.isoformat(), "datum_vorjahr": t["_d"].isoformat(),
              "regel": f"wie {t['_d'].year}: {text}", "regel_typ": typ, "sicher": sicher})
    if serie:
        v["serie"] = serie
    if block:
        v["block"] = block
    return v
