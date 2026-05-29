import base64
import os
import re
import sqlite3
import tempfile
from functools import wraps
from pathlib import Path

_ICONS_DIR = Path("/opt/rename-webhook/icons")
_RECHNUNGEN_HTML_FILE = Path("/opt/rename-webhook/rechnungen.html")

_ICON_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="-6 -6 36 36">
  <rect x="-6" y="-6" width="36" height="36" fill="#14532d"/>
  <path d="M4 2v20l2-1 2 1 2-1 2 1 2-1 2 1 2-1 2 1V2l-2 1-2-1-2 1-2-1-2 1-2-1-2 1Z" stroke="white" stroke-width="1.3" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M14 8H8" stroke="white" stroke-width="1.3" fill="none" stroke-linecap="round"/>
  <path d="M16 12H8" stroke="white" stroke-width="1.3" fill="none" stroke-linecap="round"/>
  <path d="M13 16H8" stroke="white" stroke-width="1.3" fill="none" stroke-linecap="round"/>
</svg>'''

_html_rechnungen_cache: dict = {"data": None, "mtime": 0.0}


def _get_rechnungen_html() -> str:
    if not _RECHNUNGEN_HTML_FILE.exists():
        return "<h1>rechnungen.html nicht gefunden</h1>"
    mtime = _RECHNUNGEN_HTML_FILE.stat().st_mtime
    if _html_rechnungen_cache["mtime"] != mtime or _html_rechnungen_cache["data"] is None:
        _html_rechnungen_cache["data"] = _RECHNUNGEN_HTML_FILE.read_text(encoding="utf-8")
        _html_rechnungen_cache["mtime"] = mtime
    return _html_rechnungen_cache["data"]


def _generate_rechnungen_icon(size: int, path: Path) -> None:
    import cairosvg
    _ICONS_DIR.mkdir(exist_ok=True)
    cairosvg.svg2png(bytestring=_ICON_SVG.encode(), write_to=str(path),
                     output_width=size, output_height=size)

import anthropic
import dropbox
from dropbox.files import FolderMetadata, FileMetadata
from flask import Blueprint, jsonify, request

from services.invoice.db import (
    STEUER_KATEGORIEN,
    DB_PATH,
    dateiname_exists,
    get_all,
    get_one,
    insert_raw,
    update_rechnung,
)
from shared.kalender_core import MEDIA_TYPES, log

_CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
_EXTRACT_MODEL  = "claude-haiku-4-5"

_DROPBOX_REFRESH_TOKEN = os.environ.get("DROPBOX_REFRESH_TOKEN", "")
_DROPBOX_APP_KEY       = os.environ.get("DROPBOX_APP_KEY", "")
_DROPBOX_APP_SECRET    = os.environ.get("DROPBOX_APP_SECRET", "")

_WATCH_FOLDER          = "/_gescannt-unsortiert"
_STEUERBERATER_FOLDER  = "/_Unterlagen für Steuerberater"
_PRIVAT_FOLDER         = "/_Private Rechnungen"

_EXTRACT_PROMPT = """Analysiere dieses Dokument und gib NUR ein JSON-Objekt zurück (kein Markdown):
{
  "datum": "YYYY-MM-DD",
  "firma": "Name der Firma oder Behörde",
  "betrag_raw": "-70.00€ oder +12.50€ oder leer wenn kein Betrag",
  "kategorie_rename": "Rechnung / Abrechnung / Bescheid / Gutschrift / etc.",
  "schlagwort": "kurze Beschreibung"
}
Datum: wirksames Datum aus dem Dokument (nicht Scandatum). Nur Monat+Jahr → 01. des Monats.
Betrag: Bruttobetrag. Ausgaben → negativ, Einnahmen/Erstattungen → positiv. Kein Betrag → leerer String."""

invoice_bp = Blueprint("invoice", __name__)
_API_TOKEN = os.environ.get("RECHNUNGEN_API_TOKEN", "")


def _require_token(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not _API_TOKEN:
            return jsonify({"error": "Server-Token nicht konfiguriert"}), 500
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if token != _API_TOKEN:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


def _get_dbx() -> dropbox.Dropbox:
    return dropbox.Dropbox(
        oauth2_refresh_token=_DROPBOX_REFRESH_TOKEN,
        app_key=_DROPBOX_APP_KEY,
        app_secret=_DROPBOX_APP_SECRET,
    )


def _extract_year(dateiname: str) -> str:
    m = re.search(r'(\d{4})[-_\s]', dateiname)
    if m:
        year = m.group(1)
        if 2000 <= int(year) <= 2099:
            return year
    from datetime import datetime
    return str(datetime.now().year)


def _do_verschieben(dbx, src_path: str, typ: str, unterordner: str) -> str:
    dateiname = Path(src_path).name
    jahr = _extract_year(dateiname)
    if typ == "steuerberater":
        ziel_ordner = f"{_STEUERBERATER_FOLDER}/{unterordner}/{jahr}"
    elif typ == "privat":
        ziel_ordner = f"{_PRIVAT_FOLDER}/{jahr}"
    else:
        raise ValueError(f"Ungültiger typ: {typ}")
    dst_path = f"{ziel_ordner}/{dateiname}"
    try:
        dbx.files_create_folder_v2(ziel_ordner)
    except dropbox.exceptions.ApiError as e:
        if "conflict" not in str(e).lower():
            raise
    dbx.files_move_v2(src_path, dst_path, autorename=False)
    return dst_path


@invoice_bp.route("/api/rechnungen/kategorien", methods=["GET"])
@_require_token
def kategorien():
    return jsonify(STEUER_KATEGORIEN)


@invoice_bp.route("/api/rechnungen/zielordner", methods=["GET"])
@_require_token
def zielordner():
    try:
        dbx = _get_dbx()
        result = dbx.files_list_folder(_STEUERBERATER_FOLDER)
        unterordner = sorted(
            e.name for e in result.entries if isinstance(e, FolderMetadata)
        )
        return jsonify({"steuerberater": unterordner})
    except Exception as e:
        log(f"⚠️  zielordner-Fehler: {e}")
        return jsonify({"error": str(e)}), 500


@invoice_bp.route("/api/rechnungen/eingangsordner", methods=["GET"])
@_require_token
def eingangsordner():
    try:
        dbx = _get_dbx()
        result = dbx.files_list_folder(_WATCH_FOLDER, recursive=False)
        files = []
        for e in result.entries:
            if not isinstance(e, FolderMetadata) and e.name.lower().endswith('.pdf'):
                files.append({
                    "name": e.name,
                    "path": e.path_display,
                    "size": getattr(e, "size", 0),
                })
        files.sort(key=lambda x: x["name"])
        return jsonify(files)
    except Exception as e:
        log(f"⚠️  eingangsordner-Fehler: {e}")
        return jsonify({"error": str(e)}), 500


@invoice_bp.route("/api/rechnungen/eingangs-verschieben", methods=["POST"])
@_require_token
def eingangs_verschieben():
    data = request.get_json(force=True, silent=True) or {}
    dateiname  = data.get("dateiname", "").strip()
    typ        = data.get("typ", "")
    unterordner = data.get("unterordner", "").strip()
    if not dateiname:
        return jsonify({"error": "dateiname fehlt"}), 400
    src_path = f"{_WATCH_FOLDER}/{dateiname}"
    try:
        dbx = _get_dbx()
        dst_path = _do_verschieben(dbx, src_path, typ, unterordner)
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT id FROM rechnungen WHERE dateiname=?", (dateiname,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE rechnungen SET dropbox_pfad=? WHERE id=?",
                    (dst_path, row[0])
                )
                conn.commit()
        log(f"✅ Eingangs-Datei verschoben: {dateiname} → {dst_path}")
        return jsonify({"success": True, "dropbox_pfad": dst_path})
    except Exception as e:
        log(f"⚠️  eingangs-verschieben-Fehler: {e}")
        return jsonify({"error": str(e)}), 500


@invoice_bp.route("/api/rechnungen/<int:row_id>/preview", methods=["GET"])
@_require_token
def preview(row_id: int):
    row = get_one(row_id)
    if not row:
        return jsonify({"error": "Nicht gefunden"}), 404
    dbx_path = row.get("dropbox_pfad") or f"{_WATCH_FOLDER}/{row['dateiname']}"
    try:
        dbx = _get_dbx()
        result = dbx.files_get_temporary_link(dbx_path)
        return jsonify({"url": result.link, "dateiname": row["dateiname"]})
    except dropbox.exceptions.ApiError as e:
        if "not_found" in str(e):
            return jsonify({"error": "not_found", "dateiname": row["dateiname"]}), 404
        log(f"⚠️  preview-Fehler id={row_id}: {e}")
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        log(f"⚠️  preview-Fehler id={row_id}: {e}")
        return jsonify({"error": str(e)}), 500


@invoice_bp.route("/api/rechnungen/<int:row_id>/verschieben", methods=["POST"])
@_require_token
def verschieben(row_id: int):
    row = get_one(row_id)
    if not row:
        return jsonify({"error": "Nicht gefunden"}), 404
    data = request.get_json(force=True, silent=True) or {}
    typ         = data.get("typ", "")
    unterordner = data.get("unterordner", "").strip()

    dateiname = row["dateiname"]
    src_path  = row.get("dropbox_pfad") or f"{_WATCH_FOLDER}/{dateiname}"

    try:
        dbx = _get_dbx()
        dst_path = _do_verschieben(dbx, src_path, typ, unterordner)
        update_rechnung(row_id, dropbox_pfad=dst_path)
        log(f"✅ Rechnung {row_id} verschoben → {dst_path}")
        return jsonify({"success": True, "dropbox_pfad": dst_path})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        log(f"⚠️  verschieben-Fehler id={row_id}: {e}")
        return jsonify({"error": str(e)}), 500


@invoice_bp.route("/api/rechnungen", methods=["GET"])
@_require_token
def liste():
    rows = get_all(
        limit=int(request.args.get("limit", 200)),
        offset=int(request.args.get("offset", 0)),
        steuer_kategorie=request.args.get("steuer_kategorie") or None,
        datum_von=request.args.get("datum_von") or None,
        datum_bis=request.args.get("datum_bis") or None,
    )
    return jsonify(rows)


@invoice_bp.route("/api/rechnungen/<int:row_id>", methods=["GET"])
@_require_token
def detail(row_id: int):
    row = get_one(row_id)
    if not row:
        return jsonify({"error": "Nicht gefunden"}), 404
    return jsonify(row)


@invoice_bp.route("/api/rechnungen/extract", methods=["POST"])
@_require_token
def extract():
    data = request.get_json(force=True, silent=True) or {}
    file_b64  = data.get("content_b64", "")
    suffix    = data.get("suffix", ".pdf").lower()
    if not file_b64:
        return jsonify({"error": "content_b64 fehlt"}), 400

    media_type = MEDIA_TYPES.get(suffix, "application/octet-stream")
    if suffix == ".pdf":
        content = [
            {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": file_b64}},
            {"type": "text", "text": _EXTRACT_PROMPT},
        ]
    else:
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": file_b64}},
            {"type": "text", "text": _EXTRACT_PROMPT},
        ]
    try:
        client = anthropic.Anthropic(api_key=_CLAUDE_API_KEY)
        msg = client.messages.create(model=_EXTRACT_MODEL, max_tokens=256,
                                     messages=[{"role": "user", "content": content}])
        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
        import json
        parsed = json.loads(raw)
        return jsonify(parsed)
    except Exception as e:
        log(f"⚠️  extract-Endpoint Fehler: {e}")
        return jsonify({"error": str(e)}), 500


@invoice_bp.route("/api/rechnungen", methods=["POST"])
@_require_token
def create():
    data = request.get_json(force=True, silent=True) or {}
    dateiname = data.get("dateiname", "").strip()
    if not dateiname:
        return jsonify({"error": "dateiname fehlt"}), 400
    if dateiname_exists(dateiname):
        return jsonify({"skipped": True, "dateiname": dateiname}), 200
    steuer_kat = data.get("steuer_kategorie", "Allgemeines")
    if steuer_kat not in STEUER_KATEGORIEN:
        return jsonify({"error": f"Ungültige steuer_kategorie: {steuer_kat}"}), 400
    row_id = insert_raw(
        datum=data.get("datum", ""),
        firma=data.get("firma", ""),
        betrag_raw=data.get("betrag_raw", ""),
        betrag=data.get("betrag"),
        kategorie_rename=data.get("kategorie_rename", ""),
        schlagwort=data.get("schlagwort", ""),
        roga_kuerzel=data.get("roga_kuerzel", ""),
        steuer_kategorie=steuer_kat,
        dateiname=dateiname,
    )
    return jsonify({"id": row_id, "dateiname": dateiname}), 201


@invoice_bp.route("/api/rechnungen/<int:row_id>", methods=["PUT"])
@_require_token
def update(row_id: int):
    data = request.get_json(force=True, silent=True) or {}
    if "steuer_kategorie" in data and data["steuer_kategorie"] not in STEUER_KATEGORIEN:
        return jsonify({"error": "Ungültige Kategorie"}), 400
    ok = update_rechnung(row_id, **data)
    if not ok:
        return jsonify({"error": "Keine gültigen Felder oder nicht gefunden"}), 400
    return jsonify(get_one(row_id))


# ── PWA-Routen ────────────────────────────────────────────────────────────────

@invoice_bp.route("/rechnungen/")
def rechnungen_app():
    from flask import Response
    return Response(_get_rechnungen_html(), content_type="text/html; charset=utf-8")


@invoice_bp.route("/rechnungen/manifest.json")
def rechnungen_manifest():
    from flask import Response
    import json
    data = {
        "name": "Rechnungen",
        "short_name": "Rechnungen",
        "start_url": "/rechnungen/",
        "display": "standalone",
        "background_color": "#f0fdf4",
        "theme_color": "#14532d",
        "icons": [
            {"src": "/rechnungen/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/rechnungen/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/rechnungen/apple-touch-icon.png", "sizes": "180x180", "type": "image/png"},
        ],
    }
    return Response(json.dumps(data), content_type="application/manifest+json")


@invoice_bp.route("/rechnungen/sw.js")
def rechnungen_sw():
    from flask import Response
    sw = (
        "const CACHE='rechnungen-v1';\n"
        "const SHELL=['/rechnungen/','/rechnungen/manifest.json',"
        "'/rechnungen/icon-192.png','/rechnungen/icon-512.png',"
        "'/rechnungen/apple-touch-icon.png'];\n"
        "self.addEventListener('install',e=>{e.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL)));self.skipWaiting();});\n"
        "self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))));self.clients.claim();});\n"
        "self.addEventListener('fetch',e=>{\n"
        "  const u=new URL(e.request.url);\n"
        "  if(u.pathname.startsWith('/api/'))return;\n"
        "  if(e.request.destination==='document'){e.respondWith(fetch(e.request).catch(()=>caches.match('/rechnungen/')));return;}\n"
        "  e.respondWith(caches.match(e.request).then(c=>c||fetch(e.request)));\n"
        "});\n"
    )
    return Response(sw, content_type="application/javascript")


def _serve_rechnungen_icon(size: int):
    from flask import send_file
    fname = "apple-touch-icon.png" if size == 180 else f"icon-{size}.png"
    path = _ICONS_DIR / f"rechnungen-{fname}"
    if not path.exists():
        _generate_rechnungen_icon(size, path)
    return send_file(path, mimetype="image/png")


@invoice_bp.route("/rechnungen/icon-192.png")
def rechnungen_icon_192():
    return _serve_rechnungen_icon(192)


@invoice_bp.route("/rechnungen/icon-512.png")
def rechnungen_icon_512():
    return _serve_rechnungen_icon(512)


@invoice_bp.route("/rechnungen/apple-touch-icon.png")
def rechnungen_apple_touch_icon():
    return _serve_rechnungen_icon(180)
