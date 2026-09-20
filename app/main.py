from __future__ import annotations

import hashlib
import hmac
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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
from .mcp_server import mcp as nethome_mcp, mcp_http_app
from .midea_client import midea
from .messaging import process_text_command
from .models import DeviceCommand, ScheduleCreate, ScheduleUpdate
from .scheduler import run_due_schedules

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
SESSION_COOKIE = "nethome_session"
SESSION_VALUE = "authenticated-v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    async with nethome_mcp.session_manager.run():
        yield


app = FastAPI(title="NetHome Web Control", version="0.3.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/mcp", mcp_http_app)


def _sign_session(value: str) -> str:
    secret = settings.api_token
    if not secret or secret == "change-me-before-remote-access":
        raise RuntimeError("NETHOME_API_TOKEN must be configured in production")
    sig = hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{sig}"


def _valid_session(cookie: str | None) -> bool:
    if not cookie or "." not in cookie:
        return False
    value, sig = cookie.rsplit(".", 1)
    if value != SESSION_VALUE:
        return False
    expected = hmac.new(settings.api_token.encode(), value.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def access_auth(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    if _valid_session(request.cookies.get(SESSION_COOKIE)):
        return
    expected = settings.api_token
    if expected and expected != "change-me-before-remote-access":
        if authorization == f"Bearer {expected}":
            return
    raise HTTPException(status_code=401, detail="Login required")


class LoginRequest(BaseModel):
    password: str


class EmailTextRequest(BaseModel):
    text: str
    sender: str | None = None


def automation_auth(authorization: str | None = Header(default=None)) -> None:
    expected = settings.automation_secret
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="NETHOME_AUTOMATION_SECRET is not configured",
        )
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Invalid automation secret")


def message_auth(authorization: str | None = Header(default=None)) -> None:
    expected = settings.message_secret
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="NETHOME_MESSAGE_SECRET is not configured",
        )
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Invalid messaging secret")


@app.get("/login")
def login_page(request: Request):
    if _valid_session(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC / "login.html")


@app.post("/api/login")
def login(body: LoginRequest):
    if not settings.login_password:
        raise HTTPException(status_code=503, detail="Login password is not configured")
    if not hmac.compare_digest(body.password, settings.login_password):
        raise HTTPException(status_code=401, detail="Incorrect password")
    response = JSONResponse({"ok": True})
    response.set_cookie(
        SESSION_COOKIE,
        _sign_session(SESSION_VALUE),
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
        path="/",
    )
    return response


@app.post("/api/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/")
def home(request: Request):
    if not _valid_session(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "device_name": settings.device_name,
        "device_id": settings.device_id,
        "writes_enabled": settings.allow_writes,
        "login_configured": bool(settings.login_password),
    }


@app.get("/api/device/devices", dependencies=[Depends(access_auth)])
def devices():
    try:
        result = midea.account_devices()
        add_activity("web", "list_devices", "success", f"count={len(result)}")
        return {"devices": result}
    except Exception as exc:
        add_activity("web", "list_devices", "error", str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/device/status", dependencies=[Depends(access_auth)])
def device_status():
    try:
        result = midea.status()
        add_activity("web", "status", "success")
        return {"ok": True, "status": result}
    except Exception as exc:
        add_activity("web", "status", "error", str(exc))
        return {"ok": False, "offline": "offline" in str(exc).lower(), "error": str(exc)}


@app.post("/api/device/command", dependencies=[Depends(access_auth)])
def device_command(body: DeviceCommand):
    try:
        result = midea.command(body.model_dump(exclude_none=True))
        add_activity("api", "device_command", "success", str(body.model_dump(exclude_none=True)))
        return {"ok": True, "result": result}
    except PermissionError as exc:
        raise HTTPException(status_code=423, detail=str(exc)) from exc
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc


@app.get("/api/schedules", dependencies=[Depends(access_auth)])
def schedules():
    return {"schedules": list_schedules()}


@app.post("/api/schedules", dependencies=[Depends(access_auth)])
def schedules_create(body: ScheduleCreate):
    row = create_schedule(body.model_dump())
    add_activity("api", "schedule_create", "success", f"schedule_id={row['id']}")
    return row


@app.patch("/api/schedules/{schedule_id}", dependencies=[Depends(access_auth)])
def schedules_update(schedule_id: int, body: ScheduleUpdate):
    row = update_schedule(schedule_id, body.model_dump(exclude_unset=True))
    if not row:
        raise HTTPException(status_code=404, detail="Schedule not found")
    add_activity("api", "schedule_update", "success", f"schedule_id={schedule_id}")
    return row


@app.delete("/api/schedules/{schedule_id}", dependencies=[Depends(access_auth)])
def schedules_delete(schedule_id: int):
    if not delete_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found")
    add_activity("api", "schedule_delete", "success", f"schedule_id={schedule_id}")
    return {"ok": True}


@app.get("/api/activity", dependencies=[Depends(access_auth)])
def activity(limit: int = 50):
    return {"activity": list_activity(max(1, min(limit, 200)))}


@app.post("/api/automation/run", dependencies=[Depends(automation_auth)])
def automation_run():
    return run_due_schedules()


@app.post("/api/message/email", dependencies=[Depends(message_auth)])
def email_text_message(body: EmailTextRequest):
    return process_text_command(body.text)


@app.get("/privacy", response_class=HTMLResponse)
def privacy_policy():
    return """<!doctype html>
<html><head><title>Privacy Policy</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font-family:Arial,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;line-height:1.55;color:#222}h1,h2{color:#111}</style></head>
<body>
<h1>Privacy Policy</h1>
<p><strong>GERSHON FELBERBAUM</strong> operates <strong>NetHome AC Control</strong>, a private, low-volume SMS and HVAC automation service.</p>
<h2>Information we collect</h2>
<p>We may collect the user's phone number, SMS message content, HVAC commands, system status, and weather-related requests needed to operate the service.</p>
<h2>How we use information</h2>
<p>Information is used only to provide, maintain, troubleshoot, and improve the NetHome AC Control service.</p>
<h2>SMS data sharing</h2>
<p>We do not sell or share your SMS opt-in data or personal information with third parties for marketing purposes.</p>
<h2>Retention</h2>
<p>Operational logs may be retained for troubleshooting and service reliability.</p>
<h2>Contact</h2>
<p>Email: nethomeaccontrol@gmail.com</p>
</body></html>"""


@app.get("/terms", response_class=HTMLResponse)
def terms_of_service():
    return """<!doctype html>
<html><head><title>Terms & Conditions</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font-family:Arial,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;line-height:1.55;color:#222}h1,h2{color:#111}</style></head>
<body>
<h1>Terms & Conditions</h1>
<p><strong>GERSHON FELBERBAUM</strong> operates <strong>NetHome AC Control</strong>, a private, low-volume SMS service for HVAC status, weather-based recommendations, and HVAC command confirmations.</p>
<h2>SMS Terms</h2>
<p>Message and data rates may apply. Message frequency varies based on user requests and automated replies.</p>
<p><strong>HELP:</strong> Reply HELP for help.</p>
<p><strong>STOP:</strong> Reply STOP to opt out.</p>
<p>Carriers are not liable for delayed or undelivered messages.</p>
<p>Support: nethomeaccontrol@gmail.com</p>
<p>Privacy Policy: <a href="/privacy">https://nethome-web-control-six.vercel.app/privacy</a></p>
</body></html>"""


@app.get("/sms-opt-in", response_class=HTMLResponse)
def sms_opt_in_proof():
    return """<!doctype html>
<html><head><title>NetHome AC Control SMS Opt-In</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font-family:Arial,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;line-height:1.55;color:#222}h1,h2{color:#111}.box{background:#f5f5f5;padding:16px;border-radius:8px}</style></head>
<body>
<h1>NetHome AC Control SMS Opt-In</h1><p><strong>GERSHON FELBERBAUM</strong> operates NetHome AC Control.</p>
<p>To enroll in this private HVAC messaging service, text <strong>START</strong> to <strong>+1 (502) 747-4864</strong>.</p>
<h2>What happens next</h2>
<div class="box"><strong>Welcome message:</strong><br>
Welcome to NetHome AC Control. This private service sends HVAC status, weather-based recommendations, and command confirmations. Message frequency varies. Msg & data rates may apply. Reply HELP for help or STOP to opt out.</div>
<p>After receiving the welcome message, reply <strong>YES</strong> to confirm enrollment.</p>
<div class="box"><strong>Enrollment confirmation:</strong><br>
NetHome AC Control: You are enrolled. Message frequency varies. Msg & data rates may apply. Reply HELP for help or STOP to opt out.</div>
</body></html>"""
