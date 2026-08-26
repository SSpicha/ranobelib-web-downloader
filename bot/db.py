"""
SQLite persistence helper for Telegram bot user settings and state.
"""
import sqlite3
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional

log = logging.getLogger("ranobelib_bot_db")

DB_DIR = Path(__file__).resolve().parent.parent / "user_data"
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "bot_users.db"


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they do not exist."""
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                lang TEXT DEFAULT 'uk',
                fmt TEXT DEFAULT 'epub',
                device TEXT DEFAULT 'generic',
                images_mode TEXT DEFAULT 'images',
                updated_at REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_states (
                user_id INTEGER PRIMARY KEY,
                state_json TEXT,
                updated_at REAL
            )
        """)
        conn.commit()
    log.info("SQLite database initialized at %s", DB_PATH)


def get_user_settings(user_id: int) -> Dict[str, Any]:
    with _get_conn() as conn:
        row = conn.execute("SELECT lang, fmt, device, images_mode FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
        if row:
            return dict(row)
    return {"lang": "uk", "fmt": "epub", "device": "generic", "images_mode": "images"}


def save_user_settings(user_id: int, settings: Dict[str, Any]):
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO user_settings (user_id, lang, fmt, device, images_mode, updated_at)
            VALUES (?, ?, ?, ?, ?, strftime('%s', 'now'))
            ON CONFLICT(user_id) DO UPDATE SET
                lang = excluded.lang,
                fmt = excluded.fmt,
                device = excluded.device,
                images_mode = excluded.images_mode,
                updated_at = strftime('%s', 'now')
        """, (
            user_id,
            settings.get("lang", "uk"),
            settings.get("fmt", "epub"),
            settings.get("device", "generic"),
            settings.get("images_mode", "images")
        ))
        conn.commit()


def get_user_state(user_id: int) -> Optional[Dict[str, Any]]:
    with _get_conn() as conn:
        row = conn.execute("SELECT state_json FROM user_states WHERE user_id = ?", (user_id,)).fetchone()
        if row and row["state_json"]:
            try:
                return json.loads(row["state_json"])
            except Exception:
                return None
    return None


def save_user_state(user_id: int, state: Optional[Dict[str, Any]]):
    with _get_conn() as conn:
        if state is None:
            conn.execute("DELETE FROM user_states WHERE user_id = ?", (user_id,))
        else:
            # Strip non-serializable objects (like chapters_data) before saving to DB
            st_copy = state.copy()
            st_copy.pop("chapters_data", None)
            st_copy.pop("branches", None)
            conn.execute("""
                INSERT INTO user_states (user_id, state_json, updated_at)
                VALUES (?, ?, strftime('%s', 'now'))
                ON CONFLICT(user_id) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = strftime('%s', 'now')
            """, (user_id, json.dumps(st_copy, ensure_ascii=False)))
        conn.commit()
