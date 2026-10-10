"""Dateiablage der Vereinsdokumente (ADR-029, v1.78) – auf dem Server, nicht in Dropbox.

Anders als die Flyer (`shared/flyer_store.py`, öffentliche Dropbox-Links mit `?raw=1`): Protokolle enthalten Namen
und Beschlüsse und dürfen nur über die Route mit Login- und Vereinsprüfung herauskommen. Deshalb gibt es keinen Link,
und nginx liefert das Verzeichnis nicht aus.

- Ablage flach unter `ORDNER/<uuid>.<endung>`; welcher Verein dazugehört, steht nur in der DB (Key-Übertragung =
  eine UPDATE-Zeile). Originalname nur in der DB.
- Format aus dem Inhalt (Magic Bytes), nie aus Endung oder Browser-Angabe. Office-Dateien sind ZIPs: Word/Excel an
  `[Content_Types].xml` + `word/`/`xl/`, LibreOffice an der Datei `mimetype`.
- Grenzen: `MAX_BYTES` je Datei, `MAX_VEREIN` je Verein. `ORDNER` gehört dem Dienstbenutzer `webhook` (Backup:
  `PKA/SOPs/Server-Backup.md`).
"""
from __future__ import annotations

import io
import re
import uuid
import zipfile
from pathlib import Path

ORDNER = Path("/opt/rename-webhook/vereinsdokumente")
MAX_BYTES = 20 * 1024 * 1024        # 20 MB je Datei (nginx /verein/dokumente: 45m)
MAX_VEREIN = 200 * 1024 * 1024      # 200 MB je Verein

TYPEN = {
    "pdf":  ("PDF", "application/pdf"),
    "jpg":  ("Foto", "image/jpeg"),
    "png":  ("Bild", "image/png"),
    "webp": ("Bild", "image/webp"),
    "docx": ("Word", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "xlsx": ("Excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "odt":  ("LibreOffice Text", "application/vnd.oasis.opendocument.text"),
    "ods":  ("LibreOffice Tabelle", "application/vnd.oasis.opendocument.spreadsheet"),
}
IM_BROWSER = {"pdf", "jpg", "png", "webp"}     # öffnen statt herunterladen
ERLAUBT_TEXT = "PDF, Foto (JPG, PNG, WebP), Word, Excel, LibreOffice"
_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(pdf|jpg|png|webp|docx|xlsx|odt|ods)$")


def erkenne(data: bytes) -> str | None:
    if data[:4] == b"%PDF":
        return "pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                namen = set(z.namelist())
                if "mimetype" in namen:
                    mt = z.read("mimetype")[:100].decode("ascii", "replace").strip()
                    return {"application/vnd.oasis.opendocument.text": "odt",
                            "application/vnd.oasis.opendocument.spreadsheet": "ods"}.get(mt)
                if "[Content_Types].xml" in namen:
                    if any(n.startswith("word/") for n in namen):
                        return "docx"
                    if any(n.startswith("xl/") for n in namen):
                        return "xlsx"
        except (zipfile.BadZipFile, KeyError, OSError):
            return None
    return None


def pruefe(data: bytes, belegt: int = 0, ersetzt: int = 0) -> str:
    """Endung oder ValueError mit deutschem Text. `ersetzt` = Größe einer Datei, die gleich wegfällt."""
    if not data:
        raise ValueError("Bitte eine Datei auswählen.")
    if len(data) > MAX_BYTES:
        raise ValueError(f"Datei zu groß (höchstens {MAX_BYTES // 1024 // 1024} MB).")
    ext = erkenne(data)
    if not ext:
        raise ValueError(f"Dieses Format geht nicht. Erlaubt: {ERLAUBT_TEXT}.")
    if belegt - ersetzt + len(data) > MAX_VEREIN:
        raise ValueError(f"Euer Speicher ist voll (höchstens {MAX_VEREIN // 1024 // 1024} MB je Verein). "
                         "Löscht alte Dokumente, die ihr nicht mehr braucht.")
    return ext


def speichern(data: bytes, ext: str) -> str:
    ORDNER.mkdir(mode=0o750, parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{ext}"
    ziel = ORDNER / name
    tmp = ORDNER / f".{name}.tmp"
    tmp.write_bytes(data)
    tmp.chmod(0o640)
    tmp.replace(ziel)
    return name


def pfad(name: str) -> Path | None:
    """Pfad einer gespeicherten Datei – nur für Namen, die `speichern()` erzeugt hat."""
    if not name or not _NAME_RE.match(name):
        return None
    p = ORDNER / name
    return p if p.is_file() else None


def entfernen(name: str) -> None:
    p = pfad(name)
    if p:
        try:
            p.unlink()
        except OSError:
            pass


def sicherer_name(name: str, ext: str) -> str:
    """Originalname für Anzeige und Download: ohne Pfad und Steuerzeichen, mit der echten Endung."""
    name = re.sub(r"[\x00-\x1f\x7f/\\]", "", (name or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]).strip()[:120]
    stamm = name.rsplit(".", 1)[0] if "." in name else name
    return f"{stamm or 'Dokument'}.{ext}"
