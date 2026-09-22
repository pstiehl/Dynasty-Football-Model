"""Build the site's page *skeleton* into a tmp dir and audit every link.

This is the regression test for the 2026-09-22 outage in which every nav
link on every player detail page 404ed. Read ``tests/support/link_audit``
for the mechanism; this file supplies the pages.

Why a skeleton and not ``generate_site``
----------------------------------------
``generate_site`` needs the engine, a populated database, the nflverse
corpus and the KTC artifacts. None of those are present on a checkout with
nothing installed -- and this bug was *in the chrome*, which needs none of
them. So this renders the real chrome functions into the real directory
layout ``generate_site`` writes:

    dynasty_site/               rankings.html, index.html, league.html,
                                methodology.html, sources.html,
                                prospects.html, myteam.html,
                                crossleague.html, managerscore.html,
                                404.html
    dynasty_site/players/       <slug>.html, <slug>-prospect.html
    dynasty_site/assets/        style.css

The location of the file is the whole point. ``href="rankings.html"`` is
correct in the root directory and a 404 one level down, so a test that
renders a page without writing it somewhere cannot see the difference --
which is exactly why ``tests/support/render_chrome.py``, which asserts on
this same nav, passed throughout the outage.

The per-prospect page is rendered with the **real** builder from the
committed fixture, so at least one genuinely nested page is audited as it
ships rather than as this file imagines it.

Run as a subprocess, never imported: it installs stubs into
``sys.modules`` and would hand them to any test that ran afterwards. See
the same note in ``render_chrome.py``.

    python tests/support/audit_site_links.py [--keep]

Exit 0 when every internal link resolves, 1 with a report otherwise.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import types
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests" / "support"))

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "prospects_fixture.json"


def _stub(name: str, **attrs) -> None:
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


# Every root page ``generate_site`` writes, with the nav key it passes.
# Keeping the key here (rather than defaulting them all to "rankings")
# means the audit covers the active-tab branch on every page too.
ROOT_PAGES = [
    ("rankings.html", "rankings", "Similar NFL Career Paths"),
    ("index.html", "rankings", "Similar NFL Career Paths"),
    ("league.html", "league", "Dynasty Rankings"),
    ("methodology.html", "methodology", "Methodology"),
    ("sources.html", "sources", "Sources"),
    ("prospects.html", "prospects", "Prospects"),
    ("myteam.html", "myteam", "Input Sleeper Team"),
    ("crossleague.html", "crossleague", "Best Managers"),
    ("managerscore.html", "managerscore", "Manager Score"),
]


def build_skeleton(out_root: Path) -> None:
    """Write the chrome-bearing page set into ``out_root``."""
    from dynasty import report
    from dynasty.branding import page_title

    ts = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    label = "Superflex PPR"

    (out_root / "assets").mkdir(parents=True, exist_ok=True)
    (out_root / "players").mkdir(parents=True, exist_ok=True)
    (out_root / "assets" / "style.css").write_text(
        report._shared_css(), encoding="utf-8")

    for filename, active, title in ROOT_PAGES:
        html = report._page(
            page_title(title),
            report._site_header(active, ts, label),
            f"<div class='wrap'><p>{title}</p></div>",
        )
        (out_root / filename).write_text(html, encoding="utf-8")

    # The 404 page, which is itself a set of links back into the nav.
    (out_root / "404.html").write_text(
        report.build_not_found(ts, label), encoding="utf-8")

    # A player detail page, in the directory the real one lives in. This is
    # the page whose nav 404ed in production.
    nested = report.SUBDIR_PREFIX
    (out_root / "players" / "aaron-jones-033293.html").write_text(
        report._page(
            page_title("Aaron Jones"),
            report._site_header("rankings", ts, label, prefix=nested),
            "<div class='wrap'><p>Aaron Jones</p>"
            f"<p><a href=\"{nested}league.html\">Dynasty Rankings</a></p></div>",
            prefix=nested,
        ),
        encoding="utf-8",
    )

    # Per-prospect pages from the real builder and the committed fixture.
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    veteran_slugs = set()
    for prospect in data["prospects"]:
        for comp in prospect.get("comps") or []:
            if comp.get("slug") and comp.get("nfl_gsis_id"):
                veteran_slugs.add(comp["slug"])
    # Write the veteran pages those comps cross-link to, so a dead link in
    # the audit means the chrome is wrong rather than that this harness
    # declined to create the target.
    for slug in sorted(veteran_slugs):
        (out_root / "players" / f"{slug}.html").write_text(
            report._page(
                page_title(slug),
                report._site_header("rankings", ts, label, prefix=nested),
                "<div class='wrap'><p>veteran</p></div>",
                prefix=nested,
            ),
            encoding="utf-8",
        )
    for prospect in data["prospects"]:
        slug = report._prospect_slug(prospect)
        html = report._build_prospect_page(
            prospect, label, ts, veteran_slugs=veteran_slugs)
        (out_root / "players" / f"{slug}-prospect.html").write_text(
            html, encoding="utf-8")


def main(argv: list[str]) -> int:
    _install_stubs()
    import link_audit

    keep = "--keep" in argv
    tmp = Path(tempfile.mkdtemp(prefix="dfm-link-audit-"))
    try:
        out_root = tmp / "dynasty_site"
        out_root.mkdir()
        build_skeleton(out_root)

        result = link_audit.audit_site(out_root)
        print(result.report())

        if result.pages < len(ROOT_PAGES) + 2:
            print(f"  FAIL harness rendered only {result.pages} pages")
            return 1
        if result.checked < 50:
            print(f"  FAIL only {result.checked} internal links checked; "
                  "the harness is not exercising the nav")
            return 1

        # Name the regression explicitly, so a failure reads as the bug it
        # is rather than as a generic count.
        nested_pages = [
            p for p in sorted(out_root.glob("players/*.html"))
        ]
        bad_nav = []
        for path in nested_pages:
            html = path.read_text(encoding="utf-8")
            for href in link_audit.nav_targets(html):
                if link_audit.is_external(href) or link_audit.is_template(href):
                    continue
                if not href.startswith("../"):
                    bad_nav.append(f"{path.name}: nav href {href!r} "
                                   "is not prefixed for players/")
        if bad_nav:
            print(f"{len(bad_nav)} nav links on players/ pages are "
                  "root-relative and will 404:")
            for line in bad_nav[:20]:
                print(f"  FAIL {line}")
            return 1

        if not result.ok:
            return 1
        print("All internal links resolve.")
        return 0
    finally:
        if keep:
            print(f"kept: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
