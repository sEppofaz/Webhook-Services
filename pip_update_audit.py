#!/opt/rename-webhook/bin/python3
"""
pip_update_audit.py
Wöchentlicher Server-Check: prüft ALLE App-venvs auf veraltete Pakete und CVEs (pip-audit)
und fügt einen Server-Inventar-Abschnitt hinzu (Whitelist-Abgleich, crashende Services,
apt, Node). Klassifiziert nach Ampelstatus und sendet die Zusammenfassung per Telegram.
Läuft wöchentlich via Cron – macht KEINE automatischen Updates.

Die venv-Liste und der Inventar-Check kommen aus `pip-upgrade-safe check --json`
(Single Source of Truth, derselbe Check läuft vor jedem Upgrade). Fällt das Script aus,
greift eine eigene venv-Erkennung unter /opt.

  pip_update_audit.py            # Report + Telegram
  pip_update_audit.py --dry-run  # nur ausgeben, weder secrets.env lesen noch senden
"""

import html
import json
import re
import subprocess
import sys
import urllib.request
from datetime import date
from pathlib import Path

UPGRADE_SAFE = "/usr/local/bin/pip-upgrade-safe"

AMPEL = {"rot": "🔴", "gelb": "🟡", "gruen": "🟢"}
INV_ICON = {"rot": "🔴", "gelb": "🟡", "ok": "✓"}

# Nur Paketmanager-Tools, keine App-Laufzeit-Abhängigkeiten
IGNORE_PACKAGES = {"pip", "setuptools", "wheel", "pip-api"}

MAX_NAMES_SHOWN = 6        # Minor-Updates pro venv namentlich (Rest als „+N")
TELEGRAM_LIMIT = 4000      # Telegram-Hardlimit 4096, etwas Puffer


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


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Teilt an Absatzgrenzen (Leerzeile), damit kein Block zerrissen wird."""
    chunks, cur = [], ""
    for part in text.split("\n\n"):
        if cur and len(cur) + 2 + len(part) > limit:
            chunks.append(cur)
            cur = part
        else:
            cur = f"{cur}\n\n{part}" if cur else part
    if cur:
        chunks.append(cur)
    return chunks


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


def discover_venvs_fallback() -> dict[str, str]:
    """Eigene venv-Erkennung – nur falls pip-upgrade-safe nicht aufrufbar ist."""
    venvs = {}
    for d in sorted(p for p in Path("/opt").iterdir() if p.is_dir()):
        for root in (d / "venv", d):
            if (root / "pyvenv.cfg").is_file() and (root / "bin" / "pip").exists():
                venvs[d.name] = str(root / "bin")
                break
    return venvs


def load_inventory() -> dict:
    """{'venvs': {name: bin-dir}, 'findings': [{level, text}]} von pip-upgrade-safe."""
    try:
        r = subprocess.run([UPGRADE_SAFE, "check", "--json"],
                           capture_output=True, text=True, timeout=240)
        inv = json.loads(r.stdout)
        if inv.get("venvs"):
            return inv
        raise ValueError("keine venvs im Ergebnis")
    except Exception as e:
        return {
            "venvs": discover_venvs_fallback(),
            "findings": [{"level": "gelb",
                          "text": f"Inventar-Check nicht verfügbar ({type(e).__name__}) – nur pip-Prüfung"}],
        }


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
    """Gibt Liste von CVE-Funden zurück: {name, version, id, fix}."""
    audit_bin = f"{bin_dir}/pip-audit"

    # pip-audit nicht in jedem venv – fallback auf rename-webhook
    if not Path(audit_bin).exists():
        audit_bin = "/opt/rename-webhook/bin/pip-audit"

    # --path muss auf site-packages zeigen, nicht auf die venv-Wurzel –
    # sonst matcht pip-audit keine installierten Pakete und meldet
    # stillschweigend 0 CVEs (Bug entdeckt 2026-08-16, siehe Pitfall in CLAUDE.md).
    site_packages = next(Path(bin_dir).parent.glob("lib/python3.*/site-packages"), None)
    if site_packages is None:
        return []

    try:
        result = subprocess.run(
            [audit_bin, "--format=json", f"--path={site_packages}"],
            capture_output=True, text=True, timeout=120
        )
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        vulns = []
        for dep in data.get("dependencies", []):
            for v in dep.get("vulns", []):
                vulns.append({
                    "name":    dep["name"],
                    "version": dep["version"],
                    "id":      v["id"],
                    "fix":     v.get("fix_versions") or [],
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


def cve_lines(cves: list[dict]) -> list[str]:
    """Je Paket eine Zeile (mehrere Advisories zusammengefasst, höchste Fix-Version)."""
    grouped: dict[tuple, dict] = {}
    for c in cves:
        g = grouped.setdefault((c["name"], c["version"]), {"ids": set(), "fix": []})
        g["ids"].add(c["id"])
        g["fix"] += c["fix"]
    lines = []
    for (name, version), g in grouped.items():
        fix = max(g["fix"], key=parse_version) if g["fix"] else None
        ids = sorted(g["ids"])
        what = ids[0] if len(ids) == 1 else f"{len(ids)} Advisories"
        lines.append(f"  🛑 {html.escape(name)} {html.escape(version)}: {html.escape(what)}"
                     + (f" → Fix {html.escape(fix)}" if fix else ""))
    return lines


def format_block(r: dict) -> str:
    symbol = AMPEL[r["gesamt"]]
    lines  = [f"{symbol} <b>{html.escape(r['name'])}</b>"]

    relevant = [p for p in r["outdated"] if p["name"].lower() not in IGNORE_PACKAGES]
    majors = [p for p in relevant if ampel_fuer_update(p["version"], p["latest_version"]) == "rot"]
    others = [p for p in relevant if p not in majors]

    for p in majors:
        lines.append(f"  ⚠️ {html.escape(p['name'])} {html.escape(p['version'])} → {html.escape(p['latest_version'])}")
    if others:
        names = ", ".join(html.escape(p["name"]) for p in others[:MAX_NAMES_SHOWN])
        more  = f" (+{len(others) - MAX_NAMES_SHOWN})" if len(others) > MAX_NAMES_SHOWN else ""
        lines.append(f"  ↑ {len(others)} Minor-Update(s): {names}{more}")

    lines += cve_lines(r["cves"])

    if not relevant and not r["cves"]:
        lines.append("  Alles aktuell ✓")

    # Handlungsempfehlung
    if r["cves"]:
        lines.append("  → Sicherheitslücke: zeitnah updaten!")
    elif r["gesamt"] == "rot":
        lines.append("  → Manuell prüfen vor Update!")
    elif r["gesamt"] == "gelb":
        lines.append("  → Minor-Updates, kurz testen")

    return "\n".join(lines)


def format_inventory(findings: list[dict], venv_count: int) -> str:
    """Server-Inventar-Abschnitt: Auffälligkeiten einzeln, Unauffälliges in einer Zeile."""
    warn = [f for f in findings if f["level"] in ("rot", "gelb")]
    order = {"rot": 0, "gelb": 1}
    lines = ["🖥 <b>Server-Inventar</b>"]
    for f in sorted(warn, key=lambda f: order[f["level"]]):
        lines.append(f"  {INV_ICON[f['level']]} {html.escape(f['text'])}")
    ok_count = sum(1 for f in findings if f["level"] == "ok")
    if not warn:
        lines.append(f"  ✓ {venv_count} venvs geprüft, {ok_count} Bereiche ohne Auffälligkeit")
    else:
        lines.append(f"  ✓ {venv_count} venvs geprüft, {ok_count} weitere Bereiche ohne Auffälligkeit")
    return "\n".join(lines)


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    inv = load_inventory()

    results = []
    for name, bin_dir in inv["venvs"].items():
        if not Path(f"{bin_dir}/pip").exists():
            continue
        results.append(analyse_venv(name, bin_dir))

    # Sortierung: rot → gelb → grün
    order = {"rot": 0, "gelb": 1, "gruen": 2}
    results.sort(key=lambda r: order[r["gesamt"]])

    blocks = [format_block(r) for r in results]

    rote   = sum(1 for r in results if r["gesamt"] == "rot")
    gelbe  = sum(1 for r in results if r["gesamt"] == "gelb")
    inv_rot  = sum(1 for f in inv["findings"] if f["level"] == "rot")
    inv_gelb = sum(1 for f in inv["findings"] if f["level"] == "gelb")

    header = f"🔍 <b>Pip-Update-Report – {date.today()}</b>"
    if rote:
        header += f"\n⚠️ {rote} App(s) kritisch – Handlung erforderlich"
    if inv_rot:
        header += f"\n⚠️ {inv_rot} kritische Server-Auffälligkeit(en) – Handlung erforderlich"

    message = header + "\n\n" + "\n\n".join(blocks)
    message += "\n\n" + format_inventory(inv["findings"], len(results))

    if not rote and not gelbe and not inv_rot and not inv_gelb:
        message += "\n\n✅ Alle Apps aktuell und sicher."

    if not dry_run:
        secrets = load_secrets()
        for part in split_message(message):
            send_telegram(secrets["TOKEN"], secrets["CHAT_ID"], part)
    print(message)


if __name__ == "__main__":
    main()
