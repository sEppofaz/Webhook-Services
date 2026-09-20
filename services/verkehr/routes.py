from flask import Blueprint, jsonify, request

from shared.routing import get_route

verkehr_bp = Blueprint("verkehr", __name__)


@verkehr_bp.route("/api/verkehr")
def api_verkehr():
    origin = request.args.get("origin", "").strip()
    destination = request.args.get("destination", "").strip()
    if not origin or not destination:
        return jsonify({"error": "origin und destination erforderlich"}), 400
    try:
        d = get_route(origin, destination)
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
            "start_name": d["start_name"],
            "end_name": d["end_name"],
            "traffic": d["traffic"],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
