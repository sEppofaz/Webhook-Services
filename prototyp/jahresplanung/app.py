"""Prototyp Jahresplanung – lokal, nicht integriert (Branch `jahresplanung`, Josef 2026-10-05).

Start:  ~/.venvs/vko-jahresplanung/bin/python prototyp/jahresplanung/app.py
Dann:   http://localhost:5050

Bereiche:
1. Kollisionswarnung – wie sie später im Vereinsformular erscheint (nur veröffentlichte Termine).
2. Vorjahres-Vorlage – Termine eines Vereins nach ihrem Schema ins Zieljahr übertragen.
3. Entwürfe – jeder Verein plant in seinem Bereich: anlegen, aus dem Vorjahr erzeugen, bestätigen,
   einzeln oder alle veröffentlichen. Nur der Verein selbst veröffentlicht. Login simuliert.
4. Planungsrunden – jeder freigegebene Vereinsadmin startet eine (Organisator, ohne Josef) und lädt per Link
   oder Code ein. Teilnehmer sehen alle Entwürfe mit Konflikten und ändern/bestätigen ihre eigenen (Mac/Handy).
   Verlauf wird mitgeschrieben; beim Abschluss entsteht das Ergebnis (PDF) im Archiv jedes beteiligten Vereins.
Josef gibt jedes neue Konto persönlich frei (ADR-026). Schreibt nie in den Live-Kalender. Daten: Momentaufnahme der öffentlichen /api/termine.
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
from shared.export import FORMATE, datum_text, dateiname, ergebnis_pdf, exportiere, zeit_text
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
    entwurf_ids = {x["id"] for x in andere if str(x.get("id", "")).startswith("e") and x.get("status") != "veroeffentlicht"}
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
        db.konto_anlegen(key, labels[key])   # wartet auf Josefs Freigabe
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


# ── 4. Entwürfe des Vereins (sein eigener Bereich) ───────────────────────────

@app.get("/entwuerfe")
@verein_login
def entwuerfe_seite(key):
    jahr = _zieljahr()
    eigene = db.entwuerfe(key, jahr)
    # Konflikte hier nur mit veröffentlichten Terminen – Entwürfe anderer Vereine sieht man in der Runde
    _mit_konflikten(eigene, _kalender_im_jahr(jahr))
    return render_template("entwuerfe.html", key=key, jahr=jahr, monate=_nach_monat(eigene),
                           n_offen=sum(1 for t in eigene if t["status"] != "veroeffentlicht"),
                           n_unbestaetigt=sum(1 for t in eigene if t["status"] == "entwurf"),
                           vorlage_n=len(db.offene_vorlage(key, D.vorschlaege(jahr))), konto=db.konto(key),
                           darf=db.freigegeben(key), runden=db.runden_des_vereins(key),
                           fehler=request.args.get("fehler", ""), meldung=request.args.get("meldung", ""),
                           daten_ab=D.daten_ab())


def _zurueck(jahr: int, anker: str = "", **kw):
    """Zurück zur Runde, aus der die Aktion kam (Feld `runde`, nur wenn man dabei ist), sonst zu den Entwürfen."""
    rid = request.form.get("runde", "")
    if rid.isdigit() and session.get("verein") in db.aktive_teilnehmer(int(rid)):
        return redirect(url_for("runde_seite", runde_id=int(rid), **kw) + (f"#{anker}" if anker else ""))
    return redirect(url_for("entwuerfe_seite", jahr=jahr, **kw) + (f"#{anker}" if anker else ""))


def _verlauf(key: str, jahr: int, aktion: str, details: str = "") -> None:
    """Terminänderungen im Verlauf aller offenen Runden des Vereins für dieses Jahr festhalten –
    egal ob in der Runde oder auf der Entwurfsseite geändert."""
    for rid in db.offene_runden(key, jahr):
        db.protokoll(rid, key, aktion, details)


def _kurz(datum: str) -> str:
    return f"{_wt(datum)} {_tm(datum)}"


@app.post("/entwuerfe/aus-vorjahr")
@verein_login
def entwuerfe_aus_vorjahr(key):
    jahr = _zieljahr()
    n = db.aus_vorlage(key, D.vorschlaege(jahr))
    if n:
        _verlauf(key, jahr, "Entwürfe aus dem Vorjahr erzeugt", f"{n} Termine")
    return _zurueck(jahr, meldung=f"{n} Entwürfe aus dem Vorjahr angelegt." if n else "Keine neuen Vorschläge aus dem Vorjahr.")


@app.post("/entwuerfe/neu")
@verein_login
def entwurf_neu(key):
    felder, fehler = _felder_aus_formular()
    jahr = int(felder["datum"][:4]) if _datum_ok(felder["datum"]) else _zieljahr()
    if fehler:
        return _zurueck(jahr, "neu", fehler=fehler)
    db.entwurf_neu(key, felder)
    _verlauf(key, jahr, "Termin ergänzt", f"{felder['bezeichnung']}: {_kurz(felder['datum'])}")
    return _zurueck(jahr)


@app.post("/entwuerfe/<int:eid>")
@verein_login
def entwurf_aktion(key, eid):
    t = db.entwurf(eid)
    if not t or t["verein"] != key:
        abort(404)   # fremde Termine gibt es für diesen Verein nicht
    jahr, aktion = int(t["datum"][:4]), request.form.get("aktion", "")
    name = t["bezeichnung"]
    if aktion == "loeschen":
        if db.entwurf_loeschen(eid, key):
            _verlauf(key, jahr, "Termin gelöscht", f"{name}: {_kurz(t['datum'])}")
    elif aktion in ("bestaetigen", "nicht_bestaetigen"):
        if db.bestaetigen(key, [eid], aktion == "bestaetigen"):
            _verlauf(key, jahr, "bestätigt" if aktion == "bestaetigen" else "Bestätigung zurückgenommen", name)
    elif aktion == "veroeffentlichen":
        if not db.freigegeben(key):
            abort(403, "Veröffentlichen erst nach Freigabe des Kontos.")
        if db.veroeffentlichen(key, [eid]):
            _verlauf(key, jahr, "veröffentlicht", name)
    elif aktion == "zurueckziehen":
        db.zurueckziehen(eid, key)
    elif aktion == "speichern":
        felder, fehler = _felder_aus_formular()
        if fehler:
            return _zurueck(jahr, f"t{eid}", fehler=fehler)
        if db.entwurf_aendern(eid, key, felder):
            aenderung = [f"{_kurz(t['datum'])} → {_kurz(felder['datum'])}"] if felder["datum"] != t["datum"] else []
            if felder["uhrzeit"] != t["uhrzeit"]:
                aenderung.append(f"{t['uhrzeit'] or 'ohne Uhrzeit'} → {felder['uhrzeit'] or 'ohne Uhrzeit'}")
            if felder["bezeichnung"] != name:
                aenderung.append(f"neuer Titel „{felder['bezeichnung']}“")
            if felder["ort"] != t["ort"]:
                aenderung.append("Ort geändert")
            _verlauf(key, jahr, "Termin geändert", f"{name}: " + (", ".join(aenderung) or "ohne Änderung"))
            # Neues Jahr: auch dort festhalten
            if int(felder["datum"][:4]) != jahr:
                _verlauf(key, int(felder["datum"][:4]), "Termin ergänzt (aus anderem Jahr verschoben)", name)
        jahr = int(felder["datum"][:4])
    else:
        abort(400)
    return _zurueck(jahr, f"t{eid}")


@app.post("/entwuerfe/alle")
@verein_login
def entwuerfe_alle(key):
    """Alle eigenen Entwürfe eines Jahres bestätigen oder veröffentlichen."""
    jahr = _zieljahr()
    offen = [t["_eid"] for t in db.entwuerfe(key, jahr) if t["status"] != "veroeffentlicht"]
    if request.form.get("aktion") == "veroeffentlichen":
        if not db.freigegeben(key):
            abort(403, "Veröffentlichen erst nach Freigabe des Kontos.")
        n = db.veroeffentlichen(key, offen)
        if n:
            _verlauf(key, jahr, "alle veröffentlicht", f"{n} Termine")
        return _zurueck(jahr, meldung=f"{n} Termine veröffentlicht (Prototyp: nur markiert).")
    n = db.bestaetigen(key, [t["_eid"] for t in db.entwuerfe(key, jahr) if t["status"] == "entwurf"])
    if n:
        _verlauf(key, jahr, "alle bestätigt", f"{n} Termine")
    return _zurueck(jahr, meldung=f"{n} Termine bestätigt.")


@app.get("/entwuerfe/export.<fmt>")
@verein_login
def entwuerfe_export(key, fmt):
    jahr = _zieljahr()
    return _export(f"Termine {jahr} – {session.get('verein_name', key)}", db.entwuerfe(key, jahr), fmt, mit_verein=False)


# ── 5. Planungsrunden ────────────────────────────────────────────────────────

def _versuche_ok() -> bool:
    """Gegen Durchprobieren von Codes: höchstens 10 Fehlversuche je 10 Minuten und Sitzung
    (live zusätzlich nginx-Limit und Zähler je Konto)."""
    jetzt_s = datetime.now().timestamp()
    v = [x for x in session.get("code_fehler", []) if jetzt_s - x < 600]
    session["code_fehler"] = v
    return len(v) < 10


@app.get("/planung")
def planung_alt():
    return redirect(url_for("runden_seite"))


@app.get("/runden")
@verein_login
def runden_seite(key):
    return render_template("runden.html", runden=db.runden_des_vereins(key), darf=db.freigegeben(key),
                           jahr=date.today().year + 1, fehler=request.args.get("fehler", ""),
                           code=request.args.get("code", ""))


@app.post("/runden/neu")
@verein_login
def runde_neu(key):
    if not db.freigegeben(key):
        abort(403, "Planungsrunden erst nach Freigabe des Kontos.")
    name = request.form.get("name", "").strip()[:TEXT_MAX]
    try:
        jahr = int(request.form.get("jahr", ""))
    except ValueError:
        abort(400)
    if not name or not 2026 <= jahr <= 2040:
        return redirect(url_for("runden_seite", fehler="Bitte Name und Jahr angeben."))
    return redirect(url_for("runde_seite", runde_id=db.runde_starten(name, jahr, key)))


def _beitritt_seite(key, r):
    """Gemeinsame Bestätigungsseite für Link und Code – beitreten nur per aktivem Klick."""
    teil = {t["verein_key"]: t for t in db.teilnehmer(r["id"])}
    return render_template("beitreten.html", r=r, darf=db.freigegeben(key),
                           status=(teil[key]["aktiv"] if key in teil else None),
                           organisator=D.daten()["labels"].get(r["organisator"], r["organisator"]))


@app.get("/r/<link>")
@verein_login
def runde_link(key, link):
    r = db.runde_per_link(link)
    if not r:
        abort(404, "Diesen Einladungslink gibt es nicht (mehr). Bitte beim Organisator nachfragen.")
    return _beitritt_seite(key, r)


@app.post("/runde/beitreten-code")
@verein_login
def runde_code(key):
    if not _versuche_ok():
        abort(429, "Zu viele falsche Codes. Bitte in 10 Minuten noch einmal.")
    r = db.runde_per_code(request.form.get("code", ""))
    if not r:
        session["code_fehler"] = session.get("code_fehler", []) + [datetime.now().timestamp()]
        return redirect(url_for("runden_seite", fehler="Code nicht gefunden.", code=request.form.get("code", "")[:12]))
    return _beitritt_seite(key, r)


@app.post("/runde/<int:runde_id>/beitreten")
@verein_login
def runde_beitreten(key, runde_id):
    """Beitreten braucht den gültigen Link oder Code der Runde – die ID allein reicht nicht."""
    r = db.runde(runde_id)
    nachweis = request.form.get("nachweis", "")
    if not r or nachweis not in (r["link"], r["code"]):
        abort(403, "Einladung nicht (mehr) gültig – Link oder Code beim Organisator neu holen.")
    if not db.freigegeben(key):
        abort(403, "Beitreten erst nach Freigabe eures Kontos durch VKO.")
    if r["status"] != "offen":
        abort(403, "Diese Planungsrunde ist abgeschlossen.")
    ergebnis = db.beitreten(runde_id, key)
    if ergebnis == "entfernt":
        abort(403, "Der Organisator hat euch aus dieser Runde entfernt.")
    if ergebnis == "neu":
        db.protokoll(runde_id, key, "beigetreten", "per Code" if nachweis == r["code"] else "per Link")
    return redirect(url_for("runde_seite", runde_id=runde_id))


def _runde_fuer(key, runde_id):
    r = db.runde(runde_id)
    if not r or key not in db.aktive_teilnehmer(runde_id):
        abort(404)
    if not db.freigegeben(key):
        abort(403)
    return r


def _runde_daten(r) -> tuple[list[dict], list[dict], dict]:
    """(Entwürfe der aktiven Teilnehmer im Planungsjahr mit Konflikten, Konfliktpaare am gleichen Tag, Farben)."""
    keys = db.aktive_teilnehmer(r["id"])
    termine = db.entwuerfe(jahr=r["jahr"], vereine=keys)
    # Veröffentlichte Entwürfe der Teilnehmer stecken schon in `termine` – nicht doppelt zählen
    andere = termine + [t for t in _kalender_im_jahr(r["jahr"])
                        if not (str(t.get("id", "")).startswith("e") and t.get("verein") in keys)]
    _mit_konflikten(termine, andere, zusatz=keys)   # wer in der Runde ist, zählt immer – auch Nachbarn
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
    farben = {t["verein_key"]: i % 8 for i, t in enumerate(db.teilnehmer(r["id"]))}
    return termine, paare, farben


@app.get("/runde/<int:runde_id>")
@verein_login
def runde_seite(key, runde_id):
    r = _runde_fuer(key, runde_id)
    termine, paare, farben = _runde_daten(r)
    stand = defaultdict(lambda: {"entwurf": 0, "bestaetigt": 0, "veroeffentlicht": 0})
    for t in termine:
        stand[t["verein"]][t["status"]] += 1
    return render_template("runde.html", r=r, key=key, organisator=(r["organisator"] == key),
                           stand_kennung=db.stand_version(runde_id, r["jahr"]), ergebnisse=db.ergebnisse(runde_id),
                           verlauf=list(reversed(db.protokoll_liste(runde_id)))[:200],
                           teilnehmer=db.teilnehmer(r["id"]), stand=stand, paare=paare, farben=farben,
                           labels=D.daten()["labels"], monate=_nach_monat(termine),
                           code=db.code_anzeige(r["code"]), basis=request.host_url.rstrip("/"),
                           vorlage_n=len(db.offene_vorlage(key, D.vorschlaege(r["jahr"]))),
                           treffen=request.args.get("ansicht") == "treffen",
                           fehler=request.args.get("fehler", ""), meldung=request.args.get("meldung", ""))


@app.post("/runde/<int:runde_id>/verlassen")
@verein_login
def runde_verlassen(key, runde_id):
    r = _runde_fuer(key, runde_id)
    if r["organisator"] == key:
        abort(400, "Der Organisator kann die Runde nicht verlassen – abschließen geht.")
    db.verlassen(runde_id, key)
    db.protokoll(runde_id, key, "ausgetreten")
    return redirect(url_for("runden_seite"))


def _nur_organisator(key, runde_id):
    r = _runde_fuer(key, runde_id)
    if r["organisator"] != key:
        abort(403, "Nur der Organisator.")
    return r


@app.post("/runde/<int:runde_id>/erneuern")
@verein_login
def runde_erneuern(key, runde_id):
    _nur_organisator(key, runde_id)
    db.einladung_erneuern(runde_id)
    db.protokoll(runde_id, key, "Link und Code erneuert")
    return redirect(url_for("runde_seite", runde_id=runde_id, meldung="Neuer Link und neuer Code – die alten gelten nicht mehr."))


@app.post("/runde/<int:runde_id>/teilnehmer")
@verein_login
def runde_teilnehmer(key, runde_id):
    _nur_organisator(key, runde_id)
    verein = request.form.get("verein", "")
    if verein and verein != key and verein in {t["verein_key"] for t in db.teilnehmer(runde_id)}:
        aktiv = request.form.get("aktiv") == "1"
        db.teilnehmer_setzen(runde_id, verein, aktiv)
        db.protokoll(runde_id, key, "wieder aufgenommen" if aktiv else "entfernt", D.daten()["labels"].get(verein, verein))
    return redirect(url_for("runde_seite", runde_id=runde_id) + "#teilnehmer")


@app.post("/runde/<int:runde_id>/status")
@verein_login
def runde_status(key, runde_id):
    r = _nur_organisator(key, runde_id)
    if request.form.get("aktion") == "abschliessen" and r["status"] == "offen":
        version = db.ergebnis_speichern(runde_id, key, _ergebnis_stand(r, key))
        db.runde_status(runde_id, "abgeschlossen")
        return redirect(url_for("runde_seite", runde_id=runde_id,
                                meldung=f"Runde abgeschlossen. Das Ergebnis (Version {version}) liegt jetzt im Archiv jedes beteiligten Vereins."))
    if request.form.get("aktion") == "oeffnen" and r["status"] != "offen":
        db.runde_status(runde_id, "offen")
        db.protokoll(runde_id, key, "Runde wieder geöffnet")
    return redirect(url_for("runde_seite", runde_id=runde_id))


def _ergebnis_stand(r, key) -> dict:
    """Eingefrorener Stand beim Abschluss – Grundlage für das Ergebnis-PDF im Archiv."""
    labels = D.daten()["labels"]
    termine, paare, _ = _runde_daten(r)
    keys = db.aktive_teilnehmer(r["id"])
    jetzt_s = datetime.now().isoformat(timespec="seconds")
    with db.conn() as c:
        version = (c.execute("SELECT MAX(version) FROM runde_ergebnis WHERE runde_id = ?", (r["id"],)).fetchone()[0] or 0) + 1
    verlauf = db.protokoll_liste(r["id"]) + [{"zeit": jetzt_s, "verein_key": key, "aktion": "Runde abgeschlossen",
                                              "details": f"Ergebnis Version {version}"}]
    return {
        "runde_id": r["id"], "name": r["name"], "jahr": r["jahr"], "version": version,
        "organisator": r["organisator"], "organisator_name": labels.get(r["organisator"], r["organisator"]),
        "abgeschlossen_am": jetzt_s, "abgeschlossen_von": key, "abgeschlossen_von_name": labels.get(key, key),
        "teilnehmer": sorted(({"verein": k, "name": labels.get(k, k)} for k in keys), key=lambda x: x["name"].lower()),
        "termine": [{"verein": t["verein"], "verein_name": labels.get(t["verein"], t["verein"]),
                     **{f: t.get(f, "") for f in ("datum", "uhrzeit", "uhrzeit_bis", "bezeichnung", "ort")},
                     "status": t["status"]} for t in termine],
        "konflikte": [{"datum": p["a"]["datum"], "a": f"{p['a']['bezeichnung']} ({labels.get(p['a']['verein'], p['a']['verein'])})",
                       "b": f"{p['b']['bezeichnung']} ({p['b']['verein_name']})"} for p in paare],
        "verlauf": [{**v, "verein_name": labels.get(v["verein_key"], v["verein_key"])} for v in verlauf],
    }


# ── 6. Vereins-Archiv ────────────────────────────────────────────────────────

@app.get("/archiv")
@verein_login
def archiv(key):
    return render_template("archiv.html", ergebnisse=db.ergebnisse_des_vereins(key))


@app.get("/archiv/ergebnis/<int:eid>.<fmt>")
@verein_login
def archiv_ergebnis(key, eid, fmt):
    """Nur für Vereine, die beim Abschluss beteiligt waren."""
    e = db.ergebnis(eid)
    if not e or key not in {t["verein"] for t in e["daten"]["teilnehmer"]}:
        abort(404)
    d = e["daten"]
    titel = f"Ergebnis {d['name']} {d['jahr']} (Version {d.get('version', e['version'])})"
    if fmt == "pdf":
        return Response(ergebnis_pdf(d), mimetype="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{dateiname(titel, "pdf")}"'})
    return _export(titel, d["termine"], fmt, mit_verein=True)


# ── 7. Netz: Lebenszeichen und Stand für das Live-Treffen ────────────────────

@app.get("/ping")
def ping():
    """Für den Netz-Hinweis: misst die Antwortzeit im Browser. Nie zwischenspeichern."""
    return Response(status=204, headers={"Cache-Control": "no-store"})


@app.get("/runde/<int:runde_id>/stand")
@verein_login
def runde_stand(key, runde_id):
    """Kennung des Stands – die Rundenseite fragt sie regelmäßig ab und meldet „Andere haben geändert“."""
    r = _runde_fuer(key, runde_id)
    return jsonify({"stand": db.stand_version(runde_id, r["jahr"])}), 200, {"Cache-Control": "no-store"}


@app.get("/runde/<int:runde_id>/export.<fmt>")
@verein_login
def runde_export(key, runde_id, fmt):
    r = _runde_fuer(key, runde_id)
    return _export(f"{r['name']} {r['jahr']}", db.entwuerfe(jahr=r["jahr"], vereine=db.aktive_teilnehmer(runde_id)),
                   fmt, mit_verein=True)


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
