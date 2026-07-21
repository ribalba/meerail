"""Compatibility shim for imapclient on Python 3.14+.

Python 3.14 turned imaplib.IMAP4.file into a read-only property, but
imapclient (<= 3.1.0) still assigns it on connect and after STARTTLS.
Importing this module restores a setter that writes the backing attribute.
"""

from __future__ import annotations

import imaplib
import sys

if sys.version_info >= (3, 14):
    _file_prop = imaplib.IMAP4.file
    if isinstance(_file_prop, property) and _file_prop.fset is None:
        imaplib.IMAP4.file = property(
            _file_prop.fget, lambda self, value: setattr(self, "_file", value)
        )
