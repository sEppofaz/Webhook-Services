"""Offene Aufgaben im Admin-Bereich – gemeinsam für den Admin-Tab und den 20-Uhr-Bericht.

Nur Standardbibliothek + shared.geo: läuft im Flask-Prozess wie in kalender_report.py (Cron) und
ist offline testbar. Zählt, was auf Josef wartet:
- Importe, die bestätigt werden wollen (Pending-Dateien mit neuen Terminen)
- Vereine, die auf Freigabe warten (vk_accounts.db, status='pending')
- Veranstaltungsorte künftiger Termine ohne sichere Ortschaft (Admin-Tab „Orte“)
- Register-Einträge (orte.json) mit `quelle`, aber ohne Bestätigung (Bereich „Register prüfen“)
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from pathlib import Path

from shared.geo import geo_fuer_termin, _gem_norm, _ort_norm, _lade


def offene_orte(raw: dict) -> tuple[list, int]:
    """Veranstaltungsorte künftiger Termine, deren Ortschaft nicht aus dem Ortstext oder einer
    Admin-Zuordnung stammt – gruppiert nach (Ort, Gemeinde). Zweiter Wert: Termine ganz ohne Ort."""
    labels = raw.get("_labels", {})
    meta   = raw.get("_meta", {})
    zu     = raw.get("_orte_zuordnung", [])
    heute  = date.today().isoformat()
    gruppen: dict = {}
    ohne_ortsangabe = 0
    for key, items in raw.items():
        if key.startswith("_") or not isinstance(items, list):
            continue
        m = meta.get(key, {})
        for t in items:
            if not isinstance(t, dict) or t.get("geloescht") or t.get("deleted") or t.get("datum", "") < heute:
                continue
            g = geo_fuer_termin({**t, "verein": key}, m, labels.get(key, ""), zu)
            if g is None:
                continue
            ort = re.sub(r"\s+", " ", str(t.get("ort") or "")).strip()
            if not ort:
                if not g["orte"]:
                    ohne_ortsangabe += 1
                continue
            if g.get("quelle") in ("ort", "zuordnung", "ausflug"):
                continue
            gk = (_ort_norm(ort), _gem_norm(m.get("gemeinde", "")).casefold())
            grp = gruppen.setdefault(gk, {
                "ort": ort, "gemeinde": _gem_norm(m.get("gemeinde", "")), "landkreis": m.get("landkreis", ""),
                "termine": 0, "vereine": {}, "aktuell": g["orte"], "quelle": g.get("quelle", "")})
            grp["termine"] += 1
            grp["vereine"][key] = labels.get(key, key)
    liste = sorted(gruppen.values(), key=lambda x: (bool(x["aktuell"]), x["gemeinde"], -x["termine"], x["ort"].lower()))
    for x in liste:
        x["vereine"] = [{"key": k, "label": v} for k, v in sorted(x["vereine"].items(), key=lambda kv: kv[1].lower())]
    return liste, ohne_ortsangabe


def _reg_schluessel(ort: str, gemeinde: str) -> tuple[str, str]:
    return (str(ort or "").strip().casefold(), _gem_norm(gemeinde).casefold())


def register_pruefung(raw: dict) -> dict:
    """Register-Einträge zum Prüfen (Todo #417).

    `orte.json` liegt im Git – der Server schreibt dort nie hinein (sonst scheitert der nächste
    Pull). Josefs Urteil steht deshalb in vereinstermine.json unter `_orte_geprueft`
    ({ort, gemeinde, ok, notiz, am}); „falsch“ ist eine Aufgabe für die Pflege von orte.json.
    """
    register, _ = _lade()
    urteile = {_reg_schluessel(u.get("ort"), u.get("gemeinde")): u
               for u in raw.get("_orte_geprueft", []) if isinstance(u, dict)}
    offen, falsch, bestaetigt = [], [], 0
    for e in register or []:
        if e.get("geprueft") or not e.get("quelle"):
            continue
        eintrag = {"ort": e.get("ort", ""), "gemeinde": e.get("gemeinde", ""), "plz": e.get("plz", ""),
                   "landkreis": e.get("landkreis", ""), "quelle": e.get("quelle", "")}
        u = urteile.get(_reg_schluessel(e.get("ort"), e.get("gemeinde")))
        if not u:
            offen.append(eintrag)
        elif u.get("ok"):
            bestaetigt += 1
        else:
            falsch.append({**eintrag, "notiz": u.get("notiz", ""), "am": u.get("am", "")})
    sortiert = lambda l: sorted(l, key=lambda x: (x["gemeinde"].casefold(), x["ort"].casefold()))
    return {"offen": sortiert(offen), "falsch": sortiert(falsch), "bestaetigt": bestaetigt}


def offene_aufgaben(raw: dict, pending_dir: Path, db_path: Path) -> dict:
    """Zähler für Telegram-Bericht und Admin. Fehler einer Quelle zählen dort als 0, nie als Absturz."""
    importe = 0
    try:
        for f in Path(pending_dir).glob("heimat_pending_*.json"):
            try:
                if any(e.get("_neu", True) for e in json.loads(f.read_text()).get("events", [])):
                    importe += 1
            except Exception:
                pass
    except Exception:
        pass
    vereine = 0
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:   # nur lesen, nie eine leere DB anlegen
            vereine = conn.execute("SELECT COUNT(*) FROM vereine_accounts WHERE status='pending'").fetchone()[0]
    except Exception:
        pass
    try:
        orte = len(offene_orte(raw)[0])
    except Exception:
        orte = 0
    try:
        reg = register_pruefung(raw)
        register, register_falsch = len(reg["offen"]), len(reg["falsch"])
    except Exception:
        register = register_falsch = 0
    return {"importe": importe, "vereine": vereine, "orte": orte,
            "register": register, "register_falsch": register_falsch}


def aufgaben_text(a: dict) -> str:
    """Eine Zeile für Telegram, leer wenn nichts für Josef offen ist (register_falsch ist Claudes Aufgabe)."""
    teile = []
    if a.get("importe"):
        teile.append(f"{a['importe']} Import{'e' if a['importe'] != 1 else ''} bestätigen")
    if a.get("vereine"):
        teile.append(f"{a['vereine']} Verein{'e' if a['vereine'] != 1 else ''} freigeben")
    if a.get("orte"):
        teile.append(f"{a['orte']} Ort{'e' if a['orte'] != 1 else ''} zuordnen")
    if a.get("register"):
        teile.append(f"{a['register']} Register-Eintr{'äge' if a['register'] != 1 else 'ag'} prüfen")
    return " · ".join(teile)
