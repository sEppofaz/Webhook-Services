#!/opt/rename-webhook/bin/python3
"""cron_watchdog.py [--dry-run] [--lebenszeichen]

Alle 10 Min (Cron): bewertet jeden Job aus `cron_registry.json` anhand seines
Heartbeats und meldet überfällige, fehlgeschlagene und hängende Jobs per Telegram
(Josefs Bot). Dazu aus derselben Registry: Dienste (`dienste`: läuft er, war sein
Start-Selbsttest ok), systemd-Timer (`timer`: aktiv, letzter Lauf erfolgreich und
nicht zu alt), öffentliche Seiten (`urls`: erwarteter HTTP-Status), TLS-Zertifikate
unter /etc/letsencrypt/live (nur cert.pem) und Plattenplatz (`grenzwerte`). Ein Alarm je Vorfall, Erinnerung nach 24 h, Entwarnung bei Rückkehr.

  --dry-run        nichts senden, Zustand nicht speichern, Ergebnis ausgeben
  --lebenszeichen  zusätzlich die tägliche „alles ok"-Meldung senden (Cron 08:00)

Logik und Tests: `shared/cronwatch.py`, `tests/test_cronwatch.py`. ADR-015.
"""
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
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


def _show(unit: str, *props: str) -> dict:
    r = subprocess.run(["systemctl", "show", unit, *("-p" + p for p in props)],
                       capture_output=True, text=True, timeout=15)
    return dict(z.split("=", 1) for z in r.stdout.splitlines() if "=" in z)


def _systemd_zeit(wert: str):
    """„Sun 2026-10-04 09:00:02 CEST“ → datetime (Server-Zeitzone = Europe/Berlin); „n/a“/leer → None."""
    teile = wert.split()
    if len(teile) < 3:
        return None
    try:
        return datetime.strptime(teile[1] + " " + teile[2], "%Y-%m-%d %H:%M:%S").replace(tzinfo=cw.TZ)
    except ValueError:
        return None


def timer_bewertungen(timer: dict, jetzt) -> dict:
    out = {}
    for name, cfg in timer.items():
        try:
            t = _show(name + ".timer", "ActiveState", "LastTriggerUSec")
            s = _show(name + ".service", "Result", "ActiveState")
            info = {"timer_aktiv": t.get("ActiveState"),
                    "letzter_lauf": _systemd_zeit(t.get("LastTriggerUSec", "")),
                    "result": s.get("Result"), "laeuft": s.get("ActiveState") in ("activating", "active", "deactivating")}
        except Exception as e:
            info = {"timer_aktiv": "Abfrage fehlgeschlagen (%s)" % type(e).__name__}
        out["timer:" + name] = cw.bewerte_timer(name, cfg, info, jetzt)
    return out


class _KeinRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None  # 301/302 selbst bewerten statt folgen


def _http_code(url: str) -> tuple[int | None, str]:
    opener = urllib.request.build_opener(_KeinRedirect)
    try:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "pka-cron-watchdog"}), timeout=15) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return None, type(e).__name__


def url_bewertungen(urls: dict) -> dict:
    out = {}
    for name, cfg in urls.items():
        code, fehler = _http_code(cfg["url"])
        if code not in (cfg.get("erwartet") or [200]):
            time.sleep(10)  # kurzer Aussetzer (Neustart, Netz)? Einmal nachfassen
            code, fehler = _http_code(cfg["url"])
        out["url:" + name] = cw.bewerte_url(cfg, code, fehler)
    return out


def system_bewertungen(grenz: dict, jetzt) -> dict:
    out = {}
    for cert in sorted(Path("/etc/letsencrypt/live").glob("*/cert.pem")):  # nur das öffentliche Zertifikat
        try:
            ablauf = datetime.fromtimestamp(ssl.cert_time_to_seconds(ssl._ssl._test_decode_cert(str(cert))["notAfter"]), cw.TZ)
        except Exception:
            ablauf = None
        out["zert:" + cert.parent.name] = cw.bewerte_zertifikat(cert.parent.name, ablauf, jetzt, int(grenz["zert_tage"]))
    for pfad in grenz["platte_pfade"]:
        u = shutil.disk_usage(pfad)
        out["system:platte " + pfad] = cw.bewerte_platte(pfad, 100 * u.used / u.total, int(grenz["platte_prozent"]))
    return out


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    jetzt = cw.jetzt_berlin()
    jobs = cw.lade_registry(REGISTRY)
    bew = bewertungen(jobs, jetzt)
    bew.update(dienst_bewertungen(cw.lade_dienste(REGISTRY)))
    bew.update(timer_bewertungen(cw.lade_dienste(REGISTRY, "timer"), jetzt))
    bew.update(url_bewertungen(cw.lade_dienste(REGISTRY, "urls")))
    bew.update(system_bewertungen(cw.lade_grenzwerte(REGISTRY), jetzt))
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
