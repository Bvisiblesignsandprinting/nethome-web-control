"""Owner-authenticated, encrypted Wi-Fi backups. Never emit passwords to logs."""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from . import db
from .config import settings

class WifiProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ssid: str = Field(min_length=1, max_length=32)
    password: SecretStr

    @field_validator("ssid")
    @classmethod
    def valid_ssid(cls, value):
        if len(value.encode("utf-8")) > 32 or "\x00" in value:
            raise ValueError("Invalid network name")
        return value

    @field_validator("password")
    @classmethod
    def valid_password(cls, value):
        if len(value.get_secret_value().encode("utf-8")) > 64 or "\x00" in value.get_secret_value():
            raise ValueError("Invalid network password")
        return value

class WifiBackup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profiles: list[WifiProfile] = Field(max_length=3)


def _cipher():
    # Domain-separated application key, kept outside the database. A separate
    # stable key can be configured before first backup for API-token rotation.
    secret = settings.panel_wifi_key or settings.api_token
    if not secret or secret == "change-me-before-remote-access":
        raise HTTPException(503, "Wi-Fi backup encryption is not configured")
    key = hmac.new(secret.encode(), b"nethome-wifi-backup-v1", hashlib.sha256).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _panel_id(value):
    value=value.replace(":", "").lower()
    if not re.fullmatch(r"[0-9a-f]{12}", value):
        raise HTTPException(422, "Invalid panel identifier")
    return value


@contextmanager
def _connection():
    if db._use_postgres():
        with db._pg_conn() as conn:
            with conn.cursor() as cur:
                # Private schema, never exposed through Supabase Data API.
                cur.execute("create schema if not exists nethome_private")
                cur.execute("revoke all on schema nethome_private from public, anon, authenticated")
                cur.execute("""create table if not exists nethome_private.panel_wifi (
                    panel_id text primary key, ciphertext text not null, updated_at text not null)""")
                cur.execute("alter table nethome_private.panel_wifi enable row level security")
                cur.execute("revoke all on nethome_private.panel_wifi from public, anon, authenticated")
            yield conn
    else:
        with db._sqlite_conn() as conn:
            conn.execute("create table if not exists panel_wifi (panel_id text primary key, ciphertext text not null, updated_at text not null)")
            yield conn


def _load(conn, panel_id, locked=False):
    if db._use_postgres():
        with conn.cursor() as cur:
            cur.execute("select ciphertext from nethome_private.panel_wifi where panel_id=%s" + (" for update" if locked else ""), (panel_id,))
            row=cur.fetchone()
    else:
        row=conn.execute("select ciphertext from panel_wifi where panel_id=?",(panel_id,)).fetchone()
    if not row:
        return []
    try:
        payload=json.loads(_cipher().decrypt(row["ciphertext"].encode()).decode())
        if payload.get("panel_id") != panel_id:
            raise ValueError()
        return payload["profiles"]
    except (InvalidToken, ValueError, KeyError, TypeError):
        raise HTTPException(503, "Wi-Fi backup cannot be decrypted; original encryption key required") from None


def _write(panel_id, profiles):
    with _connection() as conn:
        if db._use_postgres():
            with conn.cursor() as cur:
                cur.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))",("panel-wifi:"+panel_id,))
        else:
            conn.execute("begin immediate")
        saved=_load(conn,panel_id,True)
        # Merge, never replace: a new firmware upload must not delete old networks.
        incoming=[{"ssid":v.ssid,"password":v.password.get_secret_value()} for v in profiles]
        names={v["ssid"] for v in incoming}
        merged=(incoming+[v for v in saved if v["ssid"] not in names])[:16]
        ciphertext=_cipher().encrypt(json.dumps({"panel_id":panel_id,"profiles":merged},separators=(",",":")).encode()).decode()
        updated=datetime.now(timezone.utc).isoformat()
        if db._use_postgres():
            with conn.cursor() as cur:
                cur.execute("""insert into nethome_private.panel_wifi(panel_id,ciphertext,updated_at) values(%s,%s,%s)
                on conflict(panel_id) do update set ciphertext=excluded.ciphertext,updated_at=excluded.updated_at""",(panel_id,ciphertext,updated))
        else:
            conn.execute("insert into panel_wifi values(?,?,?) on conflict(panel_id) do update set ciphertext=excluded.ciphertext,updated_at=excluded.updated_at",(panel_id,ciphertext,updated))
    return len(merged)


def wifi_router(auth):
    router=APIRouter(prefix="/api/panel/wifi",dependencies=[Depends(auth)])

    @router.get("/{panel_id}")
    def restore(panel_id: str, response: Response):
        response.headers["Cache-Control"]="no-store, private"
        response.headers["Pragma"]="no-cache"
        with _connection() as conn:
            profiles=_load(conn,_panel_id(panel_id))
        return {"profiles":profiles}

    @router.post("/{panel_id}")
    def backup(panel_id: str, body: WifiBackup, response: Response):
        response.headers["Cache-Control"]="no-store, private"
        count=_write(_panel_id(panel_id),body.profiles)
        return {"ok":True,"profile_count":count}

    return router
