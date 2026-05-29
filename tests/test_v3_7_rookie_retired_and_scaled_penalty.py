"""v3.7 - Rookie retired-only comp pool + scaled missed-season penalty
(Phil 2026-05-28 round 5).

Phil's brief:

  > "JJ Mcarthy seems to be ranked much too high. His stats from his
  >  rookie season are pretty horrible. I think the jj mcarthy example
  >  is a sign of something bigger. i still think there a ton of
  >  historical players that are being omitted from the database for
  >  comparison."

  > "Elic Ayomanor also seems vastly overrated for this reason."

  > "similarly to the joe mixon example, Kyler Murray did not put up
  >  great stats in the last season in part due to injury."

Root cause for JJ McCarthy / Elic Ayomanor: the v3.5 retired-only filter
was applied to the cumulative-arc engine but NOT to the rookie engine.
JJ's comps were Trevor Lawrence (active 2026), Tua (active), Lamar
(active), Justin Fields (active), Brock Purdy (active), Mac Jones
(active), Sam Darnold (active), Drake Maye (active), Zach Wilson
(active) - 9 of his top 15 comps were still-active players with
truncated careers. The retired-only mandate should apply to the
rookie engine too.

Root cause for Kyler Murray: the v3.3 missed-season penalty had a
single 0.85 step for "<8 games played". Kyler played 5 of 17 (12
games missed to injury). 0.85 was too light a penalty for losing
~71% of his season. v3.7 scales the partial-season penalty linearly
by games-played fraction.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dynasty.engine.similarity_v1 import run_engine
from dynasty.engine.v2_2_penalties import (
    FULL_SEASON_GAMES,
    MISSED_FULL_SEASON_MULTIPLIER,
    MissedSeasonDiagnostics,
    PARTIAL_SEASON_GAME_THRESHOLD,
    PARTIAL_SEASON_MULTIPLIER,
    compute_missed_recent_season,
)
from dynasty.engine.fantasy_arc import CareerArc, SeasonArcPoint


# ---------------------------------------------------------------------------
# Unit tests for scaled missed-season penalty
# ---------------------------------------------------------------------------

def _make_arc(seasons):
    """Helper: build a minimal CareerArc with a list of (season, games)."""
    arc_seasons = [
        SeasonArcPoint(
            season=s, age=25, games=g, era=4,
            fp_per_game={"sf_ppr": 10.0},
        )
        for s, g in seasons
    ]
    return CareerArc(
        player_id="test",
        name="Test Player",
        position="QB",
        rookie_season=seasons[0][0] if seasons else None,
        last_season=seasons[-1][0] if seasons else None,
        retired=False,
        is_long_arc=True,
        career_arc=arc_seasons,
        peak_3yr_fp_per_game={"sf_ppr": 10.0},
        peak_season_fp_per_game={"sf_ppr": 10.0},
        career_avg_fp_per_game={"sf_ppr": 10.0},
        career_total_fp={"sf_ppr": 100.0},
    )


def test_scaled_partial_penalty_5_games():
    """v3.8 (Phil 2026-05-29): 5 of 17 games triggers the heavy-injury
    floor (< 8 games = clear injury). v3.7 produced 0.774; v3.8 floors
    at HEAVY_INJURY_FLOOR_MULTIPLIER = 0.85.
    """
    arc = _make_arc([(2023, 17), (2024, 17), (2025, 5)])
    out = compute_missed_recent_season(arc, corpus_last_season=2025)
    # v3.8: heavy-injury floor at 0.85 (was 0.774 in v3.7).
    assert abs(out.missed_season_multiplier - 0.85) < 0.01
    assert out.last_played_games == 5
    assert "5 of 17" in out.reason
    assert "heavy-injury floor" in out.reason


def test_scaled_partial_penalty_8_games():
    """8 of 17 games -> 0.818 (mid-scale)."""
    arc = _make_arc([(2024, 17), (2025, 8)])
    out = compute_missed_recent_season(arc, corpus_last_season=2025)
    # multiplier = 0.70 + (8/17)*(0.95-0.70) = 0.70 + 0.1176 = 0.8176
    assert abs(out.missed_season_multiplier - 0.818) < 0.01


def test_scaled_partial_penalty_13_games():
    """13 of 17 -> 0.891 (near full)."""
    arc = _make_arc([(2024, 17), (2025, 13)])
    out = compute_missed_recent_season(arc, corpus_last_season=2025)
    # multiplier = 0.70 + (13/17)*(0.95-0.70) = 0.70 + 0.191 = 0.891
    assert abs(out.missed_season_multiplier - 0.891) < 0.01


def test_scaled_partial_penalty_full_season_no_penalty():
    """14+ games played -> 1.0 (no penalty above PARTIAL_SEASON_GAME_THRESHOLD)."""
    for games in (14, 15, 17):
        arc = _make_arc([(2024, 17), (2025, games)])
        out = compute_missed_recent_season(arc, corpus_last_season=2025)
        assert out.missed_season_multiplier == 1.0


def test_full_missed_season_unchanged():
    """A player who didn't play at all in 2025 (Mixon-style) still\n    gets 0.70."""
    arc = _make_arc([(2023, 17), (2024, 14)])  # no 2025
    out = compute_missed_recent_season(arc, corpus_last_season=2025)
    assert out.missed_season_multiplier == MISSED_FULL_SEASON_MULTIPLIER


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def engine():
    return run_engine(current_season=2025, persist=False)


def test_kyler_murray_penalty_scales_with_partial_season(engine):
    """v3.8: Kyler at 5 games in 2025 triggers the heavy-injury floor
    (< 8 games = clear injury, not coaching/role). Floor at 0.85.
    v3.7 produced 0.77; v3.8 lifts to 0.85 because the absence is
    clearly injury-driven — the projection penalty shouldn't be as
    deep as a benching of similar games count."""
    kyler = next((r for r in engine.rankings if r["name"] == "Kyler Murray"), None)
    if kyler is None:
        pytest.skip("Kyler not in rankings")
    mult = kyler.get("missed_season_multiplier")
    games = kyler.get("missed_season_last_played_games")
    assert games == 5, f"Kyler 2025 games = {games}"
    # v3.8 heavy-injury floor: 5/17 games → 0.85.
    assert mult is not None and 0.83 < mult < 0.87, (
        f"Kyler missed_season_multiplier {mult} - v3.8 heavy-injury floor "
        f"should be ~0.85 for 5/17 games"
    )


def test_rookie_engine_corpus_excludes_active_players(engine):
    """Phil's JJ McCarthy worked example: the rookie engine's comp
    pool must not contain players whose last_season >= current - 1."""
    mccarthy = next((r for r in engine.rankings if r["name"] == "J.J. McCarthy"), None)
    if mccarthy is None:
        pytest.skip("J.J. McCarthy not in rankings")
    assert mccarthy.get("engine") == "rookie_nfl_fp_arc"
    comps = engine.comps.get(mccarthy["player_id"], [])
    active_leaks = [c for c in comps if (c.get("last_season") or 0) >= 2024]
    assert not active_leaks, (
        f"v3.7: active players in J.J. McCarthy's rookie comp pool: "
        f"{[c['name'] for c in active_leaks]}"
    )


def test_rookie_active_bust_comps_drop_mccarthy_rank(engine):
    """v3.7 effect: JJ McCarthy's comp pool now includes retired bust
    QBs (Tim Couch, Vince Young, Blake Bortles, Sam Bradford) and his
    rank should drop significantly from the pre-v3.7 #25 territory.
    Pin: not inside the top 100 (Phil's mandate).
    """
    mccarthy = next((r for r in engine.rankings if r["name"] == "J.J. McCarthy"), None)
    if mccarthy is None:
        pytest.skip("J.J. McCarthy not in rankings")
    assert mccarthy["overall_rank"] > 100, (
        f"J.J. McCarthy rank #{mccarthy['overall_rank']} - "
        f"under v3.7 retired-only with bust comps should be deeper"
    )


def test_elic_ayomanor_drops_under_retired_only(engine):
    """v3.7: Elic Ayomanor's rookie comp pool used to be dominated by
    recent-active 2024-2025 WR rookies. Under v3.7 retired-only he
    should drop from his prior top-30 perch to a more sensible spot.
    """
    elic = next((r for r in engine.rankings if r["name"] == "Elic Ayomanor"), None)
    if elic is None:
        pytest.skip("Elic Ayomanor not in rankings")
    assert elic["overall_rank"] > 60, (
        f"Elic Ayomanor rank #{elic['overall_rank']} - "
        f"v3.7 retired-only should drop him further"
    )
