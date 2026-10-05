"""Prototyp Jahresplanung – lokal, nicht integriert (Branch `jahresplanung`, Josef 2026-10-05).

Start:  ~/.venvs/vko-jahresplanung/bin/python prototyp/jahresplanung/app.py
Dann:   http://localhost:5050

Drei Bereiche:
1. Kollisionswarnung – wie sie später im Vereinsformular erscheint (Stufe A).
2. Vorjahres-Vorlage – Termine eines Vereins nach ihrem Schema ins Zieljahr übertragen (Stufe B).
3. Planungstreffen – Planungsraum je Gemeinde, Links für Vereine ohne Konto, Konflikte,
   Export (PDF, Word, Excel, LibreOffice, Kalenderdatei) (Stufe C).
Schreibt nie in den Live-Kalender. Daten: Momentaufnahme der öffentlichen /api/termine.
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import date

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for

import daten as D
import db
from shared.csrf import csrf_field, get_csrf_token, validate_csrf
from shared.export import FORMATE, datum_text, dateiname, exportiere, zeit_text
from shared.kollision import STUFE_TAG, kollisionen
from shared.termin_felder import DATUM_RE, zeit_fehler
from shared.wiederholung import MONATE

app = Flask(__name__)
app.secret_key = os.urandom(32)   # Prototyp: Sitzungen gelten bis zum Neustart

TEXT_MAX = 200


@app.context_processor
def _vorlagen_hilfen():
    return {"csrf": lambda: csrf_field(get_csrf_token()), "FORMATE": FORMATE, "STATUS": db.STATUS,
            "datum_text": datum_text, "zeit_text": zeit_text, "STUFE_TAG": STUFE_TAG}


@app.before_request
def _csrf_pruefen():
    if request.method == "POST" and not validate_csrf():
        abort(403, "Ungültige Anfrage – Seite neu laden.")


# ── Hilfen ───────────────────────────────────────────────────────────────────

def _datum_ok(s: str) -> bool:
    """Format **und** Kalender: `2027-13-01` passt auf DATUM_RE, ist aber kein Datum."""
    if not DATUM_RE.match(s or ""):
        return False
    try:
        date.fromisoformat(s)
    except ValueError:
        return False
    return True


def _felder_aus_formular() -> tuple[dict, str]:
    f = {k: request.form.get(k, "").strip() for k in ("datum", "uhrzeit", "uhrzeit_bis", "bezeichnung", "ort")}
    if not _datum_ok(f["datum"]):
        return f, "Bitte ein gültiges Datum angeben."
    if not f["bezeichnung"]:
        return f, "Bitte eine Bezeichnung angeben."
    if len(f["bezeichnung"]) > TEXT_MAX or len(f["ort"]) > TEXT_MAX:
        return f, f"Bezeichnung und Ort höchstens {TEXT_MAX} Zeichen."
    return f, zeit_fehler(f["uhrzeit"], f["uhrzeit_bis"])


def _kalender_im_jahr(jahr: int) -> list[dict]:
    """Bereits veröffentlichte Termine im Zieljahr (aus dem Live-Kalender)."""
    return [t for t in D.daten()["termine"] if t.get("datum", "")[:4] == str(jahr)]


def _mit_konflikten(eigene: list[dict], andere: list[dict]) -> list[dict]:
    """Jedem Termin seine Kollisionen anhängen (`_konflikte`), Quelle Planung/Kalender markiert."""
    d = D.daten()
    for t in eigene:
        if t.get("status") == "verworfen":
            t["_konflikte"] = []
            continue
        k = kollisionen(andere, d["meta"], d["labels"], t, d["rubriken"], wochenende=True,
                        ausser_ids={t.get("id")})
        for x in k:
            x["quelle"] = "Planung" if str(x.get("id", "")).startswith("p") else "Kalender"
        t["_konflikte"] = k
        t["_konflikt_tag"] = any(x["stufe"] == STUFE_TAG for x in k)
    return eigene


def _nach_monat(termine: list[dict]) -> list[tuple[str, list[dict]]]:
    gruppen: dict = defaultdict(list)
    for t in termine:
        gruppen[t["datum"][:7]].append(t)
    return [(f"{MONATE[int(m[5:7]) - 1]} {m[:4]}", gruppen[m]) for m in sorted(gruppen)]


def _export(titel: str, zeilen: list[dict], fmt: str, mit_verein: bool) -> Response:
    if fmt not in FORMATE:
        abort(404)
    labels = D.daten()["labels"]
    zeilen = [{**z, "verein_name": z.get("verein_name") or labels.get(z.get("verein", ""), "")} for z in zeilen]
    inhalt, mime, _ = exportiere(fmt, titel, zeilen, mit_verein)
    return Response(inhalt, mimetype=mime,
                    headers={"Content-Disposition": f'attachment; filename="{dateiname(titel, fmt)}"'})


def _zieljahr() -> int:
    try:
        return int(request.args.get("jahr", "")) if request.args.get("jahr") else date.today().year + 1
    except ValueError:
        return date.today().year + 1


# ── Start ────────────────────────────────────────────────────────────────────

@app.get("/")
def start():
    return render_template("start.html", stand=D.stand())


@app.post("/daten-holen")
def daten_holen():
    D.hole_daten()
    return redirect(url_for("start"))


# ── 1. Kollisionswarnung ─────────────────────────────────────────────────────

@app.get("/kollision")
def kollision_seite():
    return render_template("kollision.html", vereine=D.vereine(), heute=date.today().isoformat())


@app.get("/api/kollisionen")
def api_kollisionen():
    """Wie später `GET /verein/kollisionen` – der Verein kommt dort aus der Sitzung, hier aus der Auswahl."""
    von, bis = request.args.get("von", ""), request.args.get("bis", "") or request.args.get("von", "")
    if not _datum_ok(von) or not _datum_ok(bis) or bis < von:
        return jsonify([])
    tage = []
    d0, d1 = date.fromisoformat(von), date.fromisoformat(bis)
    while d0 <= d1 and len(tage) < 16:
        tage.append(d0.isoformat())
        d0 = date.fromordinal(d0.toordinal() + 1)
    d = D.daten()
    entwurf = {"verein": request.args.get("verein", ""), "tage": tage,
               "ort": request.args.get("ort", "")[:TEXT_MAX], "uhrzeit": request.args.get("uhrzeit", "")[:5]}
    k = kollisionen(d["termine"], d["meta"], d["labels"], entwurf, d["rubriken"],
                    wochenende=request.args.get("wochenende") == "1")
    return jsonify([{**x, "datum_text": datum_text(x)} for x in k])


# ── 2. Vorjahres-Vorlage ─────────────────────────────────────────────────────

@app.get("/vorlage")
def vorlage_seite():
    jahr = _zieljahr()
    verein = request.args.get("verein", "")
    eigene = []
    if verein:
        alle = D.vorschlaege(jahr)
        eigene = [dict(v, id=f"v{i}") for i, v in enumerate(alle) if v.get("verein") == verein]
        andere = [dict(v, id=f"p{i}") for i, v in enumerate(alle) if v.get("verein") != verein] \
            + _kalender_im_jahr(jahr)
        _mit_konflikten(eigene, andere)
    return render_template("vorlage.html", vereine=D.vereine(), verein=verein, jahr=jahr,
                           monate=_nach_monat(eigene), anzahl=len(eigene),
                           name=D.daten()["labels"].get(verein, ""), daten_ab=D.daten_ab())


@app.get("/vorlage/export.<fmt>")
def vorlage_export(fmt):
    jahr, verein = _zieljahr(), request.args.get("verein", "")
    zeilen = [v for v in D.vorschlaege(jahr) if v.get("verein") == verein]
    name = D.daten()["labels"].get(verein, verein)
    return _export(f"Terminvorschlag {jahr} – {name}", zeilen, fmt, mit_verein=False)


# ── 3. Planungstreffen ───────────────────────────────────────────────────────

@app.get("/planung")
def planung_liste():
    return render_template("planung.html", raeume=db.raeume(), gemeinden=D.gemeinden(),
                           jahr=date.today().year + 1)


@app.post("/planung/neu")
def planung_neu():
    gemeinde, _, landkreis = request.form.get("gemeinde", "").partition("|")
    try:
        jahr = int(request.form.get("jahr", ""))
    except ValueError:
        abort(400)
    vereine = D.vereine_der_gemeinde(gemeinde, landkreis)
    if not vereine:
        abort(400, "Keine Vereine in dieser Gemeinde.")
    token = db.neuer_raum(gemeinde, landkreis, jahr, vereine, D.vorschlaege(jahr))
    return redirect(url_for("raum_orga", token=token))


@app.post("/planung/<int:raum_id>/loeschen")
def planung_loeschen(raum_id):
    db.raum_loeschen(raum_id)
    return redirect(url_for("planung_liste"))


def _raum_oder_404(token):
    r = db.raum_per_token(token)
    if not r:
        abort(404)
    return r


def _raum_konflikte(r, eigene_key: str | None = None) -> tuple[list[dict], list[dict]]:
    """(Termine mit Konflikten, Liste der Konflikte am gleichen Tag ohne Doppel)."""
    alle = db.termine_im_raum(r["id"], ohne_verworfen=False)
    aktive = [t for t in alle if t["status"] != "verworfen"]
    andere = aktive + _kalender_im_jahr(r["jahr"])
    eigene = [t for t in alle if not eigene_key or t["verein"] == eigene_key]
    _mit_konflikten(eigene, andere)
    paare, gesehen = [], set()
    for t in eigene:
        for k in t.get("_konflikte", []):
            if k["stufe"] != STUFE_TAG:
                continue
            schluessel = tuple(sorted((t["id"], str(k["id"]))))
            if schluessel in gesehen:
                continue
            gesehen.add(schluessel)
            paare.append({"a": t, "b": k})
    paare.sort(key=lambda p: p["a"]["datum"])
    return eigene, paare


@app.get("/p/<token>")
def raum_orga(token):
    r = _raum_oder_404(token)
    termine, paare = _raum_konflikte(r)
    labels = D.daten()["labels"]
    vereine = db.vereine_im_raum(r["id"])
    farben = {v["verein_key"]: i % 8 for i, v in enumerate(vereine)}
    return render_template("raum_orga.html", r=r, vereine=vereine, paare=paare, labels=labels, farben=farben,
                           monate=_nach_monat([t for t in termine if t["status"] != "verworfen"]),
                           treffen=request.args.get("ansicht") == "treffen",
                           basis=request.host_url.rstrip("/"))


@app.post("/p/<token>/termin/<int:tid>")
def raum_orga_termin(token, tid):
    r = _raum_oder_404(token)
    felder = {k: request.form[k].strip() for k in ("datum", "status") if k in request.form}
    if "datum" in felder and not _datum_ok(felder["datum"]):
        abort(400)
    if "status" in felder and felder["status"] not in db.STATUS:
        abort(400)
    db.termin_aendern(r["id"], tid, None, felder)
    return redirect(url_for("raum_orga", token=token, ansicht=request.args.get("ansicht")) + f"#t{tid}")


@app.post("/p/<token>/status")
def raum_orga_status(token):
    r = _raum_oder_404(token)
    db.raum_status(r["id"], "abgeschlossen" if request.form.get("aktion") == "abschliessen" else "offen")
    return redirect(url_for("raum_orga", token=token))


@app.get("/p/<token>/export.<fmt>")
def raum_orga_export(token, fmt):
    r = _raum_oder_404(token)
    return _export(r["titel"], db.termine_im_raum(r["id"], ohne_verworfen=True), fmt, mit_verein=True)


def _verein_oder_404(token):
    v = db.verein_per_token(token)
    if not v:
        abort(404)
    return v, db.raum(v["raum_id"])


@app.get("/v/<token>")
def raum_verein(token):
    v, r = _verein_oder_404(token)
    termine, _ = _raum_konflikte(r, eigene_key=v["verein_key"])
    return render_template("raum_verein.html", v=v, r=r, monate=_nach_monat(termine),
                           offen=sum(1 for t in termine if t["status"] == "vorschlag"),
                           fehler=request.args.get("fehler", ""), daten_ab=D.daten_ab())


@app.post("/v/<token>/termin/<int:tid>")
def raum_verein_termin(token, tid):
    v, r = _verein_oder_404(token)
    if r["status"] != "offen":
        abort(403, "Die Planung ist abgeschlossen.")
    if request.form.get("aktion") in db.STATUS:
        db.termin_aendern(r["id"], tid, v["verein_key"], {"status": request.form["aktion"]})
    else:
        felder, fehler = _felder_aus_formular()
        if fehler:
            return redirect(url_for("raum_verein", token=token, fehler=fehler) + f"#t{tid}")
        felder["status"] = "bestaetigt"
        db.termin_aendern(r["id"], tid, v["verein_key"], felder)
    return redirect(url_for("raum_verein", token=token) + f"#t{tid}")


@app.post("/v/<token>/neu")
def raum_verein_neu(token):
    v, r = _verein_oder_404(token)
    if r["status"] != "offen":
        abort(403, "Die Planung ist abgeschlossen.")
    felder, fehler = _felder_aus_formular()
    if fehler:
        return redirect(url_for("raum_verein", token=token, fehler=fehler) + "#neu")
    db.termin_neu(r["id"], v["verein_key"], felder)
    return redirect(url_for("raum_verein", token=token))


@app.post("/v/<token>/alle-bestaetigen")
def raum_verein_alle(token):
    v, r = _verein_oder_404(token)
    if r["status"] == "offen":
        db.alle_vorschlaege_bestaetigen(r["id"], v["verein_key"])
    return redirect(url_for("raum_verein", token=token))


@app.post("/v/<token>/fertig")
def raum_verein_fertig(token):
    v, _ = _verein_oder_404(token)
    db.fertig_melden(v["id"], request.form.get("fertig") == "1")
    return redirect(url_for("raum_verein", token=token))


@app.get("/v/<token>/export.<fmt>")
def raum_verein_export(token, fmt):
    v, r = _verein_oder_404(token)
    zeilen = db.termine_im_raum(r["id"], v["verein_key"], ohne_verworfen=True)
    return _export(f"Termine {r['jahr']} – {v['verein_name']}", zeilen, fmt, mit_verein=False)


@app.template_filter("wochentag_kurz")
def _wt(datum: str) -> str:
    try:
        return ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")[date.fromisoformat(datum).weekday()]
    except ValueError:
        return ""


@app.template_filter("tm")
def _tm(datum: str) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", datum or "")
    return f"{m[3]}.{m[2]}." if m else datum


if __name__ == "__main__":
    db.init()
    if not D.TERMINE_FILE.exists():
        print("Keine Momentaufnahme – hole /api/termine …")
        print(f"{D.hole_daten()} Termine geladen.")
    app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=True)
