"""Weekly nflverse stat lines: rookie game logs + the first-N cohort.

Companion to :mod:`dynasty.sources.nflverse_career_stats`, which reads
*season* totals. This module reads the two *weekly*-derived artifacts
built by ``scripts/refresh_nflverse_weekly.py``:

``data/nflverse/player_week_current.csv.gz``
    Every regular-season weekly stat line for the in-progress season.
    A current rookie's career-to-date line is summed from these rows.

``data/nflverse/player_first_n_games.csv.gz``
    The historical cohort: each eligible player's cumulative line
    through their first N career games, for N = 1..17, one row per
    (player, N).

Why this exists
---------------

Phil, 2026-09-22: rookies were being compared to historical *college*
players. Once a rookie has NFL snaps, the honest comparison is to
historical NFL players at the same point in their own careers. That
requires per-game granularity, which the season corpus cannot provide:
a season row says "16 games, 1,100 yards", not "through two games he
had 154".

Definition of a game
--------------------

A game in which the player recorded a stat line. Inactive weeks and
active-but-untargeted weeks do not appear in the weekly file and do not
count. The same rule is applied to the current rookies and to every
historical player in the cohort, so both sides of a comparison are
measured identically. :data:`GAME_DEFINITION` is the sentence the site
renders, kept here so the page and the data cannot drift apart.

Everything here is stdlib-only and returns plain dicts, so it is
testable without the engine, numpy or the network.
"""
from __future__ import annotations

import csv
import gzip
import logging
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
NFLVERSE_DIR = _REPO_ROOT / "data" / "nflverse"
CURRENT_WEEK_PATH = NFLVERSE_DIR / "player_week_current.csv.gz"
FIRST_N_PATH = NFLVERSE_DIR / "player_first_n_games.csv.gz"

#: Rendered verbatim on every page that shows an N-games comparison.
GAME_DEFINITION = (
    "A \u201cgame\u201d is one in which the player recorded a stat line. "
    "Inactive weeks do not count, and the same rule is applied to the "
    "rookie and to every historical player he is compared against."
)

SKILL_POSITIONS = ("QB", "RB", "WR", "TE")

#: Accumulated stat columns, matching the corpus builder.
STAT_COLUMNS = (
    "completions",
    "attempts",
    "passing_yards",
    "passing_tds",
    "passing_interceptions",
    "carries",
    "rushing_yards",
    "rushing_tds",
    "receptions",
    "targets",
    "receiving_yards",
    "receiving_tds",
    "fantasy_points_ppr",
)


# ---------------------------------------------------------------------------
# Coercion (mirrors nflverse_career_stats)
# ---------------------------------------------------------------------------

def _num(s) -> float:
    if s is None:
        return 0.0
    s = str(s).strip()
    if not s or s.upper() in {"NA", "NAN", "NULL", "NONE"}:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _int(s) -> int:
    return int(_num(s))


def _read_gz_csv(path: Path) -> List[Dict[str, str]]:
    """Read a gzipped CSV into dicts. Missing file -> empty list.

    A missing artifact is a normal state on a fresh clone (the refresh
    script has not run yet), so it must not raise. Callers distinguish
    "no data" from "no comparison" explicitly.
    """
    if not path.exists():
        log.warning("nflverse weekly: %s missing - run "
                    "scripts/refresh_nflverse_weekly.py", path.name)
        return []
    try:
        with gzip.open(path, "rt") as fh:
            return list(csv.DictReader(fh))
    except (OSError, gzip.BadGzipFile) as exc:
        log.warning("nflverse weekly: %s unreadable: %s", path.name, exc)
        return []


# ---------------------------------------------------------------------------
# Current-season game logs
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _current_week_rows(path: str = "") -> Tuple[Dict[str, str], ...]:
    return tuple(_read_gz_csv(Path(path) if path else CURRENT_WEEK_PATH))


@lru_cache(maxsize=1)
def current_game_logs(path: str = "") -> Dict[str, List[Dict]]:
    """``{player_id: [game dicts, earliest week first]}`` for this season.

    Each game dict carries ``week``, ``team``, ``opponent``, and every
    column in :data:`STAT_COLUMNS`.
    """
    out: Dict[str, List[Dict]] = {}
    for raw in _current_week_rows(path):
        pid = (raw.get("player_id") or "").strip()
        if not pid:
            continue
        game = {
            "week": _int(raw.get("week")),
            "season": _int(raw.get("season")),
            "team": (raw.get("team") or "").strip(),
            "opponent": (raw.get("opponent_team") or "").strip(),
            "position": (raw.get("position") or "").strip(),
            "name": (raw.get("player_display_name") or "").strip(),
        }
        for c in STAT_COLUMNS:
            game[c] = _num(raw.get(c))
        out.setdefault(pid, []).append(game)
    for games in out.values():
        games.sort(key=lambda g: g["week"])
    return out


@lru_cache(maxsize=1)
def current_name_index(path: str = "") -> Dict[str, str]:
    """``{normalised display name: player_id}`` for the current season.

    The rookie board is keyed on prospect records that carry no gsis id,
    so the join to NFL production has to go through the name. Normalised
    to lowercase alphanumerics: the roster writes "KC Concepcion" while
    the college corpus has "Kevin Concepcion", and punctuation differs
    across sources ("Ja'Marr" / "JaMarr").
    """
    out: Dict[str, str] = {}
    for raw in _current_week_rows(path):
        pid = (raw.get("player_id") or "").strip()
        name = (raw.get("player_display_name") or "").strip()
        if pid and name:
            out.setdefault(normalise_name(name), pid)
    return out


def normalise_name(name: str) -> str:
    """Lowercase alphanumeric form used for cross-source name joins."""
    return "".join(ch for ch in str(name or "").lower() if ch.isalnum())


def current_season(path: str = "") -> Optional[int]:
    """The season the current-week artifact describes, or ``None``."""
    for raw in _current_week_rows(path):
        season = _int(raw.get("season"))
        if season:
            return season
    return None


def current_weeks(path: str = "") -> List[int]:
    """Sorted list of weeks present in the current-season artifact."""
    return sorted({_int(r.get("week")) for r in _current_week_rows(path)
                   if _int(r.get("week"))})


def career_to_date(player_id: str, path: str = "") -> Optional[Dict]:
    """A current player's cumulative line through every game he has played.

    Returns ``None`` when the player has no stat line this season --
    the honest "no NFL production to compare" state, which the caller
    must render rather than papering over.

    ``n_games`` is derived from the logs. Nothing in this module knows
    or cares that it happens to be 2 in September 2026; when week 3 is
    published the same call returns 3.
    """
    games = current_game_logs(path).get(player_id) or []
    if not games:
        return None
    totals = {c: 0.0 for c in STAT_COLUMNS}
    for g in games:
        for c in STAT_COLUMNS:
            totals[c] += g.get(c, 0.0)
    return {
        "player_id": player_id,
        "name": games[0].get("name") or "",
        "position": games[0].get("position") or "",
        "season": games[0].get("season"),
        "n_games": len(games),
        "weeks": [g["week"] for g in games],
        "team": games[-1].get("team") or "",
        "totals": {c: round(totals[c], 2) for c in STAT_COLUMNS},
        "games": games,
    }


# ---------------------------------------------------------------------------
# Historical first-N cohort
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _first_n_rows(path: str = "") -> Tuple[Dict[str, str], ...]:
    return tuple(_read_gz_csv(Path(path) if path else FIRST_N_PATH))


@lru_cache(maxsize=8)
def cohort(position: str, n_games: int, path: str = "") -> List[Dict]:
    """Historical players' cumulative line through their first ``n_games``.

    Filtered to ``position`` and to exactly ``n_games``, so every member
    of the returned cohort is directly comparable to a current rookie
    with the same number of games played. A player who never reached
    ``n_games`` in his career is simply absent -- he has no line of that
    length, and inventing one by extrapolation would manufacture the
    comparison this whole feature exists to avoid.
    """
    pos = (position or "").upper()
    out: List[Dict] = []
    for raw in _first_n_rows(path):
        if (raw.get("position") or "").upper() != pos:
            continue
        if _int(raw.get("n_games")) != int(n_games):
            continue
        rec = {
            "player_id": (raw.get("player_id") or "").strip(),
            "name": (raw.get("player_display_name") or "").strip(),
            "position": pos,
            "debut_season": _int(raw.get("debut_season")),
            "n_games": int(n_games),
        }
        for c in STAT_COLUMNS:
            rec[c] = _num(raw.get(c))
        out.append(rec)
    return out


def cohort_size(position: str, n_games: int, path: str = "") -> int:
    return len(cohort(position, n_games, path))


def clear_cache() -> None:
    """Drop every memoised read (used by tests)."""
    _current_week_rows.cache_clear()
    current_game_logs.cache_clear()
    current_name_index.cache_clear()
    _first_n_rows.cache_clear()
    cohort.cache_clear()
