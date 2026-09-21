"""Per-player career stat panel, sourced from the nflverse corpus.

Drop-in replacement for :mod:`dynasty.sources.pfr_career_stats` that
reads the season stat lines the daily refresh **already downloads**
(``scripts/refresh_nflverse_corpus.py``) instead of scraping Pro
Football Reference per player.

Why this module exists
----------------------

PFR returns HTTP 403 to GitHub-hosted runners (datacenter IP ranges),
and ``web.archive.org`` refuses connections from the same runners
(``Errno 111``). Both scrape paths are dead in CI. Our User-Agent is
honest and self-identifying, so the 403 is a deliberate network-level
block and Sports Reference's terms do not permit scraping — this module
does **not** attempt to work around it. It sources the same information
from data nflverse publishes for exactly this purpose.

It is also drastically cheaper. The PFR builder issued one throttled
HTTP request per ranked player (commit ``b3b96b1``, v3.10, took the
daily job from ~1.5 min to ~200 min). This builder reads one gzipped
CSV once per process, indexes it in memory, and serves every player
from that index.

Public surface (mirrors the PFR module)
---------------------------------------

* :func:`build_career_stats(player_id, position)` →
  ``{"player_id": str, "position": str, "rows": list[dict],
  "totals": dict, "fp_format": "superflex_ppr"}``
* :func:`career_stats_html(career)` → HTML fragment ready to splice
  into the player profile

Player keying
-------------

Keyed directly on the engine's ``player_id``. No PFR id crosswalk is
involved at all:

* Modern players carry an nflverse ``gsis_id`` (``00-XXXXXXX``), which
  is the ``player_id`` column of ``player_stats_season.csv.gz``.
* Pre-1999 retired comps carry a synthetic ``pfr_<PfrId>`` id, which is
  the ``player_id`` column of ``player_stats_season_pre1999.csv.gz``.

Both files are read into the same index, so both eras resolve through
one lookup.

Fantasy points are computed in **Superflex PPR** using constants ported
verbatim from the PFR module (1 PPR, 4 pt passing TD, 6 pt rush/rec TD,
1 pt per 25 pass yds, 1 pt per 10 rush/rec yds, -2 INT, -2 fumble).
"""
from __future__ import annotations

import csv
import gzip
import logging
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
NFLVERSE_DIR = _REPO_ROOT / "data" / "nflverse"
STATS_PATH = NFLVERSE_DIR / "player_stats_season.csv.gz"
STATS_PRE1999_PATH = NFLVERSE_DIR / "player_stats_season_pre1999.csv.gz"

# ----------------------------------------------------------------------------
# Fantasy-point scoring (Superflex PPR)
#
# Ported verbatim from ``pfr_career_stats`` — same constants, same
# formula, same rounding. ``tests/test_nflverse_career_stats.py`` asserts
# the two implementations agree for identical inputs.
# ----------------------------------------------------------------------------

PASS_YDS_PER_PT = 25.0
RUSH_YDS_PER_PT = 10.0
REC_YDS_PER_PT = 10.0
PT_PER_PASS_TD = 4.0
PT_PER_RUSH_TD = 6.0
PT_PER_REC_TD = 6.0
PT_PER_REC = 1.0          # PPR
PT_PER_INT = -2.0
PT_PER_FUMBLE = -2.0      # applied to fumbles LOST (see _row_fumbles)


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
# CSV value coercion
# ----------------------------------------------------------------------------

def _to_int(s: Optional[str]) -> int:
    """Coerce a CSV cell to int. Blank / ``NA`` / junk → 0.

    nflverse writes floats into integral columns (``"13.0"``) and uses
    empty strings plus R-style ``NA`` for missing values.
    """
    if s is None:
        return 0
    s = str(s).strip()
    if not s or s.upper() in {"NA", "NAN", "NULL", "NONE"}:
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def _to_float(s: Optional[str]) -> float:
    if s is None:
        return 0.0
    s = str(s).strip()
    if not s or s.upper() in {"NA", "NAN", "NULL", "NONE"}:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _row_fumbles(raw: Dict[str, str]) -> int:
    """Fumbles charged against fantasy points for one season row.

    PFR exposed only a combined ``fumbles`` total, so the old module
    applied the -2 penalty to *all* fumbles and documented that as a
    deliberately conservative lower bound. nflverse splits fumbles by
    phase **and** reports how many were actually lost, which is what the
    -2 constant was always meant to price, so we sum the ``*_lost``
    columns. The scoring constant is unchanged.
    """
    return (
        _to_int(raw.get("sack_fumbles_lost"))
        + _to_int(raw.get("rushing_fumbles_lost"))
        + _to_int(raw.get("receiving_fumbles_lost"))
    )


# ----------------------------------------------------------------------------
# Corpus index
# ----------------------------------------------------------------------------

# Columns we actually consume. Anything else in the 52-column schema is
# dropped at index time to keep the in-memory footprint small.
_KEEP_COLUMNS = (
    "season", "season_type", "player_id", "player_display_name",
    "player_name", "position", "position_group", "games", "recent_team",
    "completions", "attempts", "passing_yards", "passing_tds",
    "interceptions", "sack_fumbles_lost", "carries", "rushing_yards",
    "rushing_tds", "rushing_fumbles_lost", "receptions", "targets",
    "receiving_yards", "receiving_tds", "receiving_fumbles_lost",
)


def _read_corpus_file(path: Path) -> Iterable[Dict[str, str]]:
    """Yield trimmed row dicts from one gzipped nflverse stats CSV.

    Missing or unreadable files yield nothing — a fresh clone that has
    not run ``refresh_nflverse_corpus`` degrades to an empty panel
    rather than an exception.
    """
    if not path.exists():
        log.info("nflverse career stats: %s not present; skipping", path)
        return
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            for row in csv.DictReader(fh):
                yield {k: row.get(k, "") for k in _KEEP_COLUMNS}
    except (OSError, gzip.BadGzipFile, csv.Error) as exc:
        log.warning("nflverse career stats: unreadable corpus %s: %s",
                    path, exc)
        return


@lru_cache(maxsize=4)
def load_season_index(
    paths: Tuple[str, ...] = None,  # type: ignore[assignment]
) -> Dict[str, List[Dict[str, str]]]:
    """Return ``{player_id: [season rows, oldest first]}``.

    Reads every configured corpus file once per process and caches the
    result. ``paths`` is a tuple of strings (not ``Path``) so the
    ``lru_cache`` key stays hashable; pass an explicit tuple in tests to
    index a fixture instead of the real corpus.
    """
    if paths is None:
        paths = (str(STATS_PATH), str(STATS_PRE1999_PATH))

    index: Dict[str, List[Dict[str, str]]] = {}
    for p in paths:
        for raw in _read_corpus_file(Path(p)):
            pid = (raw.get("player_id") or "").strip()
            if not pid or pid == "0":
                # nflverse ships a sentinel row with player_id "0" and
                # no name; it is not a player.
                continue
            stype = (raw.get("season_type") or "").strip().upper()
            if stype and stype != "REG":
                # Career panel is regular season, matching the PFR
                # tables the old builder parsed.
                continue
            if not _to_int(raw.get("season")):
                continue
            index.setdefault(pid, []).append(raw)

    for rows in index.values():
        rows.sort(key=lambda r: _to_int(r.get("season")))
    return index


def clear_cache() -> None:
    """Drop the memoised corpus index (used by tests)."""
    load_season_index.cache_clear()


# ----------------------------------------------------------------------------
# Row normalisation
#
# Column mapping, PFR ``data-stat`` → nflverse column:
#
#   year_id    → season                games      → games
#   team       → recent_team           pass_cmp   → completions
#   pass_att   → attempts              pass_yds   → passing_yards
#   pass_td    → passing_tds           pass_int   → interceptions
#   rush_att   → carries               rush_yds   → rushing_yards
#   rush_td    → rushing_tds           targets    → targets
#   rec        → receptions            rec_yds    → receiving_yards
#   rec_td     → receiving_tds
#   fumbles    → sack_fumbles_lost + rushing_fumbles_lost
#                + receiving_fumbles_lost
#   cmp_pct    → derived (completions / attempts), as PFR did
#   age        → NOT AVAILABLE in the season corpus. The PFR builder
#                parsed it into the row dict but never rendered it
#                (it is absent from every _*_COLS tuple), so dropping
#                it changes no output.
# ----------------------------------------------------------------------------

def _common_fields(raw: Dict[str, str]) -> Dict:
    return {
        "year": str(_to_int(raw.get("season"))),
        "team": (raw.get("recent_team") or "").strip(),
        "games": _to_int(raw.get("games")),
    }


def _normalize_qb_row(raw: Dict[str, str]) -> Dict:
    row = _common_fields(raw)
    row.update({
        "pass_cmp": _to_int(raw.get("completions")),
        "pass_att": _to_int(raw.get("attempts")),
        "pass_yds": _to_int(raw.get("passing_yards")),
        "pass_td": _to_int(raw.get("passing_tds")),
        "pass_int": _to_int(raw.get("interceptions")),
        "rush_att": _to_int(raw.get("carries")),
        "rush_yds": _to_int(raw.get("rushing_yards")),
        "rush_td": _to_int(raw.get("rushing_tds")),
        "fumbles": _row_fumbles(raw),
    })
    row["cmp_pct"] = (
        round(row["pass_cmp"] / row["pass_att"] * 100, 1)
        if row["pass_att"] else 0.0
    )
    row["fp"] = _season_fp({
        "pass_yds": row["pass_yds"], "pass_td": row["pass_td"],
        "pass_int": row["pass_int"],
        "rush_yds": row["rush_yds"], "rush_td": row["rush_td"],
        "fumbles": row["fumbles"],
    })
    return row


def _normalize_skill_row(raw: Dict[str, str]) -> Dict:
    row = _common_fields(raw)
    row.update({
        "rush_att": _to_int(raw.get("carries")),
        "rush_yds": _to_int(raw.get("rushing_yards")),
        "rush_td": _to_int(raw.get("rushing_tds")),
        "targets": _to_int(raw.get("targets")),
        "rec": _to_int(raw.get("receptions")),
        "rec_yds": _to_int(raw.get("receiving_yards")),
        "rec_td": _to_int(raw.get("receiving_tds")),
        "fumbles": _row_fumbles(raw),
    })
    row["fp"] = _season_fp({
        "rush_yds": row["rush_yds"], "rush_td": row["rush_td"],
        "rec": row["rec"], "rec_yds": row["rec_yds"],
        "rec_td": row["rec_td"], "fumbles": row["fumbles"],
    })
    return row


_QB_TOTAL_KEYS = (
    "games", "pass_cmp", "pass_att", "pass_yds", "pass_td", "pass_int",
    "rush_att", "rush_yds", "rush_td", "fumbles",
)
_SKILL_TOTAL_KEYS = (
    "games", "rush_att", "rush_yds", "rush_td", "targets", "rec",
    "rec_yds", "rec_td", "fumbles",
)


def _accumulate(rows: List[Dict], keys: Tuple[str, ...]) -> Dict:
    totals: Dict[str, float] = {k: 0 for k in keys}
    totals["fp"] = 0.0
    for row in rows:
        for k in keys:
            totals[k] += row.get(k, 0)
        totals["fp"] += row.get("fp", 0.0)
    totals["fp"] = round(totals["fp"], 1)
    return totals


def _build_qb_career(raws: List[Dict[str, str]]) -> Dict:
    rows = [_normalize_qb_row(r) for r in raws]
    if not rows:
        return {"rows": [], "totals": {}}
    totals = _accumulate(rows, _QB_TOTAL_KEYS)
    totals["cmp_pct"] = (
        round(totals["pass_cmp"] / totals["pass_att"] * 100, 1)
        if totals["pass_att"] else 0.0
    )
    return {"rows": rows, "totals": totals}


def _build_skill_career(raws: List[Dict[str, str]]) -> Dict:
    rows = [_normalize_skill_row(r) for r in raws]
    if not rows:
        return {"rows": [], "totals": {}}
    return {"rows": rows, "totals": _accumulate(rows, _SKILL_TOTAL_KEYS)}


# ----------------------------------------------------------------------------
# Public builder
# ----------------------------------------------------------------------------

_SKILL_POSITIONS = {"RB", "WR", "TE", "FB", "HB"}


def _empty(player_id: Optional[str], position: str) -> Dict:
    return {
        "player_id": player_id or None,
        "position": (position or "").upper(),
        "fp_format": "superflex_ppr",
        "rows": [],
        "totals": {},
    }


def build_career_stats(
    player_id: str,
    position: str,
    *,
    paths: Tuple[str, ...] = None,  # type: ignore[assignment]
) -> Dict:
    """Build the career stats payload for ``player_id``.

    ``player_id`` is the engine's own id — an nflverse ``gsis_id`` for
    modern players, or the synthetic ``pfr_<PfrId>`` used by pre-1999
    retired comps. Both resolve against the same index.

    Never raises for ordinary missing data: an unknown id, an
    unsupported position, or an absent corpus file all return a payload
    with empty ``rows``, which :func:`career_stats_html` renders as the
    empty string.
    """
    if not player_id:
        return _empty(player_id, position)

    pos = (position or "").upper()

    try:
        index = load_season_index(paths)
    except Exception as exc:  # noqa: BLE001 - never break the build
        log.warning("nflverse career stats: index load failed: %s", exc)
        return _empty(player_id, pos)

    raws = index.get(player_id) or []
    if not raws:
        return _empty(player_id, pos)

    # Fall back to the position recorded in the corpus when the engine
    # row carries none.
    if not pos:
        pos = (raws[-1].get("position")
               or raws[-1].get("position_group") or "").strip().upper()

    if pos == "QB":
        body = _build_qb_career(raws)
    elif pos in _SKILL_POSITIONS:
        body = _build_skill_career(raws)
    else:
        body = {"rows": [], "totals": {}}

    return {
        "player_id": player_id,
        "position": pos,
        "fp_format": "superflex_ppr",
        "rows": body["rows"],
        "totals": body["totals"],
    }


# ----------------------------------------------------------------------------
# HTML rendering
#
# Column tuples, cell classes, alignment and the totals row are copied
# from ``pfr_career_stats`` so the rendered panel is visually identical
# apart from the attribution line.
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
    rows = (career or {}).get("rows") or []
    if not rows:
        return ""

    pos = (career.get("position") or "").upper()
    cols = _COLS_BY_POS.get(pos)
    if not cols:
        return ""

    totals = career.get("totals") or {}

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

    return f"""
<h2>Career <span class="accent">Stats</span></h2>
<p class="lede">Season-by-season regular-season production from nflverse,
with fantasy points computed under Superflex PPR (1 PPR · 4 pt pass TD ·
6 pt rush/rec TD · −2 INT · −2 fumble lost). Career totals on the bottom
row.</p>

<table>
<thead><tr>{head_cells}</tr></thead>
<tbody>
{chr(10).join(body_rows)}
</tbody>
</table>
"""
