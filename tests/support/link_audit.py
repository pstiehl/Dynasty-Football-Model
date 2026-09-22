"""Resolve every internal link in a built site and report the dead ones.

This exists because of a live, user-visible outage rather than a hunch.
On 2026-09-22 every nav link on every player detail page 404ed:
``_site_header`` emitted bare filenames (``rankings.html``), the detail
pages are written into ``dynasty_site/players/``, and a browser resolves a
relative href against *the directory of the page it is on*. So the nav on
``/players/aaron-jones-033293.html`` pointed at
``/players/rankings.html`` -- seven tabs, seven 404s. The stylesheet had
been given its ``../`` prefix; the nav never had.

Nothing caught it, and the reason is worth writing down: every existing
check asserts on **one page's HTML in isolation**. ``href="rankings.html"``
is correct on a root page and broken one directory down, so no assertion
about the string can see the bug. The missing ingredient is the page's
own location on disk. That is the entire subject of this module: a link is
audited *relative to the file that contains it*.

Deliberately dependency-free (stdlib ``html.parser``, no bs4) so it can run
on a checkout with nothing installed, which is where the site chrome is
otherwise unverifiable.

JS-template hrefs are skipped
-----------------------------
Several pages build rows in the browser, so their markup contains href
values that are JavaScript source, not URLs::

    <a href="players/' + esc(slug) + '.html">

Resolving that as a path is meaningless. Any href containing a quote, a
``+`` or ``esc(`` is treated as a template and reported separately in
``skipped`` rather than silently dropped -- a count nobody can see is a
place for a real bug to hide.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

#: Attributes that carry a URL we can check on disk.
URL_ATTRS = ("href", "src")

#: Substrings that mark an href as JavaScript source rather than a URL.
TEMPLATE_MARKERS = ("'", '"', "+", "esc(", "${", "{{")

#: Schemes and forms that are not a file in the built site.
_EXTERNAL_PREFIXES = (
    "http://", "https://", "//", "mailto:", "tel:", "data:",
    "javascript:", "#",
)


def is_template(url: str) -> bool:
    """True when ``url`` is a fragment of JS string concatenation."""
    return any(marker in url for marker in TEMPLATE_MARKERS)


def is_external(url: str) -> bool:
    """True when ``url`` does not name a file inside the built site."""
    stripped = url.strip()
    if not stripped:
        return True
    return stripped.startswith(_EXTERNAL_PREFIXES)


class _LinkParser(HTMLParser):
    """Collect (tag, attr, value) for every URL-bearing attribute."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: List[Tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        for name, value in attrs:
            if name in URL_ATTRS and value is not None:
                self.found.append((tag, name, value))


def extract_urls(html: str) -> List[Tuple[str, str, str]]:
    """Every ``href``/``src`` in ``html``, in document order."""
    parser = _LinkParser()
    parser.feed(html)
    parser.close()
    return parser.found


@dataclass
class DeadLink:
    """One internal link that does not resolve to a file on disk."""

    page: str          # site-relative path of the page containing the link
    url: str           # the href/src exactly as emitted
    resolved: str      # where a browser on ``page`` would go
    tag: str
    attr: str

    def __str__(self) -> str:
        return (
            f"{self.page}: <{self.tag} {self.attr}=\"{self.url}\"> "
            f"-> {self.resolved} (missing)"
        )


@dataclass
class AuditResult:
    pages: int = 0
    checked: int = 0
    dead: List[DeadLink] = field(default_factory=list)
    skipped: Dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.dead

    def report(self, limit: int = 40) -> str:
        head = (
            f"{self.pages} pages, {self.checked} internal links checked, "
            f"{len(self.dead)} dead "
            f"(skipped: "
            + ", ".join(f"{k}={v}" for k, v in sorted(self.skipped.items()))
            + ")"
        )
        if not self.dead:
            return head
        lines = [head] + [f"  DEAD {d}" for d in self.dead[:limit]]
        if len(self.dead) > limit:
            lines.append(f"  ... and {len(self.dead) - limit} more")
        return "\n".join(lines)


def audit_site(root: Path, patterns: Sequence[str] = ("**/*.html",)) -> AuditResult:
    """Walk every HTML file under ``root`` and resolve its internal links.

    Resolution is the browser's rule, not ``Path.joinpath``: the href is
    joined onto the **directory of the containing page** with POSIX
    semantics, then required to exist. A directory target is accepted when
    it holds an ``index.html``, because that is what a static host serves.

    Query strings and fragments are stripped before the on-disk lookup;
    ``league.html?x=1#top`` is a link to ``league.html``.
    """
    root = Path(root)
    result = AuditResult()
    files: List[Path] = []
    for pattern in patterns:
        files.extend(sorted(root.glob(pattern)))

    for path in files:
        rel = path.relative_to(root).as_posix()
        result.pages += 1
        html = path.read_text(encoding="utf-8", errors="replace")
        page_dir = posixpath.dirname(rel)

        for tag, attr, raw in extract_urls(html):
            url = raw.strip()
            if is_external(url):
                result.skipped["external"] = result.skipped.get("external", 0) + 1
                continue
            if is_template(url):
                result.skipped["js-template"] = (
                    result.skipped.get("js-template", 0) + 1
                )
                continue

            # Strip the parts a filesystem does not have.
            target = url.split("#", 1)[0].split("?", 1)[0]
            if not target:
                result.skipped["fragment-only"] = (
                    result.skipped.get("fragment-only", 0) + 1
                )
                continue

            if target.startswith("/"):
                # Absolute site paths are a trap on this project: a project
                # site on github.io lives under /Dynasty-Football-Model/ but
                # the custom domain serves from /. custom_domain.py says to
                # keep internal links relative, so flag rather than resolve.
                resolved = target.lstrip("/")
            else:
                resolved = posixpath.normpath(posixpath.join(page_dir, target))

            result.checked += 1
            candidate = root / resolved
            if candidate.is_dir():
                candidate = candidate / "index.html"
            if not candidate.exists():
                result.dead.append(
                    DeadLink(page=rel, url=url, resolved=resolved,
                             tag=tag, attr=attr)
                )

    return result


def nav_targets(html: str) -> List[str]:
    """The href of every link in the page's ``<nav>`` elements.

    Narrower than ``extract_urls`` on purpose: the nav is the thing that
    broke, and a test that names it reads better than one that counts all
    links on the page.
    """
    out: List[str] = []
    rest = html
    while "<nav" in rest:
        rest = rest[rest.index("<nav"):]
        end = rest.index("</nav>") if "</nav>" in rest else len(rest)
        block, rest = rest[:end], rest[end + 6:]
        for _tag, attr, value in extract_urls(block):
            if attr == "href":
                out.append(value)
    return out


def format_iter(items: Iterable[str]) -> str:
    return ", ".join(sorted(items))
