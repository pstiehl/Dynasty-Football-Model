"""YouTube highlight index — match uploaded clips to canonical players.

Why this exists
---------------
The roster reel ("watch my whole team's week in 9 minutes") needs, for every
rostered player, an ordered list of embeddable YouTube video IDs from their
most recent game. Doing that with ``search.list`` is impossible: it costs 100
quota units per query against a 10,000/day default, so a single 15-player
roster view would burn 1,500 units. One user, ten page loads, quota gone.

So we invert it. A scheduled job walks the *uploads playlist* of a curated set
of highlight channels (``playlistItems.list``, 1 unit per 50 videos), then
matches video titles to players offline. A few thousand videos a week costs
double-digit quota units, and the site reads a static JSON artifact.

This module is the offline half — parsing and matching. It deliberately has no
network dependency so it can be unit-tested without an API key. The fetching
half lives in ``scripts/refresh_highlights.py``.

Matching strategy
-----------------
Video titles are messy but formulaic:

    "Ja'Marr Chase Highlights Week 3 vs Vikings"
    "Bijan Robinson EVERY Touch | Week 2 2026"
    "Amon-Ra St. Brown | Week 1 Highlights"
    "Cowboys vs Eagles Game Highlights | 2026 NFL Week 1"

We tokenize the title, slide an n-gram window (n=2..4) over it, normalize each
window with ``dynasty.names.normalize`` (the same function the source-sync
layer uses for duplicate detection) and look it up in a name index built from
the canonical player table. That reuses the existing suffix/diacritic/period
folding, so "Amon-Ra St. Brown" and "Amon-Ra St.Brown" collapse to one key.

Ambiguous names (two active "Josh Allen"s, "Michael Thomas", "Chris Jones")
are resolved by looking for the player's NFL team nickname elsewhere in the
title, then by preferring the higher-ranked player in the model. If neither
disambiguates, the match is dropped rather than guessed — a wrong clip is much
worse than a missing one, because it silently shows the user another player's
game.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence

from .names import normalize as normalize_name


# --------------------------------------------------------------------------
# Clip taxonomy
# --------------------------------------------------------------------------

#: A cut-up of one player's touches/targets. The thing we actually want.
KIND_PLAYER = "player_cutup"
#: A full-game or team recap. Usable as a fallback when a player has no cut-up.
KIND_TEAM = "team_game"
#: Matched a player but the title doesn't look like game highlights (podcast,
#: interview, press conference, mock draft). Kept out of the reel by default.
KIND_OTHER = "other"


# Phrases that mark a genuine highlight cut-up. Presence bumps confidence;
# absence doesn't disqualify (plenty of channels just title it "Week 3").
_CUTUP_MARKERS = (
    "highlights", "every touch", "every target", "every catch", "every carry",
    "every throw", "every play", "all touches", "all targets", "film room",
    "route running", "full game", "game highlights",
)

# Micro-clips.
#
# HISTORY, because this number moved for a specific reason. It was 75s,
# set when the reel played clips back to back inside a YouTube IFrame
# *playlist*: Shorts report ``status.embeddable: true`` but still fail in
# a playlist with "An error occurred. Please try again later", so the only
# reliable defence was to refuse anything short enough to be a Short.
#
# The reel no longer embeds anything - it links out to YouTube, where
# Shorts play perfectly well. The playback reason for the floor is gone,
# and it was costing us the clips the reel is most wanted for: a live run
# dropped 167 videos as "Shorts", among them genuine single-player
# cut-ups that simply run 40-70 seconds.
#
# What survives is a floor low enough to exclude only things no highlight
# can be - a 12-second sting, an intro bumper, a reaction gif with audio.
# Everything above it is kept and *classified* on duration rather than
# gated by it. Junk in the 15-75s band is now rejected on what the title
# says (``_MEME_MARKERS``), which is the honest test: a 19-second clip of
# a player's only touch of the game is a highlight, and a padded 90-second
# meme montage never was.
MIN_CLIP_SECONDS = 15

#: Duration bands used as *classification* input (see :func:`classify`).
#: Neither one drops a video.
#:
#: At or under this, a clip is short-form: one play, one touch, one
#: sequence. Structurally incapable of being a 15-minute game recap, so
#: this protects genuine cut-ups from the recap heuristics below.
SHORT_FORM_MAX_SECONDS = 180
#: At or over this, a multi-team video is a game recap rather than one
#: player's cut-up. Condensed games run 8-12 minutes; full recaps 15-20.
#: A single player's week almost never yields seven minutes of film.
RECAP_MIN_SECONDS = 420

# Reaction/meme uploads that name a player but show a moment, not a game.
# Matched on the title, which is now the *primary* junk filter rather than
# a supplement to the duration floor - lowering that floor to 15s is what
# makes this list load-bearing.
#
# Deliberately phrasings rather than single words, so "Chiefs react to the
# win" is dropped and "Reception" is untouched. The first block is what the
# live index was actually caught carrying; the second generalises the same
# shapes (celebration, crowd, sideline, off-field) without reaching so far
# that it eats football language.
_MEME_MARKERS = (
    # Observed in the live index.
    "is a proud", "was hype", "one punch man", "locked in",
    "showing how it", "heard the chatter", "with the perfect call",
    "calm fist pump", "proud wife", "reacts to", "reaction to",
    "caught on mic", "mic'd up", "micd up",
    # Same shapes, generalised.
    "is hyped", "goes crazy", "can't believe", "cant believe",
    "loses it", "lost it after", "savage", "trash talk", "trolls",
    "shades", "claps back", "fires back at", "hilarious", "funny moment",
    "best moments off", "dance", "celebration compilation", "tunnel walk",
    "pregame outfit", "arrives in style", "wholesome", "emotional moment",
    "crowd goes", "fan reaction", "sideline moment", "caught cursing",
    "gets emotional", "speechless", "is a vibe", "unreal moment",
)

# Phrases that mean the video is a full-game or team recap, whoever it
# happens to name. Checked before the "a player was matched" branch so a
# recap whose description-derived title mentions a star does not get filed
# as that star's cut-up. Specific multi-word shapes only: bare
# "highlights" is what cut-ups are called too.
_RECAP_MARKERS = (
    "game highlights", "full game", "game recap", "condensed game",
    "full highlights", "every play of the game", "all scoring plays",
    "first half highlights", "second half highlights",
    "overtime highlights", "final drive",
)
# Deliberately NOT here: "week N highlights" and "highlights week N".
# Cut-up channels title their single-player videos exactly that way
# ("Amon-Ra St. Brown Week 2 Highlights vs Bills"), so matching on it
# would refile the best clips in the index as game recaps.

# Phrases that can only describe one player's cut-up. Presence vetoes the
# recap heuristics entirely - "Every Target and Catch" is never a recap,
# however long it runs or however many teams the title names.
_CUTUP_STRONG_MARKERS = (
    "every touch", "every target", "every catch", "every carry",
    "every throw", "every play", "every reception", "every rush",
    "all touches", "all targets", "all catches", "all carries",
    "route running", "film room", "every snap",
)

# Phrases that mean this is talk, not football. Strong negative signal -
# a "Ja'Marr Chase Trade Rumors" video in a reel is a bad experience.
_NON_GAME_MARKERS = (
    "podcast", "interview", "press conference", "presser", "mock draft",
    "trade rumors", "fantasy advice", "start or sit", "start/sit", "waiver",
    "reaction", "breakdown show", "top 10", "top ten", "career highlights",
    "college highlights", "madden", "rankings", "preview", "predictions",
)

_WEEK_RE = re.compile(r"\bweek\s*[#]?\s*(\d{1,2})\b", re.IGNORECASE)
_SEASON_RE = re.compile(r"\b(20\d{2})\b")
_TOKEN_RE = re.compile(r"[^a-z0-9]+")

# NFL team nicknames -> team abbreviation, for disambiguation and opponent
# detection. Keyed on the lowercase nickname as it appears in titles.
TEAM_NICKNAMES: Dict[str, str] = {
    "cardinals": "ARI", "falcons": "ATL", "ravens": "BAL", "bills": "BUF",
    "panthers": "CAR", "bears": "CHI", "bengals": "CIN", "browns": "CLE",
    "cowboys": "DAL", "broncos": "DEN", "lions": "DET", "packers": "GB",
    "texans": "HOU", "colts": "IND", "jaguars": "JAX", "jags": "JAX",
    "chiefs": "KC", "raiders": "LV", "chargers": "LAC", "rams": "LAR",
    "dolphins": "MIA", "vikings": "MIN", "patriots": "NE", "pats": "NE",
    "saints": "NO", "giants": "NYG", "jets": "NYJ", "eagles": "PHI",
    "steelers": "PIT", "49ers": "SF", "niners": "SF", "seahawks": "SEA",
    "buccaneers": "TB", "bucs": "TB", "titans": "TEN", "commanders": "WAS",
}


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class PlayerRef:
    """The canonical-player fields matching needs.

    Kept as a plain dataclass rather than the SQLAlchemy ``Player`` model so
    this module stays importable (and testable) without a database.
    """
    sleeper_id: str
    name: str
    position: Optional[str] = None
    team: Optional[str] = None
    gsis_id: Optional[str] = None
    #: Model overall rank, when the player is in the engine output. Used for
    #: reel ordering and to break name ties toward the more relevant player.
    rank: Optional[int] = None


@dataclass
class Video:
    """One upload, as returned by ``playlistItems.list`` + ``videos.list``."""
    video_id: str
    title: str
    channel_id: str
    channel_title: str
    published_at: str            # ISO-8601
    duration_seconds: Optional[int] = None
    #: Retained from ``status.embeddable`` for provenance, but no longer a
    #: reason to drop anything: the pages link out to YouTube instead of
    #: embedding, so a video the uploader blocked from embedding plays
    #: perfectly well on youtube.com. Dropping these was costing us clips
    #: for no remaining benefit.
    embeddable: bool = True
    #: ``statistics.viewCount`` from the same ``videos.list`` call that
    #: supplies duration. A secondary ranking signal, never a filter - the
    #: only cut-up of your TE3's two targets is the right clip for him at
    #: 400 views.
    view_count: Optional[int] = None
    #: True when the upload came from a channel curated in
    #: ``data/highlights/channels.json``. Those channels were each
    #: inspected by hand, so provenance is a real quality signal.
    trusted_channel: bool = False


@dataclass
class Clip:
    """A video matched to a player, with everything the UI needs to label it."""
    video_id: str
    title: str
    channel_title: str
    published_at: str
    duration_seconds: Optional[int]
    kind: str
    confidence: float
    week: Optional[int] = None
    season: Optional[int] = None
    opponent: Optional[str] = None
    #: True when ``published_at`` falls inside the resolved game window.
    #: ``None`` when the index was built without a window at all, which is
    #: how a consumer tells "no window was applied" apart from "this clip
    #: is outside the window".
    in_window: Optional[bool] = None
    #: NFL week of the *slate this upload covers*, derived from
    #: ``published_at`` (see :func:`slate_start_for`) - not from the title.
    #: This is what the week buckets group on.
    bucket_week: Optional[int] = None
    #: ISO date of that slate's opening Thursday. Groups uploads even in a
    #: preseason/postseason stretch where no week number can be derived.
    bucket_start: Optional[str] = None
    #: ``statistics.viewCount``, carried through for secondary ranking.
    view_count: Optional[int] = None
    #: True when the source channel is curated. Omitted when it is not, so
    #: the artifact does not grow a false flag on every untrusted clip.
    trusted: Optional[bool] = None

    def to_json(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}


# --------------------------------------------------------------------------
# Game window
# --------------------------------------------------------------------------
#
# Why a date window rather than a week number
# -------------------------------------------
# The original build derived an NFL week number from a season-start constant
# and compared it against a week parsed out of the video title. Both halves
# are unreliable:
#
#   * The derived number counts the week *currently in progress*. Asked on
#     Monday 2026-09-21 it returns 2, because 11 days have elapsed since the
#     Sep 10 kickoff -- but week 2's Monday night game has not been played,
#     let alone cut up and uploaded. The viewer wants week 1's film.
#   * The parsed number is absent from a large share of uploads and, where
#     present, is whatever the channel decided to type.
#
# So selection and ordering key off publish timestamps inside a rolling
# Thursday->Monday window instead, and the title's week survives only as a
# label and a last-resort tiebreak.

#: Thursday..Monday inclusive. Overridable so an unusual slate can be
#: modelled without a code change.
DEFAULT_WINDOW_DAYS = 5

#: Grace period, measured from the start of the slate's **Sunday** -- the
#: main slate, after which the overwhelming majority of a week's film
#: exists. With the default that puts the changeover at Monday 12:00 UTC
#: (08:00 ET).
#:
#: This anchor was corrected against the live index. An earlier revision
#: measured from the start of the slate's *final* day (Monday), which put
#: the changeover at Tuesday 12:00 UTC. Checked against the real
#: highlights.json of 2026-09-21T13:08Z that was wrong in the worst way:
#: every one of its 28 videos was published Sep 20-21, so a window of
#: Sep 10-14 matched 0 of 737 clip rows and the reel would have gone
#: completely blank on a Monday morning -- while yesterday's Sunday film
#: sat in the index unshown.
#:
#: Sunday + 36h satisfies both constraints instead:
#:
#:   * Thursday through Sunday, a viewer still gets the previous week's
#:     finished slate rather than one that has barely started -- which is
#:     the actual point of the grace period.
#:   * From Monday 08:00 ET the current slate takes over, by which point
#:     Sunday's late games (ending ~03:30 UTC) have been cut up and posted.
#:     Verified: 21 of the 28 live videos were published on the Sunday
#:     itself.
DEFAULT_GRACE_HOURS = 36

_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def parse_ts(value: Optional[str]) -> Optional[datetime]:
    """ISO-8601 (trailing ``Z`` or an offset) -> aware UTC datetime."""
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


@dataclass
class GameWindow:
    """The most recently *completed* Thursday->Monday slate.

    Three boundaries, because they answer three different questions:

    ``start``/``end``
        The slate itself -- Thursday 00:00 UTC to the last instant of
        Monday. This is what :attr:`label` renders and what the pages show.
    ``publish_cutoff``
        The newest ``published_at`` still counted as part of this slate.
        Monday night football ends around 03:30 UTC on Tuesday and its
        cut-up lands later that morning, so this runs past ``end``.
    ``opens_at``
        When this window became *the* window: the start of its Sunday plus
        the grace period. See :func:`resolve_game_window`.
    """
    start: datetime
    end: datetime
    publish_cutoff: datetime
    opens_at: Optional[datetime] = None
    grace_hours: float = DEFAULT_GRACE_HOURS
    window_days: int = DEFAULT_WINDOW_DAYS
    #: Advisory NFL week number for the slate, when a season start is known.
    #: A label only -- nothing selects or filters on it.
    week: Optional[int] = None
    #: Label of the window this one replaced, when the build had to fall
    #: back because the properly-resolved slate contained no film at all.
    #: ``None`` on a normal resolve. Set it and the pages say so rather
    #: than quietly presenting a different week as the current one.
    adjusted_from: Optional[str] = None

    @property
    def label(self) -> str:
        """``"Sep 10-14"``, or ``"Sep 30-Oct 4"`` across a month boundary."""
        s, e = self.start, self.end
        left = f"{_MONTH_ABBR[s.month - 1]} {s.day}"
        right = (f"{e.day}" if (s.month, s.year) == (e.month, e.year)
                 else f"{_MONTH_ABBR[e.month - 1]} {e.day}")
        return f"{left}\u2013{right}"

    def contains(self, published_at: Optional[str]) -> bool:
        ts = parse_ts(published_at)
        if ts is None:
            # An upload with no usable timestamp cannot be proven recent.
            # Calling it in-window would let undated clips outrank genuine
            # ones from the slate.
            return False
        return self.start <= ts <= self.publish_cutoff

    def to_json(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "publish_cutoff": self.publish_cutoff.isoformat(),
            "opens_at": self.opens_at.isoformat() if self.opens_at else None,
            "grace_hours": self.grace_hours,
            "window_days": self.window_days,
            "label": self.label,
            "week": self.week,
            "adjusted_from": self.adjusted_from,
        }


def week_number_for(
    window_start: datetime,
    season_start: Optional[datetime],
) -> Optional[int]:
    """Advisory NFL week for a window, or ``None`` without a season start.

    Both arguments are week-opening Thursdays, so this is exact -- unlike
    dividing "days since kickoff" by seven partway through a week, which
    returns the in-progress week and was the original off-by-one.
    """
    if season_start is None:
        return None
    if season_start.tzinfo is None:
        season_start = season_start.replace(tzinfo=timezone.utc)
    days = (window_start.date()
            - season_start.astimezone(timezone.utc).date()).days
    if days < 0:
        return None
    week = (days // 7) + 1
    return week if 1 <= week <= 18 else None


def window_containing(
    moment: datetime,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    grace_hours: float = DEFAULT_GRACE_HOURS,
    season_start: Optional[datetime] = None,
) -> GameWindow:
    """The slate window that ``moment`` falls in, ignoring the grace gate.

    :func:`resolve_game_window` answers "which slate should we be showing";
    this answers "which slate does this upload belong to". The build needs
    the second one as a safety net.

    Why the net is needed: the ingest is capped by ``--max-per-channel``
    (200) rather than purely by date, and the one configured channel posts
    mostly Shorts. On the 2026-09-21 index, 206 uploads were fetched, 163
    were dropped as Shorts, and the 28 survivors spanned barely two days.
    A correctly-resolved completed slate can therefore legitimately contain
    no film at all, and an empty default reel is a worse answer than
    clearly-labelled film from the slate that does have some.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)

    window_days = max(1, int(window_days))
    grace = timedelta(hours=float(grace_hours))
    thursday = (moment - timedelta(days=(moment.weekday() - 3) % 7)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end = thursday + timedelta(days=window_days) - timedelta(seconds=1)
    return GameWindow(
        start=thursday,
        end=end,
        publish_cutoff=end + grace,
        opens_at=thursday + timedelta(days=max(0, window_days - 2)) + grace,
        grace_hours=float(grace_hours),
        window_days=window_days,
        week=week_number_for(thursday, season_start),
    )


def resolve_game_window(
    as_of: Optional[datetime] = None,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    grace_hours: float = DEFAULT_GRACE_HOURS,
    season_start: Optional[datetime] = None,
) -> GameWindow:
    """The most recent slate whose main Sunday card has been played.

    A slate "opens" ``grace_hours`` after the start of its Sunday, so with
    the defaults it becomes current at Monday 12:00 UTC. The function walks
    back from the Thursday on or before ``as_of`` until it finds one that
    has opened.

    Worked example -- Monday 2026-09-21 13:00 UTC::

        candidate Thu 09-17  Sunday starts 09-20 00:00
                             opens Mon 09-21 12:00   (past, take it)
                             -> Sep 17-21, week 2

    That is the slate whose film actually exists: on the live index for
    this date all 28 videos were published Sep 20-21 and all 737 clip rows
    fell inside this window. Earlier in the weekend the same call returns
    the *previous* slate, because the current one has barely been played::

        Sun 09-20 23:00 UTC  -> Sep 10-14 (week 1)
        Mon 09-21 11:00 UTC  -> Sep 10-14 (week 1)
        Mon 09-21 12:00 UTC  -> Sep 17-21 (week 2)   <- changeover

    Nothing here is pinned to a particular week number: the week is only a
    label derived from ``season_start``, and the arithmetic is pure date
    work against ``as_of``.
    """
    as_of = (as_of or datetime.now(timezone.utc))
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    as_of = as_of.astimezone(timezone.utc)

    window_days = max(1, int(window_days))
    grace = timedelta(hours=float(grace_hours))

    # Monday is 0, Thursday is 3; on a Thursday this keeps the same day.
    thursday = (as_of - timedelta(days=(as_of.weekday() - 3) % 7)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    # Grace runs from the start of the slate's Sunday -- day 4 of a
    # Thursday-opening window. Sunday carries almost all of a week's games,
    # so that is when the clock on "have the channels posted yet" really
    # starts. Waiting for the final day (Monday) instead delayed the
    # changeover by a full 24h and blanked the reel; see
    # DEFAULT_GRACE_HOURS.
    def opens(thu: datetime) -> datetime:
        return thu + timedelta(days=max(0, window_days - 2)) + grace

    # One step back is enough for the default grace; the bound only stops a
    # pathological grace period from spinning.
    for _ in range(12):
        if opens(thursday) <= as_of:
            break
        thursday -= timedelta(days=7)

    end = thursday + timedelta(days=window_days) - timedelta(seconds=1)
    return GameWindow(
        start=thursday,
        end=end,
        publish_cutoff=end + grace,
        opens_at=opens(thursday),
        grace_hours=float(grace_hours),
        window_days=window_days,
        week=week_number_for(thursday, season_start),
    )


# --------------------------------------------------------------------------
# Week buckets
# --------------------------------------------------------------------------
#
# Completeness is not availability
# --------------------------------
# ``resolve_game_window`` answers "which slate should the reel default to
# so that film actually exists for it". Its grace period is tuned to when
# the cut-up channels have finished *posting*.
#
# The week buckets answer a different question, and the owner was explicit
# about it: a week is eligible to lead only once its final game has been
# *played*. Those two questions have different answers on exactly the day
# this was specified. Monday 2026-09-21 16:36 UTC:
#
#   * Film availability says the Sep 17-21 slate is where the uploads are.
#   * Completeness says Sep 17-21 is still in progress - Monday night
#     football kicks off at 00:15 UTC and ends around 03:30 UTC - so the
#     most recent *complete* week is Sep 10-14, week 1.
#
# Both are right about their own question. This section deliberately does
# not touch the window anchor; it adds the completeness rule alongside it,
# and the bucket list is what the pages group and order on.

#: Hours after the start of a slate's final day (Monday 00:00 UTC) before
#: that slate counts as complete. Monday night football ends around 03:30
#: UTC on Tuesday, so 30h - Tuesday 06:00 UTC - clears it with margin
#: without waiting so long that Tuesday's viewers are stuck a week back.
COMPLETION_HOURS_AFTER_FINAL_DAY = 30.0

#: A slate's film is attributed by publish time, and uploads lag the games
#: they cover. Thursday night's game kicks off at 00:15 UTC Friday, so
#: anything published before Friday 00:00 UTC is covering the *previous*
#: slate. Shifting back a day before locating the slate's Thursday puts
#: Tuesday and Wednesday clean-up uploads in the week they belong to.
SLATE_ATTRIBUTION_LAG_HOURS = 24.0


def _thursday_on_or_before(ts: datetime) -> datetime:
    """Midnight UTC on the Thursday at or before ``ts``."""
    return (ts - timedelta(days=(ts.weekday() - 3) % 7)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def slate_start_for(
    published_at: Optional[str],
    *,
    lag_hours: float = SLATE_ATTRIBUTION_LAG_HOURS,
) -> Optional[datetime]:
    """The opening Thursday of the slate an upload covers, or ``None``.

    Attribution is by publish timestamp, never by the week number in the
    title: the title's week is absent from a large share of uploads and
    wrong in some of the rest, and bucketing on it would scatter a week's
    film across the page.

    ``lag_hours`` shifts the timestamp back before the Thursday is found,
    so an upload's slate is the one whose games it can actually show::

        Fri 09-18 06:00  (TNF cut-up)      -> Thu 09-17   week 2
        Mon 09-14 20:00  (Sunday cut-up)   -> Thu 09-10   week 1
        Tue 09-15 08:00  (MNF cut-up)      -> Thu 09-10   week 1
        Thu 09-17 14:00  (pre-TNF upload)  -> Thu 09-10   week 1
    """
    ts = parse_ts(published_at)
    if ts is None:
        return None
    return _thursday_on_or_before(ts - timedelta(hours=float(lag_hours)))


@dataclass
class Slate:
    """One Thursday->Monday week, as a grouping unit for the UI."""
    start: datetime
    end: datetime
    week: Optional[int] = None
    #: When this slate's final game finishes. Past ``as_of`` -> complete.
    complete_at: Optional[datetime] = None
    complete: bool = False
    #: How many clip rows landed in this bucket. Set by
    #: :func:`build_week_buckets`; 0 for a lead slate with no film yet.
    clip_count: int = 0
    #: True for the single bucket the pages open on.
    lead: bool = False

    @property
    def label(self) -> str:
        """``"Sep 10-14"``, matching :attr:`GameWindow.label`."""
        s, e = self.start, self.end
        left = f"{_MONTH_ABBR[s.month - 1]} {s.day}"
        right = (f"{e.day}" if (s.month, s.year) == (e.month, e.year)
                 else f"{_MONTH_ABBR[e.month - 1]} {e.day}")
        return f"{left}\u2013{right}"

    def to_json(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "start_date": self.start.date().isoformat(),
            "label": self.label,
            "week": self.week,
            "complete": self.complete,
            "complete_at": (self.complete_at.isoformat()
                            if self.complete_at else None),
            "clip_count": self.clip_count,
            "lead": self.lead,
        }


def slate_for_start(
    start: datetime,
    *,
    as_of: datetime,
    window_days: int = DEFAULT_WINDOW_DAYS,
    season_start: Optional[datetime] = None,
    completion_hours: float = COMPLETION_HOURS_AFTER_FINAL_DAY,
) -> Slate:
    """Build a :class:`Slate` for a known opening Thursday."""
    window_days = max(1, int(window_days))
    end = start + timedelta(days=window_days) - timedelta(seconds=1)
    complete_at = (start + timedelta(days=window_days - 1)
                   + timedelta(hours=float(completion_hours)))
    return Slate(
        start=start,
        end=end,
        week=week_number_for(start, season_start),
        complete_at=complete_at,
        complete=complete_at <= as_of,
    )


def most_recent_complete_slate(
    as_of: Optional[datetime] = None,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    season_start: Optional[datetime] = None,
    completion_hours: float = COMPLETION_HOURS_AFTER_FINAL_DAY,
) -> Slate:
    """The newest slate whose final game has been played.

    **This is the single source of truth for "most recent complete week".**
    The artifact publishes its result and both pages read it from there,
    so the reel and My Team cannot drift apart or re-derive it in JS from
    the viewer's local clock.

    Worked example - Monday 2026-09-21 16:36 UTC, the case this exists
    for::

        candidate Thu 09-17 (week 2)  completes Tue 09-22 06:00  future
        candidate Thu 09-10 (week 1)  completes Tue 09-15 06:00  past
                                      -> lead bucket Sep 10-14, week 1

    Week 2's Monday night game has not kicked off yet, so week 2 is not
    eligible to lead however much week 2 film is already indexed.
    """
    as_of = (as_of or datetime.now(timezone.utc))
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    as_of = as_of.astimezone(timezone.utc)

    thursday = _thursday_on_or_before(as_of)
    for _ in range(12):
        slate = slate_for_start(
            thursday, as_of=as_of, window_days=window_days,
            season_start=season_start, completion_hours=completion_hours,
        )
        if slate.complete:
            return slate
        thursday -= timedelta(days=7)
    return slate_for_start(
        thursday, as_of=as_of, window_days=window_days,
        season_start=season_start, completion_hours=completion_hours,
    )


# --------------------------------------------------------------------------
# Title parsing
# --------------------------------------------------------------------------

def _fold(text: str) -> str:
    """Lowercase + strip diacritics. Mirrors names.normalize's folding."""
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.lower()


def tokenize(title: str) -> List[str]:
    """Split a title into lowercase alphanumeric tokens.

    Hyphens and periods become separators, so "Amon-Ra St. Brown" yields
    ``["amon", "ra", "st", "brown"]``. The n-gram scan rejoins them with
    spaces and hands the result to ``names.normalize``, which produces the
    same key as the canonical "Amon-Ra St. Brown" does.
    """
    return [t for t in _TOKEN_RE.split(_fold(title)) if t]


def match_key(text: str) -> Optional[str]:
    """Canonical key used on BOTH sides of the name match.

    ``names.normalize`` folds diacritics, strips generational suffixes and
    drops periods, but it deliberately keeps apostrophes and hyphens because
    it exists for cross-source player dedupe, where those characters are
    stable. Titles are not stable: "Ja'Marr Chase" shows up as "JaMarr Chase"
    and "Ja Marr Chase", "Amon-Ra" as "Amon Ra".

    So we tokenize first (which drops every non-alphanumeric character), then
    hand the space-joined result to ``normalize`` for the suffix stripping.
    Running the canonical roster name through the identical pipeline means
    "Kenneth Walker III" and a title's "Kenneth Walker" land on one key.
    """
    return normalize_name(" ".join(tokenize(text)))


def parse_week(title: str) -> Optional[int]:
    m = _WEEK_RE.search(title)
    if not m:
        return None
    wk = int(m.group(1))
    return wk if 1 <= wk <= 23 else None


def parse_season(title: str) -> Optional[int]:
    """Pull a plausible NFL season year out of the title."""
    best = None
    for m in _SEASON_RE.finditer(title):
        yr = int(m.group(1))
        if 2000 <= yr <= 2100:
            best = yr
    return best


def find_teams(title: str) -> List[str]:
    """Team abbreviations whose nickname appears in the title, in order."""
    tokens = tokenize(title)
    seen: List[str] = []
    for tok in tokens:
        abbr = TEAM_NICKNAMES.get(tok)
        if abbr and abbr not in seen:
            seen.append(abbr)
    return seen


def _has_any(title_folded: str, markers: Sequence[str]) -> bool:
    return any(m in title_folded for m in markers)


# --------------------------------------------------------------------------
# Name index + matching
# --------------------------------------------------------------------------

def build_name_index(players: Iterable[PlayerRef]) -> Dict[str, List[PlayerRef]]:
    """Map normalized name -> players sharing that name.

    A list, not a single player: "Josh Allen" and "Michael Thomas" genuinely
    collide across active rosters, and silently picking one is how you end up
    showing a linebacker's clips on a quarterback's page.
    """
    index: Dict[str, List[PlayerRef]] = {}
    for p in players:
        key = match_key(p.name)
        if not key:
            continue
        index.setdefault(key, []).append(p)
    return index


def _disambiguate(
    candidates: List[PlayerRef],
    teams_in_title: Sequence[str],
) -> Optional[PlayerRef]:
    """Pick one player from a same-name collision, or None if we can't."""
    if len(candidates) == 1:
        return candidates[0]

    # 1. Team named in the title matches exactly one candidate's team.
    if teams_in_title:
        on_team = [c for c in candidates if c.team and c.team in teams_in_title]
        if len(on_team) == 1:
            return on_team[0]

    # 2. Exactly one candidate is ranked by the model. Highlight channels cut
    #    up fantasy-relevant skill players, so the ranked one is nearly always
    #    the subject.
    ranked = [c for c in candidates if c.rank is not None]
    if len(ranked) == 1:
        return ranked[0]

    # 3. Multiple ranked candidates: only resolve if one is clearly the more
    #    prominent player. A near-tie is a coin flip, and a coin flip here
    #    means showing someone the wrong player's game.
    if len(ranked) > 1:
        ranked.sort(key=lambda c: c.rank or 10**6)
        if (ranked[1].rank or 10**6) - (ranked[0].rank or 0) >= 50:
            return ranked[0]

    return None


def match_players_with_ambiguity(
    video: Video,
    name_index: Dict[str, List[PlayerRef]],
    max_ngram: int = 4,
) -> "tuple[List[PlayerRef], int]":
    """Return ``(players named in the title, unresolved-collision count)``.

    Scans n-grams longest-first so "Amon-Ra St. Brown" wins over a spurious
    shorter match inside it, and so a title naming two players ("Chase &
    Higgins Week 3") produces both.

    The second element counts name spans that hit the index but that
    :func:`_disambiguate` refused to resolve. Callers need it because
    "we could not tell which player this is" and "this title names nobody
    we track" are different failures with different fixes, and the first
    is otherwise invisible — see :func:`build_index`.
    """
    tokens = tokenize(video.title)
    teams = find_teams(video.title)
    matched: List[PlayerRef] = []
    seen_ids: set = set()
    consumed: set = set()   # token positions already claimed by a longer match
    ambiguous = 0

    for n in range(max_ngram, 1, -1):
        for i in range(0, max(0, len(tokens) - n + 1)):
            span = set(range(i, i + n))
            if span & consumed:
                continue
            key = match_key(" ".join(tokens[i:i + n]))
            if not key:
                continue
            candidates = name_index.get(key)
            if not candidates:
                continue
            pick = _disambiguate(candidates, teams)
            if pick is None:
                # Ambiguous: burn the tokens so a shorter sub-span doesn't
                # produce an even worse match, but record nothing.
                ambiguous += 1
                consumed |= span
                continue
            if pick.sleeper_id not in seen_ids:
                matched.append(pick)
                seen_ids.add(pick.sleeper_id)
            consumed |= span

    return matched, ambiguous


def match_players(
    video: Video,
    name_index: Dict[str, List[PlayerRef]],
    max_ngram: int = 4,
) -> List[PlayerRef]:
    """Every canonical player named in the video title.

    Thin wrapper over :func:`match_players_with_ambiguity` for callers that
    only care about the matches.
    """
    return match_players_with_ambiguity(video, name_index, max_ngram)[0]


# --------------------------------------------------------------------------
# Classification + scoring
# --------------------------------------------------------------------------

def is_short(video: Video, min_seconds: int = MIN_CLIP_SECONDS) -> bool:
    """True only for sub-:data:`MIN_CLIP_SECONDS` micro-clips.

    This used to mean "is a YouTube Short", because Shorts broke IFrame
    playlist playback. Nothing embeds any more, so the floor is now just a
    sanity bound below which no highlight can exist. Shorts themselves are
    kept: see :data:`MIN_CLIP_SECONDS`.

    Unknown duration is *not* treated as too short: the duration comes
    from a separate ``videos.list`` call that can legitimately be missing,
    and discarding everything unenriched would silently empty the index.
    """
    dur = video.duration_seconds
    if dur is None:
        return False
    return dur < min_seconds


def _title_leads_with_player(
    video: Video, matched: Sequence[PlayerRef]
) -> bool:
    """True when a matched player's surname opens the title.

    Title *shape*, which is what separates the two kinds far more reliably
    than any single keyword. Cut-up channels lead with the subject -
    "Jahmyr Gibbs Week 1 Highlights vs Saints" - while recaps lead with
    the fixture - "Lions vs Saints | Week 1 Game Highlights". Checked over
    the first four tokens so a channel prefix or an emoji does not defeat
    it.
    """
    if not matched:
        return False
    head = set(tokenize(video.title)[:4])
    if not head:
        return False
    return any(
        tok in head
        for p in matched
        for tok in tokenize(p.name)[-1:]  # surname carries the signal
    )


def looks_like_recap(video: Video, matched: Sequence[PlayerRef]) -> bool:
    """True when this is a game recap rather than one player's cut-up.

    Evaluated *before* "a player was matched", because the failure this
    fixes is a 17-minute Lions/Saints recap being filed as Jahmyr Gibbs'
    cut-up simply because his name appears in the title.

    Three signals, in order of how much they are trusted:

    1. **An explicit cut-up phrase vetoes everything.** "Every Target and
       Catch" is never a recap, at any length.
    2. **Two teams plus a recap phrase.** "Game Highlights", "Full Game",
       "Condensed Game" - the fixture-shaped titles.
    3. **Two teams plus recap-length runtime.** Catches recaps whose
       titles use none of the stock phrasing. A title that leads with a
       matched player, or that is short-form, is exempt: those are
       cut-ups that happen to name both sides of the game.
    """
    folded = _fold(video.title)
    if _has_any(folded, _CUTUP_STRONG_MARKERS):
        return False

    dur = video.duration_seconds
    if dur is not None and dur <= SHORT_FORM_MAX_SECONDS:
        return False

    teams = find_teams(video.title)
    if len(teams) >= 2 and _has_any(folded, _RECAP_MARKERS):
        return True

    if dur is not None and dur >= RECAP_MIN_SECONDS:
        if _title_leads_with_player(video, matched):
            return False
        if len(teams) >= 2 or not matched:
            return True

    return False


def classify(video: Video, matched: Sequence[PlayerRef]) -> str:
    """``KIND_PLAYER`` / ``KIND_TEAM`` / ``KIND_OTHER`` for one upload.

    Order matters. Junk is rejected first, then recaps are separated from
    cut-ups, and only then does a name match decide. Putting the recap
    test ahead of the match test is the bifurcation the UI relies on: the
    two kinds are rendered as separate groups, so anything misfiled here
    shows up in the wrong section of the page.
    """
    folded = _fold(video.title)
    if _has_any(folded, _NON_GAME_MARKERS):
        return KIND_OTHER
    if _has_any(folded, _MEME_MARKERS):
        return KIND_OTHER
    if looks_like_recap(video, matched):
        return KIND_TEAM
    if matched:
        return KIND_PLAYER
    if len(find_teams(video.title)) >= 2 and _has_any(folded, _CUTUP_MARKERS):
        return KIND_TEAM
    return KIND_OTHER


def score_confidence(
    video: Video,
    player: PlayerRef,
    kind: str,
    n_matched: int,
    expected_week: Optional[int] = None,
    in_window: Optional[bool] = None,
) -> float:
    """Heuristic 0..1 confidence that this clip is really this player's game.

    Deliberately conservative. The site filters on this, so a badly-calibrated
    score shows up as either an empty reel or someone else's highlights.

    ``in_window`` is the load-bearing recency signal: it comes from the
    upload's own timestamp. ``expected_week`` is a title-text agreement
    bonus and is deliberately worth half of it -- a channel that forgets to
    type "Week 1" should not be ranked below one that typed the wrong week.
    """
    if kind == KIND_OTHER:
        return 0.0

    folded = _fold(video.title)
    score = 0.55

    if _has_any(folded, _CUTUP_MARKERS):
        score += 0.15

    # Title names the player's own team or their opponent -> strong signal
    # we've got the right person and the right game.
    teams = find_teams(video.title)
    if player.team and player.team in teams:
        score += 0.15
    elif teams:
        score += 0.05

    wk = parse_week(video.title)
    if wk is not None:
        score += 0.05
        if expected_week is not None and wk == expected_week:
            score += 0.05

    if in_window:
        score += 0.10

    # Channel provenance. Every channel in data/highlights/channels.json
    # was resolved and had its recent uploads inspected by hand before
    # being enabled, so "this came from a curated source" is a stronger
    # statement about the clip than any title keyword. Worth the same as
    # naming the player's own team, and deliberately no more: a trusted
    # channel still posts the occasional thing we do not want.
    if video.trusted_channel:
        score += 0.08

    # Duration as a shape signal rather than a gate. A cut-up that runs
    # recap-length is more likely a misfile than a very thorough cut-up.
    dur = video.duration_seconds
    if dur is not None and kind == KIND_PLAYER and dur >= RECAP_MIN_SECONDS:
        score -= 0.05

    # Several players named -> it's a shared compilation, so any single
    # player gets less screen time than a dedicated cut-up.
    if n_matched > 1:
        score -= 0.10 * (n_matched - 1)

    if kind == KIND_TEAM:
        score -= 0.20

    return round(max(0.0, min(1.0, score)), 3)


# --------------------------------------------------------------------------
# Index build
# --------------------------------------------------------------------------

def _sort_clips(clips: List[Clip]) -> None:
    """Order clips in place, recency-first inside the game window.

    Precedence, highest first:

    1. **In the window.** Out-of-window clips keep their place in the
       artifact -- the "every clip per player" toggle surfaces them -- but
       they never lead.
    2. **Cut-up over recap.** A dedicated cut-up is what the reel is for.
    3. **Newest upload.** This replaces the old ``-(week or 0)`` key, which
       made an unreliable, frequently-absent title field the dominant sort:
       a clip whose title omitted the week sorted as week 0, i.e. behind
       everything, however recent it was.
    4. Confidence, then the title's week as a final tiebreak only.

    One composite key rather than the previous two passes -- every component
    is numeric now, so the descending tie-breakers can be expressed by
    negation and the ordering is readable in one place.
    """
    def key(c: Clip):
        ts = parse_ts(c.published_at)
        return (
            0 if c.in_window in (True, None) else 1,
            0 if c.kind == KIND_PLAYER else 1,
            -(ts.timestamp() if ts else 0.0),
            -c.confidence,
            # View count, below every correctness signal above it. It
            # breaks ties between clips that are otherwise equally good;
            # it must never promote a popular recap over the cut-up of
            # the player you actually rostered, which is why it sits
            # beneath both the kind and the confidence keys.
            -(c.view_count or 0),
            -(c.week or 0),
        )

    clips.sort(key=key)


def build_index(
    players: Sequence[PlayerRef],
    videos: Iterable[Video],
    *,
    min_confidence: float = 0.5,
    max_clips_per_player: int = 5,
    expected_week: Optional[int] = None,
    window: Optional[GameWindow] = None,
    include_team_fallback: bool = True,
    min_clip_seconds: int = MIN_CLIP_SECONDS,
    trusted_channel_ids: Optional[Iterable[str]] = None,
    as_of: Optional[datetime] = None,
    season_start: Optional[datetime] = None,
) -> dict:
    """Build the full highlights artifact.

    Returns a JSON-ready dict::

        {
          "clips":   { "<sleeper_id>": [clip, ...] },
          "by_gsis": { "<gsis_id>": "<sleeper_id>" },
          "players": { "<sleeper_id>": {name, position, team, rank} },
          "window":  {start, end, publish_cutoff, label, week, ...} | None,
          "window_start": "<iso>" | None,
          "window_end":   "<iso>" | None,
          "stats":   {...}
        }

    ``window`` decides ordering, not membership: clips published outside it
    are still indexed and still reachable from the "every clip per player"
    toggle, they simply sort after everything from the completed slate. A
    player whose channel has not posted this week therefore degrades to
    older film on request rather than disappearing.

    ``stats`` buckets every dropped video into exactly one counter
    (``videos_too_short`` / ``videos_ambiguous`` / ``videos_non_game`` /
    ``videos_unmatched``), so the four drop counts plus the videos that
    produced clips account for ``videos_seen``.

    ``videos_embed_blocked`` is reported but is **not** a drop count any
    more. Nothing embeds, so a video whose uploader disabled embedding
    plays perfectly well on the link-out; excluding it only cost clips.

    ``weeks`` is the week-bucket list, newest first, with ``lead_week``
    naming the most recent *complete* one. Both pages group on this and
    neither re-derives it, so the reel and My Team cannot disagree about
    which week leads.

    Keyed on ``sleeper_id`` because that's the canonical id in this project
    and what a Sleeper roster hands back. ``by_gsis`` exists because
    ``engine.rankings`` rows are keyed by gsis id, so the rankings table and
    per-player pages need the crosswalk to find their clips.
    """
    name_index = build_name_index(players)
    trusted = {str(c) for c in (trusted_channel_ids or ()) if c}
    as_of = as_of or datetime.now(timezone.utc)

    # Team -> fantasy-relevant players, for the game-recap fallback. A recap
    # names no player, so without this it would be indexed against nobody and
    # the "include game recaps" option would silently do nothing. Restricted
    # to skill positions: nobody builds a reel to watch their kicker.
    _FALLBACK_POSITIONS = {"QB", "RB", "WR", "TE"}
    by_team: Dict[str, List[PlayerRef]] = {}
    for p in players:
        if p.team and (p.position or "").upper() in _FALLBACK_POSITIONS:
            by_team.setdefault(p.team, []).append(p)

    by_player: Dict[str, List[Clip]] = {}

    n_seen = n_embed_blocked = n_unmatched = n_other = n_ambiguous = 0
    n_shorts = 0

    for video in videos:
        n_seen += 1
        if not video.embeddable:
            # Counted for visibility, deliberately not dropped. This was a
            # drop when the reel embedded clips - error 150 stalled the
            # whole playlist. The pages link out to YouTube now, where the
            # uploader's embed setting is irrelevant, so dropping these
            # would discard perfectly playable film for a problem that no
            # longer exists.
            n_embed_blocked += 1

        # A floor, not a Shorts filter. Shorts are wanted now; a
        # sub-15-second sting still cannot be a highlight.
        if is_short(video, min_seconds=min_clip_seconds):
            n_shorts += 1
            continue

        matched, n_collisions = match_players_with_ambiguity(video, name_index)
        kind = classify(video, matched)

        # An unresolved same-name collision leaves ``matched`` empty, which
        # sends classify() down the KIND_OTHER path for an ordinary cut-up
        # title. Counting that as "not a game" hides the real failure: we
        # knew the name, we just couldn't tell which player it was. Bucket
        # it separately -- a rising ambiguous count means the name index
        # needs a tie-breaker, not that the channels went off-topic.
        dropped_ambiguous = n_collisions > 0 and not matched

        if kind == KIND_OTHER:
            if dropped_ambiguous:
                n_ambiguous += 1
            else:
                n_other += 1
            continue
        if kind == KIND_TEAM:
            if not include_team_fallback:
                continue
            # Attach the recap to every skill player on either side of the
            # game, so a player with no cut-up this week still has something.
            matched = [
                p for t in find_teams(video.title) for p in by_team.get(t, [])
            ]

        if not matched:
            if dropped_ambiguous:
                n_ambiguous += 1
            else:
                n_unmatched += 1
            continue

        teams = find_teams(video.title)
        in_window = window.contains(video.published_at) if window else None
        video.trusted_channel = (
            video.trusted_channel or video.channel_id in trusted
        )
        bucket_start = slate_start_for(video.published_at)
        bucket_week = (week_number_for(bucket_start, season_start)
                       if bucket_start else None)
        for player in matched:
            conf = score_confidence(
                video, player, kind,
                # A recap legitimately covers a whole roster; only penalise
                # genuine multi-player cut-up compilations for split focus.
                1 if kind == KIND_TEAM else len(matched),
                expected_week=expected_week,
                in_window=in_window,
            )
            if conf < min_confidence:
                continue
            opponent = next(
                (t for t in teams if player.team and t != player.team), None
            )
            by_player.setdefault(player.sleeper_id, []).append(
                Clip(
                    video_id=video.video_id,
                    title=video.title,
                    channel_title=video.channel_title,
                    published_at=video.published_at,
                    duration_seconds=video.duration_seconds,
                    kind=kind,
                    confidence=conf,
                    week=parse_week(video.title),
                    season=parse_season(video.title),
                    opponent=opponent,
                    in_window=in_window,
                    bucket_week=bucket_week,
                    bucket_start=(bucket_start.date().isoformat()
                                  if bucket_start else None),
                    view_count=video.view_count,
                    trusted=True if video.trusted_channel else None,
                )
            )

    # Sort, de-dupe by video id, truncate.
    clips_out: Dict[str, List[dict]] = {}
    for sid, clips in by_player.items():
        _sort_clips(clips)
        deduped: List[Clip] = []
        seen_vids: set = set()
        for c in clips:
            if c.video_id in seen_vids:
                continue
            seen_vids.add(c.video_id)
            deduped.append(c)
        clips_out[sid] = [c.to_json() for c in deduped[:max_clips_per_player]]

    by_gsis = {
        p.gsis_id: p.sleeper_id
        for p in players
        if p.gsis_id and p.sleeper_id in clips_out
    }
    players_out = {
        p.sleeper_id: {
            k: v for k, v in {
                "name": p.name,
                "position": p.position,
                "team": p.team,
                "rank": p.rank,
            }.items() if v is not None
        }
        for p in players
        if p.sleeper_id in clips_out
    }

    n_window_clips = sum(
        1 for cl in clips_out.values() for c in cl if c.get("in_window")
    )
    n_window_players = sum(
        1 for cl in clips_out.values() if any(c.get("in_window") for c in cl)
    )

    lead = most_recent_complete_slate(as_of, season_start=season_start)
    weeks = build_week_buckets(
        clips_out, as_of=as_of, season_start=season_start, lead=lead,
    )

    return {
        "clips": clips_out,
        "by_gsis": by_gsis,
        "players": players_out,
        "window": window.to_json() if window else None,
        # Flattened duplicates of the two fields the pages render, so a
        # reader does not have to know the nested shape to label a reel.
        "window_start": window.start.isoformat() if window else None,
        "window_end": window.end.isoformat() if window else None,
        # Week buckets. ``lead_week`` is the most recent COMPLETE slate --
        # a different question from ``window``, which is about where the
        # film is. See the "Week buckets" section for why both exist.
        "weeks": [w.to_json() for w in weeks],
        "lead_week": lead.week,
        "lead_week_start": lead.start.date().isoformat(),
        "lead_week_label": lead.label,
        "resolved_as_of": as_of.isoformat(),
        "stats": {
            "videos_seen": n_seen,
            "videos_embed_blocked": n_embed_blocked,
            "videos_too_short": n_shorts,
            "min_clip_seconds": min_clip_seconds,
            "videos_non_game": n_other,
            "videos_unmatched": n_unmatched,
            "videos_ambiguous": n_ambiguous,
            "players_with_clips": len(clips_out),
            "total_clips": sum(len(v) for v in clips_out.values()),
            "clips_in_window": n_window_clips,
            "players_with_window_clips": n_window_players,
            "clips_trusted_channel": sum(
                1 for cl in clips_out.values() for c in cl if c.get("trusted")
            ),
            "clips_player_cutup": sum(
                1 for cl in clips_out.values() for c in cl
                if c.get("kind") == KIND_PLAYER
            ),
            "clips_game_recap": sum(
                1 for cl in clips_out.values() for c in cl
                if c.get("kind") == KIND_TEAM
            ),
            "weeks_bucketed": len(weeks),
        },
    }


def build_week_buckets(
    clips_out: Dict[str, List[dict]],
    *,
    as_of: datetime,
    season_start: Optional[datetime] = None,
    lead: Optional[Slate] = None,
) -> List[Slate]:
    """Week buckets present in the index, newest slate first.

    The list is built from the slates the clips actually landed in, plus
    the lead slate whether or not it has film - a lead bucket that is
    empty is information ("nothing indexed for week 1 yet"), and dropping
    it would silently promote an incomplete week to the top of the page.

    Incomplete slates are still returned, carrying ``complete: False``, so
    the UI can show week 2 as in-progress underneath rather than pretend
    the film does not exist.
    """
    lead = lead or most_recent_complete_slate(as_of, season_start=season_start)

    starts: Dict[str, datetime] = {}
    counts: Dict[str, int] = {}
    for cl in clips_out.values():
        for c in cl:
            key = c.get("bucket_start")
            if not key:
                continue
            if key not in starts:
                try:
                    starts[key] = datetime.fromisoformat(key).replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    continue
            counts[key] = counts.get(key, 0) + 1

    starts.setdefault(lead.start.date().isoformat(), lead.start)

    out: List[Slate] = []
    for key, start in starts.items():
        slate = slate_for_start(start, as_of=as_of, season_start=season_start)
        out.append(slate)

    # Newest first. The lead bucket is not necessarily out[0]: a slate in
    # progress sorts above it by date, which is correct -- it renders
    # below the lead but keeps its real chronology in the data.
    out.sort(key=lambda s: s.start, reverse=True)
    lead_key = lead.start.date().isoformat()
    for slate in out:
        key = slate.start.date().isoformat()
        slate.clip_count = counts.get(key, 0)
        slate.lead = key == lead_key
    return out


def load_players_from_db(rank_by_gsis: Optional[Dict[str, int]] = None) -> List[PlayerRef]:
    """Read canonical players out of the project DB into PlayerRefs.

    Imported lazily so ``highlights`` stays usable (and testable) with no DB.
    ``rank_by_gsis`` comes from ``engine.rankings`` — pass it so reel ordering
    and tie-breaking can use model rank.
    """
    from .db.session import get_session
    from .db.models import Player
    from sqlalchemy import select

    rank_by_gsis = rank_by_gsis or {}
    out: List[PlayerRef] = []
    with get_session() as session:
        for p in session.execute(select(Player)).scalars():
            sid = getattr(p, "sleeper_id", None)
            if not sid:
                continue
            gsis = getattr(p, "gsis_id", None)
            out.append(
                PlayerRef(
                    sleeper_id=str(sid),
                    # ``Player`` stores the display name as ``full_name``.
                    # Reading ``name`` first left every PlayerRef nameless,
                    # which build_name_index drops on the empty match key --
                    # the whole index would come back empty in CI while every
                    # other counter looked healthy.
                    name=getattr(p, "full_name", None) or getattr(p, "name", "") or "",
                    position=getattr(p, "position", None),
                    team=getattr(p, "team", None) or getattr(p, "nfl_team", None),
                    gsis_id=gsis,
                    rank=rank_by_gsis.get(gsis) if gsis else None,
                )
            )
    return out
