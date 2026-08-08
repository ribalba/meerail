"""Turn a passphrase into the one line the journal server needs.

    python -m journal.keys                 # invent a passphrase and print both halves
    python -m journal.keys "my phrase..."  # print the server line for an existing one

The passphrase goes into every meerail install's config; what this prints for the
server is a *hash* of the derived token, so the machine holding the log never has
the passphrase, the token, or anything that can be replayed as either.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.journal import derive, suggest_passphrase  # noqa: E402


def main(argv: list[str]) -> int:
    passphrase = argv[1] if len(argv) > 1 else suggest_passphrase()
    invented = len(argv) <= 1
    keys = derive(passphrase)

    if invented:
        print("Generated passphrase (put this in every meerail install):\n")
    else:
        print("Passphrase (put this in every meerail install):\n")
    print(f"  [journal]\n  passphrase = \"{passphrase}\"\n")
    print("Server configuration — this is a hash, not a credential:\n")
    print(f"  JOURNAL_SPACES={keys.space}\n")
    print("The server never learns the passphrase, so it cannot read any record.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
