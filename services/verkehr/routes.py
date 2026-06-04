import json
import urllib.parse
import urllib.request

from flask import Blueprint, jsonify, request

from shared.secrets import load_secrets

verkehr_bp = Blueprint("verkehr", __name__)


def _get_route(api_key: str, origin: str, destination: str) -> dict:
    params = urllib.parse.urlencode({
        "origin": origin,
        "destination": destination,
        "departure_time": "now",
        "traffic_model": "best_guess",
        "key": api_key,
    })
    url = f"https://maps.googleapis.com/maps/api/directions/json?{params}"
    with urllib.request.urlopen(url, timeout=15) as r:
        data = json.loads(r.read())
    if data["status"] != "OK":
        raise RuntimeError(f"Directions API: {data['status']} – {data.get('error_message', '')}")
    leg = data["routes"][0]["legs"][0]
    return {
        "normal_sek": leg["duration"]["value"],
        "traffic_sek": leg["duration_in_traffic"]["value"],
        "dist_m": leg["distance"]["value"],
        "overview_polyline": data["routes"][0]["overview_polyline"]["points"],
    }


@verkehr_bp.route("/api/verkehr")
def api_verkehr():
    origin = request.args.get("origin", "").strip()
    destination = request.args.get("destination", "").strip()
    if not origin or not destination:
        return jsonify({"error": "origin und destination erforderlich"}), 400
    try:
        secrets = load_secrets()
        d = _get_route(secrets["GOOGLE_MAPS_API_KEY"], origin, destination)
        normal_min = d["normal_sek"] // 60
        traffic_min = d["traffic_sek"] // 60
        delta = max(0, traffic_min - normal_min)
        ampel = "green" if delta < 10 else ("yellow" if delta < 20 else "red")
        return jsonify({
            "normal_min": normal_min,
            "traffic_min": traffic_min,
            "delta_min": delta,
            "dist_km": round(d["dist_m"] / 1000, 1),
            "ampel": ampel,
            "overview_polyline": d["overview_polyline"],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
