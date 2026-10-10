"""Tab „Dokumente“ im Vereinsbereich (ADR-029, v1.78): Protokolle, Satzung, Sonstiges.

Hängt am `planung_bp` (gleicher Kopf, gleiche Anmeldung, CSRF und Fehlerseiten wie Termine · Planungsrunden ·
Einstellungen). Vereinsadmins legen an, ändern und löschen; Mitglieder lesen und laden herunter. Jede Abfrage
läuft über den Verein aus der Sitzung – fremde Dokumente ergeben 404.

Zwei Wege, ein Dokument anzulegen (einfache Abläufe): **Datei hochladen** (Formular aufklappen auf der Liste) oder
**Schreiben** (`/verein/dokumente/neu`, bei Protokollen mit Sitzungsangaben und Tagesordnungspunkten).
"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from flask import Response, abort, g, redirect, render_template, request, send_file, url_for

from services.verein.planung import _ist_admin, _nur_admin, planung_bp, verein_login
from shared import dokumente_db as D
from shared import dokumente_store as S
from shared.dokument_export import FORMATE as DOK_FORMATE, abstimmung, datum_lang, exportiere, kopfzeilen
from shared.export import dateiname
from shared.termin_felder import datum_ok
from shared.vk_db import log_audit

TITEL_MAX = 200
FELD_MAX = 500           # Ort, Leitung, Anwesende …
TEXT_MAX = 20000         # Freitext und Text je Tagesordnungspunkt
MAX_TOPS = 40
PROTOKOLL_FELDER = ("sitzungsart", "ort", "beginn", "ende", "leitung", "protokoll", "anwesende", "entschuldigt")
NEU = "__neu"            # Auswahlwert „+ Neue Kategorie …“ / „+ Neue Sitzungsart …“ (v1.82)
MINUTEN = ("00", "15", "30", "45")   # Uhrzeit in 15-Minuten-Schritten (v1.82, iPhone-Rad ignoriert `step`)


def _audit(aktion: str, did: int, key: str) -> None:
    try:
        log_audit(aktion, f"dok_{did}", key, g.user["id"])
    except Exception:
        pass


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f}".replace(".", ",") + " MB" if n >= 100 * 1024 else f"{max(1, round(n / 1024))} KB"


planung_bp.add_app_template_filter(_mb, "mb")
planung_bp.add_app_template_filter(datum_lang, "datum_lang")


def _zurueck(**kw):
    return redirect(url_for("planung.dokumente_seite", **{k: v for k, v in kw.items() if v}))


def _kopf_aus_formular(key: str) -> tuple[dict, str]:
    f = {"kategorie": request.form.get("kategorie", ""), "titel": request.form.get("titel", "").strip(),
         "datum": request.form.get("datum", "").strip(),
         "neu_kategorie": " ".join(request.form.get("neu_kategorie", "").split())}
    if f["kategorie"] == NEU:
        if not f["neu_kategorie"]:
            return f, "Bitte einen Namen für die neue Kategorie eingeben."
        if len(f["neu_kategorie"]) > D.NAME_MAX:
            return f, f"Name der Kategorie höchstens {D.NAME_MAX} Zeichen."
    elif f["kategorie"] not in D.kategorien(key):
        return f, "Bitte eine Kategorie wählen."
    if f["datum"] and not datum_ok(f["datum"]):
        return f, "Bitte ein gültiges Datum angeben."
    if len(f["titel"]) > TITEL_MAX:
        return f, f"Titel höchstens {TITEL_MAX} Zeichen."
    return f, ""


def _kat_aufloesen(key: str, kopf: dict) -> str:
    """„+ Neue Kategorie …“ erst beim Speichern anlegen (sonst blieben bei Fehlern leere Kategorien zurück)."""
    if kopf["kategorie"] != NEU:
        return kopf["kategorie"]
    eid = D.eintrag_neu(key, "kategorie", kopf["neu_kategorie"])
    if eid is None:                       # Name einer festen Kategorie → diese nehmen
        return next((k for k, n in D.KATEGORIEN.items() if n.casefold() == kopf["neu_kategorie"].casefold()),
                    "sonstiges")
    return f"k{eid}"


def _uhrzeit(feld: str) -> tuple[str, str]:
    """Stunde + Minute aus zwei Auswahlfeldern (`beginn_h`, `beginn_m`); älteres Einzelfeld `beginn` geht weiter."""
    h, m = request.form.get(f"{feld}_h", "").strip(), request.form.get(f"{feld}_m", "").strip()
    if not h and not m:
        alt = request.form.get(feld, "").strip()
        h, _, m = alt.partition(":")
        if not alt:
            return "", ""
    if not (h.isdigit() and len(h) == 2 and int(h) < 24) or not (m.isdigit() and len(m) == 2 and int(m) < 60):
        return "", "Bitte bei Beginn und Ende Stunde und Minute wählen."
    return f"{h}:{m}", ""


def _inhalt_aus_formular(kategorie: str) -> tuple[dict, str]:
    i = {"text": request.form.get("text", "").strip()}
    if len(i["text"]) > TEXT_MAX:
        return i, f"Text höchstens {TEXT_MAX} Zeichen."
    if kategorie != "protokoll":
        return i, ""
    for k in PROTOKOLL_FELDER:
        if k in ("beginn", "ende"):
            i[k], fehler = _uhrzeit(k)
            if fehler:
                return i, fehler
            continue
        i[k] = request.form.get(k, "").strip()
        if len(i[k]) > FELD_MAX:
            return i, f"Angaben zur Sitzung höchstens {FELD_MAX} Zeichen je Feld."
    if i["sitzungsart"] == NEU:
        i["sitzungsart"] = " ".join(request.form.get("neu_sitzungsart", "").split())[:D.NAME_MAX]
        if not i["sitzungsart"]:
            return i, "Bitte einen Namen für die neue Art der Sitzung eingeben."
        i["_neu_sitzungsart"] = True
    spalten = {k: request.form.getlist(f"top_{k}") for k in ("titel", "text", "beschluss", "ja", "nein", "enthaltung")}
    tops = []
    for n in range(len(spalten["titel"])):
        top = {k: (v[n].strip() if n < len(v) else "") for k, v in spalten.items()}
        if not any(top.values()):
            continue
        for k in ("ja", "nein", "enthaltung"):
            if top[k] and not (top[k].isdigit() and len(top[k]) <= 5):
                return i, "Bei der Abstimmung bitte nur Zahlen eintragen."
        if len(top["titel"]) > FELD_MAX or len(top["text"]) > TEXT_MAX or len(top["beschluss"]) > TEXT_MAX:
            return i, "Ein Tagesordnungspunkt ist zu lang."
        tops.append(top)
    if len(tops) > MAX_TOPS:
        return i, f"Höchstens {MAX_TOPS} Tagesordnungspunkte."
    i["tops"] = tops
    return i, ""


def _datei_aus_formular(key: str, ersetzt: int = 0) -> tuple[dict | None, str]:
    f = request.files.get("datei")
    if not f or not f.filename:
        return None, ""
    data = f.read(S.MAX_BYTES + 1)
    try:
        ext = S.pruefe(data, D.belegung(key), ersetzt)
    except ValueError as e:
        return None, str(e)
    return {"name": S.sicherer_name(f.filename, ext), "typ": ext, "groesse": len(data), "_data": data}, ""


def _speichere(datei: dict) -> dict:
    datei["pfad"] = S.speichern(datei.pop("_data"), datei["typ"])
    return datei


def _dokument(key: str, did: int) -> dict:
    dok = D.hole(did, key)
    if not dok:
        abort(404, "Dieses Dokument gibt es nicht (mehr).")
    return dok


# ── Liste + Hochladen ────────────────────────────────────────────────────────

@planung_bp.route("/verein/dokumente", methods=["GET", "POST"])
@verein_login
def dokumente_seite(key):
    if request.method == "POST":       # Datei hochladen
        _nur_admin()
        kopf, fehler = _kopf_aus_formular(key)
        datei, fehler2 = (None, "") if fehler else _datei_aus_formular(key)
        fehler = fehler or fehler2 or ("" if datei else "Bitte eine Datei auswählen.")
        if fehler:
            return redirect(url_for("planung.dokumente_seite", fehler=fehler) + "#hochladen")
        datei = _speichere(datei)
        titel = kopf["titel"] or datei["name"].rsplit(".", 1)[0]
        did = D.neu(key, g.user["id"], _kat_aufloesen(key, kopf), titel, kopf["datum"], "datei", datei=datei)
        _audit("dokument_neu", did, key)
        return _zurueck(meldung=f"„{titel}“ ist abgelegt.")
    suche = request.args.get("q", "").strip()[:100]
    alle = D.liste(key, suche)
    gruppen = []
    kats = D.kategorien(key)
    for kat, name in kats.items():
        jahre: dict[str, list] = {}
        for d in (d for d in alle if d["kategorie"] == kat):
            jahre.setdefault((d["datum"] or d["erstellt_am"])[:4], []).append(d)
        gruppen.append({"kat": kat, "name": name, "anzahl": sum(len(v) for v in jahre.values()),
                        "jahre": list(jahre.items())})
    belegt = D.belegung(key)
    return render_template("planung/dokumente.html", gruppen=gruppen, suche=suche, anzahl=len(alle),
                           KATEGORIEN=kats, TYPEN=S.TYPEN, erlaubt=S.ERLAUBT_TEXT, NEU=NEU,
                           belegt=belegt, max_verein=S.MAX_VEREIN, max_datei=S.MAX_BYTES,
                           voll=belegt >= S.MAX_VEREIN, neu_kat=request.args.get("kat", "protokoll"),
                           fehler=request.args.get("fehler", ""), meldung=request.args.get("meldung", ""))


# ── Schreiben ────────────────────────────────────────────────────────────────

def _formular_seite(key: str, dok: dict, fehler: str = ""):
    return render_template("planung/dokument_formular.html", dok=dok, fehler=fehler, KATEGORIEN=D.kategorien(key),
                           EINZAHL=D.einzahl(key), SITZUNGSARTEN=D.sitzungsarten(key), TYPEN=S.TYPEN,
                           erlaubt=S.ERLAUBT_TEXT, NEU=NEU, STUNDEN=[f"{h:02d}" for h in range(24)], MINUTEN=MINUTEN)


def _leer(key: str, kategorie: str) -> dict:
    return {"id": None, "art": "formular", "kategorie": kategorie if kategorie in D.kategorien(key) else "protokoll",
            "titel": "", "datum": datetime.now().strftime("%Y-%m-%d"), "inhalt": {"tops": []}}


def _sitzungsart_merken(key: str, inhalt: dict) -> None:
    if inhalt.pop("_neu_sitzungsart", False):
        D.eintrag_neu(key, "sitzungsart", inhalt["sitzungsart"])


@planung_bp.route("/verein/dokumente/neu", methods=["GET", "POST"])
@verein_login
def dokument_neu(key):
    _nur_admin()
    if request.method == "GET":
        return _formular_seite(key, _leer(key, request.args.get("kat", "protokoll")))
    kopf, fehler = _kopf_aus_formular(key)
    inhalt, fehler2 = _inhalt_aus_formular(kopf["kategorie"])
    dok = {**_leer(key, kopf["kategorie"]), **kopf, "inhalt": inhalt}
    fehler = fehler or fehler2
    if not fehler and kopf["kategorie"] == "protokoll" and not kopf["datum"]:
        fehler = "Bitte das Datum der Sitzung angeben."
    if fehler:
        inhalt.pop("_neu_sitzungsart", None)
        return _formular_seite(key, dok, fehler), 400
    kat = _kat_aufloesen(key, kopf)
    _sitzungsart_merken(key, inhalt)
    titel = kopf["titel"] or _standardtitel(key, kat, inhalt, kopf["datum"])
    did = D.neu(key, g.user["id"], kat, titel, kopf["datum"], "formular", inhalt=inhalt)
    _audit("dokument_neu", did, key)
    return redirect(url_for("planung.dokument_seite", did=did, meldung="Gespeichert."))


def _standardtitel(key: str, kategorie: str, inhalt: dict, datum: str) -> str:
    if kategorie == "protokoll":
        return f"Protokoll {inhalt.get('sitzungsart') or 'Sitzung'} {datum_lang(datum)}".strip()
    return D.einzahl(key).get(kategorie, "Dokument") + (f" {datum_lang(datum)}" if datum else "")


# ── Ansehen, Ändern, Löschen ─────────────────────────────────────────────────

@planung_bp.route("/verein/dokumente/<int:did>", methods=["GET", "POST"])
@verein_login
def dokument_seite(key, did):
    dok = _dokument(key, did)
    if request.method == "POST":
        _nur_admin()
        kopf, fehler = _kopf_aus_formular(key)
        if dok["art"] == "formular":
            inhalt, fehler2 = _inhalt_aus_formular(kopf["kategorie"])
            if not (fehler or fehler2) and kopf["kategorie"] == "protokoll" and not kopf["datum"]:
                fehler2 = "Bitte das Datum der Sitzung angeben."
            if fehler or fehler2:
                inhalt.pop("_neu_sitzungsart", None)
                return _formular_seite(key, {**dok, **kopf, "inhalt": inhalt}, fehler or fehler2), 400
            kat = _kat_aufloesen(key, kopf)
            _sitzungsart_merken(key, inhalt)
            titel = kopf["titel"] or _standardtitel(key, kat, inhalt, kopf["datum"])
            D.aendern(did, key, g.user["id"], kat, titel, kopf["datum"], inhalt=inhalt)
        else:
            datei, fehler2 = (None, "") if fehler else _datei_aus_formular(key, ersetzt=dok["groesse"])
            if fehler or fehler2:
                return redirect(url_for("planung.dokument_seite", did=did, fehler=fehler or fehler2) + "#aendern")
            if datei:
                datei = _speichere(datei)
            titel = kopf["titel"] or dok["titel"]
            D.aendern(did, key, g.user["id"], _kat_aufloesen(key, kopf), titel, kopf["datum"], datei=datei)
            if datei:
                S.entfernen(dok["datei_pfad"])
        _audit("dokument_geaendert", did, key)
        return redirect(url_for("planung.dokument_seite", did=did, meldung="Gespeichert."))
    if dok["art"] == "formular" and request.args.get("bearbeiten") and _ist_admin():
        return _formular_seite(key, dok)
    return render_template("planung/dokument.html", dok=dok, KATEGORIEN=D.kategorien(key), EINZAHL=D.einzahl(key), NEU=NEU,
                           TYPEN=S.TYPEN, IM_BROWSER=S.IM_BROWSER, DOK_FORMATE=DOK_FORMATE, erlaubt=S.ERLAUBT_TEXT,
                           kopf=kopfzeilen(dok, g.user["verein_name"]) if dok["art"] == "formular" else [],
                           abstimmung=abstimmung, max_datei=S.MAX_BYTES,
                           fehler=request.args.get("fehler", ""), meldung=request.args.get("meldung", ""))


@planung_bp.route("/verein/dokumente/<int:did>/loeschen", methods=["POST"])
@verein_login
def dokument_loeschen(key, did):
    _nur_admin()
    dok = D.loeschen(did, key)
    if not dok:
        abort(404, "Dieses Dokument gibt es nicht (mehr).")
    S.entfernen(dok["datei_pfad"])
    _audit("dokument_geloescht", did, key)
    return _zurueck(meldung=f"„{dok['titel']}“ ist gelöscht.")


@planung_bp.route("/verein/dokumente/<int:did>/datei")
@verein_login
def dokument_datei(key, did):
    dok = _dokument(key, did)
    p = S.pfad(dok["datei_pfad"]) if dok["art"] == "datei" else None
    if not p:
        abort(404, "Die Datei fehlt.")
    herunterladen = request.args.get("laden") == "1" or dok["datei_typ"] not in S.IM_BROWSER
    r = send_file(p, mimetype=S.TYPEN[dok["datei_typ"]][1], as_attachment=herunterladen,
                  download_name=dok["datei_name"], conditional=False, etag=False, max_age=0)
    r.headers["Cache-Control"] = "private, no-store"
    r.headers["X-Content-Type-Options"] = "nosniff"
    return r


@planung_bp.route("/verein/dokumente/<int:did>.<fmt>")
@verein_login
def dokument_export(key, did, fmt):
    dok = _dokument(key, did)
    if dok["art"] != "formular" or fmt not in DOK_FORMATE:
        abort(404)
    try:
        inhalt = exportiere(fmt, dok, g.user["verein_name"])
    except ImportError:
        abort(404, "Dieses Format steht gerade nicht zur Verfügung.")
    name = dateiname(dok["titel"], fmt)
    ersatz = name.encode("ascii", "replace").decode().replace("?", "_")
    return Response(inhalt, mimetype=DOK_FORMATE[fmt][1],
                    headers={"Content-Disposition": f"attachment; filename=\"{ersatz}\"; filename*=UTF-8''{quote(name)}",
                             "Cache-Control": "private, no-store"})


# ── Eigene Kategorien und Sitzungsarten verwalten (v1.82) ────────────────────

@planung_bp.route("/verein/dokumente/listen", methods=["GET", "POST"])
@verein_login
def dokument_listen(key):
    _nur_admin()
    if request.method == "POST":
        try:
            eid = int(request.form.get("id", ""))
        except ValueError:
            abort(400)
        if request.form.get("aktion") == "loeschen":
            if not D.eintrag_loeschen(key, eid):
                n = D.eintrag_belegt(key, eid)
                return redirect(url_for("planung.dokument_listen", fehler=(
                    f"Die Kategorie enthält noch {n} {'Dokument' if n == 1 else 'Dokumente'} – erst verschieben oder löschen."
                    if n else "Diesen Eintrag gibt es nicht (mehr).")))
            return redirect(url_for("planung.dokument_listen", meldung="Gelöscht."))
        if not D.eintrag_umbenennen(key, eid, request.form.get("name", "")):
            return redirect(url_for("planung.dokument_listen", fehler="Bitte einen Namen eingeben."))
        return redirect(url_for("planung.dokument_listen", meldung="Umbenannt."))
    kats = [{**e, "belegt": D.eintrag_belegt(key, e["id"])} for e in D.eintraege(key, "kategorie")]
    return render_template("planung/dokument_listen.html", kategorien=kats, sitzungsarten=D.eintraege(key, "sitzungsart"),
                           FEST_KAT=list(D.KATEGORIEN.values()), FEST_SITZ=list(D.SITZUNGSARTEN), NAME_MAX=D.NAME_MAX,
                           fehler=request.args.get("fehler", ""), meldung=request.args.get("meldung", ""))
