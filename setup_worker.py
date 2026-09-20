from __future__ import annotations

import getpass
import http.cookiejar
import json
import urllib.request

import keyring

SERVICE = "NetHomeWebControl"
WORKER_TOKEN_KEY = "worker_token"

base = input("NetHome website URL [https://nethome-web-control-six.vercel.app]: ").strip() or "https://nethome-web-control-six.vercel.app"
base = base.rstrip("/")
password = getpass.getpass("Website login password: ")

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

login = urllib.request.Request(
    base + "/api/login",
    data=json.dumps({"password": password}).encode(),
    method="POST",
    headers={"Content-Type": "application/json"},
)
with opener.open(login, timeout=20) as response:
    response.read()

with opener.open(base + "/api/worker/bootstrap-token", timeout=20) as response:
    token = json.loads(response.read().decode())["token"]

keyring.set_password(SERVICE, WORKER_TOKEN_KEY, token)
keyring.set_password(SERVICE, "remote_url", base)
print("Worker connection saved securely in Windows Credential Manager.")
