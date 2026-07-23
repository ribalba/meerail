"""Browser session bootstrap for installations protected by a server password.

Login exchanges the password for a signed session cookie that lasts
session_max_age_days (30 by default), so the browser asks once, not per visit.
Failed attempts are rate-limited per source address — this endpoint is the one
surface an internet-exposed install lets strangers hammer on.
"""

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel

from core.config import get_settings
from .. import sessions
from ..deps import UI_SESSION_COOKIE, issue_ui_session, session_max_age_seconds, ui_password

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()

login_limiter = sessions.LoginRateLimiter()


class LoginRequest(BaseModel):
    password: str


def _client_addr(request: Request) -> str:
    # Direct peer address. Behind a reverse proxy run uvicorn with
    # --proxy-headers so this is the real client, not the proxy — otherwise the
    # limiter would lock every user out together after one attacker's failures.
    return request.client.host if request.client else "unknown"


@router.get("/status")
def auth_status() -> dict:
    return {"required": bool(ui_password())}


@router.post("/login", status_code=204)
def login(payload: LoginRequest, request: Request, response: Response) -> None:
    expected = ui_password()
    if not expected:
        # Nothing to log in to — an open install has no session to issue.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No password is configured")

    addr = _client_addr(request)
    retry_after = login_limiter.retry_after(addr)
    if retry_after:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts — try again later",
            headers={"Retry-After": str(retry_after)},
        )

    if not sessions.constant_time_eq(payload.password, expected):
        login_limiter.record_failure(addr)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Wrong password")

    login_limiter.reset(addr)
    response.set_cookie(
        UI_SESSION_COOKIE,
        issue_ui_session(),
        max_age=session_max_age_seconds(),
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )


@router.post("/logout", status_code=204)
def logout(response: Response) -> None:
    response.delete_cookie(UI_SESSION_COOKIE, path="/")
