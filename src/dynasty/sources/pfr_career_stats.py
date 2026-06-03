"""Per-player career stat scraper (Pro Football Reference).

Adds a *season-by-season* career stats view per ranked player to the
profile pages. We re-use the same Wayback-fronted HTTP plumbing as
:mod:`pro_football_reference_seasonal` (the v2.4 historical scraper),
so this is purely a parser layer + per-player cache around the player
bio HTML the engine *already* downloads for birth-date lookups.

Public surface
--------------

* :func:`build_career_stats(pfr_id, position)` →
  ``{"position": str, "rows": list[dict], "totals": dict, "fp_format": str}``
* :func:`stats_cache_path(pfr_id)` → ``Path`` to JSON cache file
* :func:`career_stats_html(career)` → HTML fragment ready to splice
  into the player profile

The position arg drives which subset of stats we extract:

* QB  — passing-table seasons + rushing line per season
* RB  — rushing_and_receiving seasons (rush-leaning columns)
* WR  — receiving_and_rushing seasons (rec-leaning columns)
* TE  — receiving_and_rushing seasons (rec-leaning columns)

Fantasy points are computed in **Superflex PPR** (1 ppr, 4 passing TDs,
6 rushing/receiving TDs, 1pt per 25 pass yds, 1pt per 10 rush/rec yds,
-2 int, -2 fumble lost). PFR doesn't expose fumbles-lost reliably across
eras, so we apply the more conservative ``fumbles`` total when present
and otherwise treat fumble penalty as 0 — keeping the surfaced FP a
*lower-bound consistent* estimate.

The per-player JSON cache lives at
``data/cache/pfr_career_stats/<pfr_id>.json`` (separate from the raw
HTML cache so we can re-parse without re-scraping). When the JSON cache
is fresher than the HTML cache it's served straight from disk.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

from . import pro_football_reference_seasonal as _pfr

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = _REPO_ROOT / "data" / "cache" / "pfr_career_stats"

# ----------------------------------------------------------------------------
# Fantasy-point scoring (Superflex PPR)
# ----------------------------------------------------------------------------

PASS_YDS_PER_PT = 25.0
RUSH_YDS_PER_PT = 10.0
REC_YDS_PER_PT = 10.0
PT_PER_PASS_TD = 4.0
PT_PER_RUSH_TD = 6.0
PT_PER_REC_TD = 6.0
PT_PER_REC = 1.0          # PPR
PT_PER_INT = -2.0
PT_PER_FUMBLE = -2.0      # fumbles total (conservative — assumes lost)


def _to_int(s: Optional[str]) -> int:
    if not s:
        return 0
    s = s.strip()
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def _to_float(s: Optional[str]) -> float:
    if not s:
        return 0.0
    s = s.strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _season_fp(stats: Dict[str, float]) -> float:
    """Superflex PPR fantasy points for a single season row."""
    fp = 0.0
    fp += stats.get("pass_yds", 0) / PASS_YDS_PER_PT
    fp += stats.get("pass_td", 0) * PT_PER_PASS_TD
    fp += stats.get("pass_int", 0) * PT_PER_INT
    fp += stats.get("rush_yds", 0) / RUSH_YDS_PER_PT
    fp += stats.get("rush_td", 0) * PT_PER_RUSH_TD
    fp += stats.get("rec_yds", 0) / REC_YDS_PER_PT
    fp += stats.get("rec_td", 0) * PT_PER_REC_TD
    fp += stats.get("rec", 0) * PT_PER_REC
    fp += stats.get("fumbles", 0) * PT_PER_FUMBLE
    return round(fp, 1)


# ----------------------------------------------------------------------------
# Position → table preference
# ----------------------------------------------------------------------------

# WR and TE pages use ``receiving_and_rushing``; RB pages use
# ``rushing_and_receiving``. Either id can appear so we try both.
SKILL_TABLE_IDS = ("rushing_and_receiving", "receiving_and_rushing")


def _row_cells(tr) -> Dict[str, str]:
    return {
        td.get("data-stat"): td.get_text(strip=True)
        for td in tr.find_all(["th", "td"])
        if td.get("data-stat")
    }


def _table_rows(soup: BeautifulSoup, ids: tuple[str, ...]):
    """Yield ``(table_id, row_dicts)`` for the first matching table id."""
    for tid in ids:
        t = soup.find("table", id=tid)
        if t and t.find("tbody"):
            rows = []
            for tr in t.tbody.find_all("tr"):
                # Skip in-table sub-header rows (class="thead").
                cls = tr.get("class") or []
                if "thead" in cls:
                    continue
                d = _row_cells(tr)
                if not d:
                    continue
                # Career playoff totals get a year_id of "Career" or
                # contain a class hint; we ignore them — totals are
                # recomputed from the per-season rows below.
                year = d.get("year_id", "")
                if not year or year in {"Career", ""}:
                    continue
                # Some snapshots include "1 yr", "2 yrs" aggregate rows.
                if year.endswith("yr") or year.endswith("yrs"):
                    continue
                rows.append(d)
            return tid, rows
    return None, []


def _team_value(row: Dict[str, str]) -> str:
    # Newer PFR snapshots use ``team_name_abbr``; older ones use
    # ``team``.  Either way, strip the wrapping link text.
    return (row.get("team_name_abbr") or row.get("team") or "").strip()


def _normalize_qb_row(row: Dict[str, str]) -> Dict:
    """Build a single QB season row dict from the parsed ``passing`` table."""
    rec_part = {
        "pass_cmp": _to_int(row.get("pass_cmp")),
        "pass_att": _to_int(row.get("pass_att")),
        "pass_yds": _to_int(row.get("pass_yds")),
        "pass_td": _to_int(row.get("pass_td")),
        "pass_int": _to_int(row.get("pass_int")),
    }
    rec_part["cmp_pct"] = (
        round(rec_part["pass_cmp"] / rec_part["pass_att"] * 100, 1)
        if rec_part["pass_att"]
        else 0.0
    )
    return {
        "year": row.get("year_id", "").strip(),
        "age": _to_int(row.get("age")),
        "team": _team_value(row),
        "games": _to_int(row.get("g")) or _to_int(row.get("games")),
        **rec_part,
    }


def _normalize_skill_row(row: Dict[str, str]) -> Dict:
    """Build one season row from the rushing_and_receiving (or alt) table."""
    return {
        "year": row.get("year_id", "").strip(),
        "age": _to_int(row.get("age")),
        "team": _team_value(row),
        "games": _to_int(row.get("g")) or _to_int(row.get("games")),
        "rush_att": _to_int(row.get("rush_att")),
        "rush_yds": _to_int(row.get("rush_yds")),
        "rush_td": _to_int(row.get("rush_td")),
        "targets": _to_int(row.get("targets")),
        "rec": _to_int(row.get("rec")),
        "rec_yds": _to_int(row.get("rec_yds")),
        "rec_td": _to_int(row.get("rec_td")),
        "fumbles": _to_int(row.get("fumbles")),
    }


def _extract_rush_for_qb(soup: BeautifulSoup) -> Dict[str, Dict]:
    """Pull rushing yardage per season for a QB.

    QB scrambling production is on the same ``rushing_and_receiving`` /
    ``receiving_and_rushing`` table that the skill positions use. We
    key by ``year_id`` so we can stitch it into the passing rows.
    """
    out: Dict[str, Dict] = {}
    _, rows = _table_rows(soup, SKILL_TABLE_IDS)
    for r in rows:
        year = r.get("year_id", "").strip()
        if not year:
            continue
        out[year] = {
            "rush_att": _to_int(r.get("rush_att")),
            "rush_yds": _to_int(r.get("rush_yds")),
            "rush_td": _to_int(r.get("rush_td")),
            "fumbles": _to_int(r.get("fumbles")),
        }
    return out


# ----------------------------------------------------------------------------
# Public builder
# ----------------------------------------------------------------------------

def stats_cache_path(pfr_id: str) -> Path:
    return CACHE_DIR / f"{pfr_id}.json"


def _build_qb_career(soup: BeautifulSoup) -> Dict:
    pass_table = soup.find("table", id="passing")
    if pass_table is None or pass_table.find("tbody") is None:
        return {"rows": [], "totals": {}}
    rush_by_year = _extract_rush_for_qb(soup)
    rows: List[Dict] = []
    totals = {
        "games": 0, "pass_cmp": 0, "pass_att": 0, "pass_yds": 0,
        "pass_td": 0, "pass_int": 0, "rush_att": 0, "rush_yds": 0,
        "rush_td": 0, "fumbles": 0, "fp": 0.0,
    }
    for tr in pass_table.tbody.find_all("tr"):
        if "thead" in (tr.get("class") or []):
            continue
        d = _row_cells(tr)
        year = d.get("year_id", "").strip()
        if not year or year in {"Career", ""}:
            continue
        if year.endswith("yr") or year.endswith("yrs"):
            continue
        row = _normalize_qb_row(d)
        rush = rush_by_year.get(year, {})
        row.update(rush)
        # Stamp FP per season
        row["fp"] = _season_fp({
            "pass_yds": row["pass_yds"], "pass_td": row["pass_td"],
            "pass_int": row["pass_int"],
            "rush_yds": row.get("rush_yds", 0),
            "rush_td": row.get("rush_td", 0),
            "fumbles": row.get("fumbles", 0),
        })
        rows.append(row)
        totals["games"] += row["games"]
        totals["pass_cmp"] += row["pass_cmp"]
        totals["pass_att"] += row["pass_att"]
        totals["pass_yds"] += row["pass_yds"]
        totals["pass_td"] += row["pass_td"]
        totals["pass_int"] += row["pass_int"]
        totals["rush_att"] += row.get("rush_att", 0)
        totals["rush_yds"] += row.get("rush_yds", 0)
        totals["rush_td"] += row.get("rush_td", 0)
        totals["fumbles"] += row.get("fumbles", 0)
        totals["fp"] += row["fp"]
    totals["fp"] = round(totals["fp"], 1)
    totals["cmp_pct"] = (
        round(totals["pass_cmp"] / totals["pass_att"] * 100, 1)
        if totals["pass_att"] else 0.0
    )
    return {"rows": rows, "totals": totals}


def _build_skill_career(soup: BeautifulSoup) -> Dict:
    _tid, raw_rows = _table_rows(soup, SKILL_TABLE_IDS)
    if not raw_rows:
        return {"rows": [], "totals": {}}
    rows: List[Dict] = []
    totals = {
        "games": 0, "rush_att": 0, "rush_yds": 0, "rush_td": 0,
        "targets": 0, "rec": 0, "rec_yds": 0, "rec_td": 0,
        "fumbles": 0, "fp": 0.0,
    }
    for d in raw_rows:
        row = _normalize_skill_row(d)
        row["fp"] = _season_fp({
            "rush_yds": row["rush_yds"], "rush_td": row["rush_td"],
            "rec": row["rec"], "rec_yds": row["rec_yds"],
            "rec_td": row["rec_td"], "fumbles": row["fumbles"],
        })
        rows.append(row)
        for k in ("games", "rush_att", "rush_yds", "rush_td",
                  "targets", "rec", "rec_yds", "rec_td", "fumbles"):
            totals[k] += row[k]
        totals["fp"] += row["fp"]
    totals["fp"] = round(totals["fp"], 1)
    return {"rows": rows, "totals": totals}


def build_career_stats(pfr_id: str, position: str) -> Dict:
    """Build the career stats payload for ``pfr_id``.

    Cached at ``data/cache/pfr_career_stats/<pfr_id>.json``. The first
    call scrapes via Wayback (1 req per 4s default); subsequent calls
    read JSON from disk.
    """
    if not pfr_id:
        return {"position": position or "", "rows": [], "totals": {},
                "fp_format": "superflex_ppr", "pfr_id": None}

    cache = stats_cache_path(pfr_id)
    if cache.exists() and cache.stat().st_size > 0:
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
            if payload.get("rows"):
                return payload
        except json.JSONDecodeError:
            log.warning("Bad cache JSON at %s; rebuilding", cache)

    try:
        html = _pfr.fetch_player_bio_html(pfr_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("PFR bio fetch failed for %s: %s", pfr_id, exc)
        return {"position": position, "rows": [], "totals": {},
                "fp_format": "superflex_ppr", "pfr_id": pfr_id,
                "fetch_error": str(exc)}

    soup = BeautifulSoup(html, "lxml")
    pos = (position or "").upper()
    if pos == "QB":
        body = _build_qb_career(soup)
    elif pos in {"RB", "WR", "TE", "FB", "HB"}:
        body = _build_skill_career(soup)
    else:
        body = {"rows": [], "totals": {}}

    payload = {
        "pfr_id": pfr_id,
        "position": pos,
        "fp_format": "superflex_ppr",
        "rows": body["rows"],
        "totals": body["totals"],
    }
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


# ----------------------------------------------------------------------------
# HTML rendering
# ----------------------------------------------------------------------------

def _esc(s) -> str:
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


_QB_COLS = (
    ("year", "Year"), ("team", "Team"), ("games", "GP"),
    ("pass_cmp", "Cmp"), ("pass_att", "Att"), ("cmp_pct", "Cmp%"),
    ("pass_yds", "Pass Yds"), ("pass_td", "Pass TD"), ("pass_int", "INT"),
    ("rush_att", "Rush Att"), ("rush_yds", "Rush Yds"), ("rush_td", "Rush TD"),
    ("fp", "FP"),
)
_RB_COLS = (
    ("year", "Year"), ("team", "Team"), ("games", "GP"),
    ("rush_att", "Rush Att"), ("rush_yds", "Rush Yds"), ("rush_td", "Rush TD"),
    ("targets", "Tgt"), ("rec", "Rec"), ("rec_yds", "Rec Yds"),
    ("rec_td", "Rec TD"), ("fp", "FP"),
)
_WR_COLS = (
    ("year", "Year"), ("team", "Team"), ("games", "GP"),
    ("targets", "Tgt"), ("rec", "Rec"), ("rec_yds", "Rec Yds"),
    ("rec_td", "Rec TD"), ("rush_att", "Rush Att"), ("rush_yds", "Rush Yds"),
    ("fp", "FP"),
)

_COLS_BY_POS = {
    "QB": _QB_COLS,
    "RB": _RB_COLS,
    "WR": _WR_COLS,
    "TE": _WR_COLS,
}

# Right-aligned (numeric) keys for CSS styling.
_NUMERIC_KEYS = {
    "games", "pass_cmp", "pass_att", "cmp_pct", "pass_yds", "pass_td",
    "pass_int", "rush_att", "rush_yds", "rush_td", "targets", "rec",
    "rec_yds", "rec_td", "fp",
}


def _fmt_cell(key: str, val) -> str:
    if val in (None, ""):
        return "—"
    if key == "cmp_pct":
        return f"{val:.1f}"
    if key == "fp":
        try:
            return f"{float(val):.1f}"
        except (TypeError, ValueError):
            return "—"
    return _esc(val)


def career_stats_html(career: Dict) -> str:
    """Render the career stats payload as an HTML fragment.

    Returns the empty string if there are no rows — the caller hides
    the section heading in that case.
    """
    rows = career.get("rows") or []
    if not rows:
        return ""

    pos = (career.get("position") or "").upper()
    cols = _COLS_BY_POS.get(pos)
    if not cols:
        return ""

    totals = career.get("totals") or {}

    # Build header
    def _th(key: str, label: str) -> str:
        align = ' style="text-align:right"' if key in _NUMERIC_KEYS else ""
        return f"<th{align}>{_esc(label)}</th>"

    head_cells = "".join(_th(k, label) for k, label in cols)

    body_rows = []
    for row in rows:
        tds = []
        for key, _label in cols:
            val = row.get(key, "")
            cls = "score" if key in _NUMERIC_KEYS else "years"
            align = ' style="text-align:right"' if key in _NUMERIC_KEYS else ""
            tds.append(f'<td class="{cls}"{align}>{_fmt_cell(key, val)}</td>')
        body_rows.append("<tr>" + "".join(tds) + "</tr>")

    # Career totals row
    tds_total = []
    for key, _label in cols:
        if key == "year":
            tds_total.append("<td class='name'><strong>Career</strong></td>")
        elif key == "team":
            tds_total.append("<td class='years'>—</td>")
        else:
            val = totals.get(key, "")
            cls = "score" if key in _NUMERIC_KEYS else "years"
            align = ' style="text-align:right"' if key in _NUMERIC_KEYS else ""
            tds_total.append(
                f'<td class="{cls}"{align}><strong>{_fmt_cell(key, val)}</strong></td>'
            )
    body_rows.append(
        '<tr style="border-top:2px solid var(--accent)">'
        + "".join(tds_total)
        + "</tr>"
    )

    # Build a direct link back to the canonical PFR player page when we
    # have the id. PFR's URL pattern is
    # ``/players/<first-letter-of-id>/<pfr_id>.htm``. Adding this is the
    # right-thing under PFR's terms of use (attribution + link back) and
    # lets the reader jump from our derived fp-totals view to the
    # underlying raw splits in one click.
    pfr_id = career.get("pfr_id") or ""
    if pfr_id:
        first_letter = pfr_id[0].upper()
        pfr_url = f"https://www.pro-football-reference.com/players/{first_letter}/{pfr_id}.htm"
        source_link = (
            f'<a href="{_esc(pfr_url)}" rel="noopener" target="_blank">'
            f'Pro Football Reference</a>'
        )
    else:
        source_link = (
            '<a href="https://www.pro-football-reference.com/" '
            'rel="noopener" target="_blank">Pro Football Reference</a>'
        )

    return f"""
<h2>Career <span class="accent">Stats</span></h2>
<p class="lede">Season-by-season production from {source_link},
with fantasy points computed under Superflex PPR (1 PPR · 4 pt pass TD ·
6 pt rush/rec TD · −2 INT · −2 fumble). Career totals on the bottom
row.</p>

<table>
<thead><tr>{head_cells}</tr></thead>
<tbody>
{chr(10).join(body_rows)}
</tbody>
</table>
"""
