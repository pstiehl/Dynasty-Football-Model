"""Execute the site's real client-side JavaScript under node and assert on it.

``scripts/check_site_js.py`` proves the shipped JavaScript *parses*. This
proves some of it *works*: the id joins behind My Team, the honest unranked
reasons, the rolling game-window filter behind the reel, and the league
strength metric.

It assembles one file --

    tests/js/dom_stub.js        browser stand-ins
    tests/js/fixtures.js        artifacts, registered before anything fetches
    reel.py:_REEL_JS            the shipped reel script, verbatim
    myteam.py:_MYTEAM_JS        the shipped My Team script, verbatim
    tests/js/assertions.js      the checks

The fixture file has to come before the page scripts: myteam.js starts
``loadModelData()`` at parse time, exactly as it does in a browser, so a
fixture registered later would arrive after the fetch it was meant to serve.

-- and runs it with ``node``. Concatenation is exactly how the browser sees
these two scripts on myteam.html, so the harness also catches a collision
between them.

    python scripts/check_site_behaviour.py            # run
    python scripts/check_site_behaviour.py --emit /tmp/x.js   # just write it

Requires ``node`` on PATH. There is no browser and no layout engine here:
this says nothing about whether any of it *renders* correctly.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
JS_DIR = REPO_ROOT / "tests" / "js"
sys.path.insert(0, str(REPO_ROOT / "src"))


def bundle() -> str:
    from dynasty.reel import reel_assets
    from dynasty.myteam import _MYTEAM_JS

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from check_site_js import pos_color_json  # noqa: E402

    reel_js, _ = reel_assets()
    myteam_js = _MYTEAM_JS.replace("__POS_COLOR__", pos_color_json())

    parts = [
        "// ---- tests/js/dom_stub.js ----",
        (JS_DIR / "dom_stub.js").read_text(encoding="utf-8"),
        "// ---- tests/js/fixtures.js ----",
        (JS_DIR / "fixtures.js").read_text(encoding="utf-8"),
        "// ---- reel.py:_REEL_JS (verbatim) ----",
        reel_js,
        "// ---- myteam.py:_MYTEAM_JS (verbatim) ----",
        myteam_js,
        "// ---- tests/js/assertions.js ----",
        (JS_DIR / "assertions.js").read_text(encoding="utf-8"),
    ]
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--emit", type=Path,
                    help="Write the assembled bundle here and exit.")
    args = ap.parse_args()

    src = bundle()
    if args.emit:
        args.emit.write_text(src, encoding="utf-8")
        print(f"Wrote {args.emit} ({len(src):,} chars)")
        return 0

    node = shutil.which("node")
    if not node:
        print("node not on PATH - cannot run the behaviour harness.")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "harness.js"
        path.write_text(src, encoding="utf-8")
        proc = subprocess.run([node, str(path)], text=True)
        return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
