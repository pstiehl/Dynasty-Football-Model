"""Syntax-check the client-side JavaScript embedded in the site builders.

``reel.py`` and ``myteam.py`` carry their browser code as Python string
constants, which means a stray brace or a bad template substitution produces
a perfectly valid Python module and a page that throws on load. Nothing in
the build catches that, and there is no browser in CI to notice.

This extracts each blob exactly as ``build_reel`` / ``build_my_team`` would
emit it -- token substitutions applied -- and runs ``node --check`` over it.

    python scripts/check_site_js.py

Exit status is 0 when every blob parses, 1 otherwise. Requires ``node`` on
PATH; without it the script says so and exits 0, so it can be wired into a
build step that must not hard-fail on a machine with no Node.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


def pos_color_json() -> str:
    """The real POSITION_COLOR map, or a stand-in of the same shape.

    ``_pos_color_json`` reaches into ``report``, which transitively imports
    ``httpx``. That dependency has nothing to do with whether the JavaScript
    parses, so an environment without it falls back to a literal of the same
    type rather than failing the check.
    """
    import json
    try:
        from dynasty.myteam import _pos_color_json
        return _pos_color_json()
    except Exception as exc:  # noqa: BLE001
        print(f"  note: using a stub POSITION_COLOR ({exc.__class__.__name__}:"
              f" {exc})")
        return json.dumps({"QB": "#ef4444", "RB": "#22c55e",
                           "WR": "#3b82f6", "TE": "#f59e0b"})


def blobs() -> "list[tuple[str, str]]":
    """``(label, javascript)`` for every script the site ships."""
    from dynasty.reel import reel_assets
    from dynasty.myteam import _MYTEAM_JS
    from dynasty.player_highlights import PLAYER_HIGHLIGHTS_JS
    from dynasty.managerscore_js import (
        MANAGERSCORE_CORE_JS,
        MANAGERSCORE_UI_JS,
    )
    from dynasty.corpus_submit_js import corpus_submit_js

    # Checked with a URL spliced in: the unconfigured build is a strict
    # substring of it, so this is the strictly harder parse.
    corpus_js = corpus_submit_js("https://corpus.example.workers.dev")

    reel_js, _ = reel_assets()
    myteam_js = _MYTEAM_JS.replace("__POS_COLOR__", pos_color_json())
    hl_js = PLAYER_HIGHLIGHTS_JS
    return [
        # Shipped on EVERY page by report._page, so a parse error here takes
        # the whole site's highlights affordance down at once.
        ("player_highlights.py:PLAYER_HIGHLIGHTS_JS", hl_js),
        ("reel.py:_REEL_JS", reel_js),
        ("myteam.py:_MYTEAM_JS", myteam_js),
        ("managerscore_js.py:MANAGERSCORE_CORE_JS", MANAGERSCORE_CORE_JS),
        ("managerscore_js.py:MANAGERSCORE_UI_JS", MANAGERSCORE_UI_JS),
        ("corpus_submit_js.py:CORPUS_SUBMIT_JS", corpus_js),
        # The pages load these in one document, so also check them
        # concatenated: a duplicate top-level ``const`` across two blobs is
        # legal in isolation and a SyntaxError in the browser. myteam.html
        # now carries five scripts: Manager Score moved into it, and the
        # corpus submit script moved in with Manager Score.
        #
        # Order here matches what report._page actually emits -- the shared
        # highlight blob goes LAST, after the body, not first. Parsing does
        # not care, but a reader comparing this against the page should not
        # be told a different order than the one that ships. Whether that
        # order also *runs* is tests/js/myteam_script_order_tests.mjs.
        (
            "myteam.html (reel + myteam + managerscore + corpus + highlights)",
            "\n".join([reel_js, myteam_js, MANAGERSCORE_CORE_JS,
                       MANAGERSCORE_UI_JS, corpus_js, hl_js]),
        ),
    ]


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("node not on PATH - skipping JS syntax check.")
        return 0

    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for i, (label, js) in enumerate(blobs()):
            path = Path(tmp) / f"blob{i}.js"
            path.write_text(js, encoding="utf-8")
            proc = subprocess.run(
                [node, "--check", str(path)],
                capture_output=True, text=True,
            )
            if proc.returncode == 0:
                print(f"  OK   {label} ({len(js):,} chars)")
            else:
                failures += 1
                print(f"  FAIL {label}")
                print(proc.stderr.strip())

    print("\nAll site JavaScript parses." if not failures
          else f"\n{failures} blob(s) failed to parse.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
