"""Prototyp Jahresplanung – lokal, nicht integriert (Branch `jahresplanung`, Josef 2026-10-05).

Start:  ~/.venvs/vko-jahresplanung/bin/python prototyp/jahresplanung/app.py
Dann:   http://localhost:5050

Aufbau (Josef 2026-10-05):
- Startseite nach dem Anmelden: „Termine“ – alle Termine des Vereins (im Kalender + Entwürfe). Neuer Termin mit
  Kollisionswarnung beim Tippen; „Veröffentlichen“ oder „Als Entwurf speichern“. Vorjahres-Vorlage als Knopf.
- „Einstellungen“: mit welchen Vereinen auf Überschneidungen geprüft wird (Gemeinde automatisch + dazu/ohne).
- „Planungsrunden“: jeder freigegebene Vereinsadmin startet eine (Organisator) und lädt per Link oder Code ein;
  Teilnehmer sehen alle Entwürfe mit Konflikten und ändern/bestätigen ihre eigenen. Verlauf + Ergebnis-PDF.
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


def _mit_konflikten(eigene: list[dict], andere: list[dict], zusatz: set | None = None,
                    ohne: set | None = None) -> list[dict]:
    """Jedem Termin seine Kollisionen anhängen (`_konflikte`); Quelle Entwurf/Kalender markiert."""
    d = D.daten()
    entwurf_ids = {x["id"] for x in andere if str(x.get("id", "")).startswith("e") and x.get("status") != "veroeffentlicht"}
    for t in eigene:
        k = kollisionen(andere, d["meta"], d["labels"], t, d["rubriken"], wochenende=True,
                        ausser_ids={t.get("id")}, zusatz_vereine=zusatz, ohne_vereine=ohne)
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


def _jahr_filter() -> int | None:
    """Jahr-Filter der Termin-Seite: leer = „ab heute“ über alle Jahre."""
    try:
        j = int(request.values.get("jahr", ""))
        return j if 2020 <= j <= 2040 else None
    except ValueError:
        return None


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
    """Angemeldet ist die Startseite „Termine“ (Josef 2026-10-05), sonst kurze Erklärung + Anmelden."""
    if session.get("verein") and db.konto(session["verein"]):
        return redirect(url_for("termine_seite"))
    return render_template("start.html")


@app.post("/daten-holen")
def daten_holen():
    D.hole_daten()
    return redirect(request.referrer or url_for("start"))


@app.context_processor
def _datenstand():
    return {"stand": D.stand()}


# Frühere Tabs – jetzt in „Termine“ aufgegangen
@app.get("/kollision")
@app.get("/vorlage")
@app.get("/entwuerfe")
def alte_seiten():
    return redirect(url_for("termine_seite", **({"jahr": request.args["jahr"]} if request.args.get("jahr") else {})))


# ── Vereinskonto (simuliert) ─────────────────────────────────────────────────

@app.get("/anmelden")
def anmelden():
    return render_template("anmelden.html", konten=db.konten(), gruppen=D.vereine_gruppiert(),
                           weiter=request.args.get("weiter", ""))


def _sicheres_ziel(weiter: str) -> str:
    return weiter if weiter.startswith("/") and not weiter.startswith("//") else url_for("termine_seite")


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


# ── Termine des Vereins (Startseite) ─────────────────────────────────────────

def _kalender_termine(key: str) -> list[dict]:
    """Termine des Vereins, die schon im Kalender stehen (Momentaufnahme) – im Prototyp nur lesend."""
    out = []
    for t in D.daten()["termine"]:
        if t.get("verein") == key and t.get("datum"):
            out.append({**t, "status": "kalender", "_eid": f"k{t.get('id', '')}"})
    return out


def _pruefen(key: str, termine: list[dict]) -> list[dict]:
    """Konflikte nach dem Prüfkreis des Vereins: Gemeinde automatisch + dazu − ohne. Nur veröffentlichte Termine."""
    dazu, ohne = db.pruefkreis(key)
    jahre = {t["datum"][:4] for t in termine}
    andere = [t for t in _veroeffentlichte() if t.get("datum", "")[:4] in jahre]
    return _mit_konflikten(termine, andere, zusatz=dazu, ohne=ohne)


@app.get("/termine")
@verein_login
def termine_seite(key):
    jahr = _jahr_filter()
    heute = date.today().isoformat()
    alle = _kalender_termine(key) + db.entwuerfe(key)
    sicht = [t for t in alle if (t["datum"][:4] == str(jahr) if jahr else t["datum"] >= heute)]
    _pruefen(key, sicht)
    eigene = [t for t in sicht if t["status"] in ("entwurf", "bestaetigt")]
    vorlage_jahr = jahr or date.today().year + 1
    jahre = sorted({int(t["datum"][:4]) for t in alle} | {date.today().year, date.today().year + 1})
    return render_template("termine.html", key=key, jahr=jahr, jahre=jahre, monate=_nach_monat(sicht),
                           n_offen=len(eigene), n_unbestaetigt=sum(1 for t in eigene if t["status"] == "entwurf"),
                           vorlage_jahr=vorlage_jahr,
                           vorlage_n=len(db.offene_vorlage(key, D.vorschlaege(vorlage_jahr))), konto=db.konto(key),
                           darf=db.freigegeben(key), runden=[r for r in db.runden_des_vereins(key) if r["status"] == "offen"],
                           fehler=request.args.get("fehler", ""), meldung=request.args.get("meldung", ""),
                           heute=heute, daten_ab=D.daten_ab())


@app.get("/api/kollisionen")
def api_kollisionen():
    """Warnung im Formular „Neuer Termin“/„Ändern“ (live: `GET /verein/kollisionen`). Verein aus der Sitzung,
    Prüfkreis aus den Einstellungen, nur veröffentlichte Termine."""
    key = session.get("verein")
    if not key or not db.konto(key):
        return jsonify({"fehler": "nicht angemeldet"}), 401
    von, bis = request.args.get("von", ""), request.args.get("bis", "") or request.args.get("von", "")
    if not _datum_ok(von) or not _datum_ok(bis) or bis < von:
        return jsonify([])
    tage = []
    d0, d1 = date.fromisoformat(von), date.fromisoformat(bis)
    while d0 <= d1 and len(tage) < 16:
        tage.append(d0.isoformat())
        d0 = date.fromordinal(d0.toordinal() + 1)
    d = D.daten()
    dazu, ohne = db.pruefkreis(key)
    entwurf = {"verein": key, "tage": tage,
               "ort": request.args.get("ort", "")[:TEXT_MAX], "uhrzeit": request.args.get("uhrzeit", "")[:5]}
    ausser = {f"e{request.args['ohne_id']}"} if request.args.get("ohne_id", "").isdigit() else None
    k = kollisionen(_veroeffentlichte(), d["meta"], d["labels"], entwurf, d["rubriken"],
                    wochenende=request.args.get("wochenende") == "1", zusatz_vereine=dazu, ohne_vereine=ohne,
                    ausser_ids=ausser)
    return jsonify([{**x, "datum_text": datum_text(x)} for x in k])


def _zurueck(jahr: int | None = None, anker: str = "", **kw):
    """Zurück zur Runde, aus der die Aktion kam (Feld `runde`, nur wenn man dabei ist), sonst zu „Termine“
    (mit dem Jahr-Filter, aus dem die Aktion kam – Feld `ansicht_jahr`)."""
    rid = request.form.get("runde", "")
    if rid.isdigit() and session.get("verein") in db.aktive_teilnehmer(int(rid)):
        return redirect(url_for("runde_seite", runde_id=int(rid), **kw) + (f"#{anker}" if anker else ""))
    ansicht = request.form.get("ansicht_jahr", "")
    if ansicht.isdigit():
        kw["jahr"] = ansicht
    return redirect(url_for("termine_seite", **kw) + (f"#{anker}" if anker else ""))


def _verlauf(key: str, jahr: int, aktion: str, details: str = "") -> None:
    """Terminänderungen im Verlauf aller offenen Runden des Vereins für dieses Jahr festhalten –
    egal ob in der Runde oder auf der Termin-Seite geändert."""
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
    return _zurueck(jahr, meldung=f"{n} Entwürfe für {jahr} aus dem Vorjahr angelegt." if n else "Keine neuen Vorschläge aus dem Vorjahr.")


_MAX_TAGE = 16   # wie live (ADR-017)


@app.post("/entwuerfe/neu")
@verein_login
def entwurf_neu(key):
    """Neuer Termin: „Veröffentlichen“ (freigegebenes Konto) oder „Als Entwurf speichern“. Mehrtägig = je Tag ein Eintrag."""
    felder, fehler = _felder_aus_formular()
    bis = request.form.get("datum_bis", "").strip()
    tage = [felder["datum"]]
    if not fehler and bis:
        if not _datum_ok(bis) or bis < felder["datum"]:
            fehler = "Das bis-Datum ist ungültig oder liegt vor dem Startdatum."
        else:
            d0, d1 = date.fromisoformat(felder["datum"]), date.fromisoformat(bis)
            if (d1 - d0).days + 1 > _MAX_TAGE:
                fehler = f"Höchstens {_MAX_TAGE} Tage auf einmal."
            tage = [date.fromordinal(d0.toordinal() + i).isoformat() for i in range((d1 - d0).days + 1)]
    if fehler:
        return _zurueck(None, "neu", fehler=fehler)
    sofort = request.form.get("aktion") == "veroeffentlichen"
    if sofort and not db.freigegeben(key):
        abort(403, "Veröffentlichen erst nach Freigabe des Kontos – als Entwurf speichern geht schon.")
    ids = [db.entwurf_neu(key, {**felder, "datum": tag}) for tag in tage]
    if sofort:
        db.veroeffentlichen(key, ids)
    jahr = int(felder["datum"][:4])
    _verlauf(key, jahr, "Termin veröffentlicht" if sofort else "Termin ergänzt",
             f"{felder['bezeichnung']}: {_kurz(tage[0])}" + (f" bis {_kurz(tage[-1])}" if len(tage) > 1 else ""))
    wort = "veröffentlicht (Prototyp: nur markiert)" if sofort else "als Entwurf gespeichert"
    return _zurueck(None, f"t{ids[0]}", meldung=f"{felder['bezeichnung']}: {len(tage)} {'Tag' if len(tage) == 1 else 'Tage'} {wort}.")


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
            if int(felder["datum"][:4]) != jahr:
                _verlauf(key, int(felder["datum"][:4]), "Termin ergänzt (aus anderem Jahr verschoben)", name)
        jahr = int(felder["datum"][:4])
    else:
        abort(400)
    return _zurueck(jahr, f"t{eid}")


@app.post("/entwuerfe/alle")
@verein_login
def entwuerfe_alle(key):
    """Alle eigenen Entwürfe bestätigen oder veröffentlichen – eines Jahres (Feld `jahr`) oder, ohne Jahr,
    alle ab heute (Standardansicht von „Termine“)."""
    jahr = _jahr_filter()
    heute = date.today().isoformat()
    offen = [t for t in db.entwuerfe(key, jahr) if t["status"] != "veroeffentlicht" and (jahr or t["datum"] >= heute)]
    jahre = {int(t["datum"][:4]) for t in offen}
    if request.form.get("aktion") == "veroeffentlichen":
        if not db.freigegeben(key):
            abort(403, "Veröffentlichen erst nach Freigabe des Kontos.")
        n = db.veroeffentlichen(key, [t["_eid"] for t in offen])
        for j in jahre:
            _verlauf(key, j, "alle veröffentlicht", f"{sum(1 for t in offen if t['datum'][:4] == str(j))} Termine")
        return _zurueck(jahr, meldung=f"{n} Termine veröffentlicht (Prototyp: nur markiert).")
    unbest = [t for t in offen if t["status"] == "entwurf"]
    n = db.bestaetigen(key, [t["_eid"] for t in unbest])
    for j in {int(t["datum"][:4]) for t in unbest}:
        _verlauf(key, j, "alle bestätigt", f"{sum(1 for t in unbest if t['datum'][:4] == str(j))} Termine")
    return _zurueck(jahr, meldung=f"{n} Termine bestätigt.")


@app.get("/termine/export.<fmt>")
@verein_login
def termine_export(key, fmt):
    jahr = _jahr_filter()
    heute = date.today().isoformat()
    alle = _kalender_termine(key) + db.entwuerfe(key)
    zeilen = [t for t in alle if (t["datum"][:4] == str(jahr) if jahr else t["datum"] >= heute)]
    return _export(f"Termine {jahr or 'ab ' + date.today().strftime('%d.%m.%Y')} – {session.get('verein_name', key)}",
                   zeilen, fmt, mit_verein=False)


# ── Einstellungen: Überschneidungen prüfen mit … ─────────────────────────────

@app.route("/einstellungen", methods=["GET", "POST"])
@verein_login
def einstellungen(key):
    labels = D.daten()["labels"]
    gem, lk = D.sitz(key)
    eigene_gemeinde = [(k, n) for k, n in D.vereine_der_gemeinde(gem, lk) if k != key] if gem else []
    if request.method == "POST":
        dazu = {k for k in request.form.getlist("dazu") if k in labels}
        ohne = {k for k in request.form.getlist("ohne") if k in {x for x, _ in eigene_gemeinde}}
        db.pruefkreis_setzen(key, dazu, ohne)
        return redirect(url_for("einstellungen", meldung="Gespeichert. Gilt ab sofort für alle Warnungen."))
    dazu, ohne = db.pruefkreis(key)
    gruppen = [{**g, "vereine": [x for x in g["vereine"] if x[0] != key and x[0] not in {k for k, _ in eigene_gemeinde}]}
               for g in D.vereine_gruppiert()]
    return render_template("einstellungen.html", gemeinde=gem, landkreis=lk, eigene_gemeinde=eigene_gemeinde,
                           gruppen=[g for g in gruppen if g["vereine"]], dazu=dazu, ohne=ohne, labels=labels,
                           meldung=request.args.get("meldung", ""))


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
    # Ergebnisse gehören zur Runde (kein eigenes Archiv mehr, Josef 2026-10-05). Auch Runden, aus denen der Verein
    # nach dem Abschluss ausgetreten ist, bleiben mit ihrem Ergebnis sichtbar.
    ergebnisse = defaultdict(list)
    for e in db.ergebnisse_des_vereins(key):
        ergebnisse[e["runde_id"]].append(e)
    aktiv = db.runden_des_vereins(key)
    nur_ergebnis = [db.runde(rid) for rid in ergebnisse if rid not in {r["id"] for r in aktiv}]
    return render_template("runden.html", runden=aktiv, nur_ergebnis=[r for r in nur_ergebnis if r],
                           ergebnisse=ergebnisse, darf=db.freigegeben(key),
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
                                meldung=f"Runde abgeschlossen. Das Ergebnis (Version {version}) steht jetzt bei jedem beteiligten Verein unter „Planungsrunden“."))
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
def archiv():
    """Früheres eigenes Archiv – die Ergebnisse stehen jetzt bei der Runde unter „Planungsrunden“."""
    return redirect(url_for("runden_seite"))


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
