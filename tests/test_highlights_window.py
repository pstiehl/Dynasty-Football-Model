"""Tests for the rolling game window that drives highlight recency.

The bug these exist for: on Monday 2026-09-21 the old ``current_nfl_week()``
returned 2, because 11 days had elapsed since the Sep 10 kickoff. Week 2's
slate was still being played, so nothing from it had been uploaded, and the
reel keyed its ordering off a week number no clip could match yet. The fix
is to select on publish timestamps inside the most recently *completed*
Thursday-to-Monday slate.

No network, no DB, no clock dependency -- every case pins ``as_of``.
"""
from __future__ import annotations

from datetime import datetime, timezone

from dynasty.highlights import (
    DEFAULT_GRACE_HOURS,
    DEFAULT_WINDOW_DAYS,
    KIND_PLAYER,
    GameWindow,
    PlayerRef,
    Video,
    build_index,
    resolve_game_window,
    week_number_for,
)


SEASON_START = datetime(2026, 9, 10, tzinfo=timezone.utc)   # week 1 Thursday

PLAYERS = [
    PlayerRef("3294", "Dak Prescott", "QB", "DAL", gsis_id="00-0033077", rank=41),
    PlayerRef("12001", "Jaxson Dart", "QB", "NYG", gsis_id="00-0039163", rank=64),
]


def utc(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


def vid(video_id, title, published_at, **kw):
    kw.setdefault("channel_id", "UCtest")
    kw.setdefault("channel_title", "Test Highlights")
    kw.setdefault("duration_seconds", 240)
    return Video(video_id=video_id, title=title,
                 published_at=published_at, **kw)


def window_at(as_of, **kw):
    kw.setdefault("season_start", SEASON_START)
    return resolve_game_window(as_of, **kw)


# --------------------------------------------------------------------------
# The Monday case
# --------------------------------------------------------------------------

def test_monday_morning_serves_the_last_completed_slate():
    """08:41 EDT on Monday 2026-09-21 -- the owner's reported case."""
    w = window_at(utc(2026, 9, 21, 12, 41))
    assert w.start == utc(2026, 9, 10)
    assert w.end.date() == datetime(2026, 9, 14).date()
    assert w.label == "Sep 10\u201314"
    # Not week 2, which is the number the old season-start arithmetic gave.
    assert w.week == 1


def test_monday_window_is_stable_all_day():
    for hour in (0, 6, 12, 18, 23):
        assert window_at(utc(2026, 9, 21, hour)).label == "Sep 10\u201314"


def test_window_does_not_advance_while_monday_night_is_still_on():
    """MNF ends around 03:30 UTC Tuesday; nothing is cut up yet."""
    assert window_at(utc(2026, 9, 22, 3, 30)).label == "Sep 10\u201314"


def test_window_advances_once_the_cut_ups_have_landed():
    """Grace expires Tuesday midday UTC, before the Tuesday 16:00 CI pass."""
    assert window_at(utc(2026, 9, 22, 11, 59)).label == "Sep 10\u201314"
    assert window_at(utc(2026, 9, 22, 12, 0)).label == "Sep 17\u201321"
    assert window_at(utc(2026, 9, 22, 16, 0)).label == "Sep 17\u201321"


def test_thursday_night_game_does_not_open_a_new_window():
    """A slate in progress must never become the default."""
    w = window_at(utc(2026, 9, 24, 20))          # TNF of week 3
    assert w.label == "Sep 17\u201321"
    assert w.week == 2


def test_sunday_afternoon_still_serves_the_finished_week():
    assert window_at(utc(2026, 9, 27, 18)).label == "Sep 17\u201321"


# --------------------------------------------------------------------------
# Overrides
# --------------------------------------------------------------------------

def test_grace_hours_override_holds_the_window_open():
    late = window_at(utc(2026, 9, 22, 16), grace_hours=96)
    assert late.label == "Sep 10\u201314"


def test_zero_grace_flips_as_soon_as_the_final_day_starts():
    w = window_at(utc(2026, 9, 21, 12, 41), grace_hours=0)
    assert w.label == "Sep 17\u201321"


def test_window_days_override_changes_the_slate_length():
    w = window_at(utc(2026, 9, 24, 20), window_days=4)
    assert w.start == utc(2026, 9, 17)
    assert w.end.date() == datetime(2026, 9, 20).date()
    assert w.window_days == 4


def test_defaults_are_the_documented_ones():
    w = window_at(utc(2026, 9, 21, 12, 41))
    assert w.window_days == DEFAULT_WINDOW_DAYS == 5
    assert w.grace_hours == DEFAULT_GRACE_HOURS == 36


def test_naive_as_of_is_treated_as_utc():
    naive = datetime(2026, 9, 21, 12, 41)
    assert resolve_game_window(naive, season_start=SEASON_START).label == "Sep 10\u201314"


# --------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------

def test_label_spans_a_month_boundary():
    w = GameWindow(
        start=utc(2026, 9, 30), end=utc(2026, 10, 4, 23, 59),
        publish_cutoff=utc(2026, 10, 6),
    )
    assert w.label == "Sep 30\u2013Oct 4"


def test_week_number_is_exact_because_both_ends_are_thursdays():
    assert week_number_for(utc(2026, 9, 10), SEASON_START) == 1
    assert week_number_for(utc(2026, 9, 17), SEASON_START) == 2
    assert week_number_for(utc(2026, 12, 31), SEASON_START) == 17


def test_week_number_is_none_before_the_season_and_off_the_end():
    assert week_number_for(utc(2026, 8, 20), SEASON_START) is None
    assert week_number_for(utc(2027, 3, 4), SEASON_START) is None
    assert week_number_for(utc(2026, 9, 10), None) is None


# --------------------------------------------------------------------------
# Membership
# --------------------------------------------------------------------------

def test_contains_covers_the_slate_and_the_upload_lag():
    w = window_at(utc(2026, 9, 21, 12, 41))
    assert w.contains("2026-09-10T00:00:00Z")          # Thursday kickoff
    assert w.contains("2026-09-14T23:00:00Z")          # Monday night
    assert w.contains("2026-09-15T14:00:00Z")          # Tuesday cut-up
    assert not w.contains("2026-09-09T23:59:00Z")      # Wednesday before
    assert not w.contains("2026-09-17T12:00:00Z")      # next slate


def test_undated_upload_is_not_counted_as_current():
    """An unparsable timestamp must not outrank a genuine in-window clip."""
    w = window_at(utc(2026, 9, 21, 12, 41))
    assert not w.contains("")
    assert not w.contains(None)
    assert not w.contains("not a date")


# --------------------------------------------------------------------------
# Effect on the index
# --------------------------------------------------------------------------

def test_in_window_clips_lead_and_older_clips_are_kept():
    w = window_at(utc(2026, 9, 21, 12, 41))
    videos = [
        vid("old", "Dak Prescott Highlights Week 18", "2026-08-30T18:00:00Z"),
        vid("new", "Dak Prescott Highlights", "2026-09-14T22:00:00Z"),
    ]
    clips = build_index(PLAYERS, videos, window=w)["clips"]["3294"]

    assert [c["video_id"] for c in clips] == ["new", "old"]
    assert clips[0]["in_window"] is True
    # Retained, not filtered: the "every clip per player" toggle needs it.
    assert "in_window" not in clips[1] or clips[1]["in_window"] is False


def test_a_titled_week_no_longer_beats_a_newer_upload():
    """The regression the old ``-(week or 0)`` sort key caused.

    The stale clip shouts "Week 2" in its title; the current one does not
    mention a week at all. Recency has to win, or a reel leads on film from
    a slate that has not been played.
    """
    w = window_at(utc(2026, 9, 21, 12, 41))
    videos = [
        vid("titled", "Dak Prescott Highlights Week 2", "2026-09-02T18:00:00Z"),
        vid("current", "Dak Prescott Every Throw", "2026-09-13T18:00:00Z"),
    ]
    clips = build_index(PLAYERS, videos, window=w)["clips"]["3294"]
    assert clips[0]["video_id"] == "current"


def test_untitled_week_clip_is_not_buried():
    """Two in-window clips, only one with a week in its title."""
    w = window_at(utc(2026, 9, 21, 12, 41))
    videos = [
        vid("has_week", "Dak Prescott Highlights Week 1", "2026-09-13T10:00:00Z"),
        vid("no_week", "Dak Prescott Highlights", "2026-09-14T10:00:00Z"),
    ]
    clips = build_index(PLAYERS, videos, window=w)["clips"]["3294"]
    assert clips[0]["video_id"] == "no_week"


def test_cutup_still_outranks_a_recap_inside_the_window():
    w = window_at(utc(2026, 9, 21, 12, 41))
    videos = [
        vid("recap", "Cowboys vs Giants Game Highlights", "2026-09-14T23:00:00Z"),
        vid("cutup", "Dak Prescott Highlights", "2026-09-14T12:00:00Z"),
    ]
    clips = build_index(PLAYERS, videos, window=w)["clips"]["3294"]
    assert clips[0]["kind"] == KIND_PLAYER


def test_window_metadata_is_published_on_the_artifact():
    w = window_at(utc(2026, 9, 21, 12, 41))
    idx = build_index(PLAYERS, [vid("v", "Dak Prescott Highlights",
                                    "2026-09-14T22:00:00Z")], window=w)

    assert idx["window_start"].startswith("2026-09-10")
    assert idx["window_end"].startswith("2026-09-14")
    assert idx["window"]["label"] == "Sep 10\u201314"
    assert idx["window"]["grace_hours"] == 36
    assert idx["window"]["week"] == 1
    assert idx["stats"]["clips_in_window"] == 1
    assert idx["stats"]["players_with_window_clips"] == 1


def test_index_without_a_window_is_unchanged():
    """Callers that pass no window keep the pre-windowing behaviour."""
    idx = build_index(PLAYERS, [vid("v", "Dak Prescott Highlights",
                                    "2026-09-14T22:00:00Z")])
    assert idx["window"] is None
    assert idx["window_start"] is None
    assert "in_window" not in idx["clips"]["3294"][0]
    assert idx["stats"]["clips_in_window"] == 0


def test_in_window_clip_scores_above_the_same_clip_out_of_window():
    w = window_at(utc(2026, 9, 21, 12, 41))
    inside = build_index(PLAYERS, [vid("a", "Dak Prescott Highlights",
                                       "2026-09-14T22:00:00Z")],
                         window=w)["clips"]["3294"][0]["confidence"]
    outside = build_index(PLAYERS, [vid("b", "Dak Prescott Highlights",
                                        "2026-08-14T22:00:00Z")],
                          window=w)["clips"]["3294"][0]["confidence"]
    assert inside > outside
