"""Optional auth gate for the web UI / REST API.

A no-op unless a password is configured (SERVER_PASSWORD), so a localhost
install is open by default. The agent no longer authenticates here at all — it
talks straight to Postgres, so its credentials are the database's.

Browsers authenticate with a signed session cookie issued by
POST /api/auth/login, which names a row this server keeps so that logging out
can end it. Scripted clients send a separate API token as
`Authorization: Bearer <token>` — separate on purpose: the UI password is typed
into a browser and is not a credential to hand to a cron job, and a token that
is not the password can be changed without logging every browser out.

The decision itself lives in app/sessions.py, which imports no FastAPI and can
therefore be tested against a database without a server. This file is the
adapter: a request in, a 401 out.
"""

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session as DBSession

from core.config import get_settings
from core.database import get_db
from . import sessions

settings = get_settings()


UI_SESSION_COOKIE = "meerail_session"

# Addresses whose traffic never leaves the machine. A browser on the same host
# as the server is not sending anything over a network, so plain HTTP there is
# not cleartext in any sense that matters — which is the whole localhost
# install, and the reason the rule below is not simply "https or nothing".
LOOPBACK = ("127.0.0.1", "::1", "localhost")


def client_addr(request: Request) -> str:
    """The peer address, or — where the request came through a proxy this install
    was told to trust — the client that proxy is speaking for.

    Set `trusted_proxies` on any deployment with TLS terminated in front of it,
    or this is the proxy's own address for everyone and one attacker's five
    wrong passwords lock out every user behind it. See app/main.py.
    """
    return request.client.host if request.client else "unknown"


def is_secure_request(request: Request) -> bool:
    """Would this request's password and cookie stay off the wire?

    True over HTTPS — as the app sees it, which behind a proxy means the
    forwarded scheme from an address `trusted_proxies` names, and from anywhere
    else means the scheme of the connection itself. True on loopback, where
    there is no wire.

    Read in two places, and they have to agree: app/main.py refuses to serve the
    UI at all where this is false, and app/routers/auth.py refuses to take a
    password. The second is the backstop for a request that reached a route
    some other way; the first is what stops the form being drawn.
    """
    return request.url.scheme == "https" or client_addr(request) in LOOPBACK


def ui_password() -> str:
    return settings.server_password


def api_token() -> str:
    """The credential for scripted clients, or empty for "no API access"."""
    return settings.api_token


def session_max_age_seconds() -> int:
    return settings.session_max_age_days * 86400


def issue_ui_session(db: DBSession) -> str:
    return sessions.start(db, settings.secret_key, ui_password(), session_max_age_seconds())


def revoke_ui_session(db: DBSession, token: str | None) -> None:
    sessions.revoke(db, token, settings.secret_key, ui_password())


def require_ui_auth(
    authorization: str | None = Header(default=None),
    session: str | None = Cookie(default=None, alias=UI_SESSION_COOKIE),
    db: DBSession = Depends(get_db),
) -> None:
    if sessions.authorize(db, secret_key=settings.secret_key, password=ui_password(),
                          api_token=api_token(), authorization=authorization,
                          cookie=session):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
    )
