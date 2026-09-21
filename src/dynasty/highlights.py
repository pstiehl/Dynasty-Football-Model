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

# Phrases that mean this is talk, not football. Strong negative signal —
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
    #: False when ``status.embeddable`` is explicitly false. Non-embeddable
    #: videos are dropped at index time — they'd 150-error mid-reel.
    embeddable: bool = True


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

#: Grace period, measured from the *start of the slate's final day* -- i.e.
#: from Monday 00:00 UTC, the point at which every game but Monday night has
#: been played. Two things have to be true at once and this is the number
#: that makes both true:
#:
#:   * Monday morning (say 12:41 UTC) is still inside the grace period of
#:     the slate happening around the viewer, so they are served last
#:     week's finished film instead of a week with nothing uploaded yet.
#:   * 36 hours later is Tuesday 12:00 UTC -- after Monday night football
#:     has finished (~03:30 UTC) and after the cut-up channels have posted,
#:     and before the Tuesday 16:00 UTC refresh in daily-refresh.yml. So
#:     that second in-season pass publishes the slate it was added for.
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
        When this window became *the* window. See
        :func:`resolve_game_window`.
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


def resolve_game_window(
    as_of: Optional[datetime] = None,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    grace_hours: float = DEFAULT_GRACE_HOURS,
    season_start: Optional[datetime] = None,
) -> GameWindow:
    """The most recent Thursday->Monday slate that has finished.

    A slate "opens" ``grace_hours`` after the *start of its final day*, so
    with the defaults it becomes current at Tuesday 12:00 UTC. The function
    walks back from the Thursday on or before ``as_of`` until it finds one
    that has opened.

    Worked example -- Monday 2026-09-21 12:41 UTC, the case this exists for::

        candidate Thu 09-17  final day starts Mon 09-21 00:00
                             opens Tue 09-22 12:00   (future, skip)
        candidate Thu 09-10  final day starts Mon 09-14 00:00
                             opens Tue 09-15 12:00   (past, take it)
                             -> Sep 10-14, publish cutoff Wed 09-16 11:59:59

    A Monday-morning viewer therefore gets Sep 10-14 -- the Cowboys/Giants
    games that have actually been played and uploaded -- rather than the
    week still in progress around them. By Tuesday lunchtime, once Monday
    night's cut-ups exist, it advances to Sep 17-21 on its own.
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

    # Grace runs from the start of the final day, not from its end: on
    # Monday at 00:00 UTC every game except Monday night has been played,
    # which is the moment the clock on "have the channels posted yet"
    # actually starts.
    def opens(thu: datetime) -> datetime:
        return thu + timedelta(days=window_days - 1) + grace

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

def classify(video: Video, matched: Sequence[PlayerRef]) -> str:
    folded = _fold(video.title)
    if _has_any(folded, _NON_GAME_MARKERS):
        return KIND_OTHER
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
    (``videos_unembeddable`` / ``videos_ambiguous`` / ``videos_non_game`` /
    ``videos_unmatched``), so the four drop counts plus the videos that
    produced clips account for ``videos_seen``.

    Keyed on ``sleeper_id`` because that's the canonical id in this project
    and what a Sleeper roster hands back. ``by_gsis`` exists because
    ``engine.rankings`` rows are keyed by gsis id, so the rankings table and
    per-player pages need the crosswalk to find their clips.
    """
    name_index = build_name_index(players)

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

    n_seen = n_unembeddable = n_unmatched = n_other = n_ambiguous = 0

    for video in videos:
        n_seen += 1
        if not video.embeddable:
            # Would throw a 150 error mid-playlist and stall the reel.
            n_unembeddable += 1
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

    return {
        "clips": clips_out,
        "by_gsis": by_gsis,
        "players": players_out,
        "window": window.to_json() if window else None,
        # Flattened duplicates of the two fields the pages render, so a
        # reader does not have to know the nested shape to label a reel.
        "window_start": window.start.isoformat() if window else None,
        "window_end": window.end.isoformat() if window else None,
        "stats": {
            "videos_seen": n_seen,
            "videos_unembeddable": n_unembeddable,
            "videos_non_game": n_other,
            "videos_unmatched": n_unmatched,
            "videos_ambiguous": n_ambiguous,
            "players_with_clips": len(clips_out),
            "total_clips": sum(len(v) for v in clips_out.values()),
            "clips_in_window": n_window_clips,
            "players_with_window_clips": n_window_players,
        },
    }


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
