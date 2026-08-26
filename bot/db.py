"""
SQLite persistence helper for Telegram bot user settings, state, subscriptions, and OAuth tokens.
"""
import sqlite3
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

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
                token TEXT DEFAULT '',
                updated_at REAL
            )
        """)
        try:
            conn.execute("ALTER TABLE user_settings ADD COLUMN token TEXT DEFAULT ''")
        except Exception:
            pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_states (
                user_id INTEGER PRIMARY KEY,
                state_json TEXT,
                updated_at REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                slug TEXT,
                title TEXT,
                last_chapter_number REAL DEFAULT 0,
                updated_at REAL,
                UNIQUE(user_id, slug)
            )
        """)
        conn.commit()
    log.info("SQLite database initialized at %s", DB_PATH)


def get_user_settings(user_id: int) -> Dict[str, Any]:
    with _get_conn() as conn:
        row = conn.execute("SELECT lang, fmt, device, images_mode, token FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
        if row:
            return dict(row)
    return {"lang": "uk", "fmt": "epub", "device": "generic", "images_mode": "images", "token": ""}


def save_user_settings(user_id: int, settings: Dict[str, Any]):
    existing = get_user_settings(user_id)
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO user_settings (user_id, lang, fmt, device, images_mode, token, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, strftime('%s', 'now'))
            ON CONFLICT(user_id) DO UPDATE SET
                lang = excluded.lang,
                fmt = excluded.fmt,
                device = excluded.device,
                images_mode = excluded.images_mode,
                token = excluded.token,
                updated_at = strftime('%s', 'now')
        """, (
            user_id,
            settings.get("lang", existing.get("lang", "uk")),
            settings.get("fmt", existing.get("fmt", "epub")),
            settings.get("device", existing.get("device", "generic")),
            settings.get("images_mode", existing.get("images_mode", "images")),
            settings.get("token", existing.get("token", "")),
        ))
        conn.commit()


def set_user_token(user_id: int, token: str):
    st = get_user_settings(user_id)
    st["token"] = (token or "").strip()
    save_user_settings(user_id, st)


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
            st_copy = state.copy()
            st_copy.pop("chapters_data", None)
            st_copy.pop("branches", None)
            st_copy.pop("novel_info", None)
            conn.execute("""
                INSERT INTO user_states (user_id, state_json, updated_at)
                VALUES (?, ?, strftime('%s', 'now'))
                ON CONFLICT(user_id) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = strftime('%s', 'now')
            """, (user_id, json.dumps(st_copy, ensure_ascii=False)))
        conn.commit()


# --- Subscription Tracker Functions ---

def add_subscription(user_id: int, slug: str, title: str, last_ch: float = 0) -> bool:
    with _get_conn() as conn:
        try:
            conn.execute("""
                INSERT INTO subscriptions (user_id, slug, title, last_chapter_number, updated_at)
                VALUES (?, ?, ?, ?, strftime('%s', 'now'))
                ON CONFLICT(user_id, slug) DO UPDATE SET
                    title = excluded.title,
                    updated_at = strftime('%s', 'now')
            """, (user_id, slug, title, last_ch))
            conn.commit()
            return True
        except Exception as e:
            log.error("Failed to add subscription: %s", e)
            return False


def remove_subscription(user_id: int, slug: str) -> bool:
    with _get_conn() as conn:
        conn.execute("DELETE FROM subscriptions WHERE user_id = ? AND slug = ?", (user_id, slug))
        conn.commit()
        return True


def get_user_subscriptions(user_id: int) -> List[Dict[str, Any]]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT slug, title, last_chapter_number, updated_at FROM subscriptions WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_subscriptions() -> List[Dict[str, Any]]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT user_id, slug, title, last_chapter_number FROM subscriptions"
        ).fetchall()
        return [dict(r) for r in rows]


def update_subscription_ch(user_id: int, slug: str, last_ch: float):
    with _get_conn() as conn:
        conn.execute("""
            UPDATE subscriptions SET last_chapter_number = ?, updated_at = strftime('%s', 'now')
            WHERE user_id = ? AND slug = ?
        """, (last_ch, user_id, slug))
        conn.commit()
