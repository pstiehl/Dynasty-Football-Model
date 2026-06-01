#!/usr/bin/env python3
"""Build the pre-1999 player-stats corpus.

v2.4: 1980-1998
v3.9: 1936-1998 (Phil 2026-06-01 — "pull every drafted player" / full PFR
historical sweep so the long-arc comp pool isn't stuck at 2,440).

Scrapes PFR via Wayback (cached on disk), normalizes to the same column
schema as ``data/nflverse/player_stats_season.csv.gz``, and writes
``data/nflverse/player_stats_season_pre1999.csv.gz``.

Run from the repo root::

    python3 scripts/build_pre1999_corpus.py [--from-year 1936] [--to-year 1998]

Idempotent: HTML cache lives at ``data/pfr_cache/`` so re-runs are
network-free.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path

import pandas as pd

# Allow running as a script from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from dynasty.sources.pro_football_reference_seasonal import (  # noqa: E402
    fetch_season_table,
    parse_season_table,
)
from dynasty.scoring_rules import score_season  # noqa: E402

log = logging.getLogger("build_pre1999_corpus")

# Defaults — v3.9 expanded to cover the full PFR draft era (1936 was the
# first NFL draft). The pre-1970 AFL years are covered by PFR's stitched
# tables. The output CSV is still named ``_pre1999`` for back-compat;
# every consumer just looks for player_id rows.
DEFAULT_FROM_YEAR = 1936
DEFAULT_TO_YEAR = 1998
TABLES = ("fantasy", "passing", "rushing", "receiving")
OUTPUT_PATH = _REPO_ROOT / "data" / "nflverse" / "player_stats_season_pre1999.csv.gz"

# Canonical column order from data/nflverse/player_stats_season.csv.gz.
# Stats PFR doesn't expose pre-1999 stay NaN (or 0 for int columns where
# NaN would break downstream dtype assumptions).
NFLVERSE_COLUMNS = [
    "season", "season_type", "player_id", "player_name", "player_display_name",
    "position", "position_group", "headshot_url", "games", "recent_team",
    "completions", "attempts", "passing_yards", "passing_tds", "interceptions",
    "sacks", "sack_yards", "sack_fumbles", "sack_fumbles_lost",
    "passing_air_yards", "passing_yards_after_catch", "passing_first_downs",
    "passing_epa", "passing_2pt_conversions", "pacr", "dakota",
    "carries", "rushing_yards", "rushing_tds", "rushing_fumbles",
    "rushing_fumbles_lost", "rushing_first_downs", "rushing_epa",
    "rushing_2pt_conversions",
    "receptions", "targets", "receiving_yards", "receiving_tds",
    "receiving_fumbles", "receiving_fumbles_lost", "receiving_air_yards",
    "receiving_yards_after_catch", "receiving_first_downs", "receiving_epa",
    "receiving_2pt_conversions", "racr", "target_share", "air_yards_share",
    "wopr", "special_teams_tds", "fantasy_points", "fantasy_points_ppr",
]

POSITION_MAP = {
    # RB family
    "RB": "RB", "HB": "RB", "FB": "RB",
    # WR family
    "WR": "WR", "FL": "WR", "SE": "WR",
    # TE / QB stable
    "TE": "TE", "QB": "QB",
}
SKILL_POSITIONS = {"QB", "RB", "WR", "TE"}
POSITION_GROUP = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE"}

MULTI_TEAM_PATTERN = re.compile(r"^\d+TM$")  # "2TM", "3TM", "4TM"


def _to_int(s) -> int:
    """Parse to int; blank / non-numeric → 0."""
    if s is None:
        return 0
    s = str(s).strip()
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def _to_float(s):
    """Parse to float; blank → None (so pandas emits NaN)."""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Per-season aggregation
# ---------------------------------------------------------------------------

def _collapse_multi_team(rows: list[dict]) -> list[dict]:
    """Collapse PFR multi-team rows.

    PFR emits, for a player who played for 2+ teams in one season:
      • one combined row with team="2TM" (the *correct* stat totals)
      • two or more per-team rows immediately after, with team="DAL", "MIA", etc.

    We keep the combined row and stash the *last* per-team abbreviation as
    ``recent_team``. For single-team seasons we keep the row as-is.

    Input rows must be from a single PFR table and already sorted as PFR
    emits them.
    """
    out: list[dict] = []
    # Group by (pfr_id) in order. PFR keeps the multi-team block contiguous.
    by_id: dict[str, list[dict]] = {}
    order: list[str] = []
    for r in rows:
        pid = r["pfr_id"]
        if pid not in by_id:
            by_id[pid] = []
            order.append(pid)
        by_id[pid].append(r)

    for pid in order:
        group = by_id[pid]
        if len(group) == 1:
            r = dict(group[0])
            r["recent_team"] = r.get("team", "")
            out.append(r)
            continue

        # Find the combined row.
        combined = next(
            (r for r in group if MULTI_TEAM_PATTERN.match(r.get("team", ""))),
            None,
        )
        if combined is None:
            # No combined row — odd; just take the first row and warn.
            log.warning("multi-row group for %s has no XTM combined row; using first row", pid)
            r = dict(group[0])
            r["recent_team"] = r.get("team", "")
            out.append(r)
            continue

        per_team = [r for r in group if not MULTI_TEAM_PATTERN.match(r.get("team", ""))]
        last_team = per_team[-1]["team"] if per_team else combined.get("team", "")
        merged = dict(combined)
        merged["recent_team"] = last_team
        out.append(merged)

    return out


def _index_by_id(rows: list[dict]) -> dict[str, dict]:
    """Index rows by pfr_id. Caller must have already collapsed multi-team rows."""
    return {r["pfr_id"]: r for r in rows}


def _normalize_position(raw_pos: str) -> str | None:
    """Normalize PFR position tokens. Returns None for non-skill positions.

    PFR sometimes emits multi-position strings like "RB/FB" — take the
    first token.
    """
    if not raw_pos:
        return None
    head = re.split(r"[/,\-\s]", raw_pos.strip())[0].upper()
    return POSITION_MAP.get(head)


def _infer_position(p: dict, ru: dict, rec: dict) -> str | None:
    """v3.9 — last-resort position inference for pre-1970 PFR data.

    PFR's pre-1970 rushing/receiving tables don't carry a ``pos`` field
    and there's no fantasy table. We classify by stat profile:

    * Anyone with >= 20 pass attempts → QB (era-appropriate floor; some
      old QBs only threw 30-40 passes/year).
    * Anyone with significant rushing volume (>= 50 carries) that's at
      least 3× their receiving volume → RB.
    * Anyone with >= 15 receptions and receptions > carries → WR.
    * Otherwise (low-volume rushers / receivers) → None (filtered out).

    TE as a distinct position didn't really exist as a fantasy concept
    until the 1960s, and pre-1970 there's no reliable way to separate
    WRs from TEs without bio lookups. We bucket all pre-1970 pass
    catchers as WR to avoid spurious TE classifications.
    """
    pass_att = _to_int(p.get("pass_att"))
    rush_att = _to_int(ru.get("rush_att"))
    rec_ct = _to_int(rec.get("rec"))

    if pass_att >= 20:
        return "QB"
    if rush_att >= 50 and rush_att >= 3 * max(rec_ct, 1):
        return "RB"
    if rec_ct >= 15 and rec_ct > rush_att:
        return "WR"
    if rush_att >= 50:
        return "RB"
    if rec_ct >= 15:
        return "WR"
    return None


def _qualifies(carries: int, targets: int | None, receptions: int, attempts: int) -> bool:
    """Universe threshold from V2.4-PRE1999-LEGENDS.md §2:
    carries ≥ 50 OR targets ≥ 20 OR pass attempts ≥ 100.

    Pre-1992 ``targets`` is missing entirely — we fall back to
    ``receptions ≥ 15``, which is roughly equivalent (~75% catch rate
    was the era norm). Without this fallback we'd lose the entire 1980s
    WR / TE universe.
    """
    if carries >= 50:
        return True
    if targets is not None and targets >= 20:
        return True
    if receptions >= 15:
        return True
    if attempts >= 100:
        return True
    return False


def _fetch_table_safe(year: int, table: str, league: str = "NFL") -> list[dict]:
    """Fetch + parse + collapse one PFR season table; tolerate failures.

    Older PFR pages (pre-1970) lack the precomputed ``fantasy`` table.
    Wayback also occasionally 403s a specific snapshot. Either way we
    log + return ``[]`` so the year can still produce rows from the
    other tables.
    """
    try:
        html = fetch_season_table(year, table, league=league)
    except Exception as exc:  # noqa: BLE001
        log.warning("  %s %s table fetch failed for %d: %s", league, table, year, exc)
        return []
    try:
        rows = parse_season_table(html, table, year)
    except Exception as exc:  # noqa: BLE001
        log.warning("  %s %s table parse failed for %d: %s", league, table, year, exc)
        return []
    return _collapse_multi_team(rows)


def _leagues_for_year(year: int) -> tuple[str, ...]:
    """Return the rival-league PFR slugs to scrape for a given season.

    * 1946–1949: NFL + AAFC (the All-America Football Conference, where
      Otto Graham / Marion Motley / Buddy Young played).
    * 1960–1969: NFL + AFL (the American Football League, where O.J.
      Simpson / Lance Alworth / Joe Namath / Larry Csonka played).
    * Everything else: NFL only.
    """
    leagues = ["NFL"]
    if 1946 <= year <= 1949:
        leagues.append("AAFC")
    if 1960 <= year <= 1969:
        leagues.append("AFL")
    return tuple(leagues)


# PFR pre-computed fantasy.htm tables exist from 1970 onward (modern
# fantasy era). For 1936–1969 the URL still resolves but the page has
# no data tables — we skip the fetch to avoid 10+min of retry-backoff
# burn on 403/empty pages.
FANTASY_TABLE_FROM_YEAR = 1970


def build_season_rows(year: int) -> list[dict]:
    """Build normalized nflverse-schema rows for one PFR season.

    Pulls all four PFR tables (NFL, plus AFL 1960-1969 / AAFC 1946-1949
    when applicable), joins them on pfr_id, applies position + universe
    filters, returns one dict per qualifying player.

    Pre-1970 the fantasy table is absent (PFR didn't compute fantasy
    points that far back). For those years we synthesize the universe
    from the union of passing/rushing/receiving pfr_ids, derive position
    from whichever table the player appears in, and compute the fantasy
    point total using ``dynasty.scoring_rules.score_season`` (sf_ppr).
    """
    log.info("processing %d", year)
    leagues = _leagues_for_year(year)
    tables_to_pull = list(TABLES) if year >= FANTASY_TABLE_FROM_YEAR else [
        t for t in TABLES if t != "fantasy"
    ]
    raw: dict[str, list[dict]] = {tbl: [] for tbl in TABLES}
    for league in leagues:
        for tbl in tables_to_pull:
            raw[tbl].extend(_fetch_table_safe(year, tbl, league=league))

    fantasy = _index_by_id(raw["fantasy"])
    passing = _index_by_id(raw["passing"])
    rushing = _index_by_id(raw["rushing"])
    receiving = _index_by_id(raw["receiving"])

    out_rows: list[dict] = []

    if fantasy:
        universe_ids = list(fantasy.keys())
    else:
        # Pre-1970 path — union of stat-specific tables.
        log.info("  no fantasy table for %d; building universe from passing/rushing/receiving", year)
        universe_ids = list({**passing, **rushing, **receiving}.keys())

    # The fantasy table (when present) is the universe — it includes
    # anyone with any offensive production. Players missing from it
    # (pure ST / D) we don't want.
    for pfr_id in universe_ids:
        f = fantasy.get(pfr_id, {})
        p_stats = passing.get(pfr_id, {})
        ru_stats = rushing.get(pfr_id, {})
        rec_stats = receiving.get(pfr_id, {})

        # Position resolution. Prefer fantasy.fantasy_pos; else use the
        # ``pos`` field on whichever stat table carries one for this
        # player; else (pre-1970, when PFR's rushing/receiving tables
        # don't carry pos) infer from the player's stat profile.
        pos = _normalize_position(f.get("fantasy_pos", ""))
        if pos is None:
            for stats_dict in (p_stats, ru_stats, rec_stats):
                pos = _normalize_position(stats_dict.get("pos", ""))
                if pos is not None:
                    break
        if pos is None:
            pos = _infer_position(p_stats, ru_stats, rec_stats)
        if pos not in SKILL_POSITIONS:
            continue

        # Shorthand aliases for the rest of the function body.
        p = p_stats
        ru = ru_stats
        rec = rec_stats

        # Universe threshold. Use fantasy's pre-rolled-up totals when
        # available; else pull from the per-stat tables directly.
        carries = (
            _to_int(f.get("rush_att")) if f
            else _to_int(ru.get("rush_att"))
        )
        attempts = (
            _to_int(f.get("pass_att")) if f
            else _to_int(p.get("pass_att"))
        )
        receptions = (
            _to_int(f.get("rec")) if f
            else _to_int(rec.get("rec"))
        )
        # ``targets`` only exists 1992+ on the receiving table.
        raw_targets = rec.get("targets")
        targets_int = _to_int(raw_targets) if raw_targets else None
        if not _qualifies(carries, targets_int, receptions, attempts):
            continue

        # ``recent_team`` resolution: the fantasy table doesn't emit
        # per-team duplicate rows for multi-team seasons (only the
        # combined row, with team="2TM"), so when the fantasy row has
        # an XTM team we must look up the *actual* last team from
        # whichever stat-specific table tracks per-team rows. Pick by
        # position: rushing for RBs, receiving for WR/TE, passing for
        # QBs. Each of those tables ran through _collapse_multi_team
        # which set ``recent_team`` to the last per-team abbreviation.
        recent_team = (
            f.get("recent_team")
            or f.get("team", "")
            or p.get("recent_team") or p.get("team", "")
            or ru.get("recent_team") or ru.get("team", "")
            or rec.get("recent_team") or rec.get("team", "")
        )
        if recent_team and MULTI_TEAM_PATTERN.match(recent_team):
            stat_table = {
                "RB": ru,
                "WR": rec,
                "TE": rec,
                "QB": p,
            }[pos]
            stat_team = stat_table.get("recent_team") or ""
            if stat_team and not MULTI_TEAM_PATTERN.match(stat_team):
                recent_team = stat_team

        # Helper: prefer fantasy table's pre-rolled value; else fall
        # back to the per-stat table.
        def _f_or(stat_table: dict, key_f: str, key_alt: str) -> int:
            v = _to_int(f.get(key_f)) if f else 0
            if v:
                return v
            return _to_int(stat_table.get(key_alt))

        player_name = (
            f.get("player_name")
            or p.get("player_name")
            or ru.get("player_name")
            or rec.get("player_name")
            or ""
        )
        games_played = (
            _to_int(f.get("g")) if f.get("g") else
            _to_int(p.get("games") or ru.get("games") or rec.get("games"))
        )
        passing_yds = _f_or(p, "pass_yds", "pass_yds")
        passing_tds = _f_or(p, "pass_td", "pass_td")
        interceptions = _f_or(p, "pass_int", "pass_int")
        rushing_yds = _f_or(ru, "rush_yds", "rush_yds")
        rushing_tds = _f_or(ru, "rush_td", "rush_td")
        receiving_yds = _f_or(rec, "rec_yds", "rec_yds")
        receiving_tds = _f_or(rec, "rec_td", "rec_td")

        row = {
            "season": year,
            "season_type": "REG",
            "player_id": f"pfr_{pfr_id}",
            "player_name": player_name,
            "player_display_name": player_name,
            "position": pos,
            "position_group": POSITION_GROUP[pos],
            "headshot_url": "",
            "games": games_played,
            "recent_team": recent_team,
            # Passing stats — fantasy table has cmp/att/yds/td/int but no
            # sacks; the dedicated passing table fills those in.
            "completions": _f_or(p, "pass_cmp", "pass_cmp"),
            "attempts": attempts,
            "passing_yards": passing_yds,
            "passing_tds": passing_tds,
            "interceptions": interceptions,
            "sacks": _to_int(p.get("pass_sacked")),
            "sack_yards": _to_int(p.get("pass_sacked_yds")),
            "sack_fumbles": 0,
            "sack_fumbles_lost": 0,
            "passing_air_yards": 0,
            "passing_yards_after_catch": 0,
            "passing_first_downs": 0,
            "passing_epa": None,
            "passing_2pt_conversions": _to_int(f.get("two_pt_pass")),
            "pacr": None,
            "dakota": None,
            # Rushing stats.
            "carries": carries,
            "rushing_yards": rushing_yds,
            "rushing_tds": rushing_tds,
            "rushing_fumbles": 0,
            "rushing_fumbles_lost": _to_int(f.get("fumbles_lost")),
            "rushing_first_downs": 0,
            "rushing_epa": None,
            "rushing_2pt_conversions": 0,
            # Receiving.
            "receptions": receptions,
            "targets": targets_int if targets_int is not None else 0,
            "receiving_yards": receiving_yds,
            "receiving_tds": receiving_tds,
            "receiving_fumbles": 0,
            "receiving_fumbles_lost": 0,
            "receiving_air_yards": 0,
            "receiving_yards_after_catch": 0,
            "receiving_first_downs": 0,
            "receiving_epa": None,
            "receiving_2pt_conversions": 0,
            "racr": None,
            "target_share": None,
            "air_yards_share": 0,
            "wopr": None,
            "special_teams_tds": 0,
            "fantasy_points": _to_float(f.get("fantasy_points")) or 0.0,
            "fantasy_points_ppr": _to_float(f.get("fantasy_points_ppr")) or 0.0,
        }

        # v3.9: when the fantasy table is missing (pre-1970), compute
        # fantasy_points_ppr ourselves from the raw stats using the
        # canonical sf_ppr scoring. This is what the engine's downstream
        # PlayerSeason init reads.
        if not row["fantasy_points_ppr"]:
            row["fantasy_points_ppr"] = round(score_season(row, "sf_ppr", pos), 1)
        if not row["fantasy_points"]:
            row["fantasy_points"] = round(score_season(row, "std", pos), 1)

        out_rows.append(row)

    log.info("  %d qualifying skill players in %d", len(out_rows), year)
    return out_rows


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-year", type=int, default=DEFAULT_FROM_YEAR,
        help=f"First season to scrape (inclusive, default {DEFAULT_FROM_YEAR}).",
    )
    parser.add_argument(
        "--to-year", type=int, default=DEFAULT_TO_YEAR,
        help=f"Last season to scrape (inclusive, default {DEFAULT_TO_YEAR}).",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    years = range(args.from_year, args.to_year + 1)
    log.info("building pre-1999 corpus for seasons %d..%d", args.from_year, args.to_year)

    all_rows: list[dict] = []
    for year in years:
        try:
            all_rows.extend(build_season_rows(year))
        except Exception as exc:  # noqa: BLE001
            log.warning("skipping %d (scrape/parse error: %s)", year, exc)

    df = pd.DataFrame(all_rows, columns=NFLVERSE_COLUMNS)

    # Sanity: drop any row that ended up with no useful production.
    has_production = (
        (df["carries"] > 0) | (df["attempts"] > 0) | (df["receptions"] > 0)
    )
    df = df[has_production].reset_index(drop=True)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT_PATH, index=False, compression="gzip")

    log.info("wrote %d rows → %s", len(df), OUTPUT_PATH)
    log.info("  unique players: %d", df["player_id"].nunique())
    log.info("  seasons: %s..%s", df["season"].min(), df["season"].max())
    by_pos = df.groupby("position").size().to_dict()
    log.info("  by position: %s", by_pos)


if __name__ == "__main__":
    main()
