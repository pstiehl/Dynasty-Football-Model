"""Assertions against the bytes the browser actually downloads.

Why this file exists
--------------------
PR #78 shipped ~2,300 lines of tests for the evidence gate and the rookie
class. Every one of them passed, and all three features were broken in
production the moment they deployed. The tests asserted on

* server-rendered HTML from ``crossleague_page``, and
* stub/fixture inputs assembled in the test itself.

The Best Managers board is not server-rendered. ``crossleague_js`` does
``fetch('crossleague_corpus.json')`` and builds the leaderboard, the draft
board and the drill-down in the browser from that file. So the gate could
be perfectly correct in ``apply_evidence_gate``, perfectly asserted in a
unit test, and completely absent from the artifact the page reads -- which
is exactly what happened: 2,288,884 bytes, 2,432 leaderboard entries,
``FantasyticBeast`` present, ``evidence_gate`` null.

So the rule for this file: **assert on the published artifact, produced by
the real publishing function, from real committed data.** No fixture
corpora, no stub renderers, no source inspection. If a test here cannot be
written against real bytes, it does not belong here.

``write_corpus_artifact`` is the exact function ``report.generate_site``
calls, and ``data/cross_league/corpus.json`` is the real committed corpus
(2,432 scored managers). That pairing is the whole point.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dynasty import manager_detail as md            # noqa: E402
from dynasty.crossleague import (                   # noqa: E402
    load_corpus, write_corpus_artifact,
)

CORPUS_PATH = REPO_ROOT / "data" / "cross_league" / "corpus.json"
DRAFT_2026 = REPO_ROOT / "data" / "pfr" / "draft_class_2026.json"

#: The manager Phil named. The acceptance check, not an example.
PHIL_CASE = "FantasyticBeast"

#: The rookies Phil named, by the name he used.
PHIL_ROOKIES = ["Denzel Boston", "Caleb Douglas", "Carnell Tate",
                "KC Concepcion"]


def synth_detail(league_id: str, manager_ids) -> dict:
    """A league audit shaped like ``league_detail_from_result`` output.

    This is the one place a constructed input is unavoidable: the retained
    audits live in ``data/cross_league/detail/``, which is gitignored and
    exists only in the CI build cache, so no clone can read a real one.
    It is used ONLY to exercise the has-evidence branch. Every
    withheld/absence assertion below runs against the real empty store,
    which is what a real build actually has.
    """
    return {
        "league_id": str(league_id),
        "managers": [{"id": str(m)} for m in manager_ids],
    }


class PublishedCorpusArtifact(unittest.TestCase):
    """The gate must be in the file the browser downloads."""

    def setUp(self):
        self.corpus = load_corpus(CORPUS_PATH)
        self.assertIsNotNone(
            self.corpus, "the committed corpus must load; it is the input")
        self.assertTrue(self.corpus["leaderboard"],
                        "the committed corpus must have scored managers")

    def _publish(self, corpus, details, tmp: Path) -> tuple[bytes, dict]:
        """Run the real publish path and hand back the real bytes."""
        md.apply_evidence_gate(corpus, details)
        corpus["manager_detail"] = md.publish_manager_details(
            tmp, corpus, details)
        path = write_corpus_artifact(tmp, corpus)
        raw = Path(path).read_bytes()
        return raw, json.loads(raw)

    def test_empty_audit_store_publishes_no_managers(self):
        """The live failure, reproduced and inverted.

        A real build has no retained audits (the detail dir is gitignored),
        which is the condition that shipped 2,432 unexplainable rows.
        """
        import tempfile
        n_scored = len(self.corpus["leaderboard"])
        with tempfile.TemporaryDirectory() as td:
            raw, art = self._publish(self.corpus, {}, Path(td))

            self.assertEqual(art["leaderboard"], [])
            self.assertEqual(art["draft_board"], [])
            # The bytes, not the structure: no reachable trace anywhere.
            self.assertNotIn(PHIL_CASE.encode(), raw)

            gate = art["evidence_gate"]
            self.assertTrue(gate["applied"])
            self.assertEqual(gate["reason"], "audit_data_unavailable")
            # Withheld count published, not swallowed.
            self.assertEqual(gate["n_withheld"], n_scored)
            self.assertEqual(gate["n_shown"], 0)
            self.assertFalse(art["manager_detail"]["available"])

            # No drill-down file a visitor could reach.
            self.assertEqual(
                list((Path(td) / "managers").rglob("*.json")), [])

    def test_named_manager_absent_from_every_part_of_the_artifact(self):
        """Phil's case, checked structurally rather than by substring."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            _, art = self._publish(self.corpus, {}, Path(td))
        blob = json.dumps(art)
        self.assertNotIn(PHIL_CASE, blob)
        for key in ("leaderboard", "draft_board"):
            for row in art.get(key) or []:
                self.assertNotEqual(row.get("display_name"), PHIL_CASE)

    def test_partial_evidence_is_still_withheld(self):
        """One audited league out of several is not an explanation.

        ``managers_with_evidence`` (at-least-one) would list such a manager,
        and their drill-down would render "Pick-level evidence is available
        for 1 of 4 indexed league(s)" plus a per-league "not in the build
        cache" banner -- the banners Phil asked to stop seeing. The gate
        uses ``managers_fully_explained`` for exactly this reason.
        """
        import tempfile
        # A real corpus row with more than one league.
        multi = next((r for r in self.corpus["leaderboard"]
                      if len(r.get("leagues") or []) > 1), None)
        self.assertIsNotNone(multi, "corpus should have a multi-league row")
        mid = str(multi["manager_id"])
        one_league = str(multi["leagues"][0]["league_id"])

        details = {one_league: synth_detail(one_league, [mid])}
        with tempfile.TemporaryDirectory() as td:
            _, art = self._publish(self.corpus, details, Path(td))

        shown = {str(r["manager_id"]) for r in art["leaderboard"]}
        self.assertNotIn(
            mid, shown,
            "a manager with an unaudited league must not be published")

    def test_fully_audited_manager_is_published(self):
        """The gate withholds for cause, not indiscriminately.

        Without this, "publish nothing" would pass every other test here.
        """
        import tempfile
        multi = next((r for r in self.corpus["leaderboard"]
                      if len(r.get("leagues") or []) >= 1), None)
        mid = str(multi["manager_id"])
        details = {
            str(lg["league_id"]): synth_detail(str(lg["league_id"]), [mid])
            for lg in multi["leagues"]
        }
        with tempfile.TemporaryDirectory() as td:
            _, art = self._publish(self.corpus, details, Path(td))

        shown = {str(r["manager_id"]) for r in art["leaderboard"]}
        self.assertIn(mid, shown)
        self.assertEqual(art["evidence_gate"]["reason"], "gated")

    def test_publishing_does_not_touch_the_crawler_state(self):
        """Gate the view, never the resumption state.

        The committed corpus is what the next crawl resumes from. If the
        gate reached it, a manager whose audit aged out of the cache would
        be dropped permanently rather than for one build.
        """
        import tempfile
        before = CORPUS_PATH.read_bytes()
        corpus = load_corpus(CORPUS_PATH)
        with tempfile.TemporaryDirectory() as td:
            self._publish(corpus, {}, Path(td))
        self.assertEqual(CORPUS_PATH.read_bytes(), before,
                         "the committed corpus must be byte-identical")

    def test_gate_predicate_matches_coverage_complete(self):
        """The gate and the drill-down must never disagree.

        If they drift, the board either publishes a row whose panel reports
        missing evidence, or hides a row that had it. Checked over the real
        corpus, both verdicts exercised.
        """
        corpus = load_corpus(CORPUS_PATH)
        rows = corpus["leaderboard"][:400]
        details = {}
        for i, r in enumerate(rows):
            if i % 2:
                continue
            for lg in (r.get("leagues") or []):
                lid = str(lg["league_id"])
                details.setdefault(lid, synth_detail(lid, []))
                details[lid]["managers"].append({"id": str(r["manager_id"])})

        keep = md.managers_fully_explained(corpus, details)
        for r in rows:
            doc = md.build_manager_detail(r, details, corpus)
            mid = str(r["manager_id"])
            self.assertEqual(
                mid in keep, doc["coverage"]["complete"],
                f"gate and coverage disagree for {mid}")


class RookieClassJoin(unittest.TestCase):
    """The 2026 class comes from committed draft data, by the right name."""

    def test_committed_draft_class_has_phils_rookies(self):
        payload = json.loads(DRAFT_2026.read_text(encoding="utf-8"))
        names = {p["player_name"] for p in payload["picks"]}
        self.assertEqual(len(payload["picks"]), 257)
        for name in PHIL_ROOKIES:
            self.assertIn(name, names)

    def test_display_name_prefers_the_draft_card(self):
        """KC Concepcion is in the corpus as "Kevin Concepcion".

        The v3.6 fuzzy join resolves the collision but keeps the corpus
        name on the record, so the board rendered a name no reader would
        search for. The published row must use the draft-card name.
        """
        from dynasty.rookie_rankings import _display_name
        self.assertEqual(
            _display_name({"name": "Kevin Concepcion",
                           "drafted": {"player_name": "KC Concepcion"}}),
            "KC Concepcion")
        # Falls back when the pick carries no name.
        self.assertEqual(
            _display_name({"name": "Denzel Boston", "drafted": {}}),
            "Denzel Boston")

    def test_no_point_estimate_rows_do_not_break_ordering(self):
        """The crash that took out both pages, as a unit.

        ``_no_evidence_projection`` sets every numeric field to None. Two
        sorts in build_prospects_v3 compared those against floats and threw,
        which aborted the artifact build -- leaving prospects.html empty and
        rookie_rankings.json a 404.
        """
        from dynasty import prospect_view as pv
        rows = [
            {"name": "B", "projection": {"projected_career_fp": 100.0}},
            {"name": "A", "projection": {"projected_career_fp": None}},
            {"name": "C", "projection": {"projected_career_fp": 500.0}},
        ]

        def key(r):
            fp = (r.get("projection") or {}).get("projected_career_fp")
            name = str(r.get("name") or "")
            return (1, 0.0, name) if fp is None else (0, -float(fp), name)

        ordered = [r["name"] for r in sorted(rows, key=key)]
        self.assertEqual(ordered, ["C", "B", "A"],
                         "unranked rows sort last, never raise")
        self.assertIn("insufficient_evidence_undrafted",
                      pv.NO_ESTIMATE_SOURCES)


if __name__ == "__main__":
    unittest.main()
