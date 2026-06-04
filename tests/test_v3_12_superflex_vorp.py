"""v3.12 \u2014 Superflex Positional VORP + Dynasty Rankings UI controls.

Phil's 2026-06-03 brief on the Dynasty Rankings tab:
  - Add a position filter dropdown (client-side; All/QB/RB/WR/TE).
  - Replace the "Consensus rank" sort with "Superflex Positional Value"
    (a VORP-style ranking against position replacement levels).
  - Emit a new field ``superflex_vorp_score`` into engine_rankings.json
    so league imports can consume it.

These tests pin:
  - The VORP module computes replacement levels for the 12-team SF
    starter counts (1QB+2RB+3WR+1TE+1SF+1.5Flex) we documented.
  - apply_superflex_vorp mutates rows in place and adds both
    ``superflex_vorp_score`` and ``superflex_vorp_replacement``.
  - Top VORP players make sense (QBs lead because SF inflates QB rep,
    but elite RBs/WRs still clear the top-10).
  - The generated Dynasty Rankings page contains the new controls and
    the new column headers (smoke test on the rendered HTML).
"""

import json
import os
from pathlib import Path

import pytest

from dynasty.engine.similarity_v1 import run_engine
from dynasty.engine.superflex_vorp import (
    SUPERFLEX_STARTERS,
    apply_superflex_vorp,
    compute_replacement_levels,
)


@pytest.fixture(scope="module")
def engine():
    return run_engine(current_season=2025, persist=False)


@pytest.fixture(scope="module")
def vorp_rankings(engine):
    rankings = [dict(r) for r in engine.rankings]
    apply_superflex_vorp(rankings)
    return rankings


def _row(rows, name):
    for r in rows:
        if r["name"] == name:
            return r
    return None


def _rank(rows, name, by="superflex_vorp_score"):
    ranked = sorted(
        (r for r in rows if r.get(by) is not None),
        key=lambda r: r[by],
        reverse=True,
    )
    for i, r in enumerate(ranked, 1):
        if r["name"] == name:
            return i
    return None


# ---------------------------------------------------------------------------
# Replacement-level computation
# ---------------------------------------------------------------------------

def test_replacement_levels_use_documented_starter_counts():
    """QB/RB/WR/TE starter counts match the docstring math (12-team
    SF: 1QB+2RB+3WR+1TE + 1SF (=QB) + 1.5 Flex split 1.0 RB / 0.5 WR)."""
    assert SUPERFLEX_STARTERS == {"QB": 24, "RB": 36, "WR": 42, "TE": 12}


def test_replacement_levels_returned_per_position(engine):
    """compute_replacement_levels returns a non-zero level for each of
    the four scoring positions."""
    levels = compute_replacement_levels(engine.rankings)
    for pos in ("QB", "RB", "WR", "TE"):
        assert pos in levels
        assert levels[pos] > 0, f"{pos} replacement level should be > 0"


def test_qb_replacement_higher_than_rb_in_superflex(engine):
    """Superflex inflates QB scarcity \u2014 the 24th QB scores more total
    fp than the 36th RB (raw production), so the QB replacement level
    should be higher than RB replacement. This is the entire point of
    VORP in SF: it normalises across positions."""
    levels = compute_replacement_levels(engine.rankings)
    # In a non-SF league this would not hold; this is the SF-specific
    # signal that QB scarcity is real.
    assert levels["QB"] > levels["RB"], (
        f"QB rep {levels['QB']} should exceed RB rep {levels['RB']} in SF"
    )


# ---------------------------------------------------------------------------
# apply_superflex_vorp mutation contract
# ---------------------------------------------------------------------------

def test_apply_adds_vorp_score_to_every_row(vorp_rankings):
    """Every QB/RB/WR/TE row must have a numeric superflex_vorp_score."""
    scoring_rows = [
        r for r in vorp_rankings
        if r.get("position") in SUPERFLEX_STARTERS
    ]
    assert scoring_rows, "engine must produce scoring-position rows"
    for r in scoring_rows:
        assert r.get("superflex_vorp_score") is not None, (
            f"{r['name']} ({r['position']}) missing superflex_vorp_score"
        )
        assert r.get("superflex_vorp_replacement") is not None


def test_vorp_score_equals_production_minus_replacement(vorp_rankings):
    """VORP definition pinned: score = production_score - replacement."""
    for r in vorp_rankings:
        score = r.get("superflex_vorp_score")
        rep = r.get("superflex_vorp_replacement")
        if score is None:
            continue
        expected = round(r["production_score"] - rep, 1)
        assert score == expected, (
            f"{r['name']} VORP {score} != production {r['production_score']} "
            f"- replacement {rep} = {expected}"
        )


# ---------------------------------------------------------------------------
# Top-VORP sanity checks
# ---------------------------------------------------------------------------

def test_top_vorp_is_elite_qb_or_rb(vorp_rankings):
    """The #1 VORP player should be an elite SF QB (Allen/Hurts tier)
    or an elite young RB (Gibbs tier). Sanity check the module isn't
    inverted or computing on the wrong field."""
    ranked = sorted(
        (r for r in vorp_rankings if r.get("superflex_vorp_score") is not None),
        key=lambda r: r["superflex_vorp_score"],
        reverse=True,
    )
    top = ranked[0]
    assert top["position"] in ("QB", "RB"), (
        f"Top VORP {top['name']} is {top['position']} \u2014 expected QB or RB"
    )


def test_josh_allen_top_5_by_vorp(vorp_rankings):
    """Allen should be in the top-5 by VORP in SF \u2014 he's #1 by raw
    production score (2105) and QB replacement is high enough that
    his lead survives."""
    rank = _rank(vorp_rankings, "Josh Allen")
    assert rank is not None and rank <= 5, (
        f"Allen VORP rank {rank} should be top-5 in SF"
    )


def test_gibbs_top_10_by_vorp(vorp_rankings):
    """Gibbs (elite young RB, ~1646 prod) should clear the top-10 in
    VORP rankings since RB replacement is low (~395)."""
    rank = _rank(vorp_rankings, "Jahmyr Gibbs")
    assert rank is not None and rank <= 10, (
        f"Gibbs VORP rank {rank} should be top-10 in SF"
    )


# ---------------------------------------------------------------------------
# Smoke test: rendered HTML carries the new controls + columns
# ---------------------------------------------------------------------------

def test_league_html_has_position_filter_and_vorp_sort(tmp_path):
    """The generated league.html must contain the new position filter
    dropdown and the Superflex VORP sort button."""
    from dynasty.report import generate_site
    eng = run_engine(current_season=2025, persist=False)
    os.environ["DFM_SKIP_CAREER_STATS"] = "1"
    out = generate_site(
        output_dir=str(tmp_path / "site"),
        league_format="sf_ppr",
        limit=50,
        engine=eng,
    )
    league_html = (Path(out) / "league.html").read_text(encoding="utf-8")
    assert 'id="ov-pos"' in league_html, "position filter dropdown missing"
    assert 'id="sort-vorp"' in league_html, "Superflex VORP sort button missing"
    assert "Superflex VORP" in league_html
    assert "VORP #" in league_html, "VORP rank column header missing"
    # Consensus rank stays as a display column (per Phil's spec).
    assert "Consensus #" in league_html


def test_engine_rankings_json_carries_vorp(tmp_path):
    """engine_rankings.json must emit superflex_vorp_score for league
    imports / downstream consumers."""
    from dynasty.report import generate_site
    eng = run_engine(current_season=2025, persist=False)
    os.environ["DFM_SKIP_CAREER_STATS"] = "1"
    out = generate_site(
        output_dir=str(tmp_path / "site"),
        league_format="sf_ppr",
        limit=50,
        engine=eng,
    )
    data = json.loads((Path(out) / "engine_rankings.json").read_text())
    n_with_vorp = sum(
        1 for r in data if r.get("superflex_vorp_score") is not None
    )
    assert n_with_vorp >= 100, (
        f"only {n_with_vorp} rows have superflex_vorp_score; expected >=100"
    )
