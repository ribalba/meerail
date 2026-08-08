"""The wire between meerail installs: one passphrase, an ordered log of sealed records.

IMAP carries mail, and meerail's own promises are not mail. A reminder, the
footer a compose window prefills, the name on an account in the sidebar — none
of those are anything an IMAP server has a place to put, so three installs of
this app against the same accounts agree about the mail and disagree about
everything the app itself decided. That is what this is for.

The shape is deliberately the smallest thing that can settle a disagreement:

**An append-only log, ordered by a server.** Not a document each install edits,
because two installs editing one document need a merge rule for every field and
get it wrong for the field nobody thought about. A record says what happened, the
server says what order it happened in, and every install replaying the same log
in the same order reaches the same state. The ordering is the whole reason there
is a server at all rather than a folder full of messages: appends to a folder
race, and a sequence number does not.

**The server cannot read any of it.** Records are sealed here, on the way out,
with a key derived from the passphrase — which the server never sees. What it
stores is ciphertext and an integer, and what it can answer is "what came after
42". That is not decoration: this is a box somebody rents, holding the subjects
of the mail you have put off and the names of your folders, and the honest way
to run mail state through a machine you do not control is for it to hold bytes
it cannot open.

**Two keys from one passphrase.** One authenticates (the server holds only its
hash, so a stolen server database does not yield a login either), one encrypts.
Both are derived deterministically, because every install has nothing but the
passphrase to start from — there is no enrolment step, no key exchange, nothing
to copy between machines but the phrase itself. Deterministic derivation means a
fixed salt, which means the passphrase carries the whole weight: use a long one.
``suggest_passphrase`` exists so that the answer to "what should I put here" is
never a word somebody thought of.

What is *not* here is anything about reminders, footers or accounts. This module
knows how to seal a dict and unseal it again; app/journal.py knows what the dicts
mean. That split is what makes a second kind of record cost a handler and not a
protocol.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

# Bumped when the envelope changes shape in a way an older install cannot read.
# Records carrying a version this install does not know are skipped rather than
# rejected — a newer machine on the same passphrase must not be able to wedge an
# older one's sync loop, it should only be invisible to it.
WIRE_VERSION = 1

# One scrypt pass over the passphrase, then HKDF for each purpose. The cost is
# paid once per process (see derive's cache), so these are set for a passphrase
# that has to survive an offline attack against a stolen server database rather
# than for a login form: ~32 MB and a fraction of a second.
_SCRYPT_N = 1 << 15
_SCRYPT_R = 8
_SCRYPT_P = 1

# Fixed, and necessarily so: three machines that share only a passphrase have
# nowhere to keep a random salt. Distinct per deployment would be better and is
# not available, which is exactly why the passphrase must be long.
_SALT = b"meerail-journal-v1"

_AUTH_INFO = b"meerail-journal auth token"
_DATA_INFO = b"meerail-journal record key"


class JournalError(Exception):
    """Anything that stops a record being sealed, opened or trusted."""


@dataclass(frozen=True)
class Keys:
    """What one passphrase yields.

    ``token`` is sent to the server on every request; ``space`` is what the
    server files records under, and is a hash of the token rather than the token
    itself so that the value in its config (and in its database) is not a
    credential. ``_data`` never leaves the process.
    """

    token: str
    space: str
    _data: bytes

    def fernet(self) -> Fernet:
        return Fernet(base64.urlsafe_b64encode(self._data))


def _hkdf(master: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=_SALT, info=info).derive(master)


def space_for(token: str) -> str:
    """What the server files this token's records under.

    A hash, so that the server's config file and its database hold something
    that cannot be replayed as a login. Constant-time comparison on the server
    side still matters (an equal-prefix hash is not a useful attack, but the
    check is free); see journal/server.py.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def derive(passphrase: str) -> Keys:
    """Turn the configured passphrase into the two keys and the space name.

    Deliberately not cached across passphrases: an install has one, and a cache
    keyed on the secret is a place for it to outlive a config reload.
    """
    passphrase = (passphrase or "").strip()
    if not passphrase:
        raise JournalError("No journal passphrase is configured")
    if len(passphrase) < 16:
        # Not a policy for its own sake. The salt is fixed and public, so a short
        # phrase here is a short phrase against an attacker holding the whole
        # log — there is no per-install secret behind it to make up the
        # difference.
        raise JournalError(
            "The journal passphrase must be at least 16 characters: it is the only "
            "secret protecting the records, and the key derivation is public. "
            "core.journal.suggest_passphrase() prints a good one.")
    master = Scrypt(salt=_SALT, length=32, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P).derive(
        passphrase.encode())
    token = base64.urlsafe_b64encode(_hkdf(master, _AUTH_INFO)).decode().rstrip("=")
    return Keys(token=token, space=space_for(token), _data=_hkdf(master, _DATA_INFO))


def suggest_passphrase(words: int = 6) -> str:
    """A passphrase worth using, so nobody has to invent one.

    Hex rather than a word list because a word list is another file to ship and
    to agree on; this is copied between three machines once and then forgotten.
    """
    return "-".join(secrets.token_hex(3) for _ in range(words))


# --- Records ---------------------------------------------------------------


def envelope(kind: str, body: dict, *, instance: str, account: str | None = None,
             key: str | None = None, at: datetime | None = None) -> dict:
    """The common outside of every record, whatever it is about.

    ``kind`` picks the handler. ``account`` is an email address and never a local
    account id — the whole point is that the same mailbox has a different row id
    on each machine. ``key`` is the thing the record is *about* within its kind
    (for a reminder, the Message-ID of the conversation), and is what makes two
    records comparable: later record wins, per (kind, account, key).

    ``at`` is the writer's clock and is for display and for nothing else. Order
    is the server's sequence number, because three machines' clocks disagree by
    more than the gap between two reminders being set.
    """
    return {
        "v": WIRE_VERSION,
        "kind": kind,
        "account": account,
        "key": key,
        "instance": instance,
        "at": (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
                .replace(tzinfo=None).isoformat(timespec="seconds"),
        "body": body,
    }


def seal(keys: Keys, record: dict) -> str:
    """One record, as the server will hold it: opaque."""
    return keys.fernet().encrypt(json.dumps(record, separators=(",", ":")).encode()).decode()


def unseal(keys: Keys, blob: str) -> dict | None:
    """Open a record, or None if this install cannot or should not read it.

    Three things land here and none of them is an error worth stopping a sync
    pass for: a record sealed under a different passphrase (somebody pointed a
    fourth machine at the wrong journal), a record from a newer meerail whose
    envelope this build does not understand, and a corrupted blob. All three are
    "not for me", and the pass has to get past them to the records that are.
    """
    try:
        record = json.loads(keys.fernet().decrypt(blob.encode()))
    except (InvalidToken, ValueError):
        return None
    if not isinstance(record, dict) or record.get("v") != WIRE_VERSION:
        return None
    if not isinstance(record.get("kind"), str) or not isinstance(record.get("body"), dict):
        return None
    return record


def token_matches(presented: str, expected: str) -> bool:
    """Constant-time token comparison, for the server side."""
    return hmac.compare_digest(presented or "", expected or "")
