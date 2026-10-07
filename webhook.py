import os
import secrets
import threading
from pathlib import Path

from flask import Flask, request

from services.aktien.routes import aktien_bp
from services.auth.routes import auth_bp
from services.autoquartett.routes import autoquartett_bp
from services.kalender.routes import kalender_bp
from services.kalender_bot.routes import kalender_bot_bp
from services.rename.routes import rename_bp
from services.telegram.routes import freigabe_gruppe_ankuendigen, telegram_bp
from services.verein.routes import verein_bp
from services.verkehr.routes import verkehr_bp

_SECRET_KEY_FILE = Path("/opt/rename-webhook/flask_secret.key")

# Kostenpflichtige Endpunkte anderer Apps (TomTom, Claude) laufen im selben Prozess. Ihre
# Frontends rufen sie über umbenennen.duckdns.org auf, wo nginx sie eigens begrenzt
# (Aktien-Check ADR-001). Über die VKO-Domains (dort greift nur das allgemeine /api/-Limit)
# werden sie nicht gebraucht → gesperrt (Review 2026-10-04, Punkt 12).
_VKO_HOSTS = ("vereinskalender.online", "veranstaltungen.website")
_NICHT_VKO_PFADE = ("/api/verkehr", "/autoquartett/", "/aktien-")


def _fremde_app_ueber_vko() -> bool:
    host = (request.host or "").split(":")[0].lower()
    return host.endswith(_VKO_HOSTS) and request.path.startswith(_NICHT_VKO_PFADE)


def _load_secret_key() -> str:
    env_key = os.environ.get("FLASK_SECRET_KEY", "")
    if env_key:
        return env_key
    if _SECRET_KEY_FILE.exists():
        return _SECRET_KEY_FILE.read_text().strip()
    key = secrets.token_hex(32)
    try:
        _SECRET_KEY_FILE.write_text(key)
    except OSError:
        pass
    return key


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = _load_secret_key()
    # Flask-Session (CSRF-Token): nur per HTTPS, nicht cross-site
    app.config.update(SESSION_COOKIE_SECURE=os.environ.get("VKO_COOKIE_INSECURE", "") != "1",
                      SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)

    @app.before_request
    def _sperre_fremde_apps():
        if _fremde_app_ueber_vko():
            return "", 404
    app.register_blueprint(aktien_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(autoquartett_bp)
    app.register_blueprint(kalender_bp)
    app.register_blueprint(kalender_bot_bp)
    app.register_blueprint(rename_bp)
    app.register_blueprint(telegram_bp)
    app.register_blueprint(verein_bp)
    app.register_blueprint(verkehr_bp)
    threading.Thread(target=freigabe_gruppe_ankuendigen, daemon=True).start()
    return app


app = create_app()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000)
