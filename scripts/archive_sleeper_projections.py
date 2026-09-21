#!/usr/bin/env python3
"""Archive Sleeper's weekly projections, stamped with the date we captured them.

Why this exists
---------------
The owner asked whether a trade can be judged on "projected points at the
time versus actuals". Answering that needs a projection that provably
predates the outcome. Sleeper will happily serve projection rows for past
weeks, but they are not such a record, and the investigation that
established this is worth keeping next to the fix:

* ``GET https://api.sleeper.app/projections/nfl/{season}/{week}`` answers for
  past seasons (2023-2026 all return 750 rows).
* Every row carries ``updated_at``. For 2025 week 11 it reads 2025-11-18 --
  one day after those games. For 2025 week 2, whose games were played
  11-15 September 2025, it reads **2025-10-06**, three weeks later. For
  2023 the field is absent altogether.

So the served rows are rewritten after the fact, by an unknown amount, and
there is no way to recover what was displayed before kickoff. Using them as
"what we expected" would be presenting a post-hoc number as a forecast.
That is the one thing this project will not do, so the past is left alone.

What this script does instead is start the record. Each run writes one dated
file containing the projections **as they stand at capture time**, together
with the capture timestamp and the ``updated_at`` Sleeper reported. Once a
transaction happens after a snapshot exists, expected-versus-actual can be
computed from a projection that demonstrably predates the result.

This mirrors ``scripts/backfill_ktc_history.py`` and the dated KTC boards in
``data/consensus/history/``: small files, one per capture, provenance on
every record. It cannot be backfilled, which is exactly the point.

A second reason the archive stores the raw scoring variants rather than one
number: Sleeper publishes generic ``pts_ppr`` / ``pts_half_ppr`` /
``pts_std``, none of which is any particular league's scoring. Whoever
consumes this later will need to pick the closest variant and say which one
they picked, so all three are kept.

Usage::

    python3 scripts/archive_sleeper_projections.py                # current week
    python3 scripts/archive_sleeper_projections.py --week 4
    python3 scripts/archive_sleeper_projections.py --season 2026 --week 4
    python3 scripts/archive_sleeper_projections.py --dry-run

Standard library only: no pip, no httpx, runnable from a fresh clone.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "projections" / "history"

SLEEPER_STATE = "https://api.sleeper.app/v1/state/nfl"
# `season_type` is required: without it the endpoint answers 400, verified
# against the live API. The position filter is what keeps a snapshot small --
# unfiltered the week returns ~9,400 rows including IDP and kickers.
SLEEPER_PROJ = (
    "https://api.sleeper.app/projections/nfl/{season}/{week}"
    "?season_type=regular"
    "&position[]=QB&position[]=RB&position[]=WR&position[]=TE"
    "&order_by=pts_ppr"
)

SCHEMA = "sleeper.projections.v1"
USER_AGENT = "dynasty-football-model/projection-archiver"

# The scoring variants Sleeper publishes. Kept as-is rather than collapsed:
# none of them is a given league's own scoring, so the choice belongs to the
# consumer, who must then be able to say which one they used.
KEEP_STATS = ("pts_ppr", "pts_half_ppr", "pts_std")
TIMEOUT = 45


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def current_state() -> dict:
    """Sleeper's own idea of the season and week. Beats guessing from a clock."""
    try:
        return _get(SLEEPER_STATE) or {}
    except (urllib.error.URLError, ValueError, OSError) as exc:
        print(f"  ! could not read Sleeper state: {exc}", file=sys.stderr)
        return {}


def fetch_projections(season: str, week: int) -> list:
    url = SLEEPER_PROJ.format(season=season, week=week)
    try:
        rows = _get(url)
    except (urllib.error.URLError, ValueError, OSError) as exc:
        print(f"  ! fetch failed for {season} wk{week}: {exc}", file=sys.stderr)
        return []
    return rows or []


def distill(rows: list) -> list:
    """Keep only what a later comparison needs, and the provenance to audit it.

    ``updated_at`` is retained per row precisely because it is the field that
    disqualified the historical archive. Anyone reading these files later
    must be able to check, for themselves, that a snapshot predates the games
    it claims to project.
    """
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid = row.get("player_id")
        if pid is None:
            continue
        stats = row.get("stats") or {}
        kept = {k: stats[k] for k in KEEP_STATS if isinstance(stats.get(k), (int, float))}
        if not kept:
            # No projected points in any published variant: nothing to compare
            # against later, so the row would be dead weight in every file.
            continue
        player = row.get("player") or {}
        name = " ".join(
            p for p in [player.get("first_name"), player.get("last_name")] if p
        ).strip()
        out.append(
            {
                "player_id": str(pid),
                "name": name or None,
                "pos": player.get("position") or row.get("position"),
                "team": row.get("team"),
                "opponent": row.get("opponent"),
                "game_date": row.get("date"),
                "stats": kept,
                "sleeper_updated_at": row.get("updated_at"),
            }
        )
    out.sort(key=lambda r: -max(r["stats"].values()))
    return out


def _display(path: Path) -> str:
    """Repo-relative when it can be, absolute otherwise.

    ``relative_to`` raises for any path outside the repo, which a caller can
    legitimately choose (a test temp dir, an operator archiving elsewhere).
    A progress message must never be the thing that fails a run.
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def earliest_kickoff(rows: list) -> str | None:
    """Earliest ``game_date`` in the snapshot, as an ISO date string."""
    dates = sorted(r["game_date"] for r in rows if r.get("game_date"))
    return dates[0] if dates else None


def write_snapshot(season: str, week: int, rows: list, *, dry_run: bool) -> Path | None:
    captured = datetime.now(timezone.utc)

    # A snapshot taken after kickoff is not a forecast, and this project has
    # already refused one post-hoc "point-in-time" number (Sleeper's own
    # rewritten history) on exactly this reasoning. Applying a weaker rule to
    # our own files than to theirs would be indefensible, so the flag is
    # computed, recorded, and warned about rather than left for the reader to
    # work out from two timestamps.
    first_game = earliest_kickoff(rows)
    pre_kickoff = bool(first_game) and captured.date().isoformat() < first_game
    if first_game and not pre_kickoff:
        print(
            f"  ! WARNING: captured {captured.date().isoformat()} but the first "
            f"game of this week was {first_game}. This snapshot postdates "
            f"kickoff and is NOT valid as a point-in-time expectation; it is "
            f"being filed with pre_kickoff=false.",
            file=sys.stderr,
        )

    payload = {
        "schema": SCHEMA,
        "season": str(season),
        "week": int(week),
        "captured_at": captured.isoformat(),
        "source": SLEEPER_PROJ.format(season=season, week=week),
        "stat_keys": list(KEEP_STATS),
        "n_players": len(rows),
        "first_game_date": first_game,
        # The single field a consumer must check before treating this file as
        # a forecast rather than a record of what Sleeper said afterwards.
        "pre_kickoff": pre_kickoff,
        # Stated in the artifact, not only in this docstring, so the caveat
        # travels with the data rather than being lost on the way to a page.
        "note": (
            "Captured live at captured_at. Sleeper rewrites projection rows "
            "after the fact (2025 wk2 rows carried updated_at 2025-10-06 for "
            "games played 11-15 Sep 2025), so only snapshots taken before "
            "kickoff are valid as point-in-time expectations. Compare "
            "captured_at against game_date before using a row as a forecast. "
            "Values are Sleeper's generic scoring variants, not any league's "
            "own settings."
        ),
        "players": rows,
    }
    name = f"projections_{season}_wk{int(week):02d}_{captured.date().isoformat()}.json"
    path = OUT_DIR / name
    if dry_run:
        print(f"  (dry run) would write {_display(path)} "
              f"with {len(rows)} players")
        return None
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--season", default=None, help="Season, e.g. 2026. Default: Sleeper's current.")
    ap.add_argument("--week", default=None, type=int, help="Week. Default: Sleeper's current.")
    ap.add_argument("--dry-run", action="store_true", help="Fetch and report, write nothing.")
    args = ap.parse_args(argv)

    season, week = args.season, args.week
    if season is None or week is None:
        state = current_state()
        season = season or state.get("season")
        # `week` is the week in progress; `display_week` is what the app shows.
        if week is None:
            week = state.get("display_week") or state.get("week")
    if not season or not week:
        print("Could not determine season/week and none was supplied.", file=sys.stderr)
        return 2

    print(f"Sleeper projections — season {season}, week {week}")
    rows = fetch_projections(str(season), int(week))
    if not rows:
        print("  no rows returned; nothing archived.", file=sys.stderr)
        return 1
    kept = distill(rows)
    print(f"  fetched {len(rows)} rows, kept {len(kept)} with projected points")
    if kept:
        top = kept[0]
        print(f"  top projection: {top['name']} ({top['pos']}) {top['stats']}")

    path = write_snapshot(str(season), int(week), kept, dry_run=args.dry_run)
    if path is not None:
        print(f"  wrote {_display(path)} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
