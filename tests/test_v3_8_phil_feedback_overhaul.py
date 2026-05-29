"""v3.8 — Phil Stiehl feedback overhaul (2026-05-29).

Validates the five fixes (A-E) Phil identified after reviewing v3.7:

  A. Heavy-injury / rate-aware partial-season penalty softening.
  B. Age-weighted runway boost (cumulative + rookie engines).
  C. Stats vs similarity weight rebalance (60/40 -> 40/60 toward
     peak-anchored).
  D. Comp-pool career-arc floor for rookies (in-engine + post-stack).
  E. Retired-early flag with extrapolation (Andrew Luck, Calvin Johnson).

Each test asserts the Phil-named example in his 2026-05-29 brief.
"""

import pytest

from dynasty.engine.similarity_v1 import run_engine
from dynasty.engine.fantasy_arc import load_retired_early_overrides
from dynasty.engine.v2_2_penalties import (
    compute_missed_recent_season,
    HEAVY_INJURY_FLOOR_MULTIPLIER,
)


@pytest.fixture(scope="module")
def engine():
    return run_engine(current_season=2025, persist=False)


def _row(engine, name):
    return next(
        (r for r in engine.rankings if r["name"] == name), None,
    )


def _rank(engine, name):
    for i, r in enumerate(engine.rankings, 1):
        if r["name"] == name:
            return i
    return None


# ---------------------------------------------------------------------------
# Fix A — partial-season penalty softening
# ---------------------------------------------------------------------------

def test_heavy_injury_floor_applies_under_8_games(engine):
    """v3.8 heavy-injury floor: rookies / vets with < 8 games in their
    most-recent season can't have their missed-season multiplier go
    below HEAVY_INJURY_FLOOR_MULTIPLIER (0.85). Malik Nabers played
    4 games in 2025 — the v3.7 multiplier would have been 0.759.
    v3.8 lifts the multiplier toward 0.85 because the absence is
    clearly injury, not benching."""
    nabers = _row(engine, "Malik Nabers")
    if nabers is None:
        pytest.skip("Nabers not in rankings")
    mult = nabers.get("missed_season_multiplier")
    assert mult is not None and mult >= HEAVY_INJURY_FLOOR_MULTIPLIER - 1e-6, (
        f"Nabers missed_season_multiplier {mult} should be >= "
        f"{HEAVY_INJURY_FLOOR_MULTIPLIER} (heavy-injury floor)"
    )


# ---------------------------------------------------------------------------
# Fix B + C — Caleb (24) > Fields (27) > Watson (30)
# ---------------------------------------------------------------------------

def test_caleb_williams_ahead_of_fields_and_watson(engine):
    """Phil's brief: Caleb Williams (24) should rank materially higher
    than Justin Fields (27) and Deshaun Watson (30) on age/runway +
    own stats grounds."""
    caleb_rank = _rank(engine, "Caleb Williams")
    fields_rank = _rank(engine, "Justin Fields")
    watson_rank = _rank(engine, "Deshaun Watson")
    assert all(r is not None for r in (caleb_rank, fields_rank, watson_rank))
    assert caleb_rank < fields_rank, (
        f"Caleb #{caleb_rank} should rank ahead of Fields #{fields_rank}"
    )
    assert caleb_rank < watson_rank, (
        f"Caleb #{caleb_rank} should rank ahead of Watson #{watson_rank}"
    )


# ---------------------------------------------------------------------------
# Fix D — Jeanty / Judkins comp-pool floor
# ---------------------------------------------------------------------------

def test_jeanty_projected_meaningful_fraction_of_comp_pool(engine):
    """Phil's brief #7: Ashton Jeanty's top comp is LaDainian Tomlinson
    (3,277 career fp). His projection should be a meaningful fraction of
    his comp pool's realised career fp \u2014 not the v3.7 670 fp collapse.
    v3.8 floors at 0.45 \u00d7 top-3 non-bust mean from the top-10 sim
    subset, rate-scaled."""
    jeanty = _row(engine, "Ashton Jeanty")
    if jeanty is None:
        pytest.skip("Jeanty not in rankings")
    # Should clear at least 800 fp (v3.7 was 673).
    assert jeanty["production_score"] >= 800, (
        f"Jeanty production_score {jeanty['production_score']} should "
        f"clear 800 under v3.8 comp-pool floor"
    )


def test_judkins_projected_meaningful_fraction_of_comp_pool(engine):
    """Phil's brief #8: Quinshon Judkins similarly should clear the
    survival multiplier collapse via the comp-pool floor."""
    judkins = _row(engine, "Quinshon Judkins")
    if judkins is None:
        pytest.skip("Judkins not in rankings")
    assert judkins["production_score"] >= 750, (
        f"Judkins production_score {judkins['production_score']} should "
        f"clear 750 under v3.8 comp-pool floor"
    )


# ---------------------------------------------------------------------------
# Fix E — Andrew Luck retired-early extrapolation
# ---------------------------------------------------------------------------

def test_retired_early_overrides_load():
    """The retired-early sidecar must load with Luck + Calvin Johnson."""
    ovr = load_retired_early_overrides()
    assert "00-0029668" in ovr, "Andrew Luck pid missing from sidecar"
    assert "00-0025389" in ovr, "Calvin Johnson pid missing from sidecar"
    assert ovr["00-0029668"]["position"] == "QB"
    assert ovr["00-0029668"]["extrapolate_to_age"] == 35


def test_andrew_luck_projection_extrapolated_in_caleb_comps(engine):
    """Andrew Luck appears as a comp for Caleb Williams. v3.8 should
    extrapolate Luck's truncated career (retired age 29) to age 35
    using his final-3yr fp/G rate. Luck's post_age_projected_pts in
    Caleb's comp list should reflect the extrapolated career, not the
    realised 2014-2018 only."""
    caleb = _row(engine, "Caleb Williams")
    if caleb is None:
        pytest.skip("Caleb not in rankings")
    comps = engine.comps.get(caleb["player_id"], [])
    luck = next((c for c in comps if c["name"] == "Andrew Luck"), None)
    if luck is None:
        pytest.skip("Luck not in Caleb's comp list")
    # Luck's realised career fp (career_ppr) is ~1700; extrapolated to
    # age 35 should push post_age_projected_pts well above that.
    assert luck["post_age_projected_pts"] > 2000, (
        f"Luck post_age {luck['post_age_projected_pts']} should be "
        f"> 2000 under v3.8 retired-early extrapolation"
    )


# ---------------------------------------------------------------------------
# Cross-cutting: Phil's Puka vs Nico, Fannin vs Gadsden
# ---------------------------------------------------------------------------

def test_puka_ahead_of_nico(engine):
    """Phil's brief #1: Puka Nacua (25, peak3yr ~23.5 fp/G) should rank
    ahead of Nico Collins (27, peak3yr ~16.5 fp/G)."""
    puka_rank = _rank(engine, "Puka Nacua")
    nico_rank = _rank(engine, "Nico Collins")
    assert all(r is not None for r in (puka_rank, nico_rank))
    assert puka_rank < nico_rank, (
        f"Puka #{puka_rank} should rank ahead of Nico #{nico_rank}"
    )


def test_fannin_ahead_of_gadsden(engine):
    """Phil's brief #4: Harold Fannin (21, 11.78 fp/G) should rank
    ahead of Oronde Gadsden II (22, 8.89 fp/G) \u2014 younger and stronger
    rookie production."""
    fannin_rank = _rank(engine, "Harold Fannin Jr.")
    gadsden_rank = _rank(engine, "Oronde Gadsden II")
    if fannin_rank is None or gadsden_rank is None:
        pytest.skip("TE pair not in rankings")
    assert fannin_rank < gadsden_rank, (
        f"Fannin #{fannin_rank} should rank ahead of "
        f"Gadsden #{gadsden_rank}"
    )


def test_shedeur_sanders_still_deep(engine):
    """v3.8 must NOT promote Shedeur Sanders out of the deep tier just
    because the comp-pool floor exists. The two-stage filter (top-10
    sim subset \u2192 top-3 by post-rookie fp) should exclude Matt Ryan /
    McNabb outliers that aren't strong similarity matches."""
    rank = _rank(engine, "Shedeur Sanders")
    assert rank is not None
    assert rank > 100, (
        f"Sanders v3.8 rank #{rank} \u2014 should remain deep (>100)"
    )
