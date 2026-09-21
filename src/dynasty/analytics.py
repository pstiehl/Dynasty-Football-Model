"""Site-wide web analytics, injected at build time from the environment.

The site is static on GitHub Pages, which gives the owner **no server logs
at all** — no request count, no referrer, nothing. Before this module the
only evidence anyone visited was somebody saying so. That is the gap this
fills, and it is the whole scope: a page-view beacon.

Three properties are load-bearing, and each is enforced below rather than
documented and hoped for:

1. **The token is never in the repo.** It arrives only via the environment
   (``DFM_ANALYTICS_TOKEN``), supplied by a GitHub Actions secret. Nothing
   here has a default token, and nothing writes one to disk.

2. **Unset means absent, not broken.** With no token the functions return
   the empty string, so ``_page`` interpolates nothing — no placeholder, no
   empty ``<script>`` tag, no ``data-cf-beacon='{"token": ""}'`` that would
   ship a dead request to Cloudflare from every page load. A local build, a
   fork, and a contributor's checkout all produce a site byte-identical to
   today's.

3. **The provider is swappable by configuration.** Cloudflare Web Analytics
   is the choice because the owner already has the account and it is free
   and cookieless, but nothing above this module knows that. Moving to
   Plausible or Fathom is ``DFM_ANALYTICS_PROVIDER=plausible`` plus the new
   token — no edit to ``report.py``, no new call site. Adding a fourth
   provider is one function and one dict entry here.

Why a token is validated rather than escaped
--------------------------------------------
The Cloudflare beacon carries its token inside a JSON attribute
(``data-cf-beacon='{"token": "..."}'``), so a token containing a quote
would break out of the attribute and inject markup into every page on the
site. Escaping would keep the page valid but ship a token that cannot
work; there is no useful behaviour on that branch. So a token outside a
conservative charset is **refused** — the beacon is omitted and a warning
naming the variable (never the value) goes to stderr. A typo therefore
costs analytics, never the site.
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Callable, Dict, Mapping, Optional

# The environment is the only input. Both names are documented in
# docs/ANALYTICS.md and wired in .github/workflows/daily-refresh.yml.
ENV_TOKEN = "DFM_ANALYTICS_TOKEN"
ENV_PROVIDER = "DFM_ANALYTICS_PROVIDER"

DEFAULT_PROVIDER = "cloudflare"

# Cloudflare issues a 32-character lowercase hex token today. Pinning to
# exactly that would break the day they widen it, so this is the loosest
# charset that is still safe to place inside a quoted HTML attribute and a
# JSON string: no quotes, no angle brackets, no ampersand, no backslash,
# no whitespace. Plausible's "token" is a domain and Fathom's is a short
# site id, and both fit the same charset.
_TOKEN_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{3,127}\Z")

_WARNED: set = set()


def _warn_once(message: str) -> None:
    """Complain to stderr at most once per build.

    ``_page`` runs once per page and the site has hundreds of them, so an
    unconditional warning would bury the build log. Never interpolate a
    token into the message.
    """
    if message in _WARNED:
        return
    _WARNED.add(message)
    print(f"analytics: {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Providers
#
# A provider is ``token -> head HTML``. It may assume the token has already
# been validated. Everything it returns is emitted verbatim into <head>, so
# a provider must only ever produce a `defer`red external script: see the
# ordering note in ``snippet`` below.
# ---------------------------------------------------------------------------

def _cloudflare(token: str) -> str:
    """Cloudflare Web Analytics — free, cookieless, no consent banner.

    ``json.dumps`` builds the attribute payload so the JSON is correct by
    construction rather than by careful typing. The outer attribute is
    single-quoted because the payload's own quotes are double, which is
    also the form Cloudflare's dashboard hands out.
    """
    payload = json.dumps({"token": token}, separators=(", ", ": "))
    return (
        '<script defer src="https://static.cloudflareinsights.com/beacon.min.js" '
        f"data-cf-beacon='{payload}'></script>"
    )


def _plausible(token: str) -> str:
    """Plausible Analytics. The "token" is the registered domain."""
    return (
        f'<script defer data-domain="{token}" '
        'src="https://plausible.io/js/script.js"></script>'
    )


def _fathom(token: str) -> str:
    """Fathom Analytics. The "token" is the site id."""
    return (
        '<script defer src="https://cdn.usefathom.com/script.js" '
        f'data-site="{token}"></script>'
    )


def _none(token: str) -> str:
    """Explicitly measure nothing, even with a token in the environment.

    Useful for a staging build, and for answering "is analytics doing
    this?" without unsetting a secret.
    """
    return ""


PROVIDERS: Dict[str, Callable[[str], str]] = {
    "cloudflare": _cloudflare,
    "plausible": _plausible,
    "fathom": _fathom,
    "none": _none,
    "off": _none,
}


# ---------------------------------------------------------------------------
# The seam report.py uses
# ---------------------------------------------------------------------------

def provider_name(env: Optional[Mapping[str, str]] = None) -> str:
    """The configured provider, normalised. Never raises."""
    src = os.environ if env is None else env
    return (src.get(ENV_PROVIDER) or DEFAULT_PROVIDER).strip().lower()


def token(env: Optional[Mapping[str, str]] = None) -> str:
    """The configured token, or ``""`` when absent or malformed.

    Whitespace is stripped first: a secret pasted into the GitHub UI with a
    trailing newline is the single most likely way to get a "valid" token
    that does not work, and it is silly to lose analytics to it.
    """
    src = os.environ if env is None else env
    raw = (src.get(ENV_TOKEN) or "").strip()
    if not raw:
        return ""
    if not _TOKEN_RE.match(raw):
        # Length is safe to print; it is the one fact that makes a
        # truncated-secret mistake diagnosable. The value is not.
        _warn_once(
            f"{ENV_TOKEN} is set but is not a valid token "
            f"(length {len(raw)}, unexpected characters) - "
            "no beacon will be emitted."
        )
        return ""
    return raw


def snippet(env: Optional[Mapping[str, str]] = None) -> str:
    """The analytics markup for ``<head>``, or ``""`` when unconfigured.

    **Position contract.** The return value goes in ``<head>`` and every
    provider above emits a single ``defer``red *external* script. That is
    what makes this safe next to the hazard PR #71 fixed: a deferred
    external script cannot execute before, between, or during the page's
    inline blobs — the HTML spec defers it until after parsing, so the
    relative order of the shared highlight renderer and the page scripts
    below it is untouched no matter what this returns. A provider that
    wanted an *inline* script could not be added here without revisiting
    that, which is why the constraint is written down rather than implied.
    """
    tok = token(env)
    if not tok:
        return ""
    name = provider_name(env)
    build = PROVIDERS.get(name)
    if build is None:
        _warn_once(
            f"{ENV_PROVIDER}={name!r} is not a known provider "
            f"({', '.join(sorted(PROVIDERS))}) - no beacon will be emitted."
        )
        return ""
    return build(tok)
