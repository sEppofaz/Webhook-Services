#!/opt/rename-webhook/bin/python3
"""cron_watchdog.py [--dry-run] [--lebenszeichen]

Alle 10 Min (Cron): bewertet jeden Job aus `cron_registry.json` anhand seines
Heartbeats und meldet überfällige, fehlgeschlagene und hängende Jobs per Telegram
(Josefs Bot). Ein Alarm je Vorfall, Erinnerung nach 24 h, Entwarnung bei Rückkehr.

  --dry-run        nichts senden, Zustand nicht speichern, Ergebnis ausgeben
  --lebenszeichen  zusätzlich die tägliche „alles ok"-Meldung senden (Cron 08:00)

Logik und Tests: `shared/cronwatch.py`, `tests/test_cronwatch.py`. ADR-015.
"""
import sys
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


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    jetzt = cw.jetzt_berlin()
    jobs = cw.lade_registry(REGISTRY)
    bew = bewertungen(jobs, jetzt)
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
