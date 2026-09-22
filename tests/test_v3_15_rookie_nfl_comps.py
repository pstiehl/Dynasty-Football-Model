"""Rookies are compared to NFL players on NFL production, not college.

Phil, 2026-09-22, verbatim:

    "the model is still comparing them to college players. It should be
    comparing them to nfl players using their nfl stats to this point.
    the model should be taking all of their starts so far from the 2026
    season and comparing them to similar historical nfl players who put
    up similar stats through their first 2 games."

Two PRs in a row shipped on tests that asserted against stubs, so the
assertions here run against the **committed artifacts** -- the real
weekly corpus and the real first-N cohort -- and against HTML produced
by the real page builders.

Written so they do not rot in seven days
----------------------------------------

N grows every week. A test pinned to "Denzel Boston has 2 games" fails
the moment week 3 is published, and a test pinned to "his top comp is
Ja'Marr Chase" fails whenever the ranking legitimately changes. Neither
failure would mean anything is broken.

So the fixed-number assertions are on quantities that are permanent:
a player's totals **through his first two games** never change once
those games are played, whatever N becomes later. Everything else is
asserted as an invariant -- the cohort is the same position and the same
N as the subject, the ranking is ordered, the no-games state is not
backfilled with college comps.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from dynasty.sources import nflverse_weekly_stats as weekly      # noqa: E402
from dynasty.engine import rookie_nfl_debut_similarity as sim    # noqa: E402
from dynasty import rookie_rankings as rr                        # noqa: E402


#: Phil verified these against the nflverse ``stats_player`` release on
#: 2026-09-22: weeks 1 and 2, receptions / yards / TDs.
#:
#: Asserted as first-TWO-game totals rather than career-to-date, because
#: that is a fact that stays true in week 12.
FIRST_TWO_GAMES = {
    "Denzel Boston":  {"receptions": 7,  "receiving_yards": 154, "receiving_tds": 2},
    "Caleb Douglas":  {"receptions": 7,  "receiving_yards": 113, "receiving_tds": 0},
    "Carnell Tate":   {"receptions": 7,  "receiving_yards": 65,  "receiving_tds": 0},
    "KC Concepcion":  {"receptions": 10, "receiving_yards": 71,  "receiving_tds": 0},
}


class TestWeeklyCorpusArtifacts(unittest.TestCase):
    """The committed artifacts exist and have the shape everything reads."""

    def test_current_week_artifact_is_populated(self):
        season = weekly.current_season()
        weeks = weekly.current_weeks()
        self.assertIsNotNone(
            season, "player_week_current.csv.gz has no season - "
                    "run scripts/refresh_nflverse_weekly.py")
        self.assertTrue(weeks, "current-season artifact has no weeks")
        self.assertEqual(weeks, sorted(set(weeks)), "weeks must be unique/sorted")
        self.assertEqual(min(weeks), 1, "week 1 should be the first week")

    def test_first_n_cohort_covers_every_skill_position(self):
        for pos in ("QB", "RB", "WR", "TE"):
            with self.subTest(position=pos):
                pool = weekly.cohort(pos, 2)
                self.assertGreaterEqual(
                    len(pool), sim.MIN_COHORT,
                    f"{pos} cohort at N=2 is too small to rank against")

    def test_cohort_rows_all_carry_the_requested_n(self):
        """A cohort of N must contain only N-game lines.

        This is the load-bearing property of the whole feature: if a
        cohort leaked lines of a different length, the comparison would
        be between a rookie's 2 games and somebody's 16, which is the
        bug the feature exists to avoid.
        """
        for pos in ("WR", "RB", "QB", "TE"):
            for n in (1, 2, 3):
                with self.subTest(position=pos, n=n):
                    for row in weekly.cohort(pos, n):
                        self.assertEqual(row["n_games"], n)
                        self.assertEqual(row["position"], pos)

    def test_cohort_excludes_the_current_season(self):
        """A 2026 rookie must not be comped against another 2026 rookie.

        Neither has a career yet, so the comparison could not say what
        became of anyone.
        """
        season = weekly.current_season()
        for pos in ("WR", "RB"):
            debuts = {r["debut_season"] for r in weekly.cohort(pos, 2)}
            self.assertNotIn(season, debuts,
                             f"{pos} cohort contains current-season debuts")


class TestNamedRookiesRealProduction(unittest.TestCase):
    """The four players Phil keeps naming, against his verified numbers."""

    def test_all_four_resolve_to_an_nfl_id(self):
        for name in FIRST_TWO_GAMES:
            with self.subTest(name=name):
                self.assertIsNotNone(
                    sim.resolve_player_id(name),
                    f"{name} did not resolve in the current weekly file")

    def test_roster_name_is_kc_not_kevin_concepcion(self):
        """The roster carries "KC Concepcion"; "Kevin" is the college corpus.

        The board must resolve BOTH, because a missed join renders as
        "has not played", which would be a silent falsehood about a
        player who has caught ten passes.
        """
        self.assertIsNotNone(sim.resolve_player_id("KC Concepcion"))
        self.assertIsNotNone(
            sim.resolve_player_id("Kevin Concepcion",
                                  aliases=("KC Concepcion",)),
            "the corpus name must resolve through the alias path")

    def test_first_two_games_match_the_verified_lines(self):
        for name, expected in FIRST_TWO_GAMES.items():
            with self.subTest(name=name):
                pid = sim.resolve_player_id(name)
                line = weekly.career_to_date(pid)
                self.assertIsNotNone(line, f"{name} has no game logs")
                games = line["games"]
                self.assertGreaterEqual(
                    len(games), 2, f"{name} has fewer than two games")
                for stat, want in expected.items():
                    got = sum(g[stat] for g in games[:2])
                    self.assertEqual(
                        got, want,
                        f"{name} first-2-game {stat}: got {got}, want {want}")


class TestNIsDerivedNotHardcoded(unittest.TestCase):
    """N comes from games played and follows the season."""

    def test_career_to_date_n_equals_number_of_game_logs(self):
        pid = sim.resolve_player_id("Denzel Boston")
        line = weekly.career_to_date(pid)
        self.assertEqual(line["n_games"], len(line["games"]))
        self.assertEqual(line["n_games"], len(line["weeks"]))

    def test_comps_use_a_cohort_of_the_subjects_own_length(self):
        """Ask for N=3 and every comp must be a 3-game line.

        Built from a synthetic subject rather than a real rookie so the
        assertion holds in any week of any season.
        """
        subject = {
            "player_id": "synthetic",
            "n_games": 3,
            "targets": 15, "receptions": 10, "receiving_yards": 140,
            "receiving_tds": 1,
        }
        result = sim.comps_for_line(subject, "WR", k=5)
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["n_games"], 3)
        self.assertTrue(result["comps"])
        for c in result["comps"]:
            self.assertEqual(c["line"]["n_games"], 3)
            self.assertEqual(c["position"], "WR")

    def test_a_longer_subject_draws_a_different_cohort(self):
        """N=2 and N=6 must not return the same cohort size by accident."""
        base = {"player_id": "synthetic", "targets": 12, "receptions": 8,
                "receiving_yards": 100, "receiving_tds": 1}
        two = sim.comps_for_line(dict(base, n_games=2), "WR", k=3)
        six = sim.comps_for_line(dict(base, n_games=6), "WR", k=3)
        self.assertNotEqual(two["cohort_size"], six["cohort_size"])
        self.assertGreater(two["cohort_size"], six["cohort_size"],
                           "fewer players survive to 6 games than to 2")


class TestSimilarityCorrectness(unittest.TestCase):
    """The ranking is actually a ranking."""

    def test_comps_are_ordered_by_descending_similarity(self):
        pid = sim.resolve_player_id("Denzel Boston")
        comps = sim.comps_for_player(pid, "WR", k=10)["comps"]
        sims = [c["similarity"] for c in comps]
        self.assertEqual(sims, sorted(sims, reverse=True))
        dists = [c["distance"] for c in comps]
        self.assertEqual(dists, sorted(dists))

    def test_an_exact_copy_of_a_cohort_member_ranks_that_member_first(self):
        """Feed a cohort member's own line back in; he must come top.

        This is the test that would catch a broken feature extractor or
        a mis-scaled distance, neither of which a smoke test notices.
        """
        pool = weekly.cohort("WR", 2)
        target = max(pool, key=lambda r: r["receiving_yards"])
        probe = dict(target)
        probe["player_id"] = "probe-not-in-cohort"
        result = sim.comps_for_line(probe, "WR", k=3, with_outcomes=False)
        self.assertEqual(result["comps"][0]["player_id"], target["player_id"])
        self.assertAlmostEqual(result["comps"][0]["distance"], 0.0, places=6)

    def test_subject_is_never_his_own_comp(self):
        pool = weekly.cohort("WR", 2)
        target = pool[0]
        result = sim.comps_for_line(dict(target), "WR", k=5,
                                    with_outcomes=False)
        ids = [c["player_id"] for c in result["comps"]]
        self.assertNotIn(target["player_id"], ids)

    def test_every_comp_carries_its_own_first_n_line(self):
        """The reader must be able to see WHY a comp is a comp."""
        pid = sim.resolve_player_id("Denzel Boston")
        result = sim.comps_for_player(pid, "WR", k=10)
        for c in result["comps"]:
            self.assertIn("line", c)
            self.assertEqual(c["line"]["n_games"], result["n_games"])
            self.assertIn("receiving_yards", c["line"]["totals"])
            # ...and what became of him.
            self.assertIn("career", c)
            self.assertIn("outcome", c["career"])


class TestCareerOutcomeLabels(unittest.TestCase):
    """Pure-logic guards on the outcome bands."""

    def test_active_players_are_never_labelled_bust(self):
        self.assertEqual(
            sim.outcome_label(10.0, still_active=True), "active")
        self.assertEqual(
            sim.outcome_label(None, still_active=True), "active")

    def test_finished_careers_band_by_points(self):
        self.assertEqual(sim.outcome_label(2000.0, still_active=False), "elite")
        self.assertEqual(sim.outcome_label(800.0, still_active=False), "starter")
        self.assertEqual(sim.outcome_label(200.0, still_active=False), "depth")
        self.assertEqual(sim.outcome_label(10.0, still_active=False), "bust")
        self.assertEqual(sim.outcome_label(None, still_active=False), "unknown")


class TestDebutGuard(unittest.TestCase):
    """A player already in the league in 1999 is not a rookie."""

    def test_pre_coverage_veterans_are_excluded(self):
        import refresh_nflverse_weekly as rnw
        # Troy Aikman's first weekly row is 1999; he debuted in 1989.
        self.assertFalse(rnw.debut_is_trustworthy(1999, 1989))
        # A genuine 1999 rookie is fine.
        self.assertTrue(rnw.debut_is_trustworthy(1999, 1999))
        # Unknown rookie season at the edge of coverage: not trusted.
        self.assertFalse(rnw.debut_is_trustworthy(1999, None))
        # Anything after coverage began is unambiguous either way.
        self.assertTrue(rnw.debut_is_trustworthy(2005, None))
        self.assertTrue(rnw.debut_is_trustworthy(2005, 2004))

    def test_no_cohort_member_predates_coverage(self):
        for pos in ("WR", "RB", "QB", "TE"):
            for row in weekly.cohort(pos, 2):
                self.assertGreaterEqual(row["debut_season"], 1999)


class TestNoCollegeFallback(unittest.TestCase):
    """A rookie with no snaps gets an empty state, not college comps."""

    def test_unknown_player_yields_no_nfl_games(self):
        result = sim.comps_for_player("00-0000000-nope", "WR")
        self.assertEqual(result["state"], "no_nfl_games")
        self.assertEqual(result["comps"], [])
        self.assertEqual(result["n_games"], 0)

    def test_zero_game_line_is_not_ranked(self):
        result = sim.comps_for_line({"n_games": 0}, "WR")
        self.assertEqual(result["state"], "no_nfl_games")
        self.assertEqual(result["comps"], [])


class TestAttachToRookieBoard(unittest.TestCase):
    """The board rows carry the NFL comparison."""

    @classmethod
    def setUpClass(cls):
        from dynasty.report import _load_prospects_artifact
        cls.artifact = _load_prospects_artifact()
        if not cls.artifact:
            raise unittest.SkipTest(
                "prospects artifact absent - run scripts/build_prospects_v3.py")
        cls.rows = rr.rookie_rows(cls.artifact)
        cls.summary = rr.attach_nfl_comps(cls.rows)

    def test_named_rookies_have_nfl_games_and_comps(self):
        by_name = {r["name"]: r for r in self.rows}
        for name in FIRST_TWO_GAMES:
            with self.subTest(name=name):
                row = by_name.get(name)
                self.assertIsNotNone(row, f"{name} missing from the board")
                self.assertGreaterEqual(
                    row["nfl_games"], 2,
                    f"{name} shows no NFL games - the name join failed")
                self.assertEqual(row["nfl_comp_state"], "ok")
                self.assertTrue(row["nfl_comps"])
                for c in row["nfl_comps"]:
                    self.assertEqual(c["line"]["n_games"], row["nfl_games"])

    def test_rows_without_snaps_are_marked_not_backfilled(self):
        unplayed = [r for r in self.rows if not r.get("nfl_games")]
        self.assertTrue(unplayed, "expected some undebuted rookies")
        for r in unplayed:
            self.assertEqual(r["nfl_comp_state"], "no_nfl_games")
            self.assertEqual(r["nfl_comps"], [])
            self.assertIsNone(r["nfl_line"])

    def test_coverage_counts_add_up(self):
        cov = rr.coverage(self.artifact, self.rows)
        self.assertEqual(
            cov["n_with_nfl_games"] + cov["n_not_yet_played"],
            cov["n_shown"])
        self.assertGreater(cov["n_with_nfl_games"], 0)
        self.assertTrue(cov["nfl_sample_label"])

    def test_artifact_schema_is_bumped_and_carries_nfl_fields(self):
        payload = rr.artifact_payload(self.artifact, self.rows)
        self.assertEqual(payload["schema"], "dynasty.rookie_rankings.v2")
        self.assertEqual(payload["nfl_comp_engine"], rr.ROOKIE_NFL_ENGINE)
        self.assertIn("primary_comparison", payload)
        self.assertIn("sample_warning", payload)
        played = [r for r in payload["rookies"] if r.get("nfl_games")]
        self.assertTrue(played)

    def test_cohort_sizes_are_keyed_by_position_and_n(self):
        """Keying on N alone let one position overwrite another."""
        for key, size in self.summary["cohort_sizes"].items():
            self.assertIn("@", key)
            pos, n = key.split("@")
            self.assertEqual(size, weekly.cohort_size(pos, int(n)))


class TestRenderedPages(unittest.TestCase):
    """The HTML the builders actually emit."""

    @classmethod
    def setUpClass(cls):
        from dynasty import report
        from dynasty.report import _load_prospects_artifact
        cls.report = report
        artifact = _load_prospects_artifact()
        if not artifact:
            raise unittest.SkipTest("prospects artifact absent")
        cls.artifact = artifact
        cls.rows = rr.rookie_rows(artifact)
        rr.attach_nfl_comps(cls.rows)
        cls.cov = rr.coverage(artifact, cls.rows)
        cls.section = report._rookie_section(cls.rows, cls.cov)

    def test_rookie_section_leads_with_nfl_production(self):
        html = self.section
        self.assertIn("2026 NFL production", html)
        self.assertIn("Closest NFL comp through same games", html)
        # The college number is still present, and demoted in the header.
        self.assertIn("College proj fp", html)

    def test_rookie_section_states_the_sample_size(self):
        html = self.section
        self.assertIn("sample-banner", html)
        self.assertIn("very small sample", html)
        self.assertIn("not a projection", html)

    def test_rookie_section_shows_unplayed_players_honestly(self):
        self.assertIn("has not played", self.section)

    def test_named_rookies_render_a_stat_line_and_a_comp(self):
        # Compared against the UNESCAPED page text: the renderer escapes
        # apostrophes, so a raw "Ja'Marr Chase" never appears in the
        # markup even though the name is rendered correctly.
        import html as _html
        text = _html.unescape(self.section)
        for name in FIRST_TWO_GAMES:
            with self.subTest(name=name):
                row = next(r for r in self.rows if r["name"] == name)
                self.assertIn(name, text)
                top = row["nfl_comps"][0]["name"]
                self.assertIn(top, text,
                              f"{name}'s top comp {top} not rendered")
                # ...and his own line, not just his name.
                line = self.report._nfl_stat_line(
                    row["nfl_line"], row["position"])
                self.assertIn(line, text,
                              f"{name}'s NFL stat line not rendered")

    def test_prospect_page_puts_nfl_comps_above_college(self):
        row = next(r for r in self.rows if r["name"] == "Denzel Boston")
        prospect = next(
            p for p in self.artifact["prospects"]
            if self.report._prospect_slug(p) == row["slug"])
        import datetime as _dt
        html = self.report._build_prospect_page(
            prospect, "SF PPR", _dt.datetime.now(_dt.timezone.utc),
            rookie_row=row)

        nfl_at = html.find('NFL <span class="accent">comparables')
        college_at = html.find("secondary-panel")
        self.assertGreater(nfl_at, -1, "no NFL comparables section")
        self.assertGreater(college_at, -1, "college panel not collapsed")
        self.assertLess(nfl_at, college_at,
                        "college comps must not precede the NFL comparison")
        # College comps must not be a top-level heading for a player
        # who has NFL snaps.
        self.assertNotIn('<h2>Top-25 college', html)
        self.assertIn("Superseded by the", html)
        # Every comp's own line is on the page.
        self.assertIn("nfl-subject-row", html)
        self.assertGreaterEqual(html.count('class="nfl-line"'), 2)

    def test_prospect_page_for_an_unplayed_rookie_keeps_college_primary(self):
        row = next((r for r in self.rows if not r.get("nfl_games")), None)
        self.assertIsNotNone(row, "expected an undebuted rookie")
        prospect = next(
            p for p in self.artifact["prospects"]
            if self.report._prospect_slug(p) == row["slug"])
        import datetime as _dt
        html = self.report._build_prospect_page(
            prospect, "SF PPR", _dt.datetime.now(_dt.timezone.utc),
            rookie_row=row)
        self.assertIn("Has not played an NFL snap", html)
        # He has no NFL evidence, so the college grid is still the
        # primary view -- but it is never presented as NFL evidence.
        self.assertIn("<h2>Top-25 college", html)
        self.assertNotIn("nfl-subject-row", html)


if __name__ == "__main__":
    unittest.main()
