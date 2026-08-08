"""Symmetric encryption for the credentials this app stores itself.

meerail is a single-user local app and there is no user auth, so the only secret
that ever lands in the database is the one the Tasks integration needs: an API
token for an external tracker, which the user pastes into Settings and which has
to be replayed verbatim on every call. Mail credentials are not here at all —
those live in the agent's own config, on the host, and never reach this process.

Both functions are used from app/routers/tasks.py and nowhere else. Two id
helpers used to sit beside them (``new_id``, ``new_token``) for a server-side
credential store that was never built; nothing called either.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from core.config import get_settings

settings = get_settings()


def _fernet() -> Fernet:
    # Derive a stable 32-byte Fernet key from the configured secret.
    digest = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None
