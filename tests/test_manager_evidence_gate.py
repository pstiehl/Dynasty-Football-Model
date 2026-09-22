"""Only managers whose score can be explained appear on the board.

Phil, 2026-09-22: "I only want to display managers where you can see the
evidence of their draft, waiver wire, trade data ... for example you cannot
see the data for this manager: FantasyticBeast. Users like that should not
be included if we cannot see what is driving the manager score in the drill
down."

The condition is real and reproducible from the committed corpus:
``FantasyticBeast`` sits at rank 4 with a score of 118.7 off a single
league. The score is correct -- 20 scored picks, z = 2.16 on the draft
component -- but the pick-level audit behind it lives in the build cache
(``data/cross_league/detail/``, gitignored), so on a build where that
league was not re-scored the row opens an empty panel.

What this file can and cannot check, stated plainly
---------------------------------------------------
The detail store is **not in the repository** by design, so no test in a
clone can assert what the live build retained. What is asserted here is the
gate's behaviour against the **real 2,302-manager corpus** with a detail
store constructed to match each case:

* a store that lacks FantasyticBeast's only league -> withheld,
* a store that contains it -> listed again,
* an empty store -> gate not applied, and says so,

plus the property that matters most: the gate's verdict agrees with
``build_manager_detail``'s own coverage for every manager in the corpus. If
those two ever disagree, a row is shown whose panel is empty (or hidden
though it had evidence), which is the whole bug.

Stdlib only: ``dynasty.manager_detail`` imports nothing third-party, so
this runs on a bare checkout.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dynasty import manager_detail as md  # noqa: E402

CORPUS_PATH = REPO_ROOT / "data" / "cross_league" / "corpus.json"

#: The manager Phil named. Kept as a constant because it is the acceptance
#: check, not an example.
PHIL_CASE = "FantasyticBeast"


def load_corpus() -> dict:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


def synth_detail(league_id: str, manager_ids) -> dict:
    """A league audit shaped like ``league_detail_from_result`` output.

    Only the fields the gate and ``build_manager_detail`` read are filled;
    the schema key matters because ``read_league_details`` filters on it.
    """
    return {
        "schema": md.LEAGUE_DETAIL_SCHEMA,
        "league_id": str(league_id),
        "name": "synthetic",
        "season": "2026",
        "drafts": [],
        "weights": {},
        "managers": [
            {
                "id": str(mid),
                "name": f"m{mid}",
                "components": {},
                "picks": [],
                "trades": [],
                "trades_unscored": [],
                "waivers": [],
            }
            for mid in manager_ids
        ],
    }


def find_row(corpus: dict, display_name: str):
    for row in corpus["leaderboard"]:
        if str(row.get("display_name")) == display_name:
            return row
    return None


def names(rows) -> set:
    return {str(r.get("display_name")) for r in rows}


class TestCorpusPrecondition(unittest.TestCase):
    """The bug Phil reported must still be present in the input data.

    If this fails, the rest of the file is asserting against a corpus that
    no longer contains the case, and a green run would mean nothing.
    """

    def test_phils_named_manager_is_ranked_in_the_committed_corpus(self):
        corpus = load_corpus()
        row = find_row(corpus, PHIL_CASE)
        self.assertIsNotNone(
            row, f"{PHIL_CASE} is no longer in the committed corpus; "
                 "re-derive the acceptance case before editing this test")
        self.assertEqual(len(row.get("leagues") or []), 1,
                         "the case is a single-league manager")
        self.assertLessEqual(
            row["rank"], 20,
            f"{PHIL_CASE} is ranked {row['rank']} -- the point of the case "
            "is that an unexplainable manager was near the top")


class TestEvidenceGate(unittest.TestCase):
    def setUp(self):
        self.corpus = load_corpus()
        self.row = find_row(self.corpus, PHIL_CASE)
        self.assertIsNotNone(self.row)
        self.mid = str(self.row["manager_id"])
        self.league_id = str(self.row["leagues"][0]["league_id"])

    # ------------------------------------------------------ the named case
    def test_manager_without_evidence_is_withheld(self):
        """FantasyticBeast must not be in the published table.

        The detail store here holds a *different* league, so it is
        non-empty (the gate applies) but has nothing for this manager.
        """
        other = next(
            r for r in self.corpus["leaderboard"]
            if str(r["manager_id"]) != self.mid and (r.get("leagues") or [])
        )
        other_league = str(other["leagues"][0]["league_id"])
        details = {
            other_league: synth_detail(
                other_league, [str(other["manager_id"])]),
        }

        gate = md.apply_evidence_gate(self.corpus, details)

        self.assertTrue(gate["applied"])
        shown = names(self.corpus["leaderboard"])
        self.assertNotIn(
            PHIL_CASE, shown,
            f"{PHIL_CASE} has no drill-down evidence and must not be listed")
        self.assertIn(
            str(other["display_name"]), shown,
            "a manager whose league audit IS retained must still appear")
        self.assertNotIn(
            PHIL_CASE, names(self.corpus["draft_board"]),
            "the draft board is gated on the same evidence as the "
            "overall board")

    def test_the_same_manager_returns_once_evidence_exists(self):
        """The gate keys on evidence, not on the identity of the manager."""
        details = {
            self.league_id: synth_detail(self.league_id, [self.mid]),
        }
        gate = md.apply_evidence_gate(self.corpus, details)

        self.assertTrue(gate["applied"])
        self.assertIn(
            PHIL_CASE, names(self.corpus["leaderboard"]),
            "with the league audit present the row is explainable and "
            "should be listed")
        self.assertEqual(gate["n_shown"], 1)
        self.assertEqual(gate["n_withheld"], gate["n_scored"] - 1)

    # ------------------------------------------------------------ honesty
    def test_withheld_count_is_published_not_swallowed(self):
        details = {self.league_id: synth_detail(self.league_id, [self.mid])}
        n_scored_before = len(self.corpus["leaderboard"])

        gate = md.apply_evidence_gate(self.corpus, details)

        self.assertEqual(gate["n_scored"], n_scored_before)
        self.assertEqual(
            gate["n_shown"] + gate["n_withheld"], gate["n_scored"],
            "shown + withheld must account for every scored manager")
        self.assertEqual(self.corpus["evidence_gate"], gate_without_log(gate))
        self.assertIn("withheld", gate["why"])

    def test_ranks_are_not_renumbered(self):
        """A withheld manager leaves a gap; the survivors keep their rank.

        Renumbering would tell the manager below FantasyticBeast that they
        were rank 3 of the indexed sample when they were rank 4. The gap is
        true; the renumber is not.
        """
        before = {str(r["manager_id"]): r["rank"]
                  for r in self.corpus["leaderboard"]}
        # Retain evidence for a handful of leagues, so the result has gaps.
        keep_rows = [r for r in self.corpus["leaderboard"][:40]
                     if r.get("leagues")][:5]
        details = {}
        for r in keep_rows:
            lid = str(r["leagues"][0]["league_id"])
            details[lid] = synth_detail(lid, [str(r["manager_id"])])

        md.apply_evidence_gate(self.corpus, details)
        shown = self.corpus["leaderboard"]

        self.assertTrue(shown, "the fixture should leave some rows")
        for r in shown:
            self.assertEqual(
                r["rank"], before[str(r["manager_id"])],
                "rank must be the rank among ALL scored managers")
        ranks = [r["rank"] for r in shown]
        self.assertEqual(ranks, sorted(ranks), "order is preserved")

    def test_empty_detail_store_fails_open_and_says_so(self):
        """No evidence for anybody is a different fact from no evidence for one.

        A bare checkout and a CI cache miss both produce an empty store.
        Blanking a 2,302-row board in that case and calling it a quality
        feature would be worse than the bug.
        """
        n_before = len(self.corpus["leaderboard"])
        gate = md.apply_evidence_gate(self.corpus, {})

        self.assertFalse(gate["applied"])
        self.assertEqual(len(self.corpus["leaderboard"]), n_before,
                         "an empty store must not filter the board")
        self.assertEqual(gate["n_withheld"], 0)
        self.assertIn("no per-league audits", gate["why"])

    # --------------------------------------------------- no silent drift
    def test_evidence_gate_agrees_with_build_manager_detail(self):
        """The gate and the drill-down must never disagree.

        ``managers_with_evidence`` is a cheap index; ``build_manager_detail``
        is the real assembler. If the two diverge, the board either shows a
        row whose panel is empty or hides a row that had evidence -- both
        are the bug this feature exists to remove. Asserted over the whole
        real corpus rather than a sample.
        """
        corpus = load_corpus()
        rows = corpus["leaderboard"]
        # A store covering every other league, so the two implementations
        # are exercised across both verdicts rather than all-true.
        details = {}
        for i, r in enumerate(rows):
            for lg in (r.get("leagues") or [])[:1]:
                if i % 2 == 0:
                    lid = str(lg["league_id"])
                    details[lid] = synth_detail(lid, [str(r["manager_id"])])

        cheap = md.managers_with_evidence(corpus, details)

        disagreements = []
        for r in rows:
            doc = md.build_manager_detail(r, details, corpus)
            real = bool(doc["coverage"]["n_leagues_with_evidence"])
            if real != (str(r["manager_id"]) in cheap):
                disagreements.append(str(r.get("display_name")))
        self.assertEqual(
            disagreements[:10], [],
            f"{len(disagreements)} managers where the gate and the "
            "drill-down disagree")

    def test_committed_corpus_is_never_mutated_on_disk(self):
        """The gate runs on the published copy only.

        Gating the crawler's resumption state would drop a manager
        permanently the first time their detail aged out of the cache.
        """
        raw_before = CORPUS_PATH.read_bytes()
        corpus = load_corpus()
        md.apply_evidence_gate(corpus, {"x": synth_detail("x", ["1"])})
        self.assertEqual(CORPUS_PATH.read_bytes(), raw_before)


def gate_without_log(gate: dict) -> dict:
    """The gate block as attached to the corpus (no build-log-only keys)."""
    return {k: v for k, v in gate.items() if k != "withheld_sample"}


if __name__ == "__main__":
    unittest.main()
