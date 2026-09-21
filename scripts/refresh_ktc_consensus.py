#!/usr/bin/env python3
"""Refresh the KeepTradeCut consensus rankings cache.

KTC publishes the dynasty-rankings page as a single server-rendered HTML
document that embeds the full ``playersArray`` JSON inline. This script
issues one polite GET (Mozilla UA, ~30s timeout), parses the payload,
and writes:

  * ``data/consensus/ktc_YYYY-MM-DD.json`` — dated snapshot
  * ``data/consensus/ktc_latest.json``     — canonical "latest" pointer

Run once per day; the report builder reads ``ktc_latest.json`` and never
hits KTC directly. If KTC ever blocks us, fall back to ``fantasypros``
(planned).

Exit code 0 on success, 1 on failure (no network/parse error swallowed).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

# Repo-root-relative imports so the script works under ``python3
# scripts/refresh_ktc_consensus.py`` from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dynasty import ktc_history  # noqa: E402
from dynasty.sources.keeptradecut import (  # noqa: E402
    CONSENSUS_DIR,
    KTC_URL,
    parse_ktc_html,
    save_snapshot,
    snapshot_to_dict,
)

DP_CROSSWALK_URL = (
    "https://raw.githubusercontent.com/dynastyprocess/data/master/files/"
    "db_playerids.csv"
)


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36 "
    "(+https://github.com/pstiehl/Dynasty-Football-Model; consensus-only)"
)


def _refresh_crosswalk(client: httpx.Client, consensus_dir: Path) -> int:
    """Pull the dynastyprocess player crosswalk. Returns rows fetched.

    The CSV is regenerated nightly by dynastyprocess and includes
    ``ktc_id``, ``mfl_id``, and ``gsis_id`` columns we use to join KTC
    rows to model players. Failure is non-fatal at the call site.
    """
    resp = client.get(DP_CROSSWALK_URL)
    resp.raise_for_status()
    body = resp.text
    if "ktc_id" not in body.splitlines()[0]:
        raise RuntimeError(
            "dynastyprocess crosswalk header missing ktc_id column "
            "(schema may have changed)"
        )
    out = consensus_dir / "dp_playerids.csv"
    out.write_text(body, encoding="utf-8")
    return body.count("\n")


def refresh(*, consensus_dir: Path = CONSENSUS_DIR, timeout: float = 30.0) -> int:
    """Fetch + cache KTC snapshot AND the dynastyprocess crosswalk.

    Returns the number of players in the KTC snapshot. The crosswalk
    refresh is best-effort: if dynastyprocess is unreachable we keep
    the cached file (the diff still works, just with potentially stale
    ktc_id→gsis_id mappings).
    """
    consensus_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        try:
            n_xwalk = _refresh_crosswalk(client, consensus_dir)
            print(f"  dynastyprocess crosswalk: {n_xwalk} rows")
        except Exception as e:  # noqa: BLE001 - non-fatal
            print(f"  WARN: crosswalk refresh failed: {e}")
        resp = client.get(KTC_URL)
        resp.raise_for_status()
        html = resp.text
    snap = parse_ktc_html(html, source_url=KTC_URL)
    if not snap.players:
        raise RuntimeError(
            "KTC snapshot parsed but contained zero players "
            "(playersArray shape may have changed)"
        )
    save_snapshot(snap, consensus_dir=consensus_dir, dated=True)

    # Retain a compact dated value record as well.
    #
    # ``save_snapshot(dated=True)`` above writes ``ktc_YYYY-MM-DD.json``, but
    # that file is ~360 KB and .gitignore excludes it, and the daily workflow
    # has no commit step -- so it is destroyed with the runner every single
    # run and this project has never retained a single day of KTC price
    # history. Without history, valuing a trade made eight months ago at its
    # contemporaneous price is impossible, which is precisely what the
    # Manager Score page needs.
    #
    # This record keeps only ``ktc_id -> value`` (~6 KB/day), which is small
    # enough to commit daily and is committed back by the daily workflow. It
    # cannot be backfilled; it starts from the first run with this deployed.
    try:
        point = ktc_history.save_history_point(
            snapshot_to_dict(snap),
            history_dir=consensus_dir / "history",
        )
        if point is None:
            print("  WARN: snapshot carried no capture date; history skipped")
        else:
            print(f"  value history: {point}")
    except OSError as e:  # non-fatal: the consensus refresh itself succeeded
        print(f"  WARN: value-history write failed: {e}")

    return len(snap.players)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--consensus-dir",
        type=Path,
        default=CONSENSUS_DIR,
        help="Where to write the cached snapshot files.",
    )
    ap.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds.",
    )
    args = ap.parse_args()
    try:
        n = refresh(consensus_dir=args.consensus_dir, timeout=args.timeout)
    except (httpx.HTTPError, ValueError, RuntimeError) as e:
        print(f"refresh_ktc_consensus FAIL: {e}", file=sys.stderr)
        return 1
    print(f"refresh_ktc_consensus OK: {n} players cached to {args.consensus_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
