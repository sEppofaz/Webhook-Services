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

    erster = {"letzter_start": "2026-10-02T14:00:05+02:00", "letztes_ende": "2026-10-02T14:00:09+02:00", "letzter_exit": 0,
              "letzter_erfolg": "2026-10-02T14:00:09+02:00", "laeuft_seit": None, "erster_start": "2026-10-02T14:00:05+02:00"}
    selbst = {"name": "s", "at": ["06:30"], "grace_min": 10}  # kein aktiv_seit
    pruefe(cw.bewerte(selbst, erster, t("2026-10-02 14:30"))["status"] == cw.OK,
           "Selbstaufnahme: erster Heartbeat um 14:00, der Soll-Zeitpunkt 06:30 davor zählt nicht")
    pruefe(cw.bewerte(selbst, erster, t("2026-10-03 06:45"))["status"] == cw.UEBERFAELLIG,
           "Selbstaufnahme: am Folgetag fehlt der Lauf → überfällig")
    pruefe(cw.bewerte(selbst, None, t("2026-10-03 06:45"))["status"] == cw.UNBEOBACHTET,
           "ohne Heartbeat und ohne aktiv_seit weiter unbeobachtet")

    fail = hb("2026-10-02T06:30:01+02:00", "2026-10-02T06:30:05+02:00", 3, "2026-10-01T06:30:20+02:00")
    pruefe(cw.bewerte(job, fail, jetzt)["status"] == cw.FEHLGESCHLAGEN, "Exit-Code 3 → fehlgeschlagen")
    genesen = hb("2026-10-02T06:50:01+02:00", "2026-10-02T06:50:05+02:00", 0, "2026-10-02T06:50:05+02:00")
    pruefe(cw.bewerte(job, genesen, jetzt)["status"] == cw.OK, "späterer Erfolg nach Fehlschlag → ok")
    # Toleranz bei kurzen Intervallen: erst der zweite Fehlschlag in Folge alarmiert
    kurz = {"name": "k", "every_min": 10, "grace_min": 5, "aktiv_seit": "2026-09-01"}
    f1 = {**hb("2026-10-02T06:50:00+02:00", "2026-10-02T06:50:03+02:00", 1, "2026-10-02T06:40:00+02:00"), "fehler_in_folge": 1}
    f2 = {**f1, "fehler_in_folge": 2}
    pruefe(cw.bewerte(kurz, f1, t("2026-10-02 06:52"))["status"] == cw.OK, "alle 10 Min: erster Fehlschlag → noch kein Alarm")
    pruefe(cw.bewerte(kurz, f2, t("2026-10-02 06:52"))["status"] == cw.FEHLGESCHLAGEN, "alle 10 Min: zweiter in Folge → Alarm")
    pruefe(cw.bewerte({**job, "aktiv_seit": "2026-09-01"}, {**fail, "fehler_in_folge": 1}, jetzt)["status"] == cw.FEHLGESCHLAGEN,
           "Festzeit-Job (täglich): schon der erste Fehlschlag alarmiert")
    pruefe(cw.bewerte({**kurz, "alarm_ab_fehlern": 3}, f2, t("2026-10-02 06:52"))["status"] == cw.OK, "Schwelle je Job einstellbar (3)")
    pruefe(cw.bewerte({**kurz, "every_min": 60}, f1, t("2026-10-02 06:52"))["status"] == cw.FEHLGESCHLAGEN, "stündlicher Job: Schwelle 1")
    ohne_zaehler = hb("2026-10-02T06:50:00+02:00", "2026-10-02T06:50:03+02:00", 1, "2026-10-02T06:40:00+02:00")
    pruefe(cw.bewerte(kurz, ohne_zaehler, t("2026-10-02 06:52"))["status"] == cw.OK, "alter Heartbeat ohne Zähler zählt als ein Fehlschlag")
    pruefe(cw.alarm_schwelle({"every_min": 15}) == 2 and cw.alarm_schwelle({"at": ["06:00"]}) == 1 and cw.alarm_schwelle({"every_min": 30}) == 2,
           "Standardschwellen: ≤30 Min → 2, Festzeit → 1")

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
        pruefe(h["erster_start"].startswith("2026-10-02T06:30"), "erster_start wird einmal gesetzt", h)
        cw.heartbeat_start("job1", v, t("2026-10-03 06:30"))
        cw.heartbeat_ende("job1", 2, v, t("2026-10-03 06:30"))
        h = cw.lese_heartbeat("job1", v)
        pruefe(h["letzter_exit"] == 2 and h["letzter_erfolg"].startswith("2026-10-02"), "Fehlschlag überschreibt den letzten Erfolg nicht", h)
        pruefe(h["erster_start"].startswith("2026-10-02T06:30"), "erster_start bleibt beim zweiten Lauf unverändert", h)
        pruefe(h["fehler_in_folge"] == 1, "Fehlschlag zählt fehler_in_folge hoch", h)
        cw.heartbeat_start("job1", v, t("2026-10-04 06:30")); cw.heartbeat_ende("job1", 2, v, t("2026-10-04 06:31"))
        pruefe(cw.lese_heartbeat("job1", v)["fehler_in_folge"] == 2, "zweiter Fehlschlag in Folge → 2")
        cw.heartbeat_start("job1", v, t("2026-10-05 06:30")); cw.heartbeat_ende("job1", 0, v, t("2026-10-05 06:31"))
        pruefe(cw.lese_heartbeat("job1", v)["fehler_in_folge"] == 0, "Erfolg setzt den Zähler zurück")
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


def test_monatstag():
    print("\nTakt Monatstag")
    job = {"name": "geo", "at": ["04:00"], "monatstage": [1], "grace_min": 30, "aktiv_seit": "2026-09-01T00:00:00+02:00"}
    pruefe(cw.letzter_soll(job, t("2026-10-04 09:00")) == t("2026-10-01 04:00"), "am 4.10. ist der Soll-Zeitpunkt der 1.10. 04:00")
    pruefe(cw.letzter_soll(job, t("2026-10-01 03:59")) == t("2026-09-01 04:00"), "kurz vor 04:00 am 1. → Vormonat")
    pruefe(cw.letzter_soll(job, t("2026-03-31 12:00")) == t("2026-03-01 04:00"), "Ende eines 31-Tage-Monats findet den 1.")
    gelaufen = hb(start="2026-10-01T04:00:01+02:00", ende="2026-10-01T04:00:05+02:00", erfolg="2026-10-01T04:00:05+02:00")
    pruefe(cw.bewerte(job, gelaufen, t("2026-10-20 12:00"))["status"] == cw.OK, "Lauf am 1. vorhanden → ok bis zum nächsten 1.")
    vormonat = hb(start="2026-09-01T04:00:01+02:00", ende="2026-09-01T04:00:05+02:00", erfolg="2026-09-01T04:00:05+02:00")
    b = cw.bewerte(job, vormonat, t("2026-10-01 05:00"))
    pruefe(b["status"] == cw.UEBERFAELLIG and "01.10. 04:00" in b["grund"], "Lauf am 1.10. fehlt → überfällig", b)
    pruefe(cw.bewerte(job, vormonat, t("2026-10-01 04:20"))["status"] == cw.OK, "innerhalb der Karenz noch ok")
    beide = {**job, "monatstage": [1, 15]}
    pruefe(cw.letzter_soll(beide, t("2026-10-20 09:00")) == t("2026-10-15 04:00"), "mehrere Monatstage")


def test_dienste():
    print("\nDienste (systemd + Selbsttest)")
    jetzt = t("2026-10-04 09:00")
    d, k = {"name": "x"}, {"name": "kargl", "selbsttest": "/tmp/st.json"}
    pruefe(cw.bewerte_dienst("x", d, "active", None)["status"] == cw.OK, "active ohne Selbsttest → ok")
    for zwischen in ("activating", "reloading"):
        pruefe(cw.bewerte_dienst("x", d, zwischen, None)["status"] == cw.OK, "%s (Neustart) zählt nicht als Ausfall" % zwischen)
    aus = cw.bewerte_dienst("x", d, "failed", None)
    pruefe(aus["status"] == cw.DIENST_AUS and "failed" in aus["grund"] and "journalctl -u x" in aus["grund"],
           "failed → läuft nicht, mit Log-Befehl", aus)
    pruefe(cw.bewerte_dienst("x", d, "", None)["status"] == cw.DIENST_AUS, "leere Antwort → läuft nicht")
    ok_st = {"name": "ZUGFeRD-Selbsttest", "zeit": "2026-10-04T08:44:42+02:00", "ok": True, "fehler": []}
    pruefe(cw.bewerte_dienst("kargl", k, "active", ok_st)["status"] == cw.OK, "Selbsttest ok → ok")
    kaputt = {**ok_st, "ok": False, "fehler": ["Positionen: PostcodeCode unerwartet", "Pauschal: dito"]}
    b = cw.bewerte_dienst("kargl", k, "active", kaputt)
    pruefe(b["status"] == cw.SELBSTTEST and "PostcodeCode" in b["grund"] and "04.10. 08:44" in b["grund"]
           and "journalctl -u kargl" in b["grund"], "Selbsttest-Fehler → Alarm mit Fehlertext, Startzeit, Log-Befehl", b)
    pruefe(cw.bewerte_dienst("kargl", k, "active", None)["status"] == cw.SELBSTTEST, "Selbsttest-Datei fehlt → Alarm")
    eigen = cw.bewerte_dienst("ld", {"name": "ld", "log": "tail -n 50 /var/log/pka-ld.log"}, "failed", None)
    pruefe("tail -n 50 /var/log/pka-ld.log" in eigen["grund"] and "journalctl" not in eigen["grund"],
           "eigener Log-Befehl ersetzt journalctl", eigen)
    pruefe(cw.bewerte_dienst("kargl", k, "inactive", kaputt)["status"] == cw.DIENST_AUS, "Dienst aus hat Vorrang vor Selbsttest")
    bew = {"dienst:kargl": b, "cron_a": {"status": cw.OK, "grund": ""}}
    m, z = cw.entscheide(bew, {"alarme": {}}, jetzt)
    pruefe(len(m) == 1 and m[0].startswith("🧪 Dienst „kargl“ Selbsttest fehlgeschlagen"), "Alarmtext nennt Dienst, nicht Cron", m)
    m2, _ = cw.entscheide({"dienst:kargl": {"status": cw.OK, "grund": ""}}, z, jetzt + timedelta(minutes=10))
    pruefe(m2 == ["✅ Dienst „kargl“ läuft wieder."], "Entwarnung für Dienst", m2)
    lz = cw.lebenszeichen({"cron_a": {"status": cw.OK, "grund": ""}, "dienst:kargl": b,
                           "dienst:x": {"status": cw.OK, "grund": ""}}, jetzt)
    pruefe("1/1 Jobs ok" in lz and "1/2 Dienste ok" in lz and "Problem: kargl" in lz, "Lebenszeichen zählt Dienste getrennt", lz)
    dienste = cw.lade_dienste(ROOT / "cron_registry.json")
    pruefe("kargl-invoice" in dienste and "nginx" in dienste and "claude-code" not in dienste,
           "Registry: kargl-invoice + nginx überwacht, claude-code bewusst nicht", sorted(dienste))
    pruefe(dienste["kargl-invoice"].get("selbsttest", "").startswith("/opt/kargl-invoice/"), "kargl-invoice hat Selbsttest-Pfad")
    with tempfile.TemporaryDirectory() as tmp:
        reg = Path(tmp) / "r.json"
        reg.write_text('{"jobs": []}', encoding="utf-8")
        pruefe(cw.lade_dienste(reg) == {}, "Registry ohne Abschnitt dienste → nichts überwacht")


def test_timer_urls_system():
    print("\nTimer, Seiten, Zertifikate, Platte")
    jetzt = t("2026-10-04 09:10")
    cfg = {"max_alter_min": 90}
    gut = {"timer_aktiv": "active", "letzter_lauf": t("2026-10-04 09:00"), "result": "success", "laeuft": False}
    pruefe(cw.bewerte_timer("nf", cfg, gut, jetzt)["status"] == cw.OK, "Timer frisch + success → ok")
    pruefe(cw.bewerte_timer("nf", cfg, {**gut, "timer_aktiv": "inactive"}, jetzt)["status"] == cw.DIENST_AUS, "Timer inaktiv → läuft nicht")
    f = cw.bewerte_timer("nf", cfg, {**gut, "result": "exit-code"}, jetzt)
    pruefe(f["status"] == cw.FEHLGESCHLAGEN and "exit-code" in f["grund"] and "journalctl -u nf.service" in f["grund"],
           "letzter Lauf exit-code → fehlgeschlagen mit Log-Befehl", f)
    pruefe(cw.bewerte_timer("nf", cfg, {**gut, "result": "exit-code", "laeuft": True}, jetzt)["status"] == cw.OK,
           "läuft gerade erneut → altes Ergebnis zählt nicht")
    alt = cw.bewerte_timer("nf", cfg, {**gut, "letzter_lauf": t("2026-10-04 07:00")}, jetzt)
    pruefe(alt["status"] == cw.UEBERFAELLIG and "04.10. 07:00" in alt["grund"], "letzter Lauf zu alt → überfällig", alt)
    pruefe(cw.bewerte_timer("nf", cfg, {**gut, "letzter_lauf": None}, jetzt)["status"] == cw.UEBERFAELLIG, "nie gelaufen → überfällig")
    u = {"url": "https://x/kargl/"}
    pruefe(cw.bewerte_url(u, 200)["status"] == cw.OK, "URL 200 → ok")
    n = cw.bewerte_url(u, 404)
    pruefe(n["status"] == cw.NICHT_ERREICHBAR and "HTTP 404" in n["grund"] and "https://x/kargl/" in n["grund"], "URL 404 → nicht erreichbar", n)
    pruefe("URLError" in cw.bewerte_url(u, None, "URLError")["grund"], "keine Antwort → Fehlertyp im Text")
    pruefe(cw.bewerte_url({**u, "erwartet": [200, 302]}, 302)["status"] == cw.OK, "302 erlaubt, wenn erwartet")
    pruefe(cw.bewerte_url(u, 302)["status"] == cw.NICHT_ERREICHBAR, "302 ohne Erlaubnis → Alarm")
    pruefe(cw.bewerte_zertifikat("a.de", t("2026-11-05 04:00"), jetzt)["status"] == cw.OK, "Zertifikat 32 Tage → ok")
    z = cw.bewerte_zertifikat("a.de", t("2026-10-14 04:00"), jetzt)
    pruefe(z["status"] == cw.LAEUFT_AB and "14.10.2026" in z["grund"] and "noch 9 Tage" in z["grund"], "Zertifikat 9 Tage → Alarm", z)
    pruefe(cw.bewerte_zertifikat("a.de", t("2026-10-01 00:00"), jetzt)["status"] == cw.LAEUFT_AB, "abgelaufen → Alarm")
    pruefe(cw.bewerte_zertifikat("a.de", None, jetzt)["status"] == cw.LAEUFT_AB, "unlesbar → Alarm")
    pruefe(cw.bewerte_platte("/", 37.0)["status"] == cw.OK, "Platte 37 % → ok")
    v = cw.bewerte_platte("/", 91.2)
    pruefe(v["status"] == cw.FAST_VOLL and "91 %" in v["grund"], "Platte 91 % → fast voll", v)
    m, _ = cw.entscheide({"url:kargl": n, "zert:a.de": z, "timer:nf": f, "system:platte /": v}, {"alarme": {}}, jetzt)
    kopf = sorted(x.splitlines()[0] for x in m)
    pruefe(kopf == sorted(["❌ Timer „nf“ fehlgeschlagen", "💾 System „platte /“ fast voll",
                    "🌐 Seite „kargl“ nicht erreichbar", "🔒 Zertifikat „a.de“ läuft bald ab"]), "Alarmköpfe je Kategorie", kopf)
    lz = cw.lebenszeichen({"j": {"status": cw.OK, "grund": ""}, "url:kargl": n, "url:b": {"status": cw.OK, "grund": ""},
                           "timer:nf": {"status": cw.OK, "grund": ""}}, jetzt)
    pruefe("1/1 Jobs ok" in lz and "1/1 Timer ok" in lz and "1/2 Seiten ok" in lz and "Problem: kargl" in lz,
           "Lebenszeichen zählt Timer und Seiten", lz)
    zeilen = lz.split("\n")
    pruefe(zeilen[1] == "1/1 Jobs ok" and "1/1 Timer ok" in zeilen and
           any(z.startswith("⚠️ 1/2 Seiten ok – Problem: ") and "kargl" in z for z in zeilen),
           "Lebenszeichen: eine Zeile pro Bereich, Problem in seiner Zeile (2026-10-10)", zeilen)
    reg = ROOT / "cron_registry.json"
    timer, urls, grenz = cw.lade_dienste(reg, "timer"), cw.lade_dienste(reg, "urls"), cw.lade_grenzwerte(reg)
    pruefe({"newsletter-fetch", "orgkompass-erinnerungen", "certbot"} <= set(timer), "Registry: Timer eingetragen", sorted(timer))
    pruefe(all(u["url"].startswith("https://") for u in urls.values()) and len(urls) >= 10, "Registry: URLs https, mindestens 10")
    pruefe(grenz["zert_tage"] == 14 and grenz["platte_prozent"] == 85, "Grenzwerte geladen")


def test_abgleich():
    print("\nAbgleich: läuft, aber nicht überwacht")
    jetzt = t("2026-10-04 09:30")
    ist = {"dienst": {"kargl-invoice", "neue-app", "claude-code"}, "timer": {"newsletter-fetch", "neu-fetch"},
           "url": {"/kargl/", "/neu/", "/api/", "/acme/x/"}}
    bekannt = {"dienst": {"kargl-invoice"}, "timer": {"newsletter-fetch"}, "url": {"/kargl/"}}
    ign = {"dienst": ["claude-code"], "url": ["/api/", "/acme/*"]}
    r = cw.abgleich(ist, bekannt, ign)
    pruefe(sorted(r) == ["neu:dienst neue-app", "neu:timer neu-fetch", "neu:url /neu/"],
           "nur Neues, Bekanntes und Ignoriertes (auch per Muster) bleiben still", sorted(r))
    pruefe(all(b["status"] == cw.NICHT_UEBERWACHT and "Punkt 12" in b["grund"] for b in r.values()), "Hinweis auf BKM im Text")
    pruefe("unter „timer“" in r["neu:timer neu-fetch"]["grund"], "Hinweis nennt den passenden Registry-Abschnitt")
    pruefe(cw.abgleich(ist, ist, {}) == {}, "alles eingetragen → nichts")
    m, z = cw.entscheide(r, {"alarme": {}}, jetzt)
    pruefe(len(m) == 3 and any(x.startswith("🆕 Neu „dienst neue-app“ ist nicht überwacht") for x in m), "Alarmtext", m)
    m2, _ = cw.entscheide(r, z, jetzt + timedelta(hours=2))
    pruefe(m2 == [], "keine Wiederholung innerhalb von 24 h")
    m3, z3 = cw.entscheide({}, z, jetzt + timedelta(hours=3))
    pruefe(m3 == [] and z3["alarme"] == {}, "nach Eintragen still abgeräumt (kein falsches „läuft wieder“)", (m3, z3))
    lz = cw.lebenszeichen({"j": {"status": cw.OK, "grund": ""}, **r}, jetzt)
    pruefe("Nicht überwacht: dienst neue-app, timer neu-fetch, url /neu/" in lz and "Neu ok" not in lz, "Lebenszeichen listet Neues", lz)
    reg = ROOT / "cron_registry.json"
    ig = cw.lade_ignoriert(reg)
    pruefe("claude-code" in ig.get("dienst", []) and "/api/" in ig.get("url", []) and "_hinweis" not in ig, "Registry: ignoriert geladen", ig)


if __name__ == "__main__":
    test_soll()
    test_bewertung()
    test_alarm()
    test_dateien()
    test_cronwrap()
    test_registry()
    test_monatstag()
    test_dienste()
    test_timer_urls_system()
    test_abgleich()
    print("\n%s" % ("ALLE PRÜFUNGEN BESTANDEN" if not _fehler else "%d FEHLGESCHLAGEN: %s" % (len(_fehler), "; ".join(_fehler))))
    sys.exit(1 if _fehler else 0)
