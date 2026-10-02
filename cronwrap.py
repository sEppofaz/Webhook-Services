#!/opt/rename-webhook/bin/python3
"""cronwrap.py NAME -- <Befehl …>

Startet den Befehl wie gewohnt (stdout/stderr unverändert, Exit-Code wird
durchgereicht) und schreibt davor und danach einen Heartbeat nach
`/var/lib/pka-cron/NAME.json` (`PKA_CRON_DIR` überschreibt das Verzeichnis).
`cron_watchdog.py` wertet ihn aus. Siehe ADR-015.

**Darf einen Job nie kaputt machen:** Jeder Fehler beim Schreiben des Heartbeats
wird verschluckt (stderr-Hinweis), der Befehl läuft trotzdem.

Crontab-Beispiel:
    30 6 * * * /opt/rename-webhook/bin/python3 /opt/rename-webhook/cronwrap.py logbuch_summary -- \
        /opt/rename-webhook/bin/python3 /opt/rename-webhook/logbuch_summary.py >> /var/log/pka-logbuch.log 2>&1
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shared import cronwatch  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[2] != "--":
        print("Aufruf: cronwrap.py NAME -- BEFEHL [ARGUMENTE…]", file=sys.stderr)
        return 2
    name, befehl = argv[1], argv[3:]
    try:
        cronwatch.heartbeat_start(name)
    except Exception as ex:
        print("cronwrap: Heartbeat-Start nicht geschrieben (%s) – Job läuft trotzdem" % ex, file=sys.stderr)
    try:
        code = subprocess.call(befehl)
    except OSError as ex:
        print("cronwrap: Befehl nicht startbar: %s" % ex, file=sys.stderr)
        code = 127
    try:
        cronwatch.heartbeat_ende(name, code)
    except Exception as ex:
        print("cronwrap: Heartbeat-Ende nicht geschrieben (%s)" % ex, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
