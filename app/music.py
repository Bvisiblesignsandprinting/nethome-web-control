"""Private MP3 library. Audio goes directly between browser and Storage.

This module intentionally makes no AC calls or claims of ESP32 playback support.
"""
from __future__ import annotations

import json
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

MAX_FILE_BYTES = 25 * 1024 * 1024
OBJECT_PATTERN = re.compile(r"^[0-9a-f]{32}_[A-Za-z0-9_.-]{1,120}\.mp3$")


class UploadRequest(BaseModel):
    filename: str = Field(min_length=5, max_length=240)
    size: int = Field(gt=0, le=MAX_FILE_BYTES, strict=True)

    @field_validator("filename")
    @classmethod
    def mp3_filename(cls, value: str) -> str:
        if not value.lower().endswith(".mp3"):
            raise ValueError("Choose an MP3 file")
        if "/" in value or "\\" in value or any(ord(c) < 32 for c in value):
            raise ValueError("Invalid filename")
        return value


def object_name(filename: str) -> str:
    stem = unicodedata.normalize("NFKD", filename[:-4]).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", stem).strip(".-_")[:100] or "Music"
    return f"{uuid4().hex}_{stem}.mp3"


def validate_object(name: str) -> str:
    if not OBJECT_PATTERN.fullmatch(name):
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

    def tracks(self, offset: int):
        self.check_bucket()
        rows = self.request("POST", f"/object/list/{self.bucket}", {
            "prefix": "tracks/", "limit": 100, "offset": offset,
            "sortBy": {"column": "created_at", "order": "desc"},
        })
        tracks = []
        for row in rows:
            name = row.get("name", "")
            if not OBJECT_PATTERN.fullmatch(name) or not row.get("id"):
                continue
            tracks.append({"id": name, "title": name[33:-4].replace("-", " "),
                           "size": (row.get("metadata") or {}).get("size", 0),
                           "created_at": row.get("created_at")})
        return {"tracks": tracks, "next_offset": offset + len(rows) if len(rows) == 100 else None}

    def upload(self, body: UploadRequest):
        self.check_bucket()
        name = object_name(body.filename)
        result = self.request("POST", f"/object/upload/sign/{self.bucket}/tracks/{name}", {})
        return {"id": name, "upload_url": self.absolute_signed_url(result["url"]),
                "max_file_bytes": MAX_FILE_BYTES}

    def playback(self, name: str):
        validate_object(name)
        self.check_bucket()
        path = quote(f"{self.bucket}/tracks/{name}", safe="/")
        self.request("GET", f"/object/info/{path}")
        result = self.request("POST", f"/object/sign/{path}", {"expiresIn": 3600})
        return {"url": self.absolute_signed_url(result["signedURL"]), "expires_in": 3600,
                "playback_device": "browser"}

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

    @router.post("/tracks/{track_id}/playback")
    def playback(track_id: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return storage.playback(track_id)

    @router.delete("/tracks/{track_id}")
    def delete(track_id: str):
        return storage.delete(track_id)

    return router
