"""v3.11 — vet-as-rookie fix + sample-confidence comp-pool floor.

Phil's 2026-06-03 brief identified two bugs in the rookie engine's
cohort dispatcher and post-stack floor:

  BUG A — "Vet-as-rookie" misclassification.
    The cohort dispatcher only looked at the first season with
    >= MIN_GAMES_PER_SEASON=4 games. Vets whose actual rookie year
    had < 4 games (practice-squad call-up, short stints) and whose
    first >=4G season fell years later were misclassified as rookies.
    Tyrell Shavers (nflverse rookie_season=2023, first >=4G=2025)
    was being routed through the rookie engine and comped against
    the 2024-25 draft classes — completely wrong.

  BUG B — comp_pool_floor needs a sample-confidence gate.
    The post-stack comp-pool floor (in similarity_v1.py) ignored the
    rookie's games-played confidence factor. Bub Means (4 games in
    2024) was being anchored to the FULL comp-pool floor despite
    having only 4 games of signal.

These tests pin Phil's acceptance criteria:

  * Tyrell Shavers drops out of the rookie engine (routed to vet).
  * All 13 other vet-as-rookie misclassifications (Tonges, Tyler Badie,
    Zavier Scott, Brycen Tremayne, Mitchell Tinsley, John FitzPatrick,
    Tanner McKee, Cameron Latu, Evan Hull, Sincere McCormick, Kenny
    Yeboah, Jacob Saylors, Dylan Drummond) also leave the rookie engine.
  * Bub Means stays in the rookie engine (his actual rookie year IS
    2024, in-window) but his production_score drops below 200 because
    his 4-game confidence (0.5) scales the comp-pool floor down.
"""

import pytest

from dynasty.engine.similarity_v1 import run_engine


@pytest.fixture(scope="module")
def engine():
    return run_engine(current_season=2025, persist=False)


def _row(engine, name):
    for r in engine.rankings:
        if r["name"].lower() == name.lower():
            return r
    return None


# ---------------------------------------------------------------------------
# Bug A — vet-as-rookie fix
# ---------------------------------------------------------------------------

def test_tyrell_shavers_not_in_rookie_engine(engine):
    """v3.11 Bug A: Shavers (actual rookie 2023, first >=4G season
    2025) must be routed to the veteran engine, not comped against
    2024-25 draft classes."""
    r = _row(engine, "Tyrell Shavers")
    if r is None:
        pytest.skip("Shavers not in rankings")
    assert r["engine"] != "rookie_nfl_fp_arc", (
        f"Shavers should be in the veteran engine, got {r['engine']}"
    )


@pytest.mark.parametrize("name", [
    "Jake Tonges",
    "Zavier Scott",
    "Brycen Tremayne",
    "Tyler Badie",
    "Mitchell Tinsley",
    "John FitzPatrick",
    "Tanner McKee",
    "Cameron Latu",
    "Evan Hull",
    "Sincere McCormick",
    "Kenny Yeboah",
    "Jacob Saylors",
    "Dylan Drummond",
])
def test_other_vet_as_rookie_misclassifications_routed_to_vet(engine, name):
    """v3.11 Bug A: each of the 13 other players whose v3.10 model
    rookie year (= first >=4G season) was 2+ years after their actual
    nflverse rookie_season should now be in the veteran engine."""
    r = _row(engine, name)
    if r is None:
        pytest.skip(f"{name} not in rankings")
    assert r["engine"] != "rookie_nfl_fp_arc", (
        f"{name} should be in the veteran engine (actual rookie year "
        f"predates 2024), got {r['engine']}"
    )


# ---------------------------------------------------------------------------
# Bug B — sample-confidence comp-pool floor gate
# ---------------------------------------------------------------------------

def test_bub_means_production_score_below_200(engine):
    """v3.11 Bug B: Bub Means (4 games in 2024,
    rookie_confidence_factor=0.5) must have his post-stack comp-pool
    floor scaled by his confidence factor. v3.10 had him at 345.8 with
    a full-confidence floor; v3.11 should drop him below 200."""
    r = _row(engine, "Bub Means")
    if r is None:
        pytest.skip("Bub Means not in rankings")
    # He stays in the rookie engine (actual rookie year IS 2024).
    assert r["engine"] == "rookie_nfl_fp_arc", (
        f"Bub Means' actual rookie year is 2024 — should still be in "
        f"the rookie engine, got {r['engine']}"
    )
    assert r["production_score"] < 200, (
        f"Bub Means production_score {r['production_score']} should be "
        f"< 200 after v3.11 confidence-gated floor (was 345.8 in v3.10)"
    )


def test_bub_means_confidence_scale_recorded(engine):
    """v3.11 Bug B: when the comp-pool floor wins, the confidence
    scale used to gate it should be recorded as a diagnostic."""
    r = _row(engine, "Bub Means")
    if r is None:
        pytest.skip("Bub Means not in rankings")
    if r.get("projection_path") != "rookie_comp_pool_floor":
        pytest.skip("Bub Means' floor didn't win in this run")
    cs = r.get("post_stack_comp_pool_confidence_scale")
    assert cs is not None and cs < 1.0 - 1e-6, (
        f"Bub Means post_stack_comp_pool_confidence_scale {cs} should "
        f"be recorded and < 1.0 (his rookie_confidence_factor is 0.5)"
    )


def test_full_season_rookie_floor_unchanged(engine):
    """v3.11 Bug B: a full-season rookie with rookie_confidence_factor
    >= 1.0 should see ~the same floor as v3.10 — the confidence gate
    multiplies by 1.0 and is a no-op. Ashton Jeanty (17 games in 2025)
    is the canonical example."""
    r = _row(engine, "Ashton Jeanty")
    if r is None:
        pytest.skip("Jeanty not in rankings")
    if r.get("projection_path") != "rookie_comp_pool_floor":
        pytest.skip("Jeanty floor didn't win")
    cs = r.get("post_stack_comp_pool_confidence_scale", 1.0)
    assert cs >= 1.0 - 1e-6, (
        f"Jeanty's confidence scale {cs} should be 1.0 (full season)"
    )


# ---------------------------------------------------------------------------
# Regression: real rookies must still be in the rookie engine
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "Travis Hunter",
    "Tetairoa McMillan",
    "Cam Ward",
    "Shedeur Sanders",
    "Omarion Hampton",
    "Quinshon Judkins",
    "Ashton Jeanty",
])
def test_real_rookies_still_in_rookie_engine(engine, name):
    """v3.11 Bug A: the cohort fix must not push genuine 2025 rookies
    out of the rookie engine. Sanity check: each of these had a 2025
    actual rookie year and one >=4G NFL season."""
    r = _row(engine, name)
    if r is None:
        pytest.skip(f"{name} not in rankings")
    assert r["engine"] == "rookie_nfl_fp_arc", (
        f"{name} (2025 rookie) should be in the rookie engine, got "
        f"{r['engine']}"
    )


# ---------------------------------------------------------------------------
# Regression: v3.8 invariants
# ---------------------------------------------------------------------------

def test_puka_still_ahead_of_nico(engine):
    """v3.8 Phil brief #1 — Puka > Nico. Must survive v3.11."""
    puka = _row(engine, "Puka Nacua")
    nico = _row(engine, "Nico Collins")
    if puka is None or nico is None:
        pytest.skip("Puka/Nico not both in rankings")
    assert puka["production_score"] > nico["production_score"]


def test_caleb_still_ahead_of_fields_and_watson(engine):
    """v3.8 Phil brief #2 — Caleb > Fields > Watson. Must survive."""
    caleb = _row(engine, "Caleb Williams")
    fields = _row(engine, "Justin Fields")
    watson = _row(engine, "Deshaun Watson")
    if any(r is None for r in (caleb, fields, watson)):
        pytest.skip("Trio not all in rankings")
    assert caleb["production_score"] > fields["production_score"]
    assert caleb["production_score"] > watson["production_score"]


def test_fannin_still_ahead_of_gadsden(engine):
    """v3.8 Phil brief #4 — Fannin > Gadsden. Must survive."""
    fannin = _row(engine, "Harold Fannin Jr.")
    gadsden = _row(engine, "Oronde Gadsden II")
    if fannin is None or gadsden is None:
        pytest.skip("Fannin/Gadsden not both in rankings")
    assert fannin["production_score"] > gadsden["production_score"]
