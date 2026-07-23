"""Optional auth gate for the web UI / REST API.

A no-op unless a password is configured (SERVER_PASSWORD), so a localhost
install is open by default. The agent no longer authenticates here at all — it
talks straight to Postgres, so its credentials are the database's.

Browsers authenticate with a signed, expiring session cookie issued by
POST /api/auth/login (see app.sessions for the token format); scripted clients
can instead send the password itself as `Authorization: Bearer <password>`.
"""

from fastapi import Cookie, Header, HTTPException, status

from core.config import get_settings
from . import sessions

settings = get_settings()


UI_SESSION_COOKIE = "meerail_session"


def ui_password() -> str:
    return settings.server_password


def session_max_age_seconds() -> int:
    return settings.session_max_age_days * 86400


def issue_ui_session() -> str:
    return sessions.issue_token(settings.secret_key, ui_password(), session_max_age_seconds())


def require_ui_auth(
    authorization: str | None = Header(default=None),
    session: str | None = Cookie(default=None, alias=UI_SESSION_COOKIE),
) -> None:
    password = ui_password()
    if not password:
        return
    bearer_ok = authorization is not None and sessions.constant_time_eq(
        authorization, f"Bearer {password}"
    )
    cookie_ok = session is not None and sessions.verify_token(
        session, settings.secret_key, password
    )
    if not (bearer_ok or cookie_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
        )
