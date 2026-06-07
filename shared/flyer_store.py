import uuid
import dropbox
import dropbox.files

from shared.secrets import load_secrets

_ORDNER = "/Dokumente/Vereinskalender/flyer"
_MAX_BYTES = 8 * 1024 * 1024  # 8 MB

_MAGIC = [
    (b"%PDF",            "pdf"),
    (b"\xff\xd8",        "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
]
_WEBP_MAGIC = b"WEBP"


def _detect_ext(data: bytes) -> str | None:
    for magic, ext in _MAGIC:
        if data[:len(magic)] == magic:
            return ext
    if len(data) >= 12 and data[8:12] == _WEBP_MAGIC:
        return "webp"
    return None


def _dbx() -> dropbox.Dropbox:
    s = load_secrets()
    return dropbox.Dropbox(
        oauth2_refresh_token=s["DROPBOX_REFRESH_TOKEN"],
        app_key=s["DROPBOX_APP_KEY"],
        app_secret=s["DROPBOX_APP_SECRET"],
    )


def upload_flyer(file_bytes: bytes) -> tuple[str, str]:
    """Lädt Flyer nach Dropbox hoch. Gibt (flyer_url, flyer_path) zurück."""
    if len(file_bytes) > _MAX_BYTES:
        raise ValueError("Datei zu groß (max. 8 MB).")
    ext = _detect_ext(file_bytes)
    if not ext:
        raise ValueError("Ungültiges Format. Erlaubt: PDF, JPG, PNG, WebP.")

    pfad = f"{_ORDNER}/{uuid.uuid4().hex}.{ext}"
    dbx = _dbx()
    dbx.files_upload(file_bytes, pfad, mode=dropbox.files.WriteMode.overwrite)
    link = dbx.sharing_create_shared_link_with_settings(pfad)
    url = link.url.replace("?dl=0", "?raw=1")
    return url, pfad


def delete_flyer(dropbox_path: str) -> None:
    try:
        _dbx().files_delete_v2(dropbox_path)
    except Exception:
        pass
