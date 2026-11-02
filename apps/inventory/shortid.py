"""Short codes people read off labels and type back in (docs/design/mvp-a.md §1).

Stock items get a 6-character ``short_id`` and locations a 5-character ``code``, both drawn
from Crockford's base32 alphabet (no I, L, O or U), so they are easy to read aloud and copy.
A code someone gives a location by hand (``F80A``, ``LN2-1``) may use any letter, digit or
hyphen, but never something that reads as a stock item's short id, so whatever is typed or
scanned means one thing only.
"""

import re
import secrets

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
SHORT_ID_LENGTH = 6
LOCATION_CODE_LENGTH = 5
LOCATION_CODE_MAX_LENGTH = 12
LOCATION_CODE_PATTERN = r"^[A-Z0-9-]{1,12}$"
SHORT_ID_PATTERN = rf"^[{CROCKFORD}]{{{SHORT_ID_LENGTH}}}$"

# What people confuse when they copy a code by hand (Crockford's decoding rules).
_LOOKALIKES = str.maketrans({"I": "1", "L": "1", "O": "0"})
_SEPARATORS = re.compile(r"[\s-]+")


def random_code(length):
    """``length`` Crockford characters from the ``secrets`` module."""
    return "".join(secrets.choice(CROCKFORD) for _ in range(length))


def new_short_id():
    return random_code(SHORT_ID_LENGTH)


def new_location_code():
    return random_code(LOCATION_CODE_LENGTH)


def crockford_normalize(text):
    """Upper case, without spaces or hyphens, with I and L read as 1 and O as 0."""
    return _SEPARATORS.sub("", str(text or "")).upper().translate(_LOOKALIKES)


def as_short_id(text):
    """The short id ``text`` spells (``"7k3m-9q"`` → ``"7K3M9Q"``), or None if it spells none."""
    normalized = crockford_normalize(text)
    if len(normalized) == SHORT_ID_LENGTH and all(char in CROCKFORD for char in normalized):
        return normalized
    return None


def looks_like_short_id(text):
    return as_short_id(text) is not None


def location_code_problem(code):
    """Why ``code`` (already upper case) can't be a hand-made location code, or None."""
    if not re.fullmatch(LOCATION_CODE_PATTERN, code):
        return (
            f"{code!r} isn't a valid code: use 1 to {LOCATION_CODE_MAX_LENGTH} letters, digits "
            "or hyphens."
        )
    if looks_like_short_id(code):
        return f"{code!r} reads like a stock item's 6-character id; pick another code."
    return None
