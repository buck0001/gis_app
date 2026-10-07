"""SQLite access layer (thread-safe, no ORM - keeps dependencies minimal)."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "gis.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        columns = {
            row[1] for row in _conn.execute(
                "PRAGMA table_info(processing_jobs)").fetchall()
        }
        if "params_json" not in columns:
            _conn.execute("ALTER TABLE processing_jobs ADD COLUMN params_json TEXT")
        _conn.commit()
    return _conn


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def rows(sql: str, params: tuple = ()) -> list[dict]:
    with _lock:
        cur = get_conn().execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def row(sql: str, params: tuple = ()) -> Optional[dict]:
    with _lock:
        cur = get_conn().execute(sql, params)
        r = cur.fetchone()
        return dict(r) if r else None


def execute(sql: str, params: tuple = ()) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(sql, params)
        conn.commit()


def executemany(sql: str, seq: list[tuple]) -> None:
    with _lock:
        conn = get_conn()
        conn.executemany(sql, seq)
        conn.commit()


def jdump(obj) -> str:
    return json.dumps(obj, default=str)


def jload(text, default=None):
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except Exception:
        return default
