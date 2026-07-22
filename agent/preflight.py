"""Connection checks for `main.py --test`.

Answers one question: can the agent reach everything it needs before you trust
it with a sync? Each check is independent and read-only — nothing is written to
the database, no schema is created, and no mail is sent or modified. A check
that fails reports why rather than raising, so one broken account doesn't hide
the state of the rest.
"""

from __future__ import annotations

import os
import socket
import ssl
import stat
import sys
import time
from dataclasses import dataclass

from config import AccountConfig, AgentConfig

# Anything slower than this is broken for our purposes; without it a silently
# dropped packet hangs the check until the OS gives up (minutes).
TIMEOUT = 15.0

# Colour only for a terminal, so piping to a file or CI log stays readable.
_COLOUR = sys.stdout.isatty()


def _badge(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOUR else text


OK = _badge("  OK  ", "32")
FAIL = _badge(" FAIL ", "31")
WARN = _badge(" WARN ", "33")


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""
    warn: bool = False   # reachable, but something is worth saying


def _fmt(r: Result) -> str:
    badge = WARN if (r.ok and r.warn) else (OK if r.ok else FAIL)
    line = f"[{badge}] {r.name}"
    return f"{line}\n         {r.detail}" if r.detail else line


def _friendly(exc: Exception) -> str:
    """Turn the usual connection failures into something actionable."""
    # SQLAlchemy and httpx wrap the real socket error and stringify to a
    # multi-line dump with a docs URL; dig out the cause they're hiding.
    root = exc
    for _ in range(5):
        cause = getattr(root, "__cause__", None) or getattr(root, "__context__", None)
        if cause is None or cause is root:
            break
        root = cause

    if isinstance(root, (socket.timeout, TimeoutError)):
        return f"timed out after {TIMEOUT:.0f}s — is the host reachable, and the port right?"
    if isinstance(root, ConnectionRefusedError):
        return "connection refused — nothing is listening there"
    if isinstance(root, socket.gaierror):
        return f"cannot resolve host ({root})"
    if isinstance(root, ssl.SSLError):
        return (f"TLS error ({root}) — check the security mode, or set "
                f"verify_cert = false for Bridge's self-signed certificate")

    # Drivers (psycopg) raise their own exception types carrying the socket
    # error only as text, so fall back to matching on the message.
    text = str(root).strip()
    lowered = text.lower()
    if "connection refused" in lowered:
        return "connection refused — nothing is listening there"
    if "timed out" in lowered or "timeout" in lowered:
        return f"timed out after {TIMEOUT:.0f}s — is the host reachable, and the port right?"
    if "authentication" in lowered or "password" in lowered:
        return f"authentication rejected — {text.splitlines()[0]}"

    # Last resort: one line only, without SQLAlchemy's trailing docs URL.
    first = text.splitlines()[0] if text else repr(root)
    return f"{type(root).__name__}: {first}"


def check_config_permissions(cfg: AgentConfig) -> Result:
    """The config holds mail passwords in plaintext, so nobody else may read it."""
    name = "Config file"
    path = cfg.config_path
    if path is None:
        return Result(name, True, "path unknown — skipped", warn=True)

    # Windows has no POSIX mode bits; Python synthesises st_mode as 0666 (or
    # 0444 when read-only), which would fail the group/other test below for
    # every file and say something untrue about who can read it. NTFS ACLs are
    # the real answer there and we don't inspect them — say so and move on.
    if os.name == "nt":
        return Result(name, True,
                      f"{path}\n         Windows — NTFS ACLs not checked; "
                      f"verify with `icacls` that only you can read it",
                      warn=True)

    try:
        st = path.stat()
    except OSError as exc:
        return Result(name, False, f"{path}\n         cannot stat: {exc}")

    mode = stat.S_IMODE(st.st_mode)
    # Only group/other bits matter: 0600 and 0400 are both fine, 0644 is not.
    exposed = mode & 0o077
    if exposed:
        who = []
        if mode & 0o070:
            who.append("group")
        if mode & 0o007:
            who.append("others")
        return Result(name, False,
                      f"{path}\n         mode {mode:04o} — readable by {' and '.join(who)}, "
                      f"and it stores your mail passwords in plaintext"
                      f"\n         fix with: chmod 600 {path}")
    return Result(name, True, f"{path}\n         mode {mode:04o} — owner only")


def check_database(cfg: AgentConfig) -> Result:
    """Connect, round-trip a query, and report whether the schema is present."""
    # Imported here, not at module top: core.config snapshots the environment on
    # import, and load_config must have populated it first (see main.py).
    from sqlalchemy import inspect, text

    from core.database import engine

    # The URL carries the password; show only enough to identify the target.
    try:
        target = engine.url.render_as_string(hide_password=True)
    except Exception:
        target = cfg.database_url

    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version()")).scalar() or ""
            has_trgm = conn.execute(text(
                "SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'"
            )).scalar() is not None
            tables = set(inspect(conn).get_table_names())
    except Exception as exc:
        return Result("Database", False, f"{target}\n         {_friendly(exc)}")

    server = version.split(" on ")[0] if version else "connected"
    detail = f"{target}\n         {server}"

    if "messages" not in tables:
        # Not an error: the agent creates the schema on a normal (non --test) run.
        return Result("Database", True, detail + "\n         schema not initialised yet — "
                      "the first real run will create it", warn=True)
    if not has_trgm:
        return Result("Database", True, detail + "\n         pg_trgm extension missing — "
                      "regex search will be slow until the next run creates it", warn=True)
    return Result("Database", True, detail + f"\n         schema present ({len(tables)} tables)")


def check_tika(cfg: AgentConfig) -> Result:
    """Tika is optional-ish: without it, attachments simply aren't searchable."""
    import httpx

    url = cfg.tika_url.rstrip("/")
    try:
        resp = httpx.get(url + "/version", timeout=TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:
        return Result("Tika", True, f"{url}\n         {_friendly(exc)}\n         "
                      "attachment text extraction will be skipped — mail still syncs",
                      warn=True)
    return Result("Tika", True, f"{url}\n         {resp.text.strip()}")


def _encryption(sock: object, configured: str) -> tuple[str, bool]:
    """Describe what the socket actually negotiated, and whether that's a problem.

    Deliberately inspects the live socket instead of trusting the config: the
    point of this check is to catch a connection that *says* it is encrypted and
    isn't. Returns (description, credentials_exposed).
    """
    if isinstance(sock, ssl.SSLSocket):
        version = sock.version() or "TLS"
        return version, False
    # plain is an explicit, informed choice; the other modes promised encryption.
    return "unencrypted", configured != "plain"


def check_imap(account: AccountConfig) -> Result:
    """Connect, authenticate, and list folders — the same path a sync takes."""
    from imap import Bridge

    name = f"IMAP   {account.email}"
    target = f"{account.imap_host}:{account.imap_port} ({account.imap_security})"
    bridge = Bridge(account)
    started = time.monotonic()
    try:
        bridge.connect()
        folders = bridge.list_folders()
        crypto, exposed = _encryption(bridge.client._imap.sock, account.imap_security)
    except Exception as exc:
        return Result(name, False, f"{target}\n         {_friendly(exc)}")
    finally:
        bridge.logout()

    ms = int((time.monotonic() - started) * 1000)
    detail = f"{target}\n         authenticated in {ms}ms over {crypto}, {len(folders)} folders"
    # role_hint carries the raw SPECIAL-USE flag ("\\Sent"); "" for plain folders.
    roles = ", ".join(sorted({f["role_hint"].lstrip("\\").lower()
                              for f in folders if f.get("role_hint")}))
    if roles:
        detail += f" ({roles})"
    if exposed:
        return Result(name, True, detail + f"\n         config asks for "
                      f"{account.imap_security} but the connection is NOT encrypted — "
                      f"your password was sent in the clear", warn=True)
    if not folders:
        return Result(name, True, detail + "\n         the server listed no folders", warn=True)
    return Result(name, True, detail)


def check_smtp(account: AccountConfig) -> Result:
    """Connect and authenticate only — deliberately sends nothing."""
    import smtp

    name = f"SMTP   {account.email}"
    target = f"{account.smtp_host}:{account.smtp_port} ({account.smtp_security})"
    started = time.monotonic()
    try:
        server = smtp.connect(account, timeout=TIMEOUT)
    except Exception as exc:
        return Result(name, False, f"{target}\n         {_friendly(exc)}")
    try:
        ms = int((time.monotonic() - started) * 1000)
        crypto, exposed = _encryption(server.sock, account.smtp_security)
        sendable = ", ".join(account.send_addresses())
        detail = (f"{target}\n         authenticated in {ms}ms over {crypto}"
                  f"\n         can send as: {sendable}")
        if exposed:
            return Result(name, True, detail + f"\n         config asks for "
                          f"{account.smtp_security} but the connection is NOT encrypted — "
                          f"your password was sent in the clear", warn=True)
        return Result(name, True, detail)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def run(cfg: AgentConfig) -> int:
    """Run every check, print a report, and return a process exit code."""
    print("meerail-agent connection test\n")

    results = [check_config_permissions(cfg), check_database(cfg), check_tika(cfg)]
    for r in results:
        print(_fmt(r))

    for account in cfg.accounts:
        print()
        for check in (check_imap, check_smtp):
            r = check(account)
            results.append(r)
            print(_fmt(r))

    failed = [r for r in results if not r.ok]
    warned = [r for r in results if r.ok and r.warn]
    print()
    if failed:
        print(f"{len(failed)} of {len(results)} checks failed: "
              f"{', '.join(r.name.strip() for r in failed)}")
        return 1
    if warned:
        print(f"All {len(results)} checks passed, {len(warned)} with warnings.")
        return 0
    print(f"All {len(results)} checks passed.")
    return 0
