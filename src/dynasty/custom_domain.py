"""The hostname GitHub Pages serves this site from.

Phil registered ``nextleveldynastyfootball.com`` on 2026-09-22 (his own
Cloudflare account, so the site no longer depends on anyone else's
dashboard access). This module is the one place that fact is written down.

GitHub Pages takes the hostname for an Actions-deployed site from two
places: the repository's Pages setting, and a ``CNAME`` file at the root of
the uploaded artifact. The setting is authoritative and owner-only, which
makes it invisible from here -- nothing in a clone tells you what domain
the live site answers on, and a settings reset drops it silently. Emitting
the file on every build keeps the domain in version control where a diff
can show it changing, and makes the deploy self-describing.

The two agree by construction as long as this constant matches the setting.
If they ever disagree, GitHub honours the artifact's CNAME on deploy, so
this file wins -- which is the safer direction: it is the one under review.

Override with ``DFM_SITE_DOMAIN``. Set it to the empty string to publish
with no custom domain at all, which restores the old
``pstiehl.github.io/Dynasty-Football-Model/`` URL; that is the escape hatch
if DNS ever has to be torn down in a hurry.

Note for anyone changing this: a project site on a custom domain is served
from the *root* of that domain, not from a ``/Dynasty-Football-Model/``
subpath. Every internal link the builders emit is already relative, so the
move needs no link rewriting -- but a future absolute link starting with
``/Dynasty-Football-Model/`` would 404 on the domain while still working on
github.io, which is a nasty way to find out. Keep internal links relative.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

#: Environment variable that overrides the compiled-in domain.
ENV_DOMAIN = "DFM_SITE_DOMAIN"

#: The registered domain. Empty string disables the CNAME entirely.
DEFAULT_DOMAIN = "nextleveldynastyfootball.com"


def domain(env: Optional[Mapping[str, str]] = None) -> str:
    """The domain to publish under, or ``""`` for none.

    An unset variable means "use the registered domain". An explicitly
    empty variable means "no custom domain" -- those are different
    intentions and are deliberately distinguishable here.
    """
    src = os.environ if env is None else env
    raw = src.get(ENV_DOMAIN)
    if raw is None:
        return DEFAULT_DOMAIN
    return raw.strip().lower()


def write_cname(out_root: Path, env: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """Write ``CNAME`` into the built site. Returns the path, or None.

    GitHub wants the bare hostname and nothing else: no scheme, no
    trailing slash, no second line. Anything else and the deploy rejects
    the domain, so normalise rather than trust the input.
    """
    host = domain(env)
    if not host:
        return None

    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix):]
    host = host.strip("/").strip()
    if not host:
        return None

    path = Path(out_root) / "CNAME"
    path.write_text(f"{host}\n", encoding="utf-8")
    return path
