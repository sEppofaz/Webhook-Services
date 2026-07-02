import json
import math

import anthropic
import yfinance as yf
from flask import Blueprint, jsonify, request

from shared.secrets import load_secrets

aktien_bp = Blueprint("aktien", __name__)

_ALLOWED_ORIGIN = "https://seppofaz.github.io"

_KI_PROMPT = """Du bist ein Finanzanalyst. Gib für "{firma}" (Ticker: {ticker}) diese drei SaaS/Tech-Kennzahlen aus dem letzten verfügbaren Geschäftsjahr zurück.

Antworte NUR mit JSON (kein Text drumherum):
{{
  "nrr_pct": <Net Revenue Retention in %, z.B. 118.5>,
  "churn_pct": <jährliche Kundenabwanderungsrate in %, z.B. 8.2>,
  "ltv_cac": <LTV/CAC Verhältnis, z.B. 3.5>,
  "konfidenz": "<hoch|mittel|niedrig>",
  "hinweis": "<kurze Anmerkung auf Deutsch, max 15 Wörter>"
}}

Falls ein Wert unbekannt oder nicht anwendbar ist, setze ihn auf null.
Basiere dich auf öffentlich bekannte Informationen aus SEC-Filings, Earnings Calls oder Investorenpräsentationen."""


def _cors():
    return {
        "Access-Control-Allow-Origin": _ALLOWED_ORIGIN,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Token",
    }


def _safe(v):
    """Gibt None zurück wenn v NaN/inf ist."""
    try:
        return None if (v is None or math.isnan(float(v)) or math.isinf(float(v))) else v
    except (TypeError, ValueError):
        return None


def _yfinance_data(ticker_sym: str):
    t = yf.Ticker(ticker_sym)
    info = t.info or {}

    felder = {}
    quellen = {}

    def set_f(key, value):
        v = _safe(value)
        if v is not None:
            felder[key] = v
            quellen[key] = "yahoo"

    ev  = _safe(info.get("enterpriseValue"))
    rev = _safe(info.get("totalRevenue"))

    if ev:
        set_f("evs-ev", round(ev / 1e6, 0))
    if rev:
        rev_mio = round(rev / 1e6, 0)
        set_f("evs-umsatz", rev_mio)
        set_f("gm-umsatz",  rev_mio)
        set_f("sbc-umsatz", rev_mio)

    growth = _safe(info.get("revenueGrowth"))
    if growth is not None:
        set_f("r40-wachstum", round(growth * 100, 1))

    gm = _safe(info.get("grossMargins"))
    if gm is not None and rev:
        set_f("gm-cogs", round(rev * (1 - gm) / 1e6, 0))

    fcf = _safe(info.get("freeCashflow"))
    if fcf is not None:
        set_f("sbc-fcf", round(fcf / 1e6, 0))
        if rev:
            set_f("r40-marge", round(fcf / rev * 100, 1))

    # SBC aus Cash-Flow-Statement
    try:
        cf = t.cashflow
        if cf is not None and not cf.empty:
            for row in ("Stock Based Compensation", "Share Based Compensation"):
                if row in cf.index:
                    v = _safe(cf.loc[row].iloc[0])
                    if v is not None:
                        set_f("sbc-sbc", round(float(v) / 1e6, 0))
                    break
    except Exception:
        pass

    firma_name = info.get("longName") or ticker_sym
    return felder, quellen, firma_name


def _ki_data(ticker_sym: str, firma_name: str, secrets: dict) -> dict:
    client = anthropic.Anthropic(api_key=secrets["CLAUDE_API_KEY"])
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": _KI_PROMPT.format(
            firma=firma_name, ticker=ticker_sym
        )}],
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(raw)


def _nrr_felder(nrr_pct: float) -> dict:
    arr, churn = 1000.0, 30.0
    expansion = max(0.0, round((nrr_pct / 100 - 1) * arr + churn, 1))
    return {"nrr-arr": arr, "nrr-exp": expansion, "nrr-churn": churn}


def _churn_felder(churn_pct: float) -> dict:
    return {"churn-start": 1000.0, "churn-verlust": round(churn_pct / 100 * 1000, 1)}


def _ltvcac_felder(ratio: float) -> dict:
    return {"cac-ltv": round(ratio * 1000, 0), "cac-cac": 1000.0}


@aktien_bp.route("/aktien-lookup", methods=["OPTIONS", "POST"])
def aktien_lookup():
    ch = _cors()
    if request.method == "OPTIONS":
        return "", 204, ch

    secrets = load_secrets()
    data = request.get_json(silent=True) or {}
    ticker_sym = (data.get("ticker") or "").strip().upper()
    if not ticker_sym:
        return jsonify({"error": "ticker erforderlich"}), 400, ch

    try:
        felder, quellen, firma_name = _yfinance_data(ticker_sym)

        ki = _ki_data(ticker_sym, firma_name, secrets)
        ki_konfidenz = ki.get("konfidenz", "niedrig")
        ki_hinweis   = ki.get("hinweis", "")

        for ki_key, calc_fn, ki_field in [
            ("nrr_pct",   _nrr_felder,   "nrr"),
            ("churn_pct", _churn_felder, "churn"),
            ("ltv_cac",   _ltvcac_felder, "cac"),
        ]:
            v = _safe(ki.get(ki_key))
            if v is not None:
                sub = calc_fn(float(v))
                felder.update(sub)
                for k in sub:
                    quellen[k] = "ki-geschätzt"

        return jsonify({
            "firma":        firma_name,
            "felder":       felder,
            "quellen":      quellen,
            "ki_konfidenz": ki_konfidenz,
            "ki_hinweis":   ki_hinweis,
        }), 200, ch

    except Exception as e:
        return jsonify({"error": str(e)}), 500, ch


_US_EXCHANGES = {'NYQ', 'NMS', 'NGM', 'PCX', 'ASE', 'BTS', 'OQB', 'OQX'}

@aktien_bp.route("/aktien-search")
def aktien_search():
    ch = _cors()
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([]), 200, ch
    try:
        results = yf.Search(q, max_results=10).quotes
        hits = []
        for r in results:
            if r.get("quoteType") != "EQUITY":
                continue
            sym  = r.get("symbol", "")
            name = (r.get("shortname") or r.get("longname") or "").strip()
            if not sym or not name:
                continue
            us = r.get("exchange", "") in _US_EXCHANGES
            hits.append({"ticker": sym, "name": name, "us": us})
        hits.sort(key=lambda x: (0 if x["us"] else 1, x["ticker"]))
        return jsonify(hits[:6]), 200, ch
    except Exception as e:
        return jsonify({"error": str(e)}), 500, ch
