"""Curated direct streams from Yiddish24's public station directory.
Audio goes directly to the listener; the application never proxies or records it.
"""
from __future__ import annotations
import json
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import APIRouter, Depends, HTTPException, Response

SOURCE_URL = "https://www.yiddish24.com/streams"
CATALOG_DATE = "2026-10-22"
CATALOG_PATH = Path(__file__).parent / "data" / "yiddish24_stations.json"


def stations():
    rows=json.loads(CATALOG_PATH.read_text())
    valid=[]
    for row in rows:
        url=urlsplit(row["url"])
        if url.scheme!="https" or url.hostname not in {"music.y24.app","music2.y24.app"} or not url.path.removeprefix("/").isdigit() or url.query or url.username or url.port:
            raise RuntimeError("Invalid bundled radio station")
        valid.append({**row,"provider":"Yiddish24","format":"mp3"})
    return valid


def radio_router(auth):
    router=APIRouter(prefix="/api/music/radio",dependencies=[Depends(auth)])

    @router.get("/stations")
    def station_list(response: Response):
        response.headers["Cache-Control"]="private, max-age=300"
        return {"stations":stations(),"source_url":SOURCE_URL,"catalog_date":CATALOG_DATE}

    @router.post("/stations/{station_id}/playback")
    def playback(station_id: str,response: Response):
        response.headers["Cache-Control"]="no-store"
        row=next((v for v in stations() if v["id"]==station_id),None)
        if row is None:
            raise HTTPException(404,"Unknown radio station")
        return {"url":row["url"],"name":row["name"],"title":row["title"],"format":"mp3","live":True,"source_url":SOURCE_URL}

    return router
