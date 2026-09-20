from __future__ import annotations

import json
import ssl
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter

os.environ.setdefault("NETHOME_ALLOW_WRITES", "true")

_SSL_CONTEXT = ssl.create_default_context()
if hasattr(ssl, "VERIFY_X509_STRICT"):
    _SSL_CONTEXT.verify_flags &= ~ssl.VERIFY_X509_STRICT

class _TLSAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["ssl_context"] = _SSL_CONTEXT
        return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

_MIDEA_SESSION = requests.Session()
_MIDEA_SESSION.mount("https://", _TLSAdapter())
requests.post = _MIDEA_SESSION.post

from app.midea_client import midea

BASE_URL = os.getenv("NETHOME_REMOTE_URL", "https://nethome-web-control-six.vercel.app").rstrip("/")
POLL_SECONDS = float(os.getenv("NETHOME_WORKER_POLL_SECONDS", "2"))
STATUS_SECONDS = float(os.getenv("NETHOME_WORKER_STATUS_SECONDS", "120"))

SERVICE = "NetHomeWebControl"
WORKER_TOKEN_KEY = "worker_token"

_SSL_CONTEXT = ssl.create_default_context()
if hasattr(ssl, "VERIFY_X509_STRICT"):
    _SSL_CONTEXT.verify_flags &= ~ssl.VERIFY_X509_STRICT


def _token() -> str:
    env = os.getenv("NETHOME_WORKER_TOKEN")
    if env:
        return env
    try:
        import keyring
        value = keyring.get_password(SERVICE, WORKER_TOKEN_KEY)
        if value:
            return value
    except Exception:
        pass
    raise RuntimeError("Worker token is not configured. Run setup_worker.ps1 first.")


def _request(path: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CONTEXT) as resp:
        return json.loads(resp.read().decode() or "{}")


def _normalize_command(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return dict(value or {})


def _send_state() -> None:
    try:
        state = midea.status()
        _request("/api/worker/state", "POST", {"online": True, "state": state})
        print(f"[{datetime.now().isoformat(timespec='seconds')}] status online")
    except Exception as exc:
        _request("/api/worker/state", "POST", {"online": False, "error": str(exc)[:500]})
        print(f"[{datetime.now().isoformat(timespec='seconds')}] status error: {exc}")


def _run_job(job: dict) -> None:
    execution_id = int(job["id"])
    command = _normalize_command(job.get("command"))
    print(f"job #{execution_id}: {command}")
    try:
        result = midea.command(command)
        _request(
            f"/api/worker/{execution_id}/complete",
            "POST",
            {"ok": True, "result": result},
        )
        print(f"job #{execution_id}: success")
        time.sleep(1)
        _send_state()
    except Exception as exc:
        _request(
            f"/api/worker/{execution_id}/complete",
            "POST",
            {"ok": False, "error": str(exc)[:1000]},
        )
        print(f"job #{execution_id}: failed: {exc}")


def main() -> None:
    print("NetHome local worker")
    print(f"Remote: {BASE_URL}")
    print("Press Ctrl+C to stop.")
    last_status = 0.0
    while True:
        now = time.monotonic()
        try:
            if now - last_status >= STATUS_SECONDS:
                _send_state()
                last_status = now

            response = _request("/api/worker/next")
            job = response.get("job")
            if job:
                _run_job(job)
                last_status = time.monotonic()
            else:
                time.sleep(POLL_SECONDS)
        except urllib.error.HTTPError as exc:
            text = exc.read().decode(errors="replace")
            print(f"Worker API HTTP {exc.code}: {text[:300]}")
            time.sleep(5)
        except KeyboardInterrupt:
            print("\nWorker stopped.")
            return
        except Exception as exc:
            print(f"Worker error: {exc}")
            time.sleep(5)


if __name__ == "__main__":
    main()
