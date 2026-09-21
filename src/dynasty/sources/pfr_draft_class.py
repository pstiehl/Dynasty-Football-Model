"""Pro-Football-Reference NFL draft-class scraper (v3.3).

Pulls one season of NFL draft results from
``https://www.pro-football-reference.com/years/<YYYY>/draft.htm`` via
the Wayback Machine (PFR is Cloudflare-walled for direct hits, same
pattern we use for sports-reference CFB data).

Output: a list of structured records, one per drafted player.

Phil's 2026-05-28 brief: "the 2026 class should include the most
recent rookies that were just drafted in 2026. you should be able to
pull this from pro football reference: <PFR_URL>. and use that link
but for the different years to analyze prospects by draft class."
"""
from __future__ import annotations

import html as html_lib
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator, List, Optional

log = logging.getLogger(__name__)


PFR_BASE = "https://www.pro-football-reference.com"
CACHE_DIR = Path("data/pfr_cache/draft_class")
# Self-identifying User-Agent, matching the rest of the project.
#
# This previously impersonated Firefox 120 on Linux. Pretending to be a
# human browser to get past bot detection is not something this project
# should do: Sports Reference's terms do not permit scraping, and a 403 is
# a deliberate answer, not an obstacle. If a source declines us, the right
# response is to use a source that permits programmatic access - which is
# why the career panel is moving to nflverse.
USER_AGENT = (
    "sandpaw-dynasty-model/3.13 "
    "(+https://pstiehl.github.io/Dynasty-Football-Model/)"
)
HTTP_TIMEOUT_SECS = 30
# Wayback snapshot timestamps to try in order.
WAYBACK_TIMESTAMPS = (
    "2026",
    "2025",
    "20260601000000",
    "20251101000000",
)

# Same kill switch the seasonal scraper honours. web.archive.org refuses
# every connection from GitHub-hosted runners, so in CI each of these
# timestamps is 2s of sleep plus a connection attempt for nothing.
WAYBACK_DISABLED_BY_ENV = os.environ.get("PFR_DISABLE_WAYBACK", "").strip().lower() in (
    "1", "true", "yes",
)


def _is_connection_level(exc: BaseException) -> bool:
    """True when the host refused or dropped us rather than answering.

    A refused connection means the archive is unreachable from this network;
    every remaining timestamp will fail identically, so there is nothing to
    gain by walking the rest of the list.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return False
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        return isinstance(reason, OSError)
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECS) as resp:
        data = resp.read()
    try:
        return data.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


def _wayback_get(year: int, max_attempts: int = 4) -> str:
    pfr_path = f"/years/{year}/draft.htm"

    if WAYBACK_DISABLED_BY_ENV:
        raise RuntimeError(
            f"Wayback disabled via PFR_DISABLE_WAYBACK; no draft source for {year}"
        )

    last_exc: Optional[Exception] = None
    for ts in WAYBACK_TIMESTAMPS[:max_attempts]:
        url = f"https://web.archive.org/web/{ts}/{PFR_BASE}{pfr_path}"
        try:
            log.info("PFR draft fetch %d -> %s", year, url)
            return _http_get(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("Wayback ts=%s failed for year %d: %s", ts, year, exc)
            last_exc = exc
            # The archive is unreachable, not merely missing this snapshot.
            # Walking the remaining timestamps just burns wall-clock.
            if _is_connection_level(exc):
                log.warning(
                    "Wayback unreachable (connection refused) - abandoning "
                    "remaining timestamps for year %d",
                    year,
                )
                break
            time.sleep(2.0)
    if last_exc is None:
        raise RuntimeError(
            f"PFR draft fetch exhausted with no attempts for year={year}"
        )
    raise last_exc


def fetch_draft_html(year: int, *, cache: bool = True) -> str:
    """Fetch (or load from cache) the raw PFR draft.htm for ``year``."""
    cache_path = CACHE_DIR / f"{year}.html"
    if cache and cache_path.exists():
        return cache_path.read_text(encoding="utf-8", errors="replace")
    text = _wayback_get(year)
    if cache:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(text, encoding="utf-8")
    return text


@dataclass
class DraftPick:
    """One drafted player from a single PFR draft season."""

    year: int
    rnd: int
    pick: int                  # overall pick number
    team: Optional[str]
    player_name: str
    pfr_id: Optional[str]      # e.g. "MendFe00"
    position: Optional[str]
    age_at_draft: Optional[int]
    college: Optional[str]
    college_stats_slug: Optional[str]  # sports-reference CFB player slug

    def to_dict(self) -> dict:
        return asdict(self)


# PFR draft rows: <tr><th data-stat="draft_round">N</th>... data rows; we
# anchor on the draft_round th cell to skip header / over-header rows.
_ROW_RE = re.compile(
    r'<tr[^>]*>\s*<th[^>]*?\bdata-stat="draft_round"[^>]*>([0-9]+)S?</th>.*?</tr>',
    re.IGNORECASE | re.DOTALL,
)
_CELL_RE = re.compile(
    r'<t[hd][^>]*?data-stat="([^"]+)"[^>]*>(.*?)</t[hd]>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r'<[^>]+>')
_PFR_ID_RE = re.compile(
    r'/players/[A-Z]/([A-Za-z0-9]+)\.htm', re.IGNORECASE,
)
_CFB_SLUG_RE = re.compile(
    r'/cfb/players/([a-z0-9\-]+)\.html', re.IGNORECASE,
)


def _text_of(html: str) -> str:
    return html_lib.unescape(_TAG_RE.sub("", html)).strip()


def _parse_int(s: str) -> Optional[int]:
    s = s.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def parse_draft_html(year: int, html: str) -> List[DraftPick]:
    """Parse PFR draft.htm body into a list of DraftPick records."""
    picks: List[DraftPick] = []
    # Find the canonical drafts table first so we don't accidentally
    # pick up rows from PFR's navigation / sidebar tables.
    table_match = re.search(
        r'<table[^>]*id="drafts"[^>]*>(.*?)</table>', html,
        re.IGNORECASE | re.DOTALL,
    )
    table_html = table_match.group(1) if table_match else html
    for row_match in _ROW_RE.finditer(table_html):
        # Capture the entire <tr>...</tr> via the surrounding span.
        start, end = row_match.span()
        # The match's group(1) is the round number; we still need the
        # full row HTML for cell extraction. Re-locate via the row's
        # span in table_html.
        row_html = table_html[start:end]
        cells = {key: val for key, val in _CELL_RE.findall(row_html)}
        if not cells:
            continue
        # The th carries data-stat="draft_round" — our cell extractor
        # only catches td/th with data-stat in the value position, so
        # also accept the captured group from the row regex as a
        # fallback for the round value.
        rnd_text = _text_of(cells.get("draft_round", "")) or row_match.group(1)
        rnd = _parse_int(rnd_text)
        pick_no = _parse_int(_text_of(cells.get("draft_pick", "")))
        if rnd is None or pick_no is None:
            continue
        team = _text_of(cells.get("team", "")) or None
        player_html = cells.get("player", "") or cells.get("name_display", "")
        player_name = _text_of(player_html)
        pfr_match = _PFR_ID_RE.search(player_html)
        pfr_id = pfr_match.group(1) if pfr_match else None
        position = _text_of(cells.get("pos", "")) or None
        age = _parse_int(_text_of(cells.get("age", "")))
        college_html = cells.get("college_id", "")
        college_name = _text_of(college_html) or None
        cfb_slug_match = _CFB_SLUG_RE.search(college_html)
        cfb_slug = cfb_slug_match.group(1) if cfb_slug_match else None
        if cfb_slug is None:
            # Some seasons split the cell; try cfb-stats link key.
            for key in ("college_link", "college_stats", "college"):
                alt_html = cells.get(key, "")
                m = _CFB_SLUG_RE.search(alt_html)
                if m:
                    cfb_slug = m.group(1)
                    break
        picks.append(DraftPick(
            year=year,
            rnd=rnd,
            pick=pick_no,
            team=team,
            player_name=player_name,
            pfr_id=pfr_id,
            position=position,
            age_at_draft=age,
            college=college_name,
            college_stats_slug=cfb_slug,
        ))
    return picks


def fetch_and_parse(year: int, *, cache: bool = True) -> List[DraftPick]:
    """Convenience: fetch + parse one year's draft."""
    return parse_draft_html(year, fetch_draft_html(year, cache=cache))


def iter_years(years: Iterable[int], *, cache: bool = True) -> Iterator[DraftPick]:
    for y in years:
        yield from fetch_and_parse(y, cache=cache)
