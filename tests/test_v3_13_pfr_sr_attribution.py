"""v3.13 — Pro-Football-Reference + Sports-Reference attribution scrub.

Phil's 2026-06-03 brief: external scraped sources need explicit attribution
and links back to the canonical pages, both for ToS compliance and for
reader trust ("where did that career-stats block come from?").

This test suite pins the attribution at three layers:

  1. The Sources page (``_build_sources``) carries explicit
     Pro-Football-Reference and Sports-Reference / CFB rows with
     working links to the canonical sites.
  2. The career-stats panel rendered on every veteran profile
     (``pfr_career_stats.career_stats_html``) links the "Pro Football
     Reference" attribution directly to the canonical player page when
     the ``pfr_id`` is known, and to the PFR root otherwise.
  3. The per-player and per-prospect page footers carry a sources-page
     pointer + the right canonical link.
"""

from datetime import datetime

import pytest

from dynasty import report
from dynasty.sources import pfr_career_stats


LATEST_TS = datetime(2026, 6, 3, 12, 0, 0)


# ---------------------------------------------------------------------------
# Sources page
# ---------------------------------------------------------------------------


def _sources_html() -> str:
    return report._build_sources(LATEST_TS, "sf_ppr")


def test_sources_page_lists_pfr_with_link():
    html = _sources_html()
    assert "Pro-Football-Reference" in html
    assert "https://www.pro-football-reference.com/" in html


def test_sources_page_lists_pfr_subroles_explicitly():
    """Each distinct PFR use-case is surfaced as its own row so a reader
    can audit what we scrape and why. v3.13 lists three rows: seasonal
    stats (1936–1998), draft history (2022–2026), per-player career
    stats (v3.10)."""
    html = _sources_html()
    assert "seasonal stats" in html
    assert "draft history" in html
    assert "per-player career stats" in html


def test_sources_page_lists_sports_reference_cfb_with_link():
    html = _sources_html()
    assert "Sports-Reference / CFB" in html
    assert "https://www.sports-reference.com/cfb/" in html


def test_sources_page_lists_sports_reference_standings_explicitly():
    """CFB standings are a separate scrape that feeds the v3.0
    conference-tier weighting — surface it as its own row."""
    html = _sources_html()
    assert "standings" in html
    assert "sources_reference_cfb_standings.py" in html or \
        "sports_reference_cfb_standings.py" in html


def test_sources_page_has_attribution_callout():
    """ToS-compliant attribution block calls out Sports Reference LLC
    as the owner of both properties."""
    html = _sources_html()
    assert "Sports Reference LLC" in html
    assert "attribution" in html.lower()


def test_sources_page_links_to_nflverse_repo():
    """Existing nflverse rows now also linkify the source name (was
    plain text before v3.13)."""
    html = _sources_html()
    assert "https://github.com/nflverse/nflverse-data" in html
    # nflverse appears more than once because there are two rows
    # (player_stats_season + players).
    assert html.count("nflverse-data") >= 2


def test_sources_page_explicitly_separates_consensus_input():
    """KTC and v0.x sources are explicitly NOT model inputs — this must
    stay loud after the rewrite (avoid regressing the "we don't blend
    external opinions" claim that the engine page makes)."""
    html = _sources_html()
    assert "NOT" in html  # "explicitly NOT a model input"
    # The "no longer blends" claim sits across a line break in the
    # rendered HTML — normalise whitespace before asserting.
    normalised = " ".join(html.split())
    assert "no longer blends external opinions" in normalised


# ---------------------------------------------------------------------------
# Career-stats panel — PFR canonical link
# ---------------------------------------------------------------------------


def _career_payload(pfr_id: str) -> dict:
    """Minimal career-stats payload with at least one row so the
    renderer emits HTML (it short-circuits on empty rows)."""
    return {
        "position": "RB",
        "pfr_id": pfr_id,
        "fp_format": "superflex_ppr",
        "rows": [
            {
                "year": "2024",
                "team": "BAL",
                "age": "25",
                "games": "17",
                "rush_att": "200",
                "rush_yds": "1000",
                "rush_td": "10",
                "targets": "30",
                "rec": "25",
                "rec_yds": "200",
                "rec_td": "2",
                "fp": "250.0",
                "fp_pg": "14.7",
            },
        ],
        "totals": {
            "games": "17",
            "rush_att": "200",
            "rush_yds": "1000",
            "rush_td": "10",
            "rec": "25",
            "rec_yds": "200",
            "rec_td": "2",
            "fp": "250.0",
        },
    }


def test_career_stats_panel_links_to_canonical_pfr_page_when_id_known():
    """When we know the ``pfr_id`` we deep-link to PFR's player page so
    the reader can audit the underlying splits in one click."""
    html = pfr_career_stats.career_stats_html(_career_payload("HenrDe00"))
    # PFR URL pattern: /players/<first-letter-of-id>/<id>.htm
    assert "https://www.pro-football-reference.com/players/H/HenrDe00.htm" in html
    assert "Pro Football Reference" in html


def test_career_stats_panel_links_to_pfr_root_when_id_missing():
    """If a row's pfr_id can't be resolved (very rare — happens for
    non-PFR-bridged retired players) we fall back to the PFR root URL
    rather than dropping the attribution entirely."""
    payload = _career_payload("")
    # Wipe pfr_id explicitly to simulate the missing-id case.
    payload["pfr_id"] = ""
    html = pfr_career_stats.career_stats_html(payload)
    assert "Pro Football Reference" in html
    assert "https://www.pro-football-reference.com/" in html
    # No deep-link to a player page in this case.
    assert "/players/" not in html


def test_career_stats_panel_renders_rel_noopener_target_blank():
    """External links open in a new tab and use rel=noopener for
    standard external-link safety."""
    html = pfr_career_stats.career_stats_html(_career_payload("HenrDe00"))
    assert 'rel="noopener"' in html
    assert 'target="_blank"' in html


def test_career_stats_panel_empty_payload_short_circuits():
    """The renderer continues to short-circuit on empty rows (we
    haven't changed the empty-state contract — the player profile
    just hides the heading)."""
    payload = _career_payload("HenrDe00")
    payload["rows"] = []
    html = pfr_career_stats.career_stats_html(payload)
    assert html == ""


# ---------------------------------------------------------------------------
# Player + prospect page footer attribution
# ---------------------------------------------------------------------------


def _veteran_row(player_id: str = "00-0034796") -> dict:
    return {
        "player_id": player_id,
        "name": "Test Player",
        "position": "RB",
        "team": "BAL",
        "age": 25,
        "overall_rank": 5,
        "tier": 1,
        "projected_years_remaining": 7.0,
        "peak_3yr_fp_per_game": 14.5,
        "production_score": 1234.0,
        "comp_weighted_fp": 1100.0,
        "peak_anchored_fp": 1200.0,
        "proven_floor_fp": 0.0,
        "raw_pre_penalty": 1200.0,
        "projection_raw_pre_penalty": 1200.0,
        "survival_multiplier": 0.95,
        "sample_confidence": 1.0,
        "late_breakout_penalty": 1.0,
        "missed_season_multiplier": 1.0,
        "missed_season_reason": "",
        "projection_path": "comp_weighted",
        "n_comps": 20,
        "engine": "similarity_v1",
        "rookie_season": 2020,
        "years_pro": 5,
        "rank": 5,
    }


def _veteran_comps() -> list:
    """Minimal comp list so ``_build_player_page`` renders without
    KeyErrors."""
    out = []
    for i in range(10):
        out.append({
            "name": f"Comp {i}",
            "position": "RB",
            "last_season": 2018 - i,
            "similarity": 0.7 - 0.02 * i,
            "peak_3yr_fp_per_game": 12.0,
            "seasons_played": 8,
            "final_age": 30,
            "post_age_seasons": 4,
            "career_ppr": 1000,
            "post_age_projected_pts": 400,
            "washed_out": False,
            "snapshot_season": 2015,
            "is_pre1999_snapshot": False,
        })
    return out


def test_veteran_player_page_footer_has_sources_pointer():
    html = report._build_player_page(
        _veteran_row(), _veteran_comps(), "BAL", "sf_ppr", LATEST_TS
    )
    assert 'href="../sources.html"' in html
    assert "nflverse" in html
    assert "Pro-Football-Reference" in html


def _prospect_payload() -> dict:
    return {
        "name": "Test Prospect",
        "position": "RB",
        "school": "Test School",
        "draft_class": 2026,
        "age": 22.5,
        "cfb_player_id": "test-prospect-123",
        "model_overall_rank": 25,
        "model_pos_rank": 5,
        "projection": {
            "projected_career_fp": 1200.0,
            "projected_peak3_fp_pg": 14.0,
            "projected_years_in_league": 7.0,
            "projection_source": "comp_weighted",
            "comp_only_career_fp": 1200.0,
            "pick_tier_baseline_fp": 1000.0,
            "projection_confidence": 0.8,
            "n_meaningful_nfl_comps": 12,
            "n_comps_with_nfl": 18,
            "floor_applied": False,
        },
        "production": {
            "adj_career_fp_pg": 18.0,
            "final_season_fp_pg": 20.0,
            "peak_season_fp_pg": 22.0,
        },
        "comps": [],
        "ktc": {},
        "drafted": {},
        "conference_tier_last": "P5",
    }


def test_prospect_page_footer_has_sources_pointer():
    html = report._build_prospect_page(_prospect_payload(), "sf_ppr", LATEST_TS)
    assert 'href="../sources.html"' in html
    assert "Sports-Reference / College Football" in html
    assert "Pro-Football-Reference" in html


# ---------------------------------------------------------------------------
# Methodology page — linkified PFR / corpus floor
# ---------------------------------------------------------------------------


def test_methodology_pfr_text_is_now_a_link():
    """The corpus-floor bullet on the methodology page used to mention
    Pro-Football-Reference as plain text. v3.13 linkifies it (and
    updates the floor language from 1980 to 1936 to match v3.9)."""
    # We don't need a real EngineResult for this — just build the
    # methodology HTML directly. ``_build_methodology`` takes an
    # ``EngineResult`` though; use a small stub.
    class _StubPace:
        source = "test-fixture"
        multipliers = {}

        def get(self, pos, stat, era):
            return 1.0

    class _StubEngine:
        era_pace = _StubPace()
        rankings = []

    html = report._build_methodology(_StubEngine(), LATEST_TS, "sf_ppr")
    assert "https://www.pro-football-reference.com/" in html
    # And the floor language now matches the v3.9 corpus reality.
    assert "1936" in html


# ---------------------------------------------------------------------------
# Footer attribution — both sources now linkified
# ---------------------------------------------------------------------------


def test_footer_links_pfr_and_sr_cfb():
    """The site-wide footer attributes both PFR (NFL stats) and
    Sports-Reference / CFB (college stats for the prospect engine)."""
    footer = report._footer()
    assert "https://www.pro-football-reference.com/" in footer
    assert "https://www.sports-reference.com/cfb/" in footer
    assert "nflverse" in footer
