#!/opt/rename-webhook/bin/python3
"""mail_lebenszeichen.py – wöchentliche Testmail über Brevo (Josef 2026-10-07)

Cron: montags 08:00 über `cronwrap.py mail_lebenszeichen` (/etc/cron.d/pka-mail-lebenszeichen).

Anlass: Brevo markiert SMTP-Schlüssel, die drei Monate nicht benutzt wurden, als inaktiv. Bei nur
wenigen Registrierungen im Jahr passierte das unbemerkt – Bestätigungs- und Willkommens-Mail für
FF Hölskofen gingen am 2026-10-07 verloren. Die Testmail hält den Schlüssel in Gebrauch, und ein
Fehlschlag fällt nach spätestens einer Woche auf: `_send()` meldet ihn per Telegram, der Exit-Code 1
lässt zusätzlich den Cron-Wächter anschlagen.

    mail_lebenszeichen.py             # Testmail an Vereinskalender@icloud.com
    mail_lebenszeichen.py --dry-run   # nichts senden, nur prüfen, ob der SMTP-Zugang gesetzt ist
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

EMPFAENGER = "Vereinskalender@icloud.com"
SCHLUESSEL = ("BREVO_SMTP_USER", "BREVO_SMTP_KEY", "TOKEN", "CHAT_ID")


def main() -> int:
    a = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    a.add_argument("--dry-run", action="store_true")
    a = a.parse_args()
    from shared.secrets import load_secrets
    cfg = load_secrets()
    for k in SCHLUESSEL:                      # nur in die eigene Prozess-Umgebung, nie ausgeben
        if cfg.get(k) and not os.environ.get(k):
            os.environ[k] = cfg[k]
    fehlen = [k for k in SCHLUESSEL if not os.environ.get(k)]
    if a.dry_run:
        print(f"Trockenlauf: Empfänger {EMPFAENGER}, fehlende Einträge: {', '.join(fehlen) or 'keine'}")
        return 1 if fehlen else 0
    from shared.vk_mail import _html_wrap, _send
    jetzt = datetime.now().strftime("%d.%m.%Y %H:%M")
    body = (f"<h2>Lebenszeichen</h2><p>Der Mailversand des Vereinskalenders funktioniert ({jetzt}).</p>"
            "<p class=\"hint\">Wöchentliche Testmail, damit der Brevo-SMTP-Schlüssel aktiv bleibt. "
            "Bleibt sie aus, kommt eine Telegram-Warnung.</p>")
    ok = _send(EMPFAENGER, f"Lebenszeichen Mailversand – {jetzt}", _html_wrap("Lebenszeichen", body))
    print(f"{'✅' if ok else '❌'} Lebenszeichen an {EMPFAENGER}: {'verschickt' if ok else 'FEHLGESCHLAGEN'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
