"""Optional auth gates.

Both are no-ops unless the corresponding secret is configured, so a localhost
install is open by default. Set SERVER_AUTH_TOKEN / AGENT_TOKEN when exposing
the server beyond localhost.
"""

import secrets
import hashlib

from fastapi import Cookie, Header, HTTPException, status

from .config import get_settings

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


def require_agent_auth(x_agent_token: str | None = Header(default=None)) -> None:
    token = settings.agent_token
    if not token:
        return
    if x_agent_token != token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing agent token"
        )
