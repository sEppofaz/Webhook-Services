"""Vereinsbereich mit Tabs Termine · Planungsrunden · Einstellungen (ADR-026, live seit v1.77).

Ersetzt das frühere Dashboard (`/verein/dashboard` leitet hierher um):
- **Termine** (Startseite nach dem Login): oben „Noch nicht veröffentlicht“ (Entwürfe), darunter „Im Kalender“.
  Neuer Termin über das bekannte Formular (`/verein/termine/neu`) mit „Veröffentlichen“ oder „Als Entwurf
  speichern“ und Kollisionswarnung beim Tippen. Vorjahres-Vorlage als Karte. Terminplan-Upload und Hilfe wie bisher.
- **Planungsrunden:** jeder Vereinsadmin startet eine (Organisator) und lädt per Link oder Code ein; Teilnehmer sehen
  alle Entwürfe mit Konflikten und ändern/bestätigen ihre eigenen. Verlauf, Abschluss mit Ergebnis-PDF.
- **Einstellungen:** Vereinsprofil, Mitglieder, Passwort (bestehende Seiten), „Wer trägt eure Termine ein?“
  (`_meta.crawler_aus`, ADR-027), Prüfkreis für Überschneidungen, Rechtliches.

Nur der Vereinsadmin ändert und veröffentlicht; Mitglieder sehen alles nur lesend.
Grundsatz: einfache Abläufe (`PKA/BKM/PWA-Standards.md`) – jede Info nur einmal, keine internen Begriffe.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from collections import defaultdict
from datetime import date, datetime
from functools import wraps

from flask import (Blueprint, Response, abort, g, jsonify, make_response, redirect, render_template, request,
                   session, url_for)

from services.auth.routes import require_verein_login
from shared import planung_db as P
from shared.csrf import csrf_field, get_csrf_token, validate_csrf
from shared.export import FORMATE, dateiname, datum_text, ergebnis_pdf, exportiere, zeit_text
from shared.geo import _gem_norm
from shared.kalender_store import KalenderStore
from shared.kollision import STUFE_TAG, ist_regelgottesdienst, kollisionen
from shared.termin_felder import BESCHREIBUNG_MAX, datum_ok, zeit_fehler
from shared.vk_db import AVV_FASSUNG, db_conn, get_upload_count, log_audit
from shared.wiederholung import MONATE, vorlage

planung_bp = Blueprint("planung", __name__, template_folder="templates")

TEXT_MAX = 200
MAX_TAGE = 16          # wie das Termin-Formular (ADR-017)
UPLOAD_LIMIT = 3       # wie services/verein/routes.py
WEITER_COOKIE = "vk_weiter"   # Einladungslink vor dem Login geöffnet → nach dem Login dorthin


# ── Öffentliche Termine (dieselbe Sicht wie /api/termine), zwischengespeichert ──

_cache_lock = threading.Lock()
_cache: dict = {"schluessel": None, "daten": None, "vorschlaege": {}}


def daten() -> dict:
    """{labels, termine, meta, rubriken} – neu berechnet, wenn sich `vereinstermine.json` geändert hat, sonst
    höchstens jede Minute (Gottesdienste kommen aus einer eigenen Datei)."""
    import services.kalender.routes as K
    try:
        mtime = K.VEREINSTERMINE_FILE.stat().st_mtime
    except OSError:
        mtime = 0
    schluessel = (mtime, int(time.time() // 60))
    with _cache_lock:
        if _cache["schluessel"] != schluessel:
            _cache.update(schluessel=schluessel, daten=K.oeffentliche_termine(), vorschlaege={})
        return _cache["daten"]


def cache_leeren() -> None:
    """Nach eigenen Schreibzugriffen (Veröffentlichen) – die mtime ändert sich in derselben Sekunde evtl. nicht sichtbar."""
    with _cache_lock:
        _cache["schluessel"] = None


def labels() -> dict:
    return daten()["labels"]


def vereinsname(key: str) -> str:
    n = labels().get(key)
    if n:
        return n
    with db_conn() as c:
        r = c.execute("SELECT verein_name FROM vereine_accounts WHERE verein_key = ?", (key,)).fetchone()
    return r["verein_name"] if r else key


def daten_ab() -> date | None:
    """Erster erfasster Termin (2026: Mai) – Serien bekommen die Monate davor ergänzt."""
    tage = [t.get("datum", "") for t in daten()["termine"] if t.get("datum")]
    try:
        return date.fromisoformat(min(tage)) if tage else None
    except ValueError:
        return None


def vorschlaege(zieljahr: int) -> list[dict]:
    """Vorjahres-Vorlage für alle Vereine (je Jahr und Datenstand zwischengespeichert). Pfarrbrief-Gottesdienste nie."""
    d = daten()
    with _cache_lock:
        if zieljahr in _cache["vorschlaege"]:
            return _cache["vorschlaege"][zieljahr]
    v = vorlage(d["termine"], zieljahr, ausschliessen=lambda t: ist_regelgottesdienst(t, d["meta"].get(t.get("verein", ""))),
                daten_ab=daten_ab())
    with _cache_lock:
        _cache["vorschlaege"][zieljahr] = v
    return v


def sitz(key: str) -> tuple[str, str]:
    """(Gemeinde ohne Präfix, Landkreis) des Vereinssitzes – aus dem Kalender, sonst aus dem Vereinskonto."""
    m = daten()["meta"].get(key) or {}
    gem, lk = m.get("ortschaft_gemeinde") or m.get("gemeinde") or "", m.get("landkreis") or ""
    if not gem:
        with db_conn() as c:
            r = c.execute("SELECT gemeinde, landkreis FROM vereine_accounts WHERE verein_key = ?", (key,)).fetchone()
        if r:
            gem, lk = r["gemeinde"] or "", lk or r["landkreis"] or ""
    return _gem_norm(gem), lk or "Landkreis Landshut"


def vereine_gruppiert() -> list[dict]:
    """Vereine nach Gemeinde des Sitzes: [{gemeinde, landkreis, vereine:[(key, Name)]}], „Ohne Gemeinde“ am Ende."""
    gruppen: dict = {}
    for k, name in sorted(labels().items(), key=lambda x: x[1].lower()):
        gem, lk = sitz(k)
        gruppen.setdefault((gem or "", lk if gem else ""), []).append((k, name))
    reihenfolge = sorted(gruppen, key=lambda x: (not x[0], x[0].lower(), x[1]))
    return [{"gemeinde": gm or "Ohne Gemeinde", "landkreis": lk, "vereine": gruppen[(gm, lk)]} for gm, lk in reihenfolge]


def vereine_der_gemeinde(gemeinde: str, landkreis: str) -> list[tuple[str, str]]:
    return sorted(((k, n) for k, n in labels().items() if sitz(k) == (gemeinde, landkreis)), key=lambda x: x[1].lower())


# Kollisionswarnung in jedem Formular mit Klasse „kollision-pruefen“ (Neuer Termin, Bearbeiten, Entwurf ändern) –
# kein eigener Tab, der Hinweis kommt beim Anlegen (Josef 2026-10-05). Blockiert nie; Prüfkreis aus den Einstellungen.
# Eine Quelle für den neuen Vereinsbereich (base.html) und die bestehenden Formularseiten (_page, immer dunkel):
# Farben über die Variablen des Vereinsbereichs, dunkle Werte als Rückfall.
KOLLISION_JS = """<style>
.kollision-warn{background:var(--gelb-bg,#422006);color:var(--gelb,#fbbf24);border-radius:8px;padding:10px 12px;margin:10px 0;font-size:14px}
.kollision-warn .kollision-fuss{margin-top:6px;font-size:13px;opacity:.85}
</style>
<script>
(function () {
  const abruf = window.vkoAbruf || (u => fetch(u, { credentials: "same-origin", cache: "no-store" }).catch(() => null));
  for (const form of document.querySelectorAll("form.kollision-pruefen")) {
    const box = form.querySelector(".kollision-hinweis");
    if (!box) continue;
    const feld = n => form.querySelector(`[name=${n}]`);
    let timer = null, nr = 0;
    async function pruefen() {
      const von = feld("datum") && feld("datum").value;
      if (!von) { box.replaceChildren(); return; }
      const q = new URLSearchParams({ von, bis: (feld("datum_bis") && feld("datum_bis").value) || von,
        ort: feld("ort") ? feld("ort").value : "", uhrzeit: feld("uhrzeit") ? feld("uhrzeit").value : "",
        ohne_id: form.dataset.terminId || "" });
      const meine = ++nr;
      const r = await abruf("/verein/kollisionen?" + q);
      if (!r || !r.ok || meine !== nr) return;
      const liste = await r.json();
      box.replaceChildren();
      if (!liste.length) return;
      const div = document.createElement("div");
      div.className = "kollision-warn";
      const kopf = document.createElement("b");
      kopf.textContent = "Am selben Tag schon geplant:";
      const ul = document.createElement("ul");
      ul.style.cssText = "margin:6px 0 0;padding-left:18px";
      for (const x of liste) {   // nur textContent – Titel kommen von anderen Vereinen
        const li = document.createElement("li");
        li.textContent = `${x.datum_text}${x.uhrzeit ? ", " + x.uhrzeit : ""} – ${x.bezeichnung} (${x.verein_name})`
          + (x.nachbar ? " · Nachbarschaft" : "");
        ul.append(li);
      }
      const fuss = document.createElement("div");
      fuss.className = "kollision-fuss";
      fuss.textContent = "Nur ein Hinweis – speichern geht trotzdem. Welche Vereine geprüft werden: Einstellungen.";
      div.append(kopf, ul, fuss);
      box.append(div);
    }
    const anstossen = (e) => {
      if (!["datum", "datum_bis", "uhrzeit", "ort"].includes(e.target.name)) return;
      clearTimeout(timer); timer = setTimeout(pruefen, 400);
    };
    form.addEventListener("input", anstossen);
    form.addEventListener("change", anstossen);
    // Bearbeiten: gleich prüfen – im zugeklappten Formular erst beim Aufklappen
    if (form.dataset.sofort) {
      const klappe = form.closest(".klappe");
      if (klappe && klappe.hidden) klappe.addEventListener("aufgeklappt", pruefen, { once: true });
      else pruefen();
    }
  }
})();
</script>"""


# ── Anmeldung, Rechte, Fehler ────────────────────────────────────────────────

def verein_login(f):
    """Vereins-Login wie überall (`require_verein_login`: nur bestätigte, freigegebene Konten) + Benutzer in `g`."""
    @require_verein_login
    @wraps(f)
    def wrapper(*a, user, **kw):
        if not user.get("verein_key"):
            abort(403, "Diesem Konto ist noch kein Verein zugeordnet.")
        g.user = user
        return f(user["verein_key"], *a, **kw)
    return wrapper


def _ist_admin() -> bool:
    return bool(getattr(g, "user", None)) and g.user["role"] == "admin"


def _nur_admin() -> None:
    if not _ist_admin():
        abort(403, "Das dürfen nur Vereinsadmins.")


@planung_bp.context_processor
def _vorlagen_hilfen():
    user = getattr(g, "user", None)
    return {"csrf": lambda: csrf_field(get_csrf_token()), "FORMATE": FORMATE, "STATUS": P.STATUS,
            "datum_text": datum_text, "zeit_text": zeit_text, "STUFE_TAG": STUFE_TAG,
            "angemeldet": user["verein_key"] if user else None, "angemeldet_name": user["verein_name"] if user else "",
            "darf": _ist_admin(), "kollision_js": KOLLISION_JS,
            # Dokumentenbereich freigeschaltet (AV-Vertrag in aktueller Fassung, v1.83) + einmaliger Hinweis
            "avv_frei": bool(user) and user.get("avv_fassung") == AVV_FASSUNG,
            "hinweis_dokumente": bool(user) and _ist_admin() and user.get("avv_fassung") != AVV_FASSUNG
                                 and not user.get("hinweis_dokumente")}


@planung_bp.before_request
def _csrf_pruefen():
    if request.method == "POST" and not validate_csrf():
        # Meist eine veraltete Seite (vor Neustart/Abmeldung geladen) – zurück mit Hinweis statt nackter 403
        ziel = request.referrer if (request.referrer or "").startswith(request.host_url) else "/verein/termine"
        trenner = "&" if "?" in ziel else "?"
        return redirect(f"{ziel.split('#')[0]}{trenner}fehler=Die+Seite+war+veraltet+%E2%80%93+bitte+noch+einmal.")


@planung_bp.errorhandler(400)
@planung_bp.errorhandler(403)
@planung_bp.errorhandler(404)
@planung_bp.errorhandler(429)
def _fehlerseite(e):
    """Fehler auf Deutsch, mit Weg zurück (statt Werkzeug-Standardseite „Forbidden“)."""
    titel = {400: "Ungültige Eingabe", 403: "Nicht erlaubt", 404: "Nicht gefunden", 429: "Zu viele Versuche"}.get(e.code, "Fehler")
    text = e.description if e.description and not e.description.startswith(("The ", "You ", "Bad ", "The server")) else ""
    return render_template("planung/fehler.html", titel=titel, text=text), e.code


# ── Hilfen ───────────────────────────────────────────────────────────────────

def felder_aus_formular() -> tuple[dict, str]:
    f = {k: request.form.get(k, "").strip() for k in ("datum", "uhrzeit", "uhrzeit_bis", "bezeichnung", "ort", "beschreibung")}
    if not datum_ok(f["datum"]):
        return f, "Bitte ein gültiges Datum angeben."
    if not f["bezeichnung"]:
        return f, "Bitte eine Bezeichnung angeben."
    if len(f["bezeichnung"]) > TEXT_MAX or len(f["ort"]) > TEXT_MAX:
        return f, f"Bezeichnung und Ort höchstens {TEXT_MAX} Zeichen."
    if len(f["beschreibung"]) > BESCHREIBUNG_MAX:
        return f, f"Beschreibung höchstens {BESCHREIBUNG_MAX} Zeichen."
    return f, zeit_fehler(f["uhrzeit"], f["uhrzeit_bis"])


def _kalender_im_jahr(jahr: int) -> list[dict]:
    return [t for t in daten()["termine"] if t.get("datum", "")[:4] == str(jahr)]


def _mit_konflikten(eigene: list[dict], andere: list[dict], zusatz: set | None = None,
                    ohne: set | None = None) -> list[dict]:
    """Jedem Termin seine Kollisionen anhängen (`_konflikte`); Quelle Entwurf/Kalender markiert."""
    d = daten()
    entwurf_ids = {x["id"] for x in andere if str(x.get("id", "")).startswith("e") and x.get("status") in ("entwurf", "bestaetigt")}
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
    zeilen = [{**z, "verein_name": z.get("verein_name") or vereinsname(z.get("verein", ""))} for z in zeilen]
    try:
        inhalt, mime, _ = exportiere(fmt, titel, zeilen, mit_verein)
    except ImportError:
        abort(404, "Dieses Format steht gerade nicht zur Verfügung.")
    return Response(inhalt, mimetype=mime,
                    headers={"Content-Disposition": f'attachment; filename="{dateiname(titel, fmt)}"'})


def _jahr_filter() -> int | None:
    """Jahr-Filter der Termin-Seite: leer = „ab heute“ über alle Jahre."""
    try:
        j = int(request.values.get("jahr", ""))
        return j if 2020 <= j <= 2040 else None
    except ValueError:
        return None


def _zieljahr() -> int:
    j = _jahr_filter()
    return j or date.today().year + 1


def _wt(datum: str) -> str:
    try:
        return ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")[date.fromisoformat(datum).weekday()]
    except (ValueError, TypeError):
        return ""


def _tm(datum: str) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", datum or "")
    return f"{m[3]}.{m[2]}." if m else (datum or "")


planung_bp.add_app_template_filter(_wt, "wochentag_kurz")
planung_bp.add_app_template_filter(_tm, "tm")


def kurz(datum: str) -> str:
    return f"{_wt(datum)} {_tm(datum)}"


def verlauf(key: str, jahr: int, aktion: str, details: str = "") -> None:
    """Terminänderungen im Verlauf aller offenen Runden des Vereins für dieses Jahr festhalten –
    egal ob in der Runde, auf „Termine“ oder im Termin-Formular geändert."""
    for rid in P.offene_runden(key, jahr):
        P.protokoll(rid, key, aktion, details)


def runde_aus_formular() -> int | None:
    """Feld/Parameter `runde`: nur wenn der angemeldete Verein dort aktiv dabei ist (Rücksprung)."""
    rid = request.values.get("runde", "")
    user = getattr(g, "user", None)
    if rid.isdigit() and user and user["verein_key"] in P.aktive_teilnehmer(int(rid)):
        return int(rid)
    return None


def zurueck(anker: str = "", **kw):
    """Zurück zur Runde, aus der die Aktion kam, sonst zu „Termine“ (mit dem Jahr-Filter, aus dem sie kam)."""
    rid = runde_aus_formular()
    if rid:
        return redirect(url_for("planung.runde_seite", runde_id=rid, **kw) + (f"#{anker}" if anker else ""))
    ansicht = request.values.get("ansicht_jahr", "")
    if ansicht.isdigit():
        kw["jahr"] = ansicht
    return redirect(url_for("planung.termine_seite", **kw) + (f"#{anker}" if anker else ""))


# ── Veröffentlichen: Entwurf → Kalender ──────────────────────────────────────

def veroeffentlichen(user: dict, eids: list[int]) -> int:
    """Entwürfe in den Kalender schreiben (wie „Neuer Termin“): eigene ID, `erstellt_von`, Verein selbstverwaltet.
    Steht beim Verein schon ein Termin mit gleichem Tag, Uhrzeit und Titel, wird nicht doppelt eingetragen – der
    Entwurf zeigt dann auf diesen Termin. Erst reservieren, dann schreiben: kein Doppel bei Doppelklick."""
    key = user["verein_key"]
    liste = P.reservieren(key, eids)
    if not liste:
        return 0
    jetzt_utc = datetime.utcnow().isoformat()[:19]
    zuordnung: dict[int, str] = {}
    neu: list[str] = []

    def mutator(data):
        zuordnung.clear()
        neu.clear()
        if key not in data:
            data[key] = []
            data.setdefault("_labels", {})[key] = user["verein_name"]
        vorhanden = {(t.get("datum"), t.get("uhrzeit", ""), t.get("bezeichnung")): t.get("id")
                     for t in data[key] if not t.get("geloescht") and not t.get("deleted") and t.get("id")}
        for e in liste:
            schluessel = (e["datum"], e["uhrzeit"], e["bezeichnung"])
            if schluessel in vorhanden:
                zuordnung[e["_eid"]] = vorhanden[schluessel]
                continue
            t = {"id": str(uuid.uuid4())[:8], "datum": e["datum"], "uhrzeit": e["uhrzeit"],
                 "bezeichnung": e["bezeichnung"], "ort": e["ort"], "erstellt_von": user["email"],
                 "erstellt_am": jetzt_utc}
            if e["uhrzeit_bis"]:
                t["uhrzeit_bis"] = e["uhrzeit_bis"]
            if e["beschreibung"]:
                t["beschreibung"] = e["beschreibung"]
            data[key].append(t)
            vorhanden[schluessel] = t["id"]
            zuordnung[e["_eid"]] = t["id"]
            neu.append(t["id"])
        data[key].sort(key=lambda x: (x.get("datum", ""), x.get("uhrzeit", "")))
        data.setdefault("_meta", {}).setdefault(key, {})["selbstverwaltung"] = True
        return data

    try:
        KalenderStore.update(mutator)
    except Exception:
        P.reservierung_aufheben([e["_eid"] for e in liste])
        raise
    for eid, tid in zuordnung.items():
        P.termin_id_setzen(eid, tid)
    for tid in neu:
        log_audit("erstellt", tid, key, user["id"])
    cache_leeren()
    return len(liste)


# ── Termine (Startseite) ─────────────────────────────────────────────────────

def kalender_termine(key: str) -> list[dict]:
    """Termine des Vereins, die im Kalender stehen (ohne gelöschte)."""
    return [{**t, "status": "kalender", "_eid": f"k{t.get('id', '')}", "termin_id": t.get("id", "")}
            for t in daten()["termine"] if t.get("verein") == key and t.get("datum")]


def _pruefen(key: str, termine: list[dict]) -> list[dict]:
    """Konflikte nach dem Prüfkreis des Vereins: Gemeinde automatisch + dazu − ohne. Nur Termine im Kalender."""
    dazu, ohne = P.pruefkreis(key)
    jahre = {t["datum"][:4] for t in termine}
    andere = [t for t in daten()["termine"] if t.get("datum", "")[:4] in jahre]
    return _mit_konflikten(termine, andere, zusatz=dazu, ohne=ohne)


@planung_bp.route("/verein/termine")
@verein_login
def termine_seite(key):
    jahr = _jahr_filter()
    heute = date.today().isoformat()
    kal = kalender_termine(key)
    offen = P.entwuerfe(key, offen=True)
    alle = kal + offen
    sicht = [t for t in alle if (t["datum"][:4] == str(jahr) if jahr else t["datum"] >= heute)]
    _pruefen(key, sicht)
    eigene = [t for t in sicht if t["status"] in ("entwurf", "bestaetigt")]
    im_kalender = [t for t in sicht if t["status"] == "kalender"]
    vorlage_jahr = jahr or date.today().year + 1
    jahre = sorted({int(t["datum"][:4]) for t in alle} | {date.today().year, date.today().year + 1})
    upload_heute = get_upload_count(g.user["verein_id"], date.today().isoformat())
    return render_template("planung/termine.html", jahr=jahr, jahre=jahre, n_offen=len(eigene),
                           monate_offen=_nach_monat(eigene), monate_kalender=_nach_monat(im_kalender),
                           vorlage_jahr=vorlage_jahr,
                           vorlage_n=len(P.offene_vorlage(key, vorschlaege(vorlage_jahr), kal)),
                           runden=[r for r in P.runden_des_vereins(key) if r["status"] == "offen"],
                           upload_rest=max(0, UPLOAD_LIMIT - upload_heute), upload_limit=UPLOAD_LIMIT,
                           fehler=request.args.get("fehler", ""), meldung=request.args.get("meldung", ""),
                           upload_ok=request.args.get("upload_ok", ""), daten_ab=daten_ab())


@planung_bp.route("/verein/kollisionen")
def api_kollisionen():
    """Warnung im Formular „Neuer Termin“/„Bearbeiten“. Verein aus der Sitzung, Prüfkreis aus den Einstellungen,
    nur Termine im Kalender (Entwürfe anderer Vereine sieht man nur in einer Planungsrunde)."""
    from services.auth.routes import _session_token
    from shared.vk_db import get_session_user
    user = get_session_user(_session_token())
    if not user or not user.get("verein_key") or user["verein_status"] != "aktiv":
        return jsonify({"fehler": "nicht angemeldet"}), 401
    key = user["verein_key"]
    von, bis = request.args.get("von", ""), request.args.get("bis", "") or request.args.get("von", "")
    if not datum_ok(von) or not datum_ok(bis) or bis < von:
        return jsonify([])
    tage = []
    d0, d1 = date.fromisoformat(von), date.fromisoformat(bis)
    while d0 <= d1 and len(tage) < MAX_TAGE:
        tage.append(d0.isoformat())
        d0 = date.fromordinal(d0.toordinal() + 1)
    d = daten()
    dazu, ohne = P.pruefkreis(key)
    entwurf = {"verein": key, "tage": tage,
               "ort": request.args.get("ort", "")[:TEXT_MAX], "uhrzeit": request.args.get("uhrzeit", "")[:5]}
    ausser = {request.args["ohne_id"][:40]} if request.args.get("ohne_id") else None
    k = kollisionen(d["termine"], d["meta"], d["labels"], entwurf, d["rubriken"],
                    wochenende=request.args.get("wochenende") == "1", zusatz_vereine=dazu, ohne_vereine=ohne,
                    ausser_ids=ausser)
    return jsonify([{**x, "datum_text": datum_text(x)} for x in k]), 200, {"Cache-Control": "no-store"}


@planung_bp.route("/verein/entwuerfe/aus-vorjahr", methods=["POST"])
@verein_login
def entwuerfe_aus_vorjahr(key):
    _nur_admin()
    jahr = _zieljahr()
    n = P.aus_vorlage(key, vorschlaege(jahr), kalender_termine(key))
    if n:
        verlauf(key, jahr, "Entwürfe aus dem Vorjahr erzeugt", f"{n} Termine")
    return zurueck(meldung=f"{n} Entwürfe für {jahr} aus dem Vorjahr angelegt." if n else "Keine neuen Vorschläge aus dem Vorjahr.")


@planung_bp.route("/verein/entwuerfe/<int:eid>", methods=["POST"])
@verein_login
def entwurf_aktion(key, eid):
    _nur_admin()
    t = P.entwurf(eid)
    if not t or t["verein"] != key:
        abort(404)   # fremde Entwürfe gibt es für diesen Verein nicht
    jahr, aktion = int(t["datum"][:4]), request.form.get("aktion", "")
    name = t["bezeichnung"]
    if aktion == "loeschen":
        if P.entwurf_loeschen(eid, key):
            verlauf(key, jahr, "Termin gelöscht", f"{name}: {kurz(t['datum'])}")
        return zurueck(meldung=f"{name} gelöscht.")
    if aktion in ("bestaetigen", "nicht_bestaetigen"):
        if P.bestaetigen(key, [eid], aktion == "bestaetigen"):
            verlauf(key, jahr, "bestätigt" if aktion == "bestaetigen" else "Bestätigung zurückgenommen", name)
    elif aktion == "veroeffentlichen":
        if veroeffentlichen(g.user, [eid]):
            verlauf(key, jahr, "veröffentlicht", name)
            return zurueck(f"t{eid}", meldung=f"{name} steht jetzt im Kalender.")
    elif aktion == "speichern":
        felder, fehler = felder_aus_formular()
        if fehler:
            return zurueck(f"t{eid}", fehler=fehler)
        if P.entwurf_aendern(eid, key, felder):
            aenderung = [f"{kurz(t['datum'])} → {kurz(felder['datum'])}"] if felder["datum"] != t["datum"] else []
            if felder["uhrzeit"] != t["uhrzeit"]:
                aenderung.append(f"{t['uhrzeit'] or 'ohne Uhrzeit'} → {felder['uhrzeit'] or 'ohne Uhrzeit'}")
            if felder["bezeichnung"] != name:
                aenderung.append(f"neuer Titel „{felder['bezeichnung']}“")
            if felder["ort"] != t["ort"]:
                aenderung.append("Ort geändert")
            verlauf(key, jahr, "Termin geändert", f"{name}: " + (", ".join(aenderung) or "ohne Änderung"))
            if int(felder["datum"][:4]) != jahr:
                verlauf(key, int(felder["datum"][:4]), "Termin ergänzt (aus anderem Jahr verschoben)", name)
    else:
        abort(400)
    return zurueck(f"t{eid}")


@planung_bp.route("/verein/entwuerfe/alle", methods=["POST"])
@verein_login
def entwuerfe_alle(key):
    """Alle eigenen Entwürfe bestätigen (in der Runde) oder veröffentlichen – eines Jahres (Feld `jahr`) oder,
    ohne Jahr, alle ab heute (Standardansicht von „Termine“)."""
    _nur_admin()
    jahr = _jahr_filter()
    heute = date.today().isoformat()
    offen = [t for t in P.entwuerfe(key, jahr, offen=True) if jahr or t["datum"] >= heute]
    jahre = sorted({int(t["datum"][:4]) for t in offen})
    if request.form.get("aktion") == "veroeffentlichen":
        n = veroeffentlichen(g.user, [t["_eid"] for t in offen])
        for j in jahre:
            verlauf(key, j, "alle veröffentlicht", f"{sum(1 for t in offen if t['datum'][:4] == str(j))} Termine")
        return zurueck(meldung=f"{n} Termine stehen jetzt im Kalender.")
    unbest = [t for t in offen if t["status"] == "entwurf"]
    n = P.bestaetigen(key, [t["_eid"] for t in unbest])
    for j in sorted({int(t["datum"][:4]) for t in unbest}):
        verlauf(key, j, "alle bestätigt", f"{sum(1 for t in unbest if t['datum'][:4] == str(j))} Termine")
    return zurueck(meldung=f"{n} Termine bestätigt.")


@planung_bp.route("/verein/termine-export.<fmt>")
@verein_login
def termine_export(key, fmt):
    jahr = _jahr_filter()
    heute = date.today().isoformat()
    alle = kalender_termine(key) + P.entwuerfe(key, offen=True)
    zeilen = [t for t in alle if (t["datum"][:4] == str(jahr) if jahr else t["datum"] >= heute)]
    return _export(f"Termine {jahr or 'ab ' + date.today().strftime('%d.%m.%Y')} – {g.user['verein_name']}",
                   zeilen, fmt, mit_verein=False)


# ── Einstellungen ────────────────────────────────────────────────────────────

def crawler_aus(key: str) -> bool:
    try:
        return bool(KalenderStore.read().get("_meta", {}).get(key, {}).get("crawler_aus"))
    except Exception:
        return False


@planung_bp.route("/verein/einstellungen", methods=["GET", "POST"])
@verein_login
def einstellungen(key):
    gem, lk = sitz(key)
    eigene_gemeinde = [(k, n) for k, n in vereine_der_gemeinde(gem, lk) if k != key] if gem else []
    gem_keys = {k for k, _ in eigene_gemeinde}
    if request.method == "POST":
        _nur_admin()
        # Eine Liste mit Haken (einfache Abläufe). Gespeichert wird als Abweichung von der Gemeinde – so zählen
        # Vereine, die später in der Gemeinde dazukommen, automatisch mit.
        bekannt = labels()
        geprueft = {k for k in request.form.getlist("geprueft") if k in bekannt and k != key}
        P.pruefkreis_setzen(key, geprueft - gem_keys, gem_keys - geprueft)
        return redirect(url_for("planung.einstellungen", meldung="Gespeichert. Gilt ab sofort für alle Warnungen.") + "#pruefkreis")
    dazu, ohne = P.pruefkreis(key)
    gruppen = [{**gr, "vereine": [x for x in gr["vereine"] if x[0] != key and x[0] not in gem_keys]}
               for gr in vereine_gruppiert()]
    if eigene_gemeinde:
        gruppen.insert(0, {"gemeinde": f"{gem} – eure Gemeinde", "landkreis": "", "vereine": eigene_gemeinde})
    return render_template("planung/einstellungen.html", gemeinde=gem, gruppen=[gr for gr in gruppen if gr["vereine"]],
                           geprueft=(gem_keys - ohne) | dazu, dazu=dazu, labels=labels(), crawler_aus=crawler_aus(key),
                           email=g.user["email"], rolle=g.user["role"],
                           meldung=request.args.get("meldung", ""), fehler=request.args.get("fehler", ""))


# ── Planungsrunden ───────────────────────────────────────────────────────────

_versuche_lock = threading.Lock()
_versuche: dict[str, list[float]] = {}


def _versuche_ok(key: str) -> bool:
    """Gegen Durchprobieren von Codes: höchstens 10 Fehlversuche je 10 Minuten und Verein (zusätzlich nginx-Limit)."""
    jetzt_s = time.time()
    with _versuche_lock:
        v = [x for x in _versuche.get(key, []) if jetzt_s - x < 600]
        _versuche[key] = v
        return len(v) < 10


def _fehlversuch(key: str) -> None:
    with _versuche_lock:
        _versuche.setdefault(key, []).append(time.time())


@planung_bp.route("/verein/runden")
@verein_login
def runden_seite(key):
    # Ergebnisse gehören zur Runde. Auch Runden, aus denen der Verein nach dem Abschluss ausgetreten ist, bleiben mit
    # ihrem Ergebnis sichtbar („Frühere Runden“).
    ergebnisse = defaultdict(list)
    for e in P.ergebnisse_des_vereins(key):
        ergebnisse[e["runde_id"]].append(e)
    aktiv = P.runden_des_vereins(key)
    nur_ergebnis = [P.runde(rid) for rid in ergebnisse if rid not in {r["id"] for r in aktiv}]
    return render_template("planung/runden.html", runden=aktiv, nur_ergebnis=[r for r in nur_ergebnis if r],
                           ergebnisse=ergebnisse, jahr=date.today().year + 1, fehler=request.args.get("fehler", ""),
                           code=request.args.get("code", ""))


@planung_bp.route("/verein/runden/neu", methods=["POST"])
@verein_login
def runde_neu(key):
    _nur_admin()
    name = request.form.get("name", "").strip()[:TEXT_MAX]
    try:
        jahr = int(request.form.get("jahr", ""))
    except ValueError:
        jahr = 0
    if not name or not date.today().year <= jahr <= 2040:
        return redirect(url_for("planung.runden_seite", fehler="Bitte Name und Jahr angeben."))
    return redirect(url_for("planung.runde_seite", runde_id=P.runde_starten(name, jahr, key)))


def _beitritt_seite(key, r, nachweis):
    """Gemeinsame Bestätigungsseite für Link und Code – beitreten nur per aktivem Klick."""
    teil = {t["verein_key"]: t for t in P.teilnehmer(r["id"])}
    return render_template("planung/beitreten.html", r=r, nachweis=nachweis,
                           status=(teil[key]["aktiv"] if key in teil else None),
                           organisator=vereinsname(r["organisator"]))


@planung_bp.route("/verein/r/<link>")
def runde_link(link):
    """Einladungslink. Nicht angemeldet: Link merken (Cookie), zum Login – nach dem Login geht es hierher zurück.
    Wartet das Konto noch auf Freigabe, bleibt der Link gemerkt, bis das Anmelden klappt."""
    from services.auth.routes import _COOKIE_SECURE, _session_token
    from shared.vk_db import get_session_user
    user = get_session_user(_session_token())
    if not user or not user.get("email_verified") or user.get("verein_status") != "aktiv":
        resp = make_response(redirect("/verein/login?hint=einladung"))
        resp.set_cookie(WEITER_COOKIE, f"/verein/r/{link}"[:200], httponly=True, secure=_COOKIE_SECURE,
                        samesite="Lax", max_age=30 * 24 * 3600)
        return resp
    return _runde_link_angemeldet(link)


@verein_login
def _runde_link_angemeldet(key, link):
    r = P.runde_per_link(link)
    if not r:
        abort(404, "Diesen Einladungslink gibt es nicht (mehr). Bitte beim Organisator nachfragen.")
    resp = make_response(_beitritt_seite(key, r, r["link"]))
    resp.delete_cookie(WEITER_COOKIE)
    return resp


@planung_bp.route("/verein/runde/beitreten-code", methods=["POST"])
@verein_login
def runde_code(key):
    _nur_admin()
    if not _versuche_ok(key):
        abort(429, "Zu viele falsche Codes. Bitte in 10 Minuten noch einmal.")
    r = P.runde_per_code(request.form.get("code", ""))
    if not r:
        _fehlversuch(key)
        return redirect(url_for("planung.runden_seite", fehler="Code nicht gefunden.", code=request.form.get("code", "")[:12]))
    return _beitritt_seite(key, r, r["code"])


@planung_bp.route("/verein/runde/<int:runde_id>/beitreten", methods=["POST"])
@verein_login
def runde_beitreten(key, runde_id):
    """Beitreten braucht den gültigen Link oder Code der Runde – die ID allein reicht nicht."""
    _nur_admin()
    r = P.runde(runde_id)
    nachweis = request.form.get("nachweis", "")
    if not r or nachweis not in (r["link"], r["code"]):
        abort(403, "Einladung nicht (mehr) gültig – Link oder Code beim Organisator neu holen.")
    if r["status"] != "offen":
        abort(403, "Diese Planungsrunde ist abgeschlossen.")
    ergebnis = P.beitreten(runde_id, key)
    if ergebnis == "entfernt":
        abort(403, "Der Organisator hat euch aus dieser Runde entfernt.")
    if ergebnis == "neu":
        P.protokoll(runde_id, key, "beigetreten", "per Code" if nachweis == r["code"] else "per Link")
    return redirect(url_for("planung.runde_seite", runde_id=runde_id))


def _runde_fuer(key, runde_id):
    r = P.runde(runde_id)
    if not r or key not in P.aktive_teilnehmer(runde_id):
        abort(404)
    return r


def _runde_daten(r) -> tuple[list[dict], list[dict], dict]:
    """(Termine der aktiven Teilnehmer im Planungsjahr mit Konflikten, Konfliktpaare am gleichen Tag, Farben).
    Termine = Entwürfe der Teilnehmer; veröffentlichte mit den aktuellen Werten aus dem Kalender."""
    keys = P.aktive_teilnehmer(r["id"])
    im_jahr = _kalender_im_jahr(r["jahr"])
    kal = {(t.get("verein"), t.get("id")): t for t in im_jahr}
    alle_kal = {(t.get("verein"), t.get("id")): t for t in daten()["termine"]}
    termine = []
    for t in P.entwuerfe(jahr=r["jahr"], vereine=keys):
        if t["termin_id"]:
            live = kal.get((t["verein"], t["termin_id"])) or alle_kal.get((t["verein"], t["termin_id"]))
            if not live:
                continue   # im Kalender gelöscht
            if live.get("datum", "")[:4] != str(r["jahr"]):
                continue   # im Kalender in ein anderes Jahr verschoben
            t.update({f: live.get(f, "") for f in ("datum", "uhrzeit", "uhrzeit_bis", "bezeichnung", "ort")})
        termine.append(t)
    termine.sort(key=lambda t: (t["datum"], t["uhrzeit"], t["_eid"]))
    veroeffentlicht = {t["termin_id"] for t in termine if t["termin_id"]}
    # Veröffentlichte Entwürfe der Teilnehmer stecken schon in `termine` – ihre Kalender-Kopie nicht doppelt zählen
    andere = termine + [t for t in im_jahr if not (t.get("verein") in keys and t.get("id") in veroeffentlicht)]
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
    farben = {t["verein_key"]: i % 8 for i, t in enumerate(P.teilnehmer(r["id"]))}
    return termine, paare, farben


@planung_bp.route("/verein/runde/<int:runde_id>")
@verein_login
def runde_seite(key, runde_id):
    r = _runde_fuer(key, runde_id)
    termine, _, farben = _runde_daten(r)
    stand = defaultdict(lambda: {"entwurf": 0, "bestaetigt": 0, "veroeffentlicht": 0})
    for t in termine:
        stand[t["verein"]][t["status"]] += 1
    teilnehmer = [{**t, "verein_name": vereinsname(t["verein_key"])} for t in P.teilnehmer(r["id"])]
    return render_template("planung/runde.html", r=r, key=key, organisator=(r["organisator"] == key),
                           stand_kennung=P.stand_version(runde_id, r["jahr"]), ergebnisse=P.ergebnisse(runde_id),
                           verlauf=list(reversed(P.protokoll_liste(runde_id)))[:200],
                           teilnehmer=teilnehmer, stand=stand, farben=farben,
                           n_konflikt=sum(1 for t in termine if t.get("_konflikt_tag")),
                           namen={t["verein_key"]: t["verein_name"] for t in teilnehmer}, monate=_nach_monat(termine),
                           organisator_name=vereinsname(r["organisator"]),
                           code=P.code_anzeige(r["code"]), basis=request.host_url.rstrip("/"),
                           vorlage_n=len(P.offene_vorlage(key, vorschlaege(r["jahr"]), kalender_termine(key))),
                           fehler=request.args.get("fehler", ""), meldung=request.args.get("meldung", ""))


@planung_bp.route("/verein/runde/<int:runde_id>/verlassen", methods=["POST"])
@verein_login
def runde_verlassen(key, runde_id):
    _nur_admin()
    r = _runde_fuer(key, runde_id)
    if r["organisator"] == key:
        abort(400, "Der Organisator kann die Runde nicht verlassen – abschließen geht.")
    P.verlassen(runde_id, key)
    P.protokoll(runde_id, key, "ausgetreten")
    return redirect(url_for("planung.runden_seite"))


def _nur_organisator(key, runde_id):
    _nur_admin()
    r = _runde_fuer(key, runde_id)
    if r["organisator"] != key:
        abort(403, "Das darf nur der Organisator der Runde.")
    return r


@planung_bp.route("/verein/runde/<int:runde_id>/erneuern", methods=["POST"])
@verein_login
def runde_erneuern(key, runde_id):
    _nur_organisator(key, runde_id)
    P.einladung_erneuern(runde_id)
    P.protokoll(runde_id, key, "Link und Code erneuert")
    return redirect(url_for("planung.runde_seite", runde_id=runde_id, meldung="Neuer Link und neuer Code – die alten gelten nicht mehr."))


@planung_bp.route("/verein/runde/<int:runde_id>/teilnehmer", methods=["POST"])
@verein_login
def runde_teilnehmer(key, runde_id):
    _nur_organisator(key, runde_id)
    verein = request.form.get("verein", "")
    if verein and verein != key and verein in {t["verein_key"] for t in P.teilnehmer(runde_id)}:
        aktiv = request.form.get("aktiv") == "1"
        P.teilnehmer_setzen(runde_id, verein, aktiv)
        P.protokoll(runde_id, key, "wieder aufgenommen" if aktiv else "entfernt", vereinsname(verein))
    return redirect(url_for("planung.runde_seite", runde_id=runde_id) + "#teilnehmer")


@planung_bp.route("/verein/runde/<int:runde_id>/status", methods=["POST"])
@verein_login
def runde_status(key, runde_id):
    r = _nur_organisator(key, runde_id)
    if request.form.get("aktion") == "abschliessen" and r["status"] == "offen":
        version = P.ergebnis_speichern(runde_id, key, _ergebnis_stand(r, key))
        P.runde_status(runde_id, "abgeschlossen")
        return redirect(url_for("planung.runde_seite", runde_id=runde_id,
                                meldung=f"Runde abgeschlossen. Das Ergebnis (Version {version}) steht jetzt bei jedem beteiligten Verein unter „Planungsrunden“."))
    if request.form.get("aktion") == "oeffnen" and r["status"] != "offen":
        P.runde_status(runde_id, "offen")
        P.protokoll(runde_id, key, "Runde wieder geöffnet")
    return redirect(url_for("planung.runde_seite", runde_id=runde_id))


def _ergebnis_stand(r, key) -> dict:
    """Eingefrorener Stand beim Abschluss – Grundlage für das Ergebnis-PDF."""
    termine, paare, _ = _runde_daten(r)
    keys = P.aktive_teilnehmer(r["id"])
    jetzt_s = datetime.now().isoformat(timespec="seconds")
    version = P.naechste_version(r["id"])
    verlauf_liste = P.protokoll_liste(r["id"]) + [{"zeit": jetzt_s, "verein_key": key, "aktion": "Runde abgeschlossen",
                                                   "details": f"Ergebnis Version {version}"}]
    return {
        "runde_id": r["id"], "name": r["name"], "jahr": r["jahr"], "version": version,
        "organisator": r["organisator"], "organisator_name": vereinsname(r["organisator"]),
        "abgeschlossen_am": jetzt_s, "abgeschlossen_von": key, "abgeschlossen_von_name": vereinsname(key),
        "teilnehmer": sorted(({"verein": k, "name": vereinsname(k)} for k in keys), key=lambda x: x["name"].lower()),
        "termine": [{"verein": t["verein"], "verein_name": vereinsname(t["verein"]),
                     **{f: t.get(f, "") for f in ("datum", "uhrzeit", "uhrzeit_bis", "bezeichnung", "ort")},
                     "status": t["status"]} for t in termine],
        "konflikte": [{"datum": p["a"]["datum"], "a": f"{p['a']['bezeichnung']} ({vereinsname(p['a']['verein'])})",
                       "b": f"{p['b']['bezeichnung']} ({p['b']['verein_name']})"} for p in paare],
        "verlauf": [{**v, "verein_name": vereinsname(v["verein_key"])} for v in verlauf_liste],
    }


@planung_bp.route("/verein/ergebnis/<int:eid>.<fmt>")
@verein_login
def ergebnis_export(key, eid, fmt):
    """Nur für Vereine, die beim Abschluss beteiligt waren."""
    e = P.ergebnis(eid)
    if not e or key not in {t["verein"] for t in e["daten"]["teilnehmer"]}:
        abort(404)
    d = e["daten"]
    titel = f"Ergebnis {d['name']} {d['jahr']} (Version {d.get('version', e['version'])})"
    if fmt == "pdf":
        try:
            inhalt = ergebnis_pdf(d)
        except ImportError:
            abort(404, "Das PDF steht gerade nicht zur Verfügung – bitte Excel nehmen.")
        return Response(inhalt, mimetype="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{dateiname(titel, "pdf")}"'})
    return _export(titel, d["termine"], fmt, mit_verein=True)


# ── Netz: Lebenszeichen und Stand für das Treffen ────────────────────────────

@planung_bp.route("/verein/ping")
def ping():
    """Für den Netz-Hinweis: misst die Antwortzeit im Browser. Nie zwischenspeichern."""
    return Response(status=204, headers={"Cache-Control": "no-store"})


@planung_bp.route("/verein/runde/<int:runde_id>/stand")
@verein_login
def runde_stand(key, runde_id):
    """Kennung des Stands – die Rundenseite fragt sie regelmäßig ab und meldet „Andere haben geändert“."""
    r = _runde_fuer(key, runde_id)
    return jsonify({"stand": P.stand_version(runde_id, r["jahr"])}), 200, {"Cache-Control": "no-store"}


@planung_bp.route("/verein/runde/<int:runde_id>/export.<fmt>")
@verein_login
def runde_export(key, runde_id, fmt):
    r = _runde_fuer(key, runde_id)
    termine, _, _ = _runde_daten(r)
    return _export(f"{r['name']} {r['jahr']}", termine, fmt, mit_verein=True)
