#!/usr/bin/env python3
"""v3.3 — Refresh PFR NFL draft-class data (Phil 2026-05-28).

Pulls one or more years of NFL draft results from PFR (via Wayback)
and writes:

    data/pfr/draft_class_<YEAR>.json           # one file per year
    data/pfr/draft_classes_all.json            # combined map year -> [picks]

These records get joined onto the v3.0 prospect corpus by
``build_prospects_v3.py`` (v3.3 update) so the Prospects tab can:

  * Mark prospects who were actually drafted
  * Show the team that drafted them
  * Filter to "just-drafted rookies" instead of mixing in 5,000
    college-only names

Usage::

    PYTHONPATH=src python3 scripts/refresh_pfr_draft_classes.py \\
        [--years 2022,2023,2024,2025,2026]

Idempotent: re-running with cached HTML produces byte-identical output.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Iterable, List

# Allow running without setting PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dynasty.sources.pfr_draft_class import DraftPick, fetch_and_parse

log = logging.getLogger("refresh_pfr_draft_classes")

DEFAULT_YEARS = (2022, 2023, 2024, 2025, 2026)
# v3.9 — ``--all-history`` sweeps from 1936 (first NFL draft) to the most
# recent year. Used by ``v3.9: full PFR draft-class corpus expansion``.
FULL_HISTORY_FROM = 1936
FULL_HISTORY_TO = 2026
OUT_DIR = Path("data/pfr")


def refresh_year(year: int) -> List[DraftPick]:
    log.info("Refreshing PFR draft class %d", year)
    picks = fetch_and_parse(year)
    log.info("  → %d total picks (%d skill)",
             len(picks),
             sum(1 for p in picks if p.position in ("QB", "RB", "WR", "TE")))
    return picks


def write_outputs(years: Iterable[int], all_picks: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for y, picks in all_picks.items():
        path = OUT_DIR / f"draft_class_{y}.json"
        with path.open("w", encoding="utf-8") as f:
            json.dump(
                {"year": y, "picks": [p.to_dict() for p in picks]},
                f, indent=2, ensure_ascii=False, sort_keys=True,
            )
            f.write("\n")
        log.info("  wrote %s", path)
    combined = OUT_DIR / "draft_classes_all.json"
    payload = {
        "years": sorted(all_picks.keys()),
        "by_year": {
            str(y): [p.to_dict() for p in all_picks[y]]
            for y in sorted(all_picks.keys())
        },
    }
    with combined.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    log.info("  wrote %s", combined)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--years", default=",".join(str(y) for y in DEFAULT_YEARS),
        help=(
            "Comma-separated draft-class years to refresh. Ignored when "
            "--all-history / --from-year / --to-year is supplied."
        ),
    )
    parser.add_argument(
        "--all-history", action="store_true",
        help=f"Refresh every draft class from {FULL_HISTORY_FROM} to {FULL_HISTORY_TO}.",
    )
    parser.add_argument(
        "--from-year", type=int, default=None,
        help="Range start (inclusive). Overrides --years when paired with --to-year.",
    )
    parser.add_argument(
        "--to-year", type=int, default=None,
        help="Range end (inclusive). Overrides --years when paired with --from-year.",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.all_history:
        years = list(range(FULL_HISTORY_FROM, FULL_HISTORY_TO + 1))
    elif args.from_year is not None and args.to_year is not None:
        years = list(range(args.from_year, args.to_year + 1))
    else:
        years = [int(s) for s in args.years.split(",") if s.strip()]

    all_picks: dict = {}
    for y in years:
        try:
            all_picks[y] = refresh_year(y)
        except Exception as exc:  # noqa: BLE001
            log.warning("skipping draft class %d (error: %s)", y, exc)
    write_outputs(sorted(all_picks.keys()), all_picks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
