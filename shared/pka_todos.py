"""PKA-Todo in Todos.json (Dropbox) anlegen – für Flask-Routen und Cronjobs.

`cfg` ist os.environ (Flask, EnvironmentFile) oder das Ergebnis von load_secrets() (Cron).
Schreibt mit Revisions-Prüfung, damit eine parallele Änderung aus der Todo-App nicht
überschrieben wird (ein Wiederholungsversuch).
"""
import json
import uuid
from datetime import datetime

TODOS_FILE_PATH = "/Apps/Claude/Todo-App/Todos.json"


def todo_anlegen(text: str, cfg, kategorie: str = "pka") -> int:
    """Legt ein Todo mit Prio „mittel" an und gibt seine Nummer zurück."""
    import dropbox
    dbx = dropbox.Dropbox(
        oauth2_refresh_token=cfg.get("DROPBOX_INVOICE_REFRESH_TOKEN", ""),
        app_key=cfg.get("DROPBOX_INVOICE_APP_KEY", ""),
        app_secret=cfg.get("DROPBOX_INVOICE_APP_SECRET", ""),
    )
    for versuch in range(2):
        try:
            meta, res = dbx.files_download(TODOS_FILE_PATH)
            data, modus = json.loads(res.content.decode("utf-8")), dropbox.files.WriteMode.update(meta.rev)
        except dropbox.exceptions.ApiError:
            data, modus = {"v": 1, "todos": []}, dropbox.files.WriteMode.add
        nr = max((t.get("nr") or 0 for t in data["todos"]), default=0) + 1
        data["todos"].append({
            "id": str(uuid.uuid4()),
            "nr": nr,
            "datum": datetime.now().strftime("%Y-%m-%d"),
            "aufgabe": text,
            "prio": "mittel",
            "kategorie": kategorie,
            "erledigt": False,
            "erledigt_am": None,
            "faelligkeit": None,
            "faelligkeit_uhrzeit": None,
        })
        try:
            dbx.files_upload(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
                             TODOS_FILE_PATH, mode=modus)
            return nr
        except dropbox.exceptions.ApiError:
            if versuch:
                raise
    return 0
