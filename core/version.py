"""The one place meerail's version number is defined.

The number lives in the ``VERSION`` file at the repository root — a plain file
rather than a Python constant so that everything else can read it without a
Python interpreter: the Makefile tags images with it, the CI workflow pushes
``ribalba/meerail-server:$(cat VERSION)``, and the running server compares
itself against the copy on ``main`` to tell you an update is out.

Both images COPY it to ``/app/VERSION`` and also bake it into
``MEERAIL_VERSION`` at build time. The environment wins, which is what makes a
build reproducible from a git archive that has no VERSION file — and what lets
a CI build stamp a pre-release number without editing the tree.

Bumping it is the release: CI builds ``:<version>`` and moves ``:latest``, and
every running install notices within a day (see app/updates.py).
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
VERSION_FILE = BASE_DIR / "VERSION"

# What a version is when we genuinely do not know — someone running from a
# tarball with no VERSION file and no build stamp. Deliberately *not* "0.0.0":
# it must not compare as merely old, because `is_outdated` would then nag on
# every page load with nothing useful to say.
UNKNOWN = "unknown"


def _read() -> str:
    stamped = os.environ.get("MEERAIL_VERSION", "").strip()
    if stamped:
        return stamped
    try:
        text = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return UNKNOWN
    return text or UNKNOWN


VERSION = _read()


def parse(value: str) -> tuple[int, ...] | None:
    """The leading dotted-integer part of a version, or None if there isn't one.

    Tolerant on purpose — ``v1.2.3``, ``1.2.3-rc1`` and ``1.2.3+dirty`` all
    parse to ``(1, 2, 3)``, because the pre-release and build parts say nothing
    about *ordering* that this comparison needs to get right. Anything with no
    numeric head at all (``"unknown"``, a git sha) returns None, and every
    caller treats that as "no opinion" rather than as zero.
    """
    head = (value or "").strip().lstrip("vV")
    parts: list[int] = []
    for chunk in head.split("."):
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
        if len(digits) != len(chunk):
            # Hit the suffix (``3-rc1``): that component counted, the rest is
            # not part of the number.
            break
    return tuple(parts) or None


def is_outdated(current: str, latest: str) -> bool:
    """Is `current` strictly older than `latest`?

    False whenever either side is unparseable, and false for equal versions —
    the caller shows a banner off this, so silence is the right answer to any
    uncertainty. Shorter tuples are zero-extended so 1.2 == 1.2.0.
    """
    a, b = parse(current), parse(latest)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    a += (0,) * (width - len(a))
    b += (0,) * (width - len(b))
    return a < b


if __name__ == "__main__":
    # `python -m core.version` — used by the Makefile and CI to tag images,
    # so that the number is read from exactly one place by everyone.
    print(VERSION)
