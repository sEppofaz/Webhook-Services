"""Prototyp Jahresplanung – lokal, nicht integriert (Branch `jahresplanung`, Josef 2026-10-05).

Start:  ~/.venvs/vko-jahresplanung/bin/python prototyp/jahresplanung/app.py
Dann:   http://localhost:5050

Bereiche:
1. Kollisionswarnung – wie sie später im Vereinsformular erscheint (nur veröffentlichte Termine).
2. Vorjahres-Vorlage – Termine eines Vereins nach ihrem Schema ins Zieljahr übertragen.
3. Entwürfe (Vereinsadmin, simuliertes Login) – anlegen, aus dem Vorjahr erzeugen, Verschiebe-
   Vorschläge übernehmen, einzeln oder alle veröffentlichen. Nur der Verein selbst veröffentlicht.
4. Planungstreffen – Sicht der Organisatorin auf die Entwürfe der beteiligten Vereine: Konflikte,
   Verschiebe-Vorschläge, Einladungen (führen zur Registrierung; freigeben tut Josef), Export.
Schreibt nie in den Live-Kalender. Daten: Momentaufnahme der öffentlichen /api/termine.
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import date, datetime
from functools import wraps

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, session, url_for

import daten as D
import db
from shared.csrf import csrf_field, get_csrf_token, validate_csrf
from shared.export import FORMATE, datum_text, dateiname, exportiere, zeit_text
from shared.kollision import STUFE_TAG, kollisionen
from shared.termin_felder import DATUM_RE, zeit_fehler
from shared.wiederholung import MONATE

app = Flask(__name__)
app.secret_key = os.urandom(32)   # Prototyp: Sitzungen gelten bis zum Neustart
app.config["TEMPLATES_AUTO_RELOAD"] = True   # Vorlagen-Änderungen ohne Neustart sichtbar (zum Basteln)

TEXT_MAX = 200


@app.context_processor
def _vorlagen_hilfen():
    return {"csrf": lambda: csrf_field(get_csrf_token()), "FORMATE": FORMATE, "STATUS": db.STATUS,
            "FREIGABE": db.FREIGABE, "datum_text": datum_text, "zeit_text": zeit_text, "STUFE_TAG": STUFE_TAG,
            "angemeldet": session.get("verein"), "angemeldet_name": session.get("verein_name")}


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


def _veroeffentlichte() -> list[dict]:
    """Was im Kalender steht: Momentaufnahme + im Prototyp veröffentlichte Entwürfe."""
    return D.daten()["termine"] + db.entwuerfe(status="veroeffentlicht")


def _kalender_im_jahr(jahr: int) -> list[dict]:
    return [t for t in _veroeffentlichte() if t.get("datum", "")[:4] == str(jahr)]


def _mit_konflikten(eigene: list[dict], andere: list[dict], zusatz: set | None = None) -> list[dict]:
    """Jedem Termin seine Kollisionen anhängen (`_konflikte`); Quelle Entwurf/Kalender markiert."""
    d = D.daten()
    entwurf_ids = {x["id"] for x in andere if str(x.get("id", "")).startswith("e") and x.get("status") == "entwurf"}
    for t in eigene:
        k = kollisionen(andere, d["meta"], d["labels"], t, d["rubriken"], wochenende=True,
                        ausser_ids={t.get("id")}, zusatz_vereine=zusatz)
        for x in k:
            x["quelle"] = "Entwurf" if x.get("id") in entwurf_ids else "Kalender"
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


def _zieljahr(feld: str = "jahr") -> int:
    try:
        return int(request.values.get(feld, "")) if request.values.get(feld) else date.today().year + 1
    except ValueError:
        return date.today().year + 1


def verein_login(f):
    """Simuliertes Vereins-Login (live: require_verein_login + Rolle admin)."""
    @wraps(f)
    def wrapper(*a, **kw):
        key = session.get("verein")
        if not key or not db.konto(key):
            return redirect(url_for("anmelden", weiter=request.path))
        return f(key, *a, **kw)
    return wrapper


# ── Start ────────────────────────────────────────────────────────────────────

@app.get("/")
def start():
    return render_template("start.html", stand=D.stand())


@app.post("/daten-holen")
def daten_holen():
    D.hole_daten()
    return redirect(url_for("start"))


# ── 1. Kollisionswarnung (nur veröffentlichte Termine – Josef 2026-10-05) ───

@app.get("/kollision")
def kollision_seite():
    return render_template("kollision.html", vereine=D.vereine(), gruppen=D.vereine_gruppiert(),
                           heute=date.today().isoformat())


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
    mit = {x for x in request.args.get("mit", "").split(",") if x in d["labels"]}
    k = kollisionen(_veroeffentlichte(), d["meta"], d["labels"], entwurf, d["rubriken"],
                    wochenende=request.args.get("wochenende") == "1", zusatz_vereine=mit)
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
        andere = [dict(v, id=f"x{i}") for i, v in enumerate(alle) if v.get("verein") != verein] \
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


# ── 3. Vereinskonto (simuliert) ──────────────────────────────────────────────

@app.get("/anmelden")
def anmelden():
    return render_template("anmelden.html", konten=db.konten(), gruppen=D.vereine_gruppiert(),
                           raeume={r["id"]: r["titel"] for r in db.raeume()},
                           weiter=request.args.get("weiter", ""))


def _sicheres_ziel(weiter: str) -> str:
    return weiter if weiter.startswith("/") and not weiter.startswith("//") else url_for("entwuerfe_seite")


@app.post("/anmelden")
def anmelden_post():
    key = request.form.get("verein", "")
    labels = D.daten()["labels"]
    if key not in labels:
        abort(400)
    if request.form.get("aktion") == "konto":
        # Registrierung ohne Einladung: wartet auf Freigabe durch VKO (wie heute)
        db.konto_anlegen(key, labels[key])
    if not db.konto(key):
        abort(403, "Dieser Verein hat noch kein Konto.")
    session["verein"], session["verein_name"] = key, labels[key]
    return redirect(_sicheres_ziel(request.form.get("weiter", "")))


@app.post("/abmelden")
def abmelden():
    session.pop("verein", None)
    session.pop("verein_name", None)
    return redirect(url_for("start"))


@app.post("/demo/freigeben")
def demo_freigeben():
    """Simuliert Josefs Freigabe (live: Admin-App oder Telegram-Knopf, wie heute)."""
    db.konto_freigeben(request.form.get("verein", ""))
    return redirect(request.referrer or url_for("anmelden"))


@app.get("/einladung/<token>")
def einladung(token):
    e = db.einladung(token)
    if not e:
        abort(404)
    gueltig = e["aktiv"] and e["raum_status"] == "offen" and e["einladung_bis"] >= datetime.now().isoformat()
    return render_template("einladung.html", e=e, gueltig=gueltig, konto=db.konto(e["verein_key"]))


@app.post("/einladung/<token>")
def einladung_annehmen(token):
    """Registrierung über die Einladung. Das Konto wartet wie jede Registrierung auf Josefs Freigabe;
    die Einladung geht nur als Hinweis mit (Raum, Organisatorin). Einmalig – danach führt die Einladung
    nur noch zum Login. Live: Registrierungsformular mit vorbelegtem Verein; hier simuliert."""
    e = db.einladung(token)
    if not e:
        abort(404)
    if not (e["aktiv"] and e["raum_status"] == "offen" and e["einladung_bis"] >= datetime.now().isoformat()):
        abort(403, "Die Einladung ist nicht mehr gültig.")
    k = db.konto(e["verein_key"])
    if not k:
        if e["angenommen_am"]:
            abort(403, "Die Einladung wurde schon verwendet.")
        db.konto_anlegen(e["verein_key"], e["verein_name"], e["raum_id"])
        db.einladung_angenommen(e["id"])
    session["verein"], session["verein_name"] = e["verein_key"], e["verein_name"]
    return redirect(url_for("entwuerfe_seite", jahr=db.raum(e["raum_id"])["jahr"]))


# ── 4. Entwürfe des Vereins ──────────────────────────────────────────────────

@app.get("/entwuerfe")
@verein_login
def entwuerfe_seite(key):
    jahr = _zieljahr()
    eigene = db.entwuerfe(key, jahr)
    # Konflikte hier nur mit veröffentlichten Terminen – Entwürfe anderer Vereine sieht man im Treffen
    _mit_konflikten(eigene, _kalender_im_jahr(jahr))
    vorlage_n = len(db.offene_vorlage(key, D.vorschlaege(jahr)))   # nur noch nicht übernommene
    return render_template("entwuerfe.html", key=key, jahr=jahr, monate=_nach_monat(eigene),
                           n_entwurf=sum(1 for t in eigene if t["status"] == "entwurf"),
                           n_vorschlag=sum(1 for t in eigene if t.get("vorschlag_datum")),
                           vorlage_n=vorlage_n, konto=db.konto(key), darf=db.darf_veroeffentlichen(key),
                           raeume=db.raeume_des_vereins(key), fehler=request.args.get("fehler", ""),
                           meldung=request.args.get("meldung", ""), daten_ab=D.daten_ab())


def _zurueck(jahr, anker="", **kw):
    return redirect(url_for("entwuerfe_seite", jahr=jahr, **kw) + (f"#{anker}" if anker else ""))


@app.post("/entwuerfe/aus-vorjahr")
@verein_login
def entwuerfe_aus_vorjahr(key):
    jahr = _zieljahr()
    n = db.aus_vorlage(key, D.vorschlaege(jahr))
    return _zurueck(jahr, meldung=f"{n} Entwürfe aus dem Vorjahr angelegt." if n else "Keine neuen Vorschläge aus dem Vorjahr.")


@app.post("/entwuerfe/neu")
@verein_login
def entwurf_neu(key):
    felder, fehler = _felder_aus_formular()
    jahr = int(felder["datum"][:4]) if _datum_ok(felder["datum"]) else _zieljahr()
    if fehler:
        return _zurueck(jahr, "neu", fehler=fehler)
    db.entwurf_neu(key, felder)
    return _zurueck(jahr)


@app.post("/entwuerfe/<int:eid>")
@verein_login
def entwurf_aktion(key, eid):
    t = db.entwurf(eid)
    if not t or t["verein"] != key:
        abort(404)
    jahr, aktion = int(t["datum"][:4]), request.form.get("aktion", "")
    if aktion == "loeschen":
        db.entwurf_loeschen(eid, key)
    elif aktion in ("vorschlag_annehmen", "vorschlag_ablehnen"):
        db.vorschlag_uebernehmen(eid, key, aktion == "vorschlag_annehmen")
    elif aktion == "veroeffentlichen":
        if not db.darf_veroeffentlichen(key):
            abort(403, "Veröffentlichen erst nach Freigabe des Kontos.")
        db.veroeffentlichen(key, [eid])
    elif aktion == "zurueckziehen":
        db.zurueckziehen(eid, key)
    elif aktion == "speichern":
        felder, fehler = _felder_aus_formular()
        if fehler:
            return _zurueck(jahr, f"t{eid}", fehler=fehler)
        db.entwurf_aendern(eid, key, felder)
        jahr = int(felder["datum"][:4])
    else:
        abort(400)
    return _zurueck(jahr, f"t{eid}")


@app.post("/entwuerfe/alle-veroeffentlichen")
@verein_login
def alle_veroeffentlichen(key):
    jahr = _zieljahr()
    if not db.darf_veroeffentlichen(key):
        abort(403, "Veröffentlichen erst nach Freigabe des Kontos.")
    eids = [t["_eid"] for t in db.entwuerfe(key, jahr, status="entwurf")]
    n = db.veroeffentlichen(key, eids)
    return _zurueck(jahr, meldung=f"{n} Termine veröffentlicht (Prototyp: nur markiert).")


@app.get("/entwuerfe/export.<fmt>")
@verein_login
def entwuerfe_export(key, fmt):
    jahr = _zieljahr()
    return _export(f"Termine {jahr} – {session.get('verein_name', key)}", db.entwuerfe(key, jahr), fmt, mit_verein=False)


@app.get("/treffen/<int:raum_id>")
@verein_login
def raum_vereinssicht(key, raum_id):
    """Der Verein sieht im Treffen die Entwürfe aller beteiligten Vereine – nur lesend, eigene Termine
    ändert er auf seiner Entwurfsseite."""
    r = db.raum(raum_id)
    if not r or key not in db.aktive_keys(raum_id):
        abort(404)
    if not db.freigegeben(key):
        # Sonst könnte ein Fremder mit weitergeleiteter Einladung die Pläne aller Vereine lesen
        abort(403, "Die Entwürfe der anderen Vereine seht ihr, sobald VKO euer Konto freigegeben hat.")
    termine, paare, farben = _raum_daten(r)
    return render_template("raum_sicht.html", r=r, key=key, paare=paare, farben=farben,
                           labels=D.daten()["labels"], monate=_nach_monat(termine),
                           fertig=next((x["fertig_am"] for x in db.raeume_des_vereins(key) if x["id"] == raum_id), None))


@app.post("/treffen/<int:raum_id>/fertig")
@verein_login
def raum_fertig(key, raum_id):
    if key not in db.aktive_keys(raum_id):
        abort(404)
    db.fertig_melden(raum_id, key, request.form.get("fertig") == "1")
    return redirect(url_for("raum_vereinssicht", raum_id=raum_id))


# ── 5. Planungstreffen (Organisatorin) ───────────────────────────────────────

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
    return redirect(url_for("raum_orga", token=db.neuer_raum(gemeinde, landkreis, jahr, vereine)))


@app.post("/planung/<int:raum_id>/loeschen")
def planung_loeschen(raum_id):
    db.raum_loeschen(raum_id)
    return redirect(url_for("planung_liste"))


def _raum_oder_404(token):
    r = db.raum_per_token(token)
    if not r:
        abort(404)
    return r


def _raum_daten(r) -> tuple[list[dict], list[dict], dict]:
    """(Entwürfe + veröffentlichte Termine der aktiven Vereine im Planungsjahr mit Konflikten,
    Konfliktpaare am gleichen Tag, Farbe je Verein)."""
    keys = db.aktive_keys(r["id"])
    termine = db.entwuerfe(jahr=r["jahr"], vereine=keys)
    # Veröffentlichte Entwürfe der Raum-Vereine stecken schon in `termine` – nicht doppelt zählen
    andere = termine + [t for t in _kalender_im_jahr(r["jahr"])
                        if not (str(t.get("id", "")).startswith("e") and t.get("verein") in keys)]
    _mit_konflikten(termine, andere, zusatz=keys)   # wer im Raum ist, zählt immer – auch Nachbarn
    paare, gesehen = [], set()
    for t in termine:
        for k in t.get("_konflikte", []):
            if k["stufe"] != STUFE_TAG:
                continue
            schluessel = tuple(sorted((t["id"], str(k["id"]))))
            if schluessel not in gesehen:
                gesehen.add(schluessel)
                paare.append({"a": t, "b": k})
    paare.sort(key=lambda p: p["a"]["datum"])
    farben = {v["verein_key"]: i % 8 for i, v in enumerate(db.vereine_im_raum(r["id"]))}
    return termine, paare, farben


@app.get("/p/<token>")
def raum_orga(token):
    r = _raum_oder_404(token)
    termine, paare, farben = _raum_daten(r)
    vereine = db.vereine_im_raum(r["id"])
    im_raum = {v["verein_key"] for v in vereine if v["aktiv"]}
    gruppen = [{**g, "vereine": [x for x in g["vereine"] if x[0] not in im_raum]} for g in D.vereine_gruppiert()]
    anzahl = defaultdict(lambda: {"entwurf": 0, "veroeffentlicht": 0, "vorschlag": 0})
    for t in termine:
        anzahl[t["verein"]][t["status"]] += 1
        anzahl[t["verein"]]["vorschlag"] += bool(t.get("vorschlag_datum"))
    return render_template("raum_orga.html", r=r, vereine=vereine, paare=paare, farben=farben,
                           labels=D.daten()["labels"], monate=_nach_monat(termine), anzahl=anzahl,
                           konten=db.konten(), gruppen=[g for g in gruppen if g["vereine"]],
                           nachbarn={v["verein_key"] for v in vereine
                                     if D.sitz(v["verein_key"]) != (r["gemeinde"], r["landkreis"])},
                           treffen=request.args.get("ansicht") == "treffen",
                           basis=request.host_url.rstrip("/"), jetzt=datetime.now().isoformat())


@app.post("/p/<token>/vorschlag/<int:eid>")
def raum_orga_vorschlag(token, eid):
    """Verschiebe-Vorschlag – die Organisatorin ändert nie selbst, der Verein übernimmt (Hoheit beim Verein)."""
    r = _raum_oder_404(token)
    if r["status"] != "offen":
        abort(403, "Die Planung ist abgeschlossen.")
    datum = request.form.get("datum", "").strip()
    if request.form.get("aktion") == "zuruecknehmen":
        db.vorschlag_machen(r["id"], eid, None)
    elif _datum_ok(datum):
        db.vorschlag_machen(r["id"], eid, datum)
    else:
        abort(400)
    return redirect(url_for("raum_orga", token=token, ansicht=request.args.get("ansicht")) + f"#t{eid}")


@app.post("/p/<token>/status")
def raum_orga_status(token):
    r = _raum_oder_404(token)
    db.raum_status(r["id"], "abgeschlossen" if request.form.get("aktion") == "abschliessen" else "offen")
    return redirect(url_for("raum_orga", token=token))


@app.post("/p/<token>/verein/<int:vid>")
def raum_orga_verein(token, vid):
    r = _raum_oder_404(token)
    db.verein_aktiv(r["id"], vid, request.form.get("aktiv") == "1")
    return redirect(url_for("raum_orga", token=token) + "#vereine")


@app.post("/p/<token>/dazuholen")
def raum_orga_dazuholen(token):
    r = _raum_oder_404(token)
    labels = D.daten()["labels"]
    for key in request.form.getlist("verein")[:50]:
        if key in labels:
            db.verein_dazuholen(r["id"], key, labels[key])
    return redirect(url_for("raum_orga", token=token) + "#vereine")


@app.get("/p/<token>/export.<fmt>")
def raum_orga_export(token, fmt):
    r = _raum_oder_404(token)
    return _export(r["titel"], db.entwuerfe(jahr=r["jahr"], vereine=db.aktive_keys(r["id"])), fmt, mit_verein=True)


@app.template_filter("wochentag_kurz")
def _wt(datum: str) -> str:
    try:
        return ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")[date.fromisoformat(datum).weekday()]
    except (ValueError, TypeError):
        return ""


@app.template_filter("tm")
def _tm(datum: str) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", datum or "")
    return f"{m[3]}.{m[2]}." if m else (datum or "")


if __name__ == "__main__":
    db.init()
    if not D.TERMINE_FILE.exists():
        print("Keine Momentaufnahme – hole /api/termine …")
        print(f"{D.hole_daten()} Termine geladen.")
    app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=True)
