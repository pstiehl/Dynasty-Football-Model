"""Tests for the rolling game window that drives highlight recency.

The bug these exist for: the old ``current_nfl_week()`` derived a week from
a season-start constant and ``_sort_clips`` ordered on a week number parsed
out of video titles -- a field absent from many uploads, which therefore
sorted as week 0 and fell behind everything however recent it was. The fix
is to select and order on publish timestamps inside a rolling slate window.

The *anchor* of that window was then corrected against the live index of
2026-09-21T13:08Z, and these tests pin the corrected behaviour. All 28
videos in that index were published Sep 20-21, so a window anchored to the
slate's final day (changeover Tuesday 12:00 UTC) resolved to Sep 10-14 and
matched 0 of 737 clip rows -- a completely blank reel on a Monday morning
while Sunday's film sat unshown. The window now opens 36h after the start
of its *Sunday*, i.e. Monday 12:00 UTC.

Nothing here asserts a particular week number as "the" answer; the week is
only a label, and every case pins ``as_of`` explicitly.

No network, no DB, no clock dependency.
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
    window_containing,
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

def test_monday_morning_serves_the_slate_whose_film_exists():
    """09:00 EDT on Monday 2026-09-21, matching the live index.

    Sunday's games were the day before and 21 of the index's 28 videos were
    published on that Sunday, so Sep 17-21 is the slate with film in it.
    """
    w = window_at(utc(2026, 9, 21, 13))
    assert w.start == utc(2026, 9, 17)
    assert w.end.date() == datetime(2026, 9, 21).date()
    assert w.label == "Sep 17\u201321"
    assert w.week == 2                       # agrees with live expected_week


def test_changeover_is_monday_noon_utc():
    """Before 12:00 UTC Monday the previous slate still leads."""
    assert window_at(utc(2026, 9, 21, 11, 59)).label == "Sep 10\u201314"
    assert window_at(utc(2026, 9, 21, 12, 0)).label == "Sep 17\u201321"
    assert window_at(utc(2026, 9, 21, 12, 0)).opens_at == utc(2026, 9, 21, 12)


def test_weekend_viewer_still_gets_the_previous_finished_slate():
    """The grace period's real job: Thu-Sun must not jump to a barely
    started slate just because Thursday night football has happened."""
    for moment in (utc(2026, 9, 17, 20),     # TNF in progress
                   utc(2026, 9, 18, 12),     # Friday
                   utc(2026, 9, 19, 12),      # Saturday
                   utc(2026, 9, 20, 18),     # Sunday 1pm ET games
                   utc(2026, 9, 20, 23)):    # Sunday night
        assert window_at(moment).label == "Sep 10\u201314", moment


def test_window_holds_through_monday_night_and_into_midweek():
    for moment in (utc(2026, 9, 21, 23),     # MNF kickoff
                   utc(2026, 9, 22, 3, 30),  # MNF just ended
                   utc(2026, 9, 22, 16),     # Tuesday CI pass
                   utc(2026, 9, 23, 11)):    # Wednesday daily pass
        assert window_at(moment).label == "Sep 17\u201321", moment


def test_anchor_does_not_wait_a_full_day_for_the_final_game():
    """Regression on the corrected anchor.

    Anchoring grace to the slate's final day put the changeover at Tuesday
    12:00 UTC, which left Monday viewers a week behind the film that
    existed. Monday afternoon must already be on the current slate.
    """
    assert window_at(utc(2026, 9, 21, 18)).label != "Sep 10\u201314"
    assert window_at(utc(2026, 9, 21, 18)).week == 2


def test_thursday_night_game_does_not_open_a_new_window():
    """A slate in progress must never become the default."""
    w = window_at(utc(2026, 9, 24, 20))          # TNF of week 3
    assert w.label == "Sep 17\u201321"
    assert w.week == 2


def test_sunday_afternoon_still_serves_the_finished_week():
    assert window_at(utc(2026, 9, 27, 18)).label == "Sep 17\u201321"


def test_next_monday_advances_one_slate():
    assert window_at(utc(2026, 9, 28, 13)).label == "Sep 24\u201328"
    assert window_at(utc(2026, 9, 28, 13)).week == 3


# --------------------------------------------------------------------------
# Overrides
# --------------------------------------------------------------------------

def test_grace_hours_override_holds_the_window_open():
    late = window_at(utc(2026, 9, 22, 16), grace_hours=96)
    assert late.label == "Sep 10\u201314"


def test_zero_grace_flips_as_soon_as_the_sunday_starts():
    w = window_at(utc(2026, 9, 20, 1), grace_hours=0)
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
    naive = datetime(2026, 9, 21, 13)
    assert resolve_game_window(
        naive, season_start=SEASON_START).label == "Sep 17\u201321"


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
    w = window_at(utc(2026, 9, 21, 13))               # Sep 17-21
    assert w.contains("2026-09-17T00:00:00Z")          # Thursday kickoff
    assert w.contains("2026-09-20T21:00:00Z")          # Sunday film
    assert w.contains("2026-09-21T13:00:00Z")          # Monday uploads
    assert w.contains("2026-09-22T14:00:00Z")          # MNF cut-up, Tuesday
    assert not w.contains("2026-09-16T23:59:00Z")      # Wednesday before
    assert not w.contains("2026-09-14T22:00:00Z")      # previous slate


def test_live_index_publish_dates_all_fall_inside_the_window():
    """The concrete regression, using the live index's real timestamps.

    Every video in highlights.json at 2026-09-21T13:08Z was published on
    Sep 20 or Sep 21. Under the previous anchor none of them were in the
    resolved window and the reel was empty.
    """
    w = window_at(utc(2026, 9, 21, 13))
    for stamp in ("2026-09-20T16:02:00Z", "2026-09-20T21:45:00Z",
                  "2026-09-20T23:58:00Z", "2026-09-21T02:11:00Z",
                  "2026-09-21T12:40:00Z"):
        assert w.contains(stamp), stamp


def test_undated_upload_is_not_counted_as_current():
    """An unparsable timestamp must not outrank a genuine in-window clip."""
    w = window_at(utc(2026, 9, 21, 13))
    assert not w.contains("")
    assert not w.contains(None)
    assert not w.contains("not a date")


# --------------------------------------------------------------------------
# Effect on the index
# --------------------------------------------------------------------------

def test_in_window_clips_lead_and_older_clips_are_kept():
    w = window_at(utc(2026, 9, 21, 13))
    videos = [
        vid("old", "Dak Prescott Highlights Week 18", "2026-09-06T18:00:00Z"),
        vid("new", "Dak Prescott Highlights", "2026-09-20T22:00:00Z"),
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
    w = window_at(utc(2026, 9, 21, 13))
    videos = [
        vid("titled", "Dak Prescott Highlights Week 2", "2026-09-08T18:00:00Z"),
        vid("current", "Dak Prescott Every Throw", "2026-09-20T18:00:00Z"),
    ]
    clips = build_index(PLAYERS, videos, window=w)["clips"]["3294"]
    assert clips[0]["video_id"] == "current"


def test_untitled_week_clip_is_not_buried():
    """Two in-window clips, only one with a week in its title."""
    w = window_at(utc(2026, 9, 21, 13))
    videos = [
        vid("has_week", "Dak Prescott Highlights Week 2", "2026-09-20T10:00:00Z"),
        vid("no_week", "Dak Prescott Highlights", "2026-09-20T22:00:00Z"),
    ]
    clips = build_index(PLAYERS, videos, window=w)["clips"]["3294"]
    assert clips[0]["video_id"] == "no_week"


def test_cutup_still_outranks_a_recap_inside_the_window():
    w = window_at(utc(2026, 9, 21, 13))
    videos = [
        vid("recap", "Cowboys vs Giants Game Highlights", "2026-09-20T23:00:00Z"),
        vid("cutup", "Dak Prescott Highlights", "2026-09-20T12:00:00Z"),
    ]
    clips = build_index(PLAYERS, videos, window=w)["clips"]["3294"]
    assert clips[0]["kind"] == KIND_PLAYER


def test_window_metadata_is_published_on_the_artifact():
    w = window_at(utc(2026, 9, 21, 13))
    idx = build_index(PLAYERS, [vid("v", "Dak Prescott Highlights",
                                    "2026-09-20T22:00:00Z")], window=w)

    assert idx["window_start"].startswith("2026-09-17")
    assert idx["window_end"].startswith("2026-09-21")
    assert idx["window"]["label"] == "Sep 17\u201321"
    assert idx["window"]["grace_hours"] == 36
    assert idx["window"]["week"] == 2
    assert idx["window"]["adjusted_from"] is None
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
    w = window_at(utc(2026, 9, 21, 13))
    inside = build_index(PLAYERS, [vid("a", "Dak Prescott Highlights",
                                       "2026-09-20T22:00:00Z")],
                         window=w)["clips"]["3294"][0]["confidence"]
    outside = build_index(PLAYERS, [vid("b", "Dak Prescott Highlights",
                                        "2026-08-14T22:00:00Z")],
                          window=w)["clips"]["3294"][0]["confidence"]
    assert inside > outside


# --------------------------------------------------------------------------
# window_containing: the empty-slate safety net
# --------------------------------------------------------------------------

def test_window_containing_ignores_the_grace_gate():
    """Sunday's own slate, asked for on that Sunday.

    resolve_game_window() would still be on the previous slate here;
    window_containing answers the different question "which slate does this
    upload belong to".
    """
    w = window_containing(utc(2026, 9, 20, 21), season_start=SEASON_START)
    assert w.label == "Sep 17\u201321"
    assert w.week == 2
    assert window_at(utc(2026, 9, 20, 21)).label == "Sep 10\u201314"


def test_window_containing_brackets_its_own_moment():
    for moment in (utc(2026, 9, 17, 0), utc(2026, 9, 19, 12),
                   utc(2026, 9, 21, 23, 59)):
        w = window_containing(moment, season_start=SEASON_START)
        assert w.start <= moment <= w.end, moment


def test_adjusted_from_is_carried_onto_the_artifact():
    """What the pages read to say "nothing for X, showing Y" honestly."""
    w = window_containing(utc(2026, 9, 20, 21), season_start=SEASON_START)
    w.adjusted_from = "Sep 10\u201314"
    idx = build_index(PLAYERS, [vid("v", "Dak Prescott Highlights",
                                    "2026-09-20T22:00:00Z")], window=w)
    assert idx["window"]["adjusted_from"] == "Sep 10\u201314"
    assert idx["window"]["label"] == "Sep 17\u201321"
    assert idx["stats"]["clips_in_window"] == 1
