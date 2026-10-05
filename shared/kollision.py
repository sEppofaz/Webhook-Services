"""Termin-Kollisionen: Was ist am selben Tag (oder Wochenende) in derselben Gemeinde schon geplant?

Prototyp auf Branch `jahresplanung` (Josef 2026-10-05, Idee 1) – noch nicht in der Live-App.
Ein gemeinsamer Kern für drei Stellen: die einfache Warnung im Vereinsformular, die
Vorjahres-Vorlage und das Planungstreffen der Gemeinde.

**Gemeinde eines Termins** nach der Mischregel des Filters (ADR-025, `termin_orte_misch()`):
dort, wo er stattfindet, und – außer bei Pfarreien – am Sitz des Vereins. Verglichen wird
Gemeinde ohne Präfix + Landkreis (ADR-005), ohne Groß/Klein.

**Nicht als Kollision:** Gottesdienste aus dem Pfarrbrief (`quelle == "Pfarrbrief"`, auch über
`_meta[key].quelle`) – sonst warnte die Prüfung jeden Sonntag (Josef 2026-10-05). Und Termine des
eigenen Vereins.

Erwartet Termine in der Form von `/api/termine` (mit `verein`, optional `_geo`). Fehlt `_geo`,
wird es per `geo_fuer_termin()` berechnet. Nur Standardbibliothek + `shared.geo`.
"""
from __future__ import annotations

from datetime import date, timedelta

from shared.geo import geo_fuer_termin, termin_orte_misch

STUFE_TAG = "gleicher Tag"
STUFE_WOCHENENDE = "gleiches Wochenende"


def ist_regelgottesdienst(t: dict, meta_eintrag: dict | None = None) -> bool:
    return (t.get("quelle") or (meta_eintrag or {}).get("quelle") or "") == "Pfarrbrief"


def gemeinden_von(t: dict, meta: dict, labels: dict, rubriken: dict | None = None,
                  zuordnung: list | None = None) -> set[tuple[str, str]]:
    """{(gemeinde, landkreis)} eines Termins, klein geschrieben. Leere Gemeinden fallen weg."""
    vkey = t.get("verein", "")
    m = meta.get(vkey) or {}
    label = labels.get(vkey, "")
    geo = t.get("_geo")
    if geo is None:
        try:
            geo = geo_fuer_termin(t, m, label, zuordnung)
        except Exception:
            geo = None
    ist_pfarrei = (rubriken or {}).get(vkey) == "Pfarrei"
    return {(g.lower(), lk.lower()) for _, g, lk in termin_orte_misch(geo, m, label, ist_pfarrei) if g}


def _wochenende(d: date) -> date | None:
    """Samstag des Wochenendes (Fr–So), sonst None."""
    if d.weekday() == 4:
        return d + timedelta(days=1)
    if d.weekday() == 5:
        return d
    if d.weekday() == 6:
        return d - timedelta(days=1)
    return None


def kollisionen(termine: list[dict], meta: dict, labels: dict, entwurf: dict,
                rubriken: dict | None = None, zuordnung: list | None = None,
                wochenende: bool = False, ausser_ids: set | None = None,
                zusatz_vereine: set | None = None, ohne_vereine: set | None = None) -> list[dict]:
    """Kollisionen für einen Entwurf.

    entwurf: `verein` (Pflicht), `ort`, `uhrzeit` und entweder `datum` oder `tage` (Liste ISO-Daten,
    mehrtägig). wochenende=True meldet zusätzlich Termine am selben Wochenende (Fr–So) als
    schwächere Stufe – fürs Planungstreffen. ausser_ids: Termin-IDs, die nicht zählen (der
    Termin selbst beim Bearbeiten). zusatz_vereine: Vereine, die unabhängig von der Gemeinde
    zählen – aktiv gewählte Nachbarn jenseits der Gemeindegrenze (Josef 2026-10-05). ohne_vereine: Vereine, die nie
    zählen – vom Verein in seinen Einstellungen ausgeschlossen (schlägt zusatz_vereine).
    Rückgabe sortiert: {id, datum, uhrzeit, bezeichnung, verein, verein_name, ort, stufe, entwurf_datum,
    nachbar (True = nur über zusatz_vereine gefunden)}.
    """
    eigener = entwurf.get("verein", "")
    tage = entwurf.get("tage") or ([entwurf["datum"]] if entwurf.get("datum") else [])
    try:
        tage_d = sorted({date.fromisoformat(x) for x in tage})
    except ValueError:
        return []
    if not tage_d:
        return []
    meine = gemeinden_von({**entwurf, "datum": tage[0]}, meta, labels, rubriken, zuordnung)
    zusatz = set(zusatz_vereine or ())
    if not meine and not zusatz:
        return []

    nach_tag = {d.isoformat(): d.isoformat() for d in tage_d}
    nach_we = {}
    if wochenende:
        for d in tage_d:
            sa = _wochenende(d)
            if sa:
                for x in (sa - timedelta(days=1), sa, sa + timedelta(days=1)):
                    nach_we.setdefault(x.isoformat(), d.isoformat())

    treffer = []
    for t in termine:
        tag = t.get("datum", "")
        if tag not in nach_tag and tag not in nach_we:
            continue
        vkey = t.get("verein", "")
        if vkey == eigener or (ausser_ids and t.get("id") in ausser_ids) or (ohne_vereine and vkey in ohne_vereine):
            continue
        if t.get("geloescht") or t.get("deleted") or t.get("intern"):
            continue
        if ist_regelgottesdienst(t, meta.get(vkey)):
            continue
        in_gemeinde = bool(meine and gemeinden_von(t, meta, labels, rubriken, zuordnung) & meine)
        if not in_gemeinde and vkey not in zusatz:
            continue
        nachbar = not in_gemeinde
        treffer.append({
            "id": t.get("id", ""),
            "datum": tag, "uhrzeit": t.get("uhrzeit", ""), "bezeichnung": t.get("bezeichnung", ""),
            "verein": vkey, "verein_name": labels.get(vkey, vkey), "ort": t.get("ort", ""),
            "stufe": STUFE_TAG if tag in nach_tag else STUFE_WOCHENENDE,
            "entwurf_datum": nach_tag.get(tag) or nach_we[tag],
            "nachbar": nachbar,
        })
    treffer.sort(key=lambda x: (x["stufe"] != STUFE_TAG, x["datum"], x["uhrzeit"]))
    return treffer
