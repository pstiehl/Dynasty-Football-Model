#!/usr/bin/env python3
"""Build the *first-N-career-games* corpus from nflverse weekly stats.

Why this file exists
--------------------

Phil, 2026-09-22: "the model is still comparing them to college players.
It should be comparing them to nfl players using their nfl stats to this
point. the model should be taking all of their starts so far from the
2026 season and comparing them to similar historical nfl players who put
up similar stats through their first 2 games."

The existing rookie board projects from *college* production comped
against historical *college* players. That was the only thing available
in April. It is no longer: the 2026 class has played real NFL snaps, and
those snaps are published weekly. This script turns them into a
comparison corpus.

What it produces
----------------

Two committed artifacts under ``data/nflverse/``:

``player_week_current.csv.gz``
    Every regular-season weekly stat line for the **in-progress** season,
    trimmed to the skill-position columns the engine reads. This is what
    a 2026 rookie's career-to-date line is computed from. It grows by one
    week every week; nothing here is hardcoded to week 2.

``player_first_n_games.csv.gz``
    The historical cohort. For every skill-position player whose career
    began in the nflverse weekly era, their **cumulative** line through
    their first N career games, for N = 1..``MAX_N``. One row per
    (player, N). This is what a rookie with N games played is compared
    against — same position, same N, so the two lines are the same shape.

Definition of "a game"
----------------------

A game in which the player recorded a stat line in the weekly file.
A player who was inactive, or active but never touched the ball, does
not appear that week and it does not count against him. The same rule
is applied to the 2026 rookies and to every historical player in the
cohort, so the two sides are measured identically. This is stated on
the site rather than left for a reader to discover.

Why the cohort starts at the weekly era
---------------------------------------

"First N career games" is only knowable if we can see the player's
first game. nflverse weekly coverage begins in 1999, so a player whose
first appearance is in 1999 may well have debuted in 1998 and we would
silently treat his fifth NFL season as his rookie year. Two guards:

* ``players.csv.gz`` carries ``rookie_season``. When it is present it
  must equal the player's first season in the weekly data.
* When it is absent, the first appearance must be 2000 or later, so a
  truncated 1999 career can never be mistaken for a debut.

Players who fail both checks are dropped from the cohort rather than
included with a first-N line that is not actually their first N.

Usage
-----

    python scripts/refresh_nflverse_weekly.py              # incremental
    python scripts/refresh_nflverse_weekly.py --full       # rebuild all
    python scripts/refresh_nflverse_weekly.py --no-network # cache only
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
NFLVERSE_DIR = REPO_ROOT / "data" / "nflverse"

#: Raw per-season weekly downloads. Gitignored: ~1.1 MB gz per season and
#: fully regenerable, so committing 27 of them would add ~30 MB to the
#: repo for data the derived corpus already summarises.
WEEKLY_CACHE_DIR = NFLVERSE_DIR / "weekly_cache"

#: Committed outputs.
CURRENT_WEEK_PATH = NFLVERSE_DIR / "player_week_current.csv.gz"
FIRST_N_PATH = NFLVERSE_DIR / "player_first_n_games.csv.gz"
PLAYERS_PATH = NFLVERSE_DIR / "players.csv.gz"

#: The ``stats_player`` release. NOTE: the ``player_stats`` release path
#: that older nflverse docs reference now 404s; ``stats_player`` is the
#: live one. Verified 2026-09-22.
WEEKLY_URL_TEMPLATE = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_player/stats_player_week_{year}.csv.gz"
)

USER_AGENT = (
    "dynasty-football-model refresh script "
    "(+https://github.com/pstiehl/Dynasty-Football-Model)"
)

START_SEASON = 1999

#: Cumulative lines are stored for N = 1..MAX_N. 17 is a full modern
#: regular season: past that a "first N games" comparison is really a
#: rookie-year comparison, which the season corpus already does better.
MAX_N = 17

SKILL_POSITIONS = ("QB", "RB", "WR", "TE")

#: Columns kept from the 150-column weekly file.
WEEK_COLUMNS = [
    "player_id",
    "player_display_name",
    "position",
    "season",
    "week",
    "team",
    "opponent_team",
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
]

#: Stat columns accumulated across a player's first N games.
STAT_COLUMNS = [
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
]

FIRST_N_COLUMNS = (
    ["player_id", "player_display_name", "position", "debut_season", "n_games"]
    + STAT_COLUMNS
)


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------

def _num(s: Optional[str]) -> float:
    """CSV cell -> float. Blank / ``NA`` / junk -> 0.0.

    nflverse writes R-style ``NA`` and empty strings for missing values
    and floats into integral columns. Mirrors ``nflverse_career_stats``.
    """
    if s is None:
        return 0.0
    s = str(s).strip()
    if not s or s.upper() in {"NA", "NAN", "NULL", "NONE"}:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _int(s: Optional[str]) -> int:
    return int(_num(s))


# ---------------------------------------------------------------------------
# Season detection
# ---------------------------------------------------------------------------

def current_nfl_season(today: Optional[date] = None) -> int:
    """The season currently in progress (or most recently played).

    Mirrors ``refresh_nflverse_corpus.current_nfl_season`` exactly so the
    two caches can never disagree about which year is "current".
    """
    today = today or date.today()
    return today.year if today.month >= 9 else today.year - 1


# ---------------------------------------------------------------------------
# Fetch + cache
# ---------------------------------------------------------------------------

def _cache_path(year: int) -> Path:
    return WEEKLY_CACHE_DIR / f"stats_player_week_{year}.csv.gz"


def fetch_week_file(year: int, *, force: bool = False,
                    allow_network: bool = True) -> Optional[bytes]:
    """Return the raw gzipped weekly file for ``year``.

    Served from ``WEEKLY_CACHE_DIR`` when present unless ``force``.
    Returns ``None`` when the season is not published (404) or when the
    network is unavailable and nothing is cached -- a missing season is
    skipped, never guessed at.
    """
    path = _cache_path(year)
    # ``force`` asks for a fresh copy; it does not ask us to throw away
    # the copy we have. With the network off, force-refreshing the
    # in-progress season used to return None and silently rewrite
    # player_week_current.csv.gz with zero rows -- an offline rebuild
    # of the historical cohort wiped the rookies' game logs.
    if path.exists() and (not force or not allow_network):
        return path.read_bytes()
    if not allow_network:
        return None

    url = WEEKLY_URL_TEMPLATE.format(year=year)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if not raw or len(raw) < 1024:
        raise RuntimeError(
            f"weekly fetch for {year} returned {len(raw)} bytes - looks empty"
        )
    WEEKLY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="wb", dir=WEEKLY_CACHE_DIR, delete=False, suffix=".tmp")
    tmp_path = Path(tmp.name)
    try:
        tmp.write(raw)
        tmp.close()
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return raw


def parse_week_rows(raw: bytes) -> List[Dict[str, str]]:
    """Parse one gzipped weekly file into trimmed regular-season rows.

    Filters to regular season and skill positions. Both filters belong
    here rather than downstream: a postseason line is not part of "first
    N regular-season games", and a kicker has no comparable shape.
    """
    out: List[Dict[str, str]] = []
    with gzip.open(io.BytesIO(raw), "rt") as fh:
        for row in csv.DictReader(fh):
            if (row.get("season_type") or "").upper() != "REG":
                continue
            if (row.get("position") or "") not in SKILL_POSITIONS:
                continue
            if not (row.get("player_id") or "").strip():
                continue
            out.append({c: (row.get(c) or "") for c in WEEK_COLUMNS})
    return out


# ---------------------------------------------------------------------------
# Debut validation
# ---------------------------------------------------------------------------

def load_rookie_seasons() -> Dict[str, int]:
    """``{gsis_id: rookie_season}`` from the committed players metadata.

    Empty dict when the file is absent, in which case every player falls
    back to the conservative ">= 2000 first appearance" rule below.
    """
    if not PLAYERS_PATH.exists():
        return {}
    out: Dict[str, int] = {}
    with gzip.open(PLAYERS_PATH, "rt") as fh:
        for row in csv.DictReader(fh):
            gsis = (row.get("gsis_id") or "").strip()
            season = _int(row.get("rookie_season"))
            if gsis and season > 0:
                out[gsis] = season
    return out


def debut_is_trustworthy(first_season: int,
                         rookie_season: Optional[int]) -> bool:
    """Can we believe ``first_season`` is really this player's first year?

    The risk this guards against is one-sided: a player who was already
    established when weekly coverage began, whose fifth NFL season would
    otherwise be read as his rookie year. That can only happen at the
    edge of the window.

    * First stat line in **2000 or later** -- coverage already extended
      back a full season before he appeared, so no earlier game of his
      can be hidden from us. Trustworthy on its own.
    * First stat line in **1999**, the first covered season -- he may
      have debuted in 1998 or 1989. Believe it only when
      ``players.csv.gz`` independently says 1999 was his rookie year.

    Requiring ``rookie_season == first_season`` in *both* cases was the
    first cut and it was wrong in the second direction: it also dropped
    every player who was on a roster as a rookie but did not record a
    stat line until his second year (~29 per season, 1,080 in total).
    Those players are not ambiguous -- under this corpus's definition of
    a game, their first recorded game *is* their first game.
    """
    if first_season > START_SEASON:
        return True
    return rookie_season is not None and rookie_season == first_season


# ---------------------------------------------------------------------------
# First-N corpus
# ---------------------------------------------------------------------------

def _week_sort_key(row: Dict[str, str]) -> Tuple[int, int]:
    return (_int(row.get("season")), _int(row.get("week")))


def build_first_n_rows(
    weekly_by_player: Dict[str, List[Dict[str, str]]],
    rookie_seasons: Dict[str, int],
    *,
    max_n: int = MAX_N,
) -> List[Dict[str, object]]:
    """Cumulative first-N-games lines for every eligible player.

    ``weekly_by_player`` maps player_id -> that player's weekly rows
    across every season fetched. Rows are sorted here rather than
    assumed sorted: they arrive season-file by season-file, and a
    player's rows must be in career order for "first N" to mean
    anything.
    """
    out: List[Dict[str, object]] = []
    for pid, rows in weekly_by_player.items():
        if not rows:
            continue
        rows = sorted(rows, key=_week_sort_key)
        first_season = _int(rows[0].get("season"))
        if not debut_is_trustworthy(first_season, rookie_seasons.get(pid)):
            continue

        # Position can drift across a career in the source data (a
        # college WR listed at RB for one week, etc). The debut-season
        # position is the one that matters for a rookie comparison.
        debut_rows = [r for r in rows if _int(r.get("season")) == first_season]
        position = (debut_rows[0].get("position") or "").strip()
        if position not in SKILL_POSITIONS:
            continue
        name = (rows[0].get("player_display_name") or "").strip()

        running = {c: 0.0 for c in STAT_COLUMNS}
        for n, row in enumerate(rows[:max_n], 1):
            for c in STAT_COLUMNS:
                running[c] += _num(row.get(c))
            rec = {
                "player_id": pid,
                "player_display_name": name,
                "position": position,
                "debut_season": first_season,
                "n_games": n,
            }
            for c in STAT_COLUMNS:
                # Yardage and fantasy points carry a decimal; counts do
                # not. Rounding counts keeps the file readable and the
                # numbers exact.
                v = running[c]
                rec[c] = round(v, 2) if c == "fantasy_points_ppr" else round(v, 1)
            out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def _write_gz_csv(path: Path, columns: List[str],
                  rows: Iterable[Dict]) -> int:
    """Atomic gzipped-CSV write. Mirrors refresh_nflverse_corpus."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="wb", dir=path.parent, delete=False, suffix=".tmp")
    tmp_path = Path(tmp.name)
    tmp.close()
    total = 0
    try:
        with gzip.open(tmp_path, "wt", newline="") as gz:
            writer = csv.DictWriter(gz, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow({c: row.get(c, "") for c in columns})
                total += 1
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return total


# ---------------------------------------------------------------------------
# Public refresh API
# ---------------------------------------------------------------------------

def refresh(
    *,
    full: bool = False,
    allow_network: bool = True,
    today: Optional[date] = None,
    verbose: bool = True,
) -> Dict:
    """Refresh the weekly cache and rebuild both committed artifacts.

    ``full`` re-downloads every historical season; otherwise cached
    seasons are reused and only the in-progress season is re-pulled
    (older seasons are closed and cannot change).
    """
    season = current_nfl_season(today)

    def say(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    weekly_by_player: Dict[str, List[Dict[str, str]]] = {}
    current_rows: List[Dict[str, str]] = []
    seasons_loaded: List[int] = []
    seasons_missing: List[int] = []

    for year in range(START_SEASON, season + 1):
        is_current = (year == season)
        raw = fetch_week_file(
            year,
            # The in-progress season changes every week; closed seasons
            # do not. Re-pulling 26 static files daily is wasted traffic.
            force=full or is_current,
            allow_network=allow_network,
        )
        if raw is None:
            seasons_missing.append(year)
            say(f"  {year}: not available - skipped")
            continue
        rows = parse_week_rows(raw)
        seasons_loaded.append(year)
        say(f"  {year}: {len(rows):,} skill-position regular-season rows")
        if is_current:
            current_rows = rows
            # The current season is deliberately NOT part of the
            # historical cohort. Comparing a 2026 rookie to another 2026
            # rookie's identical two games is circular: neither has an
            # NFL career to have become anything yet.
            continue
        for row in rows:
            weekly_by_player.setdefault(row["player_id"], []).append(row)

    rookie_seasons = load_rookie_seasons()
    say(f"players metadata: {len(rookie_seasons):,} rookie_season values")

    first_n = build_first_n_rows(weekly_by_player, rookie_seasons)
    n_players = len({r["player_id"] for r in first_n})

    n_first = _write_gz_csv(FIRST_N_PATH, FIRST_N_COLUMNS, first_n)
    n_cur = _write_gz_csv(CURRENT_WEEK_PATH, WEEK_COLUMNS, current_rows)

    weeks = sorted({_int(r.get("week")) for r in current_rows})
    summary = {
        "current_season": season,
        "seasons_loaded": seasons_loaded,
        "seasons_missing": seasons_missing,
        "current_week_rows": n_cur,
        "current_weeks": weeks,
        "first_n_rows": n_first,
        "first_n_players": n_players,
        "max_n": MAX_N,
    }
    say(
        f"wrote {FIRST_N_PATH.name}: {n_first:,} rows / {n_players:,} players"
        f" (N=1..{MAX_N})"
    )
    say(
        f"wrote {CURRENT_WEEK_PATH.name}: {n_cur:,} rows,"
        f" weeks {weeks or '-'} of {season}"
    )
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--full", action="store_true",
                    help="re-download every season, ignoring the cache")
    ap.add_argument("--no-network", action="store_true",
                    help="build from the local cache only")
    args = ap.parse_args(argv)
    try:
        refresh(full=args.full, allow_network=not args.no_network)
    except Exception as exc:  # pragma: no cover - CLI surface
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
