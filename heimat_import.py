#!/opt/rename-webhook/bin/python3
"""
heimat_import.py
Fetcht Termine für alle konfigurierten Gemeinden – heimat-info.de oder eigene
Gemeinde-Webseite (termin_scraper.py) – und sendet Telegram-Vorschau mit ✅/❌ Import-Buttons.

Aufruf:
  python3 heimat_import.py              → Import-Vorschau per Telegram
  python3 heimat_import.py --add <url>  → Neue Gemeinde via Playwright entdecken
"""
import html as htmlmod
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/opt/rename-webhook")
from shared.secrets import load_secrets
from shared.telegram import send_telegram, send_telegram_inline
from shared.kalender_store import KalenderStore
from shared.termin_felder import UHRZEIT_RE
from shared.geo import ortschaft_aus_name
import termin_scraper

GEMEINDEN_FILE      = Path("/opt/rename-webhook/heimat_gemeinden.json")
VEREINSTERMINE_FILE = Path("/opt/rename-webhook/vereinstermine.json")
PENDING_DIR         = Path("/opt/rename-webhook/imports")
LAST_IMPORT_FILE    = Path("/opt/rename-webhook/last_import.json")
LOG_FILE            = "/var/log/pka-heimat.log"
API_EXPORT          = "https://heimatinfo-api-platform.azurewebsites.net"
API_EXPORT_HEADERS  = {
    "Origin":     "https://www.heimat-info.de",
    "Referer":    "https://www.heimat-info.de/",
    "User-Agent": "Mozilla/5.0",
}

MAX_KI_PRO_LAUF     = 5   # Kostenbremse: höchstens so viele KI-Abrufe pro Importlauf

# Veranstalter, die eine Gemeindeverwaltung sind → Rubrik „Gemeinde"
_GEMEINDE_VERANSTALTER = re.compile(
    r"^(gemeinde|markt|stadt|vg|verwaltungsgemeinschaft|rathaus|landratsamt)\b", re.I)
# Titel von Sitzungen ohne Veranstalterangabe → der Gemeinde zuordnen
_SITZUNG_TITEL = re.compile(
    r"(gemeinderat|marktgemeinderat|marktrat|stadtrat|ausschuss|bürgerversammlung)", re.I)

_org_cache: dict[str, str] = {}

# (pattern, name, erklaerung)
_INJECTION_PATTERNS: list[tuple[str, str, str]] = [
    (
        r'\[\s*(?:an\s+claude|system|instruction|assistant|human|ignore)\s*:',
        "KI-Direktinstruktion",
        "Claude würde die eingebetteten Anweisungen als direkte Befehle interpretieren "
        "und ausführen – z.B. Dateien löschen, SSH-Befehle absetzen oder Daten manipulieren.",
    ),
    (
        r'(?:ignore|forget|disregard)\s+(?:all\s+)?(?:previous|prior|above)\s+instructions',
        "Instruktions-Override",
        "Claude würde alle vorherigen Sicherheitsregeln und Systemanweisungen ignorieren "
        "und könnte beliebige Befehle des Angreifers ausführen.",
    ),
    (
        r'(?:du\s+bist|you\s+are)\s+(?:jetzt|now)\s+ein',
        "Identitätsübernahme",
        "Der Angreifer versucht Claudes Rolle neu zu definieren um Sicherheitsbeschränkungen "
        "zu umgehen und Claude als anderen Assistenten ohne Regeln agieren zu lassen.",
    ),
    (
        r'f[uü]hre\s+(?:jetzt\s+)?(?:sofort\s+)?aus\s*:',
        "Befehlsaufruf (deutsch)",
        "Claude würde den direkt angegebenen Befehl ausführen. Je nach Inhalt könnten "
        "Dateien gelöscht, verändert oder Daten exfiltriert werden.",
    ),
    (
        r'execute\s+(?:now|immediately|this)\s*:',
        "Befehlsaufruf (englisch)",
        "Claude würde den direkt angegebenen Befehl ausführen. Je nach Inhalt könnten "
        "Dateien gelöscht, verändert oder Daten exfiltriert werden.",
    ),
    (
        r'(?:^|\n)\s*(?:SYSTEM|INSTRUCTION|PROMPT|OVERRIDE)\s*:',
        "System-Override",
        "Versucht Claudes Systemkonfiguration zu überschreiben und könnte alle "
        "Sicherheitsmechanismen deaktivieren.",
    ),
    (
        r'\$\([^)]+\)',
        "Shell-Kommandosubstitution",
        "Der Ausdruck $(...) würde bei Ausführung in einer Shell beliebige Systembefehle "
        "starten – ohne Einschränkung auf bestimmte Aktionen.",
    ),
]

# Sammelt Findings während eines Fetch-Laufs; wird nach cmd_import() geleert
_injection_findings: list[dict] = []


def _sanitize_text(value: str, field: str = "?", gemeinde: str = "?") -> str:
    """Bereinigt externe Texte von Prompt-Injection-Versuchen.
    Verdächtige Inhalte werden ersetzt, geloggt und in _injection_findings gesammelt."""
    if not value:
        return value
    original = value[:300]
    value    = original
    for pattern, name, erklaerung in _INJECTION_PATTERNS:
        if re.search(pattern, value, re.IGNORECASE):
            _log(f"  ⚠️ Prompt-Injection ({field}, {gemeinde}): {original[:80]!r}")
            _injection_findings.append({
                "gemeinde":   gemeinde,
                "feld":       field,
                "rohtext":    original,
                "muster":     name,
                "erklaerung": erklaerung,
            })
            value = re.sub(pattern, "[GEFILTERT]", value, flags=re.IGNORECASE)
    return value


def _notify_injection(token: str, chat_id: str, findings: list[dict]) -> None:
    """Sendet eine Telegram-Warnung für alle gefundenen Injection-Versuche."""
    for f in findings:
        msg = (
            f"🚨 PROMPT INJECTION ERKANNT\n\n"
            f"Quelle: heimat-info.de\n"
            f"Gemeinde: {f['gemeinde']}\n"
            f"Feld: {f['feld']}\n\n"
            f"Roher Inhalt:\n\"{f['rohtext'][:200]}\"\n\n"
            f"Erkanntes Muster: {f['muster']}\n\n"
            f"Was hätte passieren können:\n{f['erklaerung']}\n\n"
            f"→ Inhalt wurde gefiltert und neutralisiert.\n"
            f"→ Event ist importierbar, enthält aber [GEFILTERT]-Markierung."
        )
        send_telegram(token, chat_id, msg)
        _log(f"🚨 Injection-Alert gesendet: {f['muster']} in {f['gemeinde']}/{f['feld']}")


def _slugify(name: str) -> str:
    name = name.lower()
    for a, b in [("ä","ae"),("ö","oe"),("ü","ue"),("ß","ss")]:
        name = name.replace(a, b)
    name = re.sub(r"[^a-z0-9]+", "_", name)
    return name.strip("_")[:50]


def _log(msg: str) -> None:
    ts   = datetime.now().isoformat(timespec="seconds")
    line = f"{ts} {msg}"
    print(line)
    try:   # Log-Datei fehlt/gehört root (Aufruf aus dem Dienst) → nur stdout, nie abbrechen
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _fetch_org_name(org_id: str) -> str:
    """Holt Vereinsname via Organization-API (gecacht)."""
    if org_id in _org_cache:
        return _org_cache[org_id]
    try:
        req = urllib.request.Request(
            f"{API_EXPORT}/organizations/{org_id}", headers=API_EXPORT_HEADERS)
        with urllib.request.urlopen(req, timeout=8) as r:
            name = json.loads(r.read()).get("name", "").strip()
    except Exception:
        name = ""
    _org_cache[org_id] = name
    return name


def _fetch_all_events(c_id: str) -> list[dict]:
    """Holt alle Events via Export-API (pageSize=50 max, paginiert via pageIndex)."""
    all_events, page = [], 0
    while True:
        url = f"{API_EXPORT}/export/events?pageIndex={page}&pageSize=50&c={c_id}"
        try:
            req = urllib.request.Request(url, headers=API_EXPORT_HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r:
                batch = json.loads(r.read())
        except Exception as e:
            _log(f"  ❌ Export-API Fehler page={page} c={c_id[:8]}: {e}")
            break
        if not batch:
            break
        all_events.extend(batch)
        if len(batch) < 50:
            break
        page += 1
    return all_events


def _parse_api_events(api_events: list[dict], heute: str, gemeinde_name: str = "?") -> list[dict]:
    """Konvertiert Export-API JSON-Events in internes Format."""
    try:
        from zoneinfo import ZoneInfo
        berlin = ZoneInfo("Europe/Berlin")
    except ImportError:
        berlin = None

    events = []
    for e in api_events:
        if e.get("status") != "Published":
            continue
        start = e.get("startDate") or ""
        if not start:
            continue
        try:
            dt_utc = datetime.strptime(start[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            if berlin:
                dt_loc = dt_utc.astimezone(berlin)
            else:
                from datetime import timedelta
                dt_loc = dt_utc + timedelta(hours=2)
            datum   = dt_loc.strftime("%Y-%m-%d")
            uhrzeit = "" if start.endswith("T00:00:00Z") else dt_loc.strftime("%H:%M")
        except Exception:
            continue
        if datum < heute:
            continue

        bezeichnung = _sanitize_text((e.get("title") or "").strip(), "titel", gemeinde_name)
        ort         = _sanitize_text((e.get("location") or "").strip(), "ort", gemeinde_name)
        org_id      = e.get("organizationId") or ""
        verein_name = _sanitize_text(_fetch_org_name(org_id) if org_id else "", "verein", gemeinde_name)

        if bezeichnung:
            events.append({
                "datum":        datum,
                "uhrzeit":      uhrzeit,
                "bezeichnung":  bezeichnung,
                "ort":          ort,
                "_verein_name": verein_name,
            })
    return events


def discover_c_id(url: str) -> str | None:
    """Nutzt Playwright einmalig um die c= Gemeinde-ID zu ermitteln.
    Methode 1: Base64-kodierter iframe in .borlabs-hide (neueres Borlabs)
    Methode 2: Netzwerk-Intercept nach Button-Click (älteres Borlabs)
    """
    import base64
    from playwright.sync_api import sync_playwright

    found = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page    = browser.new_page(viewport={"width": 1280, "height": 900})

        def on_request(req):
            if "heimat-info.de/embeddings" in req.url:
                m = re.search(r'c=([a-f0-9\-]{36})', req.url)
                if m:
                    found.append(m.group(1))
        page.on("request", on_request)

        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(3000)

            # Methode 1: Base64-Block aus .borlabs-hide dekodieren
            b64s = page.evaluate(
                '() => [...document.querySelectorAll(".borlabs-hide")].map(d => d.innerText.trim())')
            for b64 in b64s:
                if not b64:
                    continue
                try:
                    decoded = base64.b64decode(b64 + "==").decode("utf-8", errors="ignore")
                    m = re.search(r'c=([a-f0-9\-]{36})', decoded)
                    if m:
                        found.append(m.group(1))
                except Exception:
                    pass

            if not found:
                # Methode 2: Consent-Button klicken → Netzwerk-Intercept
                page.evaluate("""() => {
                    const sels = ['.brlbs-cmpnt-cb-btn','._brlbs-btn-accept-all',
                                  '[class*=\"accept\"]','[class*=\"consent\"]'];
                    for (const s of sels) {
                        const b = document.querySelector(s);
                        if (b) { b.click(); break; }
                    }
                }""")
                page.wait_for_timeout(10000)

        except Exception as e:
            _log(f"  Playwright-Fehler: {e}")
        finally:
            browser.close()
    return found[0] if found else None


def _existing_from_data(data: dict) -> set[tuple[str, str, str]]:
    """Alle (datum, uhrzeit, bezeichnung)-Tripel eines Kalenderstands – auch gelöschte,
    damit verworfene Termine nicht wiederkommen."""
    existing = set()
    for key, items in data.items():
        if key.startswith("_") or not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and "datum" in item:
                existing.add((
                    item["datum"],
                    item.get("uhrzeit", ""),
                    item.get("bezeichnung", "").strip().lower(),
                ))
    return existing


def _existing_events() -> set[tuple[str, str, str]]:
    """Gibt alle (datum, uhrzeit, bezeichnung)-Tripel aus vereinstermine.json zurück."""
    if not VEREINSTERMINE_FILE.exists():
        return set()
    return _existing_from_data(json.loads(VEREINSTERMINE_FILE.read_text()))


def _is_duplicate(datum: str, uhrzeit: str, bezeichnung: str,
                  existing: set[tuple[str, str, str]]) -> bool:
    """Exakter Match + Substring-Check auf gleichem Datum UND gleicher Uhrzeit.
    Zwei Events gleichen Namens zu verschiedenen Zeiten sind keine Duplikate."""
    bez = bezeichnung.strip().lower()
    if (datum, uhrzeit, bez) in existing:
        return True
    if len(bez) < 6:
        return False
    for ex_datum, ex_uhr, ex_bez in existing:
        if ex_datum != datum or ex_uhr != uhrzeit:
            continue
        if bez in ex_bez or ex_bez in bez:
            return True
    return False


def do_import(uid: str, verein_keys: list | None = None,
              excluded_events: list | None = None, geo: dict | None = None) -> str:
    """Schreibt bestätigte Events in vereinstermine.json.
    verein_keys=None: alle Events importieren.
    verein_keys=[...]: nur diese Vereine importieren; Pending-Datei bleibt mit Rest.
    excluded_events: Liste von {verein_key, datum, uhrzeit, bezeichnung} – diese Termine überspringen.
    geo: {verein_key: {heimatort, gemeinde, landkreis}} – Admin-Angaben aus der Import-Ansicht,
         gelten nur für die übernommenen Vereine; leere Felder ändern nichts.
    Duplikatprüfung und Schreiben laufen gemeinsam im Lock von KalenderStore.update() auf dem
    dann aktuellen Dateistand – parallele Änderungen gehen nicht verloren."""
    pending_file = PENDING_DIR / f"heimat_pending_{uid}.json"
    if not pending_file.exists():
        # Fallback für ältere Pending-Dateien in /tmp
        old = Path(f"/tmp/heimat_pending_{uid}.json")
        if old.exists():
            pending_file = old
        else:
            raise FileNotFoundError("Import nicht mehr vorhanden (schon bestätigt oder verworfen?)")

    pending  = json.loads(pending_file.read_text())
    events   = pending["events"]
    filter_keys = set(verein_keys) if verein_keys is not None else None
    excluded_set: set[tuple] = set()
    if excluded_events:
        for ex in excluded_events:
            excluded_set.add((
                ex.get("verein_key", ""),
                ex.get("datum", ""),
                ex.get("uhrzeit", ""),
                ex.get("bezeichnung", "").strip().lower(),
            ))
    kandidaten = [
        e for e in events
        if (filter_keys is None or e["_verein_key"] in filter_keys)
        and (e["_verein_key"], e["datum"], e.get("uhrzeit", ""),
             e["bezeichnung"].strip().lower()) not in excluded_set
    ]
    stand = {"neu": 0, "duplikat": 0, "vereine": set()}

    def _upd(data: dict) -> None:
        stand.update(neu=0, duplikat=0, vereine=set())
        labels_ = data.setdefault("_labels", {})
        meta_   = data.setdefault("_meta", {})
        gemeinde_map: dict = data.setdefault("_ortschaften", {}).setdefault("gemeinde_map", {})
        existing = _existing_from_data(data)
        for e in kandidaten:
            if not e.get("_neu", True) or _is_duplicate(
                    e["datum"], e["uhrzeit"], e["bezeichnung"], existing):
                stand["duplikat"] += 1
                continue
            key = e["_verein_key"]
            data.setdefault(key, [])
            labels_.setdefault(key, e["_label"])
            verein_gemeinde = gemeinde_map.get(e["_gemeinde"], "") or e.get("_gemeinde_amtlich", "")
            # Geo-Felder nur setzen wenn noch kein Eintrag vorhanden (nie überschreiben)
            if key not in meta_:
                meta_[key] = {
                    "heimatort": ortschaft_aus_name(e["_label"], e["_gemeinde"]) or e["_gemeinde"],
                    "gemeinde":  verein_gemeinde,
                    "landkreis": e.get("_landkreis") or "Landkreis Landshut",
                }
            if e.get("_rubrik") and not meta_[key].get("rubrik"):
                meta_[key]["rubrik"] = e["_rubrik"]
            # Ortschaft nur, wenn eine erkannt wurde – nie den Namen der heimat-Seite (= Gemeinde) einsetzen
            # (Todo #417): sonst zeigte die Kartenmarke „Bayerbach“ für Greilsberg. Die Zuordnung zur Ortschaft
            # macht geo_fuer_termin() aus dem Ortstext. gemeinde_map lernt weiter über den Seitennamen.
            ortschaft = e.get("ortschaft", "")
            if verein_gemeinde and e["_gemeinde"] not in gemeinde_map:
                gemeinde_map[e["_gemeinde"]] = verein_gemeinde
            termin = {
                "datum":        e["datum"],
                "uhrzeit":      e["uhrzeit"],
                "bezeichnung":  e["bezeichnung"],
                "veranstalter": e.get("_verein_name", ""),
                "ort":          e["ort"],
                "ortschaft":    ortschaft,
                "quelle":       e.get("quelle", ""),
                "quelle_url":   e.get("quelle_url", ""),
            }
            bis = e.get("uhrzeit_bis", "")
            if e["uhrzeit"] and UHRZEIT_RE.match(bis or "") and bis != e["uhrzeit"]:
                termin["uhrzeit_bis"] = bis
            data[key].append(termin)
            existing.add((e["datum"], e["uhrzeit"], e["bezeichnung"].strip().lower()))
            stand["neu"] += 1
            stand["vereine"].add(key)
        # Admin-Geo-Angaben für die übernommenen Vereine (maßgeblich, aber nie leeren)
        geo_keys = filter_keys if filter_keys is not None else stand["vereine"]
        for vkey, felder in (geo or {}).items():
            if vkey not in geo_keys or not isinstance(felder, dict):
                continue
            for f in ("heimatort", "gemeinde", "landkreis"):
                val = str(felder.get(f) or "").strip()
                if val:
                    meta_.setdefault(vkey, {})[f] = val

    KalenderStore.update(_upd)
    neu, duplikat = stand["neu"], stand["duplikat"]
    _log(f"✅ Import: {neu} neu, {duplikat} Duplikate übersprungen")
    LAST_IMPORT_FILE.write_text(
        json.dumps({
            "datum":   datetime.now().strftime("%Y-%m-%d %H:%M"),
            "termine": neu,
            "vereine": len(stand["vereine"]),
        }, ensure_ascii=False)
    )

    # Manuell ausgeschlossene Einzeltermine als soft-deleted speichern → kommen nicht wieder
    if excluded_set:
        excluded_ev = [
            e for e in events
            if (e["_verein_key"], e["datum"], e.get("uhrzeit", ""),
                e["bezeichnung"].strip().lower()) in excluded_set
            and e.get("_neu")
        ]
        _save_rejected_as_deleted(excluded_ev)

    # Pending-Datei: bei Teilimport Rest behalten, sonst löschen
    try:
        if filter_keys is not None:
            remaining = [e for e in events if e["_verein_key"] not in filter_keys]
            if any(e.get("_neu") for e in remaining):   # nur Duplikate übrig → erledigt
                pending["events"] = remaining
                _schreibe_pending(pending_file, pending)
            else:
                pending_file.unlink(missing_ok=True)
        else:
            pending_file.unlink(missing_ok=True)
    except OSError as ex:
        _log(f"⚠️  Pending-Datei {pending_file.name} nicht aktualisiert: {ex}")
    return f"✅ {neu} neue Termine importiert, {duplikat} Duplikate übersprungen"


def _save_rejected_as_deleted(events: list) -> None:
    """Schreibt verworfene neue Events als soft-deleted in vereinstermine.json,
    damit sie beim nächsten Import als Duplikate erkannt werden und nicht wiederkehren."""
    if not events:
        return
    now = datetime.utcnow().isoformat()[:19]
    def _upd(data):
        for e in events:
            key = e["_verein_key"]
            data.setdefault("_labels", {}).setdefault(key, e.get("_label", key))
            data.setdefault(key, []).append({
                "datum":         e["datum"],
                "uhrzeit":       e.get("uhrzeit", ""),
                "bezeichnung":   e["bezeichnung"],
                "ort":           e.get("ort", ""),
                "geloescht":     True,
                "geloescht_von": "admin_reject",
                "geloescht_am":  now,
            })
        return data
    try:
        KalenderStore.update(_upd)
    except Exception as ex:
        _log(f"⚠️  _save_rejected_as_deleted fehlgeschlagen: {ex}")


def do_reject(uid: str, verein_keys: list | None = None) -> str:
    """Verwirft Events aus der Pending-Datei.
    verein_keys=None: gesamten Import verwerfen.
    verein_keys=[...]: nur diese Vereine entfernen."""
    pending_file = PENDING_DIR / f"heimat_pending_{uid}.json"
    if not pending_file.exists():
        return "⚠️ Pending-Datei nicht gefunden"
    if verein_keys is None:
        try:
            pending_file.unlink()
        except OSError:
            pass
        return "🗑 Import verworfen"
    pending = json.loads(pending_file.read_text())
    filter_keys = set(verein_keys)
    rejected_new = [e for e in pending["events"]
                    if e["_verein_key"] in filter_keys and e.get("_neu")]
    remaining = [e for e in pending["events"] if e["_verein_key"] not in filter_keys]
    _save_rejected_as_deleted(rejected_new)
    try:
        if any(e.get("_neu") for e in remaining):   # nur Duplikate übrig → erledigt
            pending["events"] = remaining
            _schreibe_pending(pending_file, pending)
        else:
            pending_file.unlink()
    except OSError as ex:
        _log(f"⚠️  Pending-Datei {pending_file.name} nicht aktualisiert: {ex}")
    return f"🗑 {len(verein_keys)} Verein(e) verworfen"


def _cfg() -> dict:
    """Zugangsdaten: in Flask aus der Umgebung (EnvironmentFile), im Cron aus secrets.env."""
    if os.environ.get("CLAUDE_API_KEY"):
        return dict(os.environ)
    try:
        return load_secrets()
    except Exception:
        return dict(os.environ)


def _schreibe_pending(pfad: Path, inhalt: dict) -> None:
    """Pending-Datei atomar schreiben, Besitzer = Besitzer von imports/.

    Der Wochenlauf läuft als root, Bestätigen/Verwerfen im Dienst (webhook). Ein direktes
    write_text auf eine root-Datei scheiterte dort still – schon übernommene Vereine
    erschienen dann wieder als „neu“ (Review 2026-10-04, Punkt 13). rename() braucht nur
    Schreibrecht im Verzeichnis, das webhook gehört."""
    tmp = pfad.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(inhalt, ensure_ascii=False))
    try:
        st = pfad.parent.stat()
        os.chown(tmp, st.st_uid, st.st_gid)
    except OSError:
        pass
    tmp.replace(pfad)


def _lade_gemeinden() -> list:
    return json.loads(GEMEINDEN_FILE.read_text()) if GEMEINDEN_FILE.exists() else []


def _speichere_gemeinden(gemeinden: list) -> None:
    tmp = GEMEINDEN_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(gemeinden, ensure_ascii=False, indent=2))
    try:   # Cron läuft als root – Datei muss für den Service-User (webhook) schreibbar bleiben
        st = GEMEINDEN_FILE.stat()
        os.chown(tmp, st.st_uid, st.st_gid)
    except OSError:
        pass
    tmp.replace(GEMEINDEN_FILE)


def _aktualisiere_gemeinde(url: str, felder: dict) -> None:
    gemeinden = _lade_gemeinden()
    for g in gemeinden:
        if g.get("url") == url:
            for k in ("parser", "ical_url", "c_id"):
                g.pop(k, None)
            g.update(felder)
    _speichere_gemeinden(gemeinden)


def _quelle_von(g: dict) -> str:
    if (g.get("typ") or "heimat") == "heimat":
        return "heimat-info.de"
    host = urllib.parse.urlparse(g.get("url", "")).hostname or ""
    return host.removeprefix("www.")


def _ki_lauf(g: dict, heute: str, lauf: dict) -> list[dict]:
    """Eintrag mit typ "ki": erst kostenlose Wege erneut prüfen (ein inzwischen gebauter
    Parser übernimmt automatisch), sonst Claude lesen lassen."""
    html = termin_scraper.lade(g["url"])
    statisch = termin_scraper.erkenne_statisch(g["url"], html, heute)
    if statisch:
        felder = {k: v for k, v in statisch.items() if k != "termine"}
        _aktualisiere_gemeinde(g["url"], felder)
        lauf["hinweise"].append(f"✅ {g['name']}: läuft jetzt ohne KI ({felder['typ']}"
                                + (f"/{felder['parser']}" if felder.get("parser") else "") + ")")
        _log(f"  → {g['name']}: Abrufweg {felder} statt KI")
        return statisch["termine"]
    if lauf["ki_aufrufe"] >= MAX_KI_PRO_LAUF:
        raise termin_scraper.ScraperFehler(f"KI-Limit ({MAX_KI_PRO_LAUF} pro Lauf) erreicht")
    lauf["ki_aufrufe"] += 1
    termine, info = termin_scraper.ki_extrahieren(html, g["url"], heute, _cfg().get("CLAUDE_API_KEY", ""))
    _log(f"  → KI {g['name']}: {len(termine)} Termine, {info['eingabe_tokens']}+{info['ausgabe_tokens']} "
         f"Tokens, ~{info['kosten_usd']:.3f} $" + (" (Text gekürzt)" if info["gekuerzt"] else ""))
    lauf["ki"].append({"name": g["name"], "termine": len(termine), **info})
    return termine


def _zuordnen(e: dict, g: dict, aliase: dict, sv_meta: dict, existing: set) -> None:
    """Verein, Gemeinde, Rubrik und Neu-Status eines Rohtermins bestimmen."""
    veranst = e.get("_verein_name", "")
    if termin_scraper.ist_namensliste(veranst):
        veranst = e["_verein_name"] = ""     # Personen-Aufzählung nicht als Verein veröffentlichen
    if not veranst and g.get("gemeinde") and _SITZUNG_TITEL.search(e["bezeichnung"]):
        veranst = g["gemeinde"]        # Sitzung ohne Veranstalterangabe → Gemeindeverwaltung
        e["_verein_name"] = veranst
    roh_key          = _slugify(veranst) or g["verein_key"]
    e["_verein_key"] = aliase.get(roh_key, roh_key)
    e["_label"]      = veranst or g.get("label", g["name"])
    e["_gemeinde"]   = g["name"]
    e["_landkreis"]  = g.get("landkreis") or "Landkreis Landshut"
    if g.get("gemeinde"):
        e["_gemeinde_amtlich"] = g["gemeinde"]
    if _GEMEINDE_VERANSTALTER.match(veranst.strip()):
        e["_rubrik"] = "Gemeinde"
    e["quelle"]      = _quelle_von(g)
    e["quelle_url"]  = g.get("url", "")
    e["_methode"]    = g.get("typ") or "heimat"
    e["_sv"]         = bool(sv_meta.get(e["_verein_key"], {}).get("selbstverwaltung", False))
    e["_neu"]        = not _is_duplicate(e["datum"], e["uhrzeit"], e["bezeichnung"], existing)


def fetch_and_save_pending(gemeinden_filter: list | None = None,
                           vorab: dict | None = None) -> dict:
    """Fetcht Termine für alle (oder gefilterte) Gemeinden und speichert Pending.
    vorab: {url: [Rohtermine]} – schon geholte Termine (neue URL), werden nicht erneut abgerufen.
    Gibt Summary-Dict zurück: {uid, neu, duplikate, sv, fehler, gesamt, hinweise, ki}
    Bei Fehler: {"error": "..."} ohne uid."""
    PENDING_DIR.mkdir(parents=True, exist_ok=True)

    if not GEMEINDEN_FILE.exists():
        return {"error": "heimat_gemeinden.json nicht gefunden"}

    gemeinden = _lade_gemeinden()
    if gemeinden_filter:
        gemeinden = [g for g in gemeinden if g.get("url") in gemeinden_filter
                     or g.get("name") in gemeinden_filter]
    if not gemeinden:
        return {"error": "Keine Gemeinden konfiguriert"}

    heute    = datetime.now().strftime("%Y-%m-%d")
    existing = _existing_events()

    try:
        _vk_data       = json.loads(VEREINSTERMINE_FILE.read_text()) if VEREINSTERMINE_FILE.exists() else {}
        _sv_meta       = _vk_data.get("_meta", {})
        _heimat_aliase = _vk_data.get("_heimat_aliases", {})
    except Exception:
        _sv_meta       = {}
        _heimat_aliase = {}

    alle_events: list = []
    fehler: list      = []
    lauf = {"hinweise": [], "ki": [], "ki_aufrufe": 0}

    for g in gemeinden:
        typ = g.get("typ") or "heimat"
        try:
            if vorab and g.get("url") in vorab:
                events = vorab[g["url"]]
            elif typ == "heimat":
                _log(f"Fetche {g['name']} (c={g['c_id'][:8]}…)")
                api_events = _fetch_all_events(g["c_id"])
                if not api_events:
                    fehler.append(g["name"])
                    continue
                events = _parse_api_events(api_events, heute, g["name"])
            elif typ == "ki":
                _log(f"Fetche {g['name']} (KI, {g['url']})")
                events = _ki_lauf(g, heute, lauf)
            else:
                _log(f"Fetche {g['name']} ({typ}, {g['url']})")
                events = termin_scraper.hole_termine(g, heute)
        except Exception as ex:
            _log(f"  ❌ {g['name']}: {ex}")
            fehler.append(f"{g['name']} ({ex})")
            continue
        if typ != "heimat":
            for e in events:     # heimat-Texte sind in _parse_api_events schon geprüft
                for feld, name in (("bezeichnung", "titel"), ("ort", "ort"), ("_verein_name", "verein")):
                    e[feld] = _sanitize_text(e.get(feld, ""), name, g["name"])
        for e in events:
            _zuordnen(e, g, _heimat_aliase, _sv_meta, existing)
        alle_events.extend(events)
        neu_count = sum(1 for e in events if e["_neu"])
        _log(f"  → {len(events)} Termine ({neu_count} neu)")

    if not alle_events:
        return {"error": "Keine bevorstehenden Termine gefunden", "fehler": fehler,
                "hinweise": lauf["hinweise"], "ki": lauf["ki"]}

    alle_events.sort(key=lambda x: (x["datum"], x.get("uhrzeit", "")))
    uid = str(uuid.uuid4())[:8]
    quellen = list(dict.fromkeys(e["quelle"] for e in alle_events))
    _schreibe_pending(PENDING_DIR / f"heimat_pending_{uid}.json", {
        "uid":     uid,
        "quelle":  ", ".join(quellen),
        "erzeugt": datetime.now().isoformat(timespec="seconds"),
        "events":  alle_events,
    })

    neu = sum(1 for e in alle_events if e["_neu"])
    dup = sum(1 for e in alle_events if not e["_neu"])
    sv  = sum(1 for e in alle_events if e.get("_sv"))
    _log(f"✅ Pending uid={uid}: {neu} neu, {dup} dup, {sv} sv (davon)")
    return {"uid": uid, "neu": neu, "duplikate": dup, "sv": sv,
            "fehler": fehler, "gesamt": len(alle_events),
            "hinweise": lauf["hinweise"], "ki": lauf["ki"]}


def _ki_text(ki: list) -> str:
    if not ki:
        return ""
    zeilen = [f"🤖 KI-Abruf {k['name']}: {k['termine']} Termine, ~{k['kosten_usd']:.2f} $"
              + (" – Seitentext gekürzt" if k.get("gekuerzt") else "")
              + (" – Antwort abgeschnitten" if k.get("abgeschnitten") else "") for k in ki]
    return "\n".join(zeilen)


def cmd_import(secrets: dict) -> None:
    _sende_vorschau(fetch_and_save_pending(), secrets)


def _sende_vorschau(result: dict, secrets: dict) -> None:
    token   = secrets["TOKEN"]
    chat_id = secrets["CHAT_ID"]

    if _injection_findings:
        _notify_injection(token, chat_id, _injection_findings)
        _injection_findings.clear()
    extra = "\n".join(t for t in (_ki_text(result.get("ki", [])), *result.get("hinweise", [])) if t)
    if "error" in result:
        fehler = result.get("fehler", [])
        send_telegram(token, chat_id, f"⚠️ {result['error']}"
                      + (f"\nFehler bei: {', '.join(fehler)}" if fehler else "")
                      + (f"\n\n{extra}" if extra else ""))
        return

    uid        = result["uid"]
    neu_gesamt = result["neu"]
    dup_gesamt = result["duplikate"]
    sv_gesamt  = result["sv"]
    fehler     = result.get("fehler", [])

    pending_file = PENDING_DIR / f"heimat_pending_{uid}.json"
    pending      = json.loads(pending_file.read_text())
    alle_events  = pending["events"]

    def _vorschau_zeile(e: dict) -> str:
        veranst = e.get("_verein_name", "")
        ort     = e.get("ort", "")
        teile   = [e["bezeichnung"][:30]]
        if veranst: teile.append(veranst[:25])
        if ort:     teile.append(ort[:25])
        return f"• {e['datum']} {e.get('uhrzeit',''):5} – {' · '.join(teile)} [{e['_gemeinde']}]"

    neue     = [e for e in alle_events if e["_neu"]]
    vorschau = "\n".join(_vorschau_zeile(e) for e in neue[:15])
    if len(neue) > 15:
        vorschau += f"\n… +{len(neue)-15} weitere neue"

    sv_labels  = sorted({e.get("_label") or e["_verein_key"] for e in alle_events if e.get("_sv")})
    sv_hinweis = (f"\n\nℹ️ Davon {sv_gesamt} Termine bei selbstverwaltenden Vereinen (bitte bei Bestätigung prüfen): "
                  + ", ".join(sv_labels)) if sv_gesamt else ""
    zähler     = f"Gesamt: {result['gesamt']} | 🆕 Neu: {neu_gesamt} | ⏭ Duplikate: {dup_gesamt}"
    if sv_gesamt:
        zähler += f" | 🔒 SV: {sv_gesamt}"

    gemeinden_str = ", ".join(dict.fromkeys(e["_gemeinde"] for e in alle_events))
    msg = (f"🏡 Termin-Import ({pending.get('quelle', '')})\n"
           f"Gemeinden: {gemeinden_str}\n"
           f"{zähler}\n\n"
           + (vorschau if neue else "Alle Termine bereits vorhanden.")
           + sv_hinweis
           + (f"\n\n{extra}" if extra else "")
           + f"\n\n→ Admin-Bereich: vereinskalender.online/#admin → Importe")

    send_telegram_inline(token, chat_id, msg, [[
        {"text": f"✅ {neu_gesamt} importieren", "callback_data": f"heimat_ok:{uid}"},
        {"text": "❌ Verwerfen",                 "callback_data": f"heimat_no:{uid}"},
    ]])

    if fehler:
        send_telegram(token, chat_id, f"⚠️ Fetch-Fehler bei: {', '.join(fehler)}")


def cmd_add(url: str, secrets: dict) -> None:
    _sende_vorschau(fetch_and_save_pending_for_url(url), secrets)


def _get_dropbox_token(secrets: dict) -> str:
    data = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "refresh_token": secrets["DROPBOX_REFRESH_TOKEN"],
        "client_id":     secrets["DROPBOX_APP_KEY"],
        "client_secret": secrets["DROPBOX_APP_SECRET"],
    }).encode()
    req = urllib.request.Request(
        "https://api.dropbox.com/oauth2/token", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())["access_token"]


def _upload_dropbox(token: str, file_bytes: bytes, path: str) -> None:
    req = urllib.request.Request(
        "https://content.dropboxapi.com/2/files/upload",
        data=file_bytes, method="POST")
    req.add_header("Authorization",   f"Bearer {token}")
    req.add_header("Content-Type",    "application/octet-stream")
    req.add_header("Dropbox-API-Arg", json.dumps(
        {"path": path, "mode": "overwrite", "mute": True}))
    with urllib.request.urlopen(req, timeout=30):
        pass


def _download_dropbox(token: str, path: str) -> bytes:
    req = urllib.request.Request(
        "https://content.dropboxapi.com/2/files/download", method="POST")
    req.add_header("Authorization",   f"Bearer {token}")
    req.add_header("Dropbox-API-Arg", json.dumps({"path": path}))
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()




def _neuer_eintrag(url: str, geo: dict | None) -> dict:
    if geo:
        name = geo["name"]
    else:
        roh = [t for t in re.split(r"[/\-_.]+", urllib.parse.urlparse(url).path)
               if t and termin_scraper._norm(t) not in termin_scraper._PFAD_FUELL and not t.isdigit()]
        host = urllib.parse.urlparse(url).hostname or url
        name = (roh[-1].replace("-", " ").title() if roh else
                re.sub(r"^(www\.|vg\.|gemeinde-|markt-|stadt-)", "", host).split(".")[0].replace("-", " ").title())
    eintrag = {"name": name, "label": f"Veranstaltungen {name}",
               "verein_key": "veranstaltungen_" + _slugify(name), "url": url}
    if geo:
        eintrag.update(landkreis=geo["landkreis"], gemeinde=geo["gemeinde"], plz=geo["plz"])
    return eintrag


def _melde_ki_quelle(eintrag: dict, termine: list, info: dict) -> None:
    """Neue Seite läuft nur per KI → Josef informieren + Todo für einen festen Parser."""
    cfg  = _cfg()
    host = urllib.parse.urlparse(eintrag["url"]).hostname or eintrag["url"]
    jahr = info["kosten_usd"] * 52
    todo_nr = 0
    try:
        from shared.pka_todos import todo_anlegen
        todo_nr = todo_anlegen(
            f"VKO: Parser für Termin-Seite {host} bauen – läuft per KI-Rückfall "
            f"(~{info['kosten_usd']:.2f} $/Woche). Fixture nach tests/fixtures/scraper/, "
            f"in termin_scraper.PARSER eintragen; die Gemeinde wechselt dann beim nächsten Lauf "
            f"automatisch. URL: {eintrag['url']}", cfg)
    except Exception as ex:
        _log(f"⚠️  Todo für KI-Quelle nicht angelegt: {ex}")
    msg = (f"🤖 Neue Termin-Seite nur per KI lesbar\n"
           f"Seite: {eintrag['url']}\n"
           f"Gemeinde: {eintrag['name']}" + (f" ({eintrag['landkreis']})" if eintrag.get("landkreis") else
                                             " – nicht erkannt, Landkreis beim Bestätigen prüfen") + "\n"
           f"Gefunden: {len(termine)} Termine\n"
           f"Kosten: ~{info['kosten_usd']:.2f} $ pro Abruf (~{jahr:.0f} $/Jahr bei wöchentlichem Lauf), "
           f"{info['modell']}\n"
           + ("⚠️ Seitentext war zu lang und wurde gekürzt – evtl. fehlen Termine.\n" if info["gekuerzt"] else "")
           + "Kein heimat-info, kein bekannter Seiten-Baukasten, keine strukturierten Daten.\n"
           + (f"📝 Todo #{todo_nr} angelegt: festen Parser bauen." if todo_nr else
              "⚠️ Todo konnte nicht angelegt werden – bitte manuell erfassen."))
    try:
        send_telegram(cfg["TOKEN"], cfg["CHAT_ID"], msg)
    except Exception as ex:
        _log(f"⚠️  Telegram-Hinweis KI-Quelle fehlgeschlagen: {ex}")


def fetch_and_save_pending_for_url(url: str) -> dict:
    """Import für eine einzelne URL. Bekannte Gemeinde: direkt importieren.
    Neue URL – Abrufweg bestimmen und in heimat_gemeinden.json eintragen:
      1. bekannter Seiten-Baukasten / JSON-LD / iCal (kostenlos, ohne Browser)
      2. heimat-info-Einbindung per Playwright
      3. KI-Rückfall (kostet pro Lauf) → Telegram-Hinweis + Todo für einen festen Parser
    Gibt Summary-Dict zurück (wie fetch_and_save_pending) oder {"error": "..."}."""
    gemeinden = _lade_gemeinden()
    if any(g.get("url") == url for g in gemeinden):
        return fetch_and_save_pending(gemeinden_filter=[url])

    _log(f"Discovery für neue URL: {url}")
    heute = datetime.now().strftime("%Y-%m-%d")
    try:
        html = termin_scraper.lade(url)
    except Exception as ex:
        return {"error": f"Seite nicht abrufbar: {ex}"}
    eintrag = _neuer_eintrag(url, termin_scraper.gemeinde_aus_seite(html, url))
    hinweise = [f"🆕 Neue Gemeinde {eintrag['name']}"
                + (f" ({eintrag['landkreis']})" if eintrag.get("landkreis") else " – Gemeinde/Landkreis nicht erkannt")
                + ": Ortschaften in orte.json nachtragen (siehe VKO-CLAUDE.md, Geo-Register)."]

    termine = None
    try:
        statisch = termin_scraper.erkenne_statisch(url, html, heute)
    except Exception as ex:
        _log(f"  Statische Erkennung fehlgeschlagen: {ex}")
        statisch = None
    if statisch:
        termine = statisch.pop("termine")
        eintrag.update(statisch)
        _log(f"  → Abrufweg {statisch}: {len(termine)} Termine")
    else:
        try:
            c_id = discover_c_id(url)
        except Exception as ex:
            _log(f"  Playwright-Discovery fehlgeschlagen: {ex}")
            c_id = None
        if c_id:
            vorhanden = next((g for g in gemeinden if g.get("c_id") == c_id), None)
            if vorhanden:
                return fetch_and_save_pending(gemeinden_filter=[vorhanden["url"]])
            eintrag.update(typ="heimat", c_id=c_id)
            _log(f"  → heimat-info c={c_id[:8]}…")
        else:
            try:
                termine, info = termin_scraper.ki_extrahieren(
                    html, url, heute, _cfg().get("CLAUDE_API_KEY", ""))
            except Exception as ex:
                return {"error": f"Kein Abrufweg gefunden, KI-Rückfall fehlgeschlagen: {ex}"}
            _log(f"  → KI: {len(termine)} Termine, ~{info['kosten_usd']:.3f} $")
            if not termine:
                return {"error": f"Auch die KI hat auf {url} keine künftigen Termine gefunden "
                                 f"(~{info['kosten_usd']:.2f} $) – nichts gespeichert."}
            eintrag["typ"] = "ki"
            _melde_ki_quelle(eintrag, termine, info)
            hinweise.append(_ki_text([{"name": eintrag["name"], "termine": len(termine), **info}]))

    gemeinden = _lade_gemeinden()
    gemeinden.append(eintrag)
    _speichere_gemeinden(gemeinden)
    _log(f"✅ Neue Gemeinde gespeichert: {eintrag['name']} ({eintrag.get('typ')})")

    result = fetch_and_save_pending(gemeinden_filter=[url],
                                    vorab={url: termine} if termine is not None else None)
    result.setdefault("hinweise", []).extend(hinweise)
    return result


def main() -> None:
    secrets = load_secrets()
    if len(sys.argv) >= 3 and sys.argv[1] == "--add":
        cmd_add(sys.argv[2], secrets)
    else:
        cmd_import(secrets)


if __name__ == "__main__":
    main()
