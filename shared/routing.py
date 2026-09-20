"""
routing.py
Routing mit Live-Verkehr über TomTom (Geocoding + Calculate Route).
Einzige Stelle mit Anbieter-Wissen – PWA (services/verkehr), Cron (traffic_info.py)
und Telegram-Bot rufen nur get_route() auf. Key: TOMTOM_API_KEY (secrets.env).
"""

import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from shared.secrets import load_secrets

_GEOCODE_URL = "https://api.tomtom.com/search/2/geocode/{q}.json"
_ROUTE_URL = "https://api.tomtom.com/routing/1/calculateRoute/{o}:{d}/json"
_TIMEOUT = 15
_MAX_QUERY_LEN = 200
_MAX_POINTS = 400
_MAX_LANDMARKS = 8
_GEOCODE_CACHE_MAX = 500
# Mehrdeutige Ortsnamen ("Hölskofen 12") sollen in der Heimat-Region landen, nicht irgendwo in Europa.
# Bias auf Hölskofen (Pfeffenhausen) – nur Rangfolge, kein Filter; Länder zuerst DACH, dann ohne Filter.
_BIAS_LAT, _BIAS_LON = 48.6657, 11.9648
_HOME_COUNTRIES = "DE,AT,CH"
# Es gibt mehrere Orte namens Hölskofen (u. a. ~25 km östlich von Josefs Heimatort). Eine Eingabe, die nur aus
# "Hölskofen" (+ optional Hausnummer) besteht, meint immer den Heimatort → PLZ/Ort ergänzen (der Bias allein reicht nicht).
_HOME_ALIAS_RE = re.compile(r"^Hölskofen(\s+\d+\s*[a-zA-Z]?)?$", re.IGNORECASE)
_HOME_ALIAS_SUFFIX = ", 84076 Pfeffenhausen"

_geocode_cache: dict = {}


def _api_key() -> str:
    key = os.environ.get("TOMTOM_API_KEY", "") or load_secrets().get("TOMTOM_API_KEY", "")
    if not key:
        raise RuntimeError("TOMTOM_API_KEY nicht konfiguriert")
    return key


def _get_json(url: str) -> dict:
    """GET → JSON. Fehlertexte enthalten nie die URL (sie trägt den Key)."""
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            body = json.loads(e.read())
            detail = (body.get("detailedError") or {}).get("message") or body.get("error") or ""
        except Exception:
            pass
        raise RuntimeError(f"Routing-API HTTP {e.code} {detail}".strip()) from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f"Routing-API nicht erreichbar: {getattr(e, 'reason', e)}") from None


def _geocode(query: str, key: str) -> tuple:
    q = query.strip()[:_MAX_QUERY_LEN]
    if q in _geocode_cache:
        return _geocode_cache[q]
    search = q + _HOME_ALIAS_SUFFIX if _HOME_ALIAS_RE.match(q) else q
    url = _GEOCODE_URL.format(q=urllib.parse.quote(search, safe=""))
    results = []
    for countries in (_HOME_COUNTRIES, None):
        params = {"key": key, "limit": 1, "language": "de-DE", "lat": _BIAS_LAT, "lon": _BIAS_LON}
        if countries:
            params["countrySet"] = countries
        results = _get_json(url + "?" + urllib.parse.urlencode(params)).get("results") or []
        if results:
            break
    if not results:
        raise RuntimeError(f"Adresse nicht gefunden: {q}")
    pos = results[0]["position"]
    if len(_geocode_cache) >= _GEOCODE_CACHE_MAX:
        _geocode_cache.clear()
    _geocode_cache[q] = (pos["lat"], pos["lon"])
    return _geocode_cache[q]


def _downsample(points: list) -> list:
    if len(points) <= _MAX_POINTS:
        return points
    sampled = points[::math.ceil(len(points) / _MAX_POINTS)]
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return sampled


def _encode_polyline(points: list) -> str:
    """Google Encoded Polyline (Precision 5) – so erwartet es decodePolyline() im Frontend."""
    out = []
    prev_lat = prev_lng = 0
    for lat, lng in points:
        ilat, ilng = round(lat * 1e5), round(lng * 1e5)
        for v in (ilat - prev_lat, ilng - prev_lng):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev_lat, prev_lng = ilat, ilng
    return "".join(out)


def _landmark_name(ins: dict) -> str:
    sign = (ins.get("signpostText") or "").strip()
    if sign:
        return re.split(r"\s*[/;]\s*", sign)[0].strip()
    roads = ins.get("roadNumbers") or []
    return str(roads[0]).strip() if roads else ""


def _extract_landmarks(instructions: list) -> list:
    """Wegweiser-Ziele (Fallback Straßennummer) entlang der Route, max. _MAX_LANDMARKS."""
    seen = set()
    found = []
    for ins in instructions:
        name = _landmark_name(ins)
        pt = ins.get("point") or {}
        if not name or name in seen or "latitude" not in pt or "longitude" not in pt:
            continue
        seen.add(name)
        found.append({"name": name, "lat": pt["latitude"], "lng": pt["longitude"]})
    if len(found) > _MAX_LANDMARKS:
        idx = [round(i * (len(found) - 1) / (_MAX_LANDMARKS - 1)) for i in range(_MAX_LANDMARKS)]
        found = [found[i] for i in idx]
    return found


def get_route(origin: str, destination: str) -> dict:
    """Adressen → {normal_sek, traffic_sek, dist_m, overview_polyline, landmarks}."""
    key = _api_key()
    o = _geocode(origin, key)
    d = _geocode(destination, key)
    params = urllib.parse.urlencode({
        "key": key,
        "traffic": "true",
        "departAt": "now",
        "travelMode": "car",
        "routeType": "fastest",
        "computeTravelTimeFor": "all",
        "instructionsType": "text",
        "language": "de-DE",
    })
    url = _ROUTE_URL.format(o=f"{o[0]},{o[1]}", d=f"{d[0]},{d[1]}") + "?" + params
    routes = _get_json(url).get("routes") or []
    if not routes:
        raise RuntimeError("Keine Route gefunden")
    route = routes[0]
    summary = route["summary"]
    traffic = summary["travelTimeInSeconds"]
    normal = summary.get("noTrafficTravelTimeInSeconds")
    if normal is None:
        normal = traffic - summary.get("trafficDelayInSeconds", 0)
    points = [(p["latitude"], p["longitude"]) for leg in route["legs"] for p in leg["points"]]
    return {
        "normal_sek": normal,
        "traffic_sek": traffic,
        "dist_m": summary["lengthInMeters"],
        "overview_polyline": _encode_polyline(_downsample(points)),
        "landmarks": _extract_landmarks((route.get("guidance") or {}).get("instructions") or []),
    }
