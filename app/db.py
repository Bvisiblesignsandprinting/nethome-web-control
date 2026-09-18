from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "nethome.db"
DATABASE_URL = os.getenv("DATABASE_URL")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _use_postgres() -> bool:
    return bool(DATABASE_URL)


def _sqlite_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _pg_conn():
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


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
    if _use_postgres():
        # Production schema is managed in Supabase.
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("select 1")
        return

    with _sqlite_conn() as conn:
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


def _normalize_row(row: dict[str, Any] | sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for key, value in list(data.items()):
        if hasattr(value, "isoformat"):
            data[key] = value.isoformat()
    return data


def list_schedules() -> list[dict[str, Any]]:
    if _use_postgres():
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute("select * from public.schedules order by id")
            return [_normalize_row(r) for r in cur.fetchall()]

    with _sqlite_conn() as conn:
        rows = conn.execute("SELECT * FROM schedules ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def create_schedule(data: dict[str, Any]) -> dict[str, Any]:
    if _use_postgres():
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into public.schedules(
                    name, schedule_type, days, start_date, end_date, time_local,
                    action, mode, temperature, fan, enabled
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning *
                """,
                (
                    data["name"], data.get("schedule_type", "weekly"), data.get("days", ""),
                    data.get("start_date"), data.get("end_date"), data["time_local"],
                    data.get("action", "set"), data.get("mode"), data.get("temperature"),
                    data.get("fan"), bool(data.get("enabled", True)),
                ),
            )
            return _normalize_row(cur.fetchone())

    now = _now()
    with _sqlite_conn() as conn:
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


def get_schedule(schedule_id: int) -> dict[str, Any] | None:
    if _use_postgres():
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute("select * from public.schedules where id = %s", (schedule_id,))
            return _normalize_row(cur.fetchone())

    with _sqlite_conn() as conn:
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    return dict(row) if row else None


def update_schedule(schedule_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
    allowed = {
        "name", "schedule_type", "days", "start_date", "end_date", "time_local",
        "action", "mode", "temperature", "fan", "enabled",
    }

    if _use_postgres():
        fields = []
        values: list[Any] = []
        for key, value in data.items():
            if key not in allowed:
                continue
            fields.append(f"{key} = %s")
            values.append(value)
        if not fields:
            return get_schedule(schedule_id)
        values.append(schedule_id)
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"update public.schedules set {', '.join(fields)} where id = %s returning *",
                values,
            )
            return _normalize_row(cur.fetchone())

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
    with _sqlite_conn() as conn:
        conn.execute(f"UPDATE schedules SET {', '.join(fields)} WHERE id = ?", values)
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    return dict(row) if row else None


def delete_schedule(schedule_id: int) -> bool:
    if _use_postgres():
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute("delete from public.schedules where id = %s", (schedule_id,))
            return cur.rowcount > 0

    with _sqlite_conn() as conn:
        cur = conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
    return cur.rowcount > 0


def add_activity(source: str, action: str, result: str, details: str | None = None) -> None:
    if _use_postgres():
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into public.activity(source, action, result, details)
                values (%s, %s, %s, %s)
                """,
                (source, action, result, details),
            )
        return

    with _sqlite_conn() as conn:
        conn.execute(
            "INSERT INTO activity(created_at, source, action, result, details) VALUES (?, ?, ?, ?, ?)",
            (_now(), source, action, result, details),
        )


def list_activity(limit: int = 50) -> list[dict[str, Any]]:
    if _use_postgres():
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "select * from public.activity order by id desc limit %s",
                (limit,),
            )
            return [_normalize_row(r) for r in cur.fetchall()]

    with _sqlite_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
