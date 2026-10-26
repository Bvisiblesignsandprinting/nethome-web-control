"""Private MP3 library. Audio goes directly between browser and Storage.

This module intentionally makes no AC calls or claims of ESP32 playback support.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator

from .config import settings
from .panel_audio import PROFILE, convert_for_panel

MAX_FILE_BYTES = 25 * 1024 * 1024
FILE_PATTERN = re.compile(r"^[0-9a-f]{32}_[A-Za-z0-9_.-]{1,120}\.mp3$")
SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


class UploadRequest(BaseModel):
    filename: str = Field(min_length=5, max_length=240)
    size: int = Field(gt=0, le=MAX_FILE_BYTES, strict=True)
    relative_path: str | None = Field(default=None, max_length=720)

    @field_validator("filename")
    @classmethod
    def mp3_filename(cls, value: str) -> str:
        if not value.lower().endswith(".mp3"):
            raise ValueError("Choose an MP3 file")
        if "/" in value or "\\" in value or any(ord(c) < 32 for c in value):
            raise ValueError("Invalid filename")
        return value

    @field_validator("relative_path")
    @classmethod
    def safe_relative_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.replace("\\", "/")
        if value.startswith("/") or value.endswith("/"):
            raise ValueError("Invalid music folder path")
        parts = value.split("/")
        if len(parts) > 9 or not parts or parts[-1].lower().endswith(".mp3") is False:
            raise ValueError("Invalid music folder path")
        if any(not part or part in {".", ".."} or any(ord(c) < 32 for c in part) for part in parts):
            raise ValueError("Invalid music folder path")
        return value


def safe_segment(value: str, limit: int = 80) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-_")[:limit] or "Music"


def object_name(filename: str, relative_path: str | None = None) -> str:
    path = (relative_path or filename).replace("\\", "/").strip("/")
    parts = path.split("/")
    stem = unicodedata.normalize("NFKD", parts[-1][:-4]).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", stem).strip(".-_")[:100] or "Music"
    folders = [safe_segment(part) for part in parts[:-1]][:8]
    leaf = f"{uuid4().hex}_{stem}.mp3"
    return "/".join([*folders, leaf])


def validate_object(name: str) -> str:
    parts = name.split("/")
    if (len(parts) > 9 or not FILE_PATTERN.fullmatch(parts[-1]) or
            any(not SEGMENT_PATTERN.fullmatch(part) or part in {".", ".."} for part in parts[:-1])):
        raise HTTPException(404, "Track not found")
    return name


class MusicStorage:
    def __init__(self):
        self.origin = settings.music_storage_url
        self.key = settings.music_storage_key
        self.bucket = settings.music_bucket

    @property
    def configured(self) -> bool:
        parsed = urlsplit(self.origin)
        return bool(parsed.scheme == "https" and parsed.netloc and
                    not parsed.path and not parsed.query and not parsed.fragment and
                    not parsed.username and self.key and
                    re.fullmatch(r"[a-z0-9-]+", self.bucket))

    def request(self, method: str, path: str, payload: dict | None = None):
        if not self.configured:
            raise HTTPException(503, "Music storage is not connected yet")
        request = Request(
            self.origin + "/storage/v1" + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers={"Authorization": f"Bearer {self.key}", "apikey": self.key,
                     "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=15) as result:
                return json.load(result)
        except HTTPError as exc:
            # Never expose upstream responses, keys or signed URLs in errors.
            if exc.code == 404:
                raise HTTPException(404, "Music storage or track not found") from None
            raise HTTPException(502, "Music storage request failed") from None
        except (URLError, TimeoutError, ValueError):
            raise HTTPException(502, "Music storage is temporarily unavailable") from None

    def check_bucket(self):
        bucket = self.request("GET", f"/bucket/{self.bucket}")
        limit = bucket.get("file_size_limit")
        types = bucket.get("allowed_mime_types") or []
        if bucket.get("public") is not False or not limit or int(limit) > MAX_FILE_BYTES or types != ["audio/mpeg"]:
            raise HTTPException(503, "Music storage requires a private MP3-only bucket with a 25 MB limit")

    def absolute_signed_url(self, path: str) -> str:
        # Storage returns relative URLs; do not allow redirects to another host.
        if not path.startswith("/object/") or path.startswith("//"):
            raise HTTPException(502, "Music storage returned an invalid URL")
        return self.origin + "/storage/v1" + path

    def _walk_tracks(self, prefix: str = "tracks/", depth: int = 0) -> list[dict]:
        if depth > 8:
            return []
        found: list[dict] = []
        page = 0
        while page < 50:
            rows = self.request("POST", f"/object/list/{self.bucket}", {
                "prefix": prefix, "limit": 100, "offset": page * 100,
                "sortBy": {"column": "name", "order": "asc"},
            })
            for row in rows:
                name = row.get("name", "")
                if row.get("id"):
                    relative = prefix[7:] + name
                    try:
                        validate_object(relative)
                    except HTTPException:
                        continue
                    found.append({**row, "relative": relative})
                elif SEGMENT_PATTERN.fullmatch(name or "") and name not in {".", ".."}:
                    found.extend(self._walk_tracks(prefix + name + "/", depth + 1))
            if len(rows) < 100:
                break
            page += 1
        return found

    def tracks(self, offset: int):
        self.check_bucket()
        rows = sorted(self._walk_tracks(), key=lambda row: row.get("created_at") or "", reverse=True)
        tracks = []
        for row in rows[offset:offset + 100]:
            name = row["relative"]
            leaf = name.rsplit("/", 1)[-1]
            folder = name.rsplit("/", 1)[0] if "/" in name else "Unsorted"
            tracks.append({"id": name, "title": leaf[33:-4].replace("-", " "), "folder": folder,
                           "size": (row.get("metadata") or {}).get("size", 0),
                           "created_at": row.get("created_at")})
        return {"tracks": tracks, "next_offset": offset + 100 if offset + 100 < len(rows) else None}

    def upload(self, body: UploadRequest):
        self.check_bucket()
        name = object_name(body.filename, body.relative_path)
        result = self.request("POST", f"/object/upload/sign/{self.bucket}/tracks/{name}", {})
        return {"id": name, "upload_url": self.absolute_signed_url(result["url"]),
                "max_file_bytes": MAX_FILE_BYTES}

    def panel_copy(self, name: str) -> str:
        """Return a cached derivative, creating it only from our private bucket."""
        original = quote(f"{self.bucket}/tracks/{name}", safe="/")
        info = self.request("GET", f"/object/info/{original}")
        # Version the key with the original object metadata so replacements do
        # not leave the panel listening to an older cached copy.
        import hashlib
        version = {key: info.get(key) for key in ('id', 'updated_at', 'size', 'metadata')}
        stamp = hashlib.sha256(json.dumps(version, sort_keys=True).encode()).hexdigest()[:16]
        relative = f"panel-audio/{PROFILE}/{stamp}/{name}"
        path = quote(f"{self.bucket}/{relative}", safe="/")
        folder, leaf = relative.rsplit('/', 1)
        cached = self.request("POST", f"/object/list/{self.bucket}", {
            "prefix": folder, "limit": 100, "search": leaf,
        })
        if any(row.get('name') == leaf and row.get('id') for row in cached):
            return path
        auth_headers = {"Authorization": f"Bearer {self.key}", "apikey": self.key}
        # Authenticated object URLs are constructed internally. No caller-supplied
        # URL or ffmpeg network input is accepted.
        request = Request(self.origin + "/storage/v1/object/authenticated/" + original, headers=auth_headers)
        stage = "download"
        try:
            with urlopen(request, timeout=15) as source:
                data = source.read(MAX_FILE_BYTES + 1)
            stage = "convert"
            audio = convert_for_panel(data)
            stage = "upload"
            upload = Request(self.origin + "/storage/v1/object/" + path, data=audio,
                             method="POST", headers={**auth_headers, "Content-Type": "audio/mpeg", "x-upsert": "true"})
            with urlopen(upload, timeout=15) as result:
                result.read(1024)
        except Exception as exc:
            logging.getLogger(__name__).warning("Panel audio preparation failed stage=%s kind=%s status=%s", stage, type(exc).__name__, getattr(exc, "code", None))
            raise HTTPException(502, "Could not prepare the smaller music copy. Please retry.") from None
        return path

    def playback(self, name: str, *, panel: bool = False):
        validate_object(name)
        self.check_bucket()
        path = quote(f"{self.bucket}/tracks/{name}", safe="/")
        self.request("GET", f"/object/info/{path}")
        if panel:
            path = self.panel_copy(name)
        result = self.request("POST", f"/object/sign/{path}", {"expiresIn": 3600})
        return {"url": self.absolute_signed_url(result["signedURL"]), "expires_in": 3600,
                "playback_device": "panel" if panel else "browser",
                **({"audio_profile": PROFILE, "bitrate_kbps": 32, "sample_rate": 22050, "channels": 1} if panel else {})}

    def delete(self, name: str):
        validate_object(name)
        self.request("DELETE", f"/object/{self.bucket}", {"prefixes": [f"tracks/{name}"]})
        return {"ok": True}


storage = MusicStorage()


def music_router(auth: Callable) -> APIRouter:
    router = APIRouter(prefix="/api/music", dependencies=[Depends(auth)])

    @router.get("/status")
    def status():
        ready = storage.configured
        if ready:
            storage.check_bucket()
        return {"storage_ready": ready, "max_file_bytes": MAX_FILE_BYTES,
                "formats": ["mp3"], "panel_playback_ready": False}

    @router.get("/tracks")
    def tracks(response: Response, offset: int = Query(default=0, ge=0)):
        response.headers["Cache-Control"] = "no-store"
        return storage.tracks(offset)

    @router.post("/uploads")
    def upload(body: UploadRequest, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return storage.upload(body)

    @router.post("/tracks/{track_id:path}/playback")
    def playback(track_id: str, response: Response, profile: str = Query(default="original", pattern="^(original|panel)$")):
        response.headers["Cache-Control"] = "no-store"
        return storage.playback(track_id, panel=True) if profile == "panel" else storage.playback(track_id)

    @router.delete("/tracks/{track_id:path}")
    def delete(track_id: str):
        return storage.delete(track_id)

    return router
