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
    cleanup_stale_pending, import_pdf_bytes, parse_excel_bytes,
)
from shared.flyer_store import upload_flyer, delete_flyer, pruefe_flyer
from shared.rubriken import RUBRIKEN
from shared.csrf import csrf_field, get_csrf_token, validate_csrf
from shared.vk_db import (
    db_conn, get_session_user, log_audit,
    get_upload_count, increment_upload_quota,
)
from services.auth.routes import (
    _CSS, _ORTSCHAFT_JS, _PLZ_QUELLE, _ortschaft_felder, _page, _session_token,
    _telegram_ortschaft_hinweis, ortschaft_geo, require_verein_login,
)
from shared.geo import plz_gueltig
from shared.termin_felder import BESCHREIBUNG_MAX as _BESCHREIBUNG_MAX, zeit_fehler as _zeit_fehler

verein_bp = Blueprint("verein", __name__)

_BACK_PROFIL = '<a class="btn btn-sec" href="/verein/profil" style="margin-top:.75rem">← Zurück zum Profil</a>'
_BACK_DASH = '<a class="btn btn-sec" href="/verein/dashboard" style="margin-top:.75rem">← Zurück</a>'
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


# ── Dashboard ────────────────────────────────────────────────────────────────

@verein_bp.route("/verein/dashboard")
@require_verein_login
def dashboard(user):
    verein_key = user["verein_key"]
    verein_name = user["verein_name"]
    termine = _get_verein_termine(verein_key)
    heute = date.today().isoformat()

    rows = ""
    for t in sorted(termine, key=lambda x: x.get("datum", "")):
        past = t.get("datum", "") < heute
        style = "opacity:.5" if past else ""
        edit_btn = ""
        if user["role"] == "admin":
            edit_btn = (f'<a href="/verein/termine/{html.escape(t["id"])}" style="margin-left:.5rem;color:#0a84ff;text-decoration:none;font-size:.9rem">Bearbeiten</a>'
                        if t.get("id") else "")
        rows += f"""<div class="card" style="{style}">
  <div style="display:flex;justify-content:space-between;align-items:start">
    <div>
      <div style="font-weight:600">{html.escape(t.get('bezeichnung',''))}</div>
      <div style="color:#aeaeb2;font-size:.85rem">{html.escape(t.get('datum',''))} {html.escape(t.get('uhrzeit',''))}{('–' + html.escape(t['uhrzeit_bis'])) if t.get('uhrzeit_bis') else ''}</div>
      <div style="color:#aeaeb2;font-size:.85rem">{html.escape(t.get('ort',''))}</div>
      {'<div style="color:#8e8e93;font-size:.8rem">mit Beschreibung</div>' if t.get('beschreibung') else ''}
    </div>
    <div>{edit_btn}</div>
  </div>
</div>"""

    if not rows:
        rows = '<p style="color:#aeaeb2">Noch keine Termine eingetragen.</p>'

    neu_btn = ""
    upload_btn = ""
    mitglieder_link = ""
    profil_link = ""
    if user["role"] == "admin":
        neu_btn = '<a class="btn" href="/verein/termine/neu">+ Neuer Termin</a>'
        remaining = _quota_remaining(user["verein_id"])
        used = _UPLOAD_LIMIT - remaining
        upload_btn = (
            f'<a class="btn btn-sec" href="/verein/upload" style="margin-top:.5rem">'
            f'Terminplan hochladen ({used}/{_UPLOAD_LIMIT} heute)</a>'
        )
        mitglieder_link = '<a class="btn btn-sec" href="/verein/mitglieder" style="margin-top:.5rem">Mitglieder</a>'
        profil_link = '<a class="btn btn-sec" href="/verein/profil" style="margin-top:.5rem">Vereinsprofil</a>'

    upload_ok = request.args.get("upload_ok", "")
    upload_banner = ""
    if upload_ok and upload_ok.isdigit():
        upload_banner = f'<p class="ok">{upload_ok} Termine erfolgreich importiert.</p>'

    hilfe_block = ""
    if user["role"] == "admin":
        hilfe_block = """
<details style="margin-top:1rem;border:1px solid #3a3a3c;border-radius:.625rem;overflow:hidden">
  <summary style="padding:.75rem 1rem;cursor:pointer;background:#2c2c2e;color:#f2f2f7;font-size:.9rem;font-weight:600;list-style:none;display:flex;justify-content:space-between;align-items:center">
    Hilfe &amp; FAQ <span style="color:#aeaeb2;font-weight:400;font-size:.8rem">▾</span>
  </summary>
  <div style="padding:1rem;display:flex;flex-direction:column;gap:.85rem;background:#1c1c1e">

    <div>
      <div style="font-weight:600;font-size:.9rem;margin-bottom:.25rem">PDF oder Foto hochladen</div>
      <div style="color:#aeaeb2;font-size:.85rem">Claude KI liest das Dokument und extrahiert Termine automatisch. Funktioniert mit Jahresprogrammen, Pfarrbriefen und Fotos von Plakaten (JPG, PNG, HEIC). Dauer: ca. 15–60 Sek.</div>
    </div>

    <div>
      <div style="font-weight:600;font-size:.9rem;margin-bottom:.25rem">Excel-Vorlage</div>
      <div style="color:#aeaeb2;font-size:.85rem">Vorlage herunterladen, ausfüllen und hochladen – kein KI-Call, sofortige Verarbeitung.<br>
      Datumsformat: <code style="background:#3a3a3c;padding:0 4px;border-radius:3px">TT.MM.JJJJ</code> oder <code style="background:#3a3a3c;padding:0 4px;border-radius:3px">JJJJ-MM-TT</code> – kein Text wie „ca." oder Leerzeichen in der Datumsspalte.</div>
    </div>

    <div>
      <div style="font-weight:600;font-size:.9rem;margin-bottom:.25rem">Tageslimit</div>
      <div style="color:#aeaeb2;font-size:.85rem">3 Uploads pro Tag – Zurücksetzung um Mitternacht. Das Limit gilt pro Verein.</div>
    </div>

    <div>
      <div style="font-weight:600;font-size:.9rem;margin-bottom:.25rem">Upload fehlgeschlagen?</div>
      <div style="color:#aeaeb2;font-size:.85rem">
        <b style="color:#f2f2f7">PDF:</b> Seite als Foto abfotografieren und als JPG hochladen.<br>
        <b style="color:#f2f2f7">Excel:</b> Datumsspalte prüfen – nur reines Datum, kein zusätzlicher Text.<br>
        <b style="color:#f2f2f7">Allgemein:</b> Datei erneut hochladen oder per Mail an
        <a href="mailto:Vereinskalender@icloud.com" style="color:#0a84ff">Vereinskalender@icloud.com</a> schicken – wir importieren manuell.
      </div>
    </div>

    <div>
      <div style="font-weight:600;font-size:.9rem;margin-bottom:.25rem">Vorschau vor dem Speichern</div>
      <div style="color:#aeaeb2;font-size:.85rem">Beim Anlegen und Bearbeiten zeigt „Vorschau“ den Termin so, wie er im Kalender erscheint – bei mehrtägigen Terminen alle Tage. Termin antippen zeigt die Beschreibung, die Büroklammer den Flyer. Es wird dabei nichts gespeichert oder hochgeladen.</div>
    </div>

    <div>
      <div style="font-weight:600;font-size:.9rem;margin-bottom:.25rem">Flyer-Upload bei einem Termin</div>
      <div style="color:#aeaeb2;font-size:.85rem">Bild oder PDF (max. 8 MB) zuerst auf dem Gerät speichern und von dort hochladen. Ein Bild direkt aus Outlook/einer E-Mail in das Upload-Feld zu ziehen funktioniert nicht (Outlook gibt dabei nur einen internen Bild-Verweis statt der echten Datei weiter).</div>
    </div>

  </div>
</details>"""

    body = f"""
{upload_banner}<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem">
  <div>
    <div style="font-weight:600">{verein_name}</div>
    <div style="color:#aeaeb2;font-size:.85rem">{user['email']} · {user['role']}</div>
  </div>
  <form method="post" action="/verein/logout">
    <button class="btn btn-sec" style="width:auto;padding:.5rem .875rem;font-size:.85rem">Logout</button>
  </form>
</div>
{neu_btn}
{upload_btn}
<h2 style="font-size:1rem;margin:1rem 0 .5rem">Termine ({len(termine)})</h2>
{rows}
{mitglieder_link}
{profil_link}
{hilfe_block}
<hr>
<a class="btn btn-sec" href="/verein/passwort" style="margin-top:.5rem">Passwort ändern</a>
<a class="btn btn-sec" href="/" style="margin-top:.5rem">← Zurück zum Kalender</a>
<p class="hint" style="margin-top:1rem"><a href="/verein/datenschutz">Datenschutzerklärung</a> · <a href="/verein/nutzungsbedingungen">Nutzungsbedingungen</a></p>"""
    return _page(f"Dashboard – {verein_name}", body)


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


@verein_bp.route("/verein/termine/neu", methods=["GET", "POST"])
@require_verein_login
def termin_neu(user):
    if user["role"] != "admin":
        return redirect("/verein/dashboard")

    error = ""
    f = {"datum": date.today().isoformat(), "datum_bis": "", "uhrzeit": "", "uhrzeit_bis": "",
         "bezeichnung": "", "ort": ""}
    beschreibungen = [""]
    if request.method == "POST":
        if not validate_csrf():
            return _page("Fehler", '<p class="err">Ungültige Anfrage. Bitte Seite neu laden.</p>'), 403
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
            return redirect("/verein/dashboard")
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
<form id="termin-form" method="post" enctype="multipart/form-data" autocomplete="off" data-verein-key="{e(user.get('verein_key',''))}" data-verein-name="{e(user.get('verein_name',''))}">
  {csrf_field(tok)}
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
  <div id="tage">{tage_html}</div>
  <p id="tage-info" class="hint" style="color:#ff9f0a"></p>
  {_VORSCHAU_BTN}
  <button class="btn" type="submit">Termin speichern</button>
</form>
{_TAGE_JS % (_MAX_TAGE, _MAX_UPLOAD_MB * 1024 * 1024, _MAX_UPLOAD_MB)}
{_VORSCHAU_JS}
{_BACK_DASH}"""
    return _page("Neuer Termin", form)


# ── Termin bearbeiten / löschen ───────────────────────────────────────────────

@verein_bp.route("/verein/termine/<termin_id>", methods=["GET", "POST"])
@require_verein_login
def termin_edit(user, termin_id):
    if user["role"] != "admin":
        return redirect("/verein/dashboard")

    verein_key = user["verein_key"]
    data = _load_data()
    termine_list = data.get(verein_key, [])
    termin = next((t for t in termine_list if t.get("id") == termin_id and not t.get("geloescht") and not t.get("deleted")), None)

    if not termin:
        return redirect("/verein/dashboard")

    edit_error = ""
    if request.method == "POST":
        if not validate_csrf():
            return _page("Fehler", '<p class="err">Ungültige Anfrage. Bitte Seite neu laden.</p>'), 403
        aktion = request.form.get("aktion", "")
        if aktion == "loeschen":
            def del_updater(d):
                for t in d.get(verein_key, []):
                    if t.get("id") == termin_id:
                        t["geloescht"] = True
                        t["geloescht_von"] = user["email"]
                        t["geloescht_am"] = datetime.utcnow().isoformat()[:19]
                return d
            KalenderStore.update(del_updater)
            log_audit("geloescht", termin_id, verein_key, user["id"])
            return redirect("/verein/dashboard")
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
            return redirect(f"/verein/termine/{termin_id}")
        else:
            datum = request.form.get("datum", "").strip()
            uhrzeit = request.form.get("uhrzeit", "").strip()
            bezeichnung = request.form.get("bezeichnung", "").strip()
            ort = request.form.get("ort", "").strip()
            uhrzeit_bis = request.form.get("uhrzeit_bis", "").strip()
            beschreibung = request.form.get("beschreibung", "").strip()
            if not datum or not bezeichnung:
                edit_error = "Datum und Bezeichnung sind Pflichtfelder."
            elif not re.match(r"^\d{4}-\d{2}-\d{2}$", datum):
                edit_error = "Datum muss im Format YYYY-MM-DD sein."
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
                return redirect("/verein/dashboard")
            termin = {**termin, "datum": datum, "uhrzeit": uhrzeit, "uhrzeit_bis": uhrzeit_bis,
                      "bezeichnung": bezeichnung, "ort": ort, "beschreibung": beschreibung}

    tok = get_csrf_token()
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
<form id="termin-form" method="post" enctype="multipart/form-data" data-verein-key="{html.escape(verein_key)}" data-verein-name="{html.escape(user.get('verein_name',''))}" data-flyer-url="{html.escape(flyer_url)}">
  {csrf_field(tok)}
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
<hr>
<form method="post" onsubmit="return confirm('Termin wirklich löschen?')">
  {csrf_field(tok)}
  <button class="btn btn-danger" type="submit" name="aktion" value="loeschen">Termin löschen</button>
</form>
{_BACK_DASH}"""
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
                success = "Passwort erfolgreich geändert."

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
{_BACK_PROFIL if request.args.get("von") == "profil" else _BACK_DASH}"""
    return _page("Passwort ändern", form)


# ── Mitglieder ────────────────────────────────────────────────────────────────

@verein_bp.route("/verein/mitglieder", methods=["GET", "POST"])
@require_verein_login
def mitglieder(user):
    if user["role"] != "admin":
        return redirect("/verein/dashboard")

    import secrets as _sec
    from shared.vk_mail import send_invite_email

    error = ""
    success = ""
    if request.method == "POST":
        if not validate_csrf():
            return _page("Fehler", '<p class="err">Ungültige Anfrage. Bitte Seite neu laden.</p>'), 403
        aktion = request.form.get("aktion", "")
        if aktion == "einladen":
            email = request.form.get("email", "").strip().lower()
            with db_conn() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM vk_users WHERE verein_id=?", (user["verein_id"],)
                ).fetchone()[0]
                if count >= 3:
                    error = "Maximal 3 Accounts pro Verein (Admin + 2 Mitglieder)."
                else:
                    ex = conn.execute(
                        "SELECT id FROM vk_users WHERE email=?", (email,)
                    ).fetchone()
                    if ex:
                        error = "Diese E-Mail ist bereits registriert."
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
                        send_invite_email(email, token, user["verein_name"])
                        success = f"Einladung an {email} verschickt."
        elif aktion == "entfernen":
            try:
                member_id = int(request.form.get("member_id", 0))
            except ValueError:
                member_id = 0
            with db_conn() as conn:
                conn.execute(
                    "DELETE FROM vk_users WHERE id=? AND verein_id=? AND role='member'",
                    (member_id, user["verein_id"]),
                )
                success = "Mitglied entfernt."

    with db_conn() as conn:
        members = conn.execute(
            "SELECT id, email, role, aktiv, email_verified FROM vk_users WHERE verein_id=? ORDER BY id",
            (user["verein_id"],),
        ).fetchall()

    tok = get_csrf_token()
    rows = ""
    for m in members:
        status = "aktiv" if m["aktiv"] else "Einladung ausstehend"
        remove_btn = ""
        if m["role"] == "member":
            remove_btn = f'<form method="post" style="display:inline">{csrf_field(tok)}<input type="hidden" name="aktion" value="entfernen"><input type="hidden" name="member_id" value="{m["id"]}"><button style="background:none;border:none;color:#ff453a;cursor:pointer;font-size:.9rem" type="submit">Entfernen</button></form>'
        rows += f'<div class="card"><div style="display:flex;justify-content:space-between"><div><div>{m["email"]}</div><div style="color:#aeaeb2;font-size:.82rem">{m["role"]} · {status}</div></div><div>{remove_btn}</div></div></div>'

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
{_BACK_DASH}"""
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
            else:
                conn.execute(
                    """UPDATE vk_users SET password_hash=?, aktiv=1,
                       einladungs_token=NULL, einladungs_expires=NULL WHERE id=?""",
                    (_hash_pw(pw), row["id"]),
                )
                return redirect("/verein/login")

    tok = get_csrf_token()
    form = f"""
<p>Lege dein Passwort fest, um die Einladung anzunehmen.</p>
{'<p class="err">'+error+'</p>' if error else ''}
<form method="post">
  {csrf_field(tok)}
  <input type="hidden" name="token" value="{token}">
  <label>Passwort <span class="hint">(mind. 8 Zeichen)</span></label>
  <input name="password" type="password" required autocomplete="new-password">
  <label>Passwort wiederholen</label>
  <input name="password2" type="password" required autocomplete="new-password">
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
        return redirect("/verein/dashboard")

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
        return redirect("/verein/dashboard")
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
    return redirect(f"/verein/dashboard?upload_ok={total}")


# ── Upload bestätigen (POST) ──────────────────────────────────────────────────

@verein_bp.route("/verein/confirm-upload", methods=["POST"])
@require_verein_login
def confirm_upload(user):
    if user["role"] != "admin":
        return redirect("/verein/dashboard")

    import_id    = request.form.get("import_id", "")
    cleanup_stale_pending()
    pending_path = Path(f"/tmp/vk_pending_{import_id}.json")

    if not pending_path.exists():
        body = (
            '<p class="err">Import nicht gefunden oder abgelaufen. '
            'Bitte Datei erneut hochladen.</p>' + _BACK_DASH
        )
        return _page("Fehler", body), 404

    pending = json.loads(pending_path.read_text())

    if pending.get("verein_id") != user["verein_id"]:
        body = '<p class="err">Nicht autorisiert.</p>' + _BACK_DASH
        return _page("Fehler", body), 403

    vk = user["verein_key"]
    _, total = _do_save_import(pending["alle"], pending.get("auto_plz", ""), "", verein_key=vk)
    pending_path.unlink(missing_ok=True)
    def _sv_cf(d): d.setdefault("_meta", {}).setdefault(vk, {})["selbstverwaltung"] = True; return d
    KalenderStore.update(_sv_cf)
    log_audit("upload_confirmed", f"bulk_{total}", vk, user["id"], anzahl=total)
    return redirect(f"/verein/dashboard?upload_ok={total}")


# ── Datenschutz / Nutzungsbedingungen ────────────────────────────────────────

@verein_bp.route("/verein/datenschutz")
def datenschutz():
    body = f"""
<p style="color:#aeaeb2;font-size:.85rem">Stand: Oktober 2026</p>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">1. Verantwortlicher</h2>
<p>Josef Fischer, Hölskofen 13, 84092 Bayerbach b. Ergoldsbach · <a href="mailto:Vereinskalender@icloud.com">Vereinskalender@icloud.com</a></p>
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
<h2 style="font-size:1rem;margin-top:0">4. Speicherdauer</h2>
<p>Accounts werden auf Anfrage gelöscht. Schreib dazu an <a href="mailto:Vereinskalender@icloud.com">Vereinskalender@icloud.com</a>. Eingetragene Termine werden nach Ende des jeweiligen Kalenderjahres bereinigt.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">5. E-Mail-Dienst</h2>
<p>E-Mails werden über Brevo (Sendinblue SAS, Frankreich) versendet. Dabei wird die Ziel-E-Mail-Adresse an Brevo übermittelt.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">5a. Telegram-Terminerinnerungen (freiwillig)</h2>
<p>Wer den Telegram-Bot für Terminerinnerungen nutzt, speichert damit freiwillig seine Telegram-Chat-ID sowie die ausgewählten Vereins-Abonnements auf unserem Server. Diese Daten werden ausschließlich zum Versand der gewünschten Erinnerungen verwendet. Abmelden ist jederzeit mit dem Befehl /stop im Bot möglich – dabei werden alle gespeicherten Daten gelöscht. Eine Löschung ist auch per E-Mail möglich.</p>
</div>
<div class="card">
<h2 style="font-size:1rem;margin-top:0">6. Rechte</h2>
<p>Auskunft, Berichtigung, Löschung deiner Daten: Schreib an <a href="mailto:Vereinskalender@icloud.com">Vereinskalender@icloud.com</a>. Beschwerderecht bei der zuständigen Datenschutz-Aufsichtsbehörde.</p>
</div>
{_BACK_DASH}"""
    return _page("Datenschutzerklärung", body)


@verein_bp.route("/verein/nutzungsbedingungen")
def nutzungsbedingungen():
    body = f"""
<p style="color:#aeaeb2;font-size:.85rem">Stand: Oktober 2026</p>
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
{_BACK_DASH}"""
    return _page("Nutzungsbedingungen", body)


# ── Vereinsprofil ─────────────────────────────────────────────────────────────

@verein_bp.route("/verein/profil", methods=["GET", "POST"])
@require_verein_login
def verein_profil(user):
    """Alle Daten aus der Registrierung: Verein (DB vereine_accounts) und
    Ansprechpartner (vk_users des eingeloggten Admins). Die E-Mail ist Login und
    Reset-Ziel – sie wechselt erst nach Klick auf den Link an die neue Adresse."""
    if user["role"] != "admin":
        return redirect("/verein/dashboard")

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
        return redirect("/verein/dashboard")

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
{_BACK_DASH}
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
