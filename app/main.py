import asyncio
from pathlib import Path

from fastapi import Depends, FastAPI, Request, status
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from . import events, transport
from core.config import get_settings, trusted_proxy_hosts
from core.database import engine, init_db
from core.version import VERSION
from .limits import MaxBodySize
from .routers import (
    accounts, actions, analytics, auth, compose, contacts, mailboxes, messages, outbox,
    reminders, search, stream, sync, tasks, undo, version,
)
from .deps import is_secure_request, require_ui_auth, ui_password
from .workers import contacts_loop, reminders_loop

settings = get_settings()
STATIC_DIR = Path(__file__).resolve().parent / "static"

# One version number for the whole project, read from the VERSION file at the
# repository root (core/version.py) — the same one the images are tagged with.
#
# FastAPI's own docs routes are off. /openapi.json is served below behind the
# same gate as everything else — it gives away no mail, and it does give a
# stranger the complete list of what to try. The two HTML pages that render it
# are not served at all: both are a CDN script tag plus an inline bootstrap, and
# this app's Content-Security-Policy permits neither. Pointing any OpenAPI
# client at /openapi.json is what replaces them.
app = FastAPI(title="meerail", version=VERSION,
              docs_url=None, redoc_url=None, openapi_url=None)


# A body no larger than the route could possibly want, decided from the headers
# before FastAPI reads a byte of it. Added first, so it sits innermost of the
# middlewares below and its refusal still comes back out through the headers
# one. app/limits.py has the whole reasoning; the short version is that
# dependencies — and so `require_ui_auth` — run *after* the body is parsed, so
# without this a stranger could spool 100 MB to disk per request on a
# password-protected install and only then be told no.
app.add_middleware(
    MaxBodySize,
    default_bytes=settings.max_request_bytes,
    overrides={"/api/compose/attachments": settings.max_attachment_bytes + (1 << 20)},
)


@app.middleware("http")
async def require_https(request: Request, call_next):
    """Do not serve anything over a connection the password cannot survive.

    POST /api/auth/login refuses a plaintext connection, and on its own that was
    too late by a whole round trip: the browser had already been handed the
    shell, the shell had already drawn the password form, and the password was
    *in the request being refused*. Whatever the login route does with it, it
    has crossed the network in the clear. The only fix is to not serve the page
    that collects it, which has to happen here — before routing, before a body
    is read, before any of it exists.

    Only on an install that has a password to protect. An open one is the
    localhost install this app is mostly for, and forcing TLS on it would break
    the thing it is. Loopback counts as encrypted for the same reason it does in
    the login guard: there is no wire. `request.url.scheme` is the *forwarded*
    scheme wherever a trusted proxy said so, which is what makes this correct on
    a stack whose TLS ends at Traefik.

    The rule is in app/transport.py, which imports no FastAPI and is unit-tested
    without one; this is the adapter that turns its answer into a response.
    """
    verdict = transport.plaintext_verdict(
        password_set=bool(ui_password()),
        secure=is_secure_request(request),
        forwarded_proto=request.headers.get("x-forwarded-proto", ""),
        method=request.method,
    )
    if verdict == transport.SERVE:
        return await call_next(request)
    if verdict == transport.PROXY_UNTRUSTED:
        return JSONResponse(
            status_code=status.HTTP_421_MISDIRECTED_REQUEST,
            content={"detail": (
                "This request arrived over HTTPS, but meerail was not told to believe the "
                "proxy that terminated it — so it cannot tell an encrypted connection from "
                "a plaintext one, and will not hand out the UI or a session until it can. "
                "Set server.trusted_proxies (TRUSTED_PROXIES) to the address or CIDR block "
                "your reverse proxy reaches this server from."
            )},
        )
    if verdict == transport.REDIRECT:
        # Temporary, not the 301 this is conventionally done with. A permanent
        # redirect is cached by the browser for as long as it likes, and this
        # one points at an https:// endpoint that — on an install reached
        # directly on its port rather than through a proxy — may not exist. The
        # claim that this hostname is HTTPS-only belongs to the HSTS header
        # below, which is sent only over a connection that has proved it.
        return RedirectResponse(
            str(request.url.replace(scheme="https")),
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        )
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": (
            "This connection is not encrypted. Reach meerail over HTTPS — and if TLS is "
            "terminated by a proxy in front of it, set server.trusted_proxies so it can "
            "see that."
        )},
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Headers the whole app answers with, whatever the route.

    The endpoints that hand back a sender\'s bytes set their own, stricter, on
    top of these (see messages.py) — this is the floor, and it is about the
    application shell:

      * `frame-ancestors \'none\'` — nothing may frame meerail. A mail client in
        an invisible frame is a clickjacking target with Empty Trash in it.
      * `default-src \'self\'` with no `unsafe-inline` — the UI is a set of .js
        and .css files served from here and nothing else, so anything injected
        into the page has no origin it may fetch from and no inline script it may
        run. `frame-src`/`img-src` allow the blobs and data: URIs the reader
        builds for message bodies and previews.
      * `nosniff`, so a Content-Type this app chose is the one the browser uses.
      * `no-referrer`, so a message that links out does not name this install to
        the far end.
      * `Strict-Transport-Security` over HTTPS, so the *next* visit does not
        start on http:// at all. require_https above turns that first plaintext
        request away, but only after it has been made — with a hostname the
        browser remembers, there is no plaintext request to turn away. Sent
        only over HTTPS, which is the only transport a browser will accept it
        on, and only where the operator left it on (server.hsts_max_age_days).

    `includeSubDomains` is deliberately not sent. It would be the stronger
    header and it is not this app's to decide: an install at the apex of a
    domain would be pinning every unrelated service beside it to HTTPS for a
    year, from a mail client that knows nothing about them. Add it at the proxy
    if the domain is yours to commit.
    """
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if request.url.scheme == "https" and settings.hsts_max_age_days > 0:
        response.headers.setdefault(
            "Strict-Transport-Security", f"max-age={settings.hsts_max_age_days * 86400}"
        )
    return response


_CSP = "; ".join([
    "default-src \'self\'",
    # Message bodies are rendered into a sandboxed iframe from a blob: URL, and
    # attachment previews are data: URIs the page builds itself.
    "frame-src \'self\' blob: data:",
    # Remote images too, because a `srcdoc` iframe inherits this policy and the
    # reader\'s "load remote content" would otherwise be a button that does
    # nothing. What stops a tracking pixel is the sanitizer taking its URL out
    # of the message (app/mail/render.py), not this line — CSP was never that
    # mechanism, and pretending it was would only break the case where the user
    # has said yes.
    "img-src \'self\' blob: data: https: http:",
    # The reader\'s iframe carries the sender\'s own markup, sanitised, and mail
    # is laid out in inline CSS — see app/mail/render.py, which is what makes
    # that safe to allow.
    "style-src \'self\' \'unsafe-inline\'",
    "frame-ancestors \'none\'",
    "base-uri \'self\'",
    "form-action \'self\'",
])


# Believe X-Forwarded-For / X-Forwarded-Proto, but only from the addresses the
# operator named (core/config.py::trusted_proxies). Two things downstream read
# what this writes and are wrong without it on a deployment that terminates TLS
# in front: the login cookie's Secure flag, which follows request.url.scheme,
# and the login rate limiter, which counts failures per source address and would
# otherwise count every user behind the proxy as the same one.
#
# Configured here rather than as a uvicorn flag so it holds however the app is
# started — compose, a container image's CMD, or a developer's own uvicorn — and
# so the behaviour can be tested without a server (tests/test_proxy_unit.py).
_PROXIES = trusted_proxy_hosts(settings.trusted_proxies)
if _PROXIES:
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=_PROXIES)


@app.on_event("startup")
async def _startup() -> None:
    # Staging for outgoing attachments — the only thing left on disk, and the
    # server is the only process that writes it (core/config.py says why the
    # shared loader no longer does).
    settings.outbox_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    events.set_loop(asyncio.get_running_loop())
    # Mail ingest (and its Tika extraction) runs in the agent; the app only
    # listens for the resulting change notifications.
    asyncio.create_task(events.listener_loop())
    asyncio.create_task(contacts_loop())
    # The only clock in the app: mail parked on a reminder comes back from here,
    # including the reminders that fell due while this server was not running.
    asyncio.create_task(reminders_loop())


app.include_router(accounts.router)
app.include_router(auth.router)
app.include_router(mailboxes.router)
app.include_router(messages.router)
app.include_router(actions.router)
app.include_router(compose.router)
app.include_router(outbox.router)
app.include_router(reminders.router)
app.include_router(contacts.router)
app.include_router(search.router)
app.include_router(analytics.router)
app.include_router(sync.router)
app.include_router(tasks.router)
app.include_router(undo.router)
app.include_router(stream.router)
app.include_router(version.router)


@app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(require_ui_auth)])
def openapi_schema() -> JSONResponse:
    """The API description — for whoever may already read the API.

    The whole of the interactive docs, and the only part of them that was ever
    served correctly. /docs and /redoc used to sit beside this: FastAPI builds
    both out of a script tag pointing at cdn.jsdelivr.net plus an inline
    bootstrap, and the policy this app sends allows neither, so each rendered as
    a blank page with two console errors. Loosening the policy for them would
    have put a third party's JavaScript on the same origin as the mailbox, and
    vendoring a megabyte of Swagger to avoid that is a lot of repository for two
    pages nobody reads daily. Point any OpenAPI client at this instead.
    """
    return JSONResponse(get_openapi(title=app.title, version=app.version, routes=app.routes))


@app.get("/healthz")
def healthz() -> dict:
    # The database name (never the URL — that carries the password) lets the
    # test suite refuse to assert against a production server; see
    # tests/conftest.py::pytest_configure.
    return {"ok": True, "database": engine.url.database}


class NoCacheStatic(StaticFiles):
    """Static assets that always revalidate.

    Without a Cache-Control header browsers fall back to heuristic freshness
    (roughly a tenth of the file's age), so a long-untouched js file can be
    served from cache for hours while a freshly edited one is refetched. The
    front end is a set of modules that call into each other, so half-stale is
    worse than stale: app.keys.js calling an App.reader function its cached
    copy does not have yet is a TypeError, not a missing feature. ETags still
    make the revalidation a 304, so this costs a round trip, not the payload.
    """

    def file_response(self, *args, **kwargs) -> Response:
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


# Static assets (css/js/img).
app.mount("/static", NoCacheStatic(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    # Same reasoning as NoCacheStatic: the shell must not pin an old asset set.
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})
