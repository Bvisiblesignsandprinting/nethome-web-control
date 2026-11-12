from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "nethome.db"
DATABASE_URL = os.getenv("DATABASE_URL")
_PG_POOL = None
_PG_POOL_LOCK = threading.Lock()
_LOCAL_CLOUD_LOCK = threading.RLock()
_CLOUD_LOCK_KEY = 151732606
_SENSOR_LOCK_KEY = 151732607
_LOCAL_SENSOR_LOCK = threading.RLock()
_LOCAL_SMART_LOCK = threading.RLock()
_LOCAL_PLUG_LOCK = threading.RLock()


@contextmanager
def plug_operation_lock():
    """Serialize plug assignment and explicit writes across workers."""
    if _use_postgres():
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute("set local lock_timeout = '25s'")
            cur.execute('select pg_advisory_xact_lock(%s)', (151732609,))
            yield
    else:
        with _LOCAL_PLUG_LOCK:
            yield


@contextmanager
def smart_operation_lock():
    """Serialize thermostat decisions; Midea has a separate transport lock."""
    if _use_postgres():
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute("set local lock_timeout = '25s'")
            cur.execute('select pg_advisory_xact_lock(%s)', (151732608,))
            yield
    else:
        with _LOCAL_SMART_LOCK:
            yield


@contextmanager
def sensor_operation_lock():
    """Serialize sensor refreshes across serverless instances, independently of AC writes."""
    if _use_postgres():
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute("set local lock_timeout = '25s'")
            cur.execute("select pg_advisory_xact_lock(%s)", (_SENSOR_LOCK_KEY,))
            yield
    else:
        with _LOCAL_SENSOR_LOCK:
            yield


def load_sensor_cache() -> dict[str, Any]:
    if _use_postgres():
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute("select details from public.activity where source = 'tuya-cache' and action = 'sensor' order by id desc limit 1")
            row = cur.fetchone()
    else:
        with _sqlite_conn() as conn:
            row = conn.execute("select details from activity where source = 'tuya-cache' and action = 'sensor' order by id desc limit 1").fetchone()
    if not row:
        return {}
    data = json.loads(row['details'])
    return data if isinstance(data, dict) else {}


def save_sensor_cache(data: dict[str, Any]) -> None:
    _save_current_record('tuya-cache', 'sensor', 'active',
                         json.dumps(data, separators=(",", ":"), sort_keys=True))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_sharing_record(action: str) -> str | None:
    if action not in {'session', 'pending', 'plug-config', 'plug-cache', 'plug-discovery'}:
        raise ValueError('Invalid sharing record')
    if _use_postgres():
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute("select details from public.activity where source='tuya-sharing' and action=%s order by id desc limit 1", (action,))
            row = cur.fetchone()
    else:
        with _sqlite_conn() as conn:
            row = conn.execute("select details from activity where source='tuya-sharing' and action=? order by id desc limit 1", (action,)).fetchone()
    return row['details'] if row else None


def save_sharing_record(action: str, encrypted: str) -> None:
    if action not in {'session', 'pending', 'plug-config', 'plug-cache', 'plug-discovery'}:
        raise ValueError('Invalid sharing record')
    _save_current_record('tuya-sharing', action, 'active', encrypted)


def sharing_record_exists(action: str) -> bool:
    """Check the newest record without transferring its encrypted credential."""
    if action not in {'session', 'pending', 'plug-config', 'plug-cache', 'plug-discovery'}:
        raise ValueError('Invalid sharing record')
    if _use_postgres():
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute("select coalesce((select details<>'' from public.activity "
                        "where source='tuya-sharing' and action=%s order by id desc limit 1),false) as present", (action,))
            return bool(cur.fetchone()['present'])
    with _sqlite_conn() as conn:
        row = conn.execute("select coalesce((select details<>'' from activity "
                           "where source='tuya-sharing' and action=? order by id desc limit 1),0) as present", (action,)).fetchone()
        return bool(row['present'])


def _use_postgres() -> bool:
    return bool(DATABASE_URL)


def _sqlite_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _get_pg_pool():
    # Warm serverless workers reuse TLS connections; idle workers keep none.
    # No automatic query retries: an uncertain device/config write is never replayed.
    global _PG_POOL
    if _PG_POOL is None:
        with _PG_POOL_LOCK:
            if _PG_POOL is None:
                from psycopg.rows import dict_row
                from psycopg_pool import ConnectionPool
                _PG_POOL = ConnectionPool(
                    conninfo=DATABASE_URL, name="nethome", min_size=0, max_size=6,
                    timeout=10, max_idle=60, max_lifetime=300, reconnect_timeout=30,
                    kwargs={"row_factory": dict_row, "connect_timeout": 8,
                            "prepare_threshold": None},
                )
    return _PG_POOL


def _pg_conn():
    return _get_pg_pool().connection()


@contextmanager
def cloud_operation_lock():
    """Serialize NetHome/Midea calls across instances with a transaction lock."""
    if _use_postgres():
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute("set local lock_timeout = '25s'")
            cur.execute("select pg_advisory_xact_lock(%s)", (_CLOUD_LOCK_KEY,))
            yield
        return
    with _LOCAL_CLOUD_LOCK:
        yield


def _save_current_record(source: str, action: str, result: str, payload: str, *, heartbeat: bool = False, match_result: bool = False) -> None:
    """Update one current snapshot, keeping existing history and private records.

    Row locks prevent concurrent updates to an existing snapshot. A first-write
    race can create two snapshots, as before; readers always select the latest.
    No-op saves produce no UPDATE/WAL. All values remain parameterized.
    """
    if _use_postgres():
        with _pg_conn() as conn, conn.cursor() as cur:
            condition = " and result=%s" if match_result else ""
            parameters = (source, action, result) if match_result else (source, action)
            cur.execute("select id, (result is distinct from %s or details is distinct from %s) as changed from public.activity "
                        "where source=%s and action=%s" + condition + " order by id desc limit 1 for update",
                        (result, payload, *parameters))
            row = cur.fetchone()
            if row:
                if heartbeat or row['changed']:
                    cur.execute("update public.activity set result=%s, details=%s, created_at=now() where id=%s",
                                (result, payload, row['id']))
            else:
                cur.execute("insert into public.activity(source, action, result, details) values (%s,%s,%s,%s)",
                            (source, action, result, payload))
    else:
        with _sqlite_conn() as conn:
            condition = " and result=?" if match_result else ""
            parameters = (source, action, result) if match_result else (source, action)
            row = conn.execute("select id, result, details from activity where source=? and action=?" + condition + " order by id desc limit 1",
                               parameters).fetchone()
            if row:
                if heartbeat or row['result'] != result or row['details'] != payload:
                    conn.execute("update activity set result=?, details=?, created_at=? where id=?",
                                 (result, payload, _now(), row['id']))
            else:
                conn.execute("insert into activity(created_at,source,action,result,details) values (?,?,?,?,?)",
                             (_now(), source, action, result, payload))


def load_cloud_session() -> dict[str, Any] | None:
    """Load the most recent server-side Midea auth session.

    The row is intentionally excluded from the user-facing activity feed.
    """
    if _use_postgres():
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select result, details
                from public.activity
                where source = 'cloud-auth' and action = 'session'
                order by id desc
                limit 1
                """
            )
            row = cur.fetchone()
            row = _normalize_row(row)
    else:
        with _sqlite_conn() as conn:
            raw = conn.execute(
                """
                select result, details
                from activity
                where source = 'cloud-auth' and action = 'session'
                order by id desc
                limit 1
                """
            ).fetchone()
        row = dict(raw) if raw else None

    if not row or row.get("result") != "active":
        return None
    try:
        data = json.loads(row.get("details") or "{}")
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def save_cloud_session(data: dict[str, Any]) -> None:
    _save_current_record('cloud-auth', 'session', 'active',
                         json.dumps(data, separators=(",", ":"), sort_keys=True))


def clear_cloud_session() -> None:
    if _use_postgres():
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "delete from public.activity where source = 'cloud-auth' and action = 'session'"
            )
        return

    with _sqlite_conn() as conn:
        conn.execute(
            "delete from activity where source = 'cloud-auth' and action = 'session'"
        )


def load_sms_config() -> dict[str, Any]:
    """Load private SMS bridge configuration from the existing activity store."""
    if _use_postgres():
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select details
                from public.activity
                where source = 'sms-config' and action = 'settings' and result = 'active'
                order by id desc
                limit 1
                """
            )
            row = _normalize_row(cur.fetchone())
    else:
        with _sqlite_conn() as conn:
            raw = conn.execute(
                """
                select details
                from activity
                where source = 'sms-config' and action = 'settings' and result = 'active'
                order by id desc
                limit 1
                """
            ).fetchone()
        row = dict(raw) if raw else None

    if not row:
        return {}
    try:
        value = json.loads(row.get("details") or "{}")
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def save_sms_config(data: dict[str, Any]) -> None:
    _save_current_record('sms-config', 'settings', 'active',
                         json.dumps(data, separators=(",", ":"), sort_keys=True))


def load_email_bridge_config() -> dict[str, Any]:
    """Load private temporary email-bridge settings."""
    if _use_postgres():
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select details
                from public.activity
                where source = 'email-bridge-config' and action = 'settings' and result = 'active'
                order by id desc
                limit 1
                """
            )
            row = _normalize_row(cur.fetchone())
    else:
        with _sqlite_conn() as conn:
            raw = conn.execute(
                """
                select details
                from activity
                where source = 'email-bridge-config' and action = 'settings' and result = 'active'
                order by id desc
                limit 1
                """
            ).fetchone()
        row = dict(raw) if raw else None

    if not row:
        return {}
    try:
        value = json.loads(row.get("details") or "{}")
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def save_email_bridge_config(data: dict[str, Any]) -> None:
    _save_current_record('email-bridge-config', 'settings', 'active',
                         json.dumps(data, separators=(",", ":"), sort_keys=True))


def load_smart_control_config() -> dict[str, Any]:
    defaults = {
        "enabled": False,
        "target_temperature_f": 73.0,
        "sensor_calibration_f": 0.0,
        "deadband_f": 1.0,
        "min_command_interval_minutes": 5,
        "preferred_mode": "auto",
        "preset_sudah_f": 74.0,
        "preset_all_day_f": 73.0,
        "preset_sleeping_f": 72.0,
        "active_preset": "all_day",
        "comfort_since": None,
        "outside_temperature_f": None,
        "outside_checked_at": None,
        "last_command_at": None,
    }
    if _use_postgres():
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select details
                from public.activity
                where source = 'smart-control-config' and action = 'settings' and result = 'active'
                order by id desc
                limit 1
                """
            )
            row = _normalize_row(cur.fetchone())
    else:
        with _sqlite_conn() as conn:
            raw = conn.execute(
                """
                select details
                from activity
                where source = 'smart-control-config' and action = 'settings' and result = 'active'
                order by id desc
                limit 1
                """
            ).fetchone()
        row = dict(raw) if raw else None
    if row:
        try:
            value = json.loads(row.get("details") or "{}")
            if isinstance(value, dict):
                defaults.update(value)
        except Exception:
            pass
    # Smart Room owns direction selection. Old panel/SMS mode pins must not
    # prevent it cooling a hot room or heating a cold room.
    defaults['preferred_mode'] = 'auto'
    return defaults


def save_smart_control_config(data: dict[str, Any]) -> dict[str, Any]:
    current = load_smart_control_config()
    current.update(data)
    current['preferred_mode'] = 'auto'
    _save_current_record('smart-control-config', 'settings', 'active',
                         json.dumps(current, separators=(",", ":"), sort_keys=True))
    return current


def _ensure_schedule_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(schedules)").fetchall()}
    additions = {
        "schedule_type": "TEXT NOT NULL DEFAULT 'weekly'",
        "start_date": "TEXT",
        "end_date": "TEXT",
        "timezone": "TEXT NOT NULL DEFAULT 'America/New_York'",
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
                timezone TEXT NOT NULL DEFAULT 'America/New_York',
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

            CREATE TABLE IF NOT EXISTS schedule_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_id INTEGER,
                scheduled_for TEXT NOT NULL,
                executed_at TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                command TEXT NOT NULL DEFAULT '{}',
                result TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(schedule_id) REFERENCES schedules(id) ON DELETE SET NULL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS schedule_executions_once_idx
                ON schedule_executions(schedule_id, scheduled_for);
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
        elif value.__class__.__name__ == "Decimal":
            data[key] = float(value)
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
    # Polling snapshots are current state, not an ever-growing event stream.
    # User commands, schedule executions, warnings and errors retain their history.
    if action == 'device_state' or (source == 'tuya' and action == 'indoor_sensor' and result == 'success'):
        _save_current_record(source, action, result, details or '', heartbeat=True,
                             match_result=action == 'indoor_sensor')
        return
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
                "select * from public.activity where source not in ('cloud-auth', 'sms-config', 'email-bridge-config', 'google-voice-config', 'lighting-config', 'tuya-cache', 'tuya-sharing') order by id desc limit %s",
                (limit,),
            )
            return [_normalize_row(r) for r in cur.fetchall()]

    with _sqlite_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM activity WHERE source NOT IN ('cloud-auth', 'sms-config', 'email-bridge-config', 'lighting-config', 'tuya-cache', 'tuya-sharing') ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def claim_schedule_execution(
    schedule_id: int,
    scheduled_for: datetime,
    command: dict[str, Any],
) -> dict[str, Any] | None:
    """Claim one scheduled minute exactly once. Returns None if already claimed."""
    if _use_postgres():
        from psycopg.types.json import Jsonb

        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into public.schedule_executions(
                    schedule_id, scheduled_for, status, command
                ) values (%s, %s, 'pending', %s)
                on conflict (schedule_id, scheduled_for) do nothing
                returning *
                """,
                (schedule_id, scheduled_for, Jsonb(command)),
            )
            return _normalize_row(cur.fetchone())

    created_at = _now()
    try:
        with _sqlite_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO schedule_executions(
                    schedule_id, scheduled_for, status, command, created_at
                ) VALUES (?, ?, 'pending', ?, ?)
                """,
                (
                    schedule_id,
                    scheduled_for.isoformat(),
                    json.dumps(command),
                    created_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM schedule_executions WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
        return dict(row)
    except sqlite3.IntegrityError:
        return None


def finish_schedule_execution(
    execution_id: int,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    executed_at = datetime.now(timezone.utc)

    if _use_postgres():
        from psycopg.types.json import Jsonb

        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update public.schedule_executions
                set status = %s,
                    executed_at = %s,
                    result = %s,
                    error = %s
                where id = %s
                """,
                (
                    status,
                    executed_at,
                    Jsonb(result) if result is not None else None,
                    error,
                    execution_id,
                ),
            )
        return

    with _sqlite_conn() as conn:
        conn.execute(
            """
            UPDATE schedule_executions
            SET status = ?, executed_at = ?, result = ?, error = ?
            WHERE id = ?
            """,
            (
                status,
                executed_at.isoformat(),
                json.dumps(result) if result is not None else None,
                error,
                execution_id,
            ),
        )


def enqueue_device_command_at(source: str, command: dict[str, Any], scheduled_for: datetime) -> dict[str, Any]:
    """Queue an AC command for execution at or after a UTC timestamp."""
    scheduled_for = scheduled_for.astimezone(timezone.utc)
    if _use_postgres():
        from psycopg.types.json import Jsonb
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into public.schedule_executions(
                    schedule_id, scheduled_for, status, command
                ) values (null, %s, 'pending', %s)
                returning *
                """,
                (scheduled_for, Jsonb(command)),
            )
            row = _normalize_row(cur.fetchone())
        add_activity(source, "device_command_queued", "pending", f"execution_id={row['id']} command={command}")
        return row

    created_at = _now()
    with _sqlite_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO schedule_executions(
                schedule_id, scheduled_for, status, command, created_at
            ) VALUES (NULL, ?, 'pending', ?, ?)
            """,
            (scheduled_for.isoformat(), json.dumps(command), created_at),
        )
        row = conn.execute(
            "SELECT * FROM schedule_executions WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
    result = dict(row)
    add_activity(source, "device_command_queued", "pending", f"execution_id={result['id']} command={command}")
    return result


def enqueue_device_command(source: str, command: dict[str, Any]) -> dict[str, Any]:
    """Queue an immediate AC command."""
    return enqueue_device_command_at(source, command, datetime.now(timezone.utc))


def claim_due_queued_execution(now_utc: datetime) -> dict[str, Any] | None:
    """Atomically claim the oldest due ad-hoc command (schedule_id is null)."""
    now_utc = now_utc.astimezone(timezone.utc)
    executed_at = now_utc
    if _use_postgres():
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select id
                from public.schedule_executions
                where schedule_id is null
                  and status = 'pending'
                  and scheduled_for <= %s
                  and command ? '_retry_count'
                order by scheduled_for, id
                for update skip locked
                limit 1
                """,
                (now_utc,),
            )
            row = cur.fetchone()
            if not row:
                return None
            execution_id = int(row["id"] if isinstance(row, dict) else row[0])
            cur.execute(
                """
                update public.schedule_executions
                set status = 'running', executed_at = %s
                where id = %s
                returning *
                """,
                (executed_at, execution_id),
            )
            return _normalize_row(cur.fetchone())

    with _sqlite_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM schedule_executions
            WHERE schedule_id IS NULL
              AND status = 'pending'
              AND scheduled_for <= ?
              AND command LIKE '%"_retry_count"%'
            ORDER BY scheduled_for, id
            LIMIT 1
            """,
            (now_utc.isoformat(),),
        ).fetchone()
        if not row:
            return None
        execution_id = int(row["id"])
        conn.execute(
            "UPDATE schedule_executions SET status = 'running', executed_at = ? WHERE id = ? AND status = 'pending'",
            (executed_at.isoformat(), execution_id),
        )
        claimed = conn.execute("SELECT * FROM schedule_executions WHERE id = ?", (execution_id,)).fetchone()
    return dict(claimed) if claimed else None


def claim_next_execution() -> dict[str, Any] | None:
    """Atomically claim the oldest pending command for the local worker."""
    if _use_postgres():
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select *
                from public.schedule_executions
                where status = 'pending'
                order by scheduled_for, id
                limit 1
                """
            )
            return _normalize_row(cur.fetchone())

    with _sqlite_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM schedule_executions
            WHERE status = 'pending'
            ORDER BY scheduled_for, id
            LIMIT 1
            """
        ).fetchone()
    return dict(row) if row else None


def save_device_state(
    state: dict[str, Any] | None,
    online: bool,
    error: str | None = None,
    source: str = "cloud",
) -> None:
    details = json.dumps(state or {}, separators=(",", ":"))
    add_activity(
        source,
        "device_state",
        "online" if online else "offline",
        details if online else json.dumps({"state": state or {}, "error": error}, separators=(",", ":")),
    )


def get_latest_device_state() -> dict[str, Any] | None:
    if _use_postgres():
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select created_at, result, details
                from public.activity
                where source in ('cloud', 'worker') and action = 'device_state'
                order by created_at desc, id desc
                limit 1
                """
            )
            row = cur.fetchone()
            row = _normalize_row(row)
    else:
        with _sqlite_conn() as conn:
            raw = conn.execute(
                """
                SELECT created_at, result, details
                FROM activity
                WHERE source IN ('cloud', 'worker') AND action = 'device_state'
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        row = dict(raw) if raw else None

    if not row:
        return None
    try:
        payload = json.loads(row.get("details") or "{}")
    except Exception:
        payload = {}
    online = row.get("result") == "online"
    if online:
        state = payload
        error = None
    else:
        state = payload.get("state") if isinstance(payload, dict) else {}
        error = payload.get("error") if isinstance(payload, dict) else None
    return {
        "updated_at": row.get("created_at"),
        "online": online,
        "state": state or {},
        "error": error,
    }


