"""Cross-league corpus: aggregation maths, de-duplication and degradation.

Runs on the standard library alone (``python3 -m unittest``), like the rest of
the Manager Score coverage — no pip, no pytest, no network. Fixtures are
hand-built ``msScoreLeague``-shaped payloads, because the contract this file
pins is "given these per-league results, the cross-league board must say
this", and that must hold without a live crawl.

What is deliberately NOT tested here: the per-league metric itself. That is
the already-merged scorer, covered by tests/test_managerscore.py and the node
suites. This file only tests the aggregation on top of it, plus the one
guarantee that spans both — that the constants have not drifted apart.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dynasty import crossleague  # noqa: E402

NODE = shutil.which("node")


def comp(n, z, mean=0.0, shrunk=0.0):
    return {"n": n, "z": z, "mean": mean, "shrunk": shrunk, "available": n > 0}


def manager_row(mid, name, *, draft=(0, 0.0), trade=(0, 0.0), waiver=(0, 0.0),
                index=100.0, rank=1):
    return {
        "id": mid, "name": name,
        "draft": comp(*draft), "trade": comp(*trade), "waiver": comp(*waiver),
        "index": index, "rank": rank, "flags": [],
    }


def league(league_id, name, rows, *, season="2026", n_teams=12,
           lineage_ids=None, meta=None):
    return {
        "league_id": league_id, "name": name, "season": season,
        "n_teams": n_teams,
        "lineage_ids": lineage_ids if lineage_ids is not None else [league_id],
        "lineage_root": (lineage_ids or [league_id])[-1],
        "result": {"managers": rows, "drafts": [], "audit": {},
                   "weights": {}, "meta": meta or {}},
    }


class TestShrinkage(unittest.TestCase):
    """The guarantee the merged scorer pins, restated at the corpus level."""

    def test_shrink_never_overshoots_and_preserves_sign(self):
        for mean, n, k in [(2.0, 6, 6), (0.1, 40, 6), (-1.5, 10, 6),
                           (-0.2, 1, 3), (5.0, 1, 6), (0.0, 12, 5)]:
                s = crossleague.shrink(mean, n, k)
                self.assertLessEqual(abs(s), abs(mean) + 1e-12,
                                     f"overshot for mean={mean} n={n}")
                if s != 0:
                    self.assertEqual(s > 0, mean > 0,
                                     f"flipped sign for mean={mean} n={n}")

    def test_shrink_halves_at_n_equals_k(self):
        self.assertAlmostEqual(crossleague.shrink(2.0, 6, 6), 1.0)

    def test_no_events_means_no_credit(self):
        self.assertEqual(crossleague.shrink(9.0, 0, 6), 0.0)

    def test_constants_match_the_merged_scorer(self):
        """A weight change in managerscore_js must break the build, not drift.

        The cross-league board is the per-league metric aggregated. If the
        two disagree on weights or shrink constants they are two different
        metrics wearing one name.
        """
        from dynasty.managerscore_js import MANAGERSCORE_CORE_JS
        crossleague.assert_scoring_constants_match(MANAGERSCORE_CORE_JS)

    def test_constant_drift_is_actually_detected(self):
        """Prove the guard above can fail — otherwise it proves nothing."""
        from dynasty.managerscore_js import MANAGERSCORE_CORE_JS
        tampered = MANAGERSCORE_CORE_JS.replace(
            "MS_WEIGHTS = { draft: 0.50", "MS_WEIGHTS = { draft: 0.60")
        self.assertNotEqual(tampered, MANAGERSCORE_CORE_JS,
                            "fixture did not tamper anything — pattern moved")
        with self.assertRaises(AssertionError):
            crossleague.assert_scoring_constants_match(tampered)


class TestEvidenceWeighting(unittest.TestCase):

    def test_z_is_weighted_by_transaction_count_not_league_count(self):
        """30 picks of evidence must outweigh 3 picks, not tie with them.

        Unweighted averaging of per-league z-scores is the obvious
        implementation and it is wrong: it lets one thin league swing a
        manager's whole record.
        """
        rows_big = [manager_row("m1", "alice", draft=(30, 1.0)),
                    manager_row("m2", "bob", draft=(30, -1.0))]
        rows_small = [manager_row("m1", "alice", draft=(3, -1.0)),
                      manager_row("m3", "carol", draft=(3, 1.0))]
        out = crossleague.aggregate([
            league("L1", "Big", rows_big),
            league("L2", "Small", rows_small),
        ])
        alice = next(r for r in out["managers"] if r["manager_id"] == "m1")
        # weighted zbar = (30*1.0 + 3*-1.0) / 33 = 0.8181...
        self.assertAlmostEqual(alice["components"]["draft"]["zbar"],
                               (30 * 1.0 + 3 * -1.0) / 33, places=6)
        self.assertEqual(alice["components"]["draft"]["n"], 33)
        # An unweighted mean would have been (1.0 + -1.0)/2 = 0.0.
        self.assertGreater(alice["components"]["draft"]["zbar"], 0.5)

    def test_aggregate_shrinks_by_total_evidence(self):
        rows = [manager_row("m1", "alice", draft=(6, 2.0)),
                manager_row("m2", "bob", draft=(6, -2.0))]
        out = crossleague.aggregate([league("L1", "One", rows)])
        alice = next(r for r in out["managers"] if r["manager_id"] == "m1")
        # n == k == 6 -> exactly half
        self.assertAlmostEqual(alice["components"]["draft"]["z"], 1.0, places=6)


class TestVolumeAndLuckCannotWin(unittest.TestCase):

    def test_one_lucky_pick_cannot_top_the_draft_board(self):
        """The explicit brief: a one-league, one-lucky-draft manager must not
        top the board over someone with a long record."""
        lucky = manager_row("lucky", "luckyguy", draft=(1, 3.0))
        steady = manager_row("steady", "steadyhand", draft=(40, 1.0))
        filler = manager_row("filler", "filler", draft=(40, -1.0))
        out = crossleague.aggregate([
            league("L1", "One", [lucky, steady, filler])])
        board = out["draft_board"]
        names = [r["display_name"] for r in board]
        self.assertIn("steadyhand", names)
        # Gated off the board entirely: 1 pick < MIN_DRAFT_PICKS_FOR_BOARD.
        self.assertNotIn("luckyguy", names)
        self.assertEqual(board[0]["display_name"], "steadyhand")

    def test_lucky_pick_still_ranks_below_on_the_main_board(self):
        """Gated off the draft board, but not deleted from the corpus: the
        main leaderboard still lists them, shrunk toward neutral."""
        out = crossleague.aggregate([league("L1", "One", [
            manager_row("lucky", "luckyguy", draft=(1, 3.0)),
            manager_row("steady", "steadyhand", draft=(40, 1.0)),
            manager_row("filler", "filler", draft=(40, -1.0)),
        ])])
        by_name = {r["display_name"]: r for r in out["managers"]}
        self.assertIn("luckyguy", by_name)
        # 3.0 raw but n=1 against k=6 -> 3.0 * 1/7 = 0.4286
        self.assertAlmostEqual(by_name["luckyguy"]["components"]["draft"]["z"],
                               3.0 * 1 / 7, places=6)
        self.assertGreater(by_name["steadyhand"]["cross_index"],
                           by_name["luckyguy"]["cross_index"])

    def test_many_mediocre_transactions_cannot_manufacture_a_score(self):
        out = crossleague.aggregate([league("L1", "One", [
            manager_row("grinder", "grinder", draft=(400, 0.02)),
            manager_row("sharp", "sharp", draft=(20, 1.2)),
            manager_row("filler", "filler", draft=(20, -1.2)),
        ])])
        by_name = {r["display_name"]: r for r in out["managers"]}
        self.assertGreater(by_name["sharp"]["cross_index"],
                           by_name["grinder"]["cross_index"])


class TestWeightsAndComponents(unittest.TestCase):

    def test_weights_renormalise_over_live_components(self):
        """A corpus with no trades must not be scored on 35% of nothing."""
        out = crossleague.aggregate([league("L1", "One", [
            manager_row("m1", "a", draft=(10, 1.0)),
            manager_row("m2", "b", draft=(10, -1.0)),
        ])])
        w = out["components"]["weights"]
        self.assertEqual(out["components"]["live"], ["draft"])
        self.assertAlmostEqual(w["draft"], 1.0)
        self.assertAlmostEqual(w["trade"], 0.0)
        self.assertAlmostEqual(w["waiver"], 0.0)

    def test_all_three_components_renormalise_to_one(self):
        rows = [
            manager_row("m1", "a", draft=(10, 1.0), trade=(5, 0.5), waiver=(8, -0.2)),
            manager_row("m2", "b", draft=(10, -1.0), trade=(5, -0.5), waiver=(8, 0.2)),
        ]
        out = crossleague.aggregate([league("L1", "One", rows)])
        w = out["components"]["weights"]
        self.assertAlmostEqual(w["draft"] + w["trade"] + w["waiver"], 1.0)
        self.assertAlmostEqual(w["draft"], 0.50, places=6)

    def test_absent_component_is_flagged_not_penalised(self):
        rows = [
            manager_row("m1", "a", draft=(10, 1.0), trade=(5, 0.5)),
            manager_row("m2", "b", draft=(10, -1.0), trade=(5, -0.5)),
            manager_row("m3", "c", draft=(10, 0.0)),        # never traded
        ]
        out = crossleague.aggregate([league("L1", "One", rows)])
        c = next(r for r in out["managers"] if r["manager_id"] == "m3")
        self.assertEqual(c["components"]["trade"]["z"], 0.0)
        self.assertTrue(any("no trade activity" in f for f in c["flags"]))

    def test_index_is_center_plus_scale_times_composite(self):
        rows = [manager_row("m1", "a", draft=(6, 2.0)),
                manager_row("m2", "b", draft=(6, -2.0))]
        out = crossleague.aggregate([league("L1", "One", rows)])
        a = next(r for r in out["managers"] if r["manager_id"] == "m1")
        self.assertAlmostEqual(a["composite"], 1.0, places=6)
        self.assertAlmostEqual(a["cross_index"], 115.0, places=6)


class TestRankAndPercentile(unittest.TestCase):

    def test_rank_one_is_top_percentile(self):
        rows = [manager_row(f"m{i}", f"n{i}", draft=(10, 1.0 - i * 0.1))
                for i in range(10)]
        out = crossleague.aggregate([league("L1", "One", rows)])
        self.assertEqual(out["managers"][0]["rank"], 1)
        self.assertAlmostEqual(out["managers"][0]["percentile"], 100.0)
        self.assertEqual(out["managers"][-1]["rank"], 10)

    def test_per_league_breakdown_is_retained_for_audit(self):
        """A score nobody can take apart is a score nobody should trust."""
        out = crossleague.aggregate([
            league("L1", "Alpha", [manager_row("m1", "a", draft=(10, 1.0)),
                                   manager_row("m2", "b", draft=(10, -1.0))]),
            league("L2", "Beta", [manager_row("m1", "a", draft=(10, 0.5)),
                                  manager_row("m3", "c", draft=(10, -0.5))]),
        ])
        a = next(r for r in out["managers"] if r["manager_id"] == "m1")
        self.assertEqual(a["n_leagues"], 2)
        self.assertEqual({L["name"] for L in a["leagues"]}, {"Alpha", "Beta"})
        for L in a["leagues"]:
            self.assertIn("draft", L["components"])
            self.assertIn("n", L["components"]["draft"])


class TestRobustness(unittest.TestCase):

    def test_malformed_league_is_skipped_not_fatal(self):
        out = crossleague.aggregate([
            league("L1", "Good", [manager_row("m1", "a", draft=(10, 1.0)),
                                  manager_row("m2", "b", draft=(10, -1.0))]),
            {"league_id": "L2", "name": "Empty", "result": {"managers": []}},
            {"league_id": "L3", "name": "Junk"},
            None,
        ])
        self.assertEqual(len(out["managers"]), 2)
        self.assertEqual(len(out["skipped_leagues"]), 3)

    def test_empty_corpus_does_not_crash(self):
        out = crossleague.aggregate([])
        self.assertEqual(out["managers"], [])
        self.assertEqual(out["draft_board"], [])


class TestLineageDedup(unittest.TestCase):

    def test_same_dynasty_chain_counted_once(self):
        """The seed alone is four league ids for one league. Counting the
        chain as four leagues would inflate coverage fourfold."""
        head = league("2026id", "Dallas Kings",
                      [manager_row("m1", "a", draft=(10, 1.0))],
                      lineage_ids=["2026id", "2025id", "2024id", "2023id"])
        older = league("2024id", "Dallas Kings",
                       [manager_row("m1", "a", draft=(5, 0.5))],
                       lineage_ids=["2024id", "2023id"])
        kept, dropped = crossleague.dedupe_lineages([head, older])
        self.assertEqual([k["league_id"] for k in kept], ["2026id"])
        self.assertEqual(len(dropped), 1)
        self.assertIn("same dynasty chain", dropped[0]["reason"])

    def test_longest_chain_wins(self):
        short = league("A", "X", [manager_row("m1", "a")], lineage_ids=["A"])
        long_ = league("B", "X", [manager_row("m1", "a")],
                       lineage_ids=["B", "A"])
        kept, dropped = crossleague.dedupe_lineages([short, long_])
        self.assertEqual([k["league_id"] for k in kept], ["B"])

    def test_distinct_leagues_both_kept(self):
        a = league("A", "One", [manager_row("m1", "a")], lineage_ids=["A", "A0"])
        b = league("B", "Two", [manager_row("m2", "b")], lineage_ids=["B", "B0"])
        kept, dropped = crossleague.dedupe_lineages([a, b])
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, [])

    def test_dedup_is_applied_by_build_corpus(self):
        head = league("2026id", "Kings", [manager_row("m1", "a", draft=(10, 1.0)),
                                          manager_row("m2", "b", draft=(10, -1.0))],
                      lineage_ids=["2026id", "2025id"])
        dup = league("2025id", "Kings", [manager_row("m1", "a", draft=(10, 1.0))],
                     lineage_ids=["2025id"])
        corpus = crossleague.build_corpus([head, dup])
        self.assertEqual(corpus["coverage"]["n_leagues"], 1)
        self.assertEqual(len(corpus["dropped_leagues"]), 1)


class TestCompaction(unittest.TestCase):
    """Retained results must stay small: this file is committed every day.

    The first real run produced a 2.3 MB artifact because the retained block
    carried the full per-transaction audit (one 8-season league alone had 510
    picks, 219 trades and 1,310 waiver claims). Compaction took it to 118 KB.
    A multi-megabyte file rewritten daily bloats the repo permanently.
    """

    def _full_entry(self):
        row = manager_row("m1", "alice", draft=(10, 1.0), trade=(4, 0.3),
                          waiver=(7, -0.2), index=112.0, rank=1)
        entry = league("L1", "Alpha", [row],
                       meta={"nPicksScored": 10, "nTradesScored": 4,
                             "nWaivers": 7, "floor": 491})
        # The bulk the page needs and the corpus does not.
        entry["result"]["audit"] = {
            "picks": [{"name": f"Player {i}", "value": i} for i in range(500)],
            "trades": [{"id": str(i)} for i in range(200)],
            "waivers": [{"name": f"WR{i}"} for i in range(1300)],
        }
        entry["result"]["drafts"] = [{"id": "d1", "nPicks": 500}]
        return entry

    def test_audit_is_dropped(self):
        out = crossleague.compact_result(self._full_entry())
        self.assertNotIn("audit", out["result"])
        self.assertNotIn("drafts", out["result"])

    def test_compaction_is_much_smaller(self):
        full = self._full_entry()
        compact = crossleague.compact_result(full)
        self.assertLess(len(json.dumps(compact)),
                        len(json.dumps(full)) / 10,
                        "compaction must be a large win, not a rounding one")

    def test_compaction_preserves_everything_aggregation_uses(self):
        """The property that makes compaction safe: identical boards."""
        full = [
            self._full_entry(),
            league("L2", "Beta", [
                manager_row("m1", "alice", draft=(20, 0.5)),
                manager_row("m2", "bob", draft=(20, -0.5)),
            ]),
        ]
        compact = [crossleague.compact_result(e) for e in full]

        a_full = crossleague.aggregate(full)
        a_compact = crossleague.aggregate(compact)

        self.assertEqual(
            [(r["display_name"], r["cross_index"], r["rank"])
             for r in a_full["managers"]],
            [(r["display_name"], r["cross_index"], r["rank"])
             for r in a_compact["managers"]])
        self.assertEqual(
            [(r["display_name"], r["draft_z"]) for r in a_full["draft_board"]],
            [(r["display_name"], r["draft_z"])
             for r in a_compact["draft_board"]])

    def test_compaction_keeps_lineage_for_dedup(self):
        out = crossleague.compact_result(self._full_entry())
        self.assertIn("lineage_ids", out)
        self.assertEqual(out["lineage_ids"], ["L1"])

    def test_committed_corpus_stays_small_if_present(self):
        """Guards the real artifact against regrowing.

        This budget is **per indexed manager**, not absolute, and the change
        from a flat 1 MB cap is deliberate rather than a threshold being
        relaxed to make a red test green.

        The regression this guard exists to catch is the per-transaction
        audit coming back: the first real run shipped 2.3 MB for 56 managers
        (~41,000 bytes each) because it carried 510 picks, 219 trades and
        1,310 waiver claims per league. Compaction took that to ~2,100 bytes
        per manager. A flat cap could not tell those two apart once the
        corpus itself was allowed to grow -- it would fail identically for
        "the audit is back" and for "we indexed twenty times more leagues,
        exactly as asked". A per-manager budget only fires on the first.

        Measured at 110 leagues / 1,283 managers: 1,664,465 bytes, or ~1,297
        bytes per manager. The 2,500-byte budget leaves room for the score
        distribution to fill out while still tripping at ~1.9x, long before
        anything audit-shaped (16x) could land.

        The absolute ceiling is a separate, cruder backstop on repository
        growth, since this file is rewritten and committed every single day.
        Reaching it is not a licence to raise it: it means the retained block
        should move to its own artifact, or the published leaderboard should
        stop carrying a full per-league breakdown for every manager.
        """
        path = REPO_ROOT / "data" / "cross_league" / "corpus.json"
        if not path.exists():
            self.skipTest("no committed corpus in this checkout")
        size = path.stat().st_size
        mb = size / 1e6

        corpus = json.loads(path.read_text(encoding="utf-8"))
        n_managers = (corpus.get("coverage") or {}).get("n_managers") or 0
        if n_managers:
            per_manager = size / n_managers
            self.assertLess(
                per_manager, 2500,
                f"committed corpus is {per_manager:,.0f} bytes per indexed "
                f"manager ({mb:.2f} MB / {n_managers:,} managers). That is "
                f"audit-shaped: check that compact_result() is still "
                f"dropping the per-transaction detail.")

        self.assertLess(
            mb, 8.0,
            f"committed corpus is {mb:.2f} MB and is rewritten daily. Split "
            f"the retained block into its own artifact rather than raising "
            f"this number.")

    def test_published_artifact_omits_crawler_state(self):
        """Visitors must not download the crawler's resumption state.

        ``retained`` exists so the NEXT crawl can re-aggregate leagues that
        fell outside today's budget. The page never reads it, and at 110
        leagues it was ~450 KB -- a quarter of the artifact -- on every page
        load.
        """
        import tempfile as _tf
        corpus = crossleague.build_corpus([
            league("L1", "Alpha", [manager_row("m1", "alice", draft=(10, 1.0))]),
        ])
        corpus["retained"] = [{"league_id": "L1", "result": {"managers": []}}]
        with _tf.TemporaryDirectory() as td:
            path = crossleague.write_corpus_artifact(Path(td), corpus)
            published = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("retained", published)
        self.assertEqual(published["retained_omitted"]["n_leagues"], 1)
        # Everything the page actually renders must survive.
        for key in ("coverage", "leaderboard", "draft_board", "leagues"):
            self.assertIn(key, published)

    def test_committed_corpus_carries_no_audit_block(self):
        path = REPO_ROOT / "data" / "cross_league" / "corpus.json"
        if not path.exists():
            self.skipTest("no committed corpus in this checkout")
        corpus = json.loads(path.read_text(encoding="utf-8"))
        for entry in corpus.get("retained") or []:
            self.assertNotIn("audit", entry.get("result") or {})


class TestPersistenceDegradation(unittest.TestCase):
    """Without committed write access the feature must still work.

    The corpus is a committed artifact refreshed by the daily job. If the
    workflow cannot write to the repo, the crawl still runs and the page
    still ranks -- it just describes this run instead of a growing index.
    """

    def test_no_previous_corpus_degrades_to_indexed_this_run(self):
        corpus = crossleague.build_corpus([
            league("L1", "One", [manager_row("m1", "a", draft=(10, 1.0)),
                                 manager_row("m2", "b", draft=(10, -1.0))])],
            previous=None)
        self.assertFalse(corpus["persisted"])
        self.assertEqual(corpus["n_runs"], 1)
        self.assertTrue(any("only what this run indexed" in n
                            for n in corpus["notes"]))

    def test_previous_corpus_carries_provenance_forward(self):
        first = crossleague.build_corpus([
            league("L1", "One", [manager_row("m1", "a", draft=(10, 1.0)),
                                 manager_row("m2", "b", draft=(10, -1.0))])],
            previous=None,
            generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        second = crossleague.build_corpus([
            league("L1", "One", [manager_row("m1", "a", draft=(10, 1.0)),
                                 manager_row("m2", "b", draft=(10, -1.0))])],
            previous=first,
            generated_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
        self.assertTrue(second["persisted"])
        self.assertEqual(second["n_runs"], 2)
        self.assertEqual(second["first_indexed_at"], first["first_indexed_at"])

    def test_merge_retains_prior_leagues_only_when_persisted(self):
        prior = crossleague.build_corpus([
            league("L1", "One", [manager_row("m1", "a", draft=(10, 1.0)),
                                 manager_row("m2", "b", draft=(10, -1.0))])],
            previous=None)
        prior["retained"] = [{
            "league_id": "L1", "name": "One", "season": "2026",
            "lineage_ids": ["L1"],
            "result": {"managers": [manager_row("m1", "a", draft=(10, 1.0))]},
        }]
        fresh = [league("L2", "Two", [manager_row("m3", "c", draft=(10, 1.0))])]

        merged = crossleague.merge_corpus(prior, fresh)
        self.assertEqual({str(e["league_id"]) for e in merged}, {"L1", "L2"})

        # An un-persisted prior corpus has no first_indexed_at, so nothing is
        # carried: the run stands alone rather than inventing a history.
        not_persisted = dict(prior)
        not_persisted["first_indexed_at"] = None
        merged2 = crossleague.merge_corpus(not_persisted, fresh)
        self.assertEqual({str(e["league_id"]) for e in merged2}, {"L2"})

    def test_this_run_wins_over_retained_copy_of_same_league(self):
        prior = {"first_indexed_at": "2026-01-01T00:00:00+00:00",
                 "generated_at": "2026-01-01T00:00:00+00:00",
                 "retained": [{"league_id": "L1", "name": "stale",
                               "result": {"managers": []}}]}
        fresh = [league("L1", "fresh", [manager_row("m1", "a", draft=(10, 1.0))])]
        merged = crossleague.merge_corpus(prior, fresh)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["name"], "fresh")

    def test_load_corpus_tolerates_missing_and_corrupt(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(crossleague.load_corpus(Path(td) / "nope.json"))
            bad = Path(td) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            self.assertIsNone(crossleague.load_corpus(bad))
            wrong = Path(td) / "wrong.json"
            wrong.write_text(json.dumps({"schema": "something.else"}),
                             encoding="utf-8")
            self.assertIsNone(crossleague.load_corpus(wrong))


class TestCoverageHonesty(unittest.TestCase):

    def test_coverage_sentence_matches_the_requested_phrasing(self):
        corpus = {"coverage": {"n_managers": 340, "n_leagues": 28}}
        self.assertEqual(
            crossleague.coverage_sentence(corpus),
            "ranked against 340 managers across 28 dynasty leagues we've indexed")

    def test_coverage_sentence_is_singular_for_one(self):
        corpus = {"coverage": {"n_managers": 1, "n_leagues": 1}}
        self.assertEqual(
            crossleague.coverage_sentence(corpus),
            "ranked against 1 manager across 1 dynasty league we've indexed")

    def test_coverage_sentence_never_claims_all_of_sleeper(self):
        corpus = {"coverage": {"n_managers": 340, "n_leagues": 28}}
        s = crossleague.coverage_sentence(corpus).lower()
        for forbidden in ("all ", "every ", "entire", "complete"):
            self.assertNotIn(forbidden, s)
        self.assertIn("indexed", s)

    def test_coverage_counts_are_the_real_counts(self):
        corpus = crossleague.build_corpus([
            league("L1", "One", [manager_row("m1", "a", draft=(10, 1.0)),
                                 manager_row("m2", "b", draft=(10, -1.0))]),
            league("L2", "Two", [manager_row("m1", "a", draft=(10, 1.0)),
                                 manager_row("m3", "c", draft=(10, -1.0))]),
        ])
        self.assertEqual(corpus["coverage"]["n_leagues"], 2)
        self.assertEqual(corpus["coverage"]["n_managers"], 3)
        self.assertEqual(
            crossleague.coverage_sentence(corpus),
            "ranked against 3 managers across 2 dynasty leagues we've indexed")


class TestPrivacy(unittest.TestCase):

    def test_only_display_name_is_carried_into_the_artifact(self):
        """No field that could carry a real identity may reach the corpus.

        Sleeper's league/user payloads include avatars, team metadata and
        usernames. The artifact carries the pseudonymous display_name and the
        opaque user_id, and nothing else about a person.
        """
        corpus = crossleague.build_corpus([
            league("L1", "One", [manager_row("m1", "pjstiehl", draft=(10, 1.0)),
                                 manager_row("m2", "SimpsDontCry", draft=(10, -1.0))])])
        blob = json.dumps(corpus)
        for leaked in ("avatar", "email", "real_name", "phone", "username"):
            self.assertNotIn(leaked, blob,
                             f"{leaked} must never reach the corpus artifact")
        allowed = {"manager_id", "display_name", "composite", "cross_index",
                   "n_leagues", "components", "flags", "leagues", "rank",
                   "percentile"}
        self.assertEqual(set(corpus["leaderboard"][0].keys()), allowed)

    def test_no_worst_manager_board_is_produced(self):
        """Ranked positively; the tail is just the tail. There is no
        shaming board and no 'worst' ordering in the artifact."""
        corpus = crossleague.build_corpus([
            league("L1", "One", [manager_row("m1", "a", draft=(10, 1.0)),
                                 manager_row("m2", "b", draft=(10, -1.0))])])
        self.assertNotIn("worst_board", corpus)
        keys = " ".join(corpus.keys()).lower()
        for forbidden in ("worst", "shame", "bust", "loser"):
            self.assertNotIn(forbidden, keys)
        # Boards are ordered best-first.
        self.assertEqual(corpus["leaderboard"][0]["rank"], 1)
        self.assertGreaterEqual(corpus["leaderboard"][0]["cross_index"],
                                corpus["leaderboard"][-1]["cross_index"])


class TestPageJs(unittest.TestCase):
    """The renderers, exercised in node against a stub DOM.

    Mirrors how tests/test_managerscore.py verifies the Manager Score page:
    the page JS is the only implementation, so it is asserted where it runs
    rather than re-derived in Python. Skips loudly if node is absent instead
    of passing silently.
    """

    def test_crossleague_js_parses_and_renders(self):
        if not NODE:
            self.skipTest("node not available — page JS assertions skipped")
        from dynasty.crossleague_js import CROSSLEAGUE_JS

        with tempfile.TemporaryDirectory() as td:
            js = Path(td) / "xl.js"
            js.write_text(CROSSLEAGUE_JS, encoding="utf-8")

            syntax = subprocess.run([NODE, "--check", str(js)],
                                    capture_output=True, text=True)
            self.assertEqual(syntax.returncode, 0,
                             f"CROSSLEAGUE_JS is not valid JS:\n{syntax.stderr}")

            suite = REPO_ROOT / "tests" / "js" / "crossleague_dom_tests.js"
            proc = subprocess.run([NODE, str(suite), str(js)],
                                  capture_output=True, text=True)
            self.assertEqual(
                proc.returncode, 0,
                f"renderer assertions failed:\n{proc.stdout}\n{proc.stderr}")

    def test_detail_panels_open_in_the_table_that_was_clicked(self):
        """The drill-down, asserted against a DOM that can return null.

        tests/js/dom_stub.js auto-creates an element for every
        getElementById, so it cannot fail either half of the bug the owner
        reported three times: a draft board that emitted no detail row at
        all (xlToggle's ``if (!row) return;`` silently did nothing), and one
        unscoped ``xl-detail-<id>`` emitted by both tables for the same
        manager (getElementById returns the first, so a click in one table
        toggled a hidden row in the other).

        This runs the shipped JS against tests/js/mini_dom.mjs, which parses
        the rendered markup and resolves ids for real. It uses the suite's
        built-in fixture corpus, so it needs no network; the same suite takes
        a real crossleague_corpus.json as argv[2] for manual runs.
        """
        if not NODE:
            self.skipTest("node not available — drill-down assertions skipped")
        from dynasty.crossleague_js import CROSSLEAGUE_JS

        with tempfile.TemporaryDirectory() as td:
            js = Path(td) / "xl.js"
            js.write_text(CROSSLEAGUE_JS, encoding="utf-8")

            suite = REPO_ROOT / "tests" / "js" / "crossleague_scope_tests.mjs"
            proc = subprocess.run([NODE, str(suite), str(js)],
                                  capture_output=True, text=True)
            self.assertEqual(
                proc.returncode, 0,
                "drill-down panel assertions failed:\n"
                f"{proc.stdout}\n{proc.stderr}")

    def test_harness_is_valid_js(self):
        """The scoring harness runs the shipped page code; if it will not
        parse, the whole corpus build is dead."""
        if not NODE:
            self.skipTest("node not available")
        harness = REPO_ROOT / "scripts" / "js" / "score_league_harness.js"
        proc = subprocess.run([NODE, "--check", str(harness)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_harness_reuses_the_shipped_scoring_functions(self):
        """Guard against the harness quietly growing its own scorer.

        The whole point of the node harness is that the cross-league board
        aggregates the *same* metric the Manager Score page shows. If it ever
        stops calling the shipped functions by name, that has been lost.
        """
        harness = (REPO_ROOT / "scripts" / "js" / "score_league_harness.js"
                   ).read_text(encoding="utf-8")
        for fn in ("msFetchLeagueData", "msBuildInput", "msScoreLeague"):
            self.assertIn(fn, harness,
                          f"harness must call the shipped {fn}")
        # No second implementation of the maths.
        for forbidden in ("function msShrink", "function msZScores",
                          "function msExpectedCurve", "MS_WEIGHTS ="):
            self.assertNotIn(
                forbidden, harness,
                f"harness must not reimplement scoring ({forbidden})")


class TestPageRender(unittest.TestCase):
    """Render crossleague.html with a stub ``dynasty.report``.

    ``build_cross_league`` imports ``_page``/``_site_header`` lazily, inside
    the function, specifically to avoid an import cycle — so a stub is sound
    here. The real ``report.py`` wiring is checked by reading its source in
    ``TestSiteWiring`` below.
    """

    @classmethod
    def setUpClass(cls):
        import types
        stub = types.ModuleType("dynasty.report")

        def _page(title, header_html, body_html, css_href="assets/style.css"):
            return f"<!doctype html><title>{title}</title>{header_html}{body_html}"

        def _site_header(active, latest_ts, league_label):
            return f'<header data-active="{active}">nav</header>'

        stub._page = _page
        stub._site_header = _site_header
        sys.modules["dynasty.report"] = stub

        from dynasty.crossleague_page import build_cross_league
        cls.html = build_cross_league(datetime(2026, 9, 21, 12, 0), "sf_ppr")
        cls.flat = " ".join(
            __import__("re").sub(r"<[^>]+>", " ", cls.html).split())

    def test_every_placeholder_is_substituted(self):
        for token in ("__METHODOLOGY__", "__CROSSLEAGUE_CSS__",
                      "__CROSSLEAGUE_JS__"):
            self.assertNotIn(token, self.html)

    def test_marks_itself_active_in_the_nav(self):
        self.assertIn('data-active="crossleague"', self.html)

    def test_mount_points_the_renderer_writes_to_all_exist(self):
        for el in ("xl-coverage", "xl-draft-board", "xl-leaderboard",
                   "xl-leagues", "xl-results"):
            self.assertIn(f'id="{el}"', self.html,
                          f"renderer writes to #{el}")

    def test_names_the_artifact_it_consumes(self):
        self.assertIn("crossleague_corpus.json", self.html)

    def test_states_coverage_limits_prominently(self):
        """The page must never imply it covers Sleeper."""
        self.assertIn("never a census of Sleeper", self.flat)
        self.assertIn("Sleeper cannot be enumerated", self.flat)
        self.assertIn("bounded", self.flat)

    def test_privacy_note_is_on_the_page_in_plain_language(self):
        self.assertIn("read-only, unauthenticated API", self.flat)
        self.assertIn("pseudonymous handle", self.flat)
        self.assertIn("no attempt is made to connect a handle to a real person",
                      self.flat)
        self.assertIn("real names, emails, avatars", self.flat)

    def test_refuses_a_shaming_board_explicitly(self):
        self.assertIn('There is no "worst managers" board', self.flat)
        self.assertIn("ranked best-first", self.flat)

    def test_records_the_non_commercial_constraint(self):
        self.assertIn("non-commercial", self.flat)
        self.assertIn("CROSS-LEAGUE-CORPUS.md", self.flat)

    def test_explains_the_dynasty_filter_is_not_name_matching(self):
        self.assertIn("not on the league's name", self.flat)

    def test_draft_board_is_present_and_prominent(self):
        """The owner asked for the drafting board specifically, so it must
        appear before the overall board in the document."""
        self.assertIn("Best", self.html)
        self.assertLess(self.html.find('id="xl-draft-board"'),
                        self.html.find('id="xl-leaderboard"'),
                        "the draft board must come before the overall board")

    def test_links_back_to_the_per_league_page(self):
        """The cross-league board must point at the per-league metric.

        The target moved: the rebrand (#69) folded the standalone
        ``managerscore.html`` into a section of ``myteam.html`` and updated
        the page, but not this assertion, so it has been red on main since
        that merge. What the test is actually for -- "this board must not
        become an orphan; a reader has to be able to reach the per-league
        metric it aggregates" -- is unchanged, so only the href moves.
        """
        self.assertIn("myteam.html", self.html)
        self.assertNotIn(
            "managerscore.html", self.html,
            "managerscore.html no longer exists; a link to it would 404")


class TestSiteWiring(unittest.TestCase):
    """Guards against somebody removing the nav entry or the build call."""

    @classmethod
    def setUpClass(cls):
        cls.source = (REPO_ROOT / "src" / "dynasty" / "report.py"
                      ).read_text(encoding="utf-8")

    def test_nav_links_to_the_page(self):
        self.assertIn(
            'link("crossleague.html", "Best Managers", "crossleague")',
            self.source)

    def test_entry_is_in_the_primary_nav(self):
        primary = self.source[self.source.find("<nav>"):
                              self.source.find("</nav>")]
        self.assertIn("crossleague.html", primary,
                      "Best Managers must be in the primary nav row")

    def test_generate_site_builds_the_page(self):
        self.assertIn("build_cross_league(latest_ts, label)", self.source)
        self.assertIn('"crossleague.html"', self.source)

    def test_generate_site_publishes_the_corpus_when_present(self):
        self.assertIn("write_corpus_artifact(out_root, _corpus)", self.source)

    def test_site_build_degrades_without_a_corpus(self):
        """An absent corpus must warn and still build the page, not raise."""
        block = self.source[self.source.find("from .crossleague import"):]
        block = block[:block.find("# sleeper_id ->")]
        self.assertIn("if _corpus:", block)
        self.assertIn("else:", block)
        self.assertIn("warning", block)
        # The page is written outside the if/else, so it is always built.
        self.assertIn('"crossleague.html"', block)
        self.assertIn("except Exception", block)

    def test_site_build_never_crawls(self):
        """Publishing is a copy. A site build must not hit Sleeper.

        Checked against network/subprocess entry points rather than the word
        "crawl": the block legitimately *mentions* the crawler in a warning
        telling the operator how to build a corpus.
        """
        block = self.source[self.source.find("from .crossleague import"):]
        block = block[:block.find("# sleeper_id ->")]
        for forbidden in ("api.sleeper", "urlopen", "requests.", "httpx",
                          "subprocess", "crawl_cross_league.main",
                          "import crawl"):
            self.assertNotIn(forbidden, block,
                             "the site build must not crawl or spawn one")


class TestWorkflowWiring(unittest.TestCase):
    """The daily job is the only persistence mechanism; pin its shape."""

    @classmethod
    def setUpClass(cls):
        cls.wf = (REPO_ROOT / ".github" / "workflows" / "daily-refresh.yml"
                  ).read_text(encoding="utf-8")

    def test_corpus_refresh_step_exists(self):
        self.assertIn("crawl_cross_league.py", self.wf)

    def test_refresh_passes_explicit_caps(self):
        for flag in ("--max-hops", "--max-leagues", "--max-calls",
                     "--min-delay"):
            self.assertIn(flag, self.wf,
                          f"the daily crawl must pass {flag} explicitly")

    def test_corpus_is_committed_back(self):
        self.assertIn("chore(crossleague): refresh manager corpus [skip ci]",
                      self.wf)
        self.assertIn("git add data/cross_league", self.wf)

    def test_commit_step_skips_ci_to_avoid_a_loop(self):
        """This workflow triggers on push to main."""
        idx = self.wf.find("chore(crossleague)")
        self.assertIn("[skip ci]", self.wf[idx:idx + 120])

    def test_crawl_and_commit_never_block_the_deploy(self):
        """Both steps are best-effort: the site must deploy regardless."""
        for anchor in ("Refresh the cross-league manager corpus",
                       "Retain the cross-league corpus"):
            # Anchor on the step declaration, not a comment mentioning it.
            idx = self.wf.find(f"- name: {anchor}")
            self.assertGreater(idx, 0, f"step missing: {anchor}")
            self.assertIn("continue-on-error: true",
                          self.wf[idx:idx + 200],
                          f"{anchor} must not block the deploy")

    def test_crawl_runs_before_the_pages_upload(self):
        self.assertLess(self.wf.find("crawl_cross_league.py"),
                        self.wf.find("upload-pages-artifact"),
                        "the corpus must be refreshed before the site is "
                        "uploaded")

    def test_crawl_runs_after_the_model_build(self):
        """generate_site() publishes the committed corpus; the crawl must run
        after it so the fresh copy wins."""
        self.assertLess(self.wf.find("dynasty.launcher_headless"),
                        self.wf.find("crawl_cross_league.py"))

    def test_contents_write_is_granted(self):
        """Necessary but NOT sufficient: the repository-level Actions setting
        can still cap the token. See docs section 7.2 -- the design does not
        assume this works."""
        self.assertIn("contents: write", self.wf)


class TestCrawlerContract(unittest.TestCase):
    """The crawl's hard constraints, asserted on the source.

    These are guardrails against a future edit quietly removing a bound. A
    crawl of somebody else's free API with no cap is the failure mode that
    matters here, and it is cheap to pin.
    """

    def setUp(self):
        self.src = (REPO_ROOT / "scripts" / "crawl_cross_league.py"
                    ).read_text(encoding="utf-8")

    def test_dynasty_filter_is_the_confirmed_value(self):
        self.assertIn("DYNASTY_LEAGUE_TYPE = 2", self.src)

    def test_filter_is_on_settings_type_not_the_league_name(self):
        self.assertIn('settings.get("type")', self.src)
        self.assertNotIn('"dynasty" in (league.get("name")', self.src)

    def test_every_budget_is_explicit_and_configurable(self):
        for flag in ("--max-hops", "--max-leagues", "--max-calls",
                     "--min-delay"):
            self.assertIn(flag, self.src, f"{flag} must be configurable")

    def test_budget_is_reported(self):
        self.assertIn("calls_used_total", self.src)
        self.assertIn("calls_used_discovery", self.src)
        self.assertIn("calls_used_scoring", self.src)

    def test_user_agent_is_honest_and_self_identifying(self):
        self.assertIn("USER_AGENT", self.src)
        self.assertIn("github.com/pstiehl/Dynasty-Football-Model", self.src)
        for spoof in ("Mozilla/5.0", "Chrome/", "Safari/"):
            self.assertNotIn(spoof, self.src,
                             "never spoof a browser to evade access controls")

    def test_non_commercial_constraint_is_recorded_in_docs(self):
        doc = REPO_ROOT / "docs" / "CROSS-LEAGUE-CORPUS.md"
        self.assertTrue(doc.exists(), "the licensing constraint must be "
                                      "recorded in docs/")
        text = doc.read_text(encoding="utf-8").lower()
        self.assertIn("non-commercial", text)
        self.assertIn("licens", text)

    def test_partial_reads_are_discarded_not_scored(self):
        harness = (REPO_ROOT / "scripts" / "js" / "score_league_harness.js"
                   ).read_text(encoding="utf-8")
        self.assertIn("leagueRefused", harness)
        self.assertIn("discarded rather than scored on partial data", harness)


if __name__ == "__main__":
    unittest.main(verbosity=2)
