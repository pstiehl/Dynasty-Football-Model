"""Analytics wiring: both token states, asserted on the rendered site.

Two halves, because neither alone is enough:

* ``tests/support/render_analytics.py`` renders the chrome with and without
  ``DFM_ANALYTICS_TOKEN`` and asserts on the markup — the beacon appears
  exactly once with the token interpolated, or not at all with nothing left
  behind.
* ``tests/js/analytics_head_tests.mjs`` **executes** the resulting pages'
  scripts over a DOM shim. This is the half that matters for ``_page``:
  PR #71 was a live outage caused by script *order* in exactly this
  function, and order is invisible to both ``node --check`` and reading the
  Python source.

Both run as subprocesses. The Python helper installs stand-in modules into
``sys.modules`` so ``report.py`` imports without the dependency stack, and
importing it here would hand those stubs to every test that ran afterwards.
The subprocess boundary is the containment — same reasoning as
``tests/test_site_chrome.py``.

The node half skips cleanly when node is not on PATH; the Python half always
runs.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER = REPO_ROOT / "tests" / "support" / "render_analytics.py"
JS_TEST = REPO_ROOT / "tests" / "js" / "analytics_head_tests.mjs"


class TestAnalytics(unittest.TestCase):
    def test_markup_is_correct_in_both_token_states(self):
        proc = subprocess.run(
            [sys.executable, str(RENDER)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        self.assertEqual(
            proc.returncode, 0,
            f"analytics render assertions failed:\n{proc.stdout}\n{proc.stderr}",
        )
        self.assertIn("checks passed", proc.stdout)

    def test_pages_still_execute_with_the_beacon_present(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not on PATH")

        with tempfile.TemporaryDirectory() as tmp:
            emit = subprocess.run(
                [sys.executable, str(RENDER), "--emit", tmp],
                capture_output=True, text=True, cwd=str(REPO_ROOT),
            )
            self.assertEqual(
                emit.returncode, 0,
                f"could not render pages:\n{emit.stdout}\n{emit.stderr}",
            )
            proc = subprocess.run(
                [node, str(JS_TEST),
                 str(Path(tmp) / "configured.html"),
                 str(Path(tmp) / "unconfigured.html")],
                capture_output=True, text=True, cwd=str(REPO_ROOT),
            )
            self.assertEqual(
                proc.returncode, 0,
                f"page scripts failed with the beacon present:"
                f"\n{proc.stdout}\n{proc.stderr}",
            )
            self.assertIn("checks passed", proc.stdout)


if __name__ == "__main__":
    unittest.main()
