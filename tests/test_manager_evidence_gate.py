"""Only managers whose score can be explained appear on the board.

Phil, 2026-09-22: "I only want to display managers where you can see the
evidence of their draft, waiver wire, trade data ... for example you cannot
see the data for this manager: FantasyticBeast. Users like that should not
be included if we cannot see what is driving the manager score in the drill
down."

The condition is real and reproducible from the committed corpus:
``FantasyticBeast`` sits at rank 4 with a score of 118.7 off a single
league. The score is correct -- 20 scored picks, z = 2.16 on the draft
component -- but the pick-level audit behind it USED TO live only in the
build cache (``data/cross_league/detail/``, then gitignored), so on a build
where that league was not re-scored the row opened an empty panel.

That store is now committed, for the reason set out in ``.gitignore``: the
gate alone turned a 2,432-manager board into an empty one, because a fresh
checkout had no audits to explain anybody with. Whether a clone carries
audits at all is asserted in ``test_published_artifacts.CommittedAuditStore``,
against the real committed files.

What this file can and cannot check, stated plainly
---------------------------------------------------
This file drives the gate to CHOSEN states -- "this manager is audited,
this one is not" -- which real data cannot be made to do on demand. So it
asserts the gate's behaviour against the **real committed corpus** with a
detail store constructed to match each case:

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

    ``picks`` carries one scored row per manager. The gate requires an
    audit to CONTAIN a scored transaction, not merely to exist (see
    ``managers_with_scored_transactions``): a retained audit listing zero
    picks, trades and waivers describes a seat that did nothing scoreable,
    and publishing it gives a visitor a row that opens on an empty table.
    An all-empty fixture would therefore exercise the withhold path while
    claiming to test the publish path. ``synth_detail_empty`` below is the
    fixture for that state, tested explicitly.
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
                "picks": [{"season": "2024", "slot": 1,
                           "player": "Fixture Player", "surplus": 1.0}],
                "trades": [],
                "trades_unscored": [],
                "waivers": [],
            }
            for mid in manager_ids
        ],
    }


def synth_detail_empty(league_id: str, manager_ids) -> dict:
    """A retained audit that truthfully records no scored transactions."""
    d = synth_detail(league_id, manager_ids)
    for m in d["managers"]:
        m["picks"] = []
    return d


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

    def test_empty_detail_store_fails_closed_and_says_so(self):
        """An empty store withholds everything. It used to show everything.

        This test previously asserted the opposite, on the reasoning that
        "no evidence for anybody" is a different fact from "no evidence for
        this manager" and should not blank the board. The distinction is
        real; the published consequence was not. At the time the detail
        store was empty on every build (data/cross_league/detail/ was
        gitignored and only ever existed in the CI cache), so the fail-open
        branch was not an edge case -- it was the only branch that ever
        ran, and it shipped 2,432 managers with no retrievable evidence.

        The store is committed now, so this state should no longer occur in
        a normal build. The fail-closed behaviour still has to hold: it is
        what makes a future loss of the audits show up as an honest empty
        board with a published reason, instead of silently reverting to
        thousands of unexplainable rows.

        Fail-closed: no rows, and the reason published rather than a
        silently short board.
        """
        n_before = len(self.corpus["leaderboard"])
        n_draft_before = len(self.corpus["draft_board"])
        gate = md.apply_evidence_gate(self.corpus, {})

        self.assertTrue(gate["applied"])
        self.assertEqual(gate["reason"], "audit_data_unavailable")
        self.assertEqual(self.corpus["leaderboard"], [],
                         "an empty store must withhold every row")
        self.assertEqual(self.corpus["draft_board"], [])
        self.assertEqual(gate["n_withheld"], n_before)
        self.assertEqual(gate["n_draft_withheld"], n_draft_before)
        self.assertEqual(gate["n_shown"], 0)
        # The count is published, not swallowed.
        self.assertEqual(gate["n_scored"], n_before)
        self.assertIn("audit data unavailable", gate["why"])
        self.assertFalse(self.corpus["manager_detail"]["available"])

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

    def test_retained_audit_with_no_transactions_is_withheld(self):
        """A truthful audit of nothing is still an empty drill-down.

        Measured on the 2026-09-22 build: 11 of 186 otherwise-passing
        managers had n = 0 on draft, trade AND waiver and composite 0.0.
        Their audits were retained and correct -- nothing scoreable
        happened -- so the every-league-audited predicate passed them. The
        row a visitor clicks still opened on an empty table, which is the
        complaint that started this work, and a "Best Managers" board has
        no business ranking a seat with nothing behind it.

        Note what is NOT asserted: that their score is wrong. It is 0.0 and
        it is right. Only the row is withheld.
        """
        corpus = load_corpus()
        row = find_row(corpus, PHIL_CASE)
        self.assertIsNotNone(row)
        mid = str(row["manager_id"])
        leagues = [str(lg["league_id"]) for lg in row["leagues"]]

        empty = {lid: synth_detail_empty(lid, [mid]) for lid in leagues}
        md.apply_evidence_gate(corpus, empty)
        self.assertNotIn(
            PHIL_CASE, names(corpus["leaderboard"]),
            "an audit containing no scored transactions must not put a "
            "manager on the board")

        # Same manager, same leagues, one scored pick -> listed. This is
        # what proves the withhold above is caused by the empty audit and
        # not by something incidental to the fixture.
        corpus = load_corpus()
        full = {lid: synth_detail(lid, [mid]) for lid in leagues}
        md.apply_evidence_gate(corpus, full)
        self.assertIn(PHIL_CASE, names(corpus["leaderboard"]))


def gate_without_log(gate: dict) -> dict:
    """The gate block as attached to the corpus (no build-log-only keys)."""
    return {k: v for k, v in gate.items() if k != "withheld_sample"}


if __name__ == "__main__":
    unittest.main()
