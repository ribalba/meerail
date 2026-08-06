"""Browser session bootstrap for installations protected by a server password.

Login exchanges the password for a signed session cookie that lasts
session_max_age_days (30 by default), so the browser asks once, not per visit.
The cookie names a session row, so Log out ends it for good rather than only
clearing the browser's copy — see app/deps.py.

Failed attempts are rate-limited per source address — this endpoint is the one
surface an internet-exposed install lets strangers hammer on.
"""

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session as DBSession

from core.config import get_settings
from core.database import get_db
from .. import sessions
from ..deps import (
    UI_SESSION_COOKIE, client_addr, is_secure_request, issue_ui_session,
    revoke_ui_session, session_max_age_seconds, ui_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()

login_limiter = sessions.LoginRateLimiter()


class LoginRequest(BaseModel):
    password: str


@router.get("/status")
def auth_status() -> dict:
    return {"required": bool(ui_password())}


@router.post("/login", status_code=204)
def login(payload: LoginRequest, request: Request, response: Response,
          db: DBSession = Depends(get_db)) -> None:
    expected = ui_password()
    if not expected:
        # Nothing to log in to — an open install has no session to issue.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No password is configured")

    if not is_secure_request(request):
        # The backstop, not the guard. app/main.py refuses to serve the shell
        # over a plaintext connection at all, precisely because by the time a
        # request reaches here the damage is done: the password is *in this
        # request*, and refusing it does not take it back off the wire. This
        # catches a client that got here some other way — a script, a stale tab
        # loaded while the install was still on HTTPS.
        #
        # The Secure flag alone cannot help either: it is set from this same
        # scheme, so a plain-HTTP deployment quietly issued a cookie that
        # browsers were then free to send in the clear.
        #
        # An install that is genuinely reachable over HTTP only, and wants a
        # password anyway, has two ways through: put TLS in front of it, or tell
        # meerail about the proxy that already terminates TLS
        # (server.trusted_proxies), which is what makes the scheme above true.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This connection is not encrypted, so signing in would send the "
                   "password and the session cookie in the clear. Reach meerail over "
                   "HTTPS — and if TLS is terminated by a proxy in front of it, set "
                   "server.trusted_proxies so it can see that.",
        )

    addr = client_addr(request)
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
        issue_ui_session(db),
        max_age=session_max_age_seconds(),
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )


@router.post("/logout", status_code=204)
def logout(response: Response,
           session: str | None = Cookie(default=None, alias=UI_SESSION_COOKIE),
           db: DBSession = Depends(get_db)) -> None:
    """End this session — here, not only in the browser asking.

    Deleting the cookie is the half a client can do for itself, and on its own
    it was all this did: a copy of the cookie taken off the machine went on
    working for the rest of its thirty days, because nothing recorded that the
    session had ended. The row goes too, and every request checks for it.
    """
    revoke_ui_session(db, session)
    response.delete_cookie(UI_SESSION_COOKIE, path="/")
