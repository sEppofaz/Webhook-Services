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

import fnmatch
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
DIENST_AUS = "dienst_aus"          # systemd-Service läuft nicht
SELBSTTEST = "selbsttest"          # Service läuft, aber sein Start-Selbsttest meldet einen Fehler
DIENST_PREFIX = "dienst:"          # Schlüssel-Präfix in den Bewertungen, damit Dienste nicht mit Jobs kollidieren
NICHT_ERREICHBAR = "nicht_erreichbar"  # öffentliche URL liefert unerwarteten Status
LAEUFT_AB = "laeuft_ab"            # TLS-Zertifikat läuft bald ab
FAST_VOLL = "fast_voll"            # Platte über Grenzwert
NICHT_UEBERWACHT = "nicht_ueberwacht"  # läuft auf dem Server, steht aber nicht in der Registry
NEU_PREFIX = "neu:"
# Präfix → Bezeichnung in Meldungen und Lebenszeichen (Jobs ohne Präfix heissen „Cron“)
KATEGORIEN = {"dienst:": ("Dienst", "Dienste"), "timer:": ("Timer", "Timer"), "url:": ("Seite", "Seiten"),
              "zert:": ("Zertifikat", "Zertifikate"), "system:": ("System", "System"), NEU_PREFIX: ("Neu", "Neu")}
_ABGLEICH_HINWEIS = {
    "dienst": "läuft auf dem Server – in cron_registry.json unter „dienste“ eintragen",
    "timer": "ist aktiv – in cron_registry.json unter „timer“ eintragen (max_alter_min ≈ Takt + Puffer)",
    "url": "nginx-Pfad ohne Prüfung – in cron_registry.json unter „urls“ eintragen",
}
LAEUFT = ("active", "activating", "reloading")  # Neustart gerade im Gange zählt nicht als Ausfall


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
    monatstage = {int(x) for x in job.get("monatstage") or []}
    # wöchentlich: 8 Tage zurück reichen; monatlich (`monatstage`, z. B. [1]): bis 32 Tage
    for rueck in range(0, 33 if monatstage else 9):
        tag = (jetzt - timedelta(days=rueck)).date()
        if tage is not None and tag.weekday() not in tage:
            continue
        if monatstage and tag.day not in monatstage:
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
    hb = hb or {}
    # Selbstaufnahme: Mit dem ersten Heartbeat (erster Lauf über cronwrap) steht ein Job unter Aufsicht.
    # `aktiv_seit` in der Registry ist nur nötig, um einen Job VOR seinem ersten Heartbeat zu überwachen.
    seit = parse_zeit(job.get("aktiv_seit")) or parse_zeit(hb.get("erster_start"))
    if seit is None or jetzt < seit:
        return {"status": UNBEOBACHTET, "grund": "noch nicht unter Aufsicht"}

    laeuft_seit = parse_zeit(hb.get("laeuft_seit"))
    max_lauf = timedelta(minutes=int(job.get("max_laufzeit_min", 60)))
    if laeuft_seit and jetzt - laeuft_seit > max_lauf:
        minuten = int((jetzt - laeuft_seit).total_seconds() // 60)
        return {"status": HAENGT, "grund": "läuft seit %d Min (erwartet höchstens %d)" % (minuten, max_lauf.seconds // 60)}

    erfolg = parse_zeit(hb.get("letzter_erfolg"))
    ende = parse_zeit(hb.get("letztes_ende"))
    exit_code = hb.get("letzter_exit")
    # Fehlschlag zählt, wenn er neuer ist als der letzte Erfolg – und bei kurzen Intervallen erst ab
    # `schwelle` Fehlschlägen in Folge (ein einzelner API-Aussetzer bei einem */10-Job ist Rauschen)
    if exit_code not in (None, 0) and ende and (erfolg is None or ende > erfolg):
        folge = int(hb.get("fehler_in_folge") or 1)  # ältere Heartbeats ohne Zähler: ein Fehlschlag
        schwelle = alarm_schwelle(job)
        if folge >= schwelle:
            return {"status": FEHLGESCHLAGEN,
                    "grund": "letzter Lauf endete mit Exit-Code %s (%d Fehlschläge in Folge)" % (exit_code, folge)}
        return {"status": OK, "grund": "%d von %d tolerierten Fehlschlägen (Exit-Code %s)" % (folge, schwelle, exit_code)}

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


def bewerte_dienst(name: str, dienst: dict, aktiv: str, selbsttest: dict | None) -> dict:
    """Status eines systemd-Dienstes aus `systemctl is-active` und (optional) seiner Selbsttest-Datei."""
    log = "Log: %s" % (dienst.get("log") or "journalctl -u %s -n 50 --no-pager" % name)
    if aktiv not in LAEUFT:
        return {"status": DIENST_AUS, "grund": "systemd meldet „%s“\n%s" % (aktiv or "unbekannt", log)}
    if dienst.get("selbsttest"):
        if selbsttest is None:
            return {"status": SELBSTTEST, "grund": "kein Ergebnis in %s\n%s" % (dienst["selbsttest"], log)}
        if not selbsttest.get("ok"):
            fehler = selbsttest.get("fehler") or ["ohne Angabe"]
            zeit = parse_zeit(selbsttest.get("zeit"))
            wann = zeit.astimezone(TZ).strftime("%d.%m. %H:%M") if zeit else "unbekannt"
            return {"status": SELBSTTEST,
                    "grund": "%s (Start %s):\n%s\n%s" % (selbsttest.get("name", "Selbsttest"), wann,
                                                         "\n".join("- " + str(f)[:300] for f in fehler[:5]), log)}
    return {"status": OK, "grund": ""}


def bewerte_timer(name: str, cfg: dict, info: dict, jetzt: datetime) -> dict:
    """systemd-Timer: `info` = {timer_aktiv, letzter_lauf (datetime|None), result, laeuft}."""
    log = "Log: journalctl -u %s.service -n 50 --no-pager" % name
    if info.get("timer_aktiv") not in LAEUFT:
        return {"status": DIENST_AUS, "grund": "Timer ist „%s“\n%s" % (info.get("timer_aktiv") or "unbekannt", log)}
    if not info.get("laeuft") and info.get("result") not in (None, "", "success"):
        return {"status": FEHLGESCHLAGEN, "grund": "letzter Lauf: %s\n%s" % (info["result"], log)}
    lauf = info.get("letzter_lauf")
    max_alter = timedelta(minutes=int(cfg.get("max_alter_min", 60)))
    if lauf is None or jetzt - lauf > max_alter:
        wann = lauf.astimezone(TZ).strftime("%d.%m. %H:%M") if lauf else "nie"
        return {"status": UEBERFAELLIG, "grund": "letzter Lauf %s (erwartet alle %d Min)\n%s" % (wann, max_alter.total_seconds() // 60, log)}
    return {"status": OK, "grund": ""}


def bewerte_url(cfg: dict, code: int | None, fehler: str = "") -> dict:
    erwartet = cfg.get("erwartet") or [200]
    if code in erwartet:
        return {"status": OK, "grund": ""}
    ist = "HTTP %s" % code if code else (fehler or "keine Antwort")
    return {"status": NICHT_ERREICHBAR, "grund": "%s → %s (erwartet %s)" % (cfg["url"], ist, "/".join(map(str, erwartet)))}


def bewerte_zertifikat(name: str, ablauf: datetime | None, jetzt: datetime, tage: int = 14) -> dict:
    if ablauf is None:
        return {"status": LAEUFT_AB, "grund": "Ablaufdatum nicht lesbar (/etc/letsencrypt/live/%s/cert.pem)" % name}
    rest = ablauf - jetzt
    if rest < timedelta(days=tage):
        return {"status": LAEUFT_AB, "grund": "läuft am %s ab (noch %d Tage) – Erneuerung prüfen: journalctl -u certbot -n 50 --no-pager"
                % (ablauf.astimezone(TZ).strftime("%d.%m.%Y"), max(rest.days, 0))}
    return {"status": OK, "grund": ""}


def bewerte_platte(pfad: str, prozent: float, grenze: int = 85) -> dict:
    if prozent >= grenze:
        return {"status": FAST_VOLL, "grund": "%s zu %d %% belegt (Grenze %d %%) – df -h; du -xh %s --max-depth=2 | sort -h | tail"
                % (pfad, prozent, grenze, pfad)}
    return {"status": OK, "grund": ""}


def abgleich(ist: dict, bekannt: dict, ignoriert: dict) -> dict:
    """Was läuft, aber weder überwacht noch bewusst ignoriert ist. `ist`/`bekannt`: {art: set}, `ignoriert`: {art: [Muster]}.
    Ergebnis wie die anderen Bewertungen, Schlüssel `neu:<art> <name>` → ein Alarm, Erinnerung nach 24 h, bis eingetragen."""
    out = {}
    for art, namen in ist.items():
        for n in sorted(set(namen) - set(bekannt.get(art, ()))):
            if any(fnmatch.fnmatch(n, m) for m in ignoriert.get(art, [])):
                continue
            out["%s%s %s" % (NEU_PREFIX, art, n)] = {
                "status": NICHT_UEBERWACHT,
                "grund": "%s (oder unter „ignoriert“, wenn bewusst nicht). BKM: PKA/BKM/Neuer-Server-Service.md Punkt 12" % _ABGLEICH_HINWEIS.get(art, "in die Registry eintragen")}
    return out


def alarm_schwelle(job: dict) -> int:
    """Fehlschläge in Folge bis zum Alarm: je Job `alarm_ab_fehlern`; sonst 2 bei Intervallen ≤ 30 Min, 1 bei Festzeiten."""
    if job.get("alarm_ab_fehlern"):
        return max(1, int(job["alarm_ab_fehlern"]))
    return 2 if job.get("every_min") and int(job["every_min"]) <= 30 else 1


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
                meldungen.append("✅ %s läuft wieder." % _label(name))
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


_SYMBOL = {UEBERFAELLIG: "⏰", FEHLGESCHLAGEN: "❌", HAENGT: "🧱", DIENST_AUS: "🛑", SELBSTTEST: "🧪",
           NICHT_ERREICHBAR: "🌐", LAEUFT_AB: "🔒", FAST_VOLL: "💾", NICHT_UEBERWACHT: "🆕"}
_TITEL = {UEBERFAELLIG: "überfällig", FEHLGESCHLAGEN: "fehlgeschlagen", HAENGT: "hängt",
          DIENST_AUS: "läuft nicht", SELBSTTEST: "Selbsttest fehlgeschlagen",
          NICHT_ERREICHBAR: "nicht erreichbar", LAEUFT_AB: "läuft bald ab", FAST_VOLL: "fast voll",
          NICHT_UEBERWACHT: "ist nicht überwacht"}


def kategorie(name: str) -> str | None:
    return next((k for k in KATEGORIEN if name.startswith(k)), None)


def _label(name: str) -> str:
    k = kategorie(name)
    if k:
        return "%s „%s“" % (KATEGORIEN[k][0], name[len(k):])
    return "Cron „%s“" % name


def _alarmtext(name: str, b: dict, erinnerung: bool) -> str:
    kopf = "%s %s %s" % (_SYMBOL.get(b["status"], "⚠️"), _label(name), _TITEL.get(b["status"], b["status"]))
    if erinnerung:
        kopf += " (weiterhin, Erinnerung)"
    return "%s\n%s" % (kopf, b["grund"])


def lebenszeichen(bewertungen: dict, jetzt: datetime) -> str:
    """Tägliche Kurzmeldung – bleibt sie aus, ist der Wächter (oder cron) selbst tot."""
    jobs = {n: b for n, b in bewertungen.items() if kategorie(n) is None}
    beob = [n for n, b in jobs.items() if b["status"] != UNBEOBACHTET]
    ok = [n for n in beob if jobs[n]["status"] == OK]
    schlecht = [n for n in beob if jobs[n]["status"] != OK]
    offen = len(jobs) - len(beob)
    zeile = "🫀 Cron-Wächter %s: %d/%d Jobs ok" % (jetzt.astimezone(TZ).strftime("%d.%m. %H:%M"), len(ok), len(beob))
    for k, (_, mehrzahl) in KATEGORIEN.items():
        if k == NEU_PREFIX:
            continue
        teil = {n: b for n, b in bewertungen.items() if n.startswith(k)}
        if teil:
            zeile += ", %d/%d %s ok" % (sum(b["status"] == OK for b in teil.values()), len(teil), mehrzahl)
            schlecht += [n[len(k):] for n, b in teil.items() if b["status"] != OK]
    if schlecht:
        zeile += " – Problem: %s" % ", ".join(sorted(schlecht))
    if offen:
        zeile += "\n%d weitere noch nicht unter Aufsicht" % offen
    neu = sorted(n[len(NEU_PREFIX):] for n in bewertungen if n.startswith(NEU_PREFIX))
    if neu:
        zeile += "\nNicht überwacht: %s" % ", ".join(neu)
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


def lade_dienste(pfad: Path, abschnitt: str = "dienste") -> dict:
    """Abschnitt `dienste`/`timer`/`urls` der Registry als {name: eintrag}; fehlt er, wird nichts überwacht."""
    roh = json.loads(Path(pfad).read_text(encoding="utf-8"))
    return {d["name"]: d for d in roh.get(abschnitt, []) if d.get("name")}


def lade_ignoriert(pfad: Path) -> dict:
    roh = json.loads(Path(pfad).read_text(encoding="utf-8"))
    return {k: list(v) for k, v in roh.get("ignoriert", {}).items() if not k.startswith("_")}


def lade_grenzwerte(pfad: Path) -> dict:
    roh = json.loads(Path(pfad).read_text(encoding="utf-8"))
    return {"zert_tage": 14, "platte_prozent": 85, "platte_pfade": ["/"], **roh.get("grenzwerte", {})}


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
    hb.setdefault("erster_start", t)
    _atomar_schreiben(pfad, hb)


def heartbeat_ende(name: str, exit_code: int, verzeichnis: Path | None = None,
                   jetzt: datetime | None = None) -> None:
    pfad = (verzeichnis or HEARTBEAT_DIR) / ("%s.json" % name)
    hb = lese_json(pfad) or {"name": name}
    t = (jetzt or jetzt_berlin()).isoformat()
    hb.update({"letztes_ende": t, "letzter_exit": int(exit_code), "laeuft_seit": None})
    if int(exit_code) == 0:
        hb["letzter_erfolg"] = t
        hb["fehler_in_folge"] = 0
    else:
        hb["fehler_in_folge"] = int(hb.get("fehler_in_folge") or 0) + 1
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
