"""Render the site chrome and assert on it, with no third-party deps.

``tests/test_v2_2_penalties.py`` asserts the brand and the nav against a
**real** built site, which is the better test and the one CI runs. It needs
the whole dependency stack to get there: httpx, pydantic, numpy, the
engine, a database.

This is the same assertions against the chrome functions alone, reachable
on a checkout with nothing installed — an offline clone, a locked-down CI
image, a contributor who has not run ``pip install``. ``report.py`` imports
the engine and the sources at module scope, none of which
``_site_header`` / ``_footer`` / ``_page`` ever call, so the missing ones
are stubbed just far enough for the import to succeed.

**Run as a subprocess, never imported.** It installs stubs into
``sys.modules``, and a test that imported it would hand those stubs to
every test that ran afterwards — including the ones that need the real
engine. ``tests/test_site_chrome.py`` shells out to it for exactly that
reason.

    python tests/support/render_chrome.py

Exit 0 when the chrome is correct, 1 with a report otherwise.
"""
from __future__ import annotations

import re
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

FAILURES: list[str] = []
CHECKS = 0


def ok(cond, label: str, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILURES.append(f"{label}{f'  [{detail}]' if detail else ''}")


def _stub(name: str, **attrs) -> None:
    """Install a stand-in module, but only if the real one is absent."""
    try:
        __import__(name)
        return
    except Exception:  # noqa: BLE001
        pass
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod


def _install_stubs() -> None:
    class _Base:
        def __init__(self, *a, **k):
            pass

        def __init_subclass__(cls, **k):
            pass

    _stub("httpx", Client=object, AsyncClient=object, HTTPError=Exception)
    _stub("numpy")
    _stub("pandas")
    _stub("pydantic", BaseModel=_Base, Field=lambda *a, **k: None)
    _stub("pydantic_settings", BaseSettings=_Base,
          SettingsConfigDict=lambda **k: dict(**k))

    import dynasty  # noqa: F401  (real package, real __path__)

    _stub("dynasty.engine.similarity_v1", EngineResult=object, OUT_ROOT=".",
          run_engine=lambda **k: None)
    _stub("dynasty.engine.format_overlay",
          PRESETS={"sf_ppr": {"label": "Superflex PPR"}},
          OverlayResult=object, all_format_overlays=lambda e: {})
    _stub("dynasty.engine.superflex_vorp", SUPERFLEX_STARTERS={},
          apply_superflex_vorp=lambda r: None)
    _stub("dynasty.consensus", ConsensusComparison=object,
          compare_to_consensus=lambda **k: None,
          load_crosswalk=lambda *a, **k: {})
    _stub("dynasty.sources", __path__=[])
    _stub("dynasty.sources.keeptradecut", load_latest=lambda *a, **k: None)
    _stub("dynasty.sources.nflverse_career_stats",
          build_career_stats=lambda *a, **k: None,
          career_stats_html=lambda c: "")


# The four primary tabs, in order, exactly as the owner named them.
PRIMARY = [
    ("myteam.html", "Input Sleeper Team"),
    ("crossleague.html", "Best Managers"),
    ("rankings.html", "Similar NFL Career Paths"),
    ("league.html", "Dynasty Rankings"),
]

# Every label retired from the nav, across both rebrands.
RETIRED_NAV = [
    "My Team", "Similarity Scores", "Manager Score", "Roster Reel",
    "League Overlay",
]

RETIRED_BRANDS = [
    "Kings of Dynasty",          # the brand until v3.13
    "Box Score Dynasty",         # PR #52, proposed and abandoned
    "Dynasty Football Model",    # the brand until v2.2
]


def main() -> int:
    _install_stubs()

    from dynasty import player_highlights as hl
    from dynasty import report
    from dynasty.branding import SITE_NAME, page_title

    ts = datetime(2026, 9, 21, 17, 0, tzinfo=timezone.utc)
    header = report._site_header("rankings", ts, "Superflex PPR")
    nav = header[header.index("<nav>"):header.index("</nav>")]

    # ---------------------------------------------------------- nav shape
    for href, label in PRIMARY:
        ok(f'href="{href}"' in nav and f">{label}<" in nav,
           f"primary nav carries {label!r} -> {href}", nav)
    ok(len(re.findall(r"<a ", nav)) == len(PRIMARY),
       f"primary nav is exactly {len(PRIMARY)} tabs", nav)
    for dead in RETIRED_NAV:
        ok(f">{dead}<" not in nav, f"retired nav label {dead!r} is gone", nav)

    secondary = header[header.index('<nav class="secondary">'):]
    secondary = secondary[:secondary.index("</nav>")]
    for label in ("Methodology", "Sources", "Prospects"):
        ok(f">{label}<" in secondary,
           f"{label} stays in the quiet secondary row", secondary)

    # The tab that absorbed Manager Score is the one marked active when a
    # visitor lands on the old URL.
    ms_header = report._site_header("managerscore", ts, "Superflex PPR")
    ms_nav = ms_header[ms_header.index("<nav>"):ms_header.index("</nav>")]
    ok('href="myteam.html" class="active"' in ms_nav,
       "the managerscore key marks Input Sleeper Team active", ms_nav)

    # --------------------------------------------------------------- brand
    h1 = header[header.index("<h1>"):header.index("</h1>")]
    ok(re.sub(r"<[^>]+>", "", h1) == SITE_NAME,
       "the h1 renders exactly the site name", h1)

    root = report._page(page_title("Similar NFL Career Paths"), header, "<p>x</p>")
    sub = report._page(page_title("Josh Allen"), header, "<p>x</p>",
                       css_href="../assets/style.css")

    ok(f"<title>{SITE_NAME} \u2014 Similar NFL Career Paths</title>" in root,
       "the page title carries the brand and the section")
    ok(f'property="og:site_name" content="{SITE_NAME}"' in root,
       "og:site_name carries the brand, so shared links do too")
    ok(f"<footer>{SITE_NAME}" in root, "the footer carries the brand")

    for page, name in ((root, "root page"), (sub, "players/ page")):
        for dead in RETIRED_BRANDS:
            ok(dead not in page, f"{name} is free of {dead!r}")

    # ------------------------------------------------- site-wide injection
    for page, name in ((root, "root page"), (sub, "players/ page")):
        ok("var DFMHL" in page,
           f"{name} carries the shared highlights renderer")
    ok('window.DFM_BASE = "";' in root,
       "a root page resolves artifacts against the site root")
    ok('window.DFM_BASE = "../";' in sub,
       "a players/ page resolves artifacts one directory up")

    # ------------------------------------------------------- the name cell
    chip = hl.player_chip("Ja'Marr Chase", gsis="00-0036900", position="WR",
                          href="players/jamarr-chase-036900.html")
    ok(hl.PLAYER_ATTR in chip, "a player name carries the highlights marker")
    ok('href="players/jamarr-chase-036900.html"' in chip,
       "and keeps its link to the player page")
    ok("<button" in chip,
       "the affordance is a button, inert without JS rather than a dead link")

    # ------------------------------------------------------------- report
    if FAILURES:
        print(f"{len(FAILURES)} of {CHECKS} chrome checks FAILED:")
        for line in FAILURES:
            print(f"  FAIL {line}")
        return 1
    print(f"All {CHECKS} site-chrome checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
