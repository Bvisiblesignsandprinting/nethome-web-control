from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import (
    add_activity,
    create_schedule,
    delete_schedule,
    init_db,
    list_activity,
    list_schedules,
    update_schedule,
)
from .midea_client import midea
from .models import DeviceCommand, ScheduleCreate, ScheduleUpdate

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="NetHome Web Control", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def api_auth(authorization: str | None = Header(default=None)) -> None:
    expected = settings.api_token
    if not expected or expected == "change-me-before-remote-access":
        return
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Invalid API token")


@app.get("/")
def home():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "device_name": settings.device_name,
        "device_id": settings.device_id,
        "writes_enabled": settings.allow_writes,
        "api_token_configured": settings.api_token != "change-me-before-remote-access",
    }


@app.get("/api/device/devices", dependencies=[Depends(api_auth)])
def devices():
    try:
        result = midea.account_devices()
        add_activity("web", "list_devices", "success", f"count={len(result)}")
        return {"devices": result}
    except Exception as exc:
        add_activity("web", "list_devices", "error", str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/device/status", dependencies=[Depends(api_auth)])
def device_status():
    try:
        result = midea.status()
        add_activity("web", "status", "success")
        return {"ok": True, "status": result}
    except Exception as exc:
        add_activity("web", "status", "error", str(exc))
        return {"ok": False, "offline": "offline" in str(exc).lower(), "error": str(exc)}


@app.post("/api/device/command", dependencies=[Depends(api_auth)])
def device_command(body: DeviceCommand):
    try:
        result = midea.command(body.model_dump(exclude_none=True))
        add_activity("api", "device_command", "success", str(body.model_dump(exclude_none=True)))
        return {"ok": True, "result": result}
    except PermissionError as exc:
        raise HTTPException(status_code=423, detail=str(exc)) from exc
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc


@app.get("/api/schedules", dependencies=[Depends(api_auth)])
def schedules():
    return {"schedules": list_schedules()}


@app.post("/api/schedules", dependencies=[Depends(api_auth)])
def schedules_create(body: ScheduleCreate):
    row = create_schedule(body.model_dump())
    add_activity("api", "schedule_create", "success", f"schedule_id={row['id']}")
    return row


@app.patch("/api/schedules/{schedule_id}", dependencies=[Depends(api_auth)])
def schedules_update(schedule_id: int, body: ScheduleUpdate):
    row = update_schedule(schedule_id, body.model_dump(exclude_unset=True))
    if not row:
        raise HTTPException(status_code=404, detail="Schedule not found")
    add_activity("api", "schedule_update", "success", f"schedule_id={schedule_id}")
    return row


@app.delete("/api/schedules/{schedule_id}", dependencies=[Depends(api_auth)])
def schedules_delete(schedule_id: int):
    if not delete_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found")
    add_activity("api", "schedule_delete", "success", f"schedule_id={schedule_id}")
    return {"ok": True}


@app.get("/api/activity", dependencies=[Depends(api_auth)])
def activity(limit: int = 50):
    return {"activity": list_activity(max(1, min(limit, 200)))}
