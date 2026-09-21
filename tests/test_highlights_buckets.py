"""Week buckets, the relaxed duration floor, and the cut-up/recap split.

Three changes are pinned here, all of them direction changes rather than
refinements, so the tests are written to fail loudly if anyone quietly
restores the previous behaviour.

1. **A week leads only once its last game has been played.** This is a
   different question from "where is the film", which
   ``resolve_game_window`` answers and ``test_highlights_window.py``
   covers. The two deliberately disagree on a Monday.
2. **Duration is a classification input, not a gate.** The 75-second floor
   existed because Shorts broke IFrame playlist playback. Nothing embeds
   any more, so short single-player cut-ups are kept and junk is rejected
   on the title instead.
3. **Game recaps are separated from player cut-ups**, including when a
   recap's title happens to name a player.
"""
from __future__ import annotations

from datetime import datetime, timezone

from dynasty.highlights import (
    KIND_OTHER,
    KIND_PLAYER,
    KIND_TEAM,
    MIN_CLIP_SECONDS,
    RECAP_MIN_SECONDS,
    PlayerRef,
    Video,
    build_index,
    build_name_index,
    classify,
    is_short,
    match_players,
    most_recent_complete_slate,
    slate_start_for,
)


# 2026 week 1 kicks off Thursday Sep 10.
SEASON_START = datetime(2026, 9, 10, tzinfo=timezone.utc)

#: The moment the owner specified this against: Monday 2026-09-21, 12:36
#: America/New_York. Week 2's Monday night game has not kicked off.
OWNER_NOW = datetime(2026, 9, 21, 16, 36, tzinfo=timezone.utc)

PLAYERS = [
    PlayerRef("7564", "Ja'Marr Chase", "WR", "CIN", gsis_id="00-0036900", rank=1),
    PlayerRef("6794", "Tee Higgins", "WR", "CIN", gsis_id="00-0036442", rank=30),
    PlayerRef("9509", "Jahmyr Gibbs", "RB", "DET", gsis_id="00-0039040", rank=4),
    PlayerRef("6803", "Amon-Ra St. Brown", "WR", "DET", gsis_id="00-0036967", rank=8),
]


def vid(video_id, title, **kw):
    kw.setdefault("channel_id", "UCtrusted")
    kw.setdefault("channel_title", "Curtain Call Replays")
    kw.setdefault("published_at", "2026-09-14T14:00:00Z")
    kw.setdefault("duration_seconds", 240)
    return Video(video_id=video_id, title=title, **kw)


def name_index():
    return build_name_index(PLAYERS)


def kind_of(title, **kw):
    v = vid("x", title, **kw)
    return classify(v, match_players(v, name_index()))


# --------------------------------------------------------------------------
# "Most recent complete week" -- the owner's stated rule
# --------------------------------------------------------------------------

def test_monday_lead_week_is_week_one_because_week_two_is_unfinished():
    """The case this was specified against, asserted end to end.

    Monday 2026-09-21. Week 2's slate (Sep 17-21) is the one happening
    around the viewer and already has film indexed, but its Monday night
    game kicks off at 00:15 UTC and has not been played. Week 1 is the
    most recent week that has actually finished, so week 1 leads.
    """
    lead = most_recent_complete_slate(OWNER_NOW, season_start=SEASON_START)
    assert lead.week == 1
    assert lead.label == "Sep 10\u201314"
    assert lead.complete is True


def test_week_two_is_known_and_reported_as_incomplete_not_hidden():
    """An in-progress week is still a real bucket, just not the lead.

    Dropping it would be wrong in the other direction: the film exists and
    the user can see it on YouTube, so the page shows it under the lead
    marked as in progress.
    """
    idx = build_index(
        PLAYERS,
        [vid("w2", "Jahmyr Gibbs Week 2 Highlights | Every Play",
             published_at="2026-09-21T10:00:00Z")],
        as_of=OWNER_NOW, season_start=SEASON_START,
    )
    weeks = {w["week"]: w for w in idx["weeks"]}
    assert weeks[2]["complete"] is False
    assert weeks[2]["lead"] is False
    assert weeks[1]["lead"] is True


def test_lead_advances_once_monday_night_has_finished():
    """Tuesday 06:00 UTC, after MNF ends around 03:30 UTC."""
    tue = datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)
    assert most_recent_complete_slate(tue, season_start=SEASON_START).week == 2


def test_lead_does_not_advance_while_monday_night_is_being_played():
    """23:00 UTC Monday is 19:00 ET -- kickoff is still over an hour away."""
    during = datetime(2026, 9, 21, 23, 0, tzinfo=timezone.utc)
    assert most_recent_complete_slate(during, season_start=SEASON_START).week == 1


def test_sunday_night_does_not_promote_an_unfinished_week():
    sun = datetime(2026, 9, 20, 23, 0, tzinfo=timezone.utc)
    assert most_recent_complete_slate(sun, season_start=SEASON_START).week == 1


# --------------------------------------------------------------------------
# Slate attribution -- which week an upload belongs to
# --------------------------------------------------------------------------

def test_uploads_are_attributed_to_the_slate_they_cover_not_when_posted():
    """The lag matters at both ends of a week.

    A Tuesday upload is Monday night's cut-up and belongs to the week that
    just finished; a Thursday-afternoon upload predates Thursday night's
    kickoff and also belongs to the previous week. Bucketing on the raw
    publish date would split one week's film across two buckets.
    """
    cases = {
        "2026-09-11T06:00:00Z": "2026-09-10",  # TNF cut-up, week 1
        "2026-09-14T20:00:00Z": "2026-09-10",  # Sunday cut-up, week 1
        "2026-09-15T08:00:00Z": "2026-09-10",  # MNF cut-up, still week 1
        "2026-09-17T14:00:00Z": "2026-09-10",  # posted before week 2 kicks off
        "2026-09-18T06:00:00Z": "2026-09-17",  # week 2's TNF cut-up
    }
    for ts, want in cases.items():
        assert slate_start_for(ts).date().isoformat() == want, ts


def test_bucket_week_comes_from_the_timestamp_not_the_title():
    """Channels mistype week numbers; publish timestamps do not.

    A week 1 cut-up mislabelled "Week 7" in its title must still bucket
    into week 1, or one bad title scatters a week's film across the page.
    """
    idx = build_index(
        PLAYERS,
        [vid("t1", "Ja'Marr Chase Week 7 Highlights | Every Target",
             published_at="2026-09-14T14:00:00Z")],
        as_of=OWNER_NOW, season_start=SEASON_START,
    )
    clip = idx["clips"]["7564"][0]
    assert clip["week"] == 7          # what the title claimed
    assert clip["bucket_week"] == 1   # what the timestamp proves


def test_lead_bucket_is_published_even_with_no_film_in_it():
    """An empty lead week must not silently promote a later week.

    This is the failure mode the artifact has already shipped once: an
    empty current window rendered as though another week were current.
    """
    idx = build_index(
        PLAYERS,
        [vid("w2", "Jahmyr Gibbs Week 2 Highlights | Every Play",
             published_at="2026-09-21T10:00:00Z")],
        as_of=OWNER_NOW, season_start=SEASON_START,
    )
    lead = [w for w in idx["weeks"] if w["lead"]]
    assert len(lead) == 1
    assert lead[0]["week"] == 1
    assert lead[0]["clip_count"] == 0
    assert idx["lead_week"] == 1
    assert idx["lead_week_start"] == "2026-09-10"


def test_weeks_are_ordered_newest_first():
    idx = build_index(
        PLAYERS,
        [
            vid("a", "Ja'Marr Chase Highlights | Every Target",
                published_at="2026-09-14T14:00:00Z"),
            vid("b", "Ja'Marr Chase Highlights | Every Target",
                published_at="2026-09-21T10:00:00Z"),
        ],
        as_of=OWNER_NOW, season_start=SEASON_START,
    )
    starts = [w["start_date"] for w in idx["weeks"]]
    assert starts == sorted(starts, reverse=True)


# --------------------------------------------------------------------------
# The relaxed duration floor
# --------------------------------------------------------------------------

def test_floor_dropped_far_enough_to_keep_genuine_short_cutups():
    assert MIN_CLIP_SECONDS <= 20, (
        "the floor exists to exclude stings, not Shorts -- the owner "
        "explicitly wants short individual-player clips"
    )


def test_a_twenty_second_player_clip_is_kept():
    """Exactly what the 75s floor was throwing away."""
    idx = build_index(
        PLAYERS,
        [vid("s1", "Ja'Marr Chase 40 Yard Touchdown vs Vikings",
             duration_seconds=22)],
        as_of=OWNER_NOW, season_start=SEASON_START,
    )
    assert idx["clips"]["7564"][0]["video_id"] == "s1"
    assert idx["stats"]["videos_too_short"] == 0


def test_the_meme_clips_named_by_the_owner_are_still_rejected():
    """Relaxing the floor must not readmit these two.

    Both are real titles from the live index. They are what the duration
    floor was incidentally catching, and they are now rejected on the
    thing that actually makes them wrong -- the title.
    """
    assert kind_of("Taylor Swift is a proud wife after Travis Kelce's td",
                   duration_seconds=19) == KIND_OTHER
    assert kind_of("Fred Warner, one punch man",
                   duration_seconds=14) == KIND_OTHER


def test_meme_rejection_does_not_depend_on_being_short():
    """A padded 3-minute meme montage is still a meme."""
    assert kind_of("Ja'Marr Chase reacts to the win",
                   duration_seconds=200) == KIND_OTHER


def test_sub_floor_micro_clips_are_still_dropped():
    assert is_short(vid("q", "x", duration_seconds=4))
    assert not is_short(vid("q", "x", duration_seconds=MIN_CLIP_SECONDS))


def test_unknown_duration_is_never_treated_as_too_short():
    assert not is_short(vid("q", "x", duration_seconds=None))


# --------------------------------------------------------------------------
# Cut-up vs recap
# --------------------------------------------------------------------------

def test_long_two_team_recap_is_a_recap_even_when_it_names_a_player():
    """The misfile this bifurcation exists to stop.

    Before, any matched player name won outright, so a 17-minute
    Lions/Saints recap was filed as Jahmyr Gibbs' cut-up and rendered
    beside real cut-ups.
    """
    assert kind_of("Jahmyr Gibbs Lions vs Saints | Week 1 Game Highlights",
                   duration_seconds=1080) == KIND_TEAM


def test_recap_length_alone_reclassifies_an_untitled_recap():
    """Channels do not reliably type "Game Highlights"."""
    assert kind_of("Lions vs Saints Week 1", duration_seconds=1150) == KIND_TEAM


def test_an_explicit_cutup_phrase_beats_every_recap_signal():
    """"Every Target and Catch" is never a recap, at any length."""
    assert kind_of(
        "Amon-Ra St. Brown Week 1 Highlights vs Lions | Every Target and Catch",
        duration_seconds=RECAP_MIN_SECONDS + 600,
    ) == KIND_PLAYER


def test_a_cutup_naming_both_teams_is_not_dragged_into_recaps():
    """Title shape carries it: the player leads, the fixture trails."""
    assert kind_of("Ja'Marr Chase Highlights | Bengals vs Vikings Week 3",
                   duration_seconds=165) == KIND_PLAYER


def test_short_form_is_never_a_recap():
    assert kind_of("Bengals vs Vikings Week 3", duration_seconds=45) != KIND_TEAM


def test_week_n_highlights_phrasing_is_not_read_as_a_recap():
    """A regression guard on the marker list.

    "Week 2 Highlights" is how the best cut-up channel in channels.json
    titles its single-player videos, so matching on it would refile the
    most valuable clips in the index as game recaps.
    """
    assert kind_of("Jahmyr Gibbs Week 2 Highlights vs Bears",
                   duration_seconds=300) == KIND_PLAYER


def test_artifact_counts_cutups_and_recaps_separately():
    idx = build_index(
        PLAYERS,
        [
            vid("c1", "Ja'Marr Chase Week 1 Highlights | Every Target",
                duration_seconds=300),
            vid("r1", "Bengals vs Vikings | Week 1 Game Highlights",
                duration_seconds=1080),
        ],
        as_of=OWNER_NOW, season_start=SEASON_START,
    )
    assert idx["stats"]["clips_player_cutup"] >= 1
    assert idx["stats"]["clips_game_recap"] >= 1


# --------------------------------------------------------------------------
# Quality signals
# --------------------------------------------------------------------------

def test_curated_channel_raises_confidence():
    trusted = build_index(
        PLAYERS, [vid("t", "Ja'Marr Chase Week 1 Highlights | Every Target")],
        trusted_channel_ids={"UCtrusted"},
        as_of=OWNER_NOW, season_start=SEASON_START,
    )["clips"]["7564"][0]
    unknown = build_index(
        PLAYERS, [vid("u", "Ja'Marr Chase Week 1 Highlights | Every Target",
                      channel_id="UCrandom")],
        trusted_channel_ids={"UCtrusted"},
        as_of=OWNER_NOW, season_start=SEASON_START,
    )["clips"]["7564"][0]

    assert trusted["confidence"] > unknown["confidence"]
    assert trusted["trusted"] is True
    # Absent rather than false, so the artifact does not carry a flag on
    # every clip from an uncurated channel.
    assert "trusted" not in unknown


def test_view_count_is_carried_through_to_the_artifact():
    idx = build_index(
        PLAYERS,
        [vid("v", "Ja'Marr Chase Week 1 Highlights | Every Target",
             view_count=12345)],
        as_of=OWNER_NOW, season_start=SEASON_START,
    )
    assert idx["clips"]["7564"][0]["view_count"] == 12345


def test_view_count_breaks_ties_but_never_outranks_the_right_player():
    """The explicit instruction: a secondary signal, not a filter.

    A wildly popular recap must still sort below this player's own
    low-view cut-up, because being the right player's film matters more
    than being popular film.
    """
    idx = build_index(
        PLAYERS,
        [
            vid("popular-recap", "Bengals vs Vikings | Week 1 Game Highlights",
                duration_seconds=1080, view_count=5_000_000),
            vid("quiet-cutup", "Ja'Marr Chase Week 1 Highlights | Every Target",
                duration_seconds=300, view_count=400),
        ],
        as_of=OWNER_NOW, season_start=SEASON_START,
    )
    assert idx["clips"]["7564"][0]["video_id"] == "quiet-cutup"


def test_view_count_orders_two_otherwise_equal_cutups():
    idx = build_index(
        PLAYERS,
        [
            vid("quiet", "Ja'Marr Chase Week 1 Highlights | Every Target",
                view_count=10),
            vid("loud", "Ja'Marr Chase Week 1 Highlights | Every Target",
                view_count=900_000),
        ],
        as_of=OWNER_NOW, season_start=SEASON_START,
    )
    ids = [c["video_id"] for c in idx["clips"]["7564"]]
    assert ids.index("loud") < ids.index("quiet")


def test_a_clip_with_no_view_count_still_indexes():
    """viewCount is absent when an uploader hides it."""
    idx = build_index(
        PLAYERS,
        [vid("n", "Ja'Marr Chase Week 1 Highlights | Every Target",
             view_count=None)],
        as_of=OWNER_NOW, season_start=SEASON_START,
    )
    assert "view_count" not in idx["clips"]["7564"][0]
