"""Manager Score — value artifact, KTC value history, page, and page JS.

Runnable with the stdlib alone::

    python3 -m unittest discover -s tests -p 'test_managerscore*.py' -v
    python3 tests/test_managerscore.py          # same thing

Deliberately no pytest, no network, no pip. The build environment for this
work had none of them, and neither does a fresh clone.

Two things are worth knowing about the shape of these tests.

1. The scoring maths is **not** tested here. It lives only in
   ``dynasty.managerscore_js.MANAGERSCORE_CORE_JS``, because the page reads
   Sleeper live in the browser, so there is no Python implementation to
   assert against. This file shells out to ``node`` to run
   ``tests/js/managerscore_core_tests.js`` (arithmetic, shrinkage,
   degradation) and ``tests/js/managerscore_dom_tests.js`` (Sleeper JSON
   shapes against a stub DOM and a stub API). Those two files hold the bulk
   of the verification. If ``node`` is absent the JS tests skip loudly
   rather than passing silently.

2. ``dynasty.report`` cannot be imported without ``httpx`` (it pulls the
   engine and the KTC source adapter), so the page-render test installs a
   minimal stub ``dynasty.report`` in ``sys.modules`` first. That is sound
   because ``build_manager_score`` imports ``_page``/``_site_header``
   lazily, inside the function, specifically to avoid an import cycle. The
   nav wiring in the real ``report.py`` is checked by reading its source.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dynasty import ktc_history, managerscore  # noqa: E402
from dynasty.managerscore_js import (  # noqa: E402
    MANAGERSCORE_CORE_JS,
    MANAGERSCORE_UI_JS,
)

NODE = shutil.which("node")


def _snapshot(players, captured_at="2026-09-20T11:00:00+00:00"):
    """Build a minimal ``ktc.v1`` snapshot dict."""
    return {
        "schema": "ktc.v1",
        "captured_at": captured_at,
        "source_url": "https://keeptradecut.com/dynasty-rankings",
        "players": players,
    }


def _player(ktc_id, name, position, sf_value, mfl_id=None, qb_value=None):
    return {
        "ktc_id": ktc_id,
        "name": name,
        "position": position,
        "team": "BUF",
        "age": 25.0,
        "birthday": None,
        "rookie": False,
        "mfl_id": mfl_id,
        "superflex": {"value": sf_value, "rank": 1},
        "one_qb": {"value": qb_value if qb_value is not None else sf_value, "rank": 1},
    }


# ---------------------------------------------------------------------------
# Compact dated value history
# ---------------------------------------------------------------------------

class CompactValuesTests(unittest.TestCase):
    def test_players_keyed_by_ktc_id_picks_keyed_by_label(self):
        snap = _snapshot([
            _player(1, "Josh Allen", "QB", 9995),
            _player(2, "Some Back", "RB", 6000),
            _player(99, "2027 Mid 1st", "RDP", 5677),
        ])
        out = ktc_history.compact_values(snap)
        self.assertEqual(out["sf"], {"1": 9995, "2": 6000})
        self.assertEqual(out["picks"], {"2027 Mid 1st": 5677})
        self.assertNotIn("99", out["sf"], "picks must not pollute the player map")

    def test_floor_is_lowest_published_player_value(self):
        """The floor exists so off-board assets can be priced as *cheap*
        rather than *free*. Pricing them at zero would hand a windfall to
        whoever acquired them."""
        snap = _snapshot([
            _player(1, "A", "QB", 9000),
            _player(2, "B", "WR", 491),
            _player(3, "2027 Mid 1st", "RDP", 100),
        ])
        out = ktc_history.compact_values(snap)
        self.assertEqual(out["floor"], 491)

    def test_floor_ignores_picks(self):
        snap = _snapshot([
            _player(1, "A", "QB", 9000),
            _player(9, "2027 Late 4th", "RDP", 3),
        ])
        self.assertEqual(ktc_history.compact_values(snap)["floor"], 9000)

    def test_date_taken_from_captured_at(self):
        out = ktc_history.compact_values(
            _snapshot([_player(1, "A", "QB", 10)], "2026-03-04T05:06:07+00:00")
        )
        self.assertEqual(out["date"], "2026-03-04")

    def test_missing_capture_date_yields_no_date(self):
        snap = _snapshot([_player(1, "A", "QB", 10)])
        snap.pop("captured_at")
        self.assertIsNone(ktc_history.compact_values(snap)["date"])

    def test_empty_and_malformed_rows_are_survivable(self):
        for payload in ({}, {"players": None}, {"players": [None, 3, "x", {}]}):
            out = ktc_history.compact_values(payload)
            self.assertEqual(out["sf"], {})
            self.assertIsNone(out["floor"])

    def test_non_numeric_values_are_dropped_not_coerced(self):
        snap = _snapshot([
            _player(1, "A", "QB", None),
            _player(2, "B", "WR", "not a number"),
            _player(3, "C", "TE", "4200"),
        ])
        out = ktc_history.compact_values(snap)
        self.assertEqual(out["sf"], {"3": 4200})

    def test_record_stays_small(self):
        """Size is the entire reason this artifact exists: the full snapshot
        is ~360 KB/day and therefore never retained."""
        players = [_player(i, "Player %d" % i, "WR", 9000 - i) for i in range(500)]
        blob = json.dumps(
            ktc_history.compact_values(_snapshot(players)), separators=(",", ":")
        )
        self.assertLess(len(blob), 40_000, "compact record should be ~KBs, not 100s")


class HistoryPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.hist = self.tmp / "history"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_then_load_roundtrip(self):
        snap = _snapshot([_player(1, "A", "QB", 9000)], "2026-09-20T10:00:00+00:00")
        path = ktc_history.save_history_point(snap, history_dir=self.hist)
        self.assertIsNotNone(path)
        self.assertTrue(path.exists())
        self.assertEqual(path.name, "ktc_values_2026-09-20.json")
        loaded = ktc_history.load_history_point("2026-09-20", self.hist)
        self.assertEqual(loaded["sf"], {"1": 9000})

    def test_save_is_idempotent_per_day(self):
        for value in (9000, 8000, 7000):
            ktc_history.save_history_point(
                _snapshot([_player(1, "A", "QB", value)], "2026-09-20T10:00:00+00:00"),
                history_dir=self.hist,
            )
        self.assertEqual(
            ktc_history.available_history_dates(self.hist), ["2026-09-20"]
        )
        # Last write wins rather than accumulating duplicates.
        self.assertEqual(
            ktc_history.load_history_point("2026-09-20", self.hist)["sf"], {"1": 7000}
        )

    def test_index_tracks_retained_dates(self):
        for day in ("2026-09-18", "2026-09-20", "2026-09-19"):
            ktc_history.save_history_point(
                _snapshot([_player(1, "A", "QB", 1)], day + "T10:00:00+00:00"),
                history_dir=self.hist,
            )
        index = json.loads((self.hist / "index.json").read_text())
        self.assertEqual(index["dates"], ["2026-09-18", "2026-09-19", "2026-09-20"])
        self.assertEqual(index["earliest"], "2026-09-18")
        self.assertEqual(index["latest"], "2026-09-20")
        self.assertEqual(index["count"], 3)

    def test_undated_snapshot_writes_nothing(self):
        snap = _snapshot([_player(1, "A", "QB", 1)])
        snap.pop("captured_at")
        self.assertIsNone(
            ktc_history.save_history_point(snap, history_dir=self.hist)
        )
        self.assertEqual(ktc_history.available_history_dates(self.hist), [])

    def test_missing_dir_and_stray_files_degrade_quietly(self):
        self.assertEqual(
            ktc_history.available_history_dates(self.tmp / "nope"), []
        )
        self.hist.mkdir(parents=True)
        (self.hist / "README.md").write_text("x")
        (self.hist / "ktc_values_not-a-date.json").write_text("{}")
        (self.hist / "index.json").write_text("{}")
        self.assertEqual(ktc_history.available_history_dates(self.hist), [])

    def test_corrupt_point_returns_none_rather_than_raising(self):
        self.hist.mkdir(parents=True)
        (self.hist / "ktc_values_2026-09-20.json").write_text("{not json")
        self.assertIsNone(ktc_history.load_history_point("2026-09-20", self.hist))
        self.assertIsNone(ktc_history.load_history_point("2026-01-01", self.hist))


class NearestHistoryDateTests(unittest.TestCase):
    DATES = ["2026-09-01", "2026-09-10", "2026-10-01"]

    def test_picks_latest_on_or_before(self):
        self.assertEqual(
            ktc_history.nearest_history_date("2026-09-15", self.DATES), "2026-09-10"
        )

    def test_exact_match_is_used(self):
        self.assertEqual(
            ktc_history.nearest_history_date("2026-09-10", self.DATES), "2026-09-10"
        )

    def test_never_reaches_forward_in_time(self):
        """The single easiest way to fabricate a point-in-time value would be
        to use the next available snapshot after the transaction. It must
        return None instead, so the caller falls back to current and says so."""
        self.assertIsNone(
            ktc_history.nearest_history_date("2026-08-31", self.DATES)
        )

    def test_empty_inputs(self):
        self.assertIsNone(ktc_history.nearest_history_date("2026-09-15", []))
        self.assertIsNone(ktc_history.nearest_history_date("", self.DATES))
        self.assertIsNone(ktc_history.nearest_history_date(None, self.DATES))


# ---------------------------------------------------------------------------
# Crosswalk
# ---------------------------------------------------------------------------

class CrosswalkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, text):
        path = self.tmp / "dp_playerids.csv"
        path.write_text(text, encoding="utf-8")
        return path

    def test_maps_ktc_and_mfl_to_sleeper(self):
        path = self._write(
            "mfl_id,sleeper_id,ktc_id,name\n"
            "13589,4984,365,Josh Allen\n"
            "15257,7564,1004,Ja'Marr Chase\n"
        )
        by_ktc, by_mfl = managerscore.load_ktc_to_sleeper(path)
        self.assertEqual(by_ktc["365"], "4984")
        self.assertEqual(by_mfl["15257"], "7564")

    def test_float_formatted_ids_are_normalised(self):
        """dynastyprocess ships numeric columns that pandas may have written
        as floats; "365.0" must still join to ktc_id 365."""
        path = self._write("mfl_id,sleeper_id,ktc_id\n13589.0,4984.0,365.0\n")
        by_ktc, _ = managerscore.load_ktc_to_sleeper(path)
        self.assertEqual(by_ktc["365"], "4984")

    def test_rows_without_sleeper_id_are_skipped(self):
        path = self._write("mfl_id,sleeper_id,ktc_id\n1,,365\n2,4984,366\n")
        by_ktc, _ = managerscore.load_ktc_to_sleeper(path)
        self.assertEqual(by_ktc, {"366": "4984"})

    def test_missing_file_and_wrong_schema_degrade_to_empty(self):
        by_ktc, by_mfl = managerscore.load_ktc_to_sleeper(self.tmp / "absent.csv")
        self.assertEqual((by_ktc, by_mfl), ({}, {}))
        path = self._write("some,other,columns\n1,2,3\n")
        self.assertEqual(managerscore.load_ktc_to_sleeper(path), ({}, {}))

    def test_real_crosswalk_if_present(self):
        path = REPO_ROOT / "data" / "consensus" / "dp_playerids.csv"
        if not path.exists():
            self.skipTest("checked-in crosswalk not present")
        by_ktc, by_mfl = managerscore.load_ktc_to_sleeper(path)
        self.assertGreater(len(by_ktc), 100)
        self.assertGreater(len(by_mfl), 100)


# ---------------------------------------------------------------------------
# Value artifact
# ---------------------------------------------------------------------------

class ValuesArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.consensus = self.tmp / "consensus"
        self.consensus.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, players=None, crosswalk=True):
        players = players if players is not None else [
            _player(365, "Josh Allen", "QB", 9995, mfl_id="13589"),
            _player(1004, "Ja'Marr Chase", "WR", 9982, mfl_id="15257"),
            _player(777, "Unmapped Guy", "TE", 491, mfl_id=None),
            _player(9001, "2027 Mid 1st", "RDP", 5677),
        ]
        (self.consensus / managerscore.KTC_LATEST).write_text(
            json.dumps(_snapshot(players)), encoding="utf-8"
        )
        if crosswalk:
            (self.consensus / managerscore.CROSSWALK).write_text(
                "mfl_id,sleeper_id,ktc_id\n"
                "13589,4984,365\n"
                "15257,7564,1004\n",
                encoding="utf-8",
            )

    def test_happy_path(self):
        self._seed()
        art = managerscore.build_values_artifact(consensus_dir=self.consensus)
        self.assertTrue(art["available"])
        self.assertEqual(art["by_sleeper"]["4984"][0], 9995)
        self.assertEqual(art["by_sleeper"]["4984"][1], "Josh Allen")
        self.assertEqual(art["by_sleeper"]["4984"][3], 365, "ktc_id must be carried")
        self.assertEqual(art["ktc"]["floor"], 491)
        self.assertEqual(art["picks"], {"2027 Mid 1st": 5677})

    def test_ktc_id_is_present_because_history_is_keyed_by_it(self):
        """Without ktc_id in the artifact the page could never look a past
        value up, because the dated records are keyed by ktc_id."""
        self._seed()
        art = managerscore.build_values_artifact(consensus_dir=self.consensus)
        self.assertIn("ktc_id", art["fields"])
        for entry in art["by_sleeper"].values():
            self.assertIsInstance(entry[3], int)

    def test_picks_excluded_from_player_map(self):
        self._seed()
        art = managerscore.build_values_artifact(consensus_dir=self.consensus)
        names = [v[1] for v in art["by_sleeper"].values()]
        self.assertNotIn("2027 Mid 1st", names)

    def test_unmappable_players_are_simply_absent(self):
        self._seed()
        art = managerscore.build_values_artifact(consensus_dir=self.consensus)
        self.assertEqual(len(art["by_sleeper"]), 2)
        self.assertEqual(art["ktc"]["n_players"], 3, "picks excluded from count")

    def test_mfl_fallback_join(self):
        """A player missing from the ktc_id column can still bridge via
        mfl_id, which KTC ships in its own payload."""
        (self.consensus / managerscore.KTC_LATEST).write_text(
            json.dumps(_snapshot([_player(365, "Josh Allen", "QB", 9995, mfl_id="13589")])),
            encoding="utf-8",
        )
        (self.consensus / managerscore.CROSSWALK).write_text(
            "mfl_id,sleeper_id,ktc_id\n13589,4984,\n", encoding="utf-8"
        )
        art = managerscore.build_values_artifact(consensus_dir=self.consensus)
        self.assertEqual(art["by_sleeper"]["4984"][0], 9995)

    def test_missing_ktc_snapshot_degrades_with_a_reason(self):
        art = managerscore.build_values_artifact(consensus_dir=self.consensus)
        self.assertFalse(art["available"])
        self.assertEqual(art["by_sleeper"], {})
        self.assertTrue(any("ktc_latest" in n for n in art["notes"]))

    def test_malformed_ktc_snapshot_degrades_with_a_reason(self):
        (self.consensus / managerscore.KTC_LATEST).write_text("{not json")
        art = managerscore.build_values_artifact(consensus_dir=self.consensus)
        self.assertFalse(art["available"])
        self.assertTrue(any("could not be parsed" in n for n in art["notes"]))

    def test_missing_crosswalk_degrades_with_a_reason(self):
        self._seed(crosswalk=False)
        art = managerscore.build_values_artifact(consensus_dir=self.consensus)
        self.assertFalse(art["available"])
        self.assertEqual(art["by_sleeper"], {})
        self.assertTrue(any("dp_playerids" in n for n in art["notes"]))

    def test_no_history_is_stated_plainly(self):
        """The whole honesty question hinges on this note existing when there
        is no history, because that is the current state of the repo."""
        self._seed()
        art = managerscore.build_values_artifact(consensus_dir=self.consensus)
        self.assertFalse(art["history"]["available"])
        self.assertEqual(art["history"]["dates"], [])
        self.assertTrue(
            any("cannot be backfilled" in n for n in art["notes"]),
            art["notes"],
        )

    def test_history_is_advertised_when_present(self):
        self._seed()
        hist = self.consensus / "history"
        for day in ("2026-09-19", "2026-09-20"):
            ktc_history.save_history_point(
                _snapshot([_player(365, "Josh Allen", "QB", 8000)],
                          day + "T10:00:00+00:00"),
                history_dir=hist,
            )
        art = managerscore.build_values_artifact(consensus_dir=self.consensus)
        self.assertTrue(art["history"]["available"])
        self.assertEqual(art["history"]["count"], 2)
        self.assertEqual(art["history"]["earliest"], "2026-09-19")
        self.assertEqual(
            art["history"]["file_template"], "ktc_values_{date}.json"
        )

    def test_never_raises_on_any_missing_input(self):
        """generate_site must not lose the whole site build over this page."""
        for kwargs in (
            {"consensus_dir": self.tmp / "absent"},
            {"consensus_dir": self.consensus},
            {"consensus_dir": self.consensus, "history_dir": self.tmp / "nope"},
        ):
            art = managerscore.build_values_artifact(**kwargs)
            self.assertIn("available", art)

    def test_real_repo_data_maps_most_of_the_board(self):
        consensus = REPO_ROOT / "data" / "consensus"
        if not (consensus / managerscore.KTC_LATEST).exists():
            self.skipTest("checked-in KTC snapshot not present")
        art = managerscore.build_values_artifact(consensus_dir=consensus)
        self.assertTrue(art["available"])
        self.assertGreater(art["ktc"]["n_mapped"], 300)
        # Coverage should be high; a collapse here means the crosswalk broke.
        ratio = art["ktc"]["n_mapped"] / max(1, art["ktc"]["n_players"])
        self.assertGreater(ratio, 0.90, "sleeper id coverage fell below 90%%")
        self.assertIsNotNone(art["ktc"]["floor"])
        self.assertGreater(len(art["picks"]), 0, "KTC pick rows should be present")


class WriteArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.consensus = self.tmp / "consensus"
        self.consensus.mkdir(parents=True)
        (self.consensus / managerscore.KTC_LATEST).write_text(
            json.dumps(_snapshot([_player(365, "Josh Allen", "QB", 9995)])),
            encoding="utf-8",
        )
        (self.consensus / managerscore.CROSSWALK).write_text(
            "mfl_id,sleeper_id,ktc_id\n13589,4984,365\n", encoding="utf-8"
        )
        self.out = self.tmp / "site"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_artifact_to_site_root(self):
        managerscore.write_values_artifact(self.out, consensus_dir=self.consensus)
        path = self.out / managerscore.VALUES_ARTIFACT
        self.assertTrue(path.exists())
        payload = json.loads(path.read_text())
        self.assertEqual(payload["schema"], managerscore.VALUES_SCHEMA)
        self.assertTrue(payload["available"])

    def test_publishes_history_files_for_per_day_fetching(self):
        hist = self.consensus / "history"
        ktc_history.save_history_point(
            _snapshot([_player(365, "A", "QB", 8000)], "2026-09-19T10:00:00+00:00"),
            history_dir=hist,
        )
        managerscore.write_values_artifact(self.out, consensus_dir=self.consensus)
        published = self.out / managerscore.HISTORY_SUBDIR / "ktc_values_2026-09-19.json"
        self.assertTrue(published.exists(), "history must be published next to the page")
        self.assertEqual(json.loads(published.read_text())["sf"], {"365": 8000})

    def test_no_history_dir_created_when_there_is_no_history(self):
        managerscore.write_values_artifact(self.out, consensus_dir=self.consensus)
        self.assertFalse((self.out / managerscore.HISTORY_SUBDIR).exists())


# ---------------------------------------------------------------------------
# Page render
# ---------------------------------------------------------------------------

class PageRenderTests(unittest.TestCase):
    """Render managerscore.html with a stub ``dynasty.report``.

    ``build_manager_score`` imports ``_page``/``_site_header`` lazily to
    avoid an import cycle, which makes the stub both possible and faithful
    to how the real call works.
    """

    @classmethod
    def setUpClass(cls):
        cls._saved = sys.modules.get("dynasty.report")
        stub = types.ModuleType("dynasty.report")
        stub.POSITION_COLOR = {"QB": "#e74c3c"}

        def _site_header(active, latest_ts, league_label):
            return '<header data-active="%s">nav</header>' % active

        def _page(title, header_html, body_html, css_href="assets/style.css"):
            return "<!doctype html><title>%s</title>%s%s" % (
                title, header_html, body_html
            )

        stub._site_header = _site_header
        stub._page = _page
        sys.modules["dynasty.report"] = stub
        cls.html = managerscore.build_manager_score(
            datetime(2026, 9, 21, 12, 0, 0), "Superflex PPR"
        )
        # Prose assertions run against tag-stripped, whitespace-collapsed
        # text. The page wraps lines and puts <span> inside sentences, so
        # asserting on raw HTML would fail on formatting rather than on
        # meaning -- and would quietly invite someone to delete a
        # disclosure to make a test pass.
        cls.prose = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", cls.html))

    @classmethod
    def tearDownClass(cls):
        if cls._saved is not None:
            sys.modules["dynasty.report"] = cls._saved
        else:
            sys.modules.pop("dynasty.report", None)

    def test_marks_itself_active_in_the_nav(self):
        self.assertIn('data-active="managerscore"', self.html)

    def test_has_the_elements_the_js_drives(self):
        for element_id in (
            "ms-status", "ms-basis", "ms-artifact-note", "ms-summary",
            "ms-table", "ms-audit", "ms-results", "ms-drafts",
            "ms-league-list", "ms-league-label", "ms-username",
            "ms-leagueid", "ms-load-user", "ms-load-league", "ms-history",
        ):
            self.assertIn('id="%s"' % element_id, self.html, element_id)

    def test_embeds_both_js_halves(self):
        self.assertIn("msScoreLeague", self.html)
        self.assertIn("msRun", self.html)

    def test_documents_the_formula_on_the_page(self):
        """Requirement: a user must be able to tell what is measured without
        reading the source."""
        for phrase in (
            "Manager Score = 100",
            "0.50", "0.35", "0.15",
            "slot", "surplus",
            "captured by what you received",
            "captured by what you gave",
            "per-transaction mean",
            "peak value after the transaction",
        ):
            self.assertIn(phrase, self.prose, phrase)

    def test_states_the_limitations_on_the_page_not_just_in_comments(self):
        for phrase in (
            "cannot tell you",
            "Hindsight is baked in",
            "crowd, not an oracle",
            "Future picks are priced at",
            "Roster management is not measured",
            "Sleeper only",
            # the archive's own limits must be stated, not buried
            "real but sparse",
            "never after",
            "highest value we",
            "not scored at",
        ):
            self.assertIn(phrase, self.prose, phrase)

    def test_explains_volume_cannot_buy_a_score(self):
        self.assertIn("volume cannot buy", self.prose.lower())
        self.assertIn("n / (n + k)", self.prose)

    def test_says_league_reads_stay_in_the_browser(self):
        self.assertIn("in your browser", self.prose)

    def test_ships_the_basis_wording_the_banner_will_render(self):
        """The banner is produced by the JS at runtime, so the page must at
        least ship the wording that discloses the basis and its limits."""
        self.assertIn("Point-in-time value capture", self.html)
        self.assertIn("No dated value history available", self.html)
        self.assertIn("too recent to judge", self.html)

    def test_explains_why_not_the_two_rejected_bases(self):
        """The owner asked for the rejected alternatives to be justified where
        a reader can see them, not only in a pull request."""
        self.assertIn("decays as he ages", self.html)
        self.assertIn("already peaked", self.html)

    def test_states_that_nothing_is_invented(self):
        self.assertIn("interpolated, modelled or", self.prose)

    def test_no_api_key_or_secret_is_embedded(self):
        lowered = self.html.lower()
        for forbidden in ("api_key", "apikey", "secret", "authorization", "bearer "):
            self.assertNotIn(forbidden, lowered, forbidden)


class SeriesTests(unittest.TestCase):
    """The aligned series is what makes point-in-time pricing possible."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.hist = self.tmp / "history"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _point(self, day, sf, picks=None):
        ktc_history.save_history_point(
            _snapshot(
                [_player(int(k), "P" + k, "WR", v) for k, v in sf.items()]
                + [
                    {"ktc_id": 900, "name": n, "position": "RDP", "team": None,
                     "age": None, "birthday": None, "rookie": True, "mfl_id": None,
                     "superflex": {"value": v}, "one_qb": {"value": v}}
                    for n, v in (picks or {}).items()
                ],
                day + "T10:00:00+00:00",
            ),
            history_dir=self.hist,
        )

    def test_aligns_values_to_a_shared_date_axis(self):
        self._point("2025-01-01", {"1": 1000, "2": 9000})
        self._point("2026-01-01", {"1": 5000, "2": 7000})
        s = ktc_history.build_series(self.hist)
        self.assertEqual(s["dates"], ["2025-01-01", "2026-01-01"])
        self.assertEqual(s["sf"]["1"], [1000, 5000])
        self.assertEqual(s["sf"]["2"], [9000, 7000])

    def test_missing_days_are_null_not_zero(self):
        """A player off the board is unknown, not worthless. Zero would be a
        fabricated price."""
        self._point("2025-01-01", {"1": 1000})
        self._point("2026-01-01", {"1": 5000, "2": 4000})
        s = ktc_history.build_series(self.hist)
        self.assertEqual(s["sf"]["2"], [None, 4000])
        self.assertNotIn(0, s["sf"]["2"])

    def test_rows_are_padded_to_full_length(self):
        self._point("2024-01-01", {"1": 10})
        self._point("2025-01-01", {"1": 20})
        self._point("2026-01-01", {"1": 30, "9": 99})
        s = ktc_history.build_series(self.hist)
        for row in s["sf"].values():
            self.assertEqual(len(row), len(s["dates"]))

    def test_picks_get_their_own_table(self):
        self._point("2025-01-01", {"1": 10}, picks={"2027 Mid 1st": 5000})
        s = ktc_history.build_series(self.hist)
        self.assertIn("2027 Mid 1st", s["picks"])
        self.assertNotIn("2027 Mid 1st", s["sf"])

    def test_empty_history_yields_empty_series(self):
        s = ktc_history.build_series(self.hist)
        self.assertEqual(s["dates"], [])
        self.assertEqual(s["sf"], {})


class RealArchiveTests(unittest.TestCase):
    """Assertions about the committed archive itself.

    These are what stop the archive silently regressing to nothing, which
    is exactly what happened to the dated snapshots the daily job used to
    write and throw away.
    """

    HISTORY = REPO_ROOT / "data" / "consensus" / "history"

    def setUp(self):
        if not self.HISTORY.is_dir():
            self.skipTest("no committed history directory")

    def test_archive_has_real_depth(self):
        dates = ktc_history.available_history_dates(self.HISTORY)
        self.assertGreaterEqual(
            len(dates), 20,
            "the recovered KTC archive should hold a couple of dozen dated boards",
        )

    def test_archive_spans_multiple_years(self):
        dates = ktc_history.available_history_dates(self.HISTORY)
        years = {d[:4] for d in dates}
        self.assertGreaterEqual(len(years), 4, sorted(years))

    def test_every_record_is_dated_and_populated(self):
        for day in ktc_history.available_history_dates(self.HISTORY):
            point = ktc_history.load_history_point(day, self.HISTORY)
            self.assertIsNotNone(point, day)
            self.assertEqual(point["date"], day)
            self.assertTrue(point["sf"], f"{day} has no player values")

    def test_floors_are_positive(self):
        """A zero floor would mean a real asset priced at nothing."""
        for day in ktc_history.available_history_dates(self.HISTORY):
            point = ktc_history.load_history_point(day, self.HISTORY)
            if point.get("floor") is not None:
                self.assertGreater(point["floor"], 0, day)

    def test_records_carry_their_provenance(self):
        """Backfilled days must say where they came from, so nobody has to
        guess whether a number was published or invented."""
        sourced = 0
        for day in ktc_history.available_history_dates(self.HISTORY):
            point = ktc_history.load_history_point(day, self.HISTORY)
            if point.get("source"):
                self.assertIn("web.archive.org", point["source"], day)
                sourced += 1
        self.assertGreater(sourced, 0, "expected backfilled records to be sourced")

    def test_series_builds_from_the_real_archive(self):
        s = ktc_history.build_series(self.HISTORY)
        self.assertGreater(len(s["dates"]), 20)
        self.assertGreater(len(s["sf"]), 400)
        # Josh Allen (ktc_id 365) should be present and always highly valued.
        row = [v for v in (s["sf"].get("365") or []) if v is not None]
        self.assertTrue(row, "ktc_id 365 missing from the archive")
        self.assertGreater(min(row), 5000, "id 365 should be an elite value throughout")


class BackfillScriptTests(unittest.TestCase):
    """The archive tool must stay reproducible and stay out of CI."""

    SRC = REPO_ROOT / "scripts" / "backfill_ktc_history.py"

    def setUp(self):
        if not self.SRC.exists():
            self.skipTest("backfill script missing")
        self.source = self.SRC.read_text(encoding="utf-8")

    def test_documents_the_measured_yield(self):
        self.assertIn("26", self.source)
        self.assertIn("66", self.source)

    def test_states_it_is_not_for_ci(self):
        self.assertIn("NOT part of CI", self.source)

    def test_is_not_referenced_by_the_workflow(self):
        wf = (REPO_ROOT / ".github" / "workflows" / "daily-refresh.yml").read_text()
        self.assertNotIn("backfill_ktc_history", wf)

    def test_parses_a_capture_into_a_record(self):
        """Pure-function check on the archive parser, no network."""
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import backfill_ktc_history as bf

        html = (
            "<html><script>var playersArray = ["
            '{"playerID":365,"playerName":"Josh Allen","position":"QB",'
            '"superflexValues":{"value":9000}},'
            '{"playerID":900,"playerName":"2027 Mid 1st","position":"RDP",'
            '"superflexValues":{"value":5000}},'
            '{"playerID":401,"playerName":"Zero Guy","position":"WR",'
            '"superflexValues":{"value":0}}'
            "];</script></html>"
        )
        rec = bf.capture_to_record(html, "20250714192711")
        self.assertEqual(rec["date"], "2025-07-14")
        self.assertEqual(rec["sf"]["365"], 9000)
        self.assertEqual(rec["picks"]["2027 Mid 1st"], 5000)
        self.assertNotIn("900", rec["sf"], "picks must not land in the player table")
        self.assertEqual(rec["floor"], 9000,
                         "zero-valued rows must not become the floor")
        self.assertIn("web.archive.org", rec["source"])

    def test_returns_none_when_there_is_no_payload(self):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import backfill_ktc_history as bf

        self.assertIsNone(bf.capture_to_record("<html>nothing</html>", "20250714192711"))
        self.assertIsNone(bf.capture_to_record(
            "<script>var playersArray = [not json];</script>", "20250714192711"))


class ReportWiringTests(unittest.TestCase):
    """``report.py`` needs httpx to import, so assert on its source text.

    A text assertion is weak, but it does catch the failure that matters:
    somebody removing the nav entry or the generate_site call.
    """

    @classmethod
    def setUpClass(cls):
        cls.source = (SRC / "dynasty" / "report.py").read_text(encoding="utf-8")

    def test_nav_links_to_the_page(self):
        self.assertIn('link("managerscore.html", "Manager Score", "managerscore")',
                      self.source)

    def test_generate_site_builds_page_and_artifact(self):
        self.assertIn("from .managerscore import build_manager_score", self.source)
        self.assertIn("write_values_artifact(out_root)", self.source)
        self.assertIn('"managerscore.html"', self.source)

    def test_page_build_failure_cannot_break_the_site_build(self):
        idx = self.source.find("manager-score page build failed")
        self.assertGreater(idx, 0, "failure must be logged, not raised")
        self.assertIn("except Exception", self.source[max(0, idx - 600):idx])

    def test_entry_is_in_the_primary_nav_not_the_secondary_row(self):
        """The requirement was specifically the PRIMARY nav.

        ``_site_header`` builds two rows: a primary ``<nav>`` and a
        ``<nav class="secondary">`` fed by the ``secondary`` tuple
        (Methodology / Sources / Prospects). Manager Score must be in the
        first and absent from the second.
        """
        primary = self.source[
            self.source.find("<nav>"):self.source.find("</nav>")
        ]
        self.assertIn("managerscore.html", primary,
                      "Manager Score must be in the primary nav row")

        # The secondary group is the tuple passed to the `secondary` join.
        start = self.source.find("secondary = ")
        end = self.source.find("return f\"\"\"<header", start)
        secondary = self.source[start:end]
        self.assertGreater(len(secondary), 0, "secondary nav group not found")
        self.assertNotIn("managerscore.html", secondary,
                         "Manager Score must not be demoted to the secondary row")
        for demoted in ("methodology.html", "sources.html", "prospects.html"):
            self.assertIn(demoted, secondary,
                          "secondary group shape changed; re-check placement")


class RefreshScriptWiringTests(unittest.TestCase):
    def test_refresh_saves_a_history_point(self):
        source = (REPO_ROOT / "scripts" / "refresh_ktc_consensus.py").read_text()
        self.assertIn("ktc_history.save_history_point", source)
        self.assertIn("snapshot_to_dict", source)

    def test_history_dir_is_not_gitignored(self):
        """The dated full snapshots ARE ignored. The compact history must not
        be, or the accumulation silently does nothing."""
        ignore = (REPO_ROOT / ".gitignore").read_text()
        self.assertIn("data/consensus/ktc_[0-9]*-[0-9]*-[0-9]*.json", ignore)
        for line in ignore.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            self.assertNotIn("consensus/history", stripped)

    def test_workflow_retains_history(self):
        wf = (REPO_ROOT / ".github" / "workflows" / "daily-refresh.yml").read_text()
        self.assertIn("Retain KTC value history", wf)
        self.assertIn("contents: write", wf)
        self.assertIn("[skip ci]", wf, "a self-triggering commit loop must be avoided")
        self.assertIn("continue-on-error: true", wf)


# ---------------------------------------------------------------------------
# JavaScript suites (node)
# ---------------------------------------------------------------------------

class JavaScriptTests(unittest.TestCase):
    """Drive the node suites that hold the scoring verification."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.core = cls.tmp / "ms_core.js"
        cls.page = cls.tmp / "ms_page.js"
        cls.core.write_text(MANAGERSCORE_CORE_JS, encoding="utf-8")
        cls.page.write_text(
            MANAGERSCORE_CORE_JS + MANAGERSCORE_UI_JS, encoding="utf-8"
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _node(self, *args):
        if not NODE:
            self.skipTest("node not available; page JS cannot be verified here")
        proc = subprocess.run(
            [NODE, *[str(a) for a in args]],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=120,
        )
        if proc.returncode != 0:
            self.fail(
                "node exited %d\n--- stdout ---\n%s\n--- stderr ---\n%s"
                % (proc.returncode, proc.stdout, proc.stderr)
            )
        return proc.stdout

    def test_core_js_parses(self):
        self._node("--check", self.core)

    def test_page_js_parses(self):
        self._node("--check", self.page)

    def test_core_test_file_parses(self):
        self._node("--check", REPO_ROOT / "tests" / "js" / "managerscore_core_tests.js")

    def test_dom_test_file_parses(self):
        self._node("--check", REPO_ROOT / "tests" / "js" / "managerscore_dom_tests.js")

    def test_scoring_core_assertions(self):
        out = self._node(
            REPO_ROOT / "tests" / "js" / "managerscore_core_tests.js", self.core
        )
        self.assertIn("checks passed", out)
        print("\n  " + out.strip())

    def test_dom_and_sleeper_shape_assertions(self):
        out = self._node(
            REPO_ROOT / "tests" / "js" / "managerscore_dom_tests.js", self.page
        )
        self.assertIn("checks passed", out)
        print("\n  " + out.strip())

    def test_no_league_id_is_hardcoded(self):
        """The page must work for ANY Sleeper league the user types in.

        A specific league was used to verify this feature against real data,
        which is exactly the circumstance in which a test fixture gets left
        behind in shipping code. Sleeper league ids are 18-19 digit
        snowflakes, so any long digit run in the page source is one.
        """
        for label, src in (
            ("core JS", MANAGERSCORE_CORE_JS),
            ("UI JS", MANAGERSCORE_UI_JS),
            ("page module", (REPO_ROOT / "src" / "dynasty" / "managerscore.py").read_text()),
        ):
            found = re.findall(r"\b\d{15,20}\b", src)
            self.assertEqual(
                found, [],
                "%s contains what looks like a hardcoded Sleeper id: %s"
                % (label, found),
            )

    def test_league_id_flows_from_the_caller(self):
        """The league id must reach every Sleeper URL from the argument, not
        from module state."""
        self.assertIn("function msRun(leagueId", MANAGERSCORE_UI_JS)
        self.assertIn("function msFetchLeagueData(leagueId", MANAGERSCORE_UI_JS)
        self.assertIn("function msFetchMatchups(leagueId", MANAGERSCORE_UI_JS)
        # The chain is discovered, never enumerated.
        self.assertIn("previous_league_id", MANAGERSCORE_UI_JS)

    def test_reads_are_cached_and_rate_limited(self):
        """Sleeper is public, unauthenticated and free; the page must not
        hammer it. These are the mechanisms, pinned so they cannot quietly
        be removed."""
        self.assertIn("msGetCached", MANAGERSCORE_UI_JS)
        self.assertIn("msMapLimit", MANAGERSCORE_UI_JS)
        self.assertIn("MS_MAX_CONCURRENCY", MANAGERSCORE_UI_JS)
        self.assertIn("sessionStorage", MANAGERSCORE_UI_JS)
        # Matchups and transactions are the two big fan-outs and must both
        # go through the cache rather than raw fetch.
        for fn in ("msFetchMatchups", "msFetchTransactions"):
            after = MANAGERSCORE_UI_JS.split("function %s(" % fn, 1)[1]
            # Up to the next top-level function, so the whole body is read
            # rather than an arbitrary prefix of it.
            body = after.split("\nfunction ", 1)[0]
            self.assertIn("msGetCached", body,
                          "%s must fetch through the cache" % fn)
            self.assertNotIn(
                "msGetJSONSoft(", body,
                "%s must not bypass the cache with a raw fetch" % fn)

    def test_core_is_free_of_dom_and_network(self):
        """The core must stay pure, or the node suite stops being able to
        test it and the maths drifts back into the page."""
        for forbidden in ("document", "fetch(", "window", "localStorage"):
            self.assertNotIn(
                forbidden, MANAGERSCORE_CORE_JS,
                "scoring core must not reference %s" % forbidden,
            )


class ProjectionArchiveTests(unittest.TestCase):
    """The forward-only projection archive.

    The reason this script exists is that Sleeper's served projection
    history is rewritten after the fact and therefore cannot be used as a
    point-in-time expectation. The tests that matter are the ones proving
    this archive does not repeat that mistake with its own files.
    """

    def setUp(self):
        import importlib.util

        path = REPO_ROOT / "scripts" / "archive_sleeper_projections.py"
        spec = importlib.util.spec_from_file_location("_archive_proj", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.mod = mod

    @staticmethod
    def _row(pid, name, pos, pts, date="2026-10-04", updated=123):
        return {
            "player_id": pid,
            "player": {"first_name": name.split()[0], "last_name": name.split()[-1],
                       "position": pos},
            "team": "DET", "opponent": "GB", "date": date,
            "updated_at": updated,
            "stats": {"pts_ppr": pts, "pts_half_ppr": pts - 1, "pts_std": pts - 2},
        }

    def test_endpoint_carries_required_season_type(self):
        """Without season_type the live endpoint answers 400. Verified against
        the API; pinned so a future edit cannot quietly drop it."""
        self.assertIn("season_type=regular", self.mod.SLEEPER_PROJ)

    def test_distill_keeps_every_published_scoring_variant(self):
        """None of Sleeper's variants is a given league's own scoring, so the
        choice belongs to the consumer and all three must survive."""
        out = self.mod.distill([self._row("1", "Jahmyr Gibbs", "RB", 20.0)])
        self.assertEqual(len(out), 1)
        self.assertEqual(set(out[0]["stats"]), {"pts_ppr", "pts_half_ppr", "pts_std"})
        self.assertEqual(out[0]["name"], "Jahmyr Gibbs")

    def test_distill_retains_sleeper_updated_at(self):
        """This is the field that disqualified the historical archive, so a
        reader must be able to audit it per row."""
        out = self.mod.distill([self._row("1", "A B", "RB", 9.0, updated=999)])
        self.assertEqual(out[0]["sleeper_updated_at"], 999)

    def test_distill_drops_rows_with_no_projected_points(self):
        rows = [self._row("1", "A B", "RB", 10.0)]
        rows.append({"player_id": "2", "player": {}, "stats": {}})
        self.assertEqual(len(self.mod.distill(rows)), 1)

    def test_snapshot_before_kickoff_is_flagged_valid(self):
        rows = self.mod.distill([self._row("1", "A B", "RB", 10.0, date="2099-01-01")])
        with tempfile.TemporaryDirectory() as td:
            self.mod.OUT_DIR = Path(td)
            path = self.mod.write_snapshot("2026", 5, rows, dry_run=False)
            payload = json.loads(path.read_text())
        self.assertTrue(payload["pre_kickoff"])
        self.assertEqual(payload["first_game_date"], "2099-01-01")

    def test_snapshot_after_kickoff_is_flagged_invalid(self):
        """The whole point. A capture that postdates its own games is exactly
        the post-hoc 'forecast' this project refuses to present, so it is
        recorded as such rather than filed as a clean expectation."""
        rows = self.mod.distill([self._row("1", "A B", "RB", 10.0, date="2000-01-01")])
        with tempfile.TemporaryDirectory() as td:
            self.mod.OUT_DIR = Path(td)
            path = self.mod.write_snapshot("2026", 5, rows, dry_run=False)
            payload = json.loads(path.read_text())
        self.assertFalse(payload["pre_kickoff"])

    def test_snapshot_carries_its_caveat_in_the_artifact(self):
        """The caveat must travel with the data, not live only in a docstring
        that a downstream consumer will never read."""
        rows = self.mod.distill([self._row("1", "A B", "RB", 10.0)])
        with tempfile.TemporaryDirectory() as td:
            self.mod.OUT_DIR = Path(td)
            path = self.mod.write_snapshot("2026", 5, rows, dry_run=False)
            payload = json.loads(path.read_text())
        self.assertEqual(payload["schema"], "sleeper.projections.v1")
        self.assertIn("captured_at", payload)
        self.assertIn("rewrites projection rows", payload["note"])

    def test_dry_run_writes_nothing(self):
        rows = self.mod.distill([self._row("1", "A B", "RB", 10.0)])
        with tempfile.TemporaryDirectory() as td:
            self.mod.OUT_DIR = Path(td)
            self.assertIsNone(self.mod.write_snapshot("2026", 5, rows, dry_run=True))
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_committed_snapshot_is_a_valid_forecast(self):
        """Any snapshot committed to the repo must predate its own games, or
        the archive is repeating the flaw it was built to avoid."""
        hist = REPO_ROOT / "data" / "projections" / "history"
        if not hist.exists():
            self.skipTest("no projection archive committed yet")
        files = sorted(hist.glob("projections_*.json"))
        self.assertTrue(files, "archive directory exists but is empty")
        for f in files:
            payload = json.loads(f.read_text())
            self.assertEqual(payload["schema"], "sleeper.projections.v1", f.name)
            self.assertTrue(payload["n_players"] > 0, f.name)
            self.assertTrue(
                payload["pre_kickoff"],
                "%s postdates its own kickoff and must not be committed" % f.name,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
