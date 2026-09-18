from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "nethome.db"


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schedule_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(schedules)").fetchall()}
    additions = {
        "schedule_type": "TEXT NOT NULL DEFAULT 'weekly'",
        "start_date": "TEXT",
        "end_date": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE schedules ADD COLUMN {name} {definition}")


def init_db() -> None:
    with _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                schedule_type TEXT NOT NULL DEFAULT 'weekly',
                days TEXT NOT NULL DEFAULT '',
                start_date TEXT,
                end_date TEXT,
                time_local TEXT NOT NULL,
                action TEXT NOT NULL DEFAULT 'set',
                mode TEXT,
                temperature REAL,
                fan TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL,
                action TEXT NOT NULL,
                result TEXT NOT NULL,
                details TEXT
            );
            """
        )
        _ensure_schedule_columns(conn)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_schedules() -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM schedules ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def create_schedule(data: dict[str, Any]) -> dict[str, Any]:
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO schedules(
                name, schedule_type, days, start_date, end_date, time_local,
                action, mode, temperature, fan, enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["name"], data.get("schedule_type", "weekly"), data.get("days", ""),
                data.get("start_date"), data.get("end_date"), data["time_local"],
                data.get("action", "set"), data.get("mode"), data.get("temperature"),
                data.get("fan"), 1 if data.get("enabled", True) else 0, now, now,
            ),
        )
        sid = cur.lastrowid
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (sid,)).fetchone()
    return dict(row)


def update_schedule(schedule_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
    allowed = {
        "name", "schedule_type", "days", "start_date", "end_date", "time_local",
        "action", "mode", "temperature", "fan", "enabled",
    }
    fields = []
    values: list[Any] = []
    for key, value in data.items():
        if key not in allowed:
            continue
        if key == "enabled":
            value = 1 if value else 0
        fields.append(f"{key} = ?")
        values.append(value)
    if not fields:
        return get_schedule(schedule_id)
    fields.append("updated_at = ?")
    values.append(_now())
    values.append(schedule_id)
    with _conn() as conn:
        conn.execute(f"UPDATE schedules SET {', '.join(fields)} WHERE id = ?", values)
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    return dict(row) if row else None


def get_schedule(schedule_id: int) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    return dict(row) if row else None


def delete_schedule(schedule_id: int) -> bool:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
    return cur.rowcount > 0


def add_activity(source: str, action: str, result: str, details: str | None = None) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO activity(created_at, source, action, result, details) VALUES (?, ?, ?, ?, ?)",
            (_now(), source, action, result, details),
        )


def list_activity(limit: int = 50) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
