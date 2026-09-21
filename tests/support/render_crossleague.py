"""Emit the REAL crossleague.html, with no third-party deps installed.

Why this exists
---------------
``tests/js/crossleague_script_order_tests.mjs`` must execute the scripts of
the page *as the page actually ships them*, in document order. Reconstructing
that order in the test would defeat the point: PR #71's live outage was
precisely a mismatch between the order a test assumed and the order
``report._page`` emitted. So the page is built here by calling
``build_cross_league`` for real, and the test parses the resulting HTML.

``report.py`` imports the engine, httpx, pydantic and the sources at module
scope, none of which the page builders call. Same stubbing approach as
``tests/support/render_chrome.py``, and the same warning applies:

**Run as a subprocess, never imported.** It installs stubs into
``sys.modules``; importing it would hand those stubs to every test that ran
afterwards, including ones that need the real engine.

    python tests/support/render_crossleague.py <output.html>
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


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
    if len(sys.argv) < 2:
        print("usage: render_crossleague.py <output.html>", file=sys.stderr)
        return 2
    _install_stubs()

    from dynasty.crossleague_page import build_cross_league

    html = build_cross_league(
        datetime(2026, 9, 21, tzinfo=timezone.utc), "Test League"
    )
    out = Path(sys.argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
