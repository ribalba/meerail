"""Optional auth gate for the web UI / REST API.

A no-op unless SERVER_AUTH_TOKEN is configured, so a localhost install is open by
default. The agent no longer authenticates here at all — it talks straight to
Postgres, so its credentials are the database's.
"""

import secrets
import hashlib

from fastapi import Cookie, Header, HTTPException, status

from core.config import get_settings

settings = get_settings()


UI_SESSION_COOKIE = "meerail_session"


def ui_session_value() -> str:
    material = f"{settings.secret_key}\0{settings.server_auth_token}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def require_ui_auth(
    authorization: str | None = Header(default=None),
    session: str | None = Cookie(default=None, alias=UI_SESSION_COOKIE),
) -> None:
    token = settings.server_auth_token
    if not token:
        return
    bearer_ok = authorization is not None and secrets.compare_digest(authorization, f"Bearer {token}")
    cookie_ok = session is not None and secrets.compare_digest(session, ui_session_value())
    if not (bearer_ok or cookie_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing server token"
        )
