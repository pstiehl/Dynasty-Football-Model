#!/usr/bin/env python3
"""Reconstruct dated KeepTradeCut value boards from public web-archive captures.

Why this exists
---------------
KTC publishes a *live* board, not an archive, and this project never retained
one: ``refresh_ktc_consensus.py`` writes ``ktc_YYYY-MM-DD.json``, ``.gitignore``
excludes that pattern, and the daily workflow had no commit step, so every
dated snapshot died with the runner. Valuing a trade at the price that applied
on the day it happened was therefore impossible.

It turns out the data does exist, just not in this repository. The Wayback
Machine has captured ``keeptradecut.com/dynasty-rankings`` periodically since
2020, and that page server-renders the full ``playersArray`` JSON inline --
the same payload ``dynasty.sources.keeptradecut.parse_ktc_html`` already
parses. Each usable capture is a real KTC board, published by KTC, on a known
date. **Nothing is interpolated, modelled or invented.**

What it produces
----------------
One compact record per recovered date in ``data/consensus/history/``
(``ktc_values_YYYY-MM-DD.json``, ~6 KB), plus a refreshed ``index.json``.
Same format the daily job writes, so backfilled and going-forward data are
indistinguishable to consumers.

Yield, measured
---------------
66 captures returned HTTP 200; **26 contained a parseable ``playersArray``**.
The other 40 are captures where the inline payload is absent (truncated
captures, and a page structure on some dates that does not embed it). That is
the honest yield: a real but *sparse* series, roughly

    2021: 5   2022: 6   2023: 1   2024: 3   2025: 8   2026: 2

with gaps of weeks to months. Consumers must treat "peak" as the highest
value *observed*, not a true maximum, and must price a transaction from the
nearest board on or before its date. ``dynasty.managerscore`` does both and
says so on the page.

``ktc_id`` stability was verified before trusting the join: comparing 2021,
2022 and 2025 captures against the current snapshot, ids matched names
100% / 98.9% / 100% (the two 2022 "mismatches" are the same players under
changed name forms -- "Zonovan Knight" to "Bam Knight", "Josh Palmer" to
"Joshua Palmer"). Ids are stable across five years, so the archive joins to
today's crosswalk correctly.

NOT part of CI
--------------
This is a one-time reconstruction whose *output* is committed. Do not wire it
into the daily job: web.archive.org refuses connections from GitHub-hosted
runners (see the notes in ``.github/workflows/daily-refresh.yml``), and the
daily job must stay fast. Going-forward coverage comes from
``refresh_ktc_consensus.py`` filing a board per run.

Usage
-----
::

    python3 scripts/backfill_ktc_history.py --dry-run
    python3 scripts/backfill_ktc_history.py
    python3 scripts/backfill_ktc_history.py --limit 5 --delay 3

Stdlib only, so it runs in a bare checkout. Existing dates are skipped, so
re-running is safe and resumable.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dynasty import ktc_history  # noqa: E402

CDX = (
    "http://web.archive.org/cdx/search/cdx"
    "?url=keeptradecut.com/dynasty-rankings"
    "&output=json&fl=timestamp,statuscode"
    "&collapse=timestamp:8&limit=5000"
)
SNAPSHOT = "http://web.archive.org/web/{ts}id_/https://keeptradecut.com/dynasty-rankings"

# Same payload shape dynasty.sources.keeptradecut matches. Duplicated here
# rather than imported because that module pulls httpx in via the sources
# package, and this script is deliberately stdlib-only.
PLAYERS_ARRAY_RE = re.compile(r"(?:var\s+)?playersArray\s*=\s*(\[.*?\]);", re.DOTALL)

UA = (
    "Mozilla/5.0 (+https://github.com/pstiehl/Dynasty-Football-Model; "
    "one-time KTC history backfill)"
)


def _get(url: str, timeout: float) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def list_captures(timeout: float = 45.0) -> list[str]:
    rows = json.loads(_get(CDX, timeout))[1:]
    return [r[0] for r in rows if len(r) > 1 and r[1] == "200"]


def capture_to_record(html: str, ts: str) -> dict | None:
    """Turn one archived page into a compact dated value record."""
    match = PLAYERS_ARRAY_RE.search(html)
    if not match:
        return None
    try:
        rows = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None

    sf: dict[str, int] = {}
    picks: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = (row.get("superflexValues") or {}).get("value")
        if value in (None, ""):
            continue
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        name = (row.get("playerName") or "").strip()
        position = (row.get("position") or "").strip().upper()
        if position == ktc_history.PICK_POSITION:
            if name:
                picks[name] = value
            continue
        pid = row.get("playerID")
        if pid is None:
            continue
        sf[str(pid)] = value

    if not sf:
        return None

    # Floor is the cheapest *real* asset on the board. Some archived days
    # carry 0-valued rows; taking a literal minimum would make the floor
    # meaningless, so only positive values count.
    positive = [v for v in sf.values() if v > 0]
    return {
        "schema": ktc_history.HISTORY_SCHEMA,
        "date": f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}",
        "captured_at": f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}T{ts[8:10]}:{ts[10:12]}:{ts[12:14]}Z",
        "sf": sf,
        "one_qb": {},
        "picks": picks,
        "floor": min(positive) if positive else None,
        "source": f"web.archive.org/web/{ts}",
    }


def backfill(
    history_dir: Path,
    *,
    delay: float = 1.5,
    timeout: float = 90.0,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    captures = list_captures()
    if limit:
        captures = captures[:limit]
    history_dir.mkdir(parents=True, exist_ok=True)

    written, skipped, unusable, failed = 0, 0, 0, 0
    for ts in captures:
        day = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
        dest = ktc_history.history_point_path(day, history_dir)
        if dest.exists():
            skipped += 1
            continue
        try:
            html = _get(SNAPSHOT.format(ts=ts), timeout)
        except Exception as exc:  # noqa: BLE001 - archive flakiness is expected
            print(f"  {ts}  fetch failed: {type(exc).__name__}")
            failed += 1
            time.sleep(delay)
            continue

        record = capture_to_record(html, ts)
        if record is None:
            print(f"  {ts}  no usable playersArray ({len(html)} bytes)")
            unusable += 1
            time.sleep(delay)
            continue

        if dry_run:
            print(f"  {ts}  WOULD WRITE {day}: {len(record['sf'])} players")
        else:
            dest.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
            print(
                f"  {ts}  wrote {day}: {len(record['sf'])} players, "
                f"{len(record['picks'])} picks, floor {record['floor']}"
            )
        written += 1
        time.sleep(delay)

    if not dry_run:
        ktc_history.write_history_index(history_dir)

    return {
        "captures": len(captures),
        "written": written,
        "skipped": skipped,
        "unusable": unusable,
        "failed": failed,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--history-dir", type=Path, default=ROOT / "data/consensus/history")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="Seconds between archive requests. Be polite.")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    print("Backfilling KTC value history from web.archive.org…")
    try:
        summary = backfill(
            args.history_dir,
            delay=args.delay,
            timeout=args.timeout,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"backfill FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(
        "\n{captures} capture(s): {written} written, {skipped} already present, "
        "{unusable} without a usable payload, {failed} fetch failures".format(**summary)
    )
    dates = ktc_history.available_history_dates(args.history_dir)
    if dates:
        print(f"history now spans {dates[0]} .. {dates[-1]} ({len(dates)} dates)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
