import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

DB_FILE = Path("/opt/rename-webhook/vk_accounts.db")
SESSION_TIMEOUT_HOURS = 8
# Fassung von Datenschutzerklärung + Nutzungsbedingungen (v1.79). Bei wesentlicher Textänderung hochzählen –
# dann bestätigt jedes Konto beim nächsten Aufruf des Vereinsbereichs neu (`/verein/bestaetigen`).
DS_FASSUNG = "2026-10.2"   # .2 = v1.83: Server-Logs, Anthropic, Telegram, Zugriffsprotokoll, AVV ergänzt
# Fassung des AV-Vertrags für den Dokumentenbereich (v1.83, ADR-032). Neue Fassung ⇒ Bestand bleibt lesbar,
# Anlegen/Ändern erst nach erneutem Abschluss durch einen Vereinsadmin.
AVV_FASSUNG = "2026-10"


@contextmanager
def db_conn():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    # Migration: UNIQUE-Constraint auf email entfernen (eine E-Mail → mehrere Vereine)
    with db_conn() as conn:
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='vk_users'"
        ).fetchone()
        if schema and "UNIQUE NOT NULL" in schema["sql"] and "email" in schema["sql"]:
            existing_cols = [r["name"] for r in conn.execute("PRAGMA table_info(vk_users)").fetchall()]
            name_sel   = "name"    if "name"    in existing_cols else "NULL"
            telefon_sel = "telefon" if "telefon" in existing_cols else "NULL"
            conn.executescript(f"""
                PRAGMA foreign_keys=OFF;
                CREATE TABLE vk_users_new (
                    id                    INTEGER PRIMARY KEY,
                    email                 TEXT NOT NULL,
                    password_hash         TEXT NOT NULL,
                    verein_id             INTEGER NOT NULL REFERENCES vereine_accounts(id),
                    role                  TEXT NOT NULL DEFAULT 'admin',
                    aktiv                 BOOLEAN DEFAULT 1,
                    created_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
                    einladungs_token      TEXT,
                    einladungs_expires    DATETIME,
                    reset_token           TEXT,
                    reset_token_expires   DATETIME,
                    email_verified        BOOLEAN DEFAULT 0,
                    verify_token          TEXT,
                    verify_token_expires  DATETIME,
                    totp_secret           TEXT,
                    totp_recovery_hashes  TEXT,
                    login_attempts        INTEGER DEFAULT 0,
                    locked_until          DATETIME,
                    name                  TEXT,
                    telefon               TEXT
                );
                INSERT INTO vk_users_new
                    SELECT id, email, password_hash, verein_id, role, aktiv, created_at,
                           einladungs_token, einladungs_expires, reset_token, reset_token_expires,
                           email_verified, verify_token, verify_token_expires, totp_secret,
                           totp_recovery_hashes, login_attempts, locked_until,
                           {name_sel}, {telefon_sel}
                    FROM vk_users;
                DROP TABLE vk_users;
                ALTER TABLE vk_users_new RENAME TO vk_users;
                PRAGMA foreign_keys=ON;
            """)

    with db_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS vereine_accounts (
                id                  INTEGER PRIMARY KEY,
                verein_key          TEXT UNIQUE,
                verein_name         TEXT NOT NULL,
                status              TEXT NOT NULL DEFAULT 'pending',
                created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
                freigegeben_at      DATETIME,
                selbstverpflichtung BOOLEAN DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS vk_users (
                id                    INTEGER PRIMARY KEY,
                email                 TEXT NOT NULL,
                password_hash         TEXT NOT NULL,
                verein_id             INTEGER NOT NULL REFERENCES vereine_accounts(id),
                role                  TEXT NOT NULL DEFAULT 'admin',
                aktiv                 BOOLEAN DEFAULT 1,
                created_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
                einladungs_token      TEXT,
                einladungs_expires    DATETIME,
                reset_token           TEXT,
                reset_token_expires   DATETIME,
                email_verified        BOOLEAN DEFAULT 0,
                verify_token          TEXT,
                verify_token_expires  DATETIME,
                totp_secret           TEXT,
                totp_recovery_hashes  TEXT,
                login_attempts        INTEGER DEFAULT 0,
                locked_until          DATETIME,
                name                  TEXT,
                telefon               TEXT
            );

            CREATE TABLE IF NOT EXISTS vk_sessions (
                id           TEXT PRIMARY KEY,
                user_id      INTEGER NOT NULL REFERENCES vk_users(id),
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_active  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS vk_audit (
                id          INTEGER PRIMARY KEY,
                aktion      TEXT NOT NULL,
                termin_id   TEXT NOT NULL,
                verein_key  TEXT NOT NULL,
                user_id     INTEGER REFERENCES vk_users(id),
                timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP,
                anzahl      INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS upload_quota (
                verein_id INTEGER NOT NULL,
                datum     TEXT    NOT NULL,
                count     INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (verein_id, datum)
            );

            CREATE TABLE IF NOT EXISTS tg_subscriptions (
                chat_id    TEXT NOT NULL,
                verein_key TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, verein_key)
            );

            CREATE TABLE IF NOT EXISTS page_stats (
                datum           TEXT PRIMARY KEY,
                views           INTEGER NOT NULL DEFAULT 0,
                unique_visitors INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS ical_feed_requests (
                date    TEXT NOT NULL,
                ip_hash TEXT NOT NULL,
                PRIMARY KEY (date, ip_hash)
            );

            CREATE TABLE IF NOT EXISTS ical_feed_vereine (
                date       TEXT NOT NULL,
                ip_hash    TEXT NOT NULL,
                verein_key TEXT NOT NULL,
                PRIMARY KEY (date, ip_hash, verein_key)
            );

            CREATE TABLE IF NOT EXISTS page_stats_hourly (
                datum  TEXT    NOT NULL,
                stunde INTEGER NOT NULL,
                views  INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (datum, stunde)
            );

            CREATE TABLE IF NOT EXISTS page_stats_geo (
                datum    TEXT    NOT NULL,
                land     TEXT    NOT NULL,
                stadt    TEXT    NOT NULL DEFAULT '',
                besucher INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (datum, land, stadt)
            );
        """)
        # Migrations: neue Spalten (scheitern lautlos wenn bereits vorhanden)
        for col_sql in [
            "ALTER TABLE vk_users ADD COLUMN name TEXT",
            "ALTER TABLE vk_users ADD COLUMN telefon TEXT",
            "ALTER TABLE vereine_accounts ADD COLUMN rubrik TEXT NOT NULL DEFAULT 'Verein'",
            "ALTER TABLE vereine_accounts ADD COLUMN heimatort TEXT",
            "ALTER TABLE vereine_accounts ADD COLUMN plz TEXT",
            "ALTER TABLE vereine_accounts ADD COLUMN gemeinde TEXT",
            "ALTER TABLE vereine_accounts ADD COLUMN landkreis TEXT",
            "ALTER TABLE vk_audit ADD COLUMN anzahl INTEGER NOT NULL DEFAULT 1",
            # Ansprechpartner + E-Mail-Wechsel mit Bestätigung (2026-10-02)
            "ALTER TABLE vk_users ADD COLUMN anrede TEXT",
            "ALTER TABLE vk_users ADD COLUMN vorname TEXT",
            "ALTER TABLE vk_users ADD COLUMN nachname TEXT",
            "ALTER TABLE vk_users ADD COLUMN email_neu TEXT",
            "ALTER TABLE vk_users ADD COLUMN email_neu_token TEXT",
            "ALTER TABLE vk_users ADD COLUMN email_neu_expires DATETIME",
            # Kenntnisnahme Datenschutz/Nutzungsbedingungen je Fassung (v1.79), Verlauf zusätzlich in vk_audit
            "ALTER TABLE vk_users ADD COLUMN ds_fassung TEXT",
            "ALTER TABLE vk_users ADD COLUMN ds_bestaetigt_am DATETIME",
            # Dokumentenbereich per AV-Vertrag freischalten (v1.83) + einmaliger Hinweis für Admins
            "ALTER TABLE vereine_accounts ADD COLUMN avv_fassung TEXT",
            "ALTER TABLE vereine_accounts ADD COLUMN avv_am DATETIME",
            "ALTER TABLE vereine_accounts ADD COLUMN avv_user INTEGER",
            "ALTER TABLE vk_users ADD COLUMN hinweis_dokumente INTEGER NOT NULL DEFAULT 0",
            # Vorstand (v1.83): sieht zusätzlich Dokumente „nur Vorstand“; bewusst keine neue role
            "ALTER TABLE vk_users ADD COLUMN vorstand INTEGER NOT NULL DEFAULT 0",
            # 2FA: zuletzt benutzter TOTP-Zeitschritt – jeder Code gilt nur einmal (Review 2026-10-10)
            "ALTER TABLE vk_users ADD COLUMN totp_letzt INTEGER",
        ]:
            try:
                conn.execute(col_sql)
            except Exception:
                pass
        # Entwürfe, Planungsrunden, Prüfkreis (ADR-026, v1.77)
        from shared.planung_db import SCHEMA as _PLANUNG_SCHEMA
        conn.executescript(_PLANUNG_SCHEMA)
        # Dokumente der Vereine (ADR-029, v1.78)
        from shared.dokumente_db import SCHEMA as _DOKUMENTE_SCHEMA
        conn.executescript(_DOKUMENTE_SCHEMA)
        try:   # Sichtbarkeit je Dokument (v1.83) – Tabelle aus v1.78 hat die Spalte noch nicht
            conn.execute("ALTER TABLE dokument ADD COLUMN sichtbar TEXT NOT NULL DEFAULT 'alle'")
        except Exception:
            pass


def create_session(user_id: int) -> str:
    token = secrets.token_hex(32)
    cutoff = (datetime.utcnow() - timedelta(hours=SESSION_TIMEOUT_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    with db_conn() as conn:
        # Aufräumen: abgelaufene Sessions sind nutzlos und blockieren sonst das Löschen
        # von Benutzern (Fremdschlüssel vk_sessions.user_id)
        conn.execute("DELETE FROM vk_sessions WHERE created_at < ?", (cutoff,))
        conn.execute(
            "INSERT INTO vk_sessions (id, user_id) VALUES (?, ?)",
            (token, user_id),
        )
    return token


def get_session_user(token: str) -> dict | None:
    if not token:
        return None
    # Absolute Laufzeit ab Login (wie das Cookie, max_age = SESSION_TIMEOUT_HOURS) – Aktivität
    # verlängert nicht mehr unbegrenzt. Deaktivierte Benutzer (aktiv=0) haben keine Session.
    cutoff = (datetime.utcnow() - timedelta(hours=SESSION_TIMEOUT_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    with db_conn() as conn:
        row = conn.execute(
            """SELECT u.id, u.email, u.role, u.aktiv,
                      u.email_verified, u.totp_secret, u.ds_fassung, u.hinweis_dokumente, u.vorstand,
                      v.id as verein_id, v.verein_key, v.verein_name, v.status as verein_status, v.avv_fassung
               FROM vk_sessions s
               JOIN vk_users u ON u.id = s.user_id
               JOIN vereine_accounts v ON v.id = u.verein_id
               WHERE s.id = ? AND s.created_at > ? AND u.aktiv = 1""",
            (token, cutoff),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE vk_sessions SET last_active = CURRENT_TIMESTAMP WHERE id = ?",
                (token,),
            )
        return dict(row) if row else None


def delete_session(token: str):
    with db_conn() as conn:
        conn.execute("DELETE FROM vk_sessions WHERE id = ?", (token,))


def delete_user_sessions(user_id: int, ausser: str = "", conn=None) -> None:
    """Alle Sessions eines Benutzers beenden (Passwort neu/geändert, Benutzer entfernt).
    `ausser`: die aktuelle Session behalten (Passwortänderung im eingeloggten Zustand)."""
    def _del(c):
        c.execute("DELETE FROM vk_sessions WHERE user_id = ? AND id != ?", (user_id, ausser))
    if conn is not None:
        _del(conn)
    else:
        with db_conn() as c:
            _del(c)


def log_audit(aktion: str, termin_id: str, verein_key: str, user_id: int, anzahl: int = 1):
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO vk_audit (aktion, termin_id, verein_key, user_id, anzahl) VALUES (?,?,?,?,?)",
            (aktion, termin_id, verein_key, user_id, anzahl),
        )


def get_upload_count(verein_id: int, datum: str) -> int:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT count FROM upload_quota WHERE verein_id=? AND datum=?",
            (verein_id, datum),
        ).fetchone()
        return row["count"] if row else 0


def increment_upload_quota(verein_id: int, datum: str):
    with db_conn() as conn:
        conn.execute(
            """INSERT INTO upload_quota (verein_id, datum, count) VALUES (?,?,1)
               ON CONFLICT(verein_id, datum) DO UPDATE SET count = count + 1""",
            (verein_id, datum),
        )


def tg_subscribe(chat_id: str, verein_key: str) -> bool:
    with db_conn() as conn:
        existing = conn.execute(
            "SELECT 1 FROM tg_subscriptions WHERE chat_id=? AND verein_key=?",
            (chat_id, verein_key)
        ).fetchone()
        if existing:
            return False
        conn.execute(
            "INSERT INTO tg_subscriptions (chat_id, verein_key) VALUES (?,?)",
            (chat_id, verein_key)
        )
        return True


def tg_unsubscribe(chat_id: str, verein_key: str) -> bool:
    with db_conn() as conn:
        result = conn.execute(
            "DELETE FROM tg_subscriptions WHERE chat_id=? AND verein_key=?",
            (chat_id, verein_key)
        )
        return result.rowcount > 0


def tg_unsubscribe_all(chat_id: str) -> int:
    with db_conn() as conn:
        result = conn.execute(
            "DELETE FROM tg_subscriptions WHERE chat_id=?", (chat_id,)
        )
        return result.rowcount


def tg_get_subscriptions(chat_id: str) -> list[str]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT verein_key FROM tg_subscriptions WHERE chat_id=? ORDER BY verein_key",
            (chat_id,)
        ).fetchall()
        return [r["verein_key"] for r in rows]


def tg_get_subscribers_for_verein(verein_key: str) -> list[str]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT chat_id FROM tg_subscriptions WHERE verein_key=?", (verein_key,)
        ).fetchall()
        return [r["chat_id"] for r in rows]


def tg_get_all_subscriptions() -> list[dict]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT chat_id, verein_key FROM tg_subscriptions"
        ).fetchall()
        return [dict(r) for r in rows]


def get_page_stats(from_date: str, to_date: str) -> list[dict]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT datum, views, unique_visitors FROM page_stats "
            "WHERE datum >= ? AND datum <= ? ORDER BY datum",
            (from_date, to_date),
        ).fetchall()
        return [dict(r) for r in rows]
