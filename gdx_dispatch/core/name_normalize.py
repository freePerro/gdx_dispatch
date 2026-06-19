"""Conservative name normalization used at customer/user ingest sites.

Goal: fix lowercase-surname data hygiene ("mike wendt" → "Mike Wendt")
without mangling acronyms ("PPFD"), brand spellings ("iPhone"),
already-titled names ("McDonald's"), or names with embedded digits.

Rule: for each space-separated word, capitalize the first letter ONLY
if the word is entirely lowercase ASCII letters. Everything else is
left exactly as the caller passed it. This is intentionally narrow —
it catches the 80% of "user typed it lowercase" cases without touching
the long tail where Title-Case would be wrong.

Applied at:
- QBO customer pull/adopt (gdx_dispatch/modules/quickbooks/sync.py)
- POST /api/customers (gdx_dispatch/routers/customers.py)
- POST /api/users    (gdx_dispatch/routers/users.py)
"""
from __future__ import annotations

import re

_ALL_LOWER_WORD = re.compile(r"^[a-z]+$")


def humanize_name(value: str | None) -> str | None:
    """Capitalize the first letter of any space-separated word that is
    entirely lowercase ASCII. Leaves everything else alone.

    >>> humanize_name("mike wendt")
    'Mike Wendt'
    >>> humanize_name("Tim loose")
    'Tim Loose'
    >>> humanize_name("PPFD")
    'PPFD'
    >>> humanize_name("89 Lumber and supply")
    '89 Lumber And Supply'
    >>> humanize_name("McDonald's")
    "McDonald's"
    >>> humanize_name("")
    ''
    >>> humanize_name(None) is None
    True
    """
    if value is None:
        return None
    if not value:
        return value
    stripped = value.strip()
    if not stripped:
        return stripped
    parts = stripped.split(" ")
    out = []
    for part in parts:
        if part and _ALL_LOWER_WORD.match(part):
            out.append(part[0].upper() + part[1:])
        else:
            out.append(part)
    return " ".join(out)
