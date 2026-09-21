"""Render the REAL myteam.html, with no third-party deps, for JS execution.

``tests/js/myteam_script_order_tests.mjs`` needs the page exactly as the site
ships it -- every ``<script>`` in the order ``report._page`` emits them,
including the shared ``player_highlights`` blob that ``_page`` appends *after*
the body. Reading ``myteam.py`` is not enough to know that order, and PR #71
is the proof: the highlight renderer was defined after the page scripts, so a
page script touching ``DFMHL`` during evaluation threw and killed the page.

This is the ``tests/support/render_chrome.py`` stub trick pointed at
``build_my_team``: ``report.py`` imports the engine and the sources at module
scope, none of which page rendering calls, so they are stubbed just far enough
for the import to succeed and the real ``_page`` to run.

**Run as a subprocess, never imported** -- it installs stubs into
``sys.modules``.

    python3 tests/support/render_myteam.py <outdir>

Writes ``myteam-configured.html`` and ``myteam-unconfigured.html`` to
``<outdir>`` and prints each path. Exit 0 on success.
"""
from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

CORPUS_URL = "https://corpus.example.workers.dev"


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


def main() -> int:
    outdir = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    _install_stubs()

    from dynasty.myteam import build_my_team

    ts = datetime(2026, 9, 21, 17, 0, tzinfo=timezone.utc)

    for label, url in (("configured", CORPUS_URL), ("unconfigured", "")):
        old = os.environ.get("DFM_CORPUS_URL")
        if url:
            os.environ["DFM_CORPUS_URL"] = url
        else:
            os.environ.pop("DFM_CORPUS_URL", None)
        try:
            html = build_my_team(ts, "Superflex PPR")
        finally:
            if old is None:
                os.environ.pop("DFM_CORPUS_URL", None)
            else:
                os.environ["DFM_CORPUS_URL"] = old

        path = outdir / f"myteam-{label}.html"
        path.write_text(html, encoding="utf-8")
        print(path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
