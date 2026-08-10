"""Unit coverage for reading "may a folder hold a folder" off an IMAP LIST.

Pure unit test: `_folder_capabilities` takes the rows imapclient hands back and
nothing else, so a list of tuples stands in for a mail server.

The behaviour it pins is the one that made this necessary. The New Folder box
used to refuse any name with a "/" in it, everywhere, on Proton Bridge's
reasoning — there every user folder really does come back \\Noinferiors. Most
people have a Bridge account *and* something else, and on Gmail or a university
Dovecot that refusal was denying a thing the server does perfectly well.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from imap import _folder_capabilities, _user_folder_parent


def row(name, *flags, delim=b"/"):
    """One LIST row, in imapclient's (flags, delimiter, name) shape."""
    return ([f.encode() for f in flags], delim, name)


# Proton Bridge: user folders live under a \Noselect "Folders" node and every
# one of them is \Noinferiors.
BRIDGE = [
    row("INBOX", "\\HasNoChildren"),
    row("Folders", "\\Noselect", "\\HasChildren"),
    row("Folders/Receipts", "\\Noinferiors"),
    row("Folders/Haus", "\\Noinferiors"),
    row("Labels", "\\Noselect", "\\HasChildren"),
    row("Labels/Important", "\\Noinferiors"),
]

# Gmail over IMAP: a \Noselect "[Gmail]" node for the special mailboxes, user
# labels at the root, nothing marked \Noinferiors.
GMAIL = [
    row("INBOX", "\\HasNoChildren"),
    row("[Gmail]", "\\Noselect", "\\HasChildren"),
    row("[Gmail]/All Mail", "\\All", "\\HasNoChildren"),
    row("[Gmail]/Sent Mail", "\\Sent", "\\HasNoChildren"),
    row("rechnungen", "\\HasNoChildren"),
]

# A Dovecot install with the other common delimiter, and folders already nested.
DOVECOT = [
    row("INBOX", "\\HasNoChildren", delim=b"."),
    row("INBOX.Archive", "\\HasChildren", delim=b"."),
    row("INBOX.Archive.2024", "\\HasNoChildren", delim=b"."),
]


def test_bridge_cannot_nest():
    caps = _folder_capabilities(BRIDGE)
    assert caps == {"delimiter": "/", "nesting": False}


def test_gmail_can_nest():
    caps = _folder_capabilities(GMAIL)
    assert caps == {"delimiter": "/", "nesting": True}


def test_dovecot_reports_its_own_delimiter():
    caps = _folder_capabilities(DOVECOT)
    assert caps == {"delimiter": ".", "nesting": True}


def test_a_flat_namespace_cannot_nest():
    """NIL for the delimiter is a server saying it has no hierarchy at all."""
    caps = _folder_capabilities([row("INBOX", "\\HasNoChildren", delim=None)])
    assert caps == {"delimiter": "", "nesting": False}


def test_one_noinferiors_folder_does_not_condemn_the_account():
    """Some servers mark a single special mailbox as taking no children. Reading
    that as "this server does not nest" would refuse the whole account over one
    folder."""
    caps = _folder_capabilities([
        row("INBOX", "\\Noinferiors"),
        row("Archive", "\\HasNoChildren"),
    ])
    assert caps["nesting"] is True


def test_an_account_with_no_user_folders_yet_is_allowed():
    """Nothing to judge from. Most servers nest, and the honest failure for the
    rest is the CREATE being refused and saying so — not a dialog that quietly
    forbids it."""
    caps = _folder_capabilities([row("Folders", "\\Noselect", delim=b"/")])
    assert caps["nesting"] is True


def test_children_of_other_folders_are_not_read_as_siblings():
    """Only the folders where a new one would go decide it. An already-nested
    child says nothing about whether its *parent's* level takes children."""
    caps = _folder_capabilities([
        row("INBOX", "\\HasNoChildren"),
        row("Archive", "\\HasChildren"),
        row("Archive/2024", "\\Noinferiors"),
    ])
    assert caps["nesting"] is True


def test_the_bridge_namespace_node_is_still_found():
    assert _user_folder_parent(BRIDGE) == "Folders/"
    assert _user_folder_parent(GMAIL) == ""
