"""Draft status, projection basis and class grouping for the prospects board.

Covers the three defects Phil reported on the live prospects page
(2026-09-22), at the layer where each was actually caused:

1. ``Arch Manning ? RNone`` -- ``None`` formatted into a draft-round string
   and a literal ``?`` for an unknown team.
2. ``Michael Penix ATL R1``, class **2024**, at rank 1 of a *prospect*
   board -- no class scoping and no statement of which class was shown.
3. The top three rows all reading ``3200`` projected career fp and ``13.5``
   peak3 fp/g -- the ``QB``/``R1_top10`` tier constants, reached because a
   Tankathon big-board RANK was fed to the NFL draft-PICK tier table.

``dynasty.prospect_view`` is pure and stdlib-only precisely so these can be
asserted without the engine, the corpus or numpy.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dynasty import prospect_view as pv  # noqa: E402


def drafted_pick(**over):
    rec = {
        "name": "Carnell Tate", "position": "WR", "draft_class": 2026,
        "drafted": {"year": 2026, "round": 1, "pick": 4, "team": "TEN"},
        "projection": {"projected_career_fp": 1041.0,
                       "projected_peak3_fp_pg": 9.1,
                       "projection_source": "comp_weighted",
                       "n_comps_with_nfl": 14},
    }
    rec.update(over)
    return rec


def big_board(**over):
    """A Tankathon entry exactly as ``_index_tankathon_by_class`` emits it."""
    rec = {
        "name": "Arch Manning", "position": "QB", "draft_class": 2027,
        "drafted": {"year": 2027, "round": None, "pick": 4, "team": None,
                    "source": "tankathon_big_board"},
        "projection": {"projected_career_fp": None,
                       "projected_peak3_fp_pg": None,
                       "projection_source": "insufficient_evidence_undrafted",
                       "n_comps_with_nfl": 0},
    }
    rec.update(over)
    return rec


class TestDraftStatus(unittest.TestCase):
    def test_a_real_pick_reports_team_round_and_pick(self):
        st = pv.draft_status(drafted_pick())
        self.assertEqual(st["kind"], "drafted")
        self.assertEqual((st["team"], st["round"], st["pick"]), ("TEN", "1", "4"))
        self.assertEqual(pv.draft_label(drafted_pick()), "TEN R1 #4")

    def test_a_big_board_entry_is_not_drafted(self):
        """The bug: a big-board entry HAS a ``drafted`` block.

        The old check was ``if drafted:``, which is true here, so the page
        went on to format ``round=None`` and ``team=None`` into text.
        """
        rec = big_board()
        self.assertTrue(rec["drafted"], "precondition: the block is truthy")
        self.assertFalse(pv.is_drafted(rec))
        self.assertEqual(pv.draft_status(rec)["kind"], "big_board")

    def test_no_label_can_ever_contain_None_or_a_question_mark(self):
        """The exact strings Phil saw, asserted as absent.

        Checked across every degenerate shape rather than just the observed
        one, because the failure was a missing-field class of bug and the
        next missing field should not reproduce it.
        """
        shapes = [
            big_board(),
            drafted_pick(drafted={"year": 2026, "round": None, "pick": 4,
                                  "team": None}),
            drafted_pick(drafted={"year": None, "round": None, "pick": None,
                                  "team": None}),
            drafted_pick(drafted={}),
            drafted_pick(drafted=None),
            {"name": "x"},
        ]
        for rec in shapes:
            label = pv.draft_label(rec)
            self.assertNotIn("None", label, f"leak in {rec.get('drafted')!r}")
            self.assertNotIn("?", label, f"leak in {rec.get('drafted')!r}")
            self.assertNotIn("RNone", label)
            for key, value in pv.draft_status(rec).items():
                if key == "kind":
                    continue
                self.assertNotEqual(
                    value, "None",
                    f"{key} stringified a None for {rec.get('drafted')!r}")

    def test_big_board_label_says_what_the_number_is(self):
        self.assertEqual(pv.draft_label(big_board()), "big board #4")


class TestPickTier(unittest.TestCase):
    def test_tiers_match_the_projection_layer(self):
        """Guard against the two tier tables drifting apart.

        ``prospect_view`` reproduces the bounds rather than importing them,
        because ``build_prospects_v3`` is a script that pulls in the
        similarity engine. Reproduction is only safe if it is checked.
        """
        cases = [(1, "R1_top10"), (10, "R1_top10"), (11, "R1"), (32, "R1"),
                 (33, "R2"), (64, "R2"), (65, "R3"), (100, "R3"),
                 (101, "R4"), (150, "R4"), (151, "R5_6"), (200, "R5_6"),
                 (201, "R7"), (257, "R7")]
        for pick, tier in cases:
            self.assertEqual(pv.pick_tier(pick), tier, f"pick {pick}")

    def test_an_undrafted_prospect_has_no_tier(self):
        """The saturation fix, at its root.

        Arch Manning's big-board rank of 4 previously resolved to
        ``R1_top10`` and therefore to the QB tier constant of 3200 -- the
        same number as the actual first overall pick. A rank is not draft
        capital, so there is no tier.
        """
        self.assertIsNone(pv.pick_tier(4, drafted=False))
        self.assertIsNone(pv.pick_tier(None))
        self.assertEqual(pv.pick_tier(4, drafted=True), "R1_top10")

    def test_garbage_picks_do_not_raise(self):
        for bad in ("", "abc", -1, 0, None, [], {}):
            self.assertIsNone(pv.pick_tier(bad))


class TestProjectionBasis(unittest.TestCase):
    def test_a_tier_constant_is_flagged_as_one(self):
        rec = drafted_pick(projection={
            "projected_career_fp": 3200.0, "projected_peak3_fp_pg": 13.5,
            "projection_source": "pick_tier_baseline_R1_top10",
            "n_comps_with_nfl": 0})
        basis = pv.projection_basis(rec)
        self.assertEqual(basis["key"], "baseline")
        self.assertTrue(basis["is_tier_constant"])
        self.assertFalse(basis["evidence_backed"])
        # Still shown: draft capital is real signal. Just never unlabelled.
        self.assertTrue(basis["show_point_estimate"])
        self.assertIn("identical", basis["detail"])

    def test_comp_backed_projection_is_not_flagged(self):
        basis = pv.projection_basis(drafted_pick())
        self.assertEqual(basis["key"], "comps")
        self.assertTrue(basis["evidence_backed"])
        self.assertFalse(basis["is_tier_constant"])

    def test_no_evidence_means_no_number(self):
        basis = pv.projection_basis(big_board())
        self.assertEqual(basis["key"], "none")
        self.assertFalse(basis["show_point_estimate"])

    def test_every_source_the_projection_layer_emits_is_recognised(self):
        """A source this module does not know must not read as comp-backed.

        The default branch is "comps", which is the most flattering
        interpretation, so an unmapped source would silently launder a tier
        constant into an evidence-backed projection.
        """
        sources = [
            "comp_weighted", "pick_tier_baseline_R1_top10",
            "pick_tier_baseline_UDFA", "floor_30pct_pick_tier_baseline_R2",
            "blend_0.42_pick_tier_baseline_R1", "comp_weighted_no_draft_capital",
            "insufficient_evidence_undrafted",
        ]
        for source in sources:
            rec = drafted_pick(projection={
                "projected_career_fp": 1.0, "projection_source": source,
                "n_comps_with_nfl": 1})
            basis = pv.projection_basis(rec)
            self.assertIn(basis["key"],
                          ("comps", "baseline", "floor", "blend",
                           "comps_no_capital", "none"),
                          f"unmapped source {source}")
            if source.startswith(("pick_tier_baseline", "floor_")):
                self.assertFalse(
                    basis["evidence_backed"],
                    f"{source} must not read as evidence-backed")

    def test_missing_provenance_is_not_claimed_as_comp_support(self):
        rec = drafted_pick(projection={"projected_career_fp": 3200.0,
                                       "n_comps_with_nfl": 0})
        basis = pv.projection_basis(rec)
        self.assertEqual(basis["key"], "baseline")
        self.assertFalse(basis["evidence_backed"])


class TestClassGrouping(unittest.TestCase):
    def test_drafted_and_future_classes_are_separated(self):
        board = [drafted_pick(), drafted_pick(draft_class=2025),
                 big_board(), big_board(name="Dante Moore")]
        info = pv.classify_classes(board)
        self.assertEqual(info["drafted"], [2025, 2026])
        self.assertEqual(info["future"], [2027])
        self.assertEqual(info["current"], 2026,
                         "the default board is the newest DRAFTED class")

    def test_a_class_with_any_real_pick_counts_as_drafted(self):
        board = [drafted_pick(draft_class=2026),
                 big_board(draft_class=2026)]
        info = pv.classify_classes(board)
        self.assertEqual(info["drafted"], [2026])
        self.assertEqual(info["future"], [],
                         "a class is not both drafted and upcoming")

    def test_no_draft_data_yields_no_current_class(self):
        """The legacy-artifact path the page must degrade to.

        The committed fixture predates draft stamping. Claiming a rookie
        class from a file that records no picks would be invention.
        """
        info = pv.classify_classes([{"draft_class": 2024},
                                    {"draft_class": 2025}])
        self.assertEqual(info["drafted"], [])
        self.assertIsNone(info["current"])
        self.assertEqual(info["future"], [2024, 2025])

    def test_michael_penix_cannot_top_the_2026_board(self):
        """The reported symptom, as a scoping assertion.

        A 2024 player is not in the 2026 class, so no ordering decision can
        put him at the top of it.
        """
        penix = drafted_pick(name="Michael Penix", draft_class=2024,
                             projection={"projected_career_fp": 3200.0,
                                         "projection_source":
                                             "pick_tier_baseline_R1_top10",
                                         "n_comps_with_nfl": 0})
        board = [penix, drafted_pick()]
        rookie_board = pv.in_class(board, 2026)
        self.assertEqual([p["name"] for p in rookie_board], ["Carnell Tate"])


class TestSortForDisplay(unittest.TestCase):
    def test_evidence_beats_a_tier_constant_at_equal_value(self):
        constant = drafted_pick(
            name="Tier Constant", pick=None,
            projection={"projected_career_fp": 1000.0,
                        "projection_source": "pick_tier_baseline_R1",
                        "n_comps_with_nfl": 0})
        real = drafted_pick(name="Real Comps",
                            projection={"projected_career_fp": 1000.0,
                                        "projection_source": "comp_weighted",
                                        "n_comps_with_nfl": 14})
        order = [p["name"] for p in pv.sort_for_display([constant, real])]
        self.assertEqual(order[0], "Real Comps")

    def test_tier_constants_break_ties_on_real_draft_order(self):
        """Ordering a block of identical constants by dict order is arbitrary.

        When the projection carries no information about the player, draft
        position is the only real signal left.
        """
        late = drafted_pick(
            name="Late", drafted={"year": 2026, "round": 2, "pick": 40,
                                  "team": "CLE"},
            projection={"projected_career_fp": 3200.0,
                        "projection_source": "pick_tier_baseline_R1_top10",
                        "n_comps_with_nfl": 0})
        early = drafted_pick(
            name="Early", drafted={"year": 2026, "round": 1, "pick": 1,
                                   "team": "LVR"},
            projection={"projected_career_fp": 3200.0,
                        "projection_source": "pick_tier_baseline_R1_top10",
                        "n_comps_with_nfl": 0})
        order = [p["name"] for p in pv.sort_for_display([late, early])]
        self.assertEqual(order, ["Early", "Late"])

    def test_higher_projection_still_wins(self):
        low = drafted_pick(name="Low",
                           projection={"projected_career_fp": 100.0,
                                       "projection_source": "comp_weighted",
                                       "n_comps_with_nfl": 14})
        high = drafted_pick(name="High",
                            projection={"projected_career_fp": 900.0,
                                        "projection_source": "comp_weighted",
                                        "n_comps_with_nfl": 14})
        order = [p["name"] for p in pv.sort_for_display([low, high])]
        self.assertEqual(order, ["High", "Low"])

    def test_missing_projection_sorts_last_and_does_not_raise(self):
        ok = drafted_pick(name="OK")
        broken = drafted_pick(name="Broken", projection={})
        none_proj = drafted_pick(name="NoProj", projection=None)
        order = [p["name"] for p in
                 pv.sort_for_display([broken, ok, none_proj])]
        self.assertEqual(order[0], "OK")


class TestSaturationReport(unittest.TestCase):
    def test_it_counts_tier_constants_and_the_largest_identical_group(self):
        qbs = [
            drafted_pick(name=f"QB{i}", position="QB",
                         projection={"projected_career_fp": 3200.0,
                                     "projection_source":
                                         "pick_tier_baseline_R1_top10",
                                     "n_comps_with_nfl": 0})
            for i in range(3)
        ]
        report = pv.saturation_report(qbs + [drafted_pick()])
        self.assertEqual(report["n"], 4)
        self.assertEqual(report["n_baseline"], 3)
        self.assertEqual(report["n_evidence_backed"], 1)
        self.assertEqual(
            report["largest_identical_group"], 3,
            "three QBs sharing one constant is exactly what Phil saw")


if __name__ == "__main__":
    unittest.main()
