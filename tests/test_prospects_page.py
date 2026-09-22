"""prospects.html renders no formatting leaks and one coherent class view.

Wrapper around ``tests/support/render_prospects.py``, which does the work.
It is executed as a **subprocess** because it stubs missing third-party
modules into ``sys.modules`` and importing it here would hand those stubs
to every test that ran afterwards -- the same containment reason given in
``tests/test_site_chrome.py``.

The pure logic is asserted directly in ``tests/test_prospect_view.py``;
this covers what only exists after interpolation.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "tests" / "support" / "render_prospects.py"


class TestProspectsPage(unittest.TestCase):
    def test_page_renders_without_leaks_or_class_mixing(self):
        proc = subprocess.run(
            [sys.executable, str(HELPER)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        self.assertEqual(
            proc.returncode, 0,
            f"prospects-page assertions failed:\n{proc.stdout}\n{proc.stderr}",
        )
        self.assertIn("checks passed", proc.stdout)


if __name__ == "__main__":
    unittest.main()
