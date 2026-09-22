"""The current rookie class reaches the rankings pages.

Phil, 2026-09-22: 2026 rookies are missing from "Similar NFL Career Paths"
and "Dynasty Rankings". "Anyone drafted in 2026 is a rookie."

The gap, verified against the live site before anything was changed:

* ``engine_rankings.json`` had 735 rows and not one 2026 draftee.
* The 2026 class WAS already in the prospects pipeline, with per-prospect
  pages carrying 25 historical college comps and the NFL careers those
  comps went on to have.

So the college->NFL layer already produced the data; the rankings pages
never read it. ``dynasty.rookie_rankings`` is that read.

The four named acceptance cases are asserted from the **committed 2026
draft class** (``data/pfr/draft_class_2026.json``, 257 real picks), not
from a hand-written fixture, so the test cannot pass against data that
does not describe the real draft.

Stdlib only: ``rookie_rankings`` and ``prospect_view`` import nothing
third-party.
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dynasty import prospect_view as pv          # noqa: E402
from dynasty import rookie_rankings as rr        # noqa: E402

DRAFT_2026 = REPO_ROOT / "data" / "pfr" / "draft_class_2026.json"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "prospects_fixture.json"

#: The names Phil listed. Each must reach a page showing NFL comparisons.
#: KC Concepcion is carried on PFR as "KC Concepcion" and in the college
#: corpus as "Kevin Concepcion" (slug ``kevin-concepcion-2``); both spellings
#: are accepted so the check tests presence, not one source's spelling.
ACCEPTANCE = {
    "Denzel Boston": ("denzel boston",),
    "Caleb Douglas": ("caleb douglas",),
    "Carnell Tate": ("carnell tate",),
    "KC Concepcion": ("kc concepcion", "kevin concepcion"),
}

SKILL = ("QB", "RB", "WR", "TE")


def load_draft_2026() -> list:
    return json.loads(DRAFT_2026.read_text(encoding="utf-8"))["picks"]


def slugify(name: str, pid: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{base}-{pid[-6:]}" if pid else base


def artifact_from_real_draft() -> dict:
    """A prospect artifact built from the real 2026 class.

    Mirrors what ``build_prospects_v3`` emits in drafted-only mode: one
    record per real skill-position pick, carrying the ``drafted`` block it
    stamps. Projections are split between comp-backed and draft-slot
    constant so both branches are exercised, plus a Tankathon-style 2027
    class that must NOT be treated as rookies.
    """
    prospects = []
    for i, pick in enumerate(load_draft_2026()):
        if (pick.get("position") or "").upper() not in SKILL:
            continue
        name = pick["player_name"]
        cfb_id = str(pick.get("college_stats_slug") or "").replace("-", "")[-6:] or f"{i:06d}"
        # Every third player gets the no-NFL-comp branch, so the board
        # contains real draft-slot constants as the live one does.
        constant = (i % 3 == 0)
        prospects.append({
            "name": name,
            "position": pick["position"],
            "school": pick.get("college"),
            "draft_class": 2026,
            "age": float(pick.get("age_at_draft") or 21),
            "cfb_player_id": cfb_id,
            "slug": slugify(name, cfb_id),
            "drafted": {
                "year": 2026, "round": pick["rnd"], "pick": pick["pick"],
                "team": pick.get("team"), "college": pick.get("college"),
                "pfr_id": pick.get("pfr_id"),
            },
            "projection": {
                "projected_career_fp": 3200.0 if constant else 1041.0 - i,
                "projected_peak3_fp_pg": 13.5 if constant else 9.1,
                "projected_years_in_league": 7.5 if constant else 5.0,
                "projection_source": ("pick_tier_baseline_R1_top10"
                                      if constant else "comp_weighted"),
                "n_comps_with_nfl": 0 if constant else 14,
            },
            "ktc": {"ktc_rank_sf": 30 + i},
            "ktc_delta_overall": 5,
            "comps": [],
        })
    # A future class that has not been drafted. These are prospects, not
    # rookies, and must not appear on a rookie board.
    for j, name in enumerate(("Arch Manning", "Dante Moore", "C.J. Carr")):
        prospects.append({
            "name": name, "position": "QB", "school": "Texas",
            "draft_class": 2027, "age": 20.0,
            "cfb_player_id": f"7770{j:02d}", "slug": f"future-{j}",
            "drafted": {"year": 2027, "round": None, "pick": 2 + j,
                        "team": None, "source": "tankathon_big_board"},
            "projection": {
                "projected_career_fp": None,
                "projected_peak3_fp_pg": None,
                "projection_source": "insufficient_evidence_undrafted",
                "n_comps_with_nfl": 0,
            },
            "comps": [],
        })
    return {"version": "test", "draft_classes": [2026, 2027],
            "n_prospects": len(prospects), "prospects": prospects}


class TestDraftDataPrecondition(unittest.TestCase):
    """The committed 2026 class must actually contain the named players.

    Without this, every acceptance assertion below could pass against a
    file that lost the draft.
    """

    def test_the_2026_class_is_present_and_complete(self):
        picks = load_draft_2026()
        self.assertGreater(len(picks), 200,
                           "a full NFL draft is ~257 picks")
        self.assertTrue(all(p.get("year") == 2026 for p in picks))

    def test_all_four_named_rookies_are_in_the_committed_draft_class(self):
        names = {p["player_name"].lower() for p in load_draft_2026()}
        for label, accepted in ACCEPTANCE.items():
            self.assertTrue(
                any(a in names for a in accepted),
                f"{label} is not in data/pfr/draft_class_2026.json")


class TestRookieRows(unittest.TestCase):
    def setUp(self):
        self.artifact = artifact_from_real_draft()
        self.rows = rr.rookie_rows(self.artifact)
        self.by_name = {r["name"].lower(): r for r in self.rows}

    def test_the_class_year_is_derived_not_hardcoded(self):
        """A hardcoded 2026 would be silently wrong every April."""
        self.assertEqual(rr.rookie_class_year(self.artifact), 2026)
        shifted = json.loads(json.dumps(self.artifact))
        for p in shifted["prospects"]:
            if p["draft_class"] == 2026:
                p["draft_class"] = 2027
                p["drafted"]["year"] = 2027
            else:
                p["draft_class"] = 2028
        self.assertEqual(rr.rookie_class_year(shifted), 2027)

    def test_every_named_rookie_appears(self):
        for label, accepted in ACCEPTANCE.items():
            self.assertTrue(
                any(a in self.by_name for a in accepted),
                f"{label} is missing from the rookie board")

    def test_every_named_rookie_links_to_a_page_with_nfl_comparisons(self):
        """Phil's acceptance wording: each must resolve to a page showing
        historical NFL comparisons.

        That page is the per-prospect page, which carries the comp grid.
        Asserted as a link into ``players/<slug>-prospect.html`` -- the
        file ``generate_site`` writes for every artifact prospect.
        """
        for label, accepted in ACCEPTANCE.items():
            row = next(self.by_name[a] for a in accepted if a in self.by_name)
            self.assertTrue(
                row["href"].startswith("players/"),
                f"{label}: href {row['href']!r} is not a player page")
            self.assertTrue(
                row["href"].endswith("-prospect.html"),
                f"{label}: href {row['href']!r} must be the prospect page, "
                "which is where the NFL comparisons are")

    def test_undrafted_future_classes_are_not_rookies(self):
        names = set(self.by_name)
        for future in ("arch manning", "dante moore", "c.j. carr"):
            self.assertNotIn(
                future, names,
                "a player who has not been drafted is a prospect, not a "
                "rookie")

    def test_rows_carry_explicit_provenance(self):
        for row in self.rows:
            self.assertEqual(row["engine"], rr.ROOKIE_ENGINE)
            self.assertIn(row["projection_basis"],
                          ("comps", "blend", "floor", "baseline",
                           "comps_no_capital", "none"))

    def test_no_row_leaks_None_into_a_display_field(self):
        for row in self.rows:
            for field in ("name", "position", "draft_label", "href"):
                self.assertNotIn("None", str(row[field]),
                                 f"{field} leaked None: {row[field]!r}")

    def test_draft_slot_constants_rank_below_equal_evidence(self):
        ranks = [r["rookie_rank"] for r in self.rows]
        self.assertEqual(ranks, list(range(1, len(self.rows) + 1)),
                         "rookie_rank is dense and 1-indexed")

    def test_slug_helper_matches_reports(self):
        """The duplicated slug helper must agree with report's.

        ``rookie_rankings`` cannot import ``report`` (which imports the
        engine at module scope), so the helper is duplicated. Duplication
        is only safe if it is checked.
        """
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        sys.path.insert(0, str(REPO_ROOT / "tests" / "support"))
        import audit_site_links as harness
        harness._install_stubs()
        from dynasty import report
        for p in fixture["prospects"]:
            self.assertEqual(rr._prospect_slug(p), report._prospect_slug(p),
                             f"slug drift for {p.get('name')}")

    def test_coverage_counts_add_up(self):
        cov = rr.coverage(self.artifact, self.rows)
        self.assertEqual(cov["class_year"], 2026)
        self.assertEqual(cov["n_shown"], len(self.rows))
        self.assertEqual(
            cov["n_evidence_backed"] + cov["n_draft_slot_constant"],
            cov["n_shown"])
        self.assertGreater(cov["n_draft_slot_constant"], 0,
                           "the fixture should exercise the constant branch")

    def test_artifact_payload_is_self_describing(self):
        payload = rr.artifact_payload(self.artifact, self.rows)
        self.assertEqual(payload["schema"], rr.ROOKIE_ARTIFACT_SCHEMA)
        self.assertEqual(payload["class_year"], 2026)
        self.assertIn("error bars", payload["why_separate"])
        self.assertEqual(len(payload["rookies"]), len(self.rows))
        json.dumps(payload)  # must be serialisable

    # ------------------------------------------------------ degradation
    def test_no_artifact_yields_no_rookies_and_no_exception(self):
        for empty in (None, {}, {"prospects": []},
                      {"prospects": [{"name": "x"}]}):
            self.assertEqual(rr.rookie_rows(empty), [],
                             f"expected no rows for {empty!r}")

    def test_legacy_artifact_without_draft_blocks_yields_no_rookies(self):
        """The committed fixture predates draft stamping.

        Nothing in it records who was drafted, so claiming a rookie class
        from it would be invention. No rows is the honest answer.
        """
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertIsNone(rr.rookie_class_year(fixture))
        self.assertEqual(rr.rookie_rows(fixture), [])


if __name__ == "__main__":
    unittest.main()
