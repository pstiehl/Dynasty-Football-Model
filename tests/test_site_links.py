"""Every internal link in the built site must resolve to a file on disk.

The regression test for the 2026-09-22 outage: every nav link on every
player detail page 404ed. ``_site_header`` emitted bare filenames
(``rankings.html``), detail pages are written into
``dynasty_site/players/``, and a browser resolves a relative href against
the directory of the page it is on. Seven tabs, seven 404s, on hundreds of
pages. Phil's report was "clicking between tabs 404s, go back a page and
re-click".

Why the existing tests could not catch it
-----------------------------------------
``tests/support/render_chrome.py`` asserts on this exact nav and passed
throughout the outage -- it asserted ``href="rankings.html"`` is present,
which is *correct on a root page*. The bug only exists once you know where
the file was written. So the missing ingredient was never a stronger
assertion about the markup; it was the page's own path. Every check here
resolves a link **relative to the file containing it**.

The work happens in two places:

* ``tests/support/link_audit.py`` -- the resolver. Stdlib only.
* ``tests/support/audit_site_links.py`` -- renders the page skeleton into a
  tmp dir and runs the resolver over it.

The second is executed as a **subprocess**, for the reason given in
``tests/test_site_chrome.py``: it stubs missing third-party modules into
``sys.modules``, and importing it here would hand those stubs to every test
that ran afterwards.

Scope, stated honestly: the skeleton covers the site chrome and the real
per-prospect builder, which is where this bug lived. It does not build the
engine-backed pages, so a dead link introduced in a body table on
``league.html`` would not be caught here. ``test_built_site_has_no_dead_links``
below closes that gap when a real build is present -- it audits
``dynasty_site/`` if the working tree has one and skips otherwise, so CI
after a full build checks everything and a bare checkout still checks the
chrome.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "tests" / "support" / "audit_site_links.py"
BUILT_SITE = REPO_ROOT / "dynasty_site"

sys.path.insert(0, str(REPO_ROOT / "tests" / "support"))


class TestSiteLinks(unittest.TestCase):
    def test_skeleton_has_no_dead_internal_links(self):
        """The nav on a players/ page must point back out of players/."""
        proc = subprocess.run(
            [sys.executable, str(HELPER)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        self.assertEqual(
            proc.returncode, 0,
            "internal links do not resolve:\n"
            f"{proc.stdout}\n{proc.stderr}",
        )
        self.assertIn("All internal links resolve.", proc.stdout)

    def test_nested_pages_get_the_subdir_prefix(self):
        """A direct assertion on the seam, so a failure names the cause.

        The audit above proves the *symptom* is gone. This proves the
        *mechanism* is the prefix rather than something incidental, and it
        fails loudly if someone reintroduces a nested call site that
        forgets it.
        """
        import link_audit  # noqa: PLC0415  (tests/support on sys.path)

        proc = subprocess.run(
            [sys.executable, "-c", PREFIX_PROBE],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0,
                         f"{proc.stdout}\n{proc.stderr}")
        root_nav, nested_nav = (
            line.split("\t", 1)[1].split(" ")
            for line in proc.stdout.strip().splitlines()
            if line.startswith(("ROOT\t", "NESTED\t"))
        )
        self.assertTrue(root_nav, "root nav emitted no links")
        self.assertTrue(nested_nav, "nested nav emitted no links")
        for href in root_nav:
            self.assertFalse(
                href.startswith("../"),
                f"root page nav href {href!r} must not be prefixed",
            )
        for href in nested_nav:
            self.assertTrue(
                href.startswith("../"),
                f"players/ nav href {href!r} would resolve to "
                f"/players/{href} and 404",
            )
        self.assertFalse(link_audit.is_template(nested_nav[0]))

    def test_built_site_has_no_dead_links(self):
        """Audit a real build when the working tree has one.

        Skipped rather than failed when absent: a bare checkout has no
        ``dynasty_site/``, and making the chrome regression test depend on a
        full engine run is how it stops being run.
        """
        import link_audit  # noqa: PLC0415

        if not (BUILT_SITE / "rankings.html").exists():
            raise unittest.SkipTest(
                "no built site at dynasty_site/ -- run "
                "`python -m dynasty.launcher_headless` first"
            )
        result = link_audit.audit_site(BUILT_SITE)
        self.assertTrue(result.ok, result.report())

    def test_built_site_has_a_404_page(self):
        if not (BUILT_SITE / "rankings.html").exists():
            raise unittest.SkipTest("no built site at dynasty_site/")
        self.assertTrue(
            (BUILT_SITE / "404.html").exists(),
            "the build must emit 404.html so a dead link degrades to "
            "something with a way back into the site",
        )


# Run out-of-process for the sys.modules-stub reason above.
PREFIX_PROBE = r"""
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, "tests/support")
sys.path.insert(0, "src")
import audit_site_links as h
h._install_stubs()
import link_audit
from dynasty import report
ts = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
root = report._site_header("rankings", ts, "Superflex PPR")
nested = report._site_header("rankings", ts, "Superflex PPR",
                             prefix=report.SUBDIR_PREFIX)
print("ROOT\t" + " ".join(link_audit.nav_targets(root)))
print("NESTED\t" + " ".join(link_audit.nav_targets(nested)))
"""


if __name__ == "__main__":
    unittest.main()
