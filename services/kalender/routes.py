import gzip
import hmac
import ipaddress
import json
import os
import re
import threading
import time
import uuid as _uuid
from datetime import date, datetime, timedelta, timezone as _tz
from pathlib import Path

from flask import Blueprint, Response, request

from shared.admin_aufgaben import offene_orte, register_pruefung
from shared.geo import geo_fuer_termin, termin_orte_misch, abo_treffer, region_of, eintrag_fuer, _lade as _geo_register, _gem_norm, _ort_norm, ORTE_FREI_FILE
from shared.flyer_store import upload_flyer, delete_flyer
from shared.termin_felder import BESCHREIBUNG_MAX, datum_ok, zeit_fehler
from shared.vk_db import db_conn
from shared.kalender_core import (
    ICON_192_FILE,
    ICON_512_FILE,
    KALENDER_HTML_FILE,
    SW_FILE,
    MEDIA_TYPES,
    VEREINSTERMINE_FILE,
    _HEIC_SUPPORTED,
    _PG_LABELS,
    _do_save_import,
    _make_verein_key,
    cleanup_stale_pending,
    find_similar_keys,
    gottesdienste_eintraege,
    import_pdf_bytes,
    log,
    parse_excel_bytes,
    lookup_plz,
)

kalender_bp = Blueprint("kalender", __name__)

UPLOAD_TOKEN         = os.environ.get("UPLOAD_TOKEN", "")
VKO_MAINTENANCE_FILE = Path("/opt/rename-webhook/vko_maintenance")
_import_lock         = threading.Lock()

# Felder pro Termin, die nur intern/serverseitig relevant sind (Admin-E-Mails,
# interne Dropbox-Pfade) und nie an den öffentlichen, unauthentifizierten
# /api/termine-Endpunkt ausgeliefert werden dürfen (DSGVO).
_TERMIN_INTERNE_FELDER = {
    "erstellt_von", "geaendert_von", "geloescht_von",
    "geloescht", "geloescht_am", "deleted", "flyer_path",
}

_MAINTENANCE_HTML = """<!DOCTYPE html>
<html lang="de">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vereinskalender – Wartung</title>
<style>
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       font-family:-apple-system,sans-serif;background:#f5f5f7;color:#1c1c1e}
  .box{background:#fff;border-radius:18px;padding:40px 32px;max-width:420px;width:90%;
       text-align:center;box-shadow:0 4px 24px rgba(0,0,0,.10)}
  h1{font-size:22px;font-weight:700;margin:16px 0 8px}
  p{font-size:15px;color:#555;line-height:1.6;margin:0}
  .icon{font-size:52px;margin-bottom:4px}
</style>
</head>
<body>
<div class="box">
  <div class="icon">🛠</div>
  <h1>Kurze Wartungspause</h1>
  <p>Der Vereinskalender ist vorübergehend nicht verfügbar.<br>
     Wir sind bald wieder für euch da.</p>
</div>
</body>
</html>"""

_html_cache: dict = {"data": None, "mtime": 0.0}


def _get_kalender_html() -> str:
    try:
        mtime = KALENDER_HTML_FILE.stat().st_mtime
    except OSError:
        return "<h1>kalender.html nicht gefunden</h1>"
    if _html_cache["mtime"] != mtime or _html_cache["data"] is None:
        _html_cache["data"] = KALENDER_HTML_FILE.read_text(encoding="utf-8")
        _html_cache["mtime"] = mtime
    return _html_cache["data"]


_NO_CACHE = {"Cache-Control": "no-cache, no-store"}

@kalender_bp.route("/manifest.json")
def manifest_json():
    app_name = "Veranstaltungen" if "veranstaltungen.website" in request.host else "Vereinskalender"
    return json.dumps({
        "name":             app_name,
        "short_name":       app_name,
        "start_url":        "/",
        "display":          "standalone",
        "background_color": "#ffffff",
        "theme_color":      "#1c1c1e",
        "icons": [
            {"src": "/icon-192.png?v=4", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/icon-512.png?v=4", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        ],
    }), 200, {"Content-Type": "application/manifest+json", **_NO_CACHE}


@kalender_bp.route("/icon-192.png")
def icon_192():
    if ICON_192_FILE.exists():
        return ICON_192_FILE.read_bytes(), 200, {"Content-Type": "image/png", **_NO_CACHE}
    return "", 404


@kalender_bp.route("/icon-512.png")
def icon_512():
    if ICON_512_FILE.exists():
        return ICON_512_FILE.read_bytes(), 200, {"Content-Type": "image/png", **_NO_CACHE}
    return "", 404


@kalender_bp.route("/apple-touch-icon.png")
def apple_touch_icon():
    if ICON_192_FILE.exists():
        return ICON_192_FILE.read_bytes(), 200, {"Content-Type": "image/png", **_NO_CACHE}
    return "", 404


@kalender_bp.route("/sw.js")
def service_worker():
    if SW_FILE.exists():
        return SW_FILE.read_text(encoding="utf-8"), 200, {
            "Content-Type": "application/javascript",
            "Cache-Control": "no-cache, no-store",
            "Service-Worker-Allowed": "/",
        }
    return "", 404


@kalender_bp.route("/manifest-admin.json")
def manifest_admin_json():
    return json.dumps({
        "name":             "VKO Admin",
        "short_name":       "VKO Admin",
        "start_url":        "/admin",
        "display":          "standalone",
        "background_color": "#ffffff",
        "theme_color":      "#1c1c1e",
        "icons": [
            {"src": "/icon-192.png?v=4", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/icon-512.png?v=4", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        ],
    }), 200, {"Content-Type": "application/manifest+json", **_NO_CACHE}


@kalender_bp.route("/admin")
def admin_page():
    if VKO_MAINTENANCE_FILE.exists():
        return _MAINTENANCE_HTML, 503, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}
    html = _get_kalender_html().replace(
        '<link rel="manifest" href="/manifest.json?v=4">',
        '<link rel="manifest" href="/manifest-admin.json?v=4">'
    )
    return html, 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}


@kalender_bp.route("/kalender")
def kalender_page():
    if VKO_MAINTENANCE_FILE.exists():
        return _MAINTENANCE_HTML, 503, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}
    return _get_kalender_html(), 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}


@kalender_bp.route("/upload", methods=["POST"])
def upload_kalender():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        log("⚠️  /upload: ungültiges Token")
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}
    if "file" not in request.files:
        return json.dumps({"error": "Keine Datei"}), 400, {"Content-Type": "application/json"}

    f      = request.files["file"]
    fname  = (f.filename or "").lower()
    suffix = Path(fname).suffix if fname else ""
    _KALENDER_ALLOWED = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif", ".xlsx"}

    if suffix not in _KALENDER_ALLOWED:
        return json.dumps({"error": "Nur PDF, Bilder (JPG, PNG, HEIC) oder Excel (.xlsx)"}), 400, {"Content-Type": "application/json"}
    if suffix in {".heic", ".heif"} and not _HEIC_SUPPORTED:
        return json.dumps({"error": "HEIC-Format auf diesem Server nicht verfügbar"}), 400, {"Content-Type": "application/json"}

    try:
        if suffix == ".xlsx":
            alle     = parse_excel_bytes(f.read())
            auto_plz = ""
        else:
            result   = import_pdf_bytes(f.read(), suffix)
            alle     = result["alle"]
            auto_plz = result["auto_plz"]

        try:
            data = json.loads(VEREINSTERMINE_FILE.read_text()) if VEREINSTERMINE_FILE.exists() else {}
        except Exception:
            data = {}

        known_labels = set(data.get("_labels", {}).keys())
        seen_keys: set = set()
        neue_vereine_ohne_ort: list = []
        for t in alle:
            vname = (t.get("verein") or "").strip()
            if not vname:
                continue
            vkey = _make_verein_key(vname)
            if vkey in known_labels or vkey in seen_keys:
                continue
            seen_keys.add(vkey)
            similar = find_similar_keys(vkey, data.get("_labels", {}))
            entry = {"key": vkey, "name": vname}
            if similar:
                entry["similar_to"] = similar
            neue_vereine_ohne_ort.append(entry)

        if neue_vereine_ohne_ort:
            cleanup_stale_pending()
            import_id = str(_uuid.uuid4())
            form_plz  = request.form.get("plz", "").strip()
            Path(f"/tmp/vk_pending_{import_id}.json").write_text(
                json.dumps({
                    "import_id": import_id,
                    "alle":      alle,
                    "auto_plz":  auto_plz,
                    "form_plz":  form_plz,
                }, ensure_ascii=False)
            )
            log(f"⏳  Upload ausstehend: {len(neue_vereine_ohne_ort)} neue Vereine")
            all_labels_list = sorted(
                [{"key": k, "name": v} for k, v in data.get("_labels", {}).items()],
                key=lambda x: x["name"].lower()
            )
            return (
                json.dumps({
                    "pending":                     True,
                    "import_id":                   import_id,
                    "neue_vereine_ohne_ortschaft": neue_vereine_ohne_ort,
                    "all_labels_list":             all_labels_list,
                    "preview": {
                        "termine_count": len(alle),
                        "vereine":       sorted({t.get("verein", "") for t in alle}),
                    },
                }, ensure_ascii=False),
                200,
                {"Content-Type": "application/json; charset=utf-8"},
            )

        form_plz           = request.form.get("plz", "").strip()
        result_vereine, total = _do_save_import(alle, auto_plz, form_plz)
        return (
            json.dumps({"success": True, "vereine": result_vereine, "total": total}, ensure_ascii=False),
            200,
            {"Content-Type": "application/json; charset=utf-8"},
        )

    except Exception as ex:
        log(f"❌  /upload Fehler: {ex}")
        return json.dumps({"error": str(ex)}), 500, {"Content-Type": "application/json"}


@kalender_bp.route("/api/check-token", methods=["POST"])
def api_check_token():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return "", 401
    return "", 200


_NGINX_LOG  = Path("/var/log/nginx/vereinskalender.access.log")
_MONTHS_MAP = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
               "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}


def _stats_log_files(n: int) -> list[Path]:
    files = [_NGINX_LOG] if _NGINX_LOG.exists() else []
    for i in range(1, n + 1):
        p  = _NGINX_LOG.parent / f"{_NGINX_LOG.name}.{i}"
        gz = _NGINX_LOG.parent / f"{_NGINX_LOG.name}.{i}.gz"
        if p.exists():   files.append(p)
        elif gz.exists(): files.append(gz)
    return files


def _stats_read_lines(path: Path) -> list[str]:
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", errors="ignore") as f:
                return f.readlines()
        return path.read_text(errors="ignore").splitlines()
    except Exception:
        return []


def _stats_parse_dt(line: str) -> datetime | None:
    """Zeitstempel einer nginx-Zeile als zeitzonenbewusstes datetime (Offset aus dem Log,
    sonst Europe/Berlin). Vorher naiv + später als UTC gedeutet → „heute“ um 2 h verschoben."""
    m = re.search(r'\[(\d{2})/(\w{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2})(?: ([+-])(\d{2})(\d{2}))?', line)
    if not m:
        return None
    d, mo, y, h, mi, s, vz, oh, om = m.groups()
    try:
        if vz:
            tz = _tz((1 if vz == "+" else -1) * timedelta(hours=int(oh), minutes=int(om)))
        else:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("Europe/Berlin")
        return datetime(int(y), _MONTHS_MAP[mo], int(d), int(h), int(mi), int(s), tzinfo=tz)
    except (KeyError, ValueError):
        return None


def _stats_anon_ip(raw: str) -> str:
    try:
        addr = ipaddress.ip_address(raw)
        if isinstance(addr, ipaddress.IPv4Address):
            return str(ipaddress.ip_network(f"{raw}/24", strict=False).network_address)
        return str(ipaddress.ip_network(f"{raw}/48", strict=False).network_address)
    except ValueError:
        return "unknown"


def _count_page_views() -> tuple[int, int, int, int]:
    from zoneinfo import ZoneInfo
    now    = datetime.now(ZoneInfo("Europe/Berlin"))
    heute  = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = now - timedelta(days=7)
    h_cnt = w_cnt = 0
    h_ips: set[str] = set()
    w_ips: set[str] = set()
    ip_pat = re.compile(r'^(\S+)')
    for log_file in _stats_log_files(7):
        for line in _stats_read_lines(log_file):
            if '"GET /kalender' not in line and '"GET / ' not in line:
                continue
            dt = _stats_parse_dt(line)
            if dt is None:
                continue
            m = ip_pat.match(line)
            anon = _stats_anon_ip(m.group(1)) if m else "unknown"
            if dt >= heute:
                h_cnt += 1; h_ips.add(anon)
            if dt >= cutoff:
                w_cnt += 1; w_ips.add(anon)
    return h_cnt, w_cnt, len(h_ips), len(w_ips)


def _count_today_live() -> tuple[int, int]:
    """Liest die aktuellen heutigen Aufrufe live aus dem nginx-Log (Europe/Berlin)."""
    from zoneinfo import ZoneInfo
    berlin = ZoneInfo("Europe/Berlin")
    today  = datetime.now(berlin).date()
    v = 0
    ips: set[str] = set()
    ip_pat = re.compile(r'^(\S+)')
    for log_file in _stats_log_files(2):
        for line in _stats_read_lines(log_file):
            if '"GET /kalender' not in line and '"GET / ' not in line:
                continue
            dt = _stats_parse_dt(line)
            if dt is None:
                continue
            if dt.astimezone(berlin).date() != today:
                continue
            m = ip_pat.match(line)
            ips.add(_stats_anon_ip(m.group(1)) if m else "unknown")
            v += 1
    return v, len(ips)


@kalender_bp.route("/api/admin/stats/chart", methods=["GET"])
def api_admin_stats_chart():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}

    from zoneinfo import ZoneInfo
    from shared.vk_db import get_page_stats
    berlin = ZoneInfo("Europe/Berlin")
    today  = datetime.now(berlin).date()

    try:
        d = int(request.args.get("d", "30"))
    except ValueError:
        d = 30
    d = min(max(d, 7), 3650)

    from_date = (today - timedelta(days=d - 1)).isoformat()
    to_date   = today.isoformat()

    db_rows = {r["datum"]: r for r in get_page_stats(from_date, to_date)}

    # Heute immer live aus Logs (Cron läuft erst um 00:05 für den Vortag)
    tv, tu = _count_today_live()
    db_rows[today.isoformat()] = {"datum": today.isoformat(), "views": tv, "unique_visitors": tu}

    # DE-Besucher pro Tag aus page_stats_geo (Cron-Daten, kein heutiger Live-Wert)
    de_rows: dict = {}
    try:
        with db_conn() as conn:
            rows = conn.execute(
                "SELECT datum, SUM(besucher) AS n FROM page_stats_geo "
                "WHERE datum >= ? AND datum <= ? AND land = 'Deutschland' GROUP BY datum",
                (from_date, to_date),
            ).fetchall()
            de_rows = {r["datum"]: (r["n"] or 0) for r in rows}
    except Exception:
        pass

    result = []
    for i in range(d - 1, -1, -1):
        day = (today - timedelta(days=i)).isoformat()
        r   = db_rows.get(day)
        result.append({
            "datum":     day,
            "views":     r["views"]           if r else 0,
            "unique":    r["unique_visitors"]  if r else 0,
            "unique_de": de_rows.get(day, 0),
        })

    return json.dumps({"tage": result}, ensure_ascii=False), 200, {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
    }


@kalender_bp.route("/api/admin/stats/hourly", methods=["GET"])
def api_admin_stats_hourly():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}

    from zoneinfo import ZoneInfo
    berlin = ZoneInfo("Europe/Berlin")
    today  = datetime.now(berlin).date()

    try:
        d = int(request.args.get("d", "30"))
    except ValueError:
        d = 30
    d = min(max(d, 1), 365)
    from_date = (today - timedelta(days=d - 1)).isoformat()

    # Stunden aus DB aggregieren
    hourly = [0] * 24
    try:
        with db_conn() as conn:
            rows = conn.execute(
                "SELECT stunde, SUM(views) AS total FROM page_stats_hourly "
                "WHERE datum >= ? GROUP BY stunde",
                (from_date,),
            ).fetchall()
            for r in rows:
                if 0 <= r["stunde"] <= 23:
                    hourly[r["stunde"]] = r["total"] or 0
    except Exception:
        pass

    # Heute live aus Log hinzuzählen
    try:
        import re as _re
        from pathlib import Path as _Path
        from datetime import timezone as _tz, timedelta as _td
        from zoneinfo import ZoneInfo as _ZI
        _berlin = _ZI("Europe/Berlin")
        _log = _Path("/var/log/nginx/vereinskalender.access.log")
        _months = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
                   "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
        if _log.exists():
            for line in _log.read_text(errors="ignore").splitlines():
                if '"GET /kalender' not in line and '"GET / ' not in line:
                    continue
                m = _re.search(
                    r'\[(\d{2})/(\w{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})\]',
                    line
                )
                if not m:
                    continue
                dd, mo, yy, hh, mm, ss = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6)
                sign = 1 if m.group(7) == "+" else -1
                off = _tz(sign * _td(hours=int(m.group(8)), minutes=int(m.group(9))))
                try:
                    dt = datetime(int(yy), _months[mo], int(dd), int(hh), int(mm), int(ss), tzinfo=off)
                    if dt.astimezone(_berlin).date() == today:
                        hourly[dt.astimezone(_berlin).hour] += 1
                except Exception:
                    pass
    except Exception:
        pass

    return json.dumps({"stunden": hourly, "tage": d}, ensure_ascii=False), 200, {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
    }


@kalender_bp.route("/api/admin/stats/geo", methods=["GET"])
def api_admin_stats_geo():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}

    try:
        d = int(request.args.get("d", "30"))
    except ValueError:
        d = 30
    d = min(max(d, 1), 365)

    from datetime import date as _date
    von = (_date.today() - timedelta(days=d)).isoformat()

    laender = []
    staedte_de = []
    try:
        with db_conn() as conn:
            rows = conn.execute(
                "SELECT land, SUM(besucher) AS n FROM page_stats_geo "
                "WHERE datum > ? GROUP BY land ORDER BY n DESC LIMIT 20",
                (von,),
            ).fetchall()
            laender = [{"land": r["land"], "besucher": r["n"]} for r in rows]

            rows = conn.execute(
                "SELECT stadt, SUM(besucher) AS n FROM page_stats_geo "
                "WHERE datum > ? AND land = 'Deutschland' AND stadt != '' "
                "GROUP BY stadt ORDER BY n DESC LIMIT 20",
                (von,),
            ).fetchall()
            staedte_de = [{"stadt": r["stadt"], "besucher": r["n"]} for r in rows]
    except Exception:
        pass

    return json.dumps({"laender": laender, "staedte_de": staedte_de, "tage": d},
                      ensure_ascii=False), 200, {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
    }


def _dokumente_statistik() -> dict | None:
    """Nur Zahlen (Anzahl, Vereine, MB) – Inhalte der Vereinsdokumente sieht der Admin nicht (ADR-029)."""
    try:
        from shared.dokumente_db import statistik
        return statistik()
    except Exception:
        return None


def _datenschutz_statistik() -> dict | None:
    """Wie viele nutzbare Konten (aktiv, E-Mail bestätigt, Verein freigegeben) die aktuelle Fassung bestätigt haben."""
    try:
        from shared.vk_db import DS_FASSUNG, db_conn
        with db_conn() as c:
            r = c.execute("SELECT COUNT(*) AS n, COALESCE(SUM(u.ds_fassung = ?), 0) AS ok FROM vk_users u"
                          " JOIN vereine_accounts v ON v.id = u.verein_id"
                          " WHERE u.aktiv = 1 AND u.email_verified = 1 AND v.status = 'aktiv'", (DS_FASSUNG,)).fetchone()
        with db_conn() as c:
            avv = c.execute("SELECT COUNT(*) AS n FROM vereine_accounts WHERE avv_fassung IS NOT NULL").fetchone()["n"]
        return {"fassung": DS_FASSUNG, "bestaetigt": r["ok"], "konten": r["n"], "avv_vereine": avv}
    except Exception:
        return None


@kalender_bp.route("/api/admin/stats", methods=["GET"])
def api_admin_stats():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}

    h_views, w_views, h_unique, w_unique = _count_page_views()

    vereine_gesamt = vereine_aktiv = termine_kd = 0
    try:
        raw   = json.loads(VEREINSTERMINE_FILE.read_text()) if VEREINSTERMINE_FILE.exists() else {}
        heute = datetime.now().strftime("%Y-%m-%d")
        for key, items in raw.items():
            if key.startswith("_") or not isinstance(items, list):
                continue
            vereine_gesamt += 1
            kuenftige = [t for t in items if not t.get("geloescht") and t.get("datum", "") >= heute]
            if kuenftige:
                vereine_aktiv += 1
            termine_kd += len(kuenftige)
    except Exception:
        pass

    letzter_import = "–"
    from shared.kalender_core import LAST_IMPORT_FILE as last_import_file
    try:
        if last_import_file.exists():
            li = json.loads(last_import_file.read_text())
            dt = datetime.strptime(li["datum"], "%Y-%m-%d %H:%M")
            letzter_import = f"{dt.strftime('%d.%m.%Y, %H:%M')} ({li['termine']} Termine, {li['vereine']} Vereine)"
    except Exception:
        pass

    from zoneinfo import ZoneInfo
    jetzt = datetime.now(ZoneInfo("Europe/Berlin")).strftime("%d.%m.%Y, %H:%M")

    tg_subscribers = 0
    tg_vereine_count = 0
    tg_ranking = []
    ical_7d = 0
    ical_30d = 0
    ical_vereine_count = 0
    ical_ranking = []
    try:
        from datetime import date as _d, timedelta as _td
        _heute = _d.today()
        _d7  = (_heute - _td(days=7)).isoformat()
        _d30 = (_heute - _td(days=30)).isoformat()
        with db_conn() as conn:
            r = conn.execute("SELECT COUNT(DISTINCT chat_id) AS n FROM tg_subscriptions").fetchone()
            tg_subscribers = r["n"] if r else 0
            r = conn.execute("SELECT COUNT(DISTINCT verein_key) AS n FROM tg_subscriptions").fetchone()
            tg_vereine_count = r["n"] if r else 0
            rows = conn.execute(
                "SELECT verein_key, COUNT(chat_id) AS abos FROM tg_subscriptions GROUP BY verein_key ORDER BY abos DESC"
            ).fetchall()
            _labels = {}
            try:
                _raw = json.loads(VEREINSTERMINE_FILE.read_text()) if VEREINSTERMINE_FILE.exists() else {}
                _labels = _raw.get("_labels", {})
            except Exception:
                pass
            tg_ranking = [
                {"key": row["verein_key"], "name": _labels.get(row["verein_key"], row["verein_key"]), "abos": row["abos"]}
                for row in rows
            ]
            r = conn.execute("SELECT COUNT(*) AS n FROM ical_feed_requests WHERE date >= ?", (_d7,)).fetchone()
            ical_7d = r["n"] if r else 0
            r = conn.execute("SELECT COUNT(*) AS n FROM ical_feed_requests WHERE date >= ?", (_d30,)).fetchone()
            ical_30d = r["n"] if r else 0
            r = conn.execute("SELECT COUNT(DISTINCT verein_key) AS n FROM ical_feed_vereine WHERE date >= ?", (_d7,)).fetchone()
            ical_vereine_count = r["n"] if r else 0
            rows = conn.execute(
                "SELECT verein_key, COUNT(DISTINCT ip_hash) AS abos FROM ical_feed_vereine "
                "WHERE date >= ? GROUP BY verein_key ORDER BY abos DESC",
                (_d7,),
            ).fetchall()
            ical_ranking = [
                {"key": row["verein_key"], "name": _labels.get(row["verein_key"], row["verein_key"]), "abos": row["abos"]}
                for row in rows
            ]
    except Exception:
        pass

    unique_heute_de = 0
    unique_7d_de    = 0
    try:
        from datetime import date as _date
        _heute_str = _date.today().isoformat()
        _d7_str    = (_date.today() - timedelta(days=6)).isoformat()
        with db_conn() as conn:
            r = conn.execute(
                "SELECT SUM(besucher) AS n FROM page_stats_geo WHERE datum = ? AND land = 'Deutschland'",
                (_heute_str,),
            ).fetchone()
            unique_heute_de = (r["n"] or 0) if r else 0
            r = conn.execute(
                "SELECT SUM(besucher) AS n FROM page_stats_geo WHERE datum >= ? AND land = 'Deutschland'",
                (_d7_str,),
            ).fetchone()
            unique_7d_de = (r["n"] or 0) if r else 0
    except Exception:
        pass

    return json.dumps({
        "aufrufe_heute":      h_views,
        "aufrufe_7d":         w_views,
        "unique_heute":       h_unique,
        "unique_7d":          w_unique,
        "unique_heute_de":    unique_heute_de,
        "unique_7d_de":       unique_7d_de,
        "vereine_gesamt":     vereine_gesamt,
        "vereine_aktiv":      vereine_aktiv,
        "termine_kd":         termine_kd,
        "letzter_import":     letzter_import,
        "timestamp":          jetzt,
        "tg_subscribers":     tg_subscribers,
        "tg_vereine_count":   tg_vereine_count,
        "tg_ranking":         tg_ranking,
        "ical_7d":            ical_7d,
        "ical_30d":           ical_30d,
        "ical_vereine_count": ical_vereine_count,
        "ical_ranking":       ical_ranking,
        "dokumente":          _dokumente_statistik(),
        "datenschutz":        _datenschutz_statistik(),
    }, ensure_ascii=False), 200, {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}


@kalender_bp.route("/api/confirm-import", methods=["POST"])
def api_confirm_import():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}

    body               = request.get_json(silent=True) or {}
    import_id          = body.get("import_id", "")
    verein_ortschaften = {k: v for k, v in (body.get("verein_ortschaften") or {}).items() if v and v.strip()}
    key_remappings     = {k: v for k, v in (body.get("key_remappings") or {}).items() if k and v}

    cleanup_stale_pending()
    pending_path = Path(f"/tmp/vk_pending_{import_id}.json")
    if not pending_path.exists():
        return json.dumps({"error": "Import nicht gefunden oder abgelaufen"}), 404, {"Content-Type": "application/json"}

    try:
        pending  = json.loads(pending_path.read_text())
        alle     = pending["alle"]
        auto_plz = pending.get("auto_plz", "")
        form_plz = pending.get("form_plz", "")

        result_vereine, total = _do_save_import(alle, auto_plz, form_plz, verein_ortschaften, key_remappings or None)
        pending_path.unlink(missing_ok=True)
        log(f"✅  Confirm-Import: {total} Termine")

        return (
            json.dumps({"success": True, "vereine": result_vereine, "total": total}, ensure_ascii=False),
            200,
            {"Content-Type": "application/json; charset=utf-8"},
        )
    except Exception as e:
        log(f"❌  /api/confirm-import: {e}")
        return json.dumps({"error": str(e)}), 500, {"Content-Type": "application/json"}


def _merged_meta(raw: dict) -> dict:
    """Vereins-Meta wie die App sie sieht: DB (Vereinsadmin-Selbstverwaltung, Prio 2), JSON überschreibt
    (Superadmin, Prio 1). Gemeinsam für /api/termine und das Favoriten-Abo, damit beide denselben
    Vereinssitz kennen (Mischregel, ADR-025)."""
    json_meta = raw.get("_meta", {})
    try:
        with db_conn() as conn:
            db_rows = conn.execute(
                """SELECT verein_key, rubrik, heimatort, plz, gemeinde, landkreis
                   FROM vereine_accounts WHERE verein_key IS NOT NULL"""
            ).fetchall()
    except Exception:
        db_rows = []
    db_meta = {}
    for row in db_rows:
        entry = {}
        for col in ("rubrik", "heimatort", "plz", "gemeinde", "landkreis"):
            if row[col]:
                entry[col] = row[col]
        if entry:
            db_meta[row["verein_key"]] = entry

    merged_meta = {}
    for key in set(list(json_meta.keys()) + list(db_meta.keys())):
        m = {**db_meta.get(key, {}), **json_meta.get(key, {})}
        if m:
            merged_meta[key] = m
    return merged_meta


def _get_rubrik(key: str, name: str, meta_entry: dict) -> str:
    if "rubrik" in meta_entry:
        return meta_entry["rubrik"]
    if "pfarr" in name.lower():
        return "Pfarrei"
    return "Verein"


@kalender_bp.route("/api/vereine", methods=["GET"])
def api_vereine_get():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}
    try:
        raw = json.loads(VEREINSTERMINE_FILE.read_text()) if VEREINSTERMINE_FILE.exists() else {}
    except Exception:
        raw = {}
    labels = raw.get("_labels", {})
    meta   = raw.get("_meta", {})
    result = []
    for key, name in labels.items():
        m          = meta.get(key, {})
        parts      = name.strip().split()
        last_word  = parts[-1].split("/")[0] if parts else ""
        derived    = last_word if len(last_word) > 4 else ""
        result.append({
            "key":                key,
            "name":               name,
            "heimatort":          m.get("heimatort", derived),
            "heimatort_explizit": "heimatort" in m,
            "plz":                m.get("plz", ""),
            "gemeinde":           m.get("gemeinde", ""),
            "landkreis":          m.get("landkreis", ""),
            "rubrik":             _get_rubrik(key, name, m),
            "selbstverwaltung":   bool(m.get("selbstverwaltung", False)),
        })
    n_termine = {}
    for vkey, events in raw.items():
        if vkey.startswith("_") or not isinstance(events, list):
            continue
        n_termine[vkey] = sum(1 for t in events if not t.get("geloescht") and not t.get("deleted"))
    for r in result:
        r["nTermine"] = n_termine.get(r["key"], 0)
    result.sort(key=lambda x: x["name"].lower())
    return json.dumps(result, ensure_ascii=False), 200, {
        "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-cache"}


@kalender_bp.route("/api/vereine", methods=["POST"])
def api_vereine_post():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}
    body = request.get_json(silent=True) or {}
    key  = body.get("key", "").strip()
    if not key:
        return json.dumps({"error": "key fehlt"}), 400, {"Content-Type": "application/json"}
    # PLZ-Lookup (Netzwerk, ≥1 s) vor dem Lock; Lesen + Ändern + Schreiben komplett im
    # Mutator auf dem aktuellen Stand – nie einen vorher gelesenen Snapshot zurückschreiben
    # (Pitfall 2026-07-05, Review 2026-10-04 Punkt 3).
    plz = str(body.get("plz") or "").strip()
    new_meta = lookup_plz(plz) if plz and re.match(r"^\d{5}$", plz) else None
    stand: dict = {}

    def _mut(d):
        labels = d.get("_labels", {})
        if key not in labels:
            return
        if str(body.get("name") or "").strip():
            labels[key] = body["name"].strip()
        m = d.setdefault("_meta", {}).setdefault(key, {})
        for feld in ("rubrik", "heimatort", "gemeinde", "landkreis"):
            if feld in body:
                val = str(body[feld] or "").strip()
                if val:
                    m[feld] = val
                else:
                    m.pop(feld, None)
        if "selbstverwaltung" in body:
            if body["selbstverwaltung"]:
                m["selbstverwaltung"] = True
            else:
                m.pop("selbstverwaltung", None)
        if new_meta:
            saved_heimatort = m.get("heimatort")
            m.update(new_meta)
            if saved_heimatort:
                m["heimatort"] = saved_heimatort
        stand["labels"], stand["meta"] = dict(labels), dict(m)

    from shared.kalender_store import KalenderStore
    KalenderStore.update(_mut)
    if not stand:
        return json.dumps({"error": "Verein nicht gefunden"}), 404, {"Content-Type": "application/json"}
    labels = stand["labels"]
    log(f"✏️  Verein {key} ({labels.get(key)}) aktualisiert")
    m2    = stand["meta"]
    parts = labels.get(key, "").strip().split()
    lw    = parts[-1].split("/")[0] if parts else ""
    return json.dumps({
        "ok": True,
        "key": key, "name": labels.get(key, ""),
        "heimatort": m2.get("heimatort", lw if len(lw) > 4 else ""),
        "heimatort_explizit": "heimatort" in m2,
        "plz": m2.get("plz", ""), "gemeinde": m2.get("gemeinde", ""),
        "landkreis": m2.get("landkreis", ""),
        "rubrik": _get_rubrik(key, labels.get(key, ""), m2),
        "selbstverwaltung": bool(m2.get("selbstverwaltung", False)),
    }, ensure_ascii=False), 200, {"Content-Type": "application/json; charset=utf-8"}


@kalender_bp.route("/api/vereine/plz/<plz>", methods=["GET"])
def api_vereine_plz(plz):
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}
    if not re.match(r"^\d{5}$", plz):
        return json.dumps({"error": "Ungültige PLZ"}), 400, {"Content-Type": "application/json"}
    return json.dumps(lookup_plz(plz), ensure_ascii=False), 200, {"Content-Type": "application/json; charset=utf-8"}




@kalender_bp.route("/api/vereine/<key>", methods=["DELETE"])
def api_vereine_delete(key):
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}
    stand: dict = {}

    def _mut(d):
        if key not in d.get("_labels", {}):
            return
        stand["name"] = d["_labels"].pop(key, key)
        d.get("_meta", {}).pop(key, None)
        stand["n"] = len(d.pop(key, []))

    from shared.kalender_store import KalenderStore
    KalenderStore.update(_mut)
    if not stand:
        return json.dumps({"error": "Verein nicht gefunden"}), 404, {"Content-Type": "application/json"}
    name, geloescht = stand["name"], stand["n"]
    log("Verein geloescht: " + key + " (" + name + "), " + str(geloescht) + " Termine entfernt")
    return json.dumps({"ok": True, "geloescht": geloescht}, ensure_ascii=False), 200, {"Content-Type": "application/json"}


@kalender_bp.route("/api/termine")
def api_termine():
    if VKO_MAINTENANCE_FILE.exists():
        return json.dumps({"error": "Wartung"}), 503, {"Content-Type": "application/json"}
    return (
        json.dumps(oeffentliche_termine(), ensure_ascii=False),
        200,
        {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-cache, must-revalidate"},
    )


def oeffentliche_termine(raw: dict | None = None) -> dict:
    """{labels, termine, meta, rubriken} wie `/api/termine` – auch für den Vereinsbereich (Kollisionen,
    Vorjahres-Vorlage, Planungsrunden), damit dort dieselbe Sicht gilt wie im Kalender."""
    if raw is None:
        try:
            raw = json.loads(VEREINSTERMINE_FILE.read_text())
        except Exception:
            raw = {}
    labels = raw.get("_labels", {})
    labels.setdefault("ff", "FF Hölskofen")
    labels.setdefault("kp", "Königstreue Patrioten Hölskofen")
    termine = []
    for key, events in raw.items():
        if key.startswith("_") or not isinstance(events, list):
            continue
        for t in events:
            if t.get("geloescht") or t.get("deleted"):
                continue
            t_public = {k: v for k, v in t.items() if k not in _TERMIN_INTERNE_FELDER}
            termine.append({**t_public, "verein": key})

    for vkey, t in gottesdienste_eintraege(raw):
        labels[vkey] = _PG_LABELS[vkey]
        termine.append({**t, "verein": vkey})

    merged_meta = _merged_meta(raw)
    rubriken    = {k: _get_rubrik(k, v, merged_meta.get(k, {})) for k, v in labels.items()}
    # Standard-Quelle je Verein (`_meta[key].quelle`, z. B. „Pfarrbrief“ für Postau-Moosthann): gilt für Termine
    # ohne eigene Quelle – so tragen auch künftige Importe dieses Vereins sie, ohne Daten umzuschreiben.
    for t in termine:
        if not t.get("quelle") and (merged_meta.get(t["verein"]) or {}).get("quelle"):
            t["quelle"] = merged_meta[t["verein"]]["quelle"]

    # Geo-Zuordnung am Termin (ADR-014). Ein Fehler im Resolver darf die Terminliste nie kippen;
    # ohne _geo (orte.json fehlt = Kill-Switch) fällt das Frontend auf das Verein-Verhalten zurück.
    for t in termine:
        try:
            g = geo_fuer_termin(t, merged_meta.get(t["verein"]), labels.get(t["verein"], ""),
                                raw.get("_orte_zuordnung"))
        except Exception as ex:
            log(f"⚠️  geo_fuer_termin: {ex}")
            g = None
        if g is not None:
            t["_geo"] = g

    return {"labels": labels, "termine": termine, "meta": merged_meta, "rubriken": rubriken}


def _termin_passt(t: dict, termin_id: str, datum: str, bezeichnung: str) -> bool:
    """Admin-Endpunkte: Termin über seine `id` finden (eindeutig, seit v1.37 hat jeder Termin
    eine). Datum+Bezeichnung nur noch als Rückfall für Aufrufe ohne ID."""
    if termin_id:
        return t.get("id") == termin_id
    return t.get("datum") == datum and t.get("bezeichnung") == bezeichnung


@kalender_bp.route("/api/termine", methods=["PATCH"])
def api_termine_patch():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}
    body = request.get_json(force=True, silent=True) or {}
    verein_key = body.get("verein_key", "")
    old_datum = body.get("datum", "")
    old_bezeichnung = body.get("bezeichnung", "")
    new_verein_key = body.get("new_verein_key", "").strip()
    termin_id = str(body.get("id", "") or "").strip()
    changes = {k: v for k, v in body.get("changes", {}).items()
               if k in {"datum", "uhrzeit", "uhrzeit_bis", "ort", "ortschaft", "bezeichnung", "beschreibung"}}
    if not verein_key or not old_datum or not old_bezeichnung:
        return json.dumps({"error": "verein_key, datum und bezeichnung erforderlich"}), 400, {"Content-Type": "application/json"}
    found = [False]
    fehler = [""]
    changes = {k: (v.strip() if isinstance(v, str) else v) for k, v in changes.items()}
    _sort = lambda lst: sorted(lst, key=lambda x: (x.get("datum", ""), x.get("uhrzeit", "")))
    def mutator(data):
        liste = data.get(verein_key, [])
        for i, t in enumerate(liste):
            if t.get("geloescht") or t.get("deleted"):
                continue  # gelöschte Kopie mit gleichem Datum+Titel nie treffen (Admin sieht sie nicht)
            if _termin_passt(t, termin_id, old_datum, old_bezeichnung):
                # 4: bis-Uhrzeit/Beschreibung wie im Vereinsformular prüfen (gegen den Endstand)
                fehler[0] = zeit_fehler(changes.get("uhrzeit", t.get("uhrzeit", "")),
                                        changes.get("uhrzeit_bis", t.get("uhrzeit_bis", "")))
                if len(changes.get("beschreibung", "")) > BESCHREIBUNG_MAX:
                    fehler[0] = f"Beschreibung höchstens {BESCHREIBUNG_MAX} Zeichen."
                if "datum" in changes and not datum_ok(changes["datum"]):
                    fehler[0] = "Datum muss ein gültiges Datum im Format YYYY-MM-DD sein."
                found[0] = True
                if fehler[0]:
                    return
                for feld in ("uhrzeit_bis", "beschreibung"):  # leer = Feld entfernen, wie im Vereinsformular
                    if feld in changes and not changes[feld]:
                        changes.pop(feld); t.pop(feld, None)
                t.update(changes)
                if new_verein_key and new_verein_key != verein_key:
                    moved = liste.pop(i)
                    data[verein_key] = _sort(liste)
                    data.setdefault(new_verein_key, []).append(moved)
                    data[new_verein_key] = _sort(data[new_verein_key])
                elif "datum" in changes:
                    data[verein_key] = _sort(liste)
                break
    from shared.kalender_store import KalenderStore
    KalenderStore.update(mutator)
    if not found[0]:
        return json.dumps({"error": "Termin nicht gefunden"}), 404, {"Content-Type": "application/json"}
    if fehler[0]:
        return json.dumps({"error": fehler[0]}, ensure_ascii=False), 400, {"Content-Type": "application/json; charset=utf-8"}
    log(f"Termin bearbeitet: {verein_key} / {old_datum} / {old_bezeichnung}" + (f" → {new_verein_key}" if new_verein_key else ""))
    return json.dumps({"ok": True}, ensure_ascii=False), 200, {"Content-Type": "application/json; charset=utf-8"}


@kalender_bp.route("/api/termine/flyer", methods=["POST"])
def api_termine_flyer():
    """Admin: Flyer eines Termins hochladen/ersetzen (aktion=hochladen, Datei 'flyer') oder entfernen.
    Termin wie bei PATCH/DELETE über verein_key + datum + bezeichnung (multipart-Formular)."""
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}
    verein_key = request.form.get("verein_key", "")
    datum = request.form.get("datum", "")
    bezeichnung = request.form.get("bezeichnung", "")
    aktion = request.form.get("aktion", "hochladen")
    termin_id = request.form.get("id", "").strip()
    if not verein_key or not datum or not bezeichnung or aktion not in ("hochladen", "entfernen"):
        return json.dumps({"error": "verein_key, datum, bezeichnung und aktion erforderlich"}), 400, {"Content-Type": "application/json"}
    neu_url = neu_pfad = ""
    if aktion == "hochladen":
        datei = request.files.get("flyer")
        if not datei or not datei.filename:
            return json.dumps({"error": "Keine Datei"}), 400, {"Content-Type": "application/json"}
        try:
            neu_url, neu_pfad = upload_flyer(datei.read())  # Netzwerk-Call vor dem Lock
        except ValueError as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False), 400, {"Content-Type": "application/json; charset=utf-8"}
        except Exception as e:
            log(f"⚠️  Flyer-Upload (Admin) fehlgeschlagen: {type(e).__name__}")
            return json.dumps({"error": "Flyer-Upload zu Dropbox fehlgeschlagen. Bitte erneut versuchen."}, ensure_ascii=False), 502, {"Content-Type": "application/json; charset=utf-8"}
    alt = {"pfad": "", "gefunden": False}

    def mutator(data):
        for t in data.get(verein_key, []):
            if not t.get("geloescht") and not t.get("deleted") and _termin_passt(t, termin_id, datum, bezeichnung):
                alt["gefunden"] = True
                alt["pfad"] = t.get("flyer_path", "")
                if neu_url:
                    t["flyer_url"], t["flyer_path"] = neu_url, neu_pfad
                else:
                    t.pop("flyer_url", None)
                    t.pop("flyer_path", None)
                break
    from shared.kalender_store import KalenderStore
    try:
        KalenderStore.update(mutator)
    except Exception:
        if neu_pfad:
            delete_flyer(neu_pfad)
        raise
    if not alt["gefunden"]:
        if neu_pfad:
            delete_flyer(neu_pfad)
        return json.dumps({"error": "Termin nicht gefunden"}), 404, {"Content-Type": "application/json"}
    if alt["pfad"] and alt["pfad"] != neu_pfad:
        delete_flyer(alt["pfad"])
    log(f"Flyer {aktion}: {verein_key} / {datum} / {bezeichnung}")
    return json.dumps({"ok": True, "flyer_url": neu_url}, ensure_ascii=False), 200, {"Content-Type": "application/json; charset=utf-8"}


@kalender_bp.route("/api/termine", methods=["DELETE"])
def api_termine_delete():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}
    body = request.get_json(force=True, silent=True) or {}
    verein_key = body.get("verein_key", "")
    old_datum = body.get("datum", "")
    old_bezeichnung = body.get("bezeichnung", "")
    termin_id = str(body.get("id", "") or "").strip()
    if not verein_key or not old_datum or not old_bezeichnung:
        return json.dumps({"error": "verein_key, datum und bezeichnung erforderlich"}), 400, {"Content-Type": "application/json"}
    found = [False]
    flyer = {"pfad": ""}

    # Soft-Delete wie im Vereinsformular: der Termin bleibt als `geloescht` in der Datei,
    # damit ihn der wöchentliche Gemeinde-Import als Duplikat erkennt und nicht wieder
    # anlegt (Review 2026-10-04, Punkt 7). Öffentliche API/Feed filtern `geloescht`.
    def mutator(data):
        for t in data.get(verein_key, []):
            if t.get("geloescht") or t.get("deleted"):
                continue
            if _termin_passt(t, termin_id, old_datum, old_bezeichnung):
                t["geloescht"] = True
                t["geloescht_von"] = "admin"
                t["geloescht_am"] = datetime.utcnow().isoformat()[:19]
                flyer["pfad"] = t.pop("flyer_path", "") or ""
                t.pop("flyer_url", None)
                found[0] = True
                break
    from shared.kalender_store import KalenderStore
    KalenderStore.update(mutator)
    if not found[0]:
        return json.dumps({"error": "Termin nicht gefunden"}), 404, {"Content-Type": "application/json"}
    if flyer["pfad"]:
        delete_flyer(flyer["pfad"])
    log(f"Termin geloescht: {verein_key} / {old_datum} / {old_bezeichnung}")
    return json.dumps({"ok": True}, ensure_ascii=False), 200, {"Content-Type": "application/json; charset=utf-8"}


def _ics_zeiten(tag: date, uhrzeit: str, uhrzeit_bis: str = "") -> tuple[str, str]:
    """DTSTART/DTEND für einen Termin. Ohne Uhrzeit ganztägig, ohne Ende +1 Stunde
    (höchstens bis 23:59), Ende vor Beginn = endet am Folgetag. ValueError bei kaputter Uhrzeit."""
    def stamp(t: datetime) -> str:
        return t.strftime("%Y%m%dT%H%M00")
    if not uhrzeit:
        return (f"DTSTART;VALUE=DATE:{tag.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{(tag + timedelta(days=1)).strftime('%Y%m%d')}")
    start = datetime.combine(tag, datetime.strptime(uhrzeit, "%H:%M").time())
    if uhrzeit_bis:
        ende = datetime.combine(tag, datetime.strptime(uhrzeit_bis, "%H:%M").time())
        if ende <= start:
            ende += timedelta(days=1)
    else:
        ende = min(start + timedelta(hours=1), datetime.combine(tag, datetime.max.time()).replace(second=0, microsecond=0))
    return f"DTSTART:{stamp(start)}", f"DTEND:{stamp(ende)}"


def _ics_escape(text: str) -> str:
    """RFC 5545 TEXT-Escaping (Backslash, Komma, Semikolon, Zeilenumbrüche).

    Ohne dieses Escaping könnten Vereinsadmins über Bezeichnung/Ort einen
    Zeilenumbruch einschleusen und damit zusätzliche iCal-Properties/-Events
    in den geteilten Feed injizieren.
    """
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


@kalender_bp.route("/api/ical")
def api_ical():
    datum   = request.args.get("d", "").strip()
    titel   = request.args.get("t", "").strip()
    label   = request.args.get("v", "").strip()
    uhrzeit = request.args.get("u", "").strip()
    bis     = request.args.get("b", "").strip()
    ort     = request.args.get("o", "").strip()
    beschr  = request.args.get("x", "").strip()[:1000]

    if not datum or not titel:
        return "Pflichtfelder fehlen", 400
    try:
        y, mo, d = [int(x) for x in datum.split("-")]
    except Exception:
        return "Ungültiges Datum", 400

    def _p(n): return str(n).zfill(2)

    try:
        dtstart, dtend = _ics_zeiten(date(y, mo, d), uhrzeit, bis)
    except ValueError:
        return "Ungültige Uhrzeit", 400

    uid  = f"{datum}-{re.sub(r'[^a-z0-9]', '', titel.lower()[:20])}-{int(time.time())}@vereinskalender"
    desc = label + (f"\n{ort}" if ort else "") + (f"\n\n{beschr}" if beschr else "")  # echte Zeilenumbrüche – _ics_escape() macht daraus "\n"

    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Vereinskalender//DE",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        dtstart, dtend,
        f"SUMMARY:{_ics_escape(titel)}",
        f"DESCRIPTION:{_ics_escape(desc)}",
    ]
    if ort:
        lines.append(f"LOCATION:{_ics_escape(ort)}")
    lines += ["TRANSP:TRANSPARENT", "STATUS:CONFIRMED", "END:VEVENT", "END:VCALENDAR"]

    safe = re.sub(r"[^\wäöüÄÖÜß]", "-", titel).strip("-")[:40]
    return Response(
        "\r\n".join(lines),
        mimetype="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{datum}-{safe}.ics"'},
    )


def _track_ical_request(filter_vereine: set | None = None):
    import hashlib
    from zoneinfo import ZoneInfo as _ZI
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()
    try:
        addr = ipaddress.ip_address(ip)
        if addr.version == 6:
            prefix = str(ipaddress.ip_network(f"{ip}/48", strict=False).network_address)
        else:
            prefix = ".".join(ip.split(".")[:3])
    except Exception:
        prefix = ip[:20]
    ip_hash = hashlib.sha256(prefix.encode()).hexdigest()[:20]
    today = datetime.now(_ZI("Europe/Berlin")).date().isoformat()
    try:
        with db_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO ical_feed_requests (date, ip_hash) VALUES (?, ?)",
                (today, ip_hash),
            )
            for vkey in (filter_vereine or set()):
                conn.execute(
                    "INSERT OR IGNORE INTO ical_feed_vereine (date, ip_hash, verein_key) VALUES (?, ?, ?)",
                    (today, ip_hash, vkey),
                )
    except Exception:
        pass


@kalender_bp.route("/api/ical/feed")
def api_ical_feed():
    """Abonnierbarer iCal-Feed aller bevorstehenden Termine (webcal://).

    Favoriten-Abo (ADR-025): `v` = Vereine, `o` = Ortschaften („Ort|Gemeinde"), `g` = Gemeinden
    („Gemeinde|Landkreis X"), `r` = Regionen – ein Termin ist drin, wenn er auf irgendeinen davon
    passt; Orte/Gemeinden/Regionen nach der Mischregel wie im Filter der App. `ort` = altes
    Einzel-Ortschaft-Abo, gleiche Regel. Ohne jeden Parameter: alle Termine.
    """
    def _liste(name: str) -> list[str]:
        # nur vergleichen, nie ausführen – trotzdem Länge und Anzahl begrenzen
        return [x.strip()[:100] for x in request.args.get(name, "").split(",") if x.strip()][:50]

    filter_vereine = {v.lower() for v in _liste("v")}
    _track_ical_request(filter_vereine)
    f_orte = set()
    for x in _liste("o") + ([request.args.get("ort", "").strip()[:100]] if request.args.get("ort", "").strip() else []):
        ort, _, gem = x.partition("|")
        f_orte.add((ort.strip().lower(), _gem_norm(gem).lower()))
    f_gems = set()
    for x in _liste("g"):
        gem, _, lk = x.partition("|")
        if gem.strip():
            f_gems.add((_gem_norm(gem).lower(), (lk.strip() or "Landkreis Landshut").lower()))
    # Region gekürzt vergleichen: alte Favoriten (vor v1.61) tragen noch „Landkreis Landshut“ (v1.63)
    f_regs = {region_of(x).lower() for x in _liste("r") if region_of(x)}
    nach_ort = bool(f_orte or f_gems or f_regs)
    filtert  = bool(filter_vereine) or nach_ort

    try:
        raw = json.loads(VEREINSTERMINE_FILE.read_text())
    except Exception:
        raw = {}

    labels = raw.get("_labels", {})
    labels.setdefault("ff", "FF Hölskofen")
    labels.setdefault("kp", "Königstreue Patrioten Hölskofen")

    heute = datetime.now().date()
    alle  = []

    for key, events in raw.items():
        if key.startswith("_") or not isinstance(events, list):
            continue
        if filter_vereine and not nach_ort and key not in filter_vereine:
            continue
        for t in events:
            if t.get("geloescht") or t.get("deleted"):
                continue
            alle.append({**t, "_vkey": key})

    for vkey, t in gottesdienste_eintraege(raw):
        if filter_vereine and not nach_ort and vkey not in filter_vereine:
            continue
        alle.append({**t, "_vkey": vkey})

    meta = _merged_meta(raw) if nach_ort else {}

    def _passt(t):
        if not filtert:
            return True
        vkey = t.get("_vkey", "")
        if vkey in filter_vereine:
            return True
        if not nach_ort:
            return False
        name = labels.get(vkey) or _PG_LABELS.get(vkey, "")
        try:
            g = geo_fuer_termin(t, meta.get(vkey), name, raw.get("_orte_zuordnung"))
        except Exception:
            g = None
        orte = termin_orte_misch(g, meta.get(vkey), name, _get_rubrik(vkey, name, meta.get(vkey, {})) == "Pfarrei")
        return abo_treffer(orte, f_orte, f_gems, f_regs)

    heute_s = heute.strftime("%Y-%m-%d")
    kuenftige = sorted(
        [t for t in alle if t.get("datum", "") >= heute_s and _passt(t)],
        key=lambda t: (t["datum"], t.get("uhrzeit", ""))
    )

    def _p(n): return str(n).zfill(2)

    now_stamp    = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    vevent_lines = []
    uids_vergeben: set = set()

    for t in kuenftige:
        try:
            y, mo, d = [int(x) for x in t["datum"].split("-")]
        except Exception:
            continue

        bezeichnung = t.get("bezeichnung") or t.get("art") or "Termin"
        ort         = t.get("ort", "")
        vkey        = t.get("_vkey", "")
        vereinname  = labels.get(vkey, vkey.upper())
        uhrzeit     = t.get("uhrzeit", "")

        try:
            dtstart, dtend = _ics_zeiten(date(y, mo, d), uhrzeit, t.get("uhrzeit_bis", ""))
        except ValueError:
            try:  # kaputtes Ende → nur Beginn, erst danach 00:00
                dtstart, dtend = _ics_zeiten(date(y, mo, d), uhrzeit, "")
            except ValueError:
                dtstart, dtend = _ics_zeiten(date(y, mo, d), "00:00", "")

        uid_raw = f"{t['datum']}-{re.sub(r'[^a-z0-9]', '', bezeichnung.lower()[:20])}-{vkey}@vereinskalender"
        if uid_raw in uids_vergeben:
            # Kollision (z. B. zwei „Hl. Messe“ am selben Tag): Der erste Termin behält seine
            # bisherige UID (ADR-019: keine Doppelten in Abo-Kalendern), weitere bekommen eine
            # stabile Endung aus Uhrzeit + Ort – vorher zeigten Kalender-Apps nur einen davon.
            import hashlib
            zusatz = hashlib.sha1(f"{uhrzeit}|{ort}|{bezeichnung}".encode()).hexdigest()[:8]
            basis = uid_raw.replace("@vereinskalender", f"-{zusatz}")
            uid_raw, n = f"{basis}@vereinskalender", 2
            while uid_raw in uids_vergeben:
                uid_raw, n = f"{basis}-{n}@vereinskalender", n + 1
        uids_vergeben.add(uid_raw)
        beschr  = t.get("beschreibung", "")
        desc    = vereinname + (f"\n{ort}" if ort else "") + (f"\n\n{beschr}" if beschr else "")  # echte Zeilenumbrüche – _ics_escape() macht daraus "\n"

        vevent_lines += [
            "BEGIN:VEVENT",
            f"UID:{uid_raw}",
            f"DTSTAMP:{now_stamp}",
            dtstart, dtend,
            f"SUMMARY:{_ics_escape(bezeichnung)}",
            f"DESCRIPTION:{_ics_escape(desc)}",
        ]
        if ort:
            vevent_lines.append(f"LOCATION:{_ics_escape(ort)}")
        vevent_lines += ["TRANSP:TRANSPARENT", "STATUS:CONFIRMED", "END:VEVENT"]

    cal_name = "Vereinskalender"
    if filter_vereine:
        namen    = [labels.get(k, k.upper()) for k in sorted(filter_vereine)]
        cal_name = ", ".join(namen) if len(namen) <= 3 else f"Vereinskalender ({len(namen)} Vereine)"

    header = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Vereinskalender//DE",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_ics_escape(cal_name)}",
        "X-WR-TIMEZONE:Europe/Berlin",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    footer   = ["END:VCALENDAR"]
    ics_body = "\r\n".join(header + vevent_lines + footer)
    return Response(
        ics_body,
        mimetype="text/calendar; charset=utf-8",
        headers={"Content-Disposition": "inline; filename=\"vereinskalender.ics\""},
    )


# ── Admin: heimat-info Import-Management ─────────────────────────────────────

HEIMAT_PENDING_DIR = Path("/opt/rename-webhook/imports")


def _load_pending_meta(f: Path) -> dict | None:
    """Zusammenfassung einer Pending-Datei für die Admin-Ansicht: pro Verein nur die neuen
    Termine; Vereine ohne neue Termine zählen nur in `duplikate` mit."""
    try:
        data   = json.loads(f.read_text())
        events = data.get("events", [])
        existing_meta = {}
        if VEREINSTERMINE_FILE.exists():
            try:
                existing_meta = json.loads(VEREINSTERMINE_FILE.read_text()).get("_meta", {})
            except Exception:
                pass
        vereine: dict = {}
        for e in events:
            k = e["_verein_key"]
            if k not in vereine:
                gespeichert = existing_meta.get(k, {})
                vereine[k] = {
                    "key":      k,
                    "label":    e.get("_label", k),
                    "gemeinde": e.get("_gemeinde", ""),
                    "heimatort_gespeichert": gespeichert.get("heimatort", ""),
                    "gemeinde_vorschlag":    gespeichert.get("gemeinde", "") or e.get("_gemeinde_amtlich", ""),
                    "landkreis_vorschlag":   gespeichert.get("landkreis", "") or e.get("_landkreis", ""),
                    "rubrik":   gespeichert.get("rubrik", "") or e.get("_rubrik", ""),
                    "bekannt":  k in existing_meta,
                    "methode":  e.get("_methode", "heimat"),
                    "termine":  [],
                    "neu":      0,
                    "total":    0,
                }
            vereine[k]["total"] += 1
            if e.get("_neu"):
                vereine[k]["neu"] += 1
                vereine[k]["termine"].append({
                    "datum":       e["datum"],
                    "uhrzeit":     e.get("uhrzeit", ""),
                    "uhrzeit_bis": e.get("uhrzeit_bis", ""),
                    "bezeichnung": e["bezeichnung"],
                    "ort":         e.get("ort", ""),
                    "verdacht":    e.get("_verdacht", ""),   # ADR-027: in der Ansicht nicht vorangehakt
                })
        mit_neuen = [v for v in vereine.values() if v["neu"]]
        return {
            "uid":        data["uid"],
            "quelle":     data.get("quelle", "heimat-info.de"),
            "erzeugt":    data.get("erzeugt", ""),
            "gesamt":     len(events),
            "neu":        sum(1 for e in events if e.get("_neu")),
            "duplikate":  sum(1 for e in events if not e.get("_neu") and not e.get("_sv")),
            "sv":         sum(1 for e in events if e.get("_sv")),
            "verdacht":   sum(1 for e in events if e.get("_neu") and e.get("_verdacht")),
            "ohne_neue":  len(vereine) - len(mit_neuen),
            "vereine":    sorted(mit_neuen, key=lambda x: x["label"].lower()),
        }
    except Exception:
        return None


@kalender_bp.route("/api/admin/importe", methods=["GET"])
def api_admin_importe():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}
    HEIMAT_PENDING_DIR.mkdir(parents=True, exist_ok=True)
    result = []
    for f in sorted(HEIMAT_PENDING_DIR.glob("heimat_pending_*.json"),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        meta = _load_pending_meta(f)
        if meta:
            result.append(meta)
    return json.dumps(result, ensure_ascii=False), 200, {
        "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}


@kalender_bp.route("/api/admin/importe/<uid>", methods=["GET"])
def api_admin_importe_detail(uid):
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}
    pf = HEIMAT_PENDING_DIR / f"heimat_pending_{uid}.json"
    if not pf.exists():
        return json.dumps({"error": "Import nicht gefunden"}), 404, {"Content-Type": "application/json"}
    try:
        data   = json.loads(pf.read_text())
        events = data.get("events", [])
        existing_meta = {}
        if VEREINSTERMINE_FILE.exists():
            try:
                existing_meta = json.loads(VEREINSTERMINE_FILE.read_text()).get("_meta", {})
            except Exception:
                pass
        vereine: dict = {}
        for e in events:
            k = e["_verein_key"]
            if k not in vereine:
                vereine[k] = {
                    "key":      k,
                    "label":    e.get("_label", k),
                    "gemeinde": e.get("_gemeinde", ""),
                    "heimatort_gespeichert": existing_meta.get(k, {}).get("heimatort", ""),
                    "termine":  [],
                    "neu":      0,
                }
            if e.get("_neu"):
                vereine[k]["termine"].append({
                    "datum":       e["datum"],
                    "uhrzeit":     e.get("uhrzeit", ""),
                    "bezeichnung": e["bezeichnung"],
                    "ort":         e.get("ort", ""),
                })
                vereine[k]["neu"] += 1
        return json.dumps({
            "uid":     data["uid"],
            "quelle":  data.get("quelle", ""),
            "erzeugt": data.get("erzeugt", ""),
            "vereine": list(vereine.values()),
        }, ensure_ascii=False), 200, {
            "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}
    except Exception as e:
        return json.dumps({"error": str(e)}), 500, {"Content-Type": "application/json"}


@kalender_bp.route("/api/admin/importe/<uid>/confirm", methods=["POST"])
def api_admin_importe_confirm(uid):
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}
    body             = request.get_json(silent=True) or {}
    alle             = body.get("alle", False)
    verein_keys      = body.get("vereine") if not alle else None
    geo              = body.get("geo", {})  # {key: {heimatort, gemeinde, landkreis}}
    excluded_events  = body.get("excluded_events") or None  # [{verein_key, datum, uhrzeit, bezeichnung}]

    try:
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location("heimat_import", "/opt/rename-webhook/heimat_import.py")
        mod  = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        result = mod.do_import(uid, verein_keys, excluded_events, geo)
        log(f"✅  Admin-Import uid={uid}: {result}")
        return json.dumps({"ok": True, "message": result}, ensure_ascii=False), 200, {
            "Content-Type": "application/json; charset=utf-8"}
    except FileNotFoundError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False), 404, {
            "Content-Type": "application/json; charset=utf-8"}
    except Exception as e:
        log(f"❌  Admin-Import uid={uid}: {e}")
        return json.dumps({"error": str(e)}), 500, {"Content-Type": "application/json"}


@kalender_bp.route("/api/admin/importe/<uid>/reject", methods=["POST"])
def api_admin_importe_reject(uid):
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}
    body        = request.get_json(silent=True) or {}
    alle        = body.get("alle", False)
    verein_keys = body.get("vereine") if not alle else None
    try:
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location("heimat_import", "/opt/rename-webhook/heimat_import.py")
        mod  = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        result = mod.do_reject(uid, verein_keys)
        return json.dumps({"ok": True, "message": result}, ensure_ascii=False), 200, {
            "Content-Type": "application/json; charset=utf-8"}
    except Exception as e:
        return json.dumps({"error": str(e)}), 500, {"Content-Type": "application/json"}


@kalender_bp.route("/api/admin/importe/trigger", methods=["POST"])
def api_admin_importe_trigger():
    import ipaddress as _ip
    import socket as _socket
    import urllib.parse as _up

    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}

    body = request.get_json(silent=True) or {}
    url  = (body.get("url") or "").strip()
    alle = body.get("alle", False)

    if not url and not alle:
        return json.dumps({"error": "url oder alle: true erforderlich"}), 400, {
            "Content-Type": "application/json"}

    # SSRF-Schutz bei URL-Eingabe
    if url:
        parsed = _up.urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            return json.dumps({"error": "Nur HTTPS-URLs erlaubt"}), 400, {
                "Content-Type": "application/json"}
        try:
            ip = _socket.gethostbyname(parsed.hostname or "")
            addr = _ip.ip_address(ip)
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast or addr.is_unspecified:
                return json.dumps({"error": "Private/lokale Adressen nicht erlaubt"}), 400, {
                    "Content-Type": "application/json"}
        except Exception:
            return json.dumps({"error": "DNS-Auflösung fehlgeschlagen"}), 400, {
                "Content-Type": "application/json"}

    if not _import_lock.acquire(blocking=False):
        return json.dumps({"error": "Import läuft bereits – bitte warten"}), 409, {
            "Content-Type": "application/json"}

    lauf_id = _uuid.uuid4().hex[:8]
    _schreibe_import_status({"id": lauf_id, "status": "laeuft", "url": url or "",
                             "start": datetime.now().isoformat(timespec="seconds")})

    def _run():
        status = {"id": lauf_id, "url": url or "", "status": "fehler"}
        try:
            import importlib.util as _ilu
            spec = _ilu.spec_from_file_location(
                "heimat_import", "/opt/rename-webhook/heimat_import.py")
            mod = _ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if url:
                result = mod.fetch_and_save_pending_for_url(url)
            else:
                result = mod.fetch_and_save_pending()
            log(f"✅  Admin-Trigger: {result}")
            status.update(status="fehler" if "error" in result else "ok",
                          fehler=result.get("error", ""), neu=result.get("neu", 0),
                          gesamt=result.get("gesamt", 0),
                          hinweise=result.get("hinweise", []) + [
                              f"Fehler bei: {f}" for f in result.get("fehler", [])],
                          ki=[{"name": k["name"], "kosten_usd": k["kosten_usd"]}
                              for k in result.get("ki", [])])
        except Exception as e:
            log(f"❌  Admin-Trigger fehlgeschlagen: {e}")
            status["fehler"] = str(e)
        finally:
            status["ende"] = datetime.now().isoformat(timespec="seconds")
            _schreibe_import_status(status)
            _import_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return json.dumps({"ok": True, "id": lauf_id,
                       "message": "Import gestartet – das Ergebnis erscheint hier automatisch."}),\
           202, {"Content-Type": "application/json; charset=utf-8"}


_IMPORT_STATUS_FILE = HEIMAT_PENDING_DIR / "letzter_lauf.json"


def _schreibe_import_status(status: dict) -> None:
    try:
        HEIMAT_PENDING_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _IMPORT_STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, ensure_ascii=False))
        tmp.replace(_IMPORT_STATUS_FILE)
    except OSError as e:
        log(f"⚠️  Import-Status nicht geschrieben: {e}")


# ── Admin-Tab „Orte": Veranstaltungsorte einer Ortschaft zuordnen (v1.42) ─────────────
# Zuordnungen stehen in vereinstermine.json unter `_orte_zuordnung` (Liste von
# {ort, gemeinde, verein, ortschaft, am}); `verein` leer = gilt für alle Vereine der Gemeinde.

def _admin_ok() -> bool:
    token = request.headers.get("X-Upload-Token", "")
    return bool(UPLOAD_TOKEN) and hmac.compare_digest(token, UPLOAD_TOKEN)


def _json_antwort(obj, code=200):
    return json.dumps(obj, ensure_ascii=False), code, {
        "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}


@kalender_bp.route("/api/admin/orte", methods=["GET"])
def api_admin_orte():
    if not _admin_ok():
        return _json_antwort({"error": "Nicht autorisiert"}, 401)
    raw    = json.loads(VEREINSTERMINE_FILE.read_text()) if VEREINSTERMINE_FILE.exists() else {}
    labels = raw.get("_labels", {})
    zu     = raw.get("_orte_zuordnung", [])
    liste, ohne_ortsangabe = offene_orte(raw)   # gemeinsam mit dem 20-Uhr-Bericht (shared/admin_aufgaben.py)
    register, _ = _geo_register()
    try:
        fest = json.loads(ORTE_FREI_FILE.read_text())
    except Exception:
        fest = []
    return _json_antwort({
        "offen": liste,
        "ohne_ortsangabe": ohne_ortsangabe,
        "zuordnungen": [{**z, "verein_label": labels.get(z.get("verein", ""), "")} for z in zu if isinstance(z, dict)],
        "fest": [{"name": f.get("name", ""), "alias": f.get("alias", []), "ortschaft": f.get("ortschaft", "")} for f in fest],
        "ortschaften": sorted(({"ort": e["ort"], "gemeinde": e.get("gemeinde", ""), "landkreis": e.get("landkreis", "")}
                               for e in register), key=lambda e: (e["gemeinde"], e["ort"])),
    })


@kalender_bp.route("/api/admin/orte/zuordnung", methods=["POST", "DELETE"])
def api_admin_orte_zuordnung():
    if not _admin_ok():
        return _json_antwort({"error": "Nicht autorisiert"}, 401)
    body     = request.get_json(silent=True) or {}
    ort      = re.sub(r"\s+", " ", str(body.get("ort") or "")).strip()
    gemeinde = _gem_norm(str(body.get("gemeinde") or ""))
    verein   = str(body.get("verein") or "").strip()
    if not ort or len(ort) > 200:
        return _json_antwort({"error": "Ort fehlt oder ist zu lang"}, 400)
    if not verein and not gemeinde:
        return _json_antwort({"error": "Gemeinde oder Verein nötig"}, 400)
    gleich = lambda z: (_ort_norm(z.get("ort")) == _ort_norm(ort) and (z.get("verein") or "") == verein
                        and (verein or _gem_norm(z.get("gemeinde", "")).casefold() == gemeinde.casefold()))

    from shared.kalender_store import KalenderStore
    if request.method == "DELETE":
        weg = {"n": 0}

        def _loeschen(d):
            alt = d.get("_orte_zuordnung", [])
            d["_orte_zuordnung"] = [z for z in alt if not gleich(z)]
            weg["n"] = len(alt) - len(d["_orte_zuordnung"])
        KalenderStore.update(_loeschen)
        log(f"🗺  Ort-Zuordnung gelöscht: {ort!r} ({verein or gemeinde}) – {weg['n']}")
        return _json_antwort({"ok": True, "geloescht": weg["n"]})

    ausflug = bool(body.get("ausflug"))
    ziel = None if ausflug else eintrag_fuer(str(body.get("ortschaft") or ""))
    if not ausflug and not ziel:
        return _json_antwort({"error": "Unbekannte Ortschaft"}, 400)

    def _setzen(d):
        if verein and verein not in d.get("_labels", {}):
            raise ValueError("Unbekannter Verein")
        liste = [z for z in d.get("_orte_zuordnung", []) if not gleich(z)]
        eintrag = {"ort": ort, "gemeinde": gemeinde, "verein": verein,
                   "ortschaft": "" if ausflug else ziel["ort"], "am": datetime.now().isoformat(timespec="seconds")}
        if ausflug:
            eintrag["ausflug"] = True
        liste.append(eintrag)
        d["_orte_zuordnung"] = liste
    try:
        KalenderStore.update(_setzen)
    except ValueError as e:
        return _json_antwort({"error": str(e)}, 400)
    ziel_text = "Ausflugsziel" if ausflug else ziel["ort"]
    log(f"🗺  Ort-Zuordnung: {ort!r} → {ziel_text} ({verein or gemeinde})")
    return _json_antwort({"ok": True, "ortschaft": "" if ausflug else ziel["ort"], "ausflug": ausflug})


@kalender_bp.route("/api/admin/register", methods=["GET", "POST", "DELETE"])
def api_admin_register():
    """Register prüfen (Todo #417): unbestätigte Einträge aus orte.json listen und Josefs Urteil
    speichern – in vereinstermine.json `_orte_geprueft`, nie in orte.json (liegt im Git)."""
    if not _admin_ok():
        return _json_antwort({"error": "Nicht autorisiert"}, 401)
    from shared.kalender_store import KalenderStore
    if request.method == "GET":
        raw = json.loads(VEREINSTERMINE_FILE.read_text()) if VEREINSTERMINE_FILE.exists() else {}
        return _json_antwort(register_pruefung(raw))
    body     = request.get_json(silent=True) or {}
    ort      = str(body.get("ort") or "").strip()
    gemeinde = _gem_norm(str(body.get("gemeinde") or ""))
    if not ort or len(ort) > 100 or len(gemeinde) > 100:
        return _json_antwort({"error": "Ort fehlt oder ist zu lang"}, 400)
    register, _ = _geo_register()
    if not any(e.get("ort") == ort and _gem_norm(e.get("gemeinde", "")).casefold() == gemeinde.casefold()
               for e in register or []):
        return _json_antwort({"error": "Kein solcher Register-Eintrag"}, 400)
    gleich = lambda u: (str(u.get("ort", "")).casefold() == ort.casefold()
                        and _gem_norm(u.get("gemeinde", "")).casefold() == gemeinde.casefold())
    if request.method == "DELETE":
        KalenderStore.update(lambda d: d.__setitem__(
            "_orte_geprueft", [u for u in d.get("_orte_geprueft", []) if not gleich(u)]))
        log(f"🗺  Register-Urteil zurückgenommen: {ort} ({gemeinde})")
        return _json_antwort({"ok": True})
    ok    = bool(body.get("ok"))
    notiz = re.sub(r"\s+", " ", str(body.get("notiz") or "")).strip()[:300]
    if not ok and not notiz:
        return _json_antwort({"error": "Bitte kurz notieren, was nicht stimmt"}, 400)

    def _setzen(d):
        liste = [u for u in d.get("_orte_geprueft", []) if not gleich(u)]
        liste.append({"ort": ort, "gemeinde": gemeinde, "ok": ok, "notiz": "" if ok else notiz,
                      "am": datetime.now().isoformat(timespec="seconds")})
        d["_orte_geprueft"] = liste
    KalenderStore.update(_setzen)
    log(f"🗺  Register {'bestätigt' if ok else 'FALSCH'}: {ort} ({gemeinde}){'' if ok else ' – ' + notiz}")
    return _json_antwort({"ok": True})


@kalender_bp.route("/api/admin/importe/status", methods=["GET"])
def api_admin_importe_status():
    token = request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or not hmac.compare_digest(token, UPLOAD_TOKEN):
        return json.dumps({"error": "Nicht autorisiert"}), 401, {"Content-Type": "application/json"}
    try:
        status = json.loads(_IMPORT_STATUS_FILE.read_text())
    except (OSError, ValueError):
        status = {}
    return json.dumps(status, ensure_ascii=False), 200, {
        "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}
