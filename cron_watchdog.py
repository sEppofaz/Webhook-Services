#!/opt/rename-webhook/bin/python3
"""cron_watchdog.py [--dry-run] [--lebenszeichen]

Alle 10 Min (Cron): bewertet jeden Job aus `cron_registry.json` anhand seines
Heartbeats und meldet überfällige, fehlgeschlagene und hängende Jobs per Telegram
(Josefs Bot). Dazu jeden Dienst aus dem Abschnitt `dienste`: läuft er (`systemctl
is-active`), und – wo angegeben – war sein Start-Selbsttest ok. Ein Alarm je Vorfall, Erinnerung nach 24 h, Entwarnung bei Rückkehr.

  --dry-run        nichts senden, Zustand nicht speichern, Ergebnis ausgeben
  --lebenszeichen  zusätzlich die tägliche „alles ok"-Meldung senden (Cron 08:00)

Logik und Tests: `shared/cronwatch.py`, `tests/test_cronwatch.py`. ADR-015.
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shared import cronwatch as cw  # noqa: E402

REGISTRY = Path(__file__).resolve().parent / "cron_registry.json"


def bewertungen(jobs: dict, jetzt) -> dict:
    out = {}
    for name, job in jobs.items():
        if job.get("signal") == "log" and job.get("log_pfad"):
            hb = cw.heartbeat_aus_log(job["log_pfad"])
        else:
            hb = cw.lese_heartbeat(name)
        out[name] = cw.bewerte(job, hb, jetzt)
    return out


def ist_aktiv(name: str) -> str:
    try:
        r = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=15)
        return r.stdout.strip() or "unbekannt"
    except Exception as e:
        return "Abfrage fehlgeschlagen (%s)" % type(e).__name__


def dienst_bewertungen(dienste: dict) -> dict:
    out = {}
    for name, d in dienste.items():
        aktiv = ist_aktiv(name)
        if aktiv not in cw.LAEUFT:
            time.sleep(20)  # Deploy-Neustart erwischt? Einmal nachfassen, bevor Alarm
            aktiv = ist_aktiv(name)
        sel = cw.lese_json(Path(d["selbsttest"])) if d.get("selbsttest") else None
        out[cw.DIENST_PREFIX + name] = cw.bewerte_dienst(name, d, aktiv, sel)
    return out


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    jetzt = cw.jetzt_berlin()
    jobs = cw.lade_registry(REGISTRY)
    bew = bewertungen(jobs, jetzt)
    bew.update(dienst_bewertungen(cw.lade_dienste(REGISTRY)))
    meldungen, neuer_zustand = cw.entscheide(bew, cw.lade_zustand(), jetzt)
    if "--lebenszeichen" in argv:
        meldungen.append(cw.lebenszeichen(bew, jetzt))

    if dry:
        for n, b in sorted(bew.items()):
            print("%-24s %-14s %s" % (n, b["status"], b["grund"]))
        print("\nWürde senden (%d):" % len(meldungen))
        for m in meldungen:
            print("---\n" + m)
        return 0

    if meldungen:
        sys.path.insert(0, "/opt/rename-webhook")
        from shared.secrets import load_secrets
        from shared.telegram import send_telegram
        s = load_secrets()
        send_telegram(s["TOKEN"], s["CHAT_ID"], "\n\n".join(meldungen))
    # Zustand erst nach erfolgreichem Versand speichern: schlägt Telegram fehl, kommt der Alarm beim nächsten Lauf erneut
    cw.speichere_zustand(neuer_zustand)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
