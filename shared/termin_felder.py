"""Gemeinsame Prüfregeln für Termin-Felder (Vereinsformular + Admin-PATCH)."""
import re
from datetime import date

BESCHREIBUNG_MAX = 1000
UHRZEIT_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DATUM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def datum_ok(s: str) -> bool:
    """Format **und** Kalender: „2027-13-01“ oder „2027-02-30“ passen auf DATUM_RE, sind aber kein Datum (#435)."""
    if not DATUM_RE.match(s or ""):
        return False
    try:
        date.fromisoformat(s)
    except ValueError:
        return False
    return True


def zeit_fehler(uhrzeit: str, uhrzeit_bis: str) -> str:
    """Prüft Beginn/Ende. Ende früher als Beginn = endet nach Mitternacht (erlaubt)."""
    if uhrzeit and not UHRZEIT_RE.match(uhrzeit):
        return "Uhrzeit muss im Format HH:MM sein."
    if uhrzeit_bis and not UHRZEIT_RE.match(uhrzeit_bis):
        return "Bis-Uhrzeit muss im Format HH:MM sein."
    if uhrzeit_bis and not uhrzeit:
        return "Bitte zur Bis-Uhrzeit auch eine Beginn-Uhrzeit angeben."
    if uhrzeit_bis and uhrzeit_bis == uhrzeit:
        return "Bis-Uhrzeit muss sich vom Beginn unterscheiden."
    return ""
