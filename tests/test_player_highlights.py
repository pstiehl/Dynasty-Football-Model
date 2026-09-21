"""The site-wide player-highlights renderer, and the brand it is wrapped in.

Three things are pinned here.

1. **The shared renderer behaves.** ``tests/js/player_highlights_tests.js``
   runs the shipped JavaScript under node against an artifact shaped like
   the one ``highlights.build_index`` writes. That covers the id joins, the
   week bucketing carried over from PR #65, the empty states and escaping.

2. **The Python marker emitter agrees with the JavaScript one.** The site
   renders player names with two engines, so there are two marker emitters
   (``player_chip`` here, ``DFMHL.chip`` there) over one renderer. If their
   markup drifts, one half of the site silently loses the affordance, so
   the two are compared directly.

3. **The brand lives in one place.** ``dynasty.branding`` exists because
   the previous rebrand was scattered string literals and a later pass
   still found two it had missed.

None of this needs the engine, a database, or the network, so it runs under
``scripts/run_tests_stdlib.py`` as well as pytest. What it cannot do is
prove any of it *renders*: there is no browser here.
"""
from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dynasty import player_highlights as hl  # noqa: E402
from dynasty.branding import (  # noqa: E402
    SITE_NAME,
    page_title,
    site_name_html,
)

NODE = shutil.which("node")


def _marker(chip_html: str) -> dict:
    """Pull the data-dfm-player payload back out of a rendered chip."""
    m = re.search(r"data-dfm-player='([^']*)'", chip_html)
    if not m:
        m = re.search(r'data-dfm-player="([^"]*)"', chip_html)
    assert m, f"no marker in chip: {chip_html}"
    return json.loads(html.unescape(m.group(1)))


class TestBranding(unittest.TestCase):
    def test_site_name_is_the_owners_name(self):
        self.assertEqual(SITE_NAME, "Next Level Dynasty Football")

    def test_dead_rebrand_proposals_are_not_used(self):
        """PR #52 proposed "Box Score Dynasty"; the owner moved on.

        Pinned so a later merge of that branch cannot quietly reinstate it.
        """
        self.assertNotIn("Box Score", SITE_NAME)
        self.assertNotIn("Kings of", SITE_NAME)

    def test_page_title_composes(self):
        self.assertEqual(page_title("Methodology"),
                         f"{SITE_NAME} \u2014 Methodology")
        self.assertEqual(page_title(), SITE_NAME)

    def test_site_name_html_accents_one_word_and_loses_nothing(self):
        got = site_name_html()
        self.assertIn('<span class="accent">Dynasty</span>', got)
        # Stripping the markup must give back exactly the plain name: a
        # split that dropped or duplicated a word would still "contain"
        # the accent span.
        self.assertEqual(re.sub(r"<[^>]+>", "", got), SITE_NAME)


class TestPlayerChip(unittest.TestCase):
    def test_chip_offers_highlights_without_replacing_the_name_link(self):
        chip = hl.player_chip(
            "Ja'Marr Chase", gsis="00-0036900", position="WR",
            href="players/jamarr-chase-036900.html",
        )
        self.assertIn('href="players/jamarr-chase-036900.html"', chip)
        self.assertIn("data-dfm-player=", chip)
        # A button, so with JS off it is inert rather than a dead link.
        self.assertIn("<button", chip)
        self.assertNotIn("<a href=\"#\"", chip)

    def test_chip_works_with_no_ids_at_all(self):
        """Comp tables know only a name. That must still resolve by name."""
        chip = hl.player_chip("Walter Payton")
        self.assertEqual(_marker(chip), {"n": "Walter Payton"})

    def test_chip_carries_whichever_ids_the_call_site_holds(self):
        self.assertEqual(
            _marker(hl.player_chip("Josh Allen", gsis="00-0034857")),
            {"n": "Josh Allen", "g": "00-0034857"},
        )
        self.assertEqual(
            _marker(hl.player_chip("Josh Allen", sleeper_id="3294")),
            {"n": "Josh Allen", "s": "3294"},
        )

    def test_chip_trims_ids(self):
        """A leading space on a gsis id cost 70 ranking rows once."""
        self.assertEqual(
            _marker(hl.player_chip("Josh Allen", gsis=" 00-0034857 "))["g"],
            "00-0034857",
        )

    def test_chip_escapes_everything_it_interpolates(self):
        nasty = '<script>alert(1)</script>'
        chip = hl.player_chip(nasty, href='"><img onerror=1>')
        self.assertNotIn("<script>", chip)
        self.assertNotIn("<img onerror", chip)
        # The name still round-trips through the marker intact.
        self.assertEqual(_marker(chip)["n"], nasty)

    def test_extra_html_is_preserved_for_existing_badges(self):
        """Comp rows already render era / washed-out chips next to a name."""
        chip = hl.player_chip(
            "Aaron Brooks", extra_html='<span class="era-chip">1999</span>',
        )
        self.assertIn('<span class="era-chip">1999</span>', chip)

    def test_inline_block_is_marked_for_the_shared_renderer(self):
        block = hl.inline_block("Josh Allen", gsis="00-0034857")
        self.assertIn(hl.INLINE_ATTR, block)
        self.assertEqual(_inline_marker(block)["g"], "00-0034857")
        # Something honest on screen before the fetch resolves.
        self.assertIn("Loading highlights", block)


def _inline_marker(block_html: str) -> dict:
    m = re.search(r"data-dfm-highlights='([^']*)'", block_html)
    assert m, block_html
    return json.loads(html.unescape(m.group(1)))


class TestPythonAndJsMarkersAgree(unittest.TestCase):
    """The two emitters must produce the same marker for the same player.

    This is the seam where the site could silently lose the affordance on
    half its pages, because nothing else would fail if the attribute name
    or the payload keys drifted on one side only.
    """

    def test_attribute_name_matches(self):
        js = hl.PLAYER_HIGHLIGHTS_JS
        self.assertIn(hl.PLAYER_ATTR, js)
        self.assertIn(hl.INLINE_ATTR, js)

    @unittest.skipUnless(NODE, "node not available")
    def test_payload_keys_match(self):
        """Render the same player through both emitters and compare."""
        with tempfile.TemporaryDirectory() as td:
            js_path = Path(td) / "hl.js"
            js_path.write_text(
                "global.window = {};\n"
                "global.fetch = () => Promise.resolve({ok:false,status:404});\n"
                + hl.PLAYER_HIGHLIGHTS_JS
                + "\nprocess.stdout.write(DFMHL.chip('Josh Allen',"
                  "{gsis:'00-0034857',pos:'QB',href:'players/x.html'}));\n",
                encoding="utf-8",
            )
            proc = subprocess.run([NODE, str(js_path)],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)

        from_js = _marker(proc.stdout)
        from_py = _marker(hl.player_chip(
            "Josh Allen", gsis="00-0034857", position="QB",
            href="players/x.html",
        ))
        self.assertEqual(from_py, from_js)


class TestSharedRendererUnderNode(unittest.TestCase):
    @unittest.skipUnless(NODE, "node not available")
    def test_js_parses(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hl.js"
            p.write_text(hl.PLAYER_HIGHLIGHTS_JS, encoding="utf-8")
            proc = subprocess.run([NODE, "--check", str(p)],
                                  capture_output=True, text=True)
            self.assertEqual(
                proc.returncode, 0,
                f"PLAYER_HIGHLIGHTS_JS does not parse:\n{proc.stderr}")

    @unittest.skipUnless(NODE, "node not available")
    def test_renderer_assertions(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hl.js"
            p.write_text(hl.PLAYER_HIGHLIGHTS_JS, encoding="utf-8")
            suite = REPO_ROOT / "tests" / "js" / "player_highlights_tests.js"
            proc = subprocess.run([NODE, str(suite), str(p)],
                                  capture_output=True, text=True)
            self.assertEqual(
                proc.returncode, 0,
                f"renderer assertions failed:\n{proc.stdout}\n{proc.stderr}")


class TestOneImplementation(unittest.TestCase):
    """Guard the thing this PR was for: one renderer, not five.

    The previous round shipped two hand-synchronised clip cards
    (``reel.clipCard`` and ``myteam.mtClipCard``) plus a second copy of the
    week bucketing in myteam.js. If a page grows its own again, the owner's
    "highlights everywhere" turns back into "highlights, differently,
    everywhere".
    """

    def test_myteam_does_not_reimplement_clip_cards_or_bucketing(self):
        src = (REPO_ROOT / "src" / "dynasty" / "myteam.py").read_text()
        for gone in ("function mtClipCard", "function mtGroup"):
            self.assertNotIn(gone, src,
                             f"myteam.py grew its own clip renderer again: {gone}")
        self.assertIn("DFMHL.renderRoster", src,
                      "myteam.py must render film through the shared renderer")

    def test_reel_week_helpers_delegate_rather_than_duplicate(self):
        src = (REPO_ROOT / "src" / "dynasty" / "reel.py").read_text()
        # Each of these must be a one-line delegate to DFMHL.
        for fn in ("leadWeekKey", "weekHeading", "weekIsComplete",
                   "clipWeekKey", "rankClip", "windowLabel"):
            m = re.search(r"function " + fn + r"\([^)]*\) \{ return DFMHL\.",
                          src)
            self.assertIsNotNone(
                m, f"reel.py's {fn} is no longer a delegate to DFMHL")

    def test_reel_no_longer_builds_a_page(self):
        src = (REPO_ROOT / "src" / "dynasty" / "reel.py").read_text()
        self.assertNotIn("def build_reel", src)
        report = (REPO_ROOT / "src" / "dynasty" / "report.py").read_text()
        self.assertNotIn("build_reel", report)
        self.assertNotIn('"reel.html"', report)

    def test_manager_score_has_one_implementation(self):
        """The feature moved; it was not copied."""
        ms = (REPO_ROOT / "src" / "dynasty" / "managerscore.py").read_text()
        self.assertIn("def manager_score_section", ms)
        self.assertIn("def build_manager_score_pointer", ms)
        self.assertNotIn("def build_manager_score(", ms)

    def test_shared_renderer_is_injected_site_wide(self):
        """Not per page: a page nobody wires up must still get the chips."""
        report = (REPO_ROOT / "src" / "dynasty" / "report.py").read_text()
        page_fn = report[report.index("def _page("):]
        page_fn = page_fn[:page_fn.index("\n\n\n")]
        self.assertIn("_hl.assets()", page_fn)
        self.assertIn("DFM_BASE", page_fn)

    def test_manager_score_global_does_not_collide_with_the_reel(self):
        """Both scripts share myteam.html now.

        Two top-level bindings of the same name in one document is a
        SyntaxError that takes the whole page down, and reel.js declares
        ``const SLEEPER``.
        """
        ms = (REPO_ROOT / "src" / "dynasty" / "managerscore_js.py").read_text()
        self.assertNotIn("var SLEEPER =", ms)
        self.assertIn("var MS_SLEEPER =", ms)


if __name__ == "__main__":
    unittest.main()
