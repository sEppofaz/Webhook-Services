"""Cron-Überwachung per Heartbeat: Bewertung, Alarm-Entscheidung, Heartbeat-Dateien.

Jeder überwachte Cronjob läuft über `cronwrap.py NAME -- <Befehl>` und hinterlässt
eine Heartbeat-Datei `<PKA_CRON_DIR>/NAME.json`. `cron_watchdog.py` vergleicht sie
mit `cron_registry.json` und meldet überfällige, fehlgeschlagene und hängende Jobs
per Telegram. Siehe ADR-015.

**Bewusst ohne Abhängigkeiten ausser der Standardbibliothek** und ohne Netzwerk-
oder Secret-Zugriff – wie `shared/geo.py` (ADR-014), damit alles offline testbar ist
(`tests/test_cronwatch.py`). Das Versenden steckt in `cron_watchdog.py`.

Zeiten sind Ortszeit `Europe/Berlin` (Server-Timezone, CLAUDE.md „Cron-Jobs").
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
HEARTBEAT_DIR = Path(os.environ.get("PKA_CRON_DIR", "/var/lib/pka-cron"))
STATE_FILE_NAME = "_watchdog_state.json"

OK = "ok"
UNBEOBACHTET = "unbeobachtet"      # noch nicht auf cronwrap umgestellt (aktiv_seit leer) – kein Alarm
UEBERFAELLIG = "ueberfaellig"      # letzter Erfolg zu alt
FEHLGESCHLAGEN = "fehlgeschlagen"  # letzter Lauf mit Exit-Code != 0
HAENGT = "haengt"                  # läuft seit unplausibel langer Zeit
TOLERANZ_MIN = 2                   # Start darf so viel vor dem Soll-Zeitpunkt liegen (Uhrabweichung)


# ── Zeit ────────────────────────────────────────────────────────────────────
def jetzt_berlin() -> datetime:
    return datetime.now(TZ)


def parse_zeit(wert) -> datetime | None:
    """ISO-Zeitstempel → aware datetime; ohne Zeitzone gilt Europe/Berlin."""
    if not wert:
        return None
    try:
        dt = datetime.fromisoformat(str(wert))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def _hhmm(s: str) -> tuple[int, int]:
    h, m = str(s).split(":")
    return int(h), int(m)


def _tage(job: dict) -> set[int] | None:
    """Wochentage (Mo=0 … So=6) oder None für „täglich"."""
    d = job.get("days", "*")
    if d in ("*", None):
        return None
    return {int(x) for x in d}


def _am(tag: date, hhmm: str) -> datetime:
    h, m = _hhmm(hhmm)
    return datetime(tag.year, tag.month, tag.day, h, m, tzinfo=TZ)


def letzter_soll(job: dict, jetzt: datetime) -> datetime | None:
    """Jüngster geplanter Start ≤ jetzt bei Festzeit-Jobs (`at`); sonst None."""
    zeiten = job.get("at")
    if not zeiten:
        return None
    tage = _tage(job)
    for rueck in range(0, 9):  # selbst ein wöchentlicher Job hat in 8 Tagen einen Soll-Zeitpunkt
        tag = (jetzt - timedelta(days=rueck)).date()
        if tage is not None and tag.weekday() not in tage:
            continue
        kandidaten = [dt for dt in (_am(tag, z) for z in zeiten) if dt <= jetzt]
        if kandidaten:
            return max(kandidaten)
    return None


def _fenster(job: dict, tag: date) -> tuple[datetime, datetime] | None:
    f = job.get("zeitfenster")
    if not f:
        return None
    return _am(tag, f[0]), _am(tag, f[1])


# ── Bewertung ───────────────────────────────────────────────────────────────
def bewerte(job: dict, hb: dict | None, jetzt: datetime) -> dict:
    """Status eines Jobs: {"status", "grund"}. `hb` = Heartbeat oder None."""
    grace = timedelta(minutes=int(job.get("grace_min", 10)))
    seit = parse_zeit(job.get("aktiv_seit"))
    if seit is None or jetzt < seit:
        return {"status": UNBEOBACHTET, "grund": "noch nicht unter Aufsicht"}

    hb = hb or {}
    laeuft_seit = parse_zeit(hb.get("laeuft_seit"))
    max_lauf = timedelta(minutes=int(job.get("max_laufzeit_min", 60)))
    if laeuft_seit and jetzt - laeuft_seit > max_lauf:
        minuten = int((jetzt - laeuft_seit).total_seconds() // 60)
        return {"status": HAENGT, "grund": "läuft seit %d Min (erwartet höchstens %d)" % (minuten, max_lauf.seconds // 60)}

    erfolg = parse_zeit(hb.get("letzter_erfolg"))
    ende = parse_zeit(hb.get("letztes_ende"))
    exit_code = hb.get("letzter_exit")
    # Fehlschlag zählt, wenn er neuer ist als der letzte Erfolg
    if exit_code not in (None, 0) and ende and (erfolg is None or ende > erfolg):
        return {"status": FEHLGESCHLAGEN, "grund": "letzter Lauf endete mit Exit-Code %s" % exit_code}

    if job.get("every_min"):
        takt = timedelta(minutes=int(job["every_min"]))
        bezug = erfolg or seit
        fenster = _fenster(job, jetzt.date())
        if fenster:
            start, ende_f = fenster
            if not (start <= jetzt <= ende_f + grace):
                return {"status": OK, "grund": "ausserhalb des Zeitfensters"}
            bezug = max(bezug, start)
        if jetzt - bezug > takt + grace:
            return {"status": UEBERFAELLIG, "grund": _ueberfaellig_text(erfolg, jetzt)}
        return {"status": OK, "grund": ""}

    soll = letzter_soll(job, jetzt)
    if soll is None or soll < seit:
        return {"status": OK, "grund": "noch kein Soll-Zeitpunkt seit Aufsichtsbeginn"}
    if jetzt <= soll + grace:
        return {"status": OK, "grund": "Soll-Zeitpunkt noch innerhalb der Karenz"}
    start = parse_zeit(hb.get("letzter_start"))
    gelaufen = start is not None and start >= soll - timedelta(minutes=TOLERANZ_MIN)
    if not gelaufen:
        return {"status": UEBERFAELLIG,
                "grund": "Lauf von %s fehlt – %s" % (soll.strftime("%d.%m. %H:%M"), _ueberfaellig_text(erfolg, jetzt))}
    return {"status": OK, "grund": ""}


def _ueberfaellig_text(erfolg: datetime | None, jetzt: datetime) -> str:
    if erfolg is None:
        return "noch nie erfolgreich gemeldet"
    return "letzter Erfolg %s" % erfolg.astimezone(TZ).strftime("%d.%m. %H:%M")


# ── Alarm-Entscheidung (Dedupe, Erinnerung, Entwarnung) ─────────────────────
def entscheide(bewertungen: dict, state: dict, jetzt: datetime,
               erinnerung_h: int = 24) -> tuple[list[str], dict]:
    """Aus Bewertungen + bisherigem Zustand die zu sendenden Meldungen ableiten.

    Ein Alarm je Vorfall; Erinnerung frühestens nach `erinnerung_h`; Entwarnung,
    sobald ein alarmierter Job wieder ok ist. Ändert sich der Grund, gilt das als
    neuer Vorfall. Gibt (Meldungen, neuer Zustand) zurück – der Zustand wird erst
    nach erfolgreichem Versand gespeichert.
    """
    alt = dict((state or {}).get("alarme", {}))
    neu: dict = {}
    meldungen: list[str] = []
    for name in sorted(bewertungen):
        b = bewertungen[name]
        if b["status"] in (OK, UNBEOBACHTET):
            if name in alt:
                meldungen.append("✅ Cron „%s“ läuft wieder." % name)
            continue
        vorher = alt.get(name)
        zuletzt = parse_zeit((vorher or {}).get("zuletzt_gemeldet"))
        if vorher is None or vorher.get("status") != b["status"]:
            meldungen.append(_alarmtext(name, b, erinnerung=False))
            neu[name] = {"status": b["status"], "seit": jetzt.isoformat(), "zuletzt_gemeldet": jetzt.isoformat()}
        elif zuletzt is None or jetzt - zuletzt >= timedelta(hours=erinnerung_h):
            meldungen.append(_alarmtext(name, b, erinnerung=True))
            neu[name] = {**vorher, "zuletzt_gemeldet": jetzt.isoformat()}
        else:
            neu[name] = vorher
    return meldungen, {"alarme": neu}


_SYMBOL = {UEBERFAELLIG: "⏰", FEHLGESCHLAGEN: "❌", HAENGT: "🧱"}
_TITEL = {UEBERFAELLIG: "überfällig", FEHLGESCHLAGEN: "fehlgeschlagen", HAENGT: "hängt"}


def _alarmtext(name: str, b: dict, erinnerung: bool) -> str:
    kopf = "%s Cron „%s“ %s" % (_SYMBOL.get(b["status"], "⚠️"), name, _TITEL.get(b["status"], b["status"]))
    if erinnerung:
        kopf += " (weiterhin, Erinnerung)"
    return "%s\n%s" % (kopf, b["grund"])


def lebenszeichen(bewertungen: dict, jetzt: datetime) -> str:
    """Tägliche Kurzmeldung – bleibt sie aus, ist der Wächter (oder cron) selbst tot."""
    beob = [n for n, b in bewertungen.items() if b["status"] != UNBEOBACHTET]
    ok = [n for n in beob if bewertungen[n]["status"] == OK]
    schlecht = [n for n in beob if bewertungen[n]["status"] != OK]
    offen = len(bewertungen) - len(beob)
    zeile = "🫀 Cron-Wächter %s: %d/%d Jobs ok" % (jetzt.astimezone(TZ).strftime("%d.%m. %H:%M"), len(ok), len(beob))
    if schlecht:
        zeile += " – Problem: %s" % ", ".join(sorted(schlecht))
    if offen:
        zeile += "\n%d weitere noch nicht unter Aufsicht" % offen
    return zeile


# ── Registry und Dateien ────────────────────────────────────────────────────
def lade_registry(pfad: Path) -> dict:
    """Registry als {name: job}. Ein Fehler in einem Eintrag lässt die anderen gelten."""
    roh = json.loads(Path(pfad).read_text(encoding="utf-8"))
    jobs = {}
    for j in roh.get("jobs", []):
        n = j.get("name")
        if n and (j.get("every_min") or j.get("at")):
            jobs[n] = j
    return jobs


def _atomar_schreiben(pfad: Path, daten: dict) -> None:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(pfad.parent), prefix=pfad.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(daten, f, ensure_ascii=False)
        os.replace(tmp, pfad)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def lese_json(pfad: Path) -> dict | None:
    """Defekte oder fehlende Datei → None statt Absturz (ein kaputter Heartbeat darf nicht alles kippen)."""
    try:
        d = json.loads(Path(pfad).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def lese_heartbeat(name: str, verzeichnis: Path | None = None) -> dict | None:
    return lese_json((verzeichnis or HEARTBEAT_DIR) / ("%s.json" % name))


def heartbeat_start(name: str, verzeichnis: Path | None = None, jetzt: datetime | None = None) -> None:
    pfad = (verzeichnis or HEARTBEAT_DIR) / ("%s.json" % name)
    hb = lese_json(pfad) or {}
    t = (jetzt or jetzt_berlin()).isoformat()
    hb.update({"name": name, "letzter_start": t, "laeuft_seit": t})
    _atomar_schreiben(pfad, hb)


def heartbeat_ende(name: str, exit_code: int, verzeichnis: Path | None = None,
                   jetzt: datetime | None = None) -> None:
    pfad = (verzeichnis or HEARTBEAT_DIR) / ("%s.json" % name)
    hb = lese_json(pfad) or {"name": name}
    t = (jetzt or jetzt_berlin()).isoformat()
    hb.update({"letztes_ende": t, "letzter_exit": int(exit_code), "laeuft_seit": None})
    if int(exit_code) == 0:
        hb["letzter_erfolg"] = t
    _atomar_schreiben(pfad, hb)


def heartbeat_aus_log(log_pfad: str, jetzt: datetime | None = None) -> dict | None:
    """Schwaches Signal für noch nicht umgestellte Jobs: Änderungszeit des Logs gilt als Lauf."""
    try:
        mtime = datetime.fromtimestamp(os.stat(log_pfad).st_mtime, TZ)
    except OSError:
        return None
    t = mtime.isoformat()
    return {"letzter_start": t, "letztes_ende": t, "letzter_exit": 0, "letzter_erfolg": t, "laeuft_seit": None}


def speichere_zustand(state: dict, verzeichnis: Path | None = None) -> None:
    _atomar_schreiben((verzeichnis or HEARTBEAT_DIR) / STATE_FILE_NAME, state)


def lade_zustand(verzeichnis: Path | None = None) -> dict:
    return lese_json((verzeichnis or HEARTBEAT_DIR) / STATE_FILE_NAME) or {"alarme": {}}
