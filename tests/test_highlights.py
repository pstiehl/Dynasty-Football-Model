"""Tests for the YouTube highlight matcher.

No network, no DB — everything runs against in-memory fixtures so this can
gate CI without an API key.
"""
from __future__ import annotations

import pytest

from dynasty.highlights import (
    KIND_OTHER,
    KIND_PLAYER,
    KIND_TEAM,
    PlayerRef,
    Video,
    build_index,
    build_name_index,
    classify,
    find_teams,
    match_players,
    match_players_with_ambiguity,
    parse_season,
    parse_week,
    tokenize,
)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

PLAYERS = [
    PlayerRef("4046", "Patrick Mahomes", "QB", "KC", gsis_id="00-0033873", rank=3),
    PlayerRef("7564", "Ja'Marr Chase", "WR", "CIN", gsis_id="00-0036900", rank=1),
    PlayerRef("8146", "Amon-Ra St. Brown", "WR", "DET", gsis_id="00-0036997", rank=12),
    PlayerRef("9509", "Bijan Robinson", "RB", "ATL", gsis_id="00-0038542", rank=5),
    PlayerRef("6794", "Tee Higgins", "WR", "CIN", gsis_id="00-0036442", rank=40),
    PlayerRef("11604", "Brock Bowers", "TE", "LV", gsis_id="00-0039910", rank=15),
    PlayerRef("4984", "Kenneth Walker III", "RB", "SEA", gsis_id="00-0037746", rank=48),
    # Same-name collision: the QB is ranked, the linebacker is not.
    PlayerRef("4984000", "Josh Allen", "QB", "BUF", gsis_id="00-0034857", rank=2),
    PlayerRef("3163", "Josh Allen", "LB", "JAX", gsis_id="00-0035313", rank=None),
    # Unranked collision pair — should refuse to guess.
    PlayerRef("aaa", "Michael Thomas", "WR", "NO", gsis_id="00-0031381", rank=None),
    PlayerRef("bbb", "Michael Thomas", "S", "HOU", gsis_id="00-0030926", rank=None),
]


def vid(video_id, title, **kw):
    kw.setdefault("channel_id", "UCtest")
    kw.setdefault("channel_title", "Test Highlights")
    kw.setdefault("published_at", "2026-09-16T14:00:00Z")
    kw.setdefault("duration_seconds", 240)
    return Video(video_id=video_id, title=title, **kw)


@pytest.fixture
def name_index():
    return build_name_index(PLAYERS)


# --------------------------------------------------------------------------
# Title parsing
# --------------------------------------------------------------------------

def test_tokenize_splits_hyphens_and_periods():
    assert tokenize("Amon-Ra St. Brown") == ["amon", "ra", "st", "brown"]


def test_tokenize_handles_apostrophes():
    assert tokenize("Ja'Marr Chase") == ["ja", "marr", "chase"]


@pytest.mark.parametrize("title,expected", [
    ("Highlights Week 3 vs Vikings", 3),
    ("WEEK 12 | Every Touch", 12),
    ("Week#7 highlights", 7),
    ("2026 Season Recap", None),
    ("Week 47 nonsense", None),
])
def test_parse_week(title, expected):
    assert parse_week(title) == expected


def test_parse_season_takes_last_plausible_year():
    assert parse_season("2025 vs 2026 comparison") == 2026
    assert parse_season("no year here") is None


def test_find_teams_maps_nicknames_and_aliases():
    assert find_teams("Bengals vs Niners Week 1") == ["CIN", "SF"]
    assert find_teams("Bucs take on the Pats") == ["TB", "NE"]


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def test_matches_simple_name(name_index):
    v = vid("a1", "Ja'Marr Chase Highlights Week 3 vs Vikings")
    assert [p.name for p in match_players(v, name_index)] == ["Ja'Marr Chase"]


def test_matches_multi_token_name_with_punctuation(name_index):
    v = vid("a2", "Amon-Ra St. Brown | Week 1 Highlights")
    assert [p.name for p in match_players(v, name_index)] == ["Amon-Ra St. Brown"]


def test_matches_name_with_generational_suffix(name_index):
    """Title says 'Kenneth Walker', roster says 'Kenneth Walker III'."""
    v = vid("a3", "Kenneth Walker EVERY Touch Week 2")
    assert [p.name for p in match_players(v, name_index)] == ["Kenneth Walker III"]


def test_matches_two_players_in_one_title(name_index):
    v = vid("a4", "Ja'Marr Chase & Tee Higgins Highlights Week 3")
    names = {p.name for p in match_players(v, name_index)}
    assert names == {"Ja'Marr Chase", "Tee Higgins"}


def test_collision_resolved_by_team_in_title(name_index):
    v = vid("a5", "Josh Allen Highlights Week 2 | Jaguars vs Titans")
    matched = match_players(v, name_index)
    assert [p.position for p in matched] == ["LB"]


def test_collision_resolved_by_model_rank_when_team_absent(name_index):
    v = vid("a6", "Josh Allen Every Throw Week 4")
    matched = match_players(v, name_index)
    assert [p.position for p in matched] == ["QB"]


def test_unresolvable_collision_matches_nothing(name_index):
    """Two unranked same-name players and no team hint -> refuse to guess."""
    v = vid("a7", "Michael Thomas Highlights Week 5")
    assert match_players(v, name_index) == []


def test_no_false_positive_on_unrelated_title(name_index):
    v = vid("a8", "Top 10 Plays of Week 3")
    assert match_players(v, name_index) == []


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def test_classify_player_cutup(name_index):
    v = vid("b1", "Bijan Robinson Highlights Week 3")
    assert classify(v, match_players(v, name_index)) == KIND_PLAYER


def test_classify_team_game_with_no_player(name_index):
    v = vid("b2", "Cowboys vs Eagles Game Highlights | 2026 NFL Week 1")
    assert classify(v, match_players(v, name_index)) == KIND_TEAM


def test_classify_rejects_podcast_even_when_player_named(name_index):
    v = vid("b3", "Ja'Marr Chase Trade Rumors | Fantasy Podcast")
    assert classify(v, match_players(v, name_index)) == KIND_OTHER


def test_classify_rejects_college_highlights(name_index):
    v = vid("b4", "Bijan Robinson College Highlights")
    assert classify(v, match_players(v, name_index)) == KIND_OTHER


# --------------------------------------------------------------------------
# Index build
# --------------------------------------------------------------------------

def test_build_index_shape_and_crosswalk():
    videos = [
        vid("v1", "Ja'Marr Chase Highlights Week 3 vs Vikings"),
        vid("v2", "Bijan Robinson EVERY Touch | Week 3 2026"),
        vid("v3", "Ja'Marr Chase Trade Rumors | Podcast"),   # dropped
    ]
    idx = build_index(PLAYERS, videos, expected_week=3)

    assert set(idx["clips"]) == {"7564", "9509"}
    assert idx["by_gsis"]["00-0036900"] == "7564"
    assert idx["players"]["7564"]["rank"] == 1
    assert idx["stats"]["total_clips"] == 2
    assert idx["stats"]["videos_non_game"] == 1


def test_ambiguity_is_reported_alongside_matches(name_index):
    """The unresolved collision is counted, not silently swallowed."""
    v = vid("amb1", "Michael Thomas Highlights Week 5")
    matched, ambiguous = match_players_with_ambiguity(v, name_index)
    assert matched == []
    assert ambiguous == 1


def test_no_ambiguity_reported_for_clean_match(name_index):
    v = vid("amb2", "Ja'Marr Chase Highlights Week 3")
    matched, ambiguous = match_players_with_ambiguity(v, name_index)
    assert [p.sleeper_id for p in matched] == ["7564"]
    assert ambiguous == 0


def test_ambiguous_collision_counted_separately_from_non_game():
    """An unresolvable name collision is its own failure mode.

    Regression: classify() returns KIND_OTHER for a cut-up title whose only
    name was ambiguous (because ``matched`` is empty), so the video used to
    be tallied as ``videos_non_game`` -- indistinguishable from a podcast.
    """
    videos = [vid("amb3", "Michael Thomas Highlights Week 5")]
    idx = build_index(PLAYERS, videos)

    assert idx["clips"] == {}
    assert idx["stats"]["videos_ambiguous"] == 1
    assert idx["stats"]["videos_non_game"] == 0
    assert idx["stats"]["videos_unmatched"] == 0


def test_genuine_non_game_still_counts_as_non_game():
    """The ambiguous bucket must not swallow ordinary off-topic videos."""
    videos = [vid("amb4", "Ja'Marr Chase Trade Rumors | Podcast")]
    idx = build_index(PLAYERS, videos)

    assert idx["stats"]["videos_non_game"] == 1
    assert idx["stats"]["videos_ambiguous"] == 0


def test_drop_buckets_are_disjoint_and_account_for_every_video():
    """Every seen video lands in exactly one bucket, or produces clips."""
    videos = [
        vid("d1", "Ja'Marr Chase Highlights Week 3 vs Vikings"),   # clips
        vid("d2", "Michael Thomas Highlights Week 5"),             # ambiguous
        vid("d3", "Ja'Marr Chase Trade Rumors | Podcast"),         # non-game
        vid("d4", "Ja'Marr Chase Highlights", duration_seconds=4),  # too short
    ]
    idx = build_index(PLAYERS, videos)
    s = idx["stats"]

    dropped = (s["videos_too_short"] + s["videos_non_game"]
               + s["videos_unmatched"] + s["videos_ambiguous"])
    assert s["videos_seen"] == 4
    assert dropped == 3
    assert s["videos_ambiguous"] == 1
    assert s["videos_non_game"] == 1
    assert s["videos_too_short"] == 1


def test_non_embeddable_videos_are_kept_now_that_nothing_embeds():
    """Embeddability stopped being a reason to drop a video.

    It was one when the reel played clips in an IFrame player: error 150
    mid-playlist stalled the whole queue. The pages link out to YouTube
    now, where the uploader's embed setting has no effect on whether the
    viewer can watch it, so dropping these only cost us film.

    The counter survives as telemetry, which is why it is asserted on.
    """
    videos = [vid("v1", "Ja'Marr Chase Highlights Week 3", embeddable=False)]
    idx = build_index(PLAYERS, videos)
    assert idx["clips"]["7564"][0]["video_id"] == "v1"
    assert idx["stats"]["videos_embed_blocked"] == 1


def test_duplicate_video_ids_deduped():
    videos = [
        vid("same", "Ja'Marr Chase Highlights Week 3"),
        vid("same", "Ja'Marr Chase Highlights Week 3"),
    ]
    idx = build_index(PLAYERS, videos)
    assert len(idx["clips"]["7564"]) == 1


def test_player_cutup_sorts_ahead_of_team_fallback():
    videos = [
        vid("t1", "Bengals vs Vikings Game Highlights Week 3"),
        vid("p1", "Ja'Marr Chase Highlights Week 3 vs Vikings"),
    ]
    idx = build_index(PLAYERS, videos)
    assert idx["clips"]["7564"][0]["kind"] == KIND_PLAYER


def test_max_clips_per_player_truncates():
    videos = [
        vid(f"v{i}", f"Ja'Marr Chase Highlights Week {i}") for i in range(1, 9)
    ]
    idx = build_index(PLAYERS, videos, max_clips_per_player=3)
    assert len(idx["clips"]["7564"]) == 3


def test_confidence_rises_when_own_team_named():
    with_team = build_index(
        PLAYERS, [vid("c1", "Ja'Marr Chase Highlights Week 3 | Bengals")]
    )["clips"]["7564"][0]["confidence"]
    without_team = build_index(
        PLAYERS, [vid("c2", "Ja'Marr Chase Highlights Week 3")]
    )["clips"]["7564"][0]["confidence"]
    assert with_team > without_team


def test_min_confidence_filters_weak_matches():
    videos = [vid("w1", "Ja'Marr Chase Week 3")]
    assert build_index(PLAYERS, videos, min_confidence=0.95)["clips"] == {}
    assert build_index(PLAYERS, videos, min_confidence=0.5)["clips"] != {}


def test_team_recap_attaches_to_skill_players_on_both_teams():
    """A game recap names no player, so it must fan out to the rosters."""
    videos = [vid("t9", "Bengals vs Vikings Game Highlights | 2026 NFL Week 3")]
    idx = build_index(PLAYERS, videos, include_team_fallback=True)
    # Chase and Higgins are the CIN skill players in the fixture set.
    assert set(idx["clips"]) == {"7564", "6794"}
    assert idx["clips"]["7564"][0]["kind"] == KIND_TEAM


def test_team_recap_suppressed_when_fallback_disabled():
    videos = [vid("t10", "Bengals vs Vikings Game Highlights | 2026 NFL Week 3")]
    assert build_index(PLAYERS, videos, include_team_fallback=False)["clips"] == {}


def test_team_recap_not_penalised_for_roster_size():
    """Confidence must not collapse just because a recap covers many players."""
    videos = [vid("t11", "Bengals vs Vikings Game Highlights | 2026 NFL Week 3")]
    idx = build_index(PLAYERS, videos, include_team_fallback=True)
    assert idx["clips"]["7564"][0]["confidence"] >= 0.5


def test_clips_sort_newest_week_first():
    videos = [
        vid("w2", "Ja'Marr Chase Highlights Week 2", published_at="2026-09-09T14:00:00Z"),
        vid("w4", "Ja'Marr Chase Highlights Week 4", published_at="2026-09-23T14:00:00Z"),
        vid("w3", "Ja'Marr Chase Highlights Week 3", published_at="2026-09-16T14:00:00Z"),
    ]
    got = [c["week"] for c in build_index(PLAYERS, videos)["clips"]["7564"]]
    assert got == [4, 3, 2]


def test_within_a_week_newest_upload_wins():
    videos = [
        vid("old", "Ja'Marr Chase Highlights Week 3", published_at="2026-09-16T08:00:00Z"),
        vid("new", "Ja'Marr Chase Highlights Week 3", published_at="2026-09-16T20:00:00Z"),
    ]
    got = [c["video_id"] for c in build_index(PLAYERS, videos)["clips"]["7564"]]
    assert got[0] == "new"
