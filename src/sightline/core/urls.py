"""Which web addresses Sightline opens (review S-09).

Only web pages (``http``, ``https``) and e-mail links (``mailto``).  A
custom command's ``url`` or a spoken address goes to the browser's address
bar or, with no browser open, to the Windows shell, which hands any other
scheme to whatever program claims it (``file:``, ``ms-settings:``,
``search-ms:`` and the like).  An address without a scheme
("bbc.co.uk/news") is a web page.
"""

from __future__ import annotations

import re

ALLOWED_SCHEMES = ("http", "https", "mailto")
REFUSED_MESSAGE = "Sightline only opens web pages and e-mail links."
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")


def web_address(url: object) -> str | None:
    """The address to open, with ``https://`` added when it has no scheme; None when it may not be opened."""
    text = str(url or "").strip()
    if not text or "\\" in text or any(ord(ch) < 32 for ch in text):
        return None  # a backslash means a path ("\\server\share", "c:\\"), never a web address
    found = _SCHEME.match(text)
    if found is None:
        return "https://" + text.lstrip("/")
    scheme, rest = found.group(1).lower(), text[found.end() :]
    if scheme in ("http", "https"):
        return text if rest.startswith("//") and rest[2:].strip("/") else None
    if scheme == "mailto":
        return text if rest else None
    if rest[:1].isdigit() and ("." in scheme or scheme == "localhost"):
        return "https://" + text  # a host and a port ("example.com:8080/x"), not a scheme
    return None


def has_scheme(url: object) -> bool:
    """True when the address names its scheme ("https://...", "mailto:...")."""
    text = str(url or "").strip()
    found = _SCHEME.match(text)
    if found is None:
        return False
    rest = text[found.end() :]
    return not (rest[:1].isdigit() and ("." in found.group(1) or found.group(1).lower() == "localhost"))


__all__ = ["ALLOWED_SCHEMES", "REFUSED_MESSAGE", "has_scheme", "web_address"]
