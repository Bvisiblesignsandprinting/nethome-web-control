from __future__ import annotations

import os

SERVICE = "NetHomeWebControl"
ACCOUNT_KEY = "account"
PASSWORD_KEY = "password"


def save_credentials(account: str, password: str) -> None:
    import keyring
    keyring.set_password(SERVICE, ACCOUNT_KEY, account)
    keyring.set_password(SERVICE, PASSWORD_KEY, password)


def load_credentials() -> tuple[str | None, str | None]:
    env_account = os.getenv("NETHOME_ACCOUNT")
    env_password = os.getenv("NETHOME_PASSWORD")
    if env_account and env_password:
        return env_account, env_password
    try:
        import keyring
        return (
            keyring.get_password(SERVICE, ACCOUNT_KEY),
            keyring.get_password(SERVICE, PASSWORD_KEY),
        )
    except Exception:
        return None, None
