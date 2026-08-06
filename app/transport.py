"""What this server will answer over, decided before it answers anything.

The password gate has a hole no amount of care inside `POST /api/auth/login`
can close. That route refuses a plaintext connection — but a browser only
reaches it after being handed the shell, and the shell draws the password form,
and the password is *in the request being refused*. It has already crossed the
network by the time anything here has an opinion about it. The only useful place
to have the opinion is before the page that collects the password exists, which
means before routing: see app/main.py::require_https.

The decision itself lives here, taking plain values and returning a plain
string, so that "what does this server do about a plaintext request" can be
stated and tested without a request, a server or FastAPI — the same split
app/sessions.py has from app/deps.py, for the same reason.
"""

from __future__ import annotations

# Answer normally: encrypted, on loopback, or an install with no password to
# protect in the first place (the localhost case, and most of them).
SERVE = "serve"

# TLS ended at a proxy this install was not told to trust, so the app cannot
# tell an encrypted request from a plaintext one. Redirecting would send the
# browser to a URL that arrives right back here — a redirect loop standing in
# for a configuration error — so this one gets explained instead.
PROXY_UNTRUSTED = "proxy-untrusted"

# A browser that simply arrived on http://. Send it where it meant to go.
REDIRECT = "redirect"

# Plaintext, and not something that can be safely replayed against another URL:
# a redirect makes the client repeat the request, and the request it would
# repeat is the one carrying the password.
REFUSE = "refuse"

SAFE_METHODS = ("GET", "HEAD")


def plaintext_verdict(*, password_set: bool, secure: bool,
                      forwarded_proto: str = "", method: str = "GET") -> str:
    """What to do with a request, given how it arrived.

    `secure` is the app's own answer to "would this stay off the wire" —
    HTTPS as the app sees it, or loopback (app/deps.py::is_secure_request).
    `forwarded_proto` is the raw X-Forwarded-Proto header, believed by nobody
    here: it is read only to tell an operator who has a proxy and forgot to
    name it apart from a user who typed http:// by hand.
    """
    if secure or not password_set:
        return SERVE
    if "https" in (forwarded_proto or "").lower():
        return PROXY_UNTRUSTED
    if method.upper() in SAFE_METHODS:
        return REDIRECT
    return REFUSE
