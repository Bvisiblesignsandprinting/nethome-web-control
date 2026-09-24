from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from urllib.parse import parse_qs
from urllib.request import Request as UrlRequest, urlopen

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import settings
from .db import (
    add_activity,
    claim_next_execution,
    create_schedule,
    delete_schedule,
    enqueue_device_command,
    finish_schedule_execution,
    get_latest_device_state,
    init_db,
    list_activity,
    list_schedules,
    load_email_bridge_config,
    load_google_voice_config,
    load_sms_config,
    load_smart_control_config,
    save_device_state,
    save_email_bridge_config,
    save_google_voice_config,
    save_sms_config,
    save_smart_control_config,
    update_schedule,
)
from .mcp_server import mcp as nethome_mcp, mcp_http_app
from .midea_client import midea
from .messaging import process_text_command
from .models import (
    DeviceCommand,
    EmailBridgeConfigUpdate,
    GoogleVoiceConfigUpdate,
    ScheduleCreate,
    SchedulePromptRequest,
    ScheduleUpdate,
    SmartControlCalibration,
    SmartControlTarget,
    SmartControlToggle,
    SmartControlUpdate,
    SmsConfigUpdate,
)
from .scheduler import run_due_schedules
from .tuya_client import TuyaCloudError, tuya

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
SESSION_COOKIE = "nethome_session"
SESSION_VALUE = "authenticated-v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    async with nethome_mcp.session_manager.run():
        yield


app = FastAPI(title="NetHome Web Control", version="0.4.1", lifespan=lifespan)
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


@app.get("/api/panel/firmware", dependencies=[Depends(access_auth)])
def panel_firmware_manifest():
    """Firmware metadata consumed by the dedicated ESP32 panel."""
    version = settings.panel_firmware_version
    url = settings.panel_firmware_url or ""
    sha256 = settings.panel_firmware_sha256 or ""
    signed = f"{version}|{url}|{sha256}"
    signature = ""
    if settings.login_password:
        signature = hmac.new(
            settings.login_password.encode(),
            signed.encode(),
            hashlib.sha256,
        ).hexdigest()
    return {
        "available": bool(url and sha256 and signature),
        "version": version,
        "url": url or None,
        "sha256": sha256 or None,
        "signature": signature or None,
        "notes": settings.panel_firmware_notes,
    }


@app.get("/api/panel/config", dependencies=[Depends(access_auth)])
def panel_remote_config():
    """Server-controlled panel feature/configuration document."""
    try:
        payload = json.loads(settings.panel_config_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "ok": True,
        "config": payload,
    }


class EmailTextRequest(BaseModel):
    text: str
    sender: str | None = None


class WorkerCompleteRequest(BaseModel):
    ok: bool
    result: dict | None = None
    error: str | None = None


class WorkerStateRequest(BaseModel):
    online: bool
    state: dict | None = None
    error: str | None = None


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


def _bootstrap_worker_token() -> str:
    return hmac.new(
        settings.api_token.encode(),
        b"nethome-worker-v1",
        hashlib.sha256,
    ).hexdigest()


def worker_auth(authorization: str | None = Header(default=None)) -> None:
    candidates = {value for value in (settings.worker_secret, _bootstrap_worker_token()) if value}
    if not candidates:
        raise HTTPException(status_code=503, detail="Worker secret is not configured")
    supplied = authorization.removeprefix("Bearer ") if authorization and authorization.startswith("Bearer ") else ""
    if not any(hmac.compare_digest(supplied, candidate) for candidate in candidates):
        raise HTTPException(status_code=401, detail="Invalid worker secret")


def _valid_twilio_signature(
    url: str,
    params: dict[str, str],
    signature: str | None,
    fallback_token: str | None,
    fallback_secret: str | None,
) -> bool:
    """Validate Twilio signature, or a dedicated secret webhook URL."""
    token = settings.twilio_auth_token
    if token:
        if not signature:
            return False
        payload = url + "".join(key + params[key] for key in sorted(params))
        digest = hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
        expected = base64.b64encode(digest).decode()
        return hmac.compare_digest(signature, expected)

    return bool(
        fallback_secret
        and fallback_token
        and hmac.compare_digest(fallback_token, fallback_secret)
    )


def _normalize_phone(value: str | None) -> str:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits if digits else ""


def _sms_config_with_secret() -> dict:
    config = load_sms_config()
    secret = str(config.get("webhook_secret") or "")
    if not secret:
        secret = secrets.token_urlsafe(32)
        config["webhook_secret"] = secret
        save_sms_config(config)
    return config


def _email_bridge_config_with_secret() -> dict:
    config = load_email_bridge_config()
    secret = str(config.get("webhook_secret") or "")
    if not secret:
        secret = secrets.token_urlsafe(32)
        config["webhook_secret"] = secret
        save_email_bridge_config(config)
    return config


def _google_voice_config_with_secret() -> dict:
    config = load_google_voice_config()
    secret = str(config.get("bridge_secret") or "")
    if not secret:
        secret = secrets.token_urlsafe(32)
        config["bridge_secret"] = secret
        save_google_voice_config(config)
    return config


def _google_voice_apps_script(base_url: str, secret: str, allowed_phone: str) -> str:
    phone_digits = "".join(ch for ch in allowed_phone if ch.isdigit())[-10:]
    endpoint = f"{base_url}/api/message/google-voice?token={secret}"
    return f"""const NETHOME_ENDPOINT = {json.dumps(endpoint)};
const ALLOWED_PHONE = {json.dumps(phone_digits)};

function b64urlDecode(input) {{
  if (!input) return '';
  let s = String(input).replace(/-/g, '+').replace(/_/g, '/');
  while (s.length % 4) s += '=';
  return Utilities.newBlob(Utilities.base64Decode(s)).getDataAsString();
}}

function b64urlEncode(input) {{
  return Utilities.base64EncodeWebSafe(String(input), Utilities.Charset.UTF_8).replace(/=+$/g, '');
}}

function gmailApi(path, options) {{
  const opts = options || {{}};
  opts.headers = Object.assign({{
    Authorization: 'Bearer ' + ScriptApp.getOAuthToken()
  }}, opts.headers || {{}});
  opts.muteHttpExceptions = true;
  const res = UrlFetchApp.fetch('https://gmail.googleapis.com/gmail/v1/users/me/' + path, opts);
  const code = res.getResponseCode();
  const raw = res.getContentText() || '{{}}';
  if (code < 200 || code >= 300) throw new Error('Gmail API ' + code + ': ' + raw.slice(0, 300));
  return JSON.parse(raw);
}}

function headerValue(headers, name) {{
  const target = String(name).toLowerCase();
  const h = (headers || []).find(x => String(x.name || '').toLowerCase() === target);
  return h ? String(h.value || '') : '';
}}

function messagePlainText(payload) {{
  if (!payload) return '';
  if (payload.mimeType === 'text/plain' && payload.body && payload.body.data) {{
    return b64urlDecode(payload.body.data);
  }}
  const parts = payload.parts || [];
  for (const part of parts) {{
    const text = messagePlainText(part);
    if (text) return text;
  }}
  if (payload.body && payload.body.data) return b64urlDecode(payload.body.data);
  return '';
}}

function sendVoiceReply(to, subject, body, threadId, messageId) {{
  const profile = gmailApi('profile');
  const from = String(profile.emailAddress || '');
  const cleanSubject = /^re:/i.test(subject) ? subject : 'Re: ' + subject;
  const headers = [
    'From: ' + from,
    'To: ' + to,
    'Subject: ' + cleanSubject,
    messageId ? 'In-Reply-To: ' + messageId : '',
    messageId ? 'References: ' + messageId : '',
    'MIME-Version: 1.0',
    'Content-Type: text/plain; charset=UTF-8',
    'Content-Transfer-Encoding: 8bit'
  ].filter(Boolean);

  // Keep the required blank line between RFC 5322 headers and the body.
  // Do not filter the separator out, or Gmail accepts a message with an empty body.
  const mime = headers.join('\\r\\n') + '\\r\\n\\r\\n' + String(body || '');

  gmailApi('messages/send', {{
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify({{raw: b64urlEncode(mime), threadId: threadId}})
  }});
}}

function pollGoogleVoice() {{
  const props = PropertiesService.getScriptProperties();
  const allowedDigits = String(ALLOWED_PHONE || '').replace(/\\D/g, '').slice(-10);
  const formattedPhone = allowedDigits
    ? '(' + allowedDigits.slice(0,3) + ') ' + allowedDigits.slice(3,6) + '-' + allowedDigits.slice(6)
    : '';
  const query = formattedPhone
    ? 'subject:"New text message from ' + formattedPhone + '" newer_than:1d'
    : 'subject:"New text message from" newer_than:1d';

  const found = gmailApi('messages?q=' + encodeURIComponent(query) + '&maxResults=30');
  const refs = found.messages || [];
  console.log('NetHome: found ' + refs.length + ' recent Google Voice messages');

  const pending = [];
  refs.forEach(ref => {{
    const metaKey = 'done_' + String(ref.id || '');
    if (!ref.id || props.getProperty(metaKey)) return;

    const msg = gmailApi('messages/' + encodeURIComponent(ref.id) + '?format=full');
    const headers = (msg.payload && msg.payload.headers) || [];
    const from = headerValue(headers, 'From').toLowerCase();
    const subject = headerValue(headers, 'Subject');

    if (!from.includes('@txt.voice.google.com')) return;
    if (!/^new text message from/i.test(subject)) return;

    const subjectDigits = subject.replace(/\\D/g, '');
    if (allowedDigits && !subjectDigits.endsWith(allowedDigits)) return;

    pending.push({{
      id: ref.id,
      metaKey: metaKey,
      msg: msg,
      headers: headers,
      subject: subject,
      command: extractVoiceCommand(messagePlainText(msg.payload)),
      ts: Number(msg.internalDate || 0)
    }});
  }});

  pending.sort((a, b) => a.ts - b.ts);
  let sent = 0;

  for (let i = 0; i < pending.length; i++) {{
    const item = pending[i];
    if (!item.command) continue;

    const isWeekStart = /^(WEEK|WEEK SCHEDULE|SCHEDULE WEEK)\\s*:/i.test(item.command);
    const isWeekFragment = /^(TODAY|TOMORROW|MON(?:DAY)?|TUE(?:S|SDAY)?|WED(?:NESDAY)?|THU(?:R|RS|RSDAY)?|FRI(?:DAY)?|SAT(?:URDAY)?|SUN(?:DAY)?|20\\d{{2}}-\\d{{2}}-\\d{{2}})\\s+/i.test(item.command);

    if (isWeekStart) {{
      const group = [item];

      // Google Voice can split one long SMS into separate Gmail messages and
      // those messages are not guaranteed to share the same Gmail thread ID.
      // Combine nearby schedule-looking pieces from the same allowed phone.
      for (let j = i + 1; j < pending.length; j++) {{
        const next = pending[j];
        if (next.ts - item.ts > 180000) break;

        const nextIsFragment =
          /^(TODAY|TOMORROW|MON(?:DAY)?|TUE(?:S|SDAY)?|WED(?:NESDAY)?|THU(?:R|RS|RSDAY)?|FRI(?:DAY)?|SAT(?:URDAY)?|SUN(?:DAY)?|20\\d{{2}}-\\d{{2}}-\\d{{2}})\\s+/i.test(next.command || '');
        if (!nextIsFragment) break;
        group.push(next);
      }}

      const newestTs = Math.max.apply(null, group.map(x => x.ts || 0));
      if (Date.now() - newestTs < 45000) {{
        console.log('NetHome: waiting for remaining WEEK text pieces');
        break;
      }}

      const combined = group.map(x => x.command).filter(Boolean).join(' ; ');
      console.log('NetHome: combined WEEK command=' + combined.slice(0, 1200));

      try {{
        const response = UrlFetchApp.fetch(NETHOME_ENDPOINT, {{
          method: 'post',
          contentType: 'application/json',
          payload: JSON.stringify({{text: combined, sender: ALLOWED_PHONE}}),
          muteHttpExceptions: true
        }});
        const code = response.getResponseCode();
        const raw = response.getContentText() || '{{}}';
        console.log('NetHome: backend HTTP ' + code + ' body=' + raw.slice(0, 800));
        let result = {{}};
        try {{ result = JSON.parse(raw); }} catch (_) {{}}
        const replyText = result.reply || result.detail || 'NetHome command received.';

        if (code >= 200 && code < 300) {{
          const last = group[group.length - 1];
          const rfcMessageId = headerValue(last.headers, 'Message-ID');
          sendVoiceReply(headerValue(last.headers, 'From'), last.subject, replyText, last.msg.threadId, rfcMessageId);
          group.forEach(x => props.setProperty(x.metaKey, new Date().toISOString()));
          sent++;
          console.log('NetHome: combined WEEK reply sent pieces=' + group.length);
          i += group.length - 1;
        }}
      }} catch (err) {{
        console.log('NetHome bridge WEEK error: ' + err);
      }}
      continue;
    }}

    // A split schedule fragment must never be sent as a standalone direct command.
    // If the WEEK opener has not arrived in this poll yet, leave it unprocessed so
    // the next poll can combine it with the complete batch.
    if (isWeekFragment) {{
      console.log('NetHome: holding orphan WEEK fragment=' + item.command.slice(0, 200));
      continue;
    }}

    console.log('NetHome: extracted command=' + item.command);
    try {{
      const response = UrlFetchApp.fetch(NETHOME_ENDPOINT, {{
        method: 'post',
        contentType: 'application/json',
        payload: JSON.stringify({{text: item.command, sender: ALLOWED_PHONE}}),
        muteHttpExceptions: true
      }});
      const code = response.getResponseCode();
      const raw = response.getContentText() || '{{}}';
      console.log('NetHome: backend HTTP ' + code + ' body=' + raw.slice(0, 500));
      let result = {{}};
      try {{ result = JSON.parse(raw); }} catch (_) {{}}
      const replyText = result.reply || result.detail || 'NetHome command received.';

      if (code >= 200 && code < 300) {{
        const rfcMessageId = headerValue(item.headers, 'Message-ID');
        sendVoiceReply(headerValue(item.headers, 'From'), item.subject, replyText, item.msg.threadId, rfcMessageId);
        props.setProperty(item.metaKey, new Date().toISOString());
        sent++;
        console.log('NetHome: reply sent');
      }}
    }} catch (err) {{
      console.log('NetHome bridge error: ' + err);
    }}
  }}

  console.log('NetHome: pending=' + pending.length + ' replies=' + sent);
}}
function extractVoiceCommand(body) {{
  const lines = String(body || '').replace(/\\r/g, '').split('\\n')
    .map(s => s.trim()).filter(Boolean);

  const footerStarts = (line) => {{
    const s = String(line || '').trim();
    if (/^to respond to this text message/i.test(s)) return true;
    if (/^<https?:\\/\\/(?:support\\.google\\.com\\/voice|voice\\.google\\.com\\/settings)/i.test(s)) return true;
    if (/^https?:\\/\\/(?:support\\.google\\.com\\/voice|voice\\.google\\.com\\/settings)/i.test(s)) return true;
    if (/^email notifications for text messages/i.test(s)) return true;
    if (/^if you.*emails in the future/i.test(s)) return true;
    return false;
  }};

  const isBoilerplate = (line) => {{
    if (/^<https?:\\/\\/[^>]+>$/.test(line)) return true;
    if (/^https?:\\/\\//i.test(line)) return true;
    if (/^(google voice|your account|help center|help forum|google llc)/i.test(line)) return true;
    if (/^this email was sent to you/i.test(line)) return true;
    if (/^1600 amphitheatre/i.test(line)) return true;
    if (/^mountain view/i.test(line)) return true;
    if (/^new text message from/i.test(line)) return true;
    return false;
  }};

  const useful = [];
  for (const line of lines) {{
    if (footerStarts(line)) break;
    if (isBoilerplate(line)) continue;
    useful.push(line);
  }}

  if (!useful.length) return '';

  // Preserve week schedules and split schedule fragments. Google Voice can
  // deliver one long phone text as several Gmail notifications.
  if (/^(WEEK|WEEK SCHEDULE|SCHEDULE WEEK)\s*:/i.test(useful[0]) ||
      /^(TODAY|TOMORROW|MON(?:DAY)?|TUE(?:S|SDAY)?|WED(?:NESDAY)?|THU(?:R|RS|RSDAY)?|FRI(?:DAY)?|SAT(?:URDAY)?|SUN(?:DAY)?|20\d{{2}}-\d{{2}}-\d{{2}})\s+/i.test(useful[0]) ||
      /^\d{{1,2}}(?::\d{{2}})?\s*(?:AM|PM)\s+/i.test(useful[0])) {{
    return useful.join(' ; ');
  }}

  // For normal commands, use only the actual command line. Google Voice emails
  // can contain extra footer/thread text that must not become part of the command.
  const commandPattern = /^(STATUS|ON|OFF|HELP|START|YES|STOP|SCHEDULES?|MODE|WEATHER(?:\s+.*)?|TEMP(?:ERATURE)?\s+\d+(?:\.\d+)?|SET\s+\d+(?:\.\d+)?|COOL\s+\d+(?:\.\d+)?|HEAT\s+\d+(?:\.\d+)?|FAN(?:\s+(?:AUTO|LOW|MEDIUM|HIGH))?|ADD SCHEDULE\s+.*)$/i;
  for (const line of useful) {{
    if (commandPattern.test(line)) return line;
  }}

  return useful[0];
}}

function installNetHomeTrigger() {{
  // Force Apps Script to request Gmail authorization before the REST API calls below.
  // This harmless Gmail read makes the required Gmail scope part of this script automatically,
  // so you do not need to edit appsscript.json by hand.
  GmailApp.search('newer_than:1d', 0, 1);

  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('pollGoogleVoice').timeBased().everyMinutes(1).create();
  pollGoogleVoice();
}}
"""


def _extract_phone_digits_from_gateway(address: str) -> str:
    local = (address or "").split("@", 1)[0]
    return "".join(ch for ch in local if ch.isdigit())


def _postmark_inbound_text(payload: dict) -> str:
    for key in ("StrippedTextReply", "TextBody"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value

    for item in payload.get("Attachments") or []:
        if not isinstance(item, dict):
            continue
        ctype = str(item.get("ContentType") or "").lower()
        name = str(item.get("Name") or "").lower()
        if ctype.startswith("text/plain") or name.endswith(".txt"):
            raw = item.get("Content")
            if not raw:
                continue
            try:
                return base64.b64decode(raw).decode("utf-8", errors="replace").strip()
            except Exception:
                continue
    return ""


def _send_postmark_reply(token: str, from_email: str, to_email: str, text_body: str) -> None:
    payload = json.dumps(
        {
            "From": from_email,
            "To": to_email,
            "Subject": "NetHome AC",
            "TextBody": text_body,
            "MessageStream": "outbound",
        }
    ).encode()
    req = UrlRequest(
        "https://api.postmarkapp.com/email",
        data=payload,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Postmark-Server-Token": token,
        },
    )
    with urlopen(req, timeout=15) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Postmark reply failed with HTTP {response.status}")


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
        "control_backend": "vercel-midea-cloud",
        "computer_required": False,
        "twilio_signature_configured": bool(settings.twilio_auth_token),
        "sms_webhook": "/api/message/twilio",
        "direct_sms_configured": bool(load_sms_config().get("allowed_from")),
        "email_bridge_configured": bool(
            load_email_bridge_config().get("postmark_server_token")
            and load_email_bridge_config().get("inbound_address")
        ),
        "google_voice_bridge_configured": bool(
            load_google_voice_config().get("allowed_phone")
        ),
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
        state = midea.status()
        save_device_state(state, True, source="cloud")
        return {"ok": True, "status": state, "source": "vercel-cloud"}
    except Exception as exc:
        add_activity("cloud", "device_status", "error", str(exc)[:500])
        cached = get_latest_device_state()
        if cached and cached.get("online") and cached.get("state"):
            return {
                "ok": True,
                "status": cached["state"],
                "updated_at": cached.get("updated_at"),
                "source": "cached-cloud",
                "stale": True,
                "warning": str(exc)[:300],
            }
        return {
            "ok": False,
            "offline": True,
            "error": str(exc)[:500],
            "source": "vercel-cloud",
        }


@app.get("/api/indoor-sensor", dependencies=[Depends(access_auth)])
def indoor_sensor():
    if not tuya.configured:
        raise HTTPException(status_code=503, detail="Tuya indoor sensor is not configured")
    try:
        result = tuya.indoor_sensor()
        add_activity(
            "tuya",
            "indoor_sensor",
            "success",
            f"temperature_f={result.get('temperature_f')} humidity={result.get('humidity')}",
        )
        return result
    except TuyaCloudError as exc:
        add_activity("tuya", "indoor_sensor", "error", str(exc)[:500])
        return {
            "ok": False,
            "offline": True,
            "error": str(exc)[:500],
            "source": "tuya-cloud",
        }


@app.post("/api/device/command", dependencies=[Depends(access_auth)])
def device_command(body: DeviceCommand):
    if not settings.allow_writes:
        raise HTTPException(status_code=423, detail="AC writes are locked")

    command = body.model_dump(exclude_none=True)
    try:
        result = midea.command(command)
        save_device_state(result, True, source="cloud")
        add_activity(
            "web",
            "device_command",
            "success",
            f"command={command} verified={result.get('verified', False)}",
        )
        return {"ok": True, "verified": bool(result.get("verified")), "result": result}
    except Exception as exc:
        print(f"[device_command] command={command} error={type(exc).__name__}: {exc}", flush=True)
        add_activity("web", "device_command", "error", f"command={command} error={exc}")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/worker/bootstrap-token", dependencies=[Depends(access_auth)])
def worker_bootstrap_token():
    return {"token": _bootstrap_worker_token()}


@app.get("/api/worker/next", dependencies=[Depends(worker_auth)])
def worker_next():
    # The Windows worker is retired. Direct Vercel cloud control is authoritative.
    return {"job": None, "retired": True}


@app.post("/api/worker/{execution_id}/complete", dependencies=[Depends(worker_auth)])
def worker_complete(execution_id: int, body: WorkerCompleteRequest):
    return JSONResponse(status_code=410, content={"ok": False, "retired": True})


@app.post("/api/worker/state", dependencies=[Depends(worker_auth)])
def worker_state(body: WorkerStateRequest):
    return {"ok": True, "retired": True}


def _interpret_schedule_prompt(prompt: str) -> list[dict]:
    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail="AI schedule interpreter is not configured")

    now_local = datetime.now(ZoneInfo("America/New_York"))
    schema = {
        "type": "object",
        "properties": {
            "schedules": {
                "type": "array",
                "maxItems": 30,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "schedule_type": {"type": "string", "enum": ["one_time", "weekdays", "daily", "weekly"]},
                        "days": {"type": "string"},
                        "start_date": {"type": ["string", "null"]},
                        "end_date": {"type": ["string", "null"]},
                        "time_local": {"type": "string"},
                        "action": {"type": "string", "enum": ["set", "on", "off"]},
                        "mode": {"type": ["string", "null"], "enum": ["Cool", "Heat", "Auto", "Fan", "Dry", None]},
                        "temperature": {"type": ["number", "null"]},
                        "fan": {"type": ["string", "null"], "enum": ["Auto", "Low", "Medium", "High", None]},
                        "enabled": {"type": "boolean"},
                    },
                    "required": ["name", "schedule_type", "days", "start_date", "end_date", "time_local", "action", "mode", "temperature", "fan", "enabled"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["schedules"],
        "additionalProperties": False,
    }
    instructions = f"""Convert the user's plain-English HVAC scheduling request into NetHome schedule rows.
Current local date/time: {now_local.isoformat()}
Timezone: America/New_York.

Do not invent missing dates or times. If the prompt is too ambiguous to produce safe schedules, return an empty schedules array.
Use 24-hour HH:MM for time_local.
Use Mon,Tue,Wed,Thu,Fri,Sat,Sun tokens for weekly days.
For weekdays use schedule_type=weekdays. For every day use daily. For a specific date use one_time with start_date=end_date.
For set actions, include mode/temperature/fan only when the user supplied or clearly requested them.
This endpoint PREVIEWS only; nothing is saved until the user confirms on the website."""
    payload = {
        "model": settings.openai_model,
        "instructions": instructions,
        "input": prompt,
        "max_output_tokens": 1800,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "nethome_schedule_preview",
                "strict": True,
                "schema": schema,
            }
        },
    }
    req = UrlRequest(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI schedule interpretation failed: {str(exc)[:180]}") from exc

    output_text = str(data.get("output_text") or "").strip()
    if not output_text:
        for item in data.get("output") or []:
            for part in item.get("content") or []:
                if part.get("type") in {"output_text", "text"} and part.get("text"):
                    output_text = str(part["text"]).strip()
                    break
            if output_text:
                break
    if not output_text:
        return []

    parsed = json.loads(output_text)
    rows = parsed.get("schedules") if isinstance(parsed, dict) else []
    clean: list[dict] = []
    for row in rows or []:
        try:
            clean.append(ScheduleCreate.model_validate(row).model_dump())
        except Exception:
            continue
    return clean


@app.post("/api/schedules/interpret", dependencies=[Depends(access_auth)])
def schedules_interpret(body: SchedulePromptRequest):
    rows = _interpret_schedule_prompt(body.prompt)
    add_activity("web", "schedule_ai_preview", "success", f"count={len(rows)} prompt={body.prompt[:120]}")
    return {"ok": True, "schedules": rows, "saved": False}


@app.get("/api/smart-control", dependencies=[Depends(access_auth)])
def smart_control_status():
    config = load_smart_control_config()
    sensor = None
    try:
        sensor = tuya.indoor_sensor(max_cache_age_seconds=20) if tuya.configured else None
    except Exception:
        sensor = None

    panel_config = {
        "enabled": bool(config.get("enabled")),
        "target_temperature": float(config.get("target_temperature_f") or 72.0),
        "calibration_f": float(config.get("sensor_calibration_f") or 0.0),
        "deadband_f": float(config.get("deadband_f") or 1.0),
        "min_command_interval_minutes": int(config.get("min_command_interval_minutes") or 5),
        "last_command_at": config.get("last_command_at"),
    }
    return {
        "ok": True,
        "config": panel_config,
        "enabled": panel_config["enabled"],
        "target_temperature_f": panel_config["target_temperature"],
        "sensor_calibration_f": panel_config["calibration_f"],
        "deadband_f": panel_config["deadband_f"],
        "min_command_interval_minutes": panel_config["min_command_interval_minutes"],
        "last_command_at": panel_config["last_command_at"],
        "sensor": sensor,
    }


@app.post("/api/smart-control", dependencies=[Depends(access_auth)])
def smart_control_update(body: SmartControlUpdate):
    patch = {}
    if body.enabled is not None:
        patch["enabled"] = body.enabled
    if body.target_temperature is not None:
        patch["target_temperature_f"] = body.target_temperature
    if body.calibration_f is not None:
        patch["sensor_calibration_f"] = body.calibration_f
    if body.deadband_f is not None:
        patch["deadband_f"] = body.deadband_f
    if body.min_command_interval_minutes is not None:
        patch["min_command_interval_minutes"] = body.min_command_interval_minutes
    config = save_smart_control_config(patch) if patch else load_smart_control_config()
    add_activity(
        "smart-control-config",
        "update",
        "success",
        f"enabled={config.get('enabled')} target={config.get('target_temperature_f')} calibration={config.get('sensor_calibration_f')}",
    )
    return {
        "ok": True,
        "config": {
            "enabled": bool(config.get("enabled")),
            "target_temperature": float(config.get("target_temperature_f") or 72.0),
            "calibration_f": float(config.get("sensor_calibration_f") or 0.0),
            "deadband_f": float(config.get("deadband_f") or 1.0),
            "min_command_interval_minutes": int(config.get("min_command_interval_minutes") or 5),
            "last_command_at": config.get("last_command_at"),
        },
    }


@app.post("/api/smart-control/toggle", dependencies=[Depends(access_auth)])
def smart_control_toggle(body: SmartControlToggle):
    config = save_smart_control_config({"enabled": body.enabled})
    add_activity("smart-control-config", "toggle", "success", f"enabled={body.enabled}")
    return config


@app.post("/api/smart-control/target", dependencies=[Depends(access_auth)])
def smart_control_target(body: SmartControlTarget):
    config = save_smart_control_config({"target_temperature_f": body.target_temperature_f})
    add_activity("smart-control-config", "target", "success", f"target={body.target_temperature_f}")
    return config


@app.post("/api/smart-control/calibration", dependencies=[Depends(access_auth)])
def smart_control_calibration(body: SmartControlCalibration):
    config = save_smart_control_config({"sensor_calibration_f": body.sensor_calibration_f})
    add_activity("smart-control-config", "calibration", "success", f"offset={body.sensor_calibration_f}")
    return config


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
    # Legacy bridge kept temporarily for compatibility; direct Twilio SMS is preferred.
    return process_text_command(body.text)


@app.get("/api/sms/config", dependencies=[Depends(access_auth)])
def sms_config(request: Request):
    config = _sms_config_with_secret()
    base = str(request.base_url).rstrip("/")
    return {
        "ok": True,
        "allowed_from": config.get("allowed_from") or "",
        "webhook_url": f"{base}/api/message/twilio?token={config['webhook_secret']}",
        "direct_sms_ready": bool(config.get("allowed_from")),
        "twilio_signature_configured": bool(settings.twilio_auth_token),
    }


@app.post("/api/sms/config", dependencies=[Depends(access_auth)])
def sms_config_update(body: SmsConfigUpdate, request: Request):
    config = _sms_config_with_secret()
    allowed_from = _normalize_phone(body.allowed_from)
    if len(allowed_from) < 11:
        raise HTTPException(status_code=400, detail="Enter a valid mobile number.")
    config["allowed_from"] = allowed_from
    if body.rotate_webhook_secret:
        config["webhook_secret"] = secrets.token_urlsafe(32)
    save_sms_config(config)
    base = str(request.base_url).rstrip("/")
    add_activity("sms-config", "updated", "success", "Direct Twilio SMS configured.")
    return {
        "ok": True,
        "allowed_from": allowed_from,
        "webhook_url": f"{base}/api/message/twilio?token={config['webhook_secret']}",
        "direct_sms_ready": True,
    }


@app.get("/api/google-voice/config", dependencies=[Depends(access_auth)])
def google_voice_config(request: Request):
    config = _google_voice_config_with_secret()
    base = str(request.base_url).rstrip("/")
    allowed_phone = _normalize_phone(str(config.get("allowed_phone") or ""))
    return {
        "ok": True,
        "allowed_phone": allowed_phone,
        "ready": bool(allowed_phone),
        "apps_script": _google_voice_apps_script(
            base,
            str(config["bridge_secret"]),
            allowed_phone,
        ),
    }


@app.post("/api/google-voice/config", dependencies=[Depends(access_auth)])
def google_voice_config_update(body: GoogleVoiceConfigUpdate, request: Request):
    config = _google_voice_config_with_secret()
    allowed_phone = _normalize_phone(body.allowed_phone)
    if len(allowed_phone) < 11:
        raise HTTPException(status_code=400, detail="Enter a valid mobile number.")
    config["allowed_phone"] = allowed_phone
    if body.rotate_bridge_secret:
        config["bridge_secret"] = secrets.token_urlsafe(32)
    save_google_voice_config(config)
    base = str(request.base_url).rstrip("/")
    add_activity("google-voice-config", "updated", "success", "Google Voice bridge configured.")
    return {
        "ok": True,
        "allowed_phone": allowed_phone,
        "ready": True,
        "apps_script": _google_voice_apps_script(
            base,
            str(config["bridge_secret"]),
            allowed_phone,
        ),
    }


@app.post("/api/message/google-voice")
def google_voice_message(body: EmailTextRequest, request: Request):
    config = load_google_voice_config()
    expected = str(config.get("bridge_secret") or "")
    supplied = request.query_params.get("token") or ""
    if not expected or not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Invalid Google Voice bridge token")

    allowed = _normalize_phone(str(config.get("allowed_phone") or ""))
    sender = _normalize_phone(body.sender or "")
    if allowed and sender and not hmac.compare_digest(allowed, sender):
        add_activity("google-voice", "message", "rejected", f"from={sender[-4:]}")
        raise HTTPException(status_code=403, detail="Unauthorized phone")

    result = process_text_command(body.text)
    add_activity(
        "google-voice",
        "message",
        "success" if result.get("ok") else "error",
        f"command={body.text[:80]}",
    )
    return result


@app.get("/api/email-bridge/config", dependencies=[Depends(access_auth)])
def email_bridge_config(request: Request):
    config = _email_bridge_config_with_secret()
    base = str(request.base_url).rstrip("/")
    return {
        "ok": True,
        "inbound_address": config.get("inbound_address") or "",
        "from_email": config.get("from_email") or "",
        "allowed_phone": config.get("allowed_phone") or "",
        "webhook_url": f"{base}/api/message/postmark?token={config['webhook_secret']}",
        "token_configured": bool(config.get("postmark_server_token")),
        "ready": bool(
            config.get("inbound_address")
            and config.get("postmark_server_token")
            and config.get("from_email")
            and config.get("allowed_phone")
        ),
    }


@app.post("/api/email-bridge/config", dependencies=[Depends(access_auth)])
def email_bridge_config_update(body: EmailBridgeConfigUpdate, request: Request):
    config = _email_bridge_config_with_secret()
    allowed_phone = _normalize_phone(body.allowed_phone)
    if len(allowed_phone) < 11:
        raise HTTPException(status_code=400, detail="Enter a valid mobile number.")

    inbound_address = body.inbound_address.strip()
    from_email = body.from_email.strip()
    if "@" not in inbound_address or "@" not in from_email:
        raise HTTPException(status_code=400, detail="Enter valid email addresses.")

    config.update(
        {
            "inbound_address": inbound_address,
            "postmark_server_token": body.postmark_server_token.strip(),
            "from_email": from_email,
            "allowed_phone": allowed_phone,
        }
    )
    if body.rotate_webhook_secret:
        config["webhook_secret"] = secrets.token_urlsafe(32)
    save_email_bridge_config(config)

    base = str(request.base_url).rstrip("/")
    add_activity("email-bridge-config", "updated", "success", "Temporary email bridge configured.")
    return {
        "ok": True,
        "inbound_address": inbound_address,
        "from_email": from_email,
        "allowed_phone": allowed_phone,
        "webhook_url": f"{base}/api/message/postmark?token={config['webhook_secret']}",
        "ready": True,
    }


@app.post("/api/message/postmark")
async def postmark_inbound_message(request: Request):
    config = load_email_bridge_config()
    expected = str(config.get("webhook_secret") or "")
    supplied = request.query_params.get("token") or ""
    if not expected or not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Invalid email bridge webhook token")

    payload = await request.json()
    sender = str(payload.get("From") or payload.get("FromFull", {}).get("Email") or "").strip()
    sender_lower = sender.lower()
    gateway_domains = ("@vtext.com", "@vzwpix.com", "@mypixmessages.com")
    if not sender_lower.endswith(gateway_domains):
        add_activity("email-bridge", "inbound", "rejected", f"sender={sender_lower[-80:]}")
        return {"ok": True, "ignored": True, "reason": "unsupported_sender"}

    allowed_phone = _normalize_phone(str(config.get("allowed_phone") or ""))
    sender_digits = _extract_phone_digits_from_gateway(sender)
    allowed_digits = "".join(ch for ch in allowed_phone if ch.isdigit())
    if allowed_digits and sender_digits and not sender_digits.endswith(allowed_digits[-10:]):
        add_activity("email-bridge", "inbound", "rejected", f"sender={sender_digits[-4:]}")
        return {"ok": True, "ignored": True, "reason": "unauthorized_phone"}

    text_body = _postmark_inbound_text(payload)
    if not text_body:
        add_activity("email-bridge", "inbound", "ignored", "No text content found.")
        return {"ok": True, "ignored": True, "reason": "no_text"}

    result = process_text_command(text_body)
    reply = str(result.get("reply") or "Command received.")

    token = str(config.get("postmark_server_token") or "")
    from_email = str(config.get("from_email") or "")
    if token and from_email and sender:
        try:
            _send_postmark_reply(token, from_email, sender, reply)
            add_activity(
                "email-bridge",
                "reply",
                "success",
                f"to={sender_digits[-4:] if sender_digits else 'gateway'} command={text_body[:80]}",
            )
        except Exception as exc:
            add_activity("email-bridge", "reply", "error", str(exc)[:500])
    else:
        add_activity("email-bridge", "reply", "error", "Postmark outbound settings incomplete.")

    return {"ok": True, "processed": True}


@app.post("/api/message/twilio")
async def twilio_text_message(request: Request):
    raw = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(raw, keep_blank_values=True)
    params = {key: values[0] if values else "" for key, values in parsed.items()}

    config = load_sms_config()
    signature = request.headers.get("X-Twilio-Signature")
    fallback_token = request.query_params.get("token")
    fallback_secret = str(config.get("webhook_secret") or "")
    if not _valid_twilio_signature(
        str(request.url),
        params,
        signature,
        fallback_token,
        fallback_secret,
    ):
        raise HTTPException(status_code=403, detail="Invalid Twilio webhook authentication")

    body = params.get("Body", "")
    sender = _normalize_phone(params.get("From", ""))
    allowed_from = _normalize_phone(str(config.get("allowed_from") or ""))
    if not allowed_from or not hmac.compare_digest(sender, allowed_from):
        add_activity(
            "twilio",
            "sms",
            "rejected",
            f"from={sender[-4:] if sender else 'unknown'}",
        )
        twiml = '<?xml version="1.0" encoding="UTF-8"?><Response><Message>Not authorized for this private AC control number.</Message></Response>'
        return Response(content=twiml, media_type="application/xml")

    result = process_text_command(body)
    reply = escape(str(result.get("reply") or "Command received."))

    add_activity(
        "twilio",
        "sms",
        "success" if result.get("ok") else "error",
        f"from={sender[-4:] if sender else 'unknown'} command={body[:80]}",
    )

    twiml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{reply}</Message></Response>'
    return Response(content=twiml, media_type="application/xml")


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
