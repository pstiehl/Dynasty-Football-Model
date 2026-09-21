"""The brand and the nav, checkable without the dependency stack.

``tests/test_v2_2_penalties.py`` asserts the same things against a real
built site and is the authoritative test. This one covers the case where
that cannot run: a checkout with nothing installed. It exists because this
PR's whole subject — the site's name and the shape of its nav — is
otherwise only verifiable behind httpx, pydantic, numpy, the engine and a
database.

The work happens in ``tests/support/render_chrome.py``, which is run as a
**subprocess**. That script stubs missing third-party modules into
``sys.modules``; importing it here would hand those stubs to every test
that ran afterwards, including the ones that need the real engine. The
subprocess boundary is the containment.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "tests" / "support" / "render_chrome.py"


class TestSiteChrome(unittest.TestCase):
    def test_chrome_renders_the_current_brand_and_nav(self):
        proc = subprocess.run(
            [sys.executable, str(HELPER)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        self.assertEqual(
            proc.returncode, 0,
            f"site chrome assertions failed:\n{proc.stdout}\n{proc.stderr}",
        )
        self.assertIn("checks passed", proc.stdout)


if __name__ == "__main__":
    unittest.main()
