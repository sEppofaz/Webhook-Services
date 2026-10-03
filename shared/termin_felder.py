"""Gemeinsame Prüfregeln für Termin-Felder (Vereinsformular + Admin-PATCH)."""
import re

BESCHREIBUNG_MAX = 1000
UHRZEIT_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DATUM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
