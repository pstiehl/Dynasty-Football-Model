"""The site's name, in one place.

The name has now changed twice. The first rebrand ("Dynasty Football Model"
-> "Kings of Dynasty") was applied by editing eleven string literals across
five modules, and a later pass found two that had been missed. So this
module exists purely so the next rename is one edit rather than a grep.

``SITE_NAME`` is the plain-text form: page titles, ``<title>`` tags, the
footer, ``og:site_name``. ``site_name_html`` is the marked-up form for the
header ``<h1>`` and page headings, where one word carries the accent colour.

Nothing here imports anything else from the package -- ``report``,
``myteam``, ``reel``, ``managerscore`` and ``crossleague_page`` already
import from each other lazily to dodge cycles, and a constants module that
joined that graph would defeat the point.
"""
from __future__ import annotations

#: Plain text. Use for <title>, og:*, the footer and any prose.
SITE_NAME = "Next Level Dynasty Football"

#: The word inside SITE_NAME that carries the accent colour in headings.
_ACCENT_WORD = "Dynasty"

#: Short tagline shown under the header and in og:description.
SITE_TAGLINE = "Fantasy Football"


def site_name_html(accent_class: str = "accent") -> str:
    """``SITE_NAME`` with the accent word wrapped in a span.

    Split on the accent word rather than hard-coding the three fragments,
    so changing ``SITE_NAME`` alone is enough.
    """
    head, _, tail = SITE_NAME.partition(_ACCENT_WORD)
    return (
        f"{head}<span class=\"{accent_class}\">{_ACCENT_WORD}</span>{tail}"
    )


def page_title(section: str = "") -> str:
    """``"Next Level Dynasty Football — Rankings"``, or just the site name."""
    return f"{SITE_NAME} — {section}" if section else SITE_NAME
