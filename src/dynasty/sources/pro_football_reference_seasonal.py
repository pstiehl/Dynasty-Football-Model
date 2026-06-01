"""Pro Football Reference seasonal stats scraper (v2.4 pre-1999 corpus).

PFR itself is Cloudflare-protected and refuses scripted access, but the
Internet Archive's Wayback Machine has stable 2024 snapshots of every
season summary page. We fetch through Wayback at a polite 1 req/sec,
cache the raw HTML to disk, and parse with BeautifulSoup.

This module is a *source* in the same sense as the others under
``src/dynasty/sources/`` — but unlike the ranking adapters, it produces
historical seasonal stat rows for the ``player_stats_season_pre1999``
corpus extension. The orchestration script that uses it lives at
``scripts/build_pre1999_corpus.py``.

Public surface (all functions are pure-ish + cache-backed):

* :func:`fetch_season_table(year, table)` → raw HTML string
* :func:`parse_season_table(html, table, year)` → list[dict] of row data
* :func:`fetch_player_bio(pfr_id)` → ``{"pfr_id", "name", "birth_date"}``

The four supported tables are ``passing``, ``rushing``, ``receiving``,
and ``fantasy``. The ``fantasy`` table is the most useful — it has
position, all three stat lines, and PFR-computed fantasy points all in
one place — but the others fill in detail (sacks, targets 1992+) that
``fantasy`` omits.
"""
from __future__ import annotations

import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT = (
    "sandpaw-dynasty-model/2.4 "
    "(+https://pstiehl.github.io/Dynasty-Football-Model/)"
)
SUPPORTED_TABLES = ("passing", "rushing", "receiving", "fantasy")
# Wayback occasionally serves stale 403s for a specific timestamped
# capture even while neighbouring captures respond 200. We try a few
# different years (most recent first) before giving up. As of
# v3.9 (2026-06-01) the 2025 snapshots cover the older NFL season pages
# better than 2024 does — some pre-1970 pages only 200 under 2025.
WAYBACK_TIMESTAMPS = ("2025", "2024", "2023", "2022")
PFR_BASE = "https://www.pro-football-reference.com"

# Polite throttle. PFR's ToS floor is 1 req/sec, but the Wayback Machine
# proxy enforces a tighter cap (empirically ~15 req/min before it starts
# refusing TCP connections). We default to 4s between fetches; callers
# with their own batching needs can override via the env var
# ``PFR_SCRAPER_INTERVAL_SEC``.
MIN_REQUEST_INTERVAL_SEC = float(os.environ.get("PFR_SCRAPER_INTERVAL_SEC", "4.0"))

# Cache lives at <repo_root>/data/pfr_cache/. We resolve relative to this
# file so the module works whether invoked from the repo root or a script
# dir.
_REPO_ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = _REPO_ROOT / "data" / "pfr_cache"

# Wall-clock-shared throttle state. Module-level intentionally: this is a
# single-process scraper.
_last_request_at: float = 0.0


# ---------------------------------------------------------------------------
# HTTP / cache plumbing
# ---------------------------------------------------------------------------

def _throttle() -> None:
    """Block until at least MIN_REQUEST_INTERVAL_SEC has passed since last fetch."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_REQUEST_INTERVAL_SEC:
        time.sleep(MIN_REQUEST_INTERVAL_SEC - elapsed)
    _last_request_at = time.monotonic()


def _http_get(url: str, *, max_retries: int = 3, timeout: int = 60) -> str:
    """GET with throttle + exponential backoff on 4xx/5xx + connection errors.

    Wayback's 429-style response often manifests as a TCP refusal (the
    edge node simply stops accepting) or a stale 403. We treat both the
    same as 5xx and back off aggressively (up to ~3 min) before giving up.

    v3.9: reduced default max_retries from 5 to 3 so that 403s on a
    stale snapshot fall through to the next WAYBACK_TIMESTAMPS entry
    faster. Burning 5 retries on a known-stale 2024 capture before
    trying 2025 added 60+ min to a full-history scrape.
    """
    backoff = 5.0
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        _throttle()
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=timeout,
            )
            if resp.status_code == 200:
                return resp.text
            log.warning(
                "PFR fetch failed (status=%s) for %s (attempt %d/%d)",
                resp.status_code, url, attempt + 1, max_retries,
            )
            last_exc = RuntimeError(f"HTTP {resp.status_code} for {url}")
        except requests.RequestException as exc:
            last_exc = exc
            log.warning(
                "PFR fetch exception for %s (attempt %d/%d): %s",
                url, attempt + 1, max_retries, exc,
            )
        # Don't sleep after the final attempt.
        if attempt < max_retries - 1:
            sleep_s = backoff + random.uniform(0, backoff * 0.25)
            log.info("  sleeping %.1fs before retry", sleep_s)
            time.sleep(sleep_s)
            backoff = min(backoff * 2, 180.0)

    if last_exc:
        raise last_exc
    raise RuntimeError(f"PFR fetch exhausted retries for {url}")


def _http_get_with_timestamp_fallback(pfr_path: str) -> str:
    """Try each Wayback timestamp in turn until one returns 200."""
    last_exc: Optional[Exception] = None
    for ts in WAYBACK_TIMESTAMPS:
        url = f"https://web.archive.org/web/{ts}/{PFR_BASE}{pfr_path}"
        try:
            return _http_get(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("Wayback timestamp %s failed for %s: %s", ts, pfr_path, exc)
            last_exc = exc
            # Brief cooldown before switching timestamps.
            time.sleep(10.0)
    assert last_exc is not None
    raise last_exc





# ---------------------------------------------------------------------------
# Season-table scraper
# ---------------------------------------------------------------------------

def _season_cache_path(year: int, table: str, league: str = "NFL") -> Path:
    if league == "NFL":
        return CACHE_DIR / str(year) / f"{table}.html"
    # AFL (1960-1969) / AAFC (1946-1949) split-league seasons.
    return CACHE_DIR / f"{year}_{league}" / f"{table}.html"


def fetch_season_table(year: int, table: str, league: str = "NFL") -> str:
    """Fetch HTML for one PFR season table (passing/rushing/receiving/fantasy).

    Cached at ``data/pfr_cache/{year}/{table}.html`` (NFL) or
    ``data/pfr_cache/{year}_{league}/{table}.html`` (AFL / AAFC).
    Re-runs that hit the cache do not make network requests.
    """
    if table not in SUPPORTED_TABLES:
        raise ValueError(f"unsupported table: {table!r}; expected one of {SUPPORTED_TABLES}")

    cache_path = _season_cache_path(year, table, league)
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path.read_text(encoding="utf-8")

    log.info("Fetching PFR %s %d %s from Wayback", table, year, league)
    if league == "NFL":
        path = f"/years/{year}/{table}.htm"
    else:
        path = f"/years/{year}_{league}/{table}.htm"
    html = _http_get_with_timestamp_fallback(path)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(html, encoding="utf-8")
    return html


# ---------------------------------------------------------------------------
# Season-table parser
# ---------------------------------------------------------------------------

# PFR pre-pends "*" / "+" markers to All-Pro / HoF names. Strip them.
_NAME_MARKER_RE = re.compile(r"[\*\+]+$")


def _clean_name(raw: str) -> str:
    return _NAME_MARKER_RE.sub("", raw).strip()


def _to_int(s: str) -> Optional[int]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _to_float(s: str) -> Optional[float]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_season_table(html: str, table: str, year: int) -> list[dict]:
    """Parse one season's PFR table HTML to a list of row dicts.

    The dicts use PFR's native ``data-stat`` keys, with a few extras:

    * ``pfr_id`` — from ``data-append-csv`` on the name cell
    * ``player_name`` — cleaned of HoF / All-Pro markers
    * ``season`` — the year arg, copied in
    * ``table`` — the table name, copied in
    """
    if table not in SUPPORTED_TABLES:
        raise ValueError(f"unsupported table: {table!r}")

    soup = BeautifulSoup(html, "lxml")
    table_el = soup.find("table", id=table)
    if table_el is None:
        log.warning("no <table id=%r> in PFR %d %s", table, year, table)
        return []

    tbody = table_el.find("tbody")
    if tbody is None:
        return []

    rows: list[dict] = []
    for tr in tbody.find_all("tr"):
        # Skip in-table sub-header rows (PFR injects them every ~30 rows).
        cls = tr.get("class") or []
        if "thead" in cls:
            continue

        row: dict = {"season": year, "table": table}
        for cell in tr.find_all(["th", "td"]):
            stat = cell.get("data-stat")
            if not stat:
                continue
            row[stat] = cell.get_text(strip=True)
            # The name cell carries the PFR id.
            append_csv = cell.get("data-append-csv")
            if append_csv:
                row["pfr_id"] = append_csv

        if "pfr_id" not in row:
            # Skip rows without a stable id (rare aggregate rows).
            continue

        # Normalize the player name field across tables. The fantasy table
        # uses ``player``; others use ``name_display``.
        raw_name = row.get("name_display") or row.get("player") or ""
        row["player_name"] = _clean_name(raw_name)

        # Some pages emit duplicate "team" / "team_name_abbr" fields; alias
        # to a single canonical ``team`` for downstream consumers.
        row["team"] = (
            row.get("team_name_abbr")
            or row.get("team")
            or ""
        )
        rows.append(row)

    return rows


# ---------------------------------------------------------------------------
# Player bio scraper
# ---------------------------------------------------------------------------

# PFR player URLs are /players/<first-letter-of-last-name>/<id>.htm
_PFR_ID_RE = re.compile(r"^[A-Za-z]+[A-Za-z]*\d{2}$")


def _bio_cache_path(pfr_id: str) -> Path:
    return CACHE_DIR / "players" / f"{pfr_id}.html"


def _player_path(pfr_id: str) -> str:
    if not _PFR_ID_RE.match(pfr_id):
        raise ValueError(f"invalid PFR id: {pfr_id!r}")
    # The directory letter is the first letter of the *last name*, which
    # is the first character of the id ("SmitEm00" → "S").
    letter = pfr_id[0].upper()
    return f"/players/{letter}/{pfr_id}.htm"


def fetch_player_bio_html(pfr_id: str) -> str:
    """Fetch one player bio page from Wayback (with disk cache)."""
    cache_path = _bio_cache_path(pfr_id)
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path.read_text(encoding="utf-8")
    log.info("Fetching PFR player bio %s", pfr_id)
    html = _http_get_with_timestamp_fallback(_player_path(pfr_id))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(html, encoding="utf-8")
    return html


# PFR puts the birth date in a structured <span id="necro-birth"
# data-birth="YYYY-MM-DD">…</span>. When the snapshot rebuilds the
# template it sometimes drops the id, so we also fall back to a regex
# scan over the page body.
_DATA_BIRTH_RE = re.compile(r'data-birth=["\'](\d{4}-\d{2}-\d{2})["\']')
_NAME_TITLE_RE = re.compile(r"<title>([^<|]+?)\s*(?:Stats|\|)", re.IGNORECASE)


def parse_player_bio(html: str) -> dict:
    """Extract ``{birth_date, name}`` from a PFR player bio page."""
    birth: Optional[str] = None
    soup = BeautifulSoup(html, "lxml")

    span = soup.find("span", id="necro-birth")
    if span and span.get("data-birth"):
        birth = span["data-birth"]

    if not birth:
        m = _DATA_BIRTH_RE.search(html)
        if m:
            birth = m.group(1)

    name = ""
    h1 = soup.find("h1")
    if h1:
        # The display name is in <h1><span>NAME</span></h1>.
        span_name = h1.find("span")
        name = (span_name.get_text(strip=True) if span_name
                else h1.get_text(strip=True))
    if not name:
        m = _NAME_TITLE_RE.search(html)
        if m:
            name = m.group(1).strip()

    return {"birth_date": birth, "name": name}


def fetch_player_bio(pfr_id: str) -> dict:
    """Convenience: fetch + parse a player bio in one call."""
    html = fetch_player_bio_html(pfr_id)
    bio = parse_player_bio(html)
    bio["pfr_id"] = pfr_id
    return bio
