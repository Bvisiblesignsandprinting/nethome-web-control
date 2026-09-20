from __future__ import annotations

import getpass
import http.cookiejar
import json
import ssl
import urllib.request

import keyring

SERVICE = "NetHomeWebControl"
WORKER_TOKEN_KEY = "worker_token"

base = input("NetHome website URL [https://nethome-web-control-six.vercel.app]: ").strip() or "https://nethome-web-control-six.vercel.app"
base = base.rstrip("/")
password = getpass.getpass("Website login password: ")

jar = http.cookiejar.CookieJar()

# Python 3.14 enables stricter X.509 checks that reject some otherwise
# Windows-trusted certificate chains (for example, chains missing AKI).
# Keep normal certificate verification, but disable only X509_STRICT.
context = ssl.create_default_context()
if hasattr(ssl, "VERIFY_X509_STRICT"):
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT

opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(jar),
    urllib.request.HTTPSHandler(context=context),
)

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
