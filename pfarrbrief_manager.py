#!/opt/rename-webhook/bin/python3
"""
pfarrbrief_manager.py
Verarbeitet einen Pfarrbrief-Scan aus Dropbox:
- Extrahiert Gottesdienste via Claude Vision
- Filtert auf Hölskofen und Paindlkofen
- Speichert Termine in gottesdienste.json
- Verschiebt Pfarrbrief nach /Dokumente/Pfarrbriefe/
"""

import json
import re
import sys
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path

import anthropic
import dropbox

sys.path.insert(0, "/opt/rename-webhook")
from shared.secrets import load_secrets
from shared.telegram import send_telegram

GOTTESDIENSTE_FILE = Path("/opt/rename-webhook/gottesdienste.json")
def _normalize(s: str) -> str:
    return s.lower().replace("ö","oe").replace("ü","ue").replace("ä","ae").replace("ß","ss").strip()

def _ort_match(ort: str, kandidaten: list[str], schwelle: float = 0.75) -> bool:
    from difflib import SequenceMatcher
    n = _normalize(ort)
    for k in kandidaten:
        nk = _normalize(k)
        if nk in n or n in nk:
            return True
        ratio = SequenceMatcher(None, n, nk).ratio()
        if ratio >= schwelle:
            return True
    return False

ORTE_HK = ["hölskofen", "hoelskofen", "hölskofen"]
ORTE_PK = ["paindlkofen"]
ORTE_OK = ["oberköllnbach", "oberkoellnbach", "oberkollnbach"]
DROPBOX_ZIELORDNER = "/Dokumente/Pfarrbriefe"


def get_dropbox_client(secrets: dict) -> dropbox.Dropbox:
    return dropbox.Dropbox(
        oauth2_refresh_token=secrets["DROPBOX_REFRESH_TOKEN"],
        app_key=secrets["DROPBOX_APP_KEY"],
        app_secret=secrets["DROPBOX_APP_SECRET"],
    )


def download_file(dbx: dropbox.Dropbox, dropbox_path: str) -> bytes:
    _, response = dbx.files_download(dropbox_path)
    return response.content


def extract_gottesdienste(api_key: str, file_bytes: bytes, filename: str) -> list[dict]:
    import base64 as b64mod
    ext        = Path(filename).suffix.lower()
    media_type = "application/pdf" if ext == ".pdf" else "image/jpeg"
    block_type = "document" if ext == ".pdf" else "image"
    data_b64   = b64mod.standard_b64encode(file_bytes).decode("utf-8")

    heute = datetime.now().strftime("%Y-%m-%d")
    prompt = f"""Lies dieses Dokument vollständig durch alle Seiten.
Extrahiere ALLE Gottesdienst-Termine. Achte besonders auf Ortsangaben wie Hölskofen und Paindlkofen.
Gib das Ergebnis als JSON-Array zurück:
[{{"datum":"YYYY-MM-DD","uhrzeit":"HH:MM","ort":"Ortsname","art":"Art des Gottesdienstes"}}]

Zum Jahr im Feld "datum" (heute ist {heute}):
- Steht im Dokument ein Jahr (Titel, Gültigkeitszeitraum, Kopfzeile), nimm dieses.
- Steht bei einem Termin nur Tag und Monat, ergänze das Jahr so, dass der Termin
  NICHT in der Vergangenheit liegt – ein Pfarrbrief kündigt künftige Gottesdienste an.
- Schließe NIEMALS vom Wochentag auf das Jahr. Ein Wochentag passt auf viele Jahre;
  das führt zu Terminen, die Jahre zurückliegen.
- Nur wenn das Dokument selbst erkennbar alt ist, dürfen Termine in der Vergangenheit liegen.

Nur das JSON-Array, nichts anderes. Wenn kein Termin gefunden: []."""

    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": [
            {"type": block_type, "source": {"type": "base64", "media_type": media_type, "data": data_b64}},
            {"type": "text", "text": prompt},
        ]}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        result = json.loads(r.read())

    text = result["content"][0]["text"].strip()
    text = re.sub(r"```json|```", "", text).strip()
    s, e = text.find("["), text.rfind("]")
    if s == -1 or e == -1:
        return []
    return json.loads(text[s:e+1])


def filter_hk(termine: list[dict]) -> list[dict]:
    return [t for t in termine if _ort_match(t.get("ort", ""), ORTE_HK)]

def filter_pk(termine: list[dict]) -> list[dict]:
    return [t for t in termine if _ort_match(t.get("ort", ""), ORTE_PK)]

def filter_ok(termine: list[dict]) -> list[dict]:
    return [t for t in termine if _ort_match(t.get("ort", ""), ORTE_OK)]


def load_gottesdienste() -> dict:
    if GOTTESDIENSTE_FILE.exists():
        try:
            data = json.loads(GOTTESDIENSTE_FILE.read_text())
            if isinstance(data, list):
                return {"hk": data, "pk": [], "ok": []}
            # Migration hk_pk → hk+pk
            if "hk_pk" in data:
                return {"hk": data.get("hk_pk", []), "pk": [], "ok": data.get("ok", [])}
            return data
        except Exception:
            pass
    return {"hk": [], "pk": [], "ok": []}


def save_gottesdienste(data: dict) -> None:
    # Vorherigen Stand sichern – am 2026-09-27 ging der Altstand bei einem
    # manuellen Lauf verloren, weil write_text() direkt überschreibt.
    if GOTTESDIENSTE_FILE.exists():
        try:
            GOTTESDIENSTE_FILE.with_suffix(".json.bak").write_text(
                GOTTESDIENSTE_FILE.read_text()
            )
        except Exception as e:
            print(f"   ⚠️ Backup fehlgeschlagen (nicht kritisch): {e}")
    GOTTESDIENSTE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def merge_termine(bestehende: list, neue: list, verworfen: list | None = None) -> list:
    """Fügt neue Termine hinzu und entfernt vergangene.

    `verworfen` sammelt (optional) die wegen Datum entfernten Termine. Ohne das
    war der Filter stumm: am 2026-09-27 lieferte die Extraktion 21 Termine mit
    Jahr 2009 (Wochentags-Rückschluss), alle wurden verworfen – die
    Telegram-Meldung behauptete trotzdem Erfolg und listete sie auf.
    """
    keys = {(t["datum"], t["uhrzeit"], t["ort"]) for t in bestehende}
    for t in neue:
        k = (t["datum"], t["uhrzeit"], t["ort"])
        if k not in keys:
            bestehende.append(t)
            keys.add(k)
    heute = datetime.now().strftime("%Y-%m-%d")
    if verworfen is not None:
        # Nur die NEU gelieferten Termine melden. Würde man hier über `bestehende`
        # laufen, meldete jeder normale Lauf auch die inzwischen abgelaufenen
        # Termine aus der Datei als "ignoriert" – Dauer-Fehlalarm.
        verworfen.extend(t for t in neue if t.get("datum", "") < heute)
    bestehende = [t for t in bestehende if t["datum"] >= heute]
    bestehende.sort(key=lambda t: (t["datum"], t["uhrzeit"]))
    return bestehende


def main():
    if len(sys.argv) < 2:
        print("Usage: pfarrbrief_manager.py <dropbox_path>")
        sys.exit(1)

    dropbox_path = sys.argv[1]
    filename     = Path(dropbox_path).name

    secrets  = load_secrets()
    dbx      = get_dropbox_client(secrets)
    api_key  = secrets["CLAUDE_API_KEY"]
    tg_token = secrets["TOKEN"]
    chat_id  = secrets["CHAT_ID"]

    print(f"📋 Verarbeite Pfarrbrief: {filename}")

    # Datei herunterladen
    file_bytes = download_file(dbx, dropbox_path)

    # Termine extrahieren
    alle_termine = extract_gottesdienste(api_key, file_bytes, filename)
    print(f"   {len(alle_termine)} Termine gefunden")

    # Filtern
    hk = filter_hk(alle_termine)
    pk = filter_pk(alle_termine)
    ok = filter_ok(alle_termine)
    print(f"   {len(hk)} Termine Hölskofen, {len(pk)} Paindlkofen, {len(ok)} Oberköllnbach")

    # Plausibilitätsprüfung VOR Speichern und Verschieben.
    # Ein Pfarrbrief kündigt künftige Gottesdienste an. Liegt kein einziger
    # extrahierter Termin in der Zukunft, ist nicht der Pfarrbrief alt, sondern
    # die Extraktion gescheitert (2026-09-27: alle 89 Termine mit Jahr 2009).
    # Dann nichts speichern, nichts verschieben – sonst muss die Datei hinterher
    # aus dem Zielordner zurückgeholt werden.
    heute = datetime.now().strftime("%Y-%m-%d")
    kuenftig = [t for t in alle_termine if t.get("datum", "") >= heute]
    if alle_termine and not kuenftig:
        jahre = sorted({(t.get("datum") or "????")[:4] for t in alle_termine})
        print(f"   ❌ Kein einziger Termin in der Zukunft – Jahre im Ergebnis: {', '.join(jahre)}")
        send_telegram(tg_token, chat_id,
            f"❌ Pfarrbrief NICHT verarbeitet: {filename}\n\n"
            f"{len(alle_termine)} Termine erkannt, aber keiner liegt in der Zukunft "
            f"(Jahre im Ergebnis: {', '.join(jahre)}).\n\n"
            f"Das ist ein Extraktionsfehler, kein alter Pfarrbrief. Es wurde nichts "
            f"gespeichert, die Datei liegt unverändert an ihrem Platz.")
        print("⚠️ Abgebrochen – nichts gespeichert, nichts verschoben")
        return

    # Gottesdienste.json aktualisieren
    verworfen: list = []
    data = load_gottesdienste()
    data["hk"] = merge_termine(data["hk"], hk, verworfen)
    data["pk"] = merge_termine(data["pk"], pk, verworfen)
    data["ok"] = merge_termine(data["ok"], ok, verworfen)
    save_gottesdienste(data)
    gespeichert_hk = [t for t in data["hk"] if t in hk]
    gespeichert_pk = [t for t in data["pk"] if t in pk]
    if verworfen:
        print(f"   ⚠️ {len(verworfen)} Termine wegen Datum in der Vergangenheit verworfen")

    # Pfarrbrief in Zielordner verschieben (Dateiname vom Rename-Job bereits korrekt)
    ziel_path = f"{DROPBOX_ZIELORDNER}/{filename}"
    if dropbox_path.lower() == ziel_path.lower():
        # Die Datei liegt schon im Zielordner – das ist der Normalfall bei einem
        # Wiederholungslauf. Ohne diese Prüfung würde files_move_v2(autorename=True)
        # eine Dublette "… (1).pdf" anlegen.
        print(f"   Liegt bereits im Zielordner, kein Verschieben nötig")
    else:
        try:
            dbx.files_move_v2(dropbox_path, ziel_path, autorename=True)
            print(f"   Verschoben nach {ziel_path}")
        except Exception as e:
            print(f"   ⚠️ Verschieben fehlgeschlagen: {e}")

    # Telegram-Bestätigung
    zeilen = [f"📋 Pfarrbrief verarbeitet: {filename}\n"]
    if gespeichert_hk or gespeichert_pk:
        for t in sorted(gespeichert_hk + gespeichert_pk, key=lambda x: (x["datum"], x["uhrzeit"])):
            datum = datetime.strptime(t["datum"], "%Y-%m-%d").strftime("%d.%m.%Y")
            zeilen.append(f"• {datum} {t['uhrzeit']} Uhr – {t['ort']}: {t['art']}")
    else:
        zeilen.append("ℹ️ Keine künftigen Termine in Hölskofen/Paindlkofen gespeichert.")
    zeilen.append(f"\n📍 {len([t for t in data['ok'] if t in ok])} Termine Oberköllnbach gespeichert (/Pfarrbrief-ok)")
    if verworfen:
        jahre = sorted({t["datum"][:4] for t in verworfen})
        zeilen.append(f"\n⚠️ {len(verworfen)} Termine ignoriert (Datum in der Vergangenheit, "
                      f"Jahre: {', '.join(jahre)}) – bei einem aktuellen Pfarrbrief ein Hinweis "
                      f"auf falsch erkannte Jahre.")

    send_telegram(tg_token, chat_id, "\n".join(zeilen))
    print("✅ Fertig")


if __name__ == "__main__":
    main()
