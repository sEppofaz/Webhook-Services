import json
import urllib.parse
import urllib.request

import anthropic
from flask import Blueprint, jsonify, request

from shared.secrets import load_secrets

autoquartett_bp = Blueprint("autoquartett", __name__)

_ALLOWED_ORIGIN = "https://seppofaz.github.io"

_PROMPT = """Du bist ein Automobil-Experte. Gib für das Auto "{name}" ein JSON-Objekt zurück mit GENAU diesen Feldern:

{{
  "id": "eindeutiger_snake_case_identifier",
  "name": "Vollständiger Modellname",
  "hersteller": "Herstellername",
  "land": "Herstellungsland auf Deutsch",
  "baujahr": YYYY,
  "antrieb": "benziner" oder "elektro" oder "hybrid",
  "kategorie": "legende" oder "supercar" oder "hypercar" oder "elektro" oder "bmw_m",
  "bild": null,
  "kurzbeschreibung": "Maximal 2 Sätze auf Deutsch",
  "preis_eur": Zahl (Neupreis in EUR ohne Einheit),
  "preis_note": "kurze Anmerkung oder null",
  "leistung_ps": Zahl,
  "leistung_kw": Zahl,
  "drehmoment_nm": Zahl,
  "gewicht_kg": Zahl,
  "nullhundert_s": Zahl,
  "vmax_kmh": Zahl,
  "reichweite_km": Zahl oder null,
  "hubraum_ccm": Zahl oder null,
  "zylinder": Zahl oder null,
  "preis_pro_ps": Zahl (preis_eur geteilt durch leistung_ps, auf 0 Nachkommastellen gerundet),
  "leistungsgewicht_kg_ps": Zahl (gewicht_kg geteilt durch leistung_ps, auf 2 Nachkommastellen gerundet),
  "nurburgring_min": Zahl oder null
}}

Antworte NUR mit dem JSON-Objekt, kein weiterer Text, keine Erklärungen."""


def _wikipedia_image(car_name: str) -> str | None:
    for title in [car_name, car_name.replace("-", " ")]:
        try:
            params = urllib.parse.urlencode({
                "action": "query",
                "titles": title,
                "prop": "pageimages",
                "pithumbsize": 600,
                "format": "json",
                "origin": "*",
            })
            req = urllib.request.Request(
                f"https://en.wikipedia.org/w/api.php?{params}",
                headers={"User-Agent": "AutoQuartett/1.0 (private game)"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            for page in data.get("query", {}).get("pages", {}).values():
                thumb = page.get("thumbnail", {}).get("source")
                if thumb:
                    return thumb
        except Exception:
            continue
    return None


def _cors(response, status=200):
    headers = {
        "Access-Control-Allow-Origin": _ALLOWED_ORIGIN,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }
    if isinstance(response, str):
        return response, status, headers
    return response, status, headers


@autoquartett_bp.route("/autoquartett/car-lookup", methods=["OPTIONS", "POST"])
def car_lookup():
    cors_headers = {
        "Access-Control-Allow-Origin": _ALLOWED_ORIGIN,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }

    if request.method == "OPTIONS":
        return "", 204, cors_headers

    data = request.get_json(silent=True) or {}
    car_name = (data.get("name") or "").strip()
    if not car_name:
        return jsonify({"error": "name erforderlich"}), 400, cors_headers

    try:
        secrets = load_secrets()
        client = anthropic.Anthropic(api_key=secrets["CLAUDE_API_KEY"])

        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": _PROMPT.format(name=car_name)}],
        )

        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        car = json.loads(raw)
        car["bild"] = _wikipedia_image(car.get("name", car_name))
        car["custom"] = True

        return jsonify(car), 200, cors_headers

    except json.JSONDecodeError as e:
        return jsonify({"error": f"JSON-Parsing-Fehler: {e}"}), 500, cors_headers
    except Exception as e:
        return jsonify({"error": str(e)}), 500, cors_headers
