import html
import io
import json
import os
import re
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Blueprint, make_response, redirect, request

from shared.kalender_store import KalenderStore
from shared.kalender_core import (
    VEREINSTERMINE_FILE, _HEIC_SUPPORTED, _do_save_import,
    import_pdf_bytes, parse_excel_bytes,
)
from shared.flyer_store import upload_flyer, delete_flyer, pruefe_flyer
from shared.rubriken import RUBRIKEN
from shared.csrf import csrf_field, get_csrf_token, validate_csrf
from shared.vk_db import (
    AVV_FASSUNG, DS_FASSUNG, db_conn, delete_user_sessions, get_session_user, log_audit,
    get_upload_count, increment_upload_quota,
)
from services.auth.routes import (
    DS_FEHLER, DS_FELD, DS_KAESTCHEN,
    _CSS, _ORTSCHAFT_JS, _PLZ_QUELLE, _ortschaft_felder, _page, _session_token,
    _telegram_ortschaft_hinweis, ortschaft_geo, require_verein_login,
)
from shared.geo import plz_gueltig
from shared.termin_felder import BESCHREIBUNG_MAX as _BESCHREIBUNG_MAX, datum_ok, zeit_fehler as _zeit_fehler
from services.verein.planung import KOLLISION_JS

verein_bp = Blueprint("verein", __name__)

_BACK_PROFIL = '<a class="btn btn-sec" href="/verein/profil" style="margin-top:.75rem">← Zurück zum Profil</a>'
_BACK_DASH = '<a class="btn btn-sec" href="/verein/termine" style="margin-top:.75rem">← Zurück zu den Terminen</a>'
_BACK_EINST = '<a class="btn btn-sec" href="/verein/einstellungen" style="margin-top:.75rem">← Zurück zu den Einstellungen</a>'
_BACK_HISTORY = '<a class="btn btn-sec" href="javascript:history.back()" style="margin-top:.75rem">← Zurück</a>'
_UPLOAD_LIMIT = 3
_MAX_UPLOAD_MB = 40  # Summe aller Flyer je Formular; nginx /verein/(termine|upload) erlaubt 45m
_MAX_PLAN_MB = 20    # Terminplan-Upload (PDF/Bild/Excel), wie Admin-/upload


def _quota_remaining(verein_id: int) -> int:
    return max(0, _UPLOAD_LIMIT - get_upload_count(verein_id, date.today().isoformat()))


def _load_data() -> dict:
    return json.loads(VEREINSTERMINE_FILE.read_text())


def _get_verein_termine(verein_key: str) -> list:
    data = _load_data()
    alle = data.get(verein_key, [])
    return [t for t in alle if not t.get("geloescht") and not t.get("deleted")]


# ── Dashboard → „Termine“ (v1.77, ADR-026) ───────────────────────────────────

@verein_bp.route("/verein/dashboard")
def dashboard():
    """Frühere Startseite. Das Dashboard ist aufgeteilt auf Termine · Planungsrunden · Einstellungen
    (`services/verein/planung.py`); alte Lesezeichen, Mails und Links landen auf „Termine“."""
    ok = request.args.get("upload_ok", "")
    return redirect("/verein/termine" + (f"?upload_ok={ok}" if ok.isdigit() else ""))


# ── Abruf von Gemeinde-Webseiten an/aus (ADR-027) ───────────────────────────

def _crawler_aus(verein_key: str | None) -> bool:
    if not verein_key:
        return False
    try:
        return bool(_load_data().get("_meta", {}).get(verein_key, {}).get("crawler_aus"))
    except Exception:
        return False


@verein_bp.route("/verein/crawler", methods=["POST"])
@require_verein_login
def crawler_schalten(user):
    if user["role"] != "admin":
        return _page("Fehler", '<p class="err">Nur Vereinsadmins können das ändern.</p>'), 403
    if not validate_csrf():
        return _page("Fehler", '<p class="err">Ungültige Anfrage. Bitte Seite neu laden.</p>'), 403
    verein_key = user.get("verein_key")
    if not verein_key:
        return redirect("/verein/einstellungen")
    aus = request.form.get("aus") == "1"

    def _mut(d):
        meta = d.setdefault("_meta", {}).setdefault(verein_key, {})
        if aus:
            meta["crawler_aus"] = True
        else:
            meta.pop("crawler_aus", None)
        return d

    KalenderStore.update(_mut)
    log_audit("crawler_aus" if aus else "crawler_an", "", verein_key, user["id"])
    meldung = ("Gespeichert.+Termine+kommen+ab+jetzt+nur+noch+von+euch." if aus
               else "Gespeichert.+Termine+von+Gemeinde-Webseiten+werden+wieder+erg%C3%A4nzt.")
    return redirect(f"/verein/einstellungen?meldung={meldung}#quellen")


# ── Neuer Termin ─────────────────────────────────────────────────────────────

_MAX_TAGE = 16
_WOCHENTAGE = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
_FLYER_ACCEPT = ".pdf,.jpg,.jpeg,.png,.webp"
_FLYER_SUMME_HINWEIS = (f'<p class="hint">Mehrere Tage: alle Flyer zusammen max. {_MAX_UPLOAD_MB} MB pro Speichern – '
                        'weitere Flyer danach beim jeweiligen Termin ergänzen.</p>')
_FLYER_HINWEIS = ('<p class="hint">Bitte den Flyer zuerst auf dem Gerät speichern (z.B. Bild aus der E-Mail '
                  'per „Speichern unter") und von dort hochladen. Ein Bild direkt aus Outlook/der E-Mail zu '
                  'ziehen funktioniert nicht.</p>')


def _tag_label(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{_WOCHENTAGE[d.weekday()]} {d.strftime('%d.%m.%Y')}"


def _tag_block(i: int, tag: str, beschreibung: str, mehrtaegig: bool) -> str:
    """Beschreibung + Flyer für einen Tag. Bei eintägigen Terminen ohne Tages-Überschrift."""
    kopf = (f'<div class="tag-kopf" style="font-weight:600;margin-top:1rem">Tag {i + 1} · {_tag_label(tag)}</div>'
            if mehrtaegig else '<div class="tag-kopf" style="display:none"></div>')
    rahmen = ' style="border-top:1px solid #3a3a3c;margin-top:1rem"' if mehrtaegig else ""
    return f"""<div class="tag-block"{rahmen}>
  {kopf}
  <label>Beschreibung (optional)</label>
  <textarea name="beschreibung_{i}" rows="3" maxlength="{_BESCHREIBUNG_MAX}" placeholder="Wird beim Antippen des Termins angezeigt">{html.escape(beschreibung)}</textarea>
  <label>Flyer (optional, PDF/JPG/PNG/WebP, max. 8 MB)</label>
  {_FLYER_SUMME_HINWEIS if i == 0 else ''}
  {_FLYER_HINWEIS if i == 0 else ''}
  <input name="flyer_{i}" type="file" accept="{_FLYER_ACCEPT}">
</div>"""


# Blendet je Tag zwischen Datum und bis-Datum einen Block (Beschreibung + Flyer) ein.
# Bestehende Blöcke bleiben erhalten (Dateiauswahl lässt sich nicht kopieren), es wird
# nur am Ende ergänzt oder entfernt. Ohne JS bleibt der serverseitig gerenderte Block.
_TAGE_JS = """<script>
(function(){
  var MAX=%d, WT=["So","Mo","Di","Mi","Do","Fr","Sa"];
  var von=document.querySelector("[name=datum]"), bis=document.querySelector("[name=datum_bis]");
  var box=document.getElementById("tage"), info=document.getElementById("tage-info");
  var vorlage=box.querySelector(".tag-block").cloneNode(true);
  function iso(d){return d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0");}
  function lbl(d){return WT[d.getDay()]+" "+String(d.getDate()).padStart(2,"0")+"."+String(d.getMonth()+1).padStart(2,"0")+"."+d.getFullYear();}
  function tage(){
    if(!von.value)return [];
    var a=new Date(von.value+"T12:00:00"), e=bis.value?new Date(bis.value+"T12:00:00"):a, out=[];
    if(e<a)return null;
    for(var d=new Date(a);d<=e&&out.length<=MAX;d.setDate(d.getDate()+1))out.push(new Date(d));
    return out;
  }
  function neu(i){
    var b=vorlage.cloneNode(true);
    b.querySelector("textarea").name="beschreibung_"+i; b.querySelector("textarea").value="";
    b.querySelector("input[type=file]").name="flyer_"+i; b.querySelector("input[type=file]").value="";
    b.querySelectorAll(".hint").forEach(function(h){h.remove();});
    return b;
  }
  function sync(){
    var t=tage(); info.textContent="";
    if(t===null){info.textContent="Das bis-Datum liegt vor dem Startdatum.";return;}
    if(t.length>MAX){info.textContent="Höchstens "+MAX+" Tage auf einmal.";return;}
    var n=Math.max(t.length,1), bl=box.querySelectorAll(".tag-block");
    for(var i=bl.length;i<n;i++)box.appendChild(neu(i));
    bl=box.querySelectorAll(".tag-block");
    for(var j=bl.length-1;j>=n;j--)bl[j].remove();
    var mehr=n>1;
    box.querySelectorAll(".tag-block").forEach(function(b,k){
      var kopf=b.querySelector(".tag-kopf");
      kopf.style.cssText=mehr?"font-weight:600;margin-top:1rem":"display:none";
      kopf.textContent=mehr?"Tag "+(k+1)+" · "+lbl(t[k]):"";
      b.style.borderTop=mehr?"1px solid #3a3a3c":""; b.style.marginTop=mehr?"1rem":"";
    });
    if(mehr)info.textContent=n+" Einträge werden angelegt – je Tag eigene Beschreibung und eigener Flyer möglich.";
  }
  von.addEventListener("change",sync); bis.addEventListener("change",sync);
  document.getElementById("termin-form").addEventListener("submit",function(ev){
    var summe=0; box.querySelectorAll("input[type=file]").forEach(function(f){if(f.files[0])summe+=f.files[0].size;});
    if(summe>%d){ev.preventDefault();info.textContent="Alle Flyer zusammen sind größer als %d MB. Bitte einzelne Flyer nachträglich beim jeweiligen Termin hochladen.";}
  });
})();
</script>"""


# Vorschau: zeigt den echten Kalender (/?vorschau=1) im Overlay und übergibt die Formularwerte per
# postMessage. Nichts wird gespeichert oder hochgeladen – Flyer bleiben als blob:-URL auf dem Gerät.
# Termin-Darstellung kommt ausschließlich aus kalender.html (render()), hier wird keine Karte nachgebaut.
_VORSCHAU_BTN = ('<button class="btn btn-sec" type="button" id="vorschau-btn" style="margin-top:1rem">'
                 'Vorschau</button><p id="vorschau-msg" class="err" style="display:none"></p>')

_VORSCHAU_JS = """<style>
.vs-ov{position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:1000;display:flex;flex-direction:column;align-items:center;padding:12px}
.vs-box{background:#1c1c1e;border-radius:14px;width:100%;max-width:520px;flex:1;display:flex;flex-direction:column;overflow:hidden;border:1px solid #3a3a3c}
.vs-kopf{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid #3a3a3c}
.vs-kopf b{font-size:.95rem}.vs-kopf small{display:block;color:#aeaeb2;font-size:.78rem;font-weight:400}
.vs-zu{background:#2c2c2e;color:#0a84ff;border:none;border-radius:8px;padding:.45rem .8rem;font-size:.9rem;font-weight:600;cursor:pointer;flex-shrink:0}
.vs-box iframe{flex:1;width:100%;border:0;background:#1c1c1e}
.vs-flyer{flex:1;overflow:auto;display:flex;align-items:flex-start;justify-content:center;background:#000}
.vs-flyer img{max-width:100%;height:auto}.vs-flyer iframe{width:100%;height:100%;border:0;background:#fff}
</style>
<script>
(function(){
  var form=document.getElementById("termin-form"), btn=document.getElementById("vorschau-btn"), msg=document.getElementById("vorschau-msg");
  if(!form||!btn)return;
  var blobs=[], typen={}, ov=null, frame=null, daten=null;
  function val(n){var e=form.elements[n];return e&&e.value?String(e.value).trim():"";}
  function flyerVon(inp){
    if(inp&&inp.files&&inp.files[0]){var u=URL.createObjectURL(inp.files[0]);blobs.push(u);typen[u]=inp.files[0].type||"";return u;}
    return "";
  }
  function sammeln(){
    var datum=val("datum"), bez=val("bezeichnung");
    if(!datum||!bez)return "Bitte zuerst Datum und Bezeichnung ausfüllen.";
    var bl=[].slice.call(form.querySelectorAll(".tag-block"));
    var tage=bl.length?bl.map(function(b){return {b:b.querySelector("textarea"),f:b.querySelector("input[type=file]")};})
                      :[{b:form.elements["beschreibung"],f:form.elements["flyer"]}];
    var start=new Date(datum+"T12:00:00"), termine=[];
    tage.forEach(function(t,i){
      var d=new Date(start);d.setDate(d.getDate()+i);
      var iso=d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0");
      termine.push({datum:iso,uhrzeit:val("uhrzeit"),uhrzeit_bis:val("uhrzeit")?val("uhrzeit_bis"):"",bezeichnung:bez,ort:val("ort"),
        beschreibung:t.b?String(t.b.value).trim():"",flyer_url:flyerVon(t.f)||(i===0?(form.dataset.flyerUrl||""):"")});
    });
    return {typ:"vko-vorschau-daten",verein:{key:form.dataset.vereinKey||"",name:form.dataset.vereinName||""},termine:termine};
  }
  function schliessen(){
    if(ov)ov.remove();ov=null;frame=null;daten=null;
    blobs.forEach(function(u){URL.revokeObjectURL(u);});blobs=[];typen={};
  }
  function kopf(titel,sub){
    var k=document.createElement("div");k.className="vs-kopf";
    var t=document.createElement("div");t.innerHTML="<b></b><small></small>";
    t.querySelector("b").textContent=titel;t.querySelector("small").textContent=sub;
    var z=document.createElement("button");z.type="button";z.className="vs-zu";z.textContent="Schließen";
    k.appendChild(t);k.appendChild(z);return {k:k,z:z};
  }
  function flyerZeigen(url){
    if(!/^blob:/.test(url)){window.open(url,"_blank","noopener");return;}
    var o=document.createElement("div");o.className="vs-ov";o.style.zIndex="1001";
    var box=document.createElement("div");box.className="vs-box";
    var h=kopf("Flyer-Vorschau","Noch nicht hochgeladen – wird erst beim Speichern übertragen.");
    h.z.onclick=function(){o.remove();};
    var inh=document.createElement("div");inh.className="vs-flyer";
    if(/pdf/i.test(typen[url]||"")){var f=document.createElement("iframe");f.src=url;inh.appendChild(f);}
    else{var img=document.createElement("img");img.src=url;img.alt="Flyer";inh.appendChild(img);}
    box.appendChild(h.k);box.appendChild(inh);o.appendChild(box);document.body.appendChild(o);
  }
  window.addEventListener("message",function(e){
    if(e.origin!==location.origin||!frame||e.source!==frame.contentWindow||!e.data)return;
    if(e.data.typ==="vko-vorschau-bereit"&&daten)frame.contentWindow.postMessage(daten,location.origin);
    else if(e.data.typ==="vko-vorschau-flyer"&&typeof e.data.url==="string")flyerZeigen(e.data.url);
  });
  btn.addEventListener("click",function(){
    schliessen();
    var d=sammeln();
    if(typeof d==="string"){msg.textContent=d;msg.style.display="block";return;}
    msg.style.display="none";daten=d;
    ov=document.createElement("div");ov.className="vs-ov";
    var box=document.createElement("div");box.className="vs-box";
    var h=kopf("Vorschau","So erscheint der Termin im Kalender – noch nicht gespeichert. Termin antippen zeigt die Beschreibung.");
    h.z.onclick=schliessen;
    frame=document.createElement("iframe");frame.title="Vorschau";frame.src="/?vorschau=1";
    box.appendChild(h.k);box.appendChild(frame);ov.appendChild(box);document.body.appendChild(ov);
  });
  document.addEventListener("keydown",function(e){if(e.key==="Escape"&&ov&&!document.querySelectorAll(".vs-ov")[1])schliessen();});
})();
</script>"""


def _runde_ziel(user) -> int | None:
    """Parameter/Feld `runde`: Formular wurde aus einer Planungsrunde geöffnet, in der der Verein dabei ist."""
    from shared.planung_db import aktive_teilnehmer
    rid = request.values.get("runde", "")
    if rid.isdigit() and user.get("verein_key") in aktive_teilnehmer(int(rid)):
        return int(rid)
    return None


def _nach_speichern(user, meldung: str):
    """Zurück zur Runde, aus der das Formular kam, sonst zu „Termine“ – mit Rückmeldung."""
    from urllib.parse import quote
    rid = _runde_ziel(user)
    ziel = f"/verein/runde/{rid}" if rid else "/verein/termine"
    return redirect(f"{ziel}?meldung={quote(meldung)}")


def _zurueck_link(user) -> str:
    rid = _runde_ziel(user)
    if rid:
        return f'<a class="btn btn-sec" href="/verein/runde/{rid}" style="margin-top:.75rem">← Zurück zur Planungsrunde</a>'
    return _BACK_DASH


def _verlauf(user, datum: str, aktion: str, details: str) -> None:
    """Terminänderung im Verlauf offener Planungsrunden des Vereins für dieses Jahr (ADR-026)."""
    from services.verein.planung import verlauf
    try:
        verlauf(user["verein_key"], int(datum[:4]), aktion, details)
    except (ValueError, TypeError):
        pass


@verein_bp.route("/verein/termine/neu", methods=["GET", "POST"])
@require_verein_login
def termin_neu(user):
    if user["role"] != "admin":
        return redirect("/verein/termine")

    error = ""
    # Aus „Termine“ mit Jahr-Filter oder aus einer Planungsrunde: Datum leer statt heute, wenn es ein anderes Jahr ist
    jahr = request.values.get("jahr", "")
    vorbelegt = date.today().isoformat() if not jahr.isdigit() or int(jahr) == date.today().year else ""
    f = {"datum": vorbelegt, "datum_bis": "", "uhrzeit": "", "uhrzeit_bis": "",
         "bezeichnung": "", "ort": ""}
    beschreibungen = [""]
    runde_id = _runde_ziel(user)
    if request.method == "POST":
        if not validate_csrf():
            return _page("Fehler", '<p class="err">Ungültige Anfrage. Bitte Seite neu laden.</p>'), 403
        als_entwurf = request.form.get("aktion") == "entwurf"
        f = {k: request.form.get(k, "").strip() for k in f}
        datum, datum_bis = f["datum"], f["datum_bis"]
        tage = []
        if not datum or not f["bezeichnung"]:
            error = "Datum und Bezeichnung sind Pflichtfelder."
        elif not re.match(r"^\d{4}-\d{2}-\d{2}$", datum) or (datum_bis and not re.match(r"^\d{4}-\d{2}-\d{2}$", datum_bis)):
            error = "Datum muss im Format YYYY-MM-DD sein."
        else:
            try:
                start = date.fromisoformat(datum)
                ende = date.fromisoformat(datum_bis) if datum_bis else start
            except ValueError:
                start = ende = None
                error = "Ungültiges Datum."
            if not error:
                if ende < start:
                    error = "Das bis-Datum liegt vor dem Startdatum."
                elif (ende - start).days + 1 > _MAX_TAGE:
                    error = f"Höchstens {_MAX_TAGE} Tage auf einmal."
                else:
                    tage = [(start + timedelta(days=i)).isoformat() for i in range((ende - start).days + 1)]
        if not error:
            error = _zeit_fehler(f["uhrzeit"], f["uhrzeit_bis"])
        n = max(len(tage), 1)
        beschreibungen = [request.form.get(f"beschreibung_{i}", "").strip() for i in range(n)]
        if not error and any(len(b) > _BESCHREIBUNG_MAX for b in beschreibungen):
            error = f"Beschreibung höchstens {_BESCHREIBUNG_MAX} Zeichen."
        if not error and (len(f["bezeichnung"]) > 200 or len(f["ort"]) > 200):
            error = "Bezeichnung und Ort höchstens 200 Zeichen."
        if not error and als_entwurf and any(d.filename for d in request.files.values()):
            # Flyer liegen per Dropbox-Link öffentlich – ein Entwurf ist es nicht (ADR-026)
            error = "Flyer könnt ihr erst nach dem Veröffentlichen beim Termin ergänzen. Bitte ohne Flyer als Entwurf speichern."
        if not error and als_entwurf:
            from shared.planung_db import entwurf_neu
            for i, tag in enumerate(tage):
                entwurf_neu(user["verein_key"], {**f, "datum": tag, "beschreibung": beschreibungen[i],
                                                 "uhrzeit_bis": f["uhrzeit_bis"]})
            _verlauf(user, tage[0], "Termin ergänzt", f"{f['bezeichnung']}: {_tag_label(tage[0])}"
                     + (f" bis {_tag_label(tage[-1])}" if len(tage) > 1 else ""))
            n = len(tage)
            return _nach_speichern(user, f"{f['bezeichnung']}: {n} {'Tag' if n == 1 else 'Tage'} als Entwurf gespeichert.")

        # Erst alle Flyer prüfen, dann hochladen – kein halb angelegter Termin-Satz
        flyer_bytes = {}
        if not error:
            for i in range(n):
                datei = request.files.get(f"flyer_{i}")
                if datei and datei.filename:
                    inhalt = datei.read()
                    try:
                        pruefe_flyer(inhalt)
                    except ValueError as e:
                        error = (f"Flyer Tag {i + 1}: {e}" if n > 1 else str(e))
                        break
                    flyer_bytes[i] = inhalt
        flyer = {}
        if not error:
            try:
                for i, inhalt in flyer_bytes.items():
                    flyer[i] = upload_flyer(inhalt)
            except Exception:
                for _, pfad in flyer.values():
                    delete_flyer(pfad)
                error = "Flyer-Upload fehlgeschlagen. Bitte erneut versuchen."
        if not error:
            jetzt = datetime.utcnow().isoformat()[:19]
            neue = []
            for i, tag in enumerate(tage):
                t = {
                    "id": str(uuid.uuid4())[:8],
                    "datum": tag,
                    "uhrzeit": f["uhrzeit"],
                    "bezeichnung": f["bezeichnung"],
                    "ort": f["ort"],
                    "erstellt_von": user["email"],
                    "erstellt_am": jetzt,
                }
                if f["uhrzeit_bis"]:
                    t["uhrzeit_bis"] = f["uhrzeit_bis"]
                if beschreibungen[i]:
                    t["beschreibung"] = beschreibungen[i]
                if i in flyer:
                    t["flyer_url"], t["flyer_path"] = flyer[i]
                neue.append(t)
            verein_key = user["verein_key"]

            def updater(data):
                if verein_key not in data:
                    data[verein_key] = []
                    data["_labels"] = data.get("_labels", {})
                    data["_labels"][verein_key] = user["verein_name"]
                data[verein_key].extend(neue)
                data.setdefault("_meta", {}).setdefault(verein_key, {})["selbstverwaltung"] = True
                return data

            try:
                KalenderStore.update(updater)
            except Exception:
                for _, pfad in flyer.values():
                    delete_flyer(pfad)
                raise
            for t in neue:
                log_audit("erstellt", t["id"], verein_key, user["id"])
            _verlauf(user, tage[0], "Termin veröffentlicht", f"{f['bezeichnung']}: {_tag_label(tage[0])}"
                     + (f" bis {_tag_label(tage[-1])}" if len(tage) > 1 else ""))
            from services.verein.planung import cache_leeren
            cache_leeren()
            n = len(neue)
            return _nach_speichern(user, f"{f['bezeichnung']}: {n} {'Tag' if n == 1 else 'Tage'} im Kalender.")
        if request.files and any(d.filename for d in request.files.values()):
            error += " Ausgewählte Flyer bitte erneut auswählen."

    mehrtaegig = len(beschreibungen) > 1  # nur nach gültigem Datumsbereich (Fehler-Rerender)
    start = date.fromisoformat(f["datum"]) if mehrtaegig else None
    tage_html = "".join(_tag_block(i, (start + timedelta(days=i)).isoformat() if mehrtaegig else f["datum"], b, mehrtaegig)
                        for i, b in enumerate(beschreibungen))
    e = html.escape
    tok = get_csrf_token()
    form = f"""
{'<p class="err">'+e(error)+'</p>' if error else ''}
<form id="termin-form" class="kollision-pruefen" method="post" enctype="multipart/form-data" autocomplete="off" data-verein-key="{e(user.get('verein_key',''))}" data-verein-name="{e(user.get('verein_name',''))}">
  {csrf_field(tok)}
  {f'<input type="hidden" name="runde" value="{runde_id}">' if runde_id else ''}
  <label>Datum *</label>
  <input name="datum" type="date" required value="{e(f['datum'])}">
  <label>bis Datum (optional)</label>
  <p class="hint">Für mehrtägige Veranstaltungen – je Tag wird ein eigener Eintrag angelegt (max. {_MAX_TAGE} Tage), jeweils mit eigener Beschreibung und eigenem Flyer.</p>
  <input name="datum_bis" type="date" value="{e(f['datum_bis'])}">
  <label>Uhrzeit</label>
  <input name="uhrzeit" type="time" value="{e(f['uhrzeit'])}">
  <label>bis Uhrzeit (optional)</label>
  <p class="hint">Bei Eingabe einer bis-Uhrzeit wird diese im Termin mit angezeigt (z.B. 19:00–23:00 Uhr). Eine frühere Uhrzeit als der Beginn bedeutet: Ende nach Mitternacht.</p>
  <input name="uhrzeit_bis" type="time" value="{e(f['uhrzeit_bis'])}">
  <label>Bezeichnung *</label>
  <input name="bezeichnung" type="text" required placeholder="z.B. Jahreshauptversammlung" value="{e(f['bezeichnung'])}">
  <label>Ort / Veranstaltungsort</label>
  <input name="ort" type="text" placeholder="z.B. Gasthaus zur Post" value="{e(f['ort'])}">
  <div class="kollision-hinweis" aria-live="polite"></div>
  <div id="tage">{tage_html}</div>
  <p id="tage-info" class="hint" style="color:#ff9f0a"></p>
  {_VORSCHAU_BTN}
  <button class="btn" type="submit" name="aktion" value="veroeffentlichen">Veröffentlichen</button>
  <button class="btn btn-sec" type="submit" name="aktion" value="entwurf">Als Entwurf speichern</button>
  <p class="hint">Entwurf = noch nicht öffentlich, z.&nbsp;B. für die Jahresplanung. Sehen können ihn nur ihr und die Vereine eurer Planungsrunden. Flyer erst nach dem Veröffentlichen.</p>
</form>
{_TAGE_JS % (_MAX_TAGE, _MAX_UPLOAD_MB * 1024 * 1024, _MAX_UPLOAD_MB)}
{_VORSCHAU_JS}
{KOLLISION_JS}
{_zurueck_link(user)}"""
    return _page("Neuer Termin", form)


# ── Termin bearbeiten / löschen ───────────────────────────────────────────────

@verein_bp.route("/verein/termine/<termin_id>", methods=["GET", "POST"])
@require_verein_login
def termin_edit(user, termin_id):
    if user["role"] != "admin":
        return redirect("/verein/termine")

    verein_key = user["verein_key"]
    data = _load_data()
    termine_list = data.get(verein_key, [])
    termin = next((t for t in termine_list if t.get("id") == termin_id and not t.get("geloescht") and not t.get("deleted")), None)

    if not termin:
        return redirect("/verein/termine")

    edit_error = ""
    if request.method == "POST":
        if not validate_csrf():
            return _page("Fehler", '<p class="err">Ungültige Anfrage. Bitte Seite neu laden.</p>'), 403
        aktion = request.form.get("aktion", "")
        if aktion == "loeschen":
            flyer_pfade = []

            def del_updater(d):
                for t in d.get(verein_key, []):
                    if t.get("id") == termin_id:
                        t["geloescht"] = True
                        t["geloescht_von"] = user["email"]
                        t["geloescht_am"] = datetime.utcnow().isoformat()[:19]
                        # Flyer ist per Dropbox-Link öffentlich – mit dem Termin entfernen
                        if t.get("flyer_path"):
                            flyer_pfade.append(t.pop("flyer_path"))
                        t.pop("flyer_url", None)
                return d
            KalenderStore.update(del_updater)
            for pfad in flyer_pfade:
                delete_flyer(pfad)
            log_audit("geloescht", termin_id, verein_key, user["id"])
            from shared.planung_db import kalender_termin_geloescht
            kalender_termin_geloescht(verein_key, termin_id)   # auch aus den Planungsrunden
            _verlauf(user, termin.get("datum", ""), "Termin gelöscht",
                     f"{termin.get('bezeichnung', '')}: {_tag_label(termin['datum']) if datum_ok(termin.get('datum', '')) else ''}")
            from services.verein.planung import cache_leeren
            cache_leeren()
            return _nach_speichern(user, f"{termin.get('bezeichnung', 'Termin')} gelöscht.")
        elif aktion == "flyer_entfernen":
            alter_pfad = termin.get("flyer_path", "")
            if alter_pfad:
                delete_flyer(alter_pfad)
            def flyer_del_updater(d):
                for t in d.get(verein_key, []):
                    if t.get("id") == termin_id:
                        t.pop("flyer_url", None)
                        t.pop("flyer_path", None)
                return d
            KalenderStore.update(flyer_del_updater)
            log_audit("flyer_entfernt", termin_id, verein_key, user["id"])
            rid = _runde_ziel(user)
            return redirect(f"/verein/termine/{termin_id}" + (f"?runde={rid}" if rid else ""))
        else:
            datum = request.form.get("datum", "").strip()
            uhrzeit = request.form.get("uhrzeit", "").strip()
            bezeichnung = request.form.get("bezeichnung", "").strip()
            ort = request.form.get("ort", "").strip()
            uhrzeit_bis = request.form.get("uhrzeit_bis", "").strip()
            beschreibung = request.form.get("beschreibung", "").strip()
            if not datum or not bezeichnung:
                edit_error = "Datum und Bezeichnung sind Pflichtfelder."
            elif not datum_ok(datum):
                edit_error = "Bitte ein gültiges Datum angeben."
            else:
                edit_error = _zeit_fehler(uhrzeit, uhrzeit_bis)
            if not edit_error and len(beschreibung) > _BESCHREIBUNG_MAX:
                edit_error = f"Beschreibung höchstens {_BESCHREIBUNG_MAX} Zeichen."
            new_flyer_url = ""
            new_flyer_path = ""
            flyer_file = request.files.get("flyer")
            if not edit_error and flyer_file and flyer_file.filename:
                try:
                    new_flyer_url, new_flyer_path = upload_flyer(flyer_file.read())
                except ValueError as e:
                    edit_error = str(e)
                except Exception:
                    edit_error = "Flyer-Upload fehlgeschlagen. Bitte erneut versuchen."
            if not edit_error:
                alter_pfad = termin.get("flyer_path", "") if new_flyer_url else ""
                def edit_updater(d):
                    for t in d.get(verein_key, []):
                        if t.get("id") == termin_id:
                            t["datum"] = datum
                            t["uhrzeit"] = uhrzeit
                            t["bezeichnung"] = bezeichnung
                            t["ort"] = ort
                            for feld, wert in (("uhrzeit_bis", uhrzeit_bis), ("beschreibung", beschreibung)):
                                if wert:
                                    t[feld] = wert
                                else:
                                    t.pop(feld, None)
                            t["geaendert_von"] = user["email"]
                            t["geaendert_am"] = datetime.utcnow().isoformat()[:19]
                            if new_flyer_url:
                                t["flyer_url"] = new_flyer_url
                                t["flyer_path"] = new_flyer_path
                    return d
                try:
                    KalenderStore.update(edit_updater)
                except Exception:
                    if new_flyer_path:
                        delete_flyer(new_flyer_path)
                    raise
                if alter_pfad:
                    delete_flyer(alter_pfad)
                log_audit("geaendert", termin_id, verein_key, user["id"])
                aenderung = []
                if datum != termin.get("datum"):
                    aenderung.append(f"{_tag_label(termin['datum']) if datum_ok(termin.get('datum', '')) else termin.get('datum', '')} → {_tag_label(datum)}")
                if uhrzeit != termin.get("uhrzeit", ""):
                    aenderung.append(f"{termin.get('uhrzeit') or 'ohne Uhrzeit'} → {uhrzeit or 'ohne Uhrzeit'}")
                if bezeichnung != termin.get("bezeichnung"):
                    aenderung.append(f"neuer Titel „{bezeichnung}“")
                if ort != termin.get("ort", ""):
                    aenderung.append("Ort geändert")
                if aenderung:
                    _verlauf(user, datum, "Termin geändert", f"{termin.get('bezeichnung', '')}: " + ", ".join(aenderung))
                from services.verein.planung import cache_leeren
                cache_leeren()
                return _nach_speichern(user, f"{bezeichnung} gespeichert.")
            termin = {**termin, "datum": datum, "uhrzeit": uhrzeit, "uhrzeit_bis": uhrzeit_bis,
                      "bezeichnung": bezeichnung, "ort": ort, "beschreibung": beschreibung}

    tok = get_csrf_token()
    rid = _runde_ziel(user)
    runde_feld = f'<input type="hidden" name="runde" value="{rid}">' if rid else ""
    flyer_url = termin.get("flyer_url", "")
    flyer_section = ""
    if flyer_url:
        flyer_section = f"""<div style="margin-bottom:.75rem">
  <label>Aktueller Flyer</label>
  <div style="display:flex;gap:8px;align-items:center">
    <a href="{html.escape(flyer_url)}" target="_blank" rel="noopener" class="btn btn-sec" style="font-size:13px">Flyer öffnen</a>
    <!-- kein eigenes <form>: steht im Hauptformular, verschachtelte Formulare verwirft der Browser (samt onsubmit) -->
    <button class="btn btn-danger" type="submit" name="aktion" value="flyer_entfernen" formnovalidate style="font-size:13px" onclick="return confirm('Flyer wirklich entfernen?')">Entfernen</button>
  </div>
</div>"""
    form = f"""
{'<p class="err">'+html.escape(edit_error)+'</p>' if edit_error else ''}
<form id="termin-form" class="kollision-pruefen" data-sofort="1" data-termin-id="{html.escape(termin_id)}" method="post" enctype="multipart/form-data" data-verein-key="{html.escape(verein_key)}" data-verein-name="{html.escape(user.get('verein_name',''))}" data-flyer-url="{html.escape(flyer_url)}">
  {csrf_field(tok)}
  {runde_feld}
  <label>Datum</label>
  <input name="datum" type="date" required value="{html.escape(termin.get('datum',''))}">
  <label>Uhrzeit</label>
  <input name="uhrzeit" type="time" value="{html.escape(termin.get('uhrzeit',''))}">
  <label>bis Uhrzeit (optional)</label>
  <p class="hint">Bei Eingabe einer bis-Uhrzeit wird diese im Termin mit angezeigt. Eine frühere Uhrzeit als der Beginn bedeutet: Ende nach Mitternacht.</p>
  <input name="uhrzeit_bis" type="time" value="{html.escape(termin.get('uhrzeit_bis',''))}">
  <label>Bezeichnung</label>
  <input name="bezeichnung" type="text" required value="{html.escape(termin.get('bezeichnung',''))}">
  <label>Ort</label>
  <input name="ort" type="text" value="{html.escape(termin.get('ort',''))}">
  <div class="kollision-hinweis" aria-live="polite"></div>
  <label>Beschreibung (optional)</label>
  <textarea name="beschreibung" rows="3" maxlength="{_BESCHREIBUNG_MAX}" placeholder="Wird beim Antippen des Termins angezeigt">{html.escape(termin.get('beschreibung',''))}</textarea>
  {flyer_section}
  <label>{'Flyer ersetzen' if flyer_url else 'Flyer hochladen'} (PDF/JPG/PNG/WebP, max. 8 MB)</label>
  {_FLYER_HINWEIS}
  <input name="flyer" type="file" accept="{_FLYER_ACCEPT}">
  {_VORSCHAU_BTN}
  <button class="btn" type="submit" name="aktion" value="speichern">Änderungen speichern</button>
</form>
{_VORSCHAU_JS}
{KOLLISION_JS}
<hr>
<form method="post" onsubmit="return confirm('Termin wirklich löschen?')">
  {csrf_field(tok)}
  {runde_feld}
  <button class="btn btn-danger" type="submit" name="aktion" value="loeschen">Termin löschen</button>
</form>
{_zurueck_link(user)}"""
    return _page("Termin bearbeiten", form)


# ── Passwort ändern ───────────────────────────────────────────────────────────

@verein_bp.route("/verein/passwort", methods=["GET", "POST"])
@require_verein_login
def change_password(user):
    from services.auth.routes import _hash_pw, _check_pw
    error = ""
    success = ""
    if request.method == "POST":
        if not validate_csrf():
            return _page("Fehler", '<p class="err">Ungültige Anfrage. Bitte Seite neu laden.</p>'), 403
        alt = request.form.get("password_alt", "")
        neu = request.form.get("password_neu", "")
        neu2 = request.form.get("password_neu2", "")
        with db_conn() as conn:
            row = conn.execute(
                "SELECT password_hash FROM vk_users WHERE id=?", (user["id"],)
            ).fetchone()
            if not _check_pw(alt, row["password_hash"]):
                error = "Aktuelles Passwort falsch."
            elif len(neu) < 8:
                error = "Neues Passwort muss mindestens 8 Zeichen haben."
            elif neu != neu2:
                error = "Neue Passwörter stimmen nicht überein."
            else:
                conn.execute(
                    "UPDATE vk_users SET password_hash=? WHERE id=?",
                    (_hash_pw(neu), user["id"]),
                )
                delete_user_sessions(user["id"], ausser=_session_token(), conn=conn)
                success = "Passwort erfolgreich geändert. Andere Geräte sind abgemeldet."

    tok = get_csrf_token()
    form = f"""
{'<p class="err">'+error+'</p>' if error else ''}
{'<p class="ok">'+success+'</p>' if success else ''}
<form method="post" autocomplete="off">
  {csrf_field(tok)}
  <label>Aktuelles Passwort</label>
  <input name="password_alt" type="password" required autocomplete="current-password">
  <label>Neues Passwort</label>
  <input name="password_neu" type="password" required autocomplete="new-password">
  <label>Neues Passwort wiederholen</label>
  <input name="password_neu2" type="password" required autocomplete="new-password">
  <button class="btn" type="submit">Passwort ändern</button>
</form>
{_BACK_PROFIL if request.args.get("von") == "profil" else _BACK_EINST}"""
    return _page("Passwort ändern", form)


# ── Mitglieder ────────────────────────────────────────────────────────────────

@verein_bp.route("/verein/mitglieder", methods=["GET", "POST"])
@require_verein_login
def mitglieder(user):
    if user["role"] != "admin":
        return redirect("/verein/einstellungen")

    import secrets as _sec
    from shared.vk_mail import send_invite_email

    error = ""
    success = ""
    if request.method == "POST":
        if not validate_csrf():
            return _page("Fehler", '<p class="err">Ungültige Anfrage. Bitte Seite neu laden.</p>'), 403
        aktion = request.form.get("aktion", "")
        if aktion == "einladen":
            from services.auth.routes import _valid_email
            email = request.form.get("email", "").strip().lower()
            if not _valid_email(email):
                error = "Bitte eine gültige E-Mail-Adresse eingeben."
        if aktion == "einladen" and not error:
            with db_conn() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM vk_users WHERE verein_id=?", (user["verein_id"],)
                ).fetchone()[0]
                if count >= 3:
                    error = "Maximal 3 Accounts pro Verein (Admin + 2 Mitglieder)."
                else:
                    # Eine E-Mail darf mehrere Vereine haben (Login bietet dann die Auswahl) –
                    # gesperrt ist nur ein zweiter Account im selben Verein
                    ex = conn.execute(
                        "SELECT id FROM vk_users WHERE email=? AND verein_id=?", (email, user["verein_id"])
                    ).fetchone()
                    if ex:
                        error = "Diese E-Mail ist in eurem Verein bereits registriert."
                    else:
                        import bcrypt as _bc
                        token = _sec.token_urlsafe(32)
                        expires = (datetime.utcnow() + timedelta(hours=48)).isoformat()
                        tmp_hash = _bc.hashpw(_sec.token_hex(16).encode(), _bc.gensalt()).decode()
                        conn.execute(
                            """INSERT INTO vk_users
                               (email, password_hash, verein_id, role,
                                einladungs_token, einladungs_expires, email_verified, aktiv)
                               VALUES (?,?,?,'member',?,?,1,0)""",
                            (email, tmp_hash, user["verein_id"], token, expires),
                        )
                        ich = conn.execute("SELECT vorname, nachname, name FROM vk_users WHERE id=?",
                                           (user["id"],)).fetchone()
                        von = " ".join(x for x in ((ich["vorname"] or "").strip(), (ich["nachname"] or "").strip()) if x) \
                            or (ich["name"] or "").strip()
                        send_invite_email(email, token, user["verein_name"], von)
                        success = f"Einladung an {html.escape(email)} verschickt."
        elif aktion == "vorstand":
            # Vorstand markieren/zurücknehmen (v1.83): sieht Dokumente „nur Vorstand“, sonst lesend wie Mitglied
            try:
                member_id = int(request.form.get("member_id", 0))
            except ValueError:
                member_id = 0
            wert = 1 if request.form.get("wert") == "1" else 0
            with db_conn() as conn:
                n = conn.execute("UPDATE vk_users SET vorstand=? WHERE id=? AND verein_id=? AND role='member'",
                                 (wert, member_id, user["verein_id"])).rowcount
            if n:
                log_audit("vorstand_gesetzt" if wert else "vorstand_entfernt", f"user_{member_id}",
                          user["verein_key"] or "", user["id"])
                success = "Als Vorstand markiert." if wert else "Vorstand-Markierung entfernt."
        elif aktion == "entfernen":
            try:
                member_id = int(request.form.get("member_id", 0))
            except ValueError:
                member_id = 0
            with db_conn() as conn:
                ist_mitglied = conn.execute(
                    "SELECT 1 FROM vk_users WHERE id=? AND verein_id=? AND role='member'",
                    (member_id, user["verein_id"]),
                ).fetchone()
                if ist_mitglied:
                    # Fremdschlüssel: erst Sessions löschen, Audit behalten (ohne Benutzerbezug),
                    # sonst IntegrityError (Review 2026-10-04, Punkt 5)
                    delete_user_sessions(member_id, conn=conn)
                    conn.execute("UPDATE vk_audit SET user_id=NULL WHERE user_id=?", (member_id,))
                    conn.execute("DELETE FROM vk_users WHERE id=?", (member_id,))
                    success = "Mitglied entfernt."

    with db_conn() as conn:
        members = conn.execute(
            "SELECT id, email, role, aktiv, email_verified, vorstand FROM vk_users WHERE verein_id=? ORDER BY id",
            (user["verein_id"],),
        ).fetchall()

    tok = get_csrf_token()
    rows = ""
    for m in members:
        status = "aktiv" if m["aktiv"] else "Einladung ausstehend"
        remove_btn = ""
        rolle = "Vereinsadmin" if m["role"] == "admin" else ("Vorstand" if m["vorstand"] else "Mitglied")
        if m["role"] == "member":
            vorstand_btn = (f'<form method="post" style="display:inline">{csrf_field(tok)}<input type="hidden" name="aktion" value="vorstand">'
                            f'<input type="hidden" name="member_id" value="{m["id"]}"><input type="hidden" name="wert" value="{0 if m["vorstand"] else 1}">'
                            f'<button style="background:none;border:none;color:#a78bfa;cursor:pointer;font-size:.9rem;padding:6px 0" type="submit">'
                            f'{"Vorstand entfernen" if m["vorstand"] else "Zum Vorstand machen"}</button></form>')
            remove_btn = vorstand_btn + f'<form method="post" style="display:inline">{csrf_field(tok)}<input type="hidden" name="aktion" value="entfernen"><input type="hidden" name="member_id" value="{m["id"]}"><button style="background:none;border:none;color:#ff453a;cursor:pointer;font-size:.9rem;padding:6px 0" type="submit">Entfernen</button></form>'
        rows += f'<div class="card"><div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap"><div style="min-width:0;overflow-wrap:anywhere"><div>{html.escape(m["email"])}</div><div style="color:#aeaeb2;font-size:.82rem">{rolle} · {status}</div></div><div style="display:flex;gap:14px;align-items:center">{remove_btn}</div></div></div>'

    invite_form = ""
    if len(members) < 3:
        invite_form = f"""
{'<p class="err">'+error+'</p>' if error else ''}
{'<p class="ok">'+success+'</p>' if success else ''}
<form method="post">
  {csrf_field(tok)}
  <input type="hidden" name="aktion" value="einladen">
  <label>E-Mail-Adresse des neuen Mitglieds</label>
  <input name="email" type="email" required placeholder="mitglied@beispiel.de">
  <button class="btn" type="submit">Einladung verschicken</button>
</form>
<div class="spam-hint">Bitte Eingeladene auf den Spam-Ordner hinweisen.</div>"""
    else:
        invite_form = '<p class="hint">Maximale Anzahl (3) erreicht.</p>'
        if success:
            invite_form = f'<p class="ok">{success}</p>' + invite_form

    body = f"""
<h2 style="font-size:1rem;margin:0 0 .5rem">Mitglieder ({len(members)}/3)</h2>
{rows}
<hr>
{invite_form}
{_BACK_EINST}"""
    return _page("Mitglieder", body)


# ── Einladung annehmen ────────────────────────────────────────────────────────

@verein_bp.route("/verein/einladung", methods=["GET", "POST"])
def einladung():
    from services.auth.routes import _hash_pw
    token = request.args.get("token", "") or request.form.get("token", "")
    error = ""
    with db_conn() as conn:
        row = conn.execute(
            "SELECT id, einladungs_expires, aktiv FROM vk_users WHERE einladungs_token=?",
            (token,),
        ).fetchone()
        if not row:
            return _page("Ungültig", '<p class="err">Ungültiger Einladungslink.</p>'), 400
        if datetime.fromisoformat(row["einladungs_expires"]) < datetime.utcnow():
            return _page("Abgelaufen", '<p class="err">Der Einladungslink ist abgelaufen. Bitte den Vereinsadmin um eine neue Einladung bitten.</p>'), 400
        if row["aktiv"]:
            return redirect("/verein/login")

        if request.method == "POST":
            if not validate_csrf():
                return _page("Fehler", '<p class="err">Ungültige Anfrage. Bitte Seite neu laden.</p>'), 403
            pw = request.form.get("password", "")
            pw2 = request.form.get("password2", "")
            if len(pw) < 8:
                error = "Passwort muss mindestens 8 Zeichen haben."
            elif pw != pw2:
                error = "Passwörter stimmen nicht überein."
            elif not request.form.get(DS_FELD):
                error = DS_FEHLER
            else:
                conn.execute(
                    """UPDATE vk_users SET password_hash=?, aktiv=1,
                       einladungs_token=NULL, einladungs_expires=NULL,
                       ds_fassung=?, ds_bestaetigt_am=CURRENT_TIMESTAMP WHERE id=?""",
                    (_hash_pw(pw), DS_FASSUNG, row["id"]),
                )
                return redirect("/verein/login")

    tok = get_csrf_token()
    form = f"""
<p>Lege dein Passwort fest, um die Einladung anzunehmen.</p>
{'<p class="err">'+error+'</p>' if error else ''}
<form method="post">
  {csrf_field(tok)}
  <input type="hidden" name="token" value="{html.escape(token)}">
  <label>Passwort <span class="hint">(mind. 8 Zeichen)</span></label>
  <input name="password" type="password" required autocomplete="new-password">
  <label>Passwort wiederholen</label>
  <input name="password2" type="password" required autocomplete="new-password">
  {DS_KAESTCHEN}
  <button class="btn" type="submit">Einladung annehmen</button>
</form>"""
    return _page("Einladung annehmen", form)


# ── Upload-Template ──────────────────────────────────────────────────────────

@verein_bp.route("/verein/upload-template")
@require_verein_login
def upload_template(user):
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Termine"

    header_labels = ["Datum *", "Uhrzeit", "Bezeichnung *", "Ort", "Ortschaft"]
    hfill = PatternFill("solid", fgColor="6D28D9")
    hfont = Font(bold=True, color="FFFFFF")
    for col, label in enumerate(header_labels, 1):
        c = ws.cell(row=1, column=col, value=label)
        c.fill = hfill
        c.font = hfont
        c.alignment = Alignment(horizontal="center")

    for row_data in [
        ("15.06.2026", "19:00", "Jahreshauptversammlung", "GH Zur Post, Hölskofen", "Hölskofen"),
        ("22.06.2026", "",      "Sommerfest",             "Festplatz Postau",       "Postau"),
    ]:
        ws.append(row_data)

    for col, w in zip("ABCDE", [14, 10, 35, 30, 18]):
        ws.column_dimensions[col].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    resp = make_response(buf.read())
    resp.headers["Content-Type"] = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    resp.headers["Content-Disposition"] = "attachment; filename=terminplan-vorlage.xlsx"
    return resp


# ── Upload-Seite (GET) ────────────────────────────────────────────────────────

@verein_bp.route("/verein/upload", methods=["GET"])
@require_verein_login
def upload_page(user):
    if user["role"] != "admin":
        return redirect("/verein/termine")

    remaining = _quota_remaining(user["verein_id"])
    used = _UPLOAD_LIMIT - remaining

    if remaining == 0:
        body = (
            f'<p class="err">Tageslimit erreicht ({_UPLOAD_LIMIT}/{_UPLOAD_LIMIT} Uploads heute). '
            f'Morgen wieder verfügbar.</p>{_BACK_DASH}'
        )
        return _page("Terminplan hochladen", body)

    quota_bar = (
        f'<p class="hint" style="margin-bottom:1rem">'
        f'Uploads heute: {used}/{_UPLOAD_LIMIT}</p>'
    )
    tok = get_csrf_token()
    body = f"""{quota_bar}
<div class="card">
  <h2 style="font-size:.95rem;margin-top:0">PDF oder Foto</h2>
  <p class="hint">Claude KI extrahiert die Termine automatisch aus dem Dokument.</p>
  <form method="post" action="/verein/upload" enctype="multipart/form-data">
    {csrf_field(tok)}
    <input type="hidden" name="typ" value="vision">
    <label>Datei (PDF, JPG, PNG, HEIC, max. {_MAX_PLAN_MB} MB)</label>
    <input type="file" name="file" required accept=".pdf,.jpg,.jpeg,.png,.heic,.heif">
    <button class="btn" type="submit">Hochladen &amp; analysieren</button>
  </form>
</div>
<div class="card">
  <h2 style="font-size:.95rem;margin-top:0">Excel-Tabelle</h2>
  <p class="hint">Trage Termine in die Vorlage ein und lade sie hoch – ohne KI, keine Extraktion.</p>
  <a class="btn btn-sec" href="/verein/upload-template"
     style="margin-bottom:.75rem">Vorlage herunterladen (.xlsx)</a>
  <form method="post" action="/verein/upload" enctype="multipart/form-data">
    {csrf_field(tok)}
    <input type="hidden" name="typ" value="excel">
    <label>Ausgefüllte Excel-Datei (.xlsx, max. {_MAX_PLAN_MB} MB)</label>
    <input type="file" name="file" required accept=".xlsx">
    <button class="btn" type="submit">Termine importieren</button>
  </form>
</div>
{_BACK_DASH}"""
    return _page(f"Terminplan hochladen – {user['verein_name']}", body)


# ── Upload verarbeiten (POST) ─────────────────────────────────────────────────

@verein_bp.route("/verein/upload", methods=["POST"])
@require_verein_login
def upload_process(user):
    if user["role"] != "admin":
        return redirect("/verein/termine")
    if not validate_csrf():
        return _page("Fehler", '<p class="err">Ungültige Anfrage. Bitte Seite neu laden.</p>'), 403

    verein_name = user["verein_name"]
    verein_key  = user["verein_key"]
    verein_id   = user["verein_id"]
    heute       = date.today().isoformat()

    if _quota_remaining(verein_id) <= 0:
        body = (
            f'<p class="err">Tageslimit erreicht ({_UPLOAD_LIMIT}/{_UPLOAD_LIMIT} Uploads heute).</p>'
            + _BACK_DASH
        )
        return _page("Limit erreicht", body), 429

    if "file" not in request.files:
        return redirect("/verein/upload")

    f      = request.files["file"]
    fname  = (f.filename or "").lower()
    suffix = Path(fname).suffix if fname else ""
    typ    = request.form.get("typ", "vision")

    if typ == "excel":
        if suffix != ".xlsx":
            body = '<p class="err">Nur .xlsx-Dateien erlaubt.</p>' + _BACK_DASH
            return _page("Fehler", body), 400
    else:
        allowed = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif"}
        if suffix not in allowed:
            body = '<p class="err">Nur PDF oder Bilder (JPG, PNG, HEIC) erlaubt.</p>' + _BACK_DASH
            return _page("Fehler", body), 400
        if suffix in {".heic", ".heif"} and not _HEIC_SUPPORTED:
            body = '<p class="err">HEIC-Format auf diesem Server nicht verfügbar.</p>' + _BACK_DASH
            return _page("Fehler", body), 400

    inhalt = f.read()
    if len(inhalt) > _MAX_PLAN_MB * 1024 * 1024:
        body = f'<p class="err">Datei zu groß (max. {_MAX_PLAN_MB} MB). Tipp: PDF verkleinern oder einzelne Seiten als Foto hochladen.</p>' + _BACK_DASH
        return _page("Fehler", body), 413

    # Quota jetzt erhöhen (vor dem eigentlichen Parse/Claude-Call, um Missbrauch zu verhindern) –
    # aber erst nach der Format-Validierung, damit ein falsches Dateiformat keinen Slot kostet
    increment_upload_quota(verein_id, heute)

    auto_plz = ""
    alle: list = []

    if typ == "excel":
        try:
            alle = parse_excel_bytes(inhalt)
        except Exception as ex:
            body = f'<p class="err">Fehler beim Lesen der Excel-Datei: {html.escape(str(ex))}</p>' + _BACK_DASH
            return _page("Fehler", body), 400
    else:
        try:
            result   = import_pdf_bytes(inhalt, suffix)
            alle     = result["alle"]
            auto_plz = result.get("auto_plz", "")
        except Exception as ex:
            body = f'<p class="err">Fehler bei der KI-Analyse: {html.escape(str(ex))}</p>' + _BACK_DASH
            return _page("Fehler", body), 500

    if not alle:
        body = '<p class="err">Keine Termine gefunden.</p>' + _BACK_DASH
        return _page("Keine Termine", body)

    # Alle Termine diesem Verein zuordnen
    for t in alle:
        t["verein"] = verein_name
        t["quelle"] = verein_name

    _, total = _do_save_import(alle, auto_plz, "", verein_key=verein_key)
    def _sv_up(d): d.setdefault("_meta", {}).setdefault(verein_key, {})["selbstverwaltung"] = True; return d
    KalenderStore.update(_sv_up)
    log_audit("upload", f"bulk_{total}", verein_key, user["id"], anzahl=total)
    return redirect(f"/verein/termine?upload_ok={total}")


# /verein/confirm-upload entfernt (Review 2026-10-04): Vereins-Uploads speichern direkt,
# Pending-Dateien entstehen nur beim Admin-/upload (ohne verein_id) – die Route war tot
# und ohne CSRF-Prüfung. Audit-Aktion `upload_confirmed` bleibt in kalender_report gezählt.


# ── Datenschutz / Nutzungsbedingungen ────────────────────────────────────────

# „Stand“ aus der Fassung, die jedes Konto bestätigt (`DS_FASSUNG`, `/verein/bestaetigen`)
_MONATE = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
           "November", "Dezember")
_STAND = f"{_MONATE[int(DS_FASSUNG[5:7]) - 1]} {DS_FASSUNG[:4]}"

@verein_bp.route("/verein/datenschutz")
def datenschutz():
    body = f"""
<p style="color:#aeaeb2;font-size:.85rem">Stand: {_STAND}</p>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">1. Verantwortlicher</h2>
<p>Josef Fischer, Hölskofen 13, 84092 Bayerbach b. Ergoldsbach · <a href="mailto:info@vereinskalender.online">info@vereinskalender.online</a></p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">2. Erhobene Daten</h2>
<p>Bei der Registrierung und im Vereinsprofil erfassen wir: Vereinsname, Rubrik, PLZ und Ortschaft des Vereins, Anrede, Vor- und Nachname, E-Mail-Adresse und Telefonnummer des Ansprechpartners sowie das Passwort (verschlüsselt gespeichert, niemals im Klartext). Öffentlich angezeigt werden nur Vereinsname, Rubrik, Ort und die Vereinstermine. Name, E-Mail-Adresse und Telefonnummer des Ansprechpartners sind nicht öffentlich; sie dienen der Anmeldung, der Anrede in E-Mails und Rückfragen des Betreibers.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">3. Zweck der Verarbeitung</h2>
<p>Die Daten dienen ausschließlich dem Betrieb des Vereinskalenders: Identifizierung des Vereins, Authentifizierung des Accounts und Benachrichtigungen (Bestätigungs-E-Mails).</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">2a. Hosting und Server-Protokolle</h2>
<p>Der Vereinskalender läuft auf einem Server der Hetzner Online GmbH im Rechenzentrum Helsinki (Finnland, EU). Bei jedem Aufruf speichert der Webserver IP-Adresse, Zeitpunkt, aufgerufene Seite und Browserkennung in einem Protokoll, um Fehler und Angriffe zu erkennen (berechtigtes Interesse, Art. 6 Abs. 1 lit. f DSGVO). Diese Protokolle werden nach 14 Tagen gelöscht.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">3a. Rechtsgrundlage</h2>
<p>Konto, Vereinsbereich, Planungsrunden und Vereinsdokumente verarbeiten wir zur Erfüllung des Nutzungsvertrags (Art. 6 Abs. 1 lit. b DSGVO) – ohne diese Daten ist die Nutzung nicht möglich. Die Telegram-Erinnerungen sind freiwillig (Art. 6 Abs. 1 lit. a DSGVO). Jedes Konto bestätigt bei der Registrierung bzw. beim ersten Aufruf des Vereinsbereichs, dass es diese Datenschutzerklärung gelesen hat und die Nutzungsbedingungen akzeptiert; dazu speichern wir Zeitpunkt und Fassung. Ändert sich die Erklärung wesentlich, wird erneut gefragt.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">4. Speicherdauer</h2>
<p>Accounts werden auf Anfrage gelöscht. Schreib dazu an <a href="mailto:info@vereinskalender.online">info@vereinskalender.online</a>. Eingetragene Termine werden nach Ende des jeweiligen Kalenderjahres bereinigt.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">3b. Benachrichtigung des Betreibers</h2>
<p>Bei einer neuen Registrierung erhält der Betreiber über den Messenger Telegram eine Nachricht mit Vereinsname, Ortschaft, Name, E-Mail-Adresse und Telefonnummer des Ansprechpartners, um die Anmeldung freizugeben. Scheitert eine E-Mail, meldet das System die Zieladresse ebenfalls dort.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">3c. Termine aus PDF oder Foto übernehmen</h2>
<p>Lädt ein Verein einen Terminplan als PDF oder Foto hoch, wird die Datei zur Texterkennung an die KI Claude der Anthropic PBC (USA) übermittelt. Anthropic verarbeitet die Datei nur zur Beantwortung und nutzt sie nicht zum Training; die Übermittlung in die USA stützt sich auf die Standardvertragsklauseln der EU-Kommission im Auftragsverarbeitungsvertrag von Anthropic. Bitte keine Dokumente mit Personendaten hochladen, die über die Termine hinausgehen. Vereinsdokumente (4b) werden nie an eine KI übermittelt.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">5. E-Mail-Dienst</h2>
<p>E-Mails werden über Mailjet (Sinch Mailjet SAS, Frankreich) versendet. Dabei werden die Ziel-E-Mail-Adresse und der Inhalt der Mail an Mailjet übermittelt. Öffnungen und Klicks auf Links werden nicht ausgewertet – die Links zeigen direkt auf vereinskalender.online.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">4a. Entwürfe und Planungsrunden</h2>
<p>Termine, die ein Verein als Entwurf speichert, sind nicht öffentlich. Sie sehen nur der Verein selbst und die Vereine einer Planungsrunde, der er beigetreten ist. In einer Planungsrunde wird ein Verlauf geführt (welcher Verein wann beigetreten ist, Termine geändert, bestätigt oder veröffentlicht hat) – nur mit Vereinsnamen und Terminen, ohne Personendaten. Schließt der Organisator die Runde ab, wird dieser Stand als Ergebnis für die beteiligten Vereine festgehalten.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">4b. Vereinsdokumente</h2>
<p>Im Vereinsbereich kann ein Verein Protokolle, seine Satzung und andere Unterlagen ablegen – als hochgeladene Datei oder direkt geschrieben. Diese Dokumente sind nicht öffentlich. Sie sehen nur die angemeldeten Admins und Mitglieder dieses Vereins (Dokumente „nur Vorstand“ nur Admins und als Vorstand markierte Mitglieder); anlegen, ändern und löschen können nur die Vereinsadmins. Die Dateien liegen auf dem Server des Vereinskalenders (Hetzner Online GmbH) und in der nächtlichen Sicherung, die zusätzlich verschlüsselt bei Dropbox liegt. Der Betreiber sieht die Inhalte nicht ein. Gelöschte Dokumente sind sofort weg, aus den Sicherungen nach spätestens 30 Tagen; wird das Vereinskonto gelöscht, werden alle Dokumente des Vereins mit gelöscht. Für den Inhalt – auch für Namen von Mitgliedern in Protokollen – ist der Verein verantwortlich; der Betreiber verarbeitet diese Daten in seinem Auftrag nach dem <a href="/verein/avv">AV-Vertrag</a>, den ein Vereinsadmin zum Freischalten des Bereichs abschließt (dort auch die Unterauftragnehmer). Wer ein Dokument angelegt, geändert, angesehen, heruntergeladen oder gelöscht hat, wird mit Konto und Zeitpunkt protokolliert und den Vereinsadmins angezeigt; diese Einträge werden nach 12 Monaten gelöscht.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">5a. Telegram-Terminerinnerungen (freiwillig)</h2>
<p>Wer den Telegram-Bot für Terminerinnerungen nutzt, speichert damit freiwillig seine Telegram-Chat-ID sowie die ausgewählten Vereins-Abonnements auf unserem Server. Diese Daten werden ausschließlich zum Versand der gewünschten Erinnerungen verwendet. Abmelden ist jederzeit mit dem Befehl /stop im Bot möglich – dabei werden alle gespeicherten Daten gelöscht. Eine Löschung ist auch per E-Mail möglich.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">6. Rechte</h2>
<p>Auskunft, Berichtigung, Löschung deiner Daten: Schreib an <a href="mailto:info@vereinskalender.online">info@vereinskalender.online</a>. Beschwerderecht bei der zuständigen Datenschutz-Aufsichtsbehörde.</p>
</div>
{_BACK_HISTORY}"""
    return _page("Datenschutzerklärung", body)


@verein_bp.route("/verein/avv")
def avv():
    """AV-Vertrag für den Dokumentenbereich (Art. 28 DSGVO, v1.83, ADR-032). Fassung = AVV_FASSUNG; der Abschluss
    läuft über `/verein/dokumente/freischalten`. Nur belegte Maßnahmen in Anlage 1 (am Server geprüft 2026-10-10)."""
    k = lambda t, h: f'<div class="card"><h2 style="font-size:1rem;margin-top:0">{t}</h2>{h}</div>'
    body = f"""
<p style="color:#aeaeb2;font-size:.85rem">Fassung {AVV_FASSUNG} · gilt für den Dokumentenbereich im Vereinsbereich</p>
{k("1. Vertragspartner und Gegenstand", "<p><b>Auftraggeber</b> ist der Verein, dessen Vereinsadmin diesen Vertrag im Vereinsbereich abschließt. <b>Auftragnehmer</b> ist Josef Fischer, Hölskofen 13, 84092 Bayerbach b. Ergoldsbach, info@vereinskalender.online (Betreiber von vereinskalender.online).</p><p>Der Auftragnehmer speichert für den Auftraggeber im Dokumentenbereich Unterlagen des Vereins (z. B. Protokolle, Satzung, sonstige Dokumente) und stellt sie den angemeldeten Konten des Vereins bereit (Ansehen, Herunterladen, Export). Der Vertrag gilt, solange der Dokumentenbereich genutzt wird bzw. das Vereinskonto besteht.</p>")}
{k("2. Art der Daten und Betroffene", "<p>Inhalte der abgelegten Dokumente, insbesondere Namen von Mitgliedern, Anwesenheiten, Funktionen, Beschlüsse und Abstimmungsergebnisse, sowie die Angaben, welches Konto wann ein Dokument angelegt, geändert, angesehen, heruntergeladen oder gelöscht hat. Betroffen sind Mitglieder, Organe und sonstige im Dokument genannte Personen des Auftraggebers sowie die Nutzer seiner Vereinskonten. Besondere Kategorien von Daten (Art. 9 DSGVO) sollen nicht abgelegt werden.</p>")}
{k("3. Weisungen", "<p>Der Auftragnehmer verarbeitet die Daten nur nach dokumentierter Weisung des Auftraggebers. Die Weisungen ergeben sich aus diesem Vertrag und aus den Aktionen der Vereinsadmins im Vereinsbereich (Anlegen, Ändern, Löschen, Sichtbarkeit). Weitere Weisungen erteilt der Auftraggeber per E-Mail an info@vereinskalender.online. Hält der Auftragnehmer eine Weisung für rechtswidrig, weist er den Auftraggeber darauf hin.</p>")}
{k("4. Pflichten des Auftragnehmers", "<ul><li>Er sieht die Inhalte nicht ein, außer der Auftraggeber verlangt es (z. B. zur Fehlersuche) oder es ist gesetzlich vorgeschrieben.</li><li>Personen mit Zugang zum Server sind zur Vertraulichkeit verpflichtet.</li><li>Er trifft die technischen und organisatorischen Maßnahmen nach Anlage 1 (Art. 32 DSGVO) und passt sie dem Stand der Technik an, ohne das Schutzniveau zu senken.</li><li>Er unterstützt den Auftraggeber bei Anfragen Betroffener (Auskunft, Berichtigung, Löschung) und bei seinen Pflichten nach Art. 32–36 DSGVO, soweit ihm das möglich ist.</li><li>Er meldet eine Verletzung des Schutzes der Daten unverzüglich, möglichst innerhalb von 48 Stunden nach Kenntnis, an die hinterlegte E-Mail-Adresse der Vereinsadmins.</li><li>Er stellt die zum Nachweis nötigen Informationen bereit und ermöglicht Überprüfungen nach vorheriger Absprache; in der Regel genügt eine schriftliche Auskunft.</li></ul>")}
{k("5. Unterauftragnehmer", "<p>Der Auftraggeber stimmt den Unterauftragnehmern in Anlage 2 zu. Über neue oder geänderte Unterauftragnehmer informiert der Auftragnehmer vorab durch eine neue Fassung dieses Vertrags; der Auftraggeber kann widersprechen, indem er den Dokumentenbereich nicht weiter nutzt und seine Dokumente löscht. Unterauftragnehmer werden vertraglich auf dasselbe Schutzniveau verpflichtet.</p>")}
{k("6. Löschung und Rückgabe", "<p>Der Auftraggeber kann jederzeit alle Dokumente herunterladen und löschen. Gelöschte Dokumente sind sofort entfernt und aus den Sicherungen nach spätestens 30 Tagen. Wird das Vereinskonto gelöscht, werden alle Dokumente mitgelöscht. Eine Pflicht zur Aufbewahrung über das Vertragsende hinaus übernimmt der Auftragnehmer nicht.</p>")}
{k("7. Haftung, Schluss", "<p>Es gilt Art. 82 DSGVO. Der Dienst ist kostenlos; ein Anspruch auf dauerhafte Aufbewahrung besteht nicht (siehe Nutzungsbedingungen). Abschluss in elektronischer Form (Art. 28 Abs. 9 DSGVO): Der Vereinsadmin bestätigt den Vertrag im Vereinsbereich; Zeitpunkt, Fassung und Konto werden gespeichert. Eine neue Fassung muss erneut bestätigt werden, bevor neue Dokumente angelegt werden können.</p>")}
{k("Anlage 1 – Technische und organisatorische Maßnahmen", "<ul><li><b>Verschlüsselte Übertragung:</b> nur HTTPS (TLS 1.2/1.3), HSTS.</li><li><b>Zugang:</b> Anmeldung mit E-Mail und Passwort (bcrypt-Hash), Sperre nach 5 Fehlversuchen für 15 Minuten, Sitzungen höchstens 8 Stunden, Cookies <i>Secure</i>/<i>HttpOnly</i>, Schutz gegen gefälschte Formularaufrufe (CSRF).</li><li><b>Trennung:</b> Jede Abfrage ist an den Verein der Sitzung gebunden; Dokumente anderer Vereine sind nicht erreichbar (automatisch getestet).</li><li><b>Rechte:</b> Anlegen, Ändern, Löschen nur durch Vereinsadmins; Mitglieder lesen. Dokumente „nur Vorstand“ sehen nur Vereinsadmins und als Vorstand markierte Mitglieder. Dateien werden nur nach Anmeldung ausgeliefert, ohne öffentlichen Link, nicht im Browser-Cache gespeichert.</li><li><b>Protokollierung:</b> Anlegen, Ändern, Ansehen, Herunterladen, Export und Löschen werden mit Konto und Zeitpunkt protokolliert, Aufbewahrung 12 Monate.</li><li><b>Server:</b> Rechenzentrum der Hetzner Online GmbH in Helsinki (Finnland, EU); Server-Zugang nur per SSH-Schlüssel, Firewall, automatische Sperre bei Angriffsversuchen (fail2ban); wöchentliche Prüfung der Software-Pakete auf bekannte Sicherheitslücken.</li><li><b>Sicherung:</b> nächtliche Sicherung, mit GPG verschlüsselt, 21 Tage auf dem Server und 30 Tage bei Dropbox; Dropbox erhält nur die verschlüsselte Datei.</li><li><b>Prüfung der Uploads:</b> Dateityp wird am Inhalt erkannt, nur PDF, Bilder und Office-Formate, höchstens 20 MB je Datei.</li><li><b>Nicht umgesetzt:</b> Die Festplatte des Servers ist nicht zusätzlich verschlüsselt.</li></ul>")}
{k("Anlage 2 – Unterauftragnehmer", "<ul><li><b>Hetzner Online GmbH</b>, Industriestr. 25, 91710 Gunzenhausen – Server und Speicherung, Rechenzentrum Helsinki (Finnland, EU).</li><li><b>Dropbox International Unlimited Company</b>, Dublin (Irland) – Ablage der verschlüsselten nächtlichen Sicherung; Dropbox kann die Inhalte nicht lesen.</li><li><b>Sinch Mailjet SAS</b>, Paris (Frankreich) – Versand von E-Mails an die Vereinskonten (enthalten keine Dokumentinhalte).</li><li><b>Sendinblue SAS (Brevo)</b>, Paris (Frankreich) – Ersatz für den E-Mail-Versand, falls Mailjet ausfällt.</li></ul>")}
<p class="hint">Dies ist ein Mustervertrag für einen kostenlosen Dienst. Er ersetzt keine Rechtsberatung.</p>
<button class="btn btn-sec" onclick="window.print()" style="margin-top:.5rem">Drucken / als PDF speichern</button>
{_BACK_HISTORY}"""
    return _page("AV-Vertrag Dokumentenbereich", body)


@verein_bp.route("/verein/nutzungsbedingungen")
def nutzungsbedingungen():
    body = f"""
<p style="color:#aeaeb2;font-size:.85rem">Stand: {_STAND}</p>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">1. Nutzung</h2>
<p>Der Vereinskalender vereinskalender.online dient der Veröffentlichung von Vereinsterminen und öffentlichen Veranstaltungen. Die Nutzung ist kostenlos.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">2. Registrierung</h2>
<p>Gaststätten dürfen öffentliche Veranstaltungen eintragen. Wer sich registriert, trägt die Verantwortung für die Richtigkeit seiner Daten.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">3. Haftungsausschluss</h2>
<p>Der Betreiber übernimmt keine Gewähr für die Richtigkeit der eingetragenen Termine. Urheberrechtlich geschützte Inhalte dürfen nicht ohne Genehmigung eingestellt werden.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">4. Kündigung</h2>
<p>Der Betreiber kann Accounts bei Verstoß gegen diese Bedingungen ohne Vorankündigung sperren oder löschen.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">5. Vereinsdokumente</h2>
<p>Im Bereich „Dokumente“ legt der Verein eigene Unterlagen ab (z. B. Protokolle, Satzung). Für den Inhalt und für darin enthaltene personenbezogene Daten ist der Verein verantwortlich; der Betreiber speichert sie nur in seinem Auftrag und sieht sie nicht ein. Bitte nur ablegen, was der Verein dafür auch speichern darf. Es gibt kein Recht auf dauerhafte Aufbewahrung – wichtige Unterlagen bitte zusätzlich selbst sichern (Herunterladen). Der Bereich steht zur Verfügung, sobald ein Vereinsadmin den <a href="/verein/avv">AV-Vertrag</a> abgeschlossen hat.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">6. Entwürfe und Planungsrunden</h2>
<p>Entwürfe und Planungsrunden sind für Termine gedacht. Bitte dort keine personenbezogenen Daten Dritter eintragen (z. B. Namen von Mitgliedern mit Aufgaben oder Kontaktdaten) – dafür gibt es den Dokumentenbereich.</p>
</div>
{_BACK_HISTORY}"""
    return _page("Nutzungsbedingungen", body)


# ── Vereinsprofil ─────────────────────────────────────────────────────────────

@verein_bp.route("/verein/profil", methods=["GET", "POST"])
@require_verein_login
def verein_profil(user):
    """Alle Daten aus der Registrierung: Verein (DB vereine_accounts) und
    Ansprechpartner (vk_users des eingeloggten Admins). Die E-Mail ist Login und
    Reset-Ziel – sie wechselt erst nach Klick auf den Link an die neue Adresse."""
    if user["role"] != "admin":
        return redirect("/verein/einstellungen")

    from services.auth.routes import (
        _ansprechpartner_felder, _check_pw, _valid_email, ansprechpartner_fehler,
    )
    from shared.vk_mail import gruss_aus, send_email_change_confirm, send_email_change_notice

    error = ok = ""

    with db_conn() as conn:
        va = conn.execute(
            "SELECT verein_name, rubrik, heimatort, plz, gemeinde, landkreis FROM vereine_accounts WHERE id=?",
            (user["verein_id"],),
        ).fetchone()
        usr = conn.execute(
            """SELECT email, telefon, anrede, vorname, nachname, name, email_neu, email_neu_expires,
                      password_hash
               FROM vk_users WHERE id=?""", (user["id"],)
        ).fetchone()

    if not va or not usr:
        return redirect("/verein/einstellungen")

    verein_name = va["verein_name"]
    rubrik      = va["rubrik"] or "Verein"
    heimatort   = va["heimatort"] or ""
    plz         = va["plz"] or ""
    gemeinde    = va["gemeinde"] or ""
    landkreis   = va["landkreis"] or ""
    telefon     = usr["telefon"] or ""
    anrede      = usr["anrede"] or ""
    vorname     = usr["vorname"] or ""
    nachname    = usr["nachname"] or ""
    if not vorname and not nachname and usr["name"]:
        # Altbestand: nur `name` (vom Admin gesetzt) → als Vorschlag aufteilen
        teile = usr["name"].strip().rsplit(" ", 1)
        vorname, nachname = (teile[0], teile[1]) if len(teile) == 2 else ("", teile[0])
    email       = usr["email"]
    email_neu   = usr["email_neu"] if usr["email_neu_expires"] and \
        usr["email_neu_expires"] > datetime.utcnow().isoformat() else ""
    eingabe_email = email

    if request.method == "POST":
        if not validate_csrf():
            return _page("Fehler", '<p class="err">Ungültige Anfrage. Bitte Seite neu laden.</p>'), 403
        f = {k: request.form.get(k, "").strip() for k in
             ("verein_name", "rubrik", "heimatort", "plz", "telefon", "anrede", "vorname", "nachname", "email")}
        f["email"] = f["email"].lower()
        pw_aktuell = request.form.get("password_aktuell", "")
        email_wechsel = f["email"] != email.lower()
        eingabe_email = f["email"]

        if not f["verein_name"] or len(f["verein_name"]) < 3:
            error = "Vereinsname muss mindestens 3 Zeichen haben."
        elif f["rubrik"] not in RUBRIKEN:
            error = "Bitte eine gültige Rubrik wählen."
        elif not plz_gueltig(f["plz"]):
            error = "Bitte die PLZ angeben (5 Ziffern, z.B. 84092)."
        elif not f["heimatort"] or len(f["heimatort"]) < 2:
            error = "Bitte die Ortschaft angeben."
        elif ansprechpartner_fehler(f["anrede"], f["vorname"], f["nachname"]):
            error = ansprechpartner_fehler(f["anrede"], f["vorname"], f["nachname"])
        elif not _valid_email(f["email"]):
            error = "Bitte eine gültige E-Mail-Adresse eingeben."
        elif email_wechsel and not _check_pw(pw_aktuell, usr["password_hash"]):
            error = "Zum Ändern der E-Mail-Adresse bitte das aktuelle Passwort eingeben."
        elif not f["telefon"]:
            error = "Bitte eine Telefonnummer für Rückfragen angeben."
        else:
            if f["plz"] != plz or f["heimatort"] != heimatort or not gemeinde:
                new_gemeinde, new_landkreis, hinweise = ortschaft_geo(f["heimatort"], f["plz"])
                if hinweise:
                    _telegram_ortschaft_hinweis(f["verein_name"], f["plz"], f["heimatort"], hinweise)
            else:
                new_gemeinde, new_landkreis = gemeinde, landkreis

            token = None
            with db_conn() as conn:
                conn.execute(
                    """UPDATE vereine_accounts
                       SET verein_name=?, rubrik=?, heimatort=?, plz=?, gemeinde=?, landkreis=?
                       WHERE id=?""",
                    (f["verein_name"], f["rubrik"], f["heimatort"], f["plz"],
                     new_gemeinde or None, new_landkreis or None, user["verein_id"]),
                )
                conn.execute(
                    "UPDATE vk_users SET telefon=?, anrede=?, vorname=?, nachname=?, name=? WHERE id=?",
                    (f["telefon"], f["anrede"], f["vorname"], f["nachname"],
                     f"{f['vorname']} {f['nachname']}", user["id"]),
                )
                if email_wechsel:
                    import secrets as _sec
                    token = _sec.token_urlsafe(32)
                    conn.execute(
                        "UPDATE vk_users SET email_neu=?, email_neu_token=?, email_neu_expires=? WHERE id=?",
                        (f["email"], token, (datetime.utcnow() + timedelta(hours=24)).isoformat(), user["id"]),
                    )

            log_audit("email_wechsel_angefordert" if email_wechsel else "profil_geaendert",
                      "", user.get("verein_key") or "", user["id"])
            _profil_in_kalender(user.get("verein_key"), f["verein_name"], verein_name, {
                "rubrik": f["rubrik"], "heimatort": f["heimatort"], "plz": f["plz"],
                "gemeinde": new_gemeinde, "landkreis": new_landkreis,
            })
            gruss = gruss_aus(f)
            if email_wechsel and token:
                send_email_change_confirm(f["email"], token, f["verein_name"], gruss=gruss)
                send_email_change_notice(email, f["email"], f["verein_name"], gruss=gruss)
                email_neu = f["email"]

            verein_name, rubrik, heimatort, plz = f["verein_name"], f["rubrik"], f["heimatort"], f["plz"]
            gemeinde, landkreis, telefon = new_gemeinde, new_landkreis, f["telefon"]
            anrede, vorname, nachname = f["anrede"], f["vorname"], f["nachname"]
            eingabe_email = email
            ok = "Profil gespeichert."
            if email_wechsel:
                ok += (f" Wir haben einen Bestätigungslink an <strong>{html.escape(email_neu)}</strong> geschickt."
                       " Bis zum Klick darauf bleibt die bisherige Adresse gültig.")

        if error:
            # Eingaben behalten
            verein_name, rubrik, heimatort, plz = f["verein_name"], f["rubrik"] or rubrik, f["heimatort"], f["plz"]
            telefon, anrede, vorname, nachname = f["telefon"], f["anrede"], f["vorname"], f["nachname"]

    rubrik_opts = "".join(
        f'<option value="{r}"{" selected" if rubrik == r else ""}>{r}</option>'
        for r in RUBRIKEN
    )
    geo_hint = f'<p class="hint">{html.escape(gemeinde)}, {html.escape(landkreis)}</p>' if gemeinde else ""
    neu_hint = (f'<p class="hint">Bestätigung ausstehend für <strong>{html.escape(email_neu)}</strong> '
                f'(Link per E-Mail, 24 Stunden gültig).</p>') if email_neu else ""

    tok = get_csrf_token()
    body = f"""
{'<p class="err">'+error+'</p>' if error else ''}
{'<p class="ok">'+ok+'</p>' if ok else ''}
<form method="post" autocomplete="on">
  {csrf_field(tok)}
  <h2 style="font-size:1rem;margin:1rem 0 0">Verein</h2>
  <label>Vereinsname</label>
  <input name="verein_name" type="text" required autocomplete="organization" value="{html.escape(verein_name)}">
  <label>Rubrik</label>
  <select name="rubrik" required>
    {rubrik_opts}
  </select>
{_ortschaft_felder(plz, heimatort)}
  {geo_hint}
  <h2 style="font-size:1rem;margin:1.5rem 0 0">Ansprechpartner</h2>
{_ansprechpartner_felder(anrede, vorname, nachname)}
  <label>E-Mail <span class="hint">(damit loggt ihr euch ein)</span></label>
  <input name="email" type="text" inputmode="email" autocorrect="off" autocapitalize="none" required autocomplete="email" value="{html.escape(eingabe_email)}">
  {neu_hint}
  <label>Aktuelles Passwort <span class="hint">(nur nötig, wenn du die E-Mail änderst)</span></label>
  <input name="password_aktuell" type="password" autocomplete="current-password">
  <label>Telefon Ansprechpartner</label>
  <input name="telefon" type="tel" required autocomplete="tel" placeholder="z.B. 0172 1234567" value="{html.escape(telefon)}">
  <button class="btn" type="submit">Speichern</button>
</form>
<a class="btn btn-sec" href="/verein/passwort?von=profil" style="margin-top:.75rem">Passwort ändern</a>
{_BACK_EINST}
{_PLZ_QUELLE}
{_ORTSCHAFT_JS}"""
    return _page("Vereinsprofil", body)


def _profil_in_kalender(verein_key: str | None, neuer_name: str, alter_name: str, werte: dict) -> None:
    """Profiländerungen in vereinstermine.json nachziehen – sonst sieht der Kalender
    sie nie (Kalender liest `_labels`/`_meta`, nicht die DB).

    Nur für Vereine, die im Kalender schon existieren (`_labels`), also freigegeben.
    Andere `_meta`-Felder (`selbstverwaltung`, `ortschaft_gemeinde`-Override …)
    bleiben unangetastet; leere Werte überschreiben nichts."""
    if not verein_key:
        return

    def _mut(d, vk=verein_key):
        labels = d.get("_labels") or {}
        if vk not in labels:
            return
        if neuer_name != alter_name:
            labels[vk] = neuer_name
        m = d.setdefault("_meta", {}).setdefault(vk, {})
        for k, v in werte.items():
            if v:
                m[k] = v
    KalenderStore.update(_mut)


@verein_bp.route("/verein/email-bestaetigen", methods=["GET"])
def email_bestaetigen():
    """Link aus der Mail an die neue Adresse: erst jetzt wird sie Login-Adresse."""
    token = request.args.get("token", "")
    with db_conn() as conn:
        row = conn.execute(
            "SELECT id, email_neu, email_neu_expires FROM vk_users WHERE email_neu_token=?",
            (token,),
        ).fetchone() if token else None
        if not row or not row["email_neu"] or (row["email_neu_expires"] or "") < datetime.utcnow().isoformat():
            return _page("Link ungültig",
                         '<p class="err">Der Link ist ungültig oder abgelaufen. '
                         'Bitte die Änderung im Vereinsprofil erneut anstoßen.</p>'
                         '<a class="btn btn-sec" href="/verein/login">Zum Login</a>')
        conn.execute(
            """UPDATE vk_users SET email=?, email_verified=1,
                      email_neu=NULL, email_neu_token=NULL, email_neu_expires=NULL
               WHERE id=?""",
            (row["email_neu"], row["id"]),
        )
    return _page("E-Mail bestätigt",
                 f'<p class="ok">Ab sofort loggst du dich mit <strong>{html.escape(row["email_neu"])}</strong> ein.</p>'
                 '<a class="btn" href="/verein/login">Zum Login</a>')
