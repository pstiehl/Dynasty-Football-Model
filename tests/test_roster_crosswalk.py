"""Tests for the roster_index.json -> engine_rankings.json join repair.

Every case here is modelled on a defect measured in the live artifacts of
2026-09-21, where only 170 of 735 ranking rows were reachable from any
Sleeper id. Two independent causes, both reproduced below:

* 866 crosswalk rows stored the gsis id with a leading space, so a string
  join failed on ids that were actually identical (Kyler Murray, A.J.
  Brown, DK Metcalf, Terry McLaurin, ...).
* 8,303 rows carried no gsis id at all (Jalen Hurts, Jahmyr Gibbs, Bijan
  Robinson, ...).

No network, no DB.
"""
from __future__ import annotations

from dynasty.roster_crosswalk import (
    GSIS,
    backfill,
    index_rankings_by_name,
    resolve_gsis,
    unreachable_rankings,
)


RANKINGS = [
    {"player_id": "00-0036389", "name": "Jalen Hurts", "position": "QB",
     "overall_rank": 2},
    {"player_id": "00-0035228", "name": "Kyler Murray", "position": "QB",
     "overall_rank": 12},
    {"player_id": "00-0037746", "name": "Kenneth Walker III", "position": "RB",
     "overall_rank": 48},
    # Same-name, same-position collision: must never be guessed.
    {"player_id": "00-0090001", "name": "Chris Olave", "position": "WR",
     "overall_rank": 70},
    {"player_id": "00-0090002", "name": "Chris Olave", "position": "WR",
     "overall_rank": 300},
    # Same name, different positions: position separates them.
    {"player_id": "00-0034857", "name": "Josh Allen", "position": "QB",
     "overall_rank": 1},
    {"player_id": "00-0035313", "name": "Josh Allen", "position": "LB",
     "overall_rank": 400},
]


def crosswalk():
    return {
        # leading space -- the whitespace defect
        "5849": ["Kyler Murray", "QB", "ARI", " 00-0035228"],
        # empty -- the backfill case
        "6904": ["Jalen Hurts", "QB", "PHI", ""],
        # suffix mismatch against the engine's name
        "4984": ["Kenneth Walker", "RB", "SEA", ""],
        # unresolvable collision
        "5003": ["Chris Olave", "WR", "NO", ""],
        # resolvable by position
        "6905": ["Josh Allen", "QB", "BUF", ""],
        # already correct: must be left exactly as-is
        "7564": ["Ja'Marr Chase", "WR", "CIN", "00-0036900"],
        # no engine row under any form of this name
        "9999": ["Nobody Atall", "WR", "FA", ""],
    }


# --------------------------------------------------------------------------
# Whitespace
# --------------------------------------------------------------------------

def test_leading_space_is_trimmed_and_counted():
    cw = crosswalk()
    stats = backfill(cw, RANKINGS)
    assert cw["5849"][GSIS] == "00-0035228"
    assert stats["gsis_trimmed"] == 1


def test_trimmed_id_actually_joins():
    """The point of the trim: the row becomes reachable."""
    cw = crosswalk()
    backfill(cw, RANKINGS)
    ranked = {r["player_id"] for r in RANKINGS}
    assert cw["5849"][GSIS] in ranked


def test_whitespace_only_id_is_treated_as_empty_and_backfilled():
    cw = {"6904": ["Jalen Hurts", "QB", "PHI", "   "]}
    backfill(cw, RANKINGS)
    assert cw["6904"][GSIS] == "00-0036389"


def test_trim_is_not_counted_as_a_backfill():
    cw = {"5849": ["Kyler Murray", "QB", "ARI", " 00-0035228"]}
    stats = backfill(cw, RANKINGS)
    assert stats["gsis_trimmed"] == 1
    assert stats["gsis_backfilled"] == 0


# --------------------------------------------------------------------------
# Name backfill
# --------------------------------------------------------------------------

def test_empty_gsis_is_filled_from_a_unique_name():
    cw = crosswalk()
    backfill(cw, RANKINGS)
    assert cw["6904"][GSIS] == "00-0036389"


def test_generational_suffix_folds_to_one_key():
    """Crosswalk says "Kenneth Walker", engine says "Kenneth Walker III"."""
    cw = crosswalk()
    backfill(cw, RANKINGS)
    assert cw["4984"][GSIS] == "00-0037746"


def test_collision_at_the_same_position_is_refused():
    cw = crosswalk()
    stats = backfill(cw, RANKINGS)
    assert cw["5003"][GSIS] == ""
    assert stats["gsis_ambiguous"] == 1


def test_collision_is_resolved_by_position():
    cw = crosswalk()
    backfill(cw, RANKINGS)
    assert cw["6905"][GSIS] == "00-0034857"      # the QB, not the LB


def test_existing_id_is_never_replaced():
    cw = crosswalk()
    backfill(cw, RANKINGS)
    assert cw["7564"][GSIS] == "00-0036900"


def test_unmatchable_name_is_left_empty():
    cw = crosswalk()
    stats = backfill(cw, RANKINGS)
    assert cw["9999"][GSIS] == ""
    assert stats["gsis_missing"] >= 1


# --------------------------------------------------------------------------
# Accounting
# --------------------------------------------------------------------------

def test_every_entry_lands_in_exactly_one_bucket():
    cw = crosswalk()
    stats = backfill(cw, RANKINGS)
    buckets = (stats["gsis_present"] + stats["gsis_backfilled"]
               + stats["gsis_ambiguous"] + stats["gsis_missing"])
    assert stats["entries"] == len(cw) == buckets


def test_reachability_is_reported():
    cw = crosswalk()
    stats = backfill(cw, RANKINGS)
    assert stats["rankings_total"] == len(RANKINGS)
    assert stats["rankings_reachable"] + stats["rankings_unreachable"] \
        == stats["rankings_total"]
    # Hurts, Murray, Walker, Josh Allen QB, Chase-is-not-in-RANKINGS...
    assert stats["rankings_reachable"] >= 4


def test_unreachable_rankings_lists_best_first():
    cw = crosswalk()
    backfill(cw, RANKINGS)
    missing = unreachable_rankings(cw, RANKINGS)
    ranks = [r["overall_rank"] for r in missing]
    assert ranks == sorted(ranks)
    # The refused collision leaves both Olave rows unreachable.
    assert {r["name"] for r in missing} >= {"Chris Olave"}


# --------------------------------------------------------------------------
# Robustness
# --------------------------------------------------------------------------

def test_malformed_row_does_not_raise():
    cw = {"a": ["Short", "WR"], "b": None, "c": "nonsense"}
    stats = backfill(cw, RANKINGS)
    assert stats["gsis_missing"] == 3


def test_empty_inputs_are_safe():
    assert backfill({}, [])["entries"] == 0
    assert index_rankings_by_name([]) == {}
    assert unreachable_rankings({}, []) == []


def test_resolve_gsis_reports_its_outcome():
    by_name = index_rankings_by_name(RANKINGS)
    assert resolve_gsis("Jalen Hurts", "QB", by_name) == ("00-0036389", "matched")
    assert resolve_gsis("Chris Olave", "WR", by_name)[1] == "ambiguous"
    assert resolve_gsis("Nobody Atall", "WR", by_name)[1] == "no_candidate"
    assert resolve_gsis("", "WR", by_name)[1] == "no_candidate"
