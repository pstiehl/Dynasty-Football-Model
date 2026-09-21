"""Compact dated KeepTradeCut value history.

Why this module exists
----------------------
``scripts/refresh_ktc_consensus.py`` already calls
``keeptradecut.save_snapshot(dated=True)``, which writes
``data/consensus/ktc_YYYY-MM-DD.json``. It looks like this project keeps a
KTC history. It does not:

* that file is ~360 KB, and
* ``.gitignore`` excludes it via
  ``data/consensus/ktc_[0-9]*-[0-9]*-[0-9]*.json``, and
* ``.github/workflows/daily-refresh.yml`` runs with
  ``permissions: contents: read`` and has no commit step.

So every dated snapshot the daily job writes is deleted along with the
runner. At the time of writing ``data/consensus/`` contains exactly two
files — ``ktc_latest.json`` and ``dp_playerids.csv`` — and zero dated
snapshots. **There is no KTC price history to value a past trade against,
and none can be reconstructed from anything on disk.**

What this module does about it
------------------------------
It keeps a *small* file instead of a big one. A value history needs only
``ktc_id -> value``: names, ages, teams, ADP, tiers and trend are all
re-derivable from the current snapshot because ``ktc_id`` is stable. That
takes one day from ~360 KB to ~6 KB, which is cheap enough to commit every
day indefinitely (~2 MB/year) and cheap enough for a browser to fetch one
day at a time.

This **cannot be backfilled**. History begins the first time the daily job
runs with this code deployed. Everything before that date is valued at
current prices and must be labelled as such — see
``dynasty.managerscore``, which degrades per-transaction rather than
silently pretending a current price is a historical one.

Deliberately stdlib-only: ``dynasty.sources.keeptradecut`` cannot be
imported without ``httpx`` (via ``dynasty.sources.__init__`` ->
``base``), and both the site build and the test suite need to read history
without a network stack installed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

CONSENSUS_DIR = Path("data/consensus")
HISTORY_DIR = CONSENSUS_DIR / "history"
HISTORY_PREFIX = "ktc_values_"
HISTORY_SCHEMA = "ktc.values.v1"
INDEX_SCHEMA = "ktc.values.index.v1"

#: KTC marks rookie draft picks with this position code.
PICK_POSITION = "RDP"


# ---------------------------------------------------------------------------
# Building a compact record
# ---------------------------------------------------------------------------

def compact_values(snapshot: Dict) -> Dict:
    """Reduce a full ``ktc.v1`` snapshot dict to the record we retain.

    Takes the *dict* form (``keeptradecut.snapshot_to_dict`` output, i.e.
    exactly what ``ktc_latest.json`` holds) rather than the dataclass, so
    this module stays importable without the sources package.

    Players are keyed by the stable ``ktc_id``. Rookie draft picks are keyed
    by their label ("2027 Mid 1st") instead, because a pick's ktc_id carries
    no stable meaning across seasons while the label is exactly what a
    consumer looks up.
    """
    sf: Dict[str, int] = {}
    one_qb: Dict[str, int] = {}
    picks: Dict[str, int] = {}

    for row in snapshot.get("players") or []:
        if not isinstance(row, dict):
            continue
        name = (row.get("name") or "").strip()
        position = (row.get("position") or "").strip().upper()
        sf_val = _as_int((row.get("superflex") or {}).get("value"))
        qb_val = _as_int((row.get("one_qb") or {}).get("value"))

        if position == PICK_POSITION:
            if name and sf_val is not None:
                picks[name] = sf_val
            continue

        ktc_id = row.get("ktc_id")
        if ktc_id is None:
            continue
        key = str(ktc_id)
        if sf_val is not None:
            sf[key] = sf_val
        if qb_val is not None:
            one_qb[key] = qb_val

    player_values = list(sf.values())
    return {
        "schema": HISTORY_SCHEMA,
        "date": _date_of(snapshot),
        "captured_at": snapshot.get("captured_at"),
        "sf": sf,
        "one_qb": one_qb,
        "picks": picks,
        # Lowest published value on the board that day. Consumers need this
        # to price an asset that is *off* the board without pretending it is
        # worth nothing: KTC publishes a top 500, and the 500th player is
        # not free.
        "floor": min(player_values) if player_values else None,
    }


def _as_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _date_of(snapshot: Dict) -> Optional[str]:
    """UTC calendar date of a snapshot, from its ``captured_at``."""
    raw = snapshot.get("captured_at")
    if isinstance(raw, str) and len(raw) >= 10:
        candidate = raw[:10]
        if candidate[4:5] == "-" and candidate[7:8] == "-":
            return candidate
    return None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def history_point_path(day: str, history_dir: Path = HISTORY_DIR) -> Path:
    return Path(history_dir) / f"{HISTORY_PREFIX}{day}.json"


def save_history_point(
    snapshot: Dict, *, history_dir: Path = HISTORY_DIR
) -> Optional[Path]:
    """Write one day's compact value record and refresh the index.

    Idempotent per day: re-running overwrites that day's file rather than
    accumulating duplicates. Returns ``None`` when the snapshot carries no
    usable capture date, because a dated artifact with a guessed date is
    worse than no artifact.
    """
    payload = compact_values(snapshot)
    day = payload.get("date")
    if not day:
        return None
    history_dir = Path(history_dir)
    history_dir.mkdir(parents=True, exist_ok=True)
    path = history_point_path(day, history_dir)
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    write_history_index(history_dir)
    return path


def available_history_dates(history_dir: Path = HISTORY_DIR) -> List[str]:
    """Sorted ISO dates for which a compact value record exists on disk."""
    history_dir = Path(history_dir)
    if not history_dir.is_dir():
        return []
    days: List[str] = []
    for child in history_dir.iterdir():
        name = child.name
        if not name.startswith(HISTORY_PREFIX) or not name.endswith(".json"):
            continue
        day = name[len(HISTORY_PREFIX):-len(".json")]
        if _looks_like_date(day):
            days.append(day)
    return sorted(days)


def _looks_like_date(value: str) -> bool:
    return (
        len(value) == 10
        and value[4] == "-"
        and value[7] == "-"
        and value[:4].isdigit()
        and value[5:7].isdigit()
        and value[8:].isdigit()
    )


def write_history_index(history_dir: Path = HISTORY_DIR) -> Dict:
    """Write ``index.json`` describing which dates are retained."""
    history_dir = Path(history_dir)
    history_dir.mkdir(parents=True, exist_ok=True)
    dates = available_history_dates(history_dir)
    index = {
        "schema": INDEX_SCHEMA,
        "dates": dates,
        "earliest": dates[0] if dates else None,
        "latest": dates[-1] if dates else None,
        "count": len(dates),
        "file_template": HISTORY_PREFIX + "{date}.json",
    }
    (history_dir / "index.json").write_text(
        json.dumps(index, indent=2), encoding="utf-8"
    )
    return index


def load_history_point(
    day: str, history_dir: Path = HISTORY_DIR
) -> Optional[Dict]:
    path = history_point_path(day, history_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def build_series(history_dir: Path = HISTORY_DIR) -> Dict:
    """Collapse every retained day into one aligned time series.

    ``{"dates": [d0, d1, ...], "sf": {ktc_id: [v0, v1, ...]}, "picks": {...}}``
    with ``null`` where a player was not on the board that day.

    One artifact instead of N dated files because the page needs the *whole*
    series per asset, not one day: valuing a transaction requires the value
    on its date **and** the peak over every later date. Fetching 27 files to
    answer that would be 27 round trips on page load.
    """
    dates = available_history_dates(history_dir)
    sf: Dict[str, List[Optional[int]]] = {}
    picks: Dict[str, List[Optional[int]]] = {}
    floors: List[Optional[int]] = []

    for i, day in enumerate(dates):
        point = load_history_point(day, history_dir) or {}
        floors.append(point.get("floor"))
        for key, value in (point.get("sf") or {}).items():
            sf.setdefault(key, [None] * len(dates))[i] = value
        for key, value in (point.get("picks") or {}).items():
            picks.setdefault(key, [None] * len(dates))[i] = value

    # Rows created part-way through are short; pad them to full length.
    for table in (sf, picks):
        for key, row in table.items():
            if len(row) < len(dates):
                row.extend([None] * (len(dates) - len(row)))

    return {
        "schema": "ktc.series.v1",
        "dates": dates,
        "floors": floors,
        "sf": sf,
        "picks": picks,
    }


def nearest_history_date(target: str, dates: List[str]) -> Optional[str]:
    """Latest retained date on or before ``target``.

    Returns ``None`` when the target predates everything retained. The
    caller must then fall back to current values and say so, rather than
    reaching *forward* in time for a price that did not exist yet — which
    would be the single easiest way to fabricate a point-in-time number.
    """
    if not target:
        return None
    best: Optional[str] = None
    for day in sorted(dates or []):
        if day <= target:
            best = day
        else:
            break
    return best
