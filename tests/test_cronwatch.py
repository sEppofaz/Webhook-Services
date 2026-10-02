#!/usr/bin/env python3
"""Offline-Abnahme für shared/cronwatch.py, cronwrap.py und cron_registry.json.

    python3 tests/test_cronwatch.py

Ohne Server, ohne Telegram, ohne Secrets, ohne pytest. Alle Zeiten sind feste
Berliner Ortszeiten, damit die Prüfungen unabhängig vom Tag des Laufs sind.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shared import cronwatch as cw  # noqa: E402

_fehler: list[str] = []


def pruefe(bedingung, beschreibung, detail=""):
    if bedingung:
        print("  ok   %s" % beschreibung)
    else:
        print("  FEHL %s%s" % (beschreibung, ("  – " + str(detail)) if detail else ""))
        _fehler.append(beschreibung)


def t(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=cw.TZ)


def hb(start=None, ende=None, exit_code=0, erfolg=None, laeuft=None):
    d = {"letzter_start": start, "letztes_ende": ende, "letzter_exit": exit_code,
         "letzter_erfolg": erfolg, "laeuft_seit": laeuft}
    return {k: v for k, v in d.items() if v is not None or k in ("letzter_exit", "laeuft_seit")}


def test_soll():
    print("\nSoll-Zeitpunkt")
    taeglich = {"at": ["06:30"]}
    pruefe(cw.letzter_soll(taeglich, t("2026-10-02 07:00")) == t("2026-10-02 06:30"), "täglich: heute 06:30")
    pruefe(cw.letzter_soll(taeglich, t("2026-10-02 06:00")) == t("2026-10-01 06:30"), "vor 06:30: gestern")
    zwei = {"at": ["00:10", "20:00"]}
    pruefe(cw.letzter_soll(zwei, t("2026-10-02 12:00")) == t("2026-10-02 00:10"), "zwei Zeiten: nimmt die jüngere ≤ jetzt")
    mi = {"at": ["07:00"], "days": [2]}  # Mittwoch
    pruefe(cw.letzter_soll(mi, t("2026-10-02 12:00")) == t("2026-09-30 07:00"), "wöchentlich Mi: Freitag → Mittwoch davor")
    pruefe(cw.letzter_soll({"at": ["06:00"], "days": [1, 3]}, t("2026-10-02 12:00")) == t("2026-10-01 06:00"),
           "Di+Do: Freitag → Donnerstag")
    pruefe(cw.letzter_soll({"at": ["06:00"], "days": [6]}, t("2026-10-02 12:00")) == t("2026-09-27 06:00"),
           "sonntags: Freitag → Sonntag davor")
    # Zeitumstellung: 2026-03-29 gibt es 02:30 nicht – darf nicht abstürzen
    try:
        cw.letzter_soll({"at": ["02:30"]}, t("2026-03-29 12:00"))
        pruefe(True, "Zeitumstellung (02:30 existiert nicht) wirft nicht")
    except Exception as ex:
        pruefe(False, "Zeitumstellung (02:30 existiert nicht) wirft nicht", ex)


def test_bewertung():
    print("\nBewertung")
    job = {"name": "x", "at": ["06:30"], "grace_min": 10, "aktiv_seit": "2026-09-01"}
    jetzt = t("2026-10-02 07:00")
    ok_hb = hb("2026-10-02T06:30:01+02:00", "2026-10-02T06:30:20+02:00", 0, "2026-10-02T06:30:20+02:00")
    pruefe(cw.bewerte(job, ok_hb, jetzt)["status"] == cw.OK, "Lauf heute erfolgt → ok")
    alt = hb("2026-10-01T06:30:01+02:00", "2026-10-01T06:30:20+02:00", 0, "2026-10-01T06:30:20+02:00")
    b = cw.bewerte(job, alt, jetzt)
    pruefe(b["status"] == cw.UEBERFAELLIG and "02.10. 06:30" in b["grund"], "Lauf von heute fehlt → überfällig", b)
    pruefe(cw.bewerte(job, alt, t("2026-10-02 06:35"))["status"] == cw.OK, "innerhalb der Karenz noch ok")
    pruefe(cw.bewerte(job, alt, t("2026-10-02 06:41"))["status"] == cw.UEBERFAELLIG, "nach der Karenz überfällig")
    pruefe(cw.bewerte(job, None, jetzt)["status"] == cw.UEBERFAELLIG, "nie gemeldet, aber unter Aufsicht → überfällig")
    pruefe(cw.bewerte({**job, "aktiv_seit": None}, None, jetzt)["status"] == cw.UNBEOBACHTET,
           "ohne aktiv_seit: unbeobachtet, kein Alarm")
    pruefe(cw.bewerte({**job, "aktiv_seit": "2026-10-03"}, None, jetzt)["status"] == cw.UNBEOBACHTET,
           "aktiv_seit in der Zukunft: noch unbeobachtet")
    pruefe(cw.bewerte({**job, "aktiv_seit": "2026-10-02"}, None, t("2026-10-02 07:00"))["status"] == cw.UEBERFAELLIG,
           "aktiv_seit heute, Soll 06:30 verpasst → überfällig (06:30 liegt nach Mitternacht von aktiv_seit)")
    pruefe(cw.bewerte({**job, "aktiv_seit": "2026-10-02T14:00"}, None, t("2026-10-02 14:30"))["status"] == cw.OK,
           "aktiv_seit mit Uhrzeit: der Soll-Zeitpunkt 06:30 vor Aufsichtsbeginn zählt nicht")

    fail = hb("2026-10-02T06:30:01+02:00", "2026-10-02T06:30:05+02:00", 3, "2026-10-01T06:30:20+02:00")
    pruefe(cw.bewerte(job, fail, jetzt)["status"] == cw.FEHLGESCHLAGEN, "Exit-Code 3 → fehlgeschlagen")
    genesen = hb("2026-10-02T06:50:01+02:00", "2026-10-02T06:50:05+02:00", 0, "2026-10-02T06:50:05+02:00")
    pruefe(cw.bewerte(job, genesen, jetzt)["status"] == cw.OK, "späterer Erfolg nach Fehlschlag → ok")
    haengt = hb("2026-10-02T05:00:00+02:00", None, 0, "2026-10-01T06:30:20+02:00", laeuft="2026-10-02T05:00:00+02:00")
    pruefe(cw.bewerte(job, haengt, jetzt)["status"] == cw.HAENGT, "läuft seit 2 h (Grenze 60 Min) → hängt")
    lang = {**job, "max_laufzeit_min": 180}
    pruefe(cw.bewerte(lang, haengt, jetzt)["status"] != cw.HAENGT, "höhere Laufzeitgrenze je Job")

    takt = {"name": "y", "every_min": 15, "grace_min": 5, "aktiv_seit": "2026-09-01"}
    frisch = hb("2026-10-02T06:50:00+02:00", "2026-10-02T06:50:02+02:00", 0, "2026-10-02T06:50:02+02:00")
    pruefe(cw.bewerte(takt, frisch, t("2026-10-02 07:00"))["status"] == cw.OK, "alle 15 Min: vor 10 Min gelaufen → ok")
    pruefe(cw.bewerte(takt, frisch, t("2026-10-02 07:15"))["status"] == cw.UEBERFAELLIG, "alle 15 Min: 25 Min her → überfällig")
    fenster = {"name": "n", "every_min": 10, "grace_min": 5, "zeitfenster": ["06:00", "22:00"], "aktiv_seit": "2026-09-01"}
    nacht = hb("2026-10-01T21:50:00+02:00", "2026-10-01T21:50:01+02:00", 0, "2026-10-01T21:50:01+02:00")
    pruefe(cw.bewerte(fenster, nacht, t("2026-10-02 03:00"))["status"] == cw.OK, "Zeitfenster: nachts kein Alarm")
    pruefe(cw.bewerte(fenster, nacht, t("2026-10-02 06:05"))["status"] == cw.OK, "Zeitfenster: kurz nach Fensterbeginn noch ok")
    pruefe(cw.bewerte(fenster, nacht, t("2026-10-02 06:30"))["status"] == cw.UEBERFAELLIG, "Zeitfenster: im Fenster nichts gelaufen → überfällig")


def test_alarm():
    print("\nAlarm-Entscheidung")
    jetzt = t("2026-10-02 07:00")
    schlecht = {"a": {"status": cw.UEBERFAELLIG, "grund": "weg"}, "b": {"status": cw.OK, "grund": ""}}
    m, zustand = cw.entscheide(schlecht, {"alarme": {}}, jetzt)
    pruefe(len(m) == 1 and "„a“" in m[0] and "überfällig" in m[0], "neuer Vorfall → genau eine Meldung", m)
    m2, zustand2 = cw.entscheide(schlecht, zustand, jetzt + timedelta(minutes=10))
    pruefe(m2 == [], "gleicher Vorfall 10 Min später → keine Meldung (Dedupe)", m2)
    m3, zustand3 = cw.entscheide(schlecht, zustand2, jetzt + timedelta(hours=25))
    pruefe(len(m3) == 1 and "Erinnerung" in m3[0], "nach 25 h → Erinnerung", m3)
    m4, _ = cw.entscheide(schlecht, zustand3, jetzt + timedelta(hours=26))
    pruefe(m4 == [], "direkt nach der Erinnerung wieder still", m4)
    gut = {"a": {"status": cw.OK, "grund": ""}, "b": {"status": cw.OK, "grund": ""}}
    m5, zustand5 = cw.entscheide(gut, zustand3, jetzt + timedelta(hours=27))
    pruefe(len(m5) == 1 and "läuft wieder" in m5[0] and zustand5["alarme"] == {}, "Rückkehr → Entwarnung, Zustand leer", m5)
    wechsel = {"a": {"status": cw.FEHLGESCHLAGEN, "grund": "Exit 1"}}
    m6, _ = cw.entscheide(wechsel, zustand, jetzt + timedelta(minutes=5))
    pruefe(len(m6) == 1 and "fehlgeschlagen" in m6[0], "anderer Status = neuer Vorfall → neue Meldung", m6)
    unb = {"u": {"status": cw.UNBEOBACHTET, "grund": ""}}
    m7, z7 = cw.entscheide(unb, {"alarme": {}}, jetzt)
    pruefe(m7 == [] and z7["alarme"] == {}, "unbeobachtete Jobs lösen nie etwas aus")
    pruefe(cw.entscheide({}, {}, jetzt)[1] == {"alarme": {}}, "leerer Zustand (None-ähnlich) wird verkraftet")
    lz = cw.lebenszeichen({"a": {"status": cw.OK, "grund": ""}, "b": {"status": cw.UNBEOBACHTET, "grund": ""},
                           "c": {"status": cw.UEBERFAELLIG, "grund": "x"}}, jetzt)
    pruefe("1/2" in lz and "Problem: c" in lz and "1 weitere" in lz, "Lebenszeichen zählt ok/unter Aufsicht/offen", lz)


def test_dateien():
    print("\nHeartbeat-Dateien")
    with tempfile.TemporaryDirectory() as d:
        v = Path(d)
        cw.heartbeat_start("job1", v, t("2026-10-02 06:30"))
        h = cw.lese_heartbeat("job1", v)
        pruefe(h and h["laeuft_seit"] and h["letzter_start"], "Start schreibt laeuft_seit und letzter_start", h)
        cw.heartbeat_ende("job1", 0, v, t("2026-10-02 06:31"))
        h = cw.lese_heartbeat("job1", v)
        pruefe(h["laeuft_seit"] is None and h["letzter_exit"] == 0 and h["letzter_erfolg"], "Ende 0: Erfolg gesetzt", h)
        cw.heartbeat_start("job1", v, t("2026-10-03 06:30"))
        cw.heartbeat_ende("job1", 2, v, t("2026-10-03 06:30"))
        h = cw.lese_heartbeat("job1", v)
        pruefe(h["letzter_exit"] == 2 and h["letzter_erfolg"].startswith("2026-10-02"), "Fehlschlag überschreibt den letzten Erfolg nicht", h)
        (v / "kaputt.json").write_text("{nicht json")
        pruefe(cw.lese_heartbeat("kaputt", v) is None, "kaputte Datei → None statt Absturz")
        pruefe(cw.lese_heartbeat("gibtsnicht", v) is None, "fehlende Datei → None")
        (v / "liste.json").write_text("[1,2]")
        pruefe(cw.lese_heartbeat("liste", v) is None, "falscher JSON-Typ → None")
        cw.speichere_zustand({"alarme": {"x": {"status": "ueberfaellig"}}}, v)
        pruefe(cw.lade_zustand(v)["alarme"]["x"]["status"] == "ueberfaellig", "Zustand round-trip")
        pruefe(cw.lade_zustand(v / "nirgends") == {"alarme": {}}, "fehlender Zustand → leer")
        pruefe(not [p for p in v.iterdir() if ".json." in p.name], "keine liegengebliebenen Temp-Dateien")
        log = v / "x.log"
        log.write_text("x")
        w = cw.heartbeat_aus_log(str(log))
        pruefe(w and w["letzter_exit"] == 0 and cw.parse_zeit(w["letzter_erfolg"]), "Log-Signal: Änderungszeit als Lauf")
        pruefe(cw.heartbeat_aus_log(str(v / "weg.log")) is None, "fehlendes Log → None")


def test_cronwrap():
    print("\ncronwrap")
    with tempfile.TemporaryDirectory() as d:
        env = {**os.environ, "PKA_CRON_DIR": d}
        wrap = [sys.executable, str(ROOT / "cronwrap.py")]
        r = subprocess.run(wrap + ["a", "--", sys.executable, "-c", "print('hallo')"], env=env, capture_output=True, text=True)
        pruefe(r.returncode == 0 and r.stdout.strip() == "hallo", "Ausgabe und Exit-Code 0 werden durchgereicht", (r.returncode, r.stdout))
        pruefe((Path(d) / "a.json").exists() and json.loads((Path(d) / "a.json").read_text())["letzter_exit"] == 0, "Heartbeat geschrieben")
        r = subprocess.run(wrap + ["b", "--", sys.executable, "-c", "import sys;sys.exit(7)"], env=env, capture_output=True, text=True)
        pruefe(r.returncode == 7 and json.loads((Path(d) / "b.json").read_text())["letzter_exit"] == 7, "Exit-Code 7 wird durchgereicht und vermerkt")
        r = subprocess.run(wrap + ["c", "--", "/nicht/vorhanden"], env=env, capture_output=True, text=True)
        pruefe(r.returncode == 127 and json.loads((Path(d) / "c.json").read_text())["letzter_exit"] == 127, "nicht startbarer Befehl → 127 vermerkt")
        r = subprocess.run(wrap + ["nur"], env=env, capture_output=True, text=True)
        pruefe(r.returncode == 2, "falscher Aufruf → Exit 2")
        # Heartbeat-Verzeichnis nicht beschreibbar: der Job muss trotzdem laufen
        env2 = {**os.environ, "PKA_CRON_DIR": "/proc/gibt/es/nicht"}
        r = subprocess.run(wrap + ["d", "--", sys.executable, "-c", "print('lief')"], env=env2, capture_output=True, text=True)
        pruefe(r.returncode == 0 and "lief" in r.stdout and "Heartbeat" in r.stderr, "unbeschreibbares Heartbeat-Verzeichnis bricht den Job nicht ab", (r.returncode, r.stderr[:120]))


def test_registry():
    print("\nRegistry")
    roh = json.loads((ROOT / "cron_registry.json").read_text(encoding="utf-8"))
    namen = [j.get("name") for j in roh["jobs"]]
    pruefe(len(namen) == len(set(namen)) and all(namen), "Namen eindeutig und nicht leer", namen)
    jobs = cw.lade_registry(ROOT / "cron_registry.json")
    pruefe(len(jobs) == len(roh["jobs"]), "jeder Eintrag hat Takt (every_min oder at)")
    formal = True
    for j in roh["jobs"]:
        for z in j.get("at", []) + (j.get("zeitfenster") or []):
            h, m = cw._hhmm(z)
            formal &= 0 <= h < 24 and 0 <= m < 60
        formal &= all(0 <= int(x) <= 6 for x in (j.get("days") if isinstance(j.get("days"), list) else []))
        formal &= not (j.get("every_min") and j.get("at"))
    pruefe(formal, "Uhrzeiten, Wochentage gültig; kein Job mit zwei Taktarten")
    pruefe(all(j.get("signal") != "log" or j.get("log_pfad", "").startswith("/var/log/") for j in roh["jobs"]),
           "Log-Signale zeigen auf /var/log")
    jetzt = t("2026-10-02 12:00")
    for n, j in jobs.items():
        b = cw.bewerte(j, None, jetzt)
        if b["status"] != cw.UNBEOBACHTET:
            pruefe(False, "Registry im Lieferzustand ohne aktiv_seit", n)
            break
    else:
        pruefe(True, "Lieferzustand: alle Jobs unbeobachtet (kein Alarmsturm beim ersten Lauf)")


if __name__ == "__main__":
    test_soll()
    test_bewertung()
    test_alarm()
    test_dateien()
    test_cronwrap()
    test_registry()
    print("\n%s" % ("ALLE PRÜFUNGEN BESTANDEN" if not _fehler else "%d FEHLGESCHLAGEN: %s" % (len(_fehler), "; ".join(_fehler))))
    sys.exit(1 if _fehler else 0)
