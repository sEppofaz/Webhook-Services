#!/opt/rename-webhook/bin/python3
"""
pip_update_audit.py
Prüft alle App-venvs auf veraltete Pakete und CVEs (pip-audit).
Klassifiziert Updates nach Ampelstatus und sendet Zusammenfassung per Telegram.
Läuft wöchentlich via Cron – macht KEINE automatischen Updates.
"""

import json
import re
import subprocess
import urllib.request
from pathlib import Path

VENVS = {
    "rename-webhook": "/opt/rename-webhook/bin",
    "claude-remote":  "/opt/claude-remote/bin",
    "kargl-invoice":  "/opt/kargl-invoice/bin",
    "life-doku":      "/opt/life-doku/venv/bin",
    "rechnungen":     "/opt/rechnungen/venv/bin",
}

AMPEL = {"rot": "🔴", "gelb": "🟡", "gruen": "🟢"}

# Nur Paketmanager-Tools, keine App-Laufzeit-Abhängigkeiten
IGNORE_PACKAGES = {"pip", "setuptools", "wheel", "pip-api"}


def load_secrets() -> dict:
    secrets = {}
    for line in Path("/etc/pka/secrets.env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^export\s+", "", line)
        if "=" in line:
            k, _, v = line.partition("=")
            secrets[k.strip()] = v.strip().strip('"').strip("'")
    return secrets


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    req     = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


def parse_version(v: str) -> tuple:
    """Gibt (major, minor, patch) zurück, fehlende Teile als 0."""
    parts = re.split(r"[.\-]", v)
    result = []
    for p in parts[:3]:
        try:
            result.append(int(p))
        except ValueError:
            result.append(0)
    while len(result) < 3:
        result.append(0)
    return tuple(result)


def ampel_fuer_update(current: str, latest: str) -> str:
    cur = parse_version(current)
    lat = parse_version(latest)
    if lat[0] > cur[0]:
        return "rot"
    if lat[1] > cur[1]:
        return "gelb"
    return "gruen"


def pip_outdated(bin_dir: str) -> list[dict]:
    """Gibt Liste von {name, version, latest_version} zurück."""
    try:
        result = subprocess.run(
            [f"{bin_dir}/pip", "list", "--outdated", "--format=json"],
            capture_output=True, text=True, timeout=60
        )
        return json.loads(result.stdout) if result.stdout.strip() else []
    except Exception:
        return []


def pip_audit(bin_dir: str) -> list[dict]:
    """Gibt Liste von CVE-Funden zurück: {name, version, id, description}."""
    audit_bin = f"{bin_dir}/pip-audit"
    pip_bin   = f"{bin_dir}/pip"

    # pip-audit nicht in jedem venv – fallback auf rename-webhook
    if not Path(audit_bin).exists():
        audit_bin = "/opt/rename-webhook/bin/pip-audit"

    try:
        result = subprocess.run(
            [audit_bin, "--format=json", "--requirement",
             "/dev/stdin", "--no-deps"],
            input="",
            capture_output=True, text=True, timeout=120
        )
        # pip-audit mit -r (requirements aus venv)
        result2 = subprocess.run(
            [audit_bin, "--format=json",
             f"--path={Path(bin_dir).parent}"],
            capture_output=True, text=True, timeout=120
        )
        data = json.loads(result2.stdout) if result2.stdout.strip() else {}
        vulns = []
        for dep in data.get("dependencies", []):
            for v in dep.get("vulns", []):
                vulns.append({
                    "name":        dep["name"],
                    "version":     dep["version"],
                    "id":          v["id"],
                    "description": v.get("description", "")[:120],
                })
        return vulns
    except Exception:
        return []


def analyse_venv(name: str, bin_dir: str) -> dict:
    outdated = pip_outdated(bin_dir)
    cves     = pip_audit(bin_dir)

    # Paketmanager-Tools aus Ampel-Berechnung ausschließen
    relevant = [p for p in outdated if p["name"].lower() not in IGNORE_PACKAGES]

    # Gesamt-Ampel bestimmen
    if cves:
        gesamt = "rot"
    elif not relevant:
        gesamt = "gruen"
    else:
        einzelampeln = [ampel_fuer_update(p["version"], p["latest_version"]) for p in relevant]
        if "rot" in einzelampeln:
            gesamt = "rot"
        elif "gelb" in einzelampeln:
            gesamt = "gelb"
        else:
            gesamt = "gruen"

    return {
        "name":     name,
        "gesamt":   gesamt,
        "outdated": outdated,
        "cves":     cves,
    }


def format_block(r: dict) -> str:
    symbol = AMPEL[r["gesamt"]]
    lines  = [f"{symbol} <b>{r['name']}</b>"]

    for p in r["outdated"]:
        if p["name"].lower() in IGNORE_PACKAGES:
            continue
        a = ampel_fuer_update(p["version"], p["latest_version"])
        prefix = "⚠️" if a == "rot" else ("↑" if a == "gelb" else "·")
        lines.append(f"  {prefix} {p['name']} {p['version']} → {p['latest_version']}")

    for c in r["cves"]:
        lines.append(f"  🛑 CVE {c['id']}: {c['name']} {c['version']}")

    relevant_shown = [p for p in r["outdated"] if p["name"].lower() not in IGNORE_PACKAGES]
    if not relevant_shown and not r["cves"]:
        lines.append("  Alles aktuell ✓")

    # Handlungsempfehlung
    if r["gesamt"] == "rot":
        lines.append("  → Manuell prüfen vor Update!")
    elif r["gesamt"] == "gelb":
        lines.append("  → Minor-Updates, kurz testen")

    return "\n".join(lines)


def main() -> None:
    from datetime import date
    secrets = load_secrets()

    results = []
    for name, bin_dir in VENVS.items():
        if not Path(f"{bin_dir}/pip").exists():
            continue
        results.append(analyse_venv(name, bin_dir))

    # Sortierung: rot → gelb → grün
    order = {"rot": 0, "gelb": 1, "gruen": 2}
    results.sort(key=lambda r: order[r["gesamt"]])

    blocks = [format_block(r) for r in results]

    rote   = sum(1 for r in results if r["gesamt"] == "rot")
    gelbe  = sum(1 for r in results if r["gesamt"] == "gelb")
    header = f"🔍 <b>Pip-Update-Report – {date.today()}</b>"
    if rote:
        header += f"\n⚠️ {rote} App(s) kritisch – Handlung erforderlich"

    message = header + "\n\n" + "\n\n".join(blocks)

    if not rote and not gelbe:
        message += "\n\n✅ Alle Apps aktuell und sicher."

    send_telegram(secrets["TOKEN"], secrets["CHAT_ID"], message)
    print(message)


if __name__ == "__main__":
    main()
