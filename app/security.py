"""Small crypto/id helpers.

meerail is a single-user local app, so there is no user auth. These helpers
provide opaque ids/tokens and symmetric encryption for any credential that is
optionally stored server-side (Bridge creds normally live in the agent config).
"""

import base64
import hashlib
import secrets
import uuid

from cryptography.fernet import Fernet, InvalidToken

from core.config import get_settings

settings = get_settings()


def new_id() -> str:
    return str(uuid.uuid4())


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


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
