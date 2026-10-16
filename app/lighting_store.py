"""Atomic lighting drafts in the existing private server-side configuration store."""
from __future__ import annotations

import json
from contextlib import closing
from copy import deepcopy

from fastapi import HTTPException

from . import db

SOURCE = "lighting-config"
LOCK_KEY = 151732607
ZONE_DEFINITIONS = (
    {"id": "rgb-walls", "name": "Walls", "kind": "rgbw", "description": "RGB lights on the walls"},
    {"id": "rgb-waterfall", "name": "Waterfall", "kind": "rgbw", "description": "RGB lights at the waterfall"},
    {"id": "regular-walls-roof", "name": "Walls + Roof", "kind": "switch", "description": "Regular LED strips · one group for now"},
    {"id": "chandeliers", "name": "Chandeliers", "kind": "switch", "description": "Regular LED bulbs · on/off only"},
)


def defaults():
    return {"version": 1, "revision": 0, "zones": {
        z["id"]: ({"power": False, "brightness": 80, "color": "#45A5FF", "effect": {
            "mode": "solid", "colors": [], "cycle_seconds": 12, "transition": 100,
            "red_balance": 140, "green_balance": 100, "blue_balance": 100,
        }}
                  if z["kind"] == "rgbw" else {"power": False})
        for z in ZONE_DEFINITIONS
    }, "schedules": []}


def _decode(row):
    if not row:
        return defaults()
    try:
        value = json.loads(row["details"])
        assert value["version"] == 1 and type(value["revision"]) is int
        assert isinstance(value["zones"], dict) and isinstance(value["schedules"], list)
        assert set(value["zones"]) == {z["id"] for z in ZONE_DEFINITIONS}
        # Older saved lighting drafts predate effects. Enrich them in memory so
        # website and panel clients receive one stable shape without a migration.
        effect_default = defaults()["zones"]["rgb-walls"]["effect"]
        for zone in ZONE_DEFINITIONS:
            if zone["kind"] == "rgbw":
                value["zones"][zone["id"]].setdefault("effect", deepcopy(effect_default))
        return value
    except (ValueError, TypeError, KeyError, AssertionError):
        raise HTTPException(503, "Saved lighting settings need repair; no changes were made.")


def document(mutate=None, *, revision=None, action="settings"):
    """Read a snapshot, or compare-and-update it under one database transaction."""
    pg = db._use_postgres()
    table = "public.activity" if pg else "activity"
    with (db._pg_conn() if pg else closing(db._sqlite_conn())) as conn:
        with conn.transaction() if pg else conn:
            with conn.cursor() if pg else closing(conn.cursor()) as cur:
                if mutate:
                    if pg:
                        cur.execute("set local lock_timeout = '5s'")
                        cur.execute("select pg_advisory_xact_lock(%s)", (LOCK_KEY,))
                    else:
                        cur.execute("BEGIN IMMEDIATE")
                placeholder = "%s" if pg else "?"
                cur.execute(
                    f"select details from {table} where source = {placeholder} and action = 'settings' "
                    "and result = 'active' order by id desc limit 1", (SOURCE,))
                value = _decode(cur.fetchone())
                if not mutate:
                    return deepcopy(value)
                if value["revision"] != revision:
                    raise HTTPException(409, "Lighting changed in another window. Refresh and try again.")
                mutate(value)
                value["revision"] += 1
                cur.execute(f"delete from {table} where source = {placeholder} and action = 'settings'", (SOURCE,))
                payload = json.dumps(value, separators=(",", ":"))
                if pg:
                    cur.execute("insert into public.activity(source, action, result, details) values (%s, 'settings', 'active', %s)", (SOURCE, payload))
                    cur.execute("insert into public.activity(source, action, result, details) values ('lighting', %s, 'saved-preview', %s)", (action, "Saved for later connection; no hardware command sent."))
                else:
                    cur.execute("insert into activity(created_at, source, action, result, details) values (?, ?, 'settings', 'active', ?)", (db._now(), SOURCE, payload))
                    cur.execute("insert into activity(created_at, source, action, result, details) values (?, 'lighting', ?, 'saved-preview', ?)", (db._now(), action, "Saved for later connection; no hardware command sent."))
                return deepcopy(value)
