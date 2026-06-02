"""Dynasty Football Model — v1.0 report builder.

Renders the static site from the v1 similarity engine. Mirrors the basketball
model's UI architecture: shared CSS, site header, rankings / league / methodology
/ sources / prospects pages, per-player pages with career-arc comparables.

The v0.x ``generate_site`` API is kept as a thin wrapper that ignores the old
``additional_formats`` parameter; the new site renders every PRESET overlay
into ``league.html`` via client-side JS, no per-format file fanout needed.
"""
from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .engine.similarity_v1 import EngineResult, OUT_ROOT, run_engine
from .engine.format_overlay import PRESETS, OverlayResult, all_format_overlays
from .consensus import (
    ConsensusComparison,
    compare_to_consensus,
    load_crosswalk,
)
from .sources.keeptradecut import load_latest as load_latest_ktc
from .sources import pfr_career_stats as _pfr_career


# ---------------------------------------------------------------------------
# Position colour palette (mirrors basketball model)
# ---------------------------------------------------------------------------

POSITION_COLOR = {
    "QB": "#e74c3c",
    "RB": "#27ae60",
    "WR": "#3498db",
    "TE": "#f39c12",
}


def _esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def _slug(name: str, pid: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{s}-{pid.replace('-', '')[-6:]}"


def _pos_badge(pos: str) -> str:
    color = POSITION_COLOR.get(pos, "#9ca3af")
    return f'<span class="pos-badge" style="background:{color}">{_esc(pos)}</span>'


# ---------------------------------------------------------------------------
# Shared CSS (mirrors basketball model exactly, with football accent colour)
# ---------------------------------------------------------------------------

def _shared_css() -> str:
    return """
:root {
  --bg: #ffffff; --card: #ffffff; --border: #e5e7eb; --text: #0f172a;
  --muted: #64748b; --accent: #1d4ed8; --accent-dark: #1e3a8a;
  --hover: #eff6ff; --header-bg: #0f172a; --header-text: #f8fafc;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.55;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
header.site {
  background: var(--header-bg);
  color: var(--header-text); padding: 20px 36px;
  border-bottom: 3px solid var(--accent);
}
header.site .row { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 16px; }
header.site h1 { margin: 0; font-size: 20px; font-weight: 700; letter-spacing: -0.01em; }
header.site h1 a { color: var(--header-text); }
header.site h1 .accent { color: var(--accent); }
header.site nav a {
  color: var(--header-text); opacity: 0.75; margin-left: 22px; font-size: 14px; font-weight: 500;
}
header.site nav a:hover { opacity: 1; text-decoration: none; }
header.site nav a.active { opacity: 1; border-bottom: 2px solid var(--accent); padding-bottom: 4px; }
header.site .meta { opacity: 0.6; font-size: 12px; margin-top: 4px; }
.container { max-width: 1240px; margin: 0 auto; padding: 28px 36px; }
.container.narrow { max-width: 900px; }
h2 { color: var(--text); font-size: 22px; margin-top: 32px; font-weight: 600; }
h2 .accent { color: var(--accent); }
h3 { color: var(--text); font-size: 16px; margin-top: 22px; font-weight: 600; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 20px 24px; margin-bottom: 18px; }
.lede { font-size: 15px; color: var(--muted); margin: 8px 0 18px 0; max-width: 720px; }
.kpi-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin-bottom: 22px; }
.kpi { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 14px 18px; }
.kpi .num { font-size: 24px; font-weight: 700; color: var(--accent); font-variant-numeric: tabular-nums; }
.kpi .label { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
table { width: 100%; background: var(--card); border-collapse: collapse;
  border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
th { background: #f8fafc; padding: 11px 14px; text-align: left;
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--muted); border-bottom: 1px solid var(--border); font-weight: 700; }
td { padding: 10px 14px; border-bottom: 1px solid var(--border); font-size: 14px; vertical-align: middle; }
tr:last-child td { border-bottom: none; }
tr.player-row:hover { background: var(--hover); cursor: pointer; }
td.rank { font-weight: 700; color: var(--accent); width: 50px; }
td.name { font-weight: 600; }
td.score { font-weight: 700; text-align: right; font-variant-numeric: tabular-nums; color: var(--accent); }
td.years, td.team, td.tier, td.consensus { color: var(--muted); font-variant-numeric: tabular-nums; }
.pos-badge { display: inline-block; color: white; padding: 3px 8px; border-radius: 4px;
  font-size: 11px; font-weight: 700; min-width: 32px; text-align: center; }
.controls { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
  padding: 14px 18px; margin-bottom: 18px; display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }
.controls input, .controls select { font: inherit; padding: 7px 11px;
  border: 1px solid var(--border); border-radius: 6px; background: white; }
.controls input { flex: 1; min-width: 220px; }
.controls button { font: inherit; padding: 8px 16px; border: 0; border-radius: 6px;
  background: var(--accent); color: white; font-weight: 600; cursor: pointer; }
.controls button:hover { background: var(--accent-dark); }
.stats { color: var(--muted); font-size: 13px; margin-left: auto; }
.div-chip { display: inline-block; padding: 3px 9px; border-radius: 12px; font-size: 11px;
  font-weight: 600; font-variant-numeric: tabular-nums; }
.div-up { background: #ecfdf5; color: #047857; }
.div-up-big { background: #16a34a; color: white; }
.div-down { background: #fef2f2; color: #b91c1c; }
.div-down-big { background: #dc2626; color: white; }
.div-flat { background: #f3f4f6; color: #6b7280; }
.div-none { background: #f3f4f6; color: var(--muted); font-style: italic; }
.era-chip { display: inline-block; padding: 3px 9px; border-radius: 12px; font-size: 11px;
  font-weight: 600; font-variant-numeric: tabular-nums;
  background: #fef3c7; color: #92400e; margin-left: 4px; }
.callout { background: #eff6ff; border: 1px solid #93c5fd;
  border-left: 4px solid var(--accent); border-radius: 6px; padding: 14px 18px;
  color: #1e3a8a; margin: 16px 0; font-size: 14px; }
.callout strong { color: var(--accent-dark); }
.player-header { background: var(--header-bg); color: var(--header-text); padding: 28px 36px; border-bottom: 3px solid var(--accent); }
.player-header h1 { margin: 0; font-size: 28px; }
.player-header .sub { opacity: 0.75; font-size: 14px; margin-top: 4px; }
.player-header .metrics { display: flex; gap: 28px; margin-top: 18px; flex-wrap: wrap; }
.player-header .metric .num { font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums; color: var(--accent); }
.player-header .metric .label { opacity: 0.75; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
footer { color: var(--muted); font-size: 12px; padding: 32px 40px; text-align: center; border-top: 1px solid var(--border); margin-top: 40px; }
.tag { display: inline-block; padding: 2px 9px; border-radius: 10px;
  font-size: 11px; font-weight: 600; background: #eef2ff; color: #4338ca; }
.tag.tag-retired { background: #fef3c7; color: #92400e; }
.tag.tag-prospect { background: #fdf4ff; color: #86198f; }
.comp-tier-elite { color: #b45309; font-weight: 600; }
.comp-tier-above-avg { color: #047857; font-weight: 600; }
.comp-tier-starter { color: #1d4ed8; }
.style-badge { display: inline-block; padding: 2px 8px; border-radius: 10px;
  font-size: 11px; font-weight: 700; letter-spacing: 0.02em; margin-left: 4px;
  background: rgba(255,255,255,0.15); color: var(--header-text); }
.style-pocket { background: rgba(96, 165, 250, 0.25); }
.style-mobile { background: rgba(167, 139, 250, 0.30); }
.style-dual_threat { background: rgba(250, 204, 21, 0.35); color: #fde68a; }
.comp-tier-deep { color: var(--muted); }

/* v3.0 PR 6 — prospect page styling */
.prospect-status-row { display:flex; gap:10px; flex-wrap:wrap; margin: 4px 0 18px; }
.status-pill { display:inline-block; padding: 7px 14px; border-radius: 999px;
  font-size: 13px; font-weight: 600; line-height: 1.4; }
.status-pill-ok { background: #ecfdf5; color: #047857; border: 1px solid #6ee7b7; }
.status-pill-warn { background: #fffbeb; color: #92400e; border: 1px solid #fcd34d; }
.status-pill a { color: inherit; text-decoration: underline; }
.prospect-te-flag { display:inline-block; padding: 2px 9px; border-radius: 10px;
  font-size: 12px; font-weight: 700; background: #fef3c7; color: #92400e; margin-left: 6px; }
.prospect-te-mini { font-size: 12px; margin-left: 2px; }
.prospect-te-row td:first-child { box-shadow: inset 3px 0 0 #f59e0b; }
.callout-warn { background: #fffbeb; border-color: #fcd34d; border-left-color: #f59e0b; color: #92400e; }
.chip-row { display:inline-flex; gap:6px; align-items:center; flex-wrap:wrap; }
.chip { font: inherit; font-size: 12px; padding: 6px 12px; border-radius: 999px;
  background: white; border: 1px solid var(--border); color: var(--text);
  cursor: pointer; font-weight: 600; }
.chip:hover { background: var(--hover); }
.chip.active { background: var(--accent); color: white; border-color: var(--accent); }
.hit-chip { display:inline-block; padding: 3px 9px; border-radius: 10px;
  font-size: 11px; font-weight: 700; letter-spacing: 0.02em; text-transform: uppercase; }
.hit-elite { background: #ecfdf5; color: #047857; }
.hit-starter { background: #fef3c7; color: #92400e; }
.hit-bust { background: #fef2f2; color: #b91c1c; }
.hit-unknown { background: #f3f4f6; color: #6b7280; }
.comp-row.comp-hit-elite td:first-child { box-shadow: inset 3px 0 0 #10b981; }
.comp-row.comp-hit-starter td:first-child { box-shadow: inset 3px 0 0 #f59e0b; }
.comp-row.comp-hit-bust td:first-child { box-shadow: inset 3px 0 0 #dc2626; }
.comp-row.comp-hit-unknown td:first-child { box-shadow: inset 3px 0 0 #9ca3af; }
"""


def _site_header(active: str, latest_ts: Optional[datetime], league_label: str) -> str:
    ts = latest_ts.strftime("%B %d, %Y at %I:%M %p UTC") if latest_ts else "—"

    def link(href, label, key):
        cls = ' class="active"' if key == active else ""
        return f'<a href="{href}"{cls}>{label}</a>'

    return f"""<header class="site">
  <div class="row">
    <div>
      <h1><a href="rankings.html">Kings of <span class="accent">Dynasty</span></a></h1>
      <div class="meta">Fantasy Football · Updated {_esc(ts)} · Default format: {_esc(league_label)}</div>
    </div>
    <nav>
      {link("rankings.html", "Similarity Scores", "rankings")}
      {link("league.html", "Dynasty Rankings", "league")}
      {link("methodology.html", "Methodology", "methodology")}
      {link("sources.html", "Sources", "sources")}
      {link("prospects.html", "Prospects", "prospects")}
    </nav>
  </div>
</header>"""


def _footer() -> str:
    return (
        '<footer>'
        'Kings of Dynasty · Fantasy Football · open source on '
        '<a href="https://github.com/pstiehl/Dynasty-Football-Model">GitHub</a> · '
        'Stats: <a href="https://github.com/nflverse/nflverse-data">nflverse</a> + Pro-Football-Reference'
        '</footer>'
    )


def _page(title: str, header_html: str, body_html: str, css_href: str = "assets/style.css") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<link rel="stylesheet" href="{css_href}">
</head>
<body>
{header_html}
{body_html}
{_footer()}
</body>
</html>"""


# ---------------------------------------------------------------------------
# Rankings page
# ---------------------------------------------------------------------------

def _comp_tier_class(comp_tier: str) -> str:
    if not comp_tier:
        return "comp-tier-deep"
    for k in ("elite", "above-avg", "starter", "deep"):
        if comp_tier.startswith(k):
            return f"comp-tier-{k}"
    return "comp-tier-deep"


def _build_rankings(engine: EngineResult, latest_ts: datetime, league_label: str,
                    team_lookup: Dict[str, str], limit: int = 300) -> str:
    rows_html = ""
    for row in engine.rankings[:limit]:
        slug = _slug(row["name"], row["player_id"])
        comp_class = _comp_tier_class(row["comp_tier"])
        team = team_lookup.get(row["player_id"], "—")
        rows_html += f"""<tr class="player-row" data-name="{_esc(row['name'].lower())}" data-position="{_esc(row['position'])}" onclick="location='players/{slug}.html'">
<td class="rank">{row['overall_rank']}</td>
<td class="name">{_esc(row['name'])}</td>
<td>{_pos_badge(row['position'])}</td>
<td class="team">{_esc(team)}</td>
<td class="years">{row['age']}</td>
<td class="years">{row['projected_years_remaining']:.1f}</td>
<td class="tier">T{row['tier']}</td>
<td class="years"><span class="{comp_class}">{_esc(row['comp_tier'])}</span></td>
<td class="score">{row['production_score']:.0f}</td>
</tr>"""

    body = f"""<div class="container">

<h2>Similarity <span class="accent">Scores</span></h2>
<p class="lede">Players ranked by projected lifetime fantasy points,
comped to historical players with similar <strong>fantasy production curves</strong>
under modern scoring. Each active player's fp/g arc is matched against the
<strong>long-arc</strong> NFL corpus (retired ∪ 8+ season veterans). Comps' realised
post-snapshot fantasy points (era-pace adjusted to current era, scored under
{_esc(league_label)}) are similarity-weighted and time-discounted into a
projected remaining career.</p>

<div class="kpi-row">
  <div class="kpi"><div class="num">{len(engine.rankings):,}</div><div class="label">Active players ranked</div></div>
  <div class="kpi"><div class="num">{len(engine.long_arc_corpus):,}</div><div class="label">Long-arc comp pool</div></div>
  <div class="kpi"><div class="num">v2.0</div><div class="label">Engine · fantasy-point arc</div></div>
</div>

<div class="callout"><strong>v2.0.0 — fantasy-point-arc methodology.</strong>
v1.x ranked players by per-stat z-score shape: "who do their counting stats
look like?" That structurally buried Josh Allen and the modern dual-threat
elite because z-scoring is scale-invariant within era — it ignored that
rushing TDs score 6 pts each. v2.0 ranks players by the
<strong>fantasy points they actually produce</strong> under modern scoring, comping
them to historical players whose fp/g curves match. Elite-fp QBs cluster at
the top regardless of style. See <a href="methodology.html">Methodology</a>.</div>

<div class="controls">
  <input id="q" placeholder="Search by player name…" type="search">
  <select id="pos">
    <option value="">All positions</option>
    <option value="QB">QB</option><option value="RB">RB</option>
    <option value="WR">WR</option><option value="TE">TE</option>
  </select>
  <span class="stats" id="stats"></span>
</div>

<table>
<thead><tr>
  <th>#</th><th>Player</th><th>Pos</th><th>Team</th>
  <th>Age</th><th>Yrs Left</th><th>Tier</th>
  <th>Comp Tier</th>
  <th style="text-align:right">Value</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table>

<script>
const q = document.getElementById('q');
const pos = document.getElementById('pos');
const stats = document.getElementById('stats');
const rows = document.querySelectorAll('.player-row');
function update() {{
  const qv = (q.value || '').toLowerCase();
  const pv = pos.value || '';
  let shown = 0;
  rows.forEach(r => {{
    const ok = (!qv || r.dataset.name.includes(qv)) && (!pv || r.dataset.position === pv);
    r.style.display = ok ? '' : 'none';
    if (ok) shown++;
  }});
  stats.textContent = shown + ' / ' + rows.length + ' players';
}}
q.addEventListener('input', update); pos.addEventListener('change', update); update();
</script>

</div>"""

    return _page(
        "Kings of Dynasty — Similarity Scores",
        _site_header("rankings", latest_ts, league_label),
        body,
    )


# ---------------------------------------------------------------------------
# League overlay page
# ---------------------------------------------------------------------------

def _build_league(
    overlays: Dict[str, OverlayResult],
    latest_ts: datetime,
    league_label: str,
    team_lookup: Dict[str, str],
    *,
    engine: Optional[EngineResult] = None,
) -> str:
    """Render the **Dynasty Rankings** tab as a consensus-vs-model diff.

    The previous behavior (re-rank under a per-league overlay) was useful
    but the consensus comparison is the higher-value view per Phil's
    direction (2026-05-22): "show the prognostication that is happening
    in the dynasty community when the stats do not necessarily back it
    up."

    Surface:
      - Superflex: model vs KeepTradeCut ``superflexValues.rank``.
      - 1QB:       model vs KeepTradeCut ``oneQBValues.rank``.
      - Delta column: positive = model is more BEARISH than the crowd,
                      negative = model is more BULLISH than the crowd.

    Falls back to the legacy overlay (Superflex vs 2QB) when no KTC
    snapshot is cached locally, so the site still builds in offline /
    CI environments where ``scripts/refresh_ktc_consensus.py`` has not
    been run.
    """
    ktc_snap = load_latest_ktc()
    if ktc_snap is not None and engine is not None:
        return _build_league_consensus(
            engine=engine,
            ktc_snap=ktc_snap,
            latest_ts=latest_ts,
            league_label=league_label,
            team_lookup=team_lookup,
        )
    return _build_league_overlay_legacy(
        overlays=overlays,
        latest_ts=latest_ts,
        league_label=league_label,
        team_lookup=team_lookup,
    )


def _build_league_consensus(
    *,
    engine: EngineResult,
    ktc_snap,
    latest_ts: datetime,
    league_label: str,
    team_lookup: Dict[str, str],
) -> str:
    """Consensus-vs-model diff body for the Dynasty Rankings tab.

    v2.3.4 (Phil 2026-05-22):
      * Drop the 1QB PPR format toggle — Superflex only. "The point is
        to show that production scores are in some ways detached from
        the consensus," which works with one format clearly.
      * Ensure every row's player name links to /players/<slug>.html
        (the similarity-score page). Pre-fix the slug field was None
        because the engine rankings don't carry a slug; we now compute
        it locally from (name, player_id) matching ``_slug()``.
    """
    crosswalk = load_crosswalk()
    # Superflex PPR is the only format on the Dynasty Rankings tab.
    # KTC's 1QB consensus is still computed by ``compare_to_consensus``
    # for callers that want it (engine.overlays still ships it), but the
    # site UI no longer surfaces a toggle.
    formats = ("sf_ppr",)
    # Map player_id → slug from the source rankings so every consensus
    # row gets a valid /players/<slug>.html link. Phil's 2026-05-22
    # bug report: "when you click into a player in the dynasty rankings
    # tab this should link to the player's similarity score."
    slug_by_pid: Dict[str, str] = {
        r["player_id"]: _slug(r["name"], r["player_id"])
        for r in engine.rankings
    }
    payload: Dict[str, Dict] = {}
    for fmt in formats:
        cmp = compare_to_consensus(
            model_rankings=engine.rankings,
            ktc_snapshot=ktc_snap,
            crosswalk=crosswalk,
            league_format=fmt,
        )
        payload[fmt] = {
            "label": "Superflex PPR",
            "matched": len(cmp.rows),
            "unmatched": cmp.n_unmatched_consensus,
            "rows": [
                {
                    "name": r.name,
                    "pos": r.position,
                    "age": r.age,
                    "team": r.team or team_lookup.get(r.gsis_id, "—"),
                    "model_rank": r.model_rank,
                    "consensus_rank": r.consensus_rank,
                    "delta": r.delta,
                    "score": round(r.production_score, 1),
                    "ktc_value": r.consensus_value,
                    "tier": r.consensus_tier,
                    "pos_rank": r.consensus_positional_rank,
                    # Compute slug from the engine row by gsis_id so
                    # every row clicks through to its player page.
                    "slug": slug_by_pid.get(r.gsis_id) or r.slug,
                }
                for r in cmp.rows
            ],
        }
    payload_json = json.dumps(payload)
    consensus_ts = ktc_snap.captured_at.strftime("%b %d, %Y at %H:%M UTC")

    body = f"""<div class="container">

<h2>Dynasty <span class="accent">Rankings</span> · Consensus vs Model</h2>
<p class="lede">Where does the data agree with the dynasty community, and
where does it disagree? Each row pairs the model's similarity-score rank
with the <a href="https://keeptradecut.com/dynasty-rankings">KeepTradeCut</a>
community consensus for the same league format.</p>

<div class="callout">
  <strong>How to read the delta.</strong>
  <span class="div-chip div-up">↑ 5</span> /
  <span class="div-chip div-up-big">↑ 15</span> (green up-arrow) means the
  <em>model</em> ranks the player <em>higher</em> than the crowd does
  (model is more bullish on the data).
  <span class="div-chip div-down">↓ 5</span> /
  <span class="div-chip div-down-big">↓ 15</span> (red down-arrow) means
  the crowd ranks them higher than the data justifies. Big deltas
  surface the players the community is pricing on narrative rather
  than production.
</div>

<div class="controls">
  Format: <span class="stats"><strong>Superflex PPR</strong></span>
  &nbsp;· Sort:
  <button onclick="setSort('model')" id="sort-model">Model rank</button>
  <button onclick="setSort('consensus')" id="sort-consensus">Consensus rank</button>
  <button onclick="setSort('bullish')" id="sort-bullish">Model bullish</button>
  <button onclick="setSort('bearish')" id="sort-bearish">Model bearish</button>
  <span class="stats" id="ov-stats"></span>
</div>

<table>
<thead><tr>
  <th>Model #</th><th>Player</th><th>Pos</th><th>Team</th>
  <th>Age</th>
  <th style="text-align:right">Consensus #</th>
  <th style="text-align:right">Δ</th>
  <th style="text-align:right">Score</th>
  <th style="text-align:right">KTC value</th>
</tr></thead>
<tbody id="ov-body"></tbody>
</table>

<p class="footnote">Consensus snapshot: KeepTradeCut, captured {_esc(consensus_ts)}.
Refresh with <code>python3 scripts/refresh_ktc_consensus.py</code>.
Matching uses dynastyprocess <code>ktc_id→gsis_id</code> crosswalk; rows
that cannot be resolved to a model player are excluded.</p>

<script>
const CONSENSUS = {payload_json};
// v2.3.4: Superflex PPR is the only format on the Dynasty Rankings tab
// per Phil 2026-05-22 ("On Dynasty Rankings tab it should only be
// Superflex PPR. Let's get rid of the 1QB PPR format button."). The
// payload still uses a dict keyed by format string for back-compat with
// the legacy overlay fallback path.
const currentFmt = 'sf_ppr';
let currentSort = 'model';
// Consensus-page delta semantics (per Phil 2026-05-22):
// Model ranking a player HIGHER than the crowd is the bullish
// data-disagrees-with-narrative signal → green up-arrow.
// Crowd ranks player higher than the data justifies → red down-arrow.
// Delta = model_rank - consensus_rank, so a NEGATIVE delta means the
// model has the smaller rank number = ranks the player higher.
function chip(d) {{
  if (d <= -10) return '<span class="div-chip div-up-big">↑ '+(-d)+'</span>';
  if (d < 0)   return '<span class="div-chip div-up">↑ '+(-d)+'</span>';
  if (d >= 10) return '<span class="div-chip div-down-big">↓ '+d+'</span>';
  if (d > 0)   return '<span class="div-chip div-down">↓ '+d+'</span>';
  return '<span class="div-chip div-flat">0</span>';
}}
function posBadge(p) {{
  const colors = {{ QB: '#e74c3c', RB: '#27ae60', WR: '#3498db', TE: '#f39c12' }};
  const c = colors[p] || '#9ca3af';
  return '<span class="pos-badge" style="background:'+c+'">'+p+'</span>';
}}
function sortedRows(fmt, sort) {{
  const rows = CONSENSUS[fmt].rows.slice();
  if (sort === 'consensus') rows.sort((a, b) => a.consensus_rank - b.consensus_rank);
  else if (sort === 'bullish') rows.sort((a, b) => a.delta - b.delta);
  else if (sort === 'bearish') rows.sort((a, b) => b.delta - a.delta);
  else rows.sort((a, b) => a.model_rank - b.model_rank);
  return rows;
}}
function render() {{
  const data = CONSENSUS[currentFmt];
  const rows = sortedRows(currentFmt, currentSort);
  const body = document.getElementById('ov-body');
  body.innerHTML = rows.map(r => {{
    const slugCell = r.slug
      ? '<a href="players/'+r.slug+'.html">'+r.name+'</a>'
      : r.name;
    return '<tr class="player-row"><td class="rank">'+r.model_rank+'</td>'+
      '<td class="name">'+slugCell+'</td>'+
      '<td>'+posBadge(r.pos)+'</td>'+
      '<td class="team">'+(r.team||'—')+'</td>'+
      '<td class="years">'+(r.age==null?'—':r.age)+'</td>'+
      '<td class="years" style="text-align:right">'+r.consensus_rank+'</td>'+
      '<td class="years" style="text-align:right">'+chip(r.delta)+'</td>'+
      '<td class="score" style="text-align:right">'+r.score.toFixed(0)+'</td>'+
      '<td class="score" style="text-align:right">'+(r.ktc_value==null?'—':r.ktc_value)+'</td>'+
      '</tr>';
  }}).join('');
  document.getElementById('ov-stats').textContent =
    data.label + ' · ' + data.matched + ' players matched';
  ['model','consensus','bullish','bearish'].forEach(k => {{
    const b = document.getElementById('sort-'+k);
    if (b) b.style.opacity = (k === currentSort) ? '1' : '0.55';
  }});
}}
function setSort(s) {{ currentSort = s; render(); }}
render();
</script>

</div>"""

    return _page(
        "Kings of Dynasty — Dynasty Rankings",
        _site_header("league", latest_ts, league_label),
        body,
    )


def _build_league_overlay_legacy(
    *,
    overlays: Dict[str, OverlayResult],
    latest_ts: datetime,
    league_label: str,
    team_lookup: Dict[str, str],
) -> str:
    """Fallback Dynasty Rankings body (Superflex-vs-2QB overlay).

    Used when no KTC consensus snapshot is cached locally. Preserves the
    pre-v2.3 behaviour so the site still builds in offline / CI envs.
    """
    DYNASTY_RANKINGS_PRESETS = ("sf_ppr", "2qb_ppr")
    overlay_payload: Dict[str, Dict] = {}
    for fmt in DYNASTY_RANKINGS_PRESETS:
        ov = overlays.get(fmt)
        if ov is None:
            continue
        overlay_payload[fmt] = {
            "label": ov.label,
            "rankings": [
                {
                    "name": r["name"],
                    "pos": r["position"],
                    "age": r["age"],
                    "team": team_lookup.get(r["player_id"], "—"),
                    "value": r["league_value"],
                    "delta": r["vs_default_delta"],
                    "slug": _slug(r["name"], r["player_id"]),
                }
                for r in ov.rankings[:300]
            ],
        }
    payload_json = json.dumps(overlay_payload)
    preset_buttons = "".join(
        f'<button onclick="setFormat(\'{fmt}\')" id="btn-{fmt}">{_esc(PRESETS[fmt]["label"])}</button> '
        for fmt in DYNASTY_RANKINGS_PRESETS if fmt in PRESETS
    )
    body = f"""<div class="container">
<h2>Dynasty <span class="accent">Rankings</span></h2>
<p class="lede">No consensus snapshot is cached locally yet. Showing the
legacy format overlay (Superflex PPR vs 2QB PPR). Run
<code>python3 scripts/refresh_ktc_consensus.py</code> and rebuild to
enable the consensus-vs-model view.</p>
<div class="controls">Preset: {preset_buttons}<span class="stats" id="ov-stats"></span></div>
<table><thead><tr><th>#</th><th>Player</th><th>Pos</th><th>Team</th><th>Age</th>
<th style="text-align:right">vs default</th><th style="text-align:right">League value</th></tr></thead>
<tbody id="ov-body"></tbody></table>
<script>
const OVERLAY = {payload_json};
function chip(d) {{
  if (d > 10) return '<span class="div-chip div-up-big">+'+d+'</span>';
  if (d > 0)  return '<span class="div-chip div-up">+'+d+'</span>';
  if (d < -10) return '<span class="div-chip div-down-big">'+d+'</span>';
  if (d < 0)  return '<span class="div-chip div-down">'+d+'</span>';
  return '<span class="div-chip div-flat">0</span>';
}}
function posBadge(p) {{
  const colors = {{ QB: '#e74c3c', RB: '#27ae60', WR: '#3498db', TE: '#f39c12' }};
  const c = colors[p] || '#9ca3af';
  return '<span class="pos-badge" style="background:'+c+'">'+p+'</span>';
}}
function setFormat(fmt) {{
  const data = OVERLAY[fmt];
  const body = document.getElementById('ov-body');
  body.innerHTML = data.rankings.map((r, i) =>
    '<tr class="player-row" onclick="location=\'players/'+r.slug+'.html\'"><td class="rank">'+(i+1)+'</td>'+
    '<td class="name">'+r.name+'</td>'+
    '<td>'+posBadge(r.pos)+'</td>'+
    '<td class="team">'+r.team+'</td>'+
    '<td class="years">'+r.age+'</td>'+
    '<td class="years" style="text-align:right">'+chip(r.delta)+'</td>'+
    '<td class="score">'+r.value.toFixed(0)+'</td></tr>'
  ).join('');
  document.getElementById('ov-stats').textContent = data.label + ' · ' + data.rankings.length + ' players';
  Object.keys(OVERLAY).forEach(k => {{
    const b = document.getElementById('btn-'+k);
    if (b) b.style.opacity = (k === fmt) ? '1' : '0.55';
  }});
}}
setFormat('sf_ppr');
</script>
</div>"""
    return _page(
        "Kings of Dynasty — Dynasty Rankings",
        _site_header("league", latest_ts, league_label),
        body,
    )


# ---------------------------------------------------------------------------
# Methodology page
# ---------------------------------------------------------------------------

def _build_methodology(engine: EngineResult, latest_ts: datetime,
                       league_label: str) -> str:
    # Render era-pace multiplier table from corpus values.
    eras = (1, 2, 3, 4)
    pace = engine.era_pace
    rows = ""
    for pos in ("QB", "RB", "WR", "TE"):
        stats_for_pos = sorted(pace.multipliers.get(pos, {}).keys())
        for stat in stats_for_pos:
            cells = "".join(
                f"<td class='years'>{pace.get(pos, stat, e):.2f}×</td>"
                for e in eras
            )
            rows += f"<tr><td class='name'>{pos}</td><td>{_esc(stat)}</td>{cells}</tr>"

    body = f"""<div class="container narrow">

<h2>v2.2 <span class="accent">Methodology — Fantasy-Point Arc + Penalty Stack</span></h2>

<p class="lede">v1.x ranked players by per-stat z-score shape — "who do their
counting stats look like?" That structurally buried elite dual-threats like
Josh Allen because z-scoring is scale-invariant within era and ignored that a
rushing TD scores 6 points. v2.0 replaces the engine: players are matched by
their <strong>fantasy production curves</strong> under modern scoring, then
projected forward against historical players with similar curves.</p>

<h3>1 · Era-pace pre-adjustment of historical stats</h3>
<p>Before any scoring or similarity math, every historical season's raw
stat line is multiplied by an empirically-calibrated
position+stat+era_from→Era-4 ratio. A 2010 Peyton Manning passing-yards
total becomes "what would this season produce if it happened today". The
full table:</p>

<table style="margin-top:8px">
<thead><tr><th>Pos</th><th>Stat</th><th>Era 1→4</th><th>Era 2→4</th><th>Era 3→4</th><th>Era 4→4</th></tr></thead>
<tbody>{rows}</tbody>
</table>
<p class="lede" style="margin-top:8px">Source: <code>{_esc(engine.era_pace.source)}</code> ·
multipliers derived from the median per-game rate within each era × position × stat cell,
clamped to [0.6, 2.0].</p>

<h3>2 · Fantasy-point arc corpus</h3>
<p>Era-adjusted stats are run through {_esc(league_label)} scoring (plus
sf_ppr, 1qb_ppr, 2qb_ppr, half_ppr, std, sf_te_premium variants) to
produce a <code>fp_per_game</code> arc for every player-season. The result
is a per-player, per-format career arc in MODERN-fp-equivalent units —
completely free of stat-shape distortion.</p>

<h3>3 · The long-arc corpus</h3>
<p>The comp pool is restricted to <em>long-arc</em> players: retired through
2022 OR 8+ NFL seasons OR 33+ years old with 6+ seasons. Long-arc active
veterans (Rodgers, Stafford, Russell Wilson) contribute only their COMPLETED
seasons — the in-progress season never leaks.</p>

<h3>4 · Fantasy-arc similarity vector (10-dim, in fp units)</h3>
<ol>
  <li><code>v[0]</code> = fp/g at the current age (weight 1.0)</li>
  <li><code>v[1]</code> = fp/g at age-1 (weight 0.7)</li>
  <li><code>v[2]</code> = fp/g at age-2 (weight 0.5)</li>
  <li><code>v[3]</code> = career-avg fp/g through current age</li>
  <li><code>v[4]</code> = peak-3yr-avg fp/g through current age</li>
  <li><code>v[5]</code> = peak-single-season fp/g (any age through current)</li>
  <li><code>v[6]</code> = career-total fp through current age (scaled / 100)</li>
  <li><code>v[7]</code> = trajectory slope (fp/g per career-season)</li>
  <li><code>v[8]</code> = durability (games / possible_games)</li>
  <li><code>v[9]</code> = career-stage fp percentile within position</li>
</ol>
<p>Similarity is feature-importance-weighted inverse-distance (not cosine
because we want magnitude to matter): two players with similar fp/g
production trajectories under modern scoring are similar, regardless of
how they earned those points.</p>

<h3>5 · Projection pipeline</h3>
<ol>
  <li>For an active player at age <em>A</em>, find top-20 long-arc comps at
      same position, age ±1, career-stage ±1, ranked by
      feature-weighted similarity.</li>
  <li>For each comp, sum their realised post-age fantasy points under the
      target format (already in modern-fp units — era-pace was applied at
      corpus build).</li>
  <li>Time-discount 5%/year, similarity-weight, sum → <code>comp_weighted_fp</code>.</li>
  <li>Compute <code>peak_anchored_fp</code> = target's projection-rate ×
      17 games × expected remaining years × mid-life discount factor.
      Projection-rate = <em>max(recent_3yr × 1.10, peak_3yr × 0.90)</em> —
      blends current form with all-time ceiling so a single down year
      doesn't crash a proven star.</li>
  <li>Take <code>max(comp_weighted_fp, peak_anchored_fp)</code> when the
      target's peak-3yr clears the elite tier (QB ≥18, RB ≥15, WR ≥16,
      TE ≥12). Sub-elite players fall back to comp-weighted.</li>
  <li>For mobile / dual-threat QBs, multiply by 1.05–1.10 (modern
      medicine continues to extend mobile-QB careers; lift on projected
      years remaining matches v1.1 at up to 1.50× for display).</li>
</ol>

<h3>6 · v2.2 penalty stack — survival, confidence, late breakout</h3>
<p>v2.0/v2.1 projected forward by similarity-weighting comps' realised
post-snapshot fantasy points. That left three known overrates: small-sample
players got full credit for limited NFL data, players with bust-prone comp
pools got no penalty for the pool's collapse rate, and late-breakout QBs
were rewarded for "years remaining" the empirical record says they
rarely cash in.</p>
<p>v2.2 composes three multiplicative penalties on top of the v2.0/v2.1
raw projection:</p>
<ol>
  <li><strong>Survival multiplier.</strong> For each player's top-20
      comp pool, compute <code>bust_rate</code> (fraction of comps who
      retired by age 30 with &lt;8 NFL seasons) and
      <code>short_career_rate</code> (≤5 NFL seasons). Multiplier =
      <code>(1 - bust)×0.20 + (1 - short)×0.10 + 0.70</code>, floored
      at 0.65 and capped at 1.0. Clean comp pools (Allen, Mahomes,
      Hurts, Lamar) score 1.0; bust-heavy pools (Anthony Richardson)
      score 0.78–0.92.</li>
  <li><strong>Confidence shrinkage.</strong> Career NFL starts /32 caps
      at 1.0 (≈2 full seasons = full confidence). QBs under 16 career
      starts are additionally capped at 0.5. Above-baseline
      projections are pulled toward the position-tier median; below-
      baseline projections are straight-multiplied by confidence (no
      artificial lift). Sample-of-15 starts (Anthony Richardson) =
      confidence 0.47.</li>
  <li><strong>Late-breakout penalty (QB only).</strong> Multiplier keyed
      to the QB's first NFL season with ≥250 pass attempts or
      ≥10 games as primary starter:
      <ul>
        <li>breakout_age ≤ 22: 1.00 (no penalty)</li>
        <li>breakout_age = 23: 0.95</li>
        <li>breakout_age = 24: 0.88</li>
        <li>breakout_age ≥ 25: 0.80</li>
      </ul>
      Confidence-weighted: low-NFL-sample 2nd-year QBs (Daniels,
      24 starts) take a softer share than established late-breakouts
      (Bo Nix, 34+ starts). Empirical: see
      <a href="https://github.com/pstiehl/Dynasty-Football-Model/blob/main/docs/LATE-BREAKOUT-QBs.md">LATE-BREAKOUT-QBs.md</a>.</li>
</ol>
<p>Stack composition order:
<code>raw → ×survival → ×confidence + baseline×(1−conf) → ×late_breakout</code>.
Floored at 0.20×raw and capped at 1.00×raw — penalties are penalties,
not lifts. Per-player diagnostics (bust_rate, durable_career_rate,
breakout_age, confidence) are saved to <code>data/diagnostics/v2.2_*.json</code>.</p>

<h3>7 · Why this is dynasty-appropriate</h3>
<p>Dynasty value is the projected lifetime fantasy points a player will
score for your roster. v1.x's stat-shape matching answered a different
question ("what shape of NFL career does this player project to have?")
which correlated imperfectly with fantasy production. v2.0 measures the
thing we actually care about directly: <em>fantasy points produced under
modern scoring</em>.</p>

<h3>8 · Format overlay</h3>
<p>The base <a href="rankings.html">Similarity Scores</a> page uses
Superflex PPR. The <a href="league.html">Dynasty Rankings</a> page reads
per-format fp totals directly from the pre-computed arc corpus (no
re-scoring needed) and recomputes positional VORP baselines under the
target roster rules. v2.2 keeps two preset formats: Superflex PPR and
2QB PPR.</p>

<h3 id="prospects">9 · v3.0 Prospect Engine — PRs 3 + 4 + 5 + 6</h3>
<p>v3.0 adds a college→NFL similarity layer on top of the v2 NFL
production engine. Skill-position college prospects are compared against
a 2000+ college corpus using era-pace-adjusted, conference-tier-weighted
fp/g curves. Each prospect's top-25 college comps are then bridged to
NFL outcomes (where the comp eventually played) to project the
prospect's career fantasy points and peak3 fp/g.</p>
<ol>
  <li><strong>PR 3 — similarity engine.</strong> 10-dim career-stage
      feature vector keyed to age + career-stage length. Distance is
      feature-importance weighted; the comp pool is the v3.0 prospect
      corpus (≈5k college skill-position players, 2000-present).</li>
  <li><strong>PR 4 — projection layer.</strong> For each comp with an
      NFL career, the engine pulls career_fp and peak3_fp_pg, then
      similarity-weights them into a per-prospect projection. Comps
      without NFL data (washed out of CFB → never bridged) feed the
      survival multiplier instead.</li>
  <li><strong>PR 5 — back-test.</strong> Position-aware gates against
      the 2017–2021 holdout classes (now mostly NFL-resolved). Results
      below.</li>
  <li><strong>PR 6 — prospects UI ship.</strong> This page +
      per-prospect pages + the status banner. TE-experimental flag is
      surfaced on every TE row, every TE prospect page, and here.</li>
</ol>

<h3>v3.0 back-test results (position-aware gate)</h3>
<table style="max-width:780px;margin-top:8px">
<thead><tr><th>Metric</th><th style="text-align:right">Result</th><th style="text-align:right">Target</th><th>Status</th></tr></thead>
<tbody>
<tr><td class="name">Hit@10 (elite in top-50)</td><td class="score" style="text-align:right">36%</td><td class="years" style="text-align:right">≥ 22%</td><td><span class="hit-chip hit-elite">pass</span></td></tr>
<tr><td class="name">KTC head-to-head</td><td class="score" style="text-align:right">50%</td><td class="years" style="text-align:right">≥ 50%</td><td><span class="hit-chip hit-elite">pass</span></td></tr>
<tr><td class="name">Spearman ρ (overall)</td><td class="score" style="text-align:right">+0.203</td><td class="years" style="text-align:right">≥ 0.28</td><td><span class="hit-chip hit-bust">fail</span></td></tr>
<tr><td class="name">Spearman ρ (ex-TE)</td><td class="score" style="text-align:right">+0.274</td><td class="years" style="text-align:right">≥ 0.28</td><td><span class="hit-chip hit-starter">borderline</span></td></tr>
<tr><td class="name">Bust@10</td><td class="score" style="text-align:right">38%</td><td class="years" style="text-align:right">≥ 55%</td><td><span class="hit-chip hit-bust">fail (partly artifact)</span></td></tr>
</tbody>
</table>

<h3>Per-position Spearman ρ — the structural blind spot</h3>
<table style="max-width:520px;margin-top:8px">
<thead><tr><th>Position</th><th style="text-align:right">Spearman ρ</th><th>Read</th></tr></thead>
<tbody>
<tr><td class="name">QB</td><td class="score" style="text-align:right">+0.249</td><td><span class="hit-chip hit-starter">validated</span></td></tr>
<tr><td class="name">RB</td><td class="score" style="text-align:right">+0.282</td><td><span class="hit-chip hit-elite">validated</span></td></tr>
<tr><td class="name">WR</td><td class="score" style="text-align:right">+0.276</td><td><span class="hit-chip hit-elite">validated</span></td></tr>
<tr><td class="name">TE</td><td class="score" style="text-align:right">+0.086</td><td><span class="hit-chip hit-bust">experimental</span></td></tr>
</tbody>
</table>

<div class="callout callout-warn" style="margin-top:14px">
<strong>TE limitation — explicit disclosure.</strong> TE Spearman ρ of
<strong>0.086</strong> is materially below QB/RB/WR. Two structural
causes: (1) the college→NFL bridge corpus for TEs is thin (fewer
production-eligible TE seasons in the comp pool), and (2) TE fantasy
outcomes are heavily landing-spot-dependent in a way the engine doesn't
yet model. The page ships under a <em>QB/RB/WR validated, TE
experimental</em> label. <strong>v3.1 roadmap:</strong> TE-specific
feature weights (route tree, target share at age 21–22, blocking-vs-receiving
split) + landing-spot prior. Treat TE comps as directional, not
projections.</div>

<h3>Known limitations</h3>
<ul>
  <li><strong>Corpus floor: 1980.</strong> The v2.4 backfill added 1980-1998
      seasons from Pro-Football-Reference, so workhorse-era RBs (Walter
      Payton, Marcus Allen, Eric Dickerson, Earl Campbell), peak-era passers
      (Marino, Montana, Kelly, Young, Moon), and elite-era WRs (Rice,
      Largent, Reed, Carter) are now comp-eligible. Pre-1980 players
      (Jim Brown, OJ Simpson, Sayers) remain out of scope — 14-game seasons
      and undefined era-pace multipliers make the comparison unreliable.</li>
  <li>Pre-1999 comps carry a 0.9× confidence haircut because era-pace
      adjustment is principled but not perfect. They display with an
      <span class="era-chip">⏳ 1985</span> badge on player pages.</li>
  <li>Birth dates missing for some retired players — we fall back to
      <em>rookie_season + 22</em> as an age estimate (~2% of corpus).</li>
  <li>Sample-of-1 comp pools (e.g. Aaron Rodgers at 41 with only Tom Brady
      as a same-age comp) fall back to comp-weighted only — the
      peak-anchored projection requires ≥3 comps.</li>
</ul>

</div>"""

    return _page(
        "Kings of Dynasty — Methodology",
        _site_header("methodology", latest_ts, league_label),
        body,
    )


# ---------------------------------------------------------------------------
# Sources page (slim)
# ---------------------------------------------------------------------------

def _build_sources(latest_ts: datetime, league_label: str) -> str:
    body = """<div class="container narrow">

<h2>Data <span class="accent">Sources</span></h2>
<p class="lede">v1.0 runs on one primary data source. Auxiliary sources from
the v0.x composite are still synced for metadata but no longer feed the
ranking.</p>

<table>
<thead><tr><th>Source</th><th>Role</th><th>Where it's used</th></tr></thead>
<tbody>
<tr><td class="name">nflverse · player_stats_season</td>
    <td><span class="tag">primary</span></td>
    <td>Every per-season stat line for every NFL skill player back to 1999. The
    retired-only similarity corpus and the era-pace calibration are built
    entirely from this file.</td></tr>
<tr><td class="name">nflverse · players</td>
    <td><span class="tag">primary</span></td>
    <td>Player metadata: positions, birth dates, rookie/last seasons, draft
    info. Used to filter to skill positions and to compute age.</td></tr>
<tr><td class="name">Sleeper API</td>
    <td><span class="tag">metadata</span></td>
    <td>Current roster + team for active players. Powers the team column on
    the rankings page and the league-import flow on
    <a href="league.html">/league.html</a>.</td></tr>
<tr><td class="name">MyFantasyLeague API</td>
    <td><span class="tag">metadata</span></td>
    <td>League-import for MFL leagues. Same overlay engine, just different
    roster-fetch path.</td></tr>
<tr><td class="name">NFL Draft history</td>
    <td><span class="tag">metadata</span></td>
    <td>Draft round/pick for current players (shown on player pages). Not in
    the composite.</td></tr>
<tr><td class="name"><a href="https://keeptradecut.com/dynasty-rankings">KeepTradeCut</a></td>
    <td><span class="tag">consensus</span></td>
    <td>Community-driven dynasty rankings. Used only on the
    <a href="league.html">Dynasty Rankings</a> tab to diff the model
    against the crowd — explicitly NOT a model input. Refreshed daily;
    see <a href="https://github.com/pstiehl/Dynasty-Football-Model/blob/main/docs/CONSENSUS-VS-MODEL.md">CONSENSUS-VS-MODEL.md</a>.</td></tr>
<tr><td class="name">dynastyprocess <code>db_playerids.csv</code></td>
    <td><span class="tag">metadata</span></td>
    <td>Free player-id crosswalk maintained by <a href="https://github.com/dynastyprocess/data">dynastyprocess</a>.
    Provides the <code>ktc_id → gsis_id</code> mapping that joins the
    KeepTradeCut consensus snapshot to model players.</td></tr>
</tbody>
</table>

<p class="lede" style="margin-top:18px">v0.x sources (FantasyCalc,
DynastyProcess, FantasyPros, Brainy Ballers, FFC ADP, PFF, RAS, NFL Impact,
DynastyProcess, etc.) have been removed from the composite. The engine no
longer blends external opinions — it produces its own ranking from raw
production history. See <a href="methodology.html">Methodology</a>.</p>

</div>"""
    return _page(
        "Kings of Dynasty — Sources",
        _site_header("sources", latest_ts, league_label),
        body,
    )


# ---------------------------------------------------------------------------
# Prospects page (decoupled)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# v3.0 Prospects page (PR 6)
# ---------------------------------------------------------------------------

# Default location of the v3.0 projection-layer artifact, produced by
# scripts/build_prospects_v3.py (PR 4). If missing, the site falls back
# to a clearly-marked placeholder so CI builds never break.
_PROSPECTS_ALL_JSON = Path("data") / "engine_v3" / "prospects_all.json"

# Per-position Spearman ρ from the PR 5 back-test (position-aware gate).
# The structural blind spot is TE: 0.086 vs 0.27+ for QB/RB/WR. The UX
# disclosure on this page and the methodology section reflect this.
_PR5_BACKTEST = {
    "hit_at_10": 0.36,
    "bust_at_10": 0.38,
    "ktc_h2h": 0.50,
    "spearman_overall": 0.203,
    "spearman_ex_te": 0.274,
    "by_position": {
        "QB": 0.249,
        "RB": 0.282,
        "WR": 0.276,
        "TE": 0.086,
    },
}


def _prospect_slug(prospect: Dict) -> str:
    """Stable slug for a prospect page. Uses last-6 of cfb_player_id
    (or full id if shorter) so the URL is collision-resistant against
    veteran player pages even before the ``-prospect`` suffix."""
    name = prospect.get("name") or "prospect"
    pid = str(prospect.get("cfb_player_id") or prospect.get("slug") or "")
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    tail = re.sub(r"[^a-z0-9]+", "", pid.lower())[-6:] or "x"
    return f"{s}-{tail}"


def _load_prospects_artifact(prospects_path: Optional[Path] = None) -> Optional[Dict]:
    """Load the projection-layer artifact. Falls back across:
      1. Explicit ``prospects_path`` argument (test fixture or override)
      2. ``data/engine_v3/prospects_all.json``
      3. Looping ``data/engine_v3/prospects_<year>.json`` files
    Returns None when nothing is available (caller renders a placeholder).
    """
    candidates: List[Path] = []
    if prospects_path is not None:
        candidates.append(Path(prospects_path))
    candidates.append(_PROSPECTS_ALL_JSON)

    for path in candidates:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue

    # Try per-class fallback
    base = Path("data") / "engine_v3"
    if base.exists():
        merged_prospects: List[Dict] = []
        classes: List[int] = []
        for f in sorted(base.glob("prospects_*.json")):
            if f.name == "prospects_all.json":
                continue
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if d.get("draft_class"):
                classes.append(d["draft_class"])
            merged_prospects.extend(d.get("prospects", []))
        if merged_prospects:
            return {
                "version": "per-class-merge",
                "draft_classes": sorted(set(classes)),
                "n_prospects": len(merged_prospects),
                "prospects": merged_prospects,
            }
    return None


def _ktc_capture_label(artifact: Optional[Dict]) -> str:
    """Pull the KTC capture timestamp if the artifact carries one;
    otherwise return a neutral label."""
    if not artifact:
        return "—"
    meta = artifact.get("meta") or {}
    ts = (
        meta.get("ktc_captured_at")
        or meta.get("ktc_snapshot")
        or artifact.get("ktc_captured_at")
    )
    return _esc(ts) if ts else "latest available"


def _fmt_or_dash(value, suffix: str = "", fmt: str = "{:.1f}") -> str:
    if value is None:
        return "—"
    try:
        return fmt.format(float(value)) + suffix
    except Exception:
        return _esc(str(value))


def _delta_chip(delta: Optional[float]) -> str:
    """Color-coded KTC delta chip. Positive = model bullish vs KTC.
    Negative = model bearish. None = unranked."""
    if delta is None:
        return '<span class="div-chip div-none">—</span>'
    try:
        d = float(delta)
    except Exception:
        return '<span class="div-chip div-none">—</span>'
    if d >= 50:
        cls = "div-up-big"; arrow = "↑↑"
    elif d > 0:
        cls = "div-up"; arrow = "↑"
    elif d <= -50:
        cls = "div-down-big"; arrow = "↓↓"
    elif d < 0:
        cls = "div-down"; arrow = "↓"
    else:
        cls = "div-flat"; arrow = "—"
    return f'<span class="div-chip {cls}">{arrow} {int(round(d)):+d}</span>'


def _te_experimental_pill() -> str:
    return (
        '<span class="prospect-te-flag" '
        'title="TE engine is experimental. Back-test Spearman ρ = 0.086 '
        'for TE vs 0.27+ for QB/RB/WR. Limited bridge coverage + heavy '
        'landing-spot dependence. Treat TE comps as preview-grade.">'
        '⚠️ experimental</span>'
    )


def _build_prospects(latest_ts: datetime, league_label: str,
                    prospects_path: Optional[Path] = None) -> str:
    artifact = _load_prospects_artifact(prospects_path)

    if not artifact or not artifact.get("prospects"):
        # Graceful placeholder. CI must not crash when the engine cache is
        # stale or absent (e.g. fresh clone, network-step failure earlier).
        body = """<div class="container narrow">

<h2>Draft <span class="accent">Prospects</span></h2>
<p class="lede">v3.0 prospect engine cache not yet populated — refreshes
pending. The daily build will populate this page on the next successful
run of <code>scripts/build_prospects_v3.py</code>.</p>

<div class="callout"><strong>v3.0 status.</strong> The engine ships under
a “QB/RB/WR validated, TE experimental” label. Once the projection-layer
artifact regenerates, this page will display the ranked prospect board
with per-prospect comp pages.</div>

</div>"""
        return _page(
            "Kings of Dynasty — Prospects",
            _site_header("prospects", latest_ts, league_label),
            body,
        )

    prospects = list(artifact.get("prospects", []))
    classes = sorted({int(p.get("draft_class")) for p in prospects
                      if p.get("draft_class") is not None})

    # Sort by model overall rank, defaulting unranked to the bottom.
    prospects.sort(key=lambda p: p.get("model_overall_rank") or 10**9)

    # Default render limit — keep the page snappy. Skill positions only
    # are already enforced upstream (engine only emits QB/RB/WR/TE), so
    # we don't filter here.
    display_prospects = prospects[:150]

    # ---- Table rows ------------------------------------------------------
    rows_html = ""
    for p in display_prospects:
        pos = p.get("position", "")
        slug = _prospect_slug(p)
        ktc = p.get("ktc") or {}
        ktc_rank_sf = ktc.get("ktc_rank_sf")
        delta = p.get("ktc_delta_overall")
        is_te = pos == "TE"
        te_flag = ' <span title="TE engine is experimental — see status banner" class="prospect-te-mini">⚠️</span>' if is_te else ""
        # v3.3 — PFR-drafted badge (Phil 2026-05-28). If we matched
        # this prospect to an actual NFL draft pick, surface the round
        # / pick / team inline so the table answers "is this person
        # actually a 2026 NFL rookie?" at a glance.
        drafted = p.get("drafted") or {}
        if drafted:
            d_round = drafted.get("round")
            d_pick = drafted.get("pick")
            d_team = drafted.get("team") or "?"
            drafted_html = (
                f' <span class="draft-chip" title="Drafted {drafted.get("year")} '
                f'round {d_round} pick {d_pick}, {d_team}">🏈 {d_team} R{d_round} #{d_pick}</span>'
            )
        else:
            drafted_html = ""
        proj = p.get("projection") or {}
        career_fp = proj.get("projected_career_fp")
        peak3 = proj.get("projected_peak3_fp_pg")
        rank = p.get("model_overall_rank", "—")
        rows_html += (
            f'<tr class="player-row prospect-row{" prospect-te-row" if is_te else ""}" '
            f'data-name="{_esc((p.get("name") or "").lower())}" '
            f'data-position="{_esc(pos)}" '
            f'data-class="{_esc(p.get("draft_class") or "")}" '
            f'data-drafted="{1 if drafted else 0}" '
            f'onclick="location=\'players/{slug}-prospect.html\'">'
            f'<td class="rank">{_esc(rank)}{te_flag}</td>'
            f'<td class="name"><a href="players/{slug}-prospect.html">{_esc(p.get("name",""))}</a>{drafted_html}</td>'
            f'<td>{_pos_badge(pos)}</td>'
            f'<td class="years">{_esc(p.get("draft_class") or "—")}</td>'
            f'<td class="team">{_esc(p.get("school") or "—")}</td>'
            f'<td class="years">{_fmt_or_dash(p.get("age"), fmt="{:.1f}")}</td>'
            f'<td class="score">{_fmt_or_dash(career_fp, fmt="{:.0f}")}</td>'
            f'<td class="years" style="text-align:right">{_fmt_or_dash(peak3, fmt="{:.1f}")}</td>'
            f'<td class="years" style="text-align:right">{_esc(ktc_rank_sf) if ktc_rank_sf is not None else "—"}</td>'
            f'<td style="text-align:right">{_delta_chip(delta)}</td>'
            f'</tr>'
        )

    # ---- Filter chips ----------------------------------------------------
    class_chip_html = "".join(
        f'<button type="button" class="chip class-chip" data-class="{c}">{c}</button>'
        for c in classes
    )

    # ---- Body ------------------------------------------------------------
    n_prospects_total = len(prospects)
    ktc_label = _ktc_capture_label(artifact)

    body = f"""<div class="container">

<h2>Prospect <span class="accent">Rankings — v3.6</span></h2>
<p class="lede">Skill-position prospects who were <strong>actually drafted</strong>
in each class (per <a href="https://www.pro-football-reference.com/years/2026/draft.htm">Pro-Football-Reference</a>
for 2022–2026; <a href="https://www.tankathon.com/nfl/big_board">Tankathon</a>
big-board for the upcoming 2027 class). Each player is comped against
the v3.0 college→NFL similarity corpus (PR 3) and projected via the v3.0
projection layer, blended with a draft-pick-tier baseline (v3.4) so a
first-overall pick can't read 1.4 projected career fp just because his
college-fp comp pool happened to be undrafted small-school backups.
KTC delta surfaces where the model disagrees with the current consensus
dynasty board — positive (green) = model bullish, negative (red) = model
bearish. Default sort is by NFL draft pick within each class; click any
column to re-sort.</p>

<div class="prospect-status-row">
  <span class="status-pill status-pill-ok">✅ QB · RB · WR engine validated
    (back-test Hit@10 {_PR5_BACKTEST["hit_at_10"]:.0%}, ρ {_PR5_BACKTEST["spearman_ex_te"]:.2f} ex-TE)</span>
  <span class="status-pill status-pill-warn">⚠️ TE engine experimental —
    limited bridge coverage + landing-spot dependence. Treat TE comps as
    preview-grade. <a href="methodology.html#prospects">Roadmap</a>:
    TE-specific feature weights.</span>
</div>

<div class="kpi-row">
  <div class="kpi"><div class="num">{n_prospects_total:,}</div><div class="label">Prospects scored</div></div>
  <div class="kpi"><div class="num">{len(classes)}</div><div class="label">Draft classes</div></div>
  <div class="kpi"><div class="num">v3.0</div><div class="label">Engine · college→NFL similarity</div></div>
  <div class="kpi"><div class="num">{_PR5_BACKTEST["hit_at_10"]:.0%}</div><div class="label">Back-test Hit@10 (target 22%)</div></div>
</div>

<div class="callout"><strong>How to read this page.</strong> Click any row to
see that prospect's comp grid — the top-25 historical players whose
college production curve looks closest to the prospect's. Hit labels colour
each comp by NFL outcome (elite / starter / bust / unknown). The model's
rank reflects similarity-weighted projected career fp; KTC delta surfaces
where the model and the consensus board disagree.</div>

<div class="controls">
  <input id="q" placeholder="Search by prospect name…" type="search">
  <div class="chip-row" id="pos-chips">
    <button type="button" class="chip pos-chip active" data-pos="">All</button>
    <button type="button" class="chip pos-chip" data-pos="QB">QB</button>
    <button type="button" class="chip pos-chip" data-pos="RB">RB</button>
    <button type="button" class="chip pos-chip" data-pos="WR">WR</button>
    <button type="button" class="chip pos-chip" data-pos="TE">TE ⚠️</button>
  </div>
  <div class="chip-row" id="class-chips">
    <button type="button" class="chip class-chip active" data-class="">All classes</button>
    {class_chip_html}
  </div>
  <span class="stats" id="stats"></span>
</div>

<p class="lede" style="font-size:13px;margin-top:6px">KTC snapshot: {ktc_label} ·
Showing {len(display_prospects)} of {n_prospects_total:,} ranked prospects.
Click a column header to sort.</p>

<table id="prospect-table" data-sortable="true">
<thead><tr>
  <th data-sort="rank" data-sort-type="num">#</th>
  <th data-sort="name">Prospect</th>
  <th data-sort="pos">Pos</th>
  <th data-sort="class" data-sort-type="num">Class</th>
  <th data-sort="school">School</th>
  <th data-sort="age" data-sort-type="num">Age</th>
  <th data-sort="career_fp" data-sort-type="num" style="text-align:right">Proj career fp</th>
  <th data-sort="peak3" data-sort-type="num" style="text-align:right">Proj peak3 fp/g</th>
  <th data-sort="ktc" data-sort-type="num" style="text-align:right">KTC SF</th>
  <th data-sort="delta" data-sort-type="num" style="text-align:right">Δ vs KTC</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table>

<p class="lede" style="margin-top:18px">See the
<a href="methodology.html#prospects">v3.0 prospect engine methodology</a>
for the back-test table, position-aware gate results, and the documented
TE limitation + v3.1 roadmap.</p>

<script>
const q = document.getElementById('q');
const stats = document.getElementById('stats');
const rows = document.querySelectorAll('.prospect-row');
let activePos = '';
let activeClass = '';
function update() {{
  const qv = (q.value || '').toLowerCase();
  let shown = 0;
  rows.forEach(r => {{
    const okName = !qv || r.dataset.name.includes(qv);
    const okPos = !activePos || r.dataset.position === activePos;
    const okClass = !activeClass || r.dataset.class === activeClass;
    const ok = okName && okPos && okClass;
    r.style.display = ok ? '' : 'none';
    if (ok) shown++;
  }});
  stats.textContent = shown + ' / ' + rows.length + ' prospects';
}}
q.addEventListener('input', update);
document.querySelectorAll('.pos-chip').forEach(c => {{
  c.addEventListener('click', () => {{
    document.querySelectorAll('.pos-chip').forEach(x => x.classList.remove('active'));
    c.classList.add('active');
    activePos = c.dataset.pos || '';
    update();
  }});
}});
document.querySelectorAll('.class-chip').forEach(c => {{
  c.addEventListener('click', () => {{
    document.querySelectorAll('.class-chip').forEach(x => x.classList.remove('active'));
    c.classList.add('active');
    activeClass = c.dataset.class || '';
    update();
  }});
}});
// Click-to-sort on column headers.
const table = document.getElementById('prospect-table');
const tbody = table.querySelector('tbody');
const headers = table.querySelectorAll('th[data-sort]');
let lastSort = null;
let sortAsc = true;
function getCellValue(tr, idx, kind) {{
  const cell = tr.children[idx];
  if (!cell) return '';
  const txt = (cell.innerText || '').trim();
  if (kind === 'num') {{
    const n = parseFloat(txt.replace(/[^0-9.\\-]/g, ''));
    return isNaN(n) ? -Infinity : n;
  }}
  return txt.toLowerCase();
}}
headers.forEach((h, idx) => {{
  h.style.cursor = 'pointer';
  h.addEventListener('click', () => {{
    const kind = h.dataset.sortType === 'num' ? 'num' : 'str';
    if (lastSort === idx) sortAsc = !sortAsc; else {{ lastSort = idx; sortAsc = kind === 'str'; }}
    const sorted = Array.from(tbody.querySelectorAll('tr')).sort((a, b) => {{
      const av = getCellValue(a, idx, kind);
      const bv = getCellValue(b, idx, kind);
      if (av < bv) return sortAsc ? -1 : 1;
      if (av > bv) return sortAsc ? 1 : -1;
      return 0;
    }});
    sorted.forEach(r => tbody.appendChild(r));
  }});
}});
update();
</script>

</div>"""
    return _page(
        "Kings of Dynasty — Prospects",
        _site_header("prospects", latest_ts, league_label),
        body,
    )


def _comp_hit_class(hit_label: str) -> str:
    return {
        "elite": "comp-hit-elite",
        "starter": "comp-hit-starter",
        "bust": "comp-hit-bust",
    }.get(hit_label or "", "comp-hit-unknown")


def _build_prospect_page(prospect: Dict, label: str, latest_ts: datetime,
                         veteran_slugs: Optional[set] = None) -> str:
    """Per-prospect page: header + projection panel + top-25 comps with
    hit-label colouring + KTC delta + (TE only) experimental callout.
    Mirrors the veteran ``_build_player_page`` layout."""
    veteran_slugs = veteran_slugs or set()
    name = prospect.get("name", "—")
    pos = prospect.get("position", "—")
    school = prospect.get("school", "—")
    draft_class = prospect.get("draft_class", "—")
    age = prospect.get("age")
    proj = prospect.get("projection") or {}
    ktc = prospect.get("ktc") or {}
    production = prospect.get("production") or {}
    rank = prospect.get("model_overall_rank", "—")
    pos_rank = prospect.get("model_pos_rank", "—")
    is_te = (pos == "TE")
    te_callout = ""
    if is_te:
        te_callout = (
            '<div class="callout callout-warn" style="margin-top:14px">'
            '<strong>v3.0 note — TE projections are preview-grade.</strong> '
            "The engine's TE Spearman ρ is 0.086 vs 0.27+ for QB/RB/WR. "
            'TE-specific feature weights are on the v3.1 roadmap. Use this '
            'comp grid as a directional indicator, not a hard projection. See '
            '<a href="../methodology.html#prospects">methodology</a>.</div>'
        )

    header_html = f"""<div class="player-header">
  <h1>{_esc(name)}{(" " + _te_experimental_pill()) if is_te else ""}</h1>
  <div class="sub">{_pos_badge(pos)} · {_esc(school)} · Class of {_esc(draft_class)} · Model rank #{_esc(rank)} (pos #{_esc(pos_rank)})</div>
  <div class="metrics">
    <div class="metric"><div class="num">{_fmt_or_dash(proj.get("projected_career_fp"), fmt="{:.0f}")}</div><div class="label">Projected career fp</div></div>
    <div class="metric"><div class="num">{_fmt_or_dash(proj.get("projected_peak3_fp_pg"), fmt="{:.1f}")}</div><div class="label">Projected peak3 fp/g</div></div>
    <div class="metric"><div class="num">{_fmt_or_dash(proj.get("projected_years_in_league"), fmt="{:.1f}")}</div><div class="label">Proj yrs in league</div></div>
    <div class="metric"><div class="num">{_fmt_or_dash(age, fmt="{:.1f}")}</div><div class="label">Age</div></div>
    <div class="metric"><div class="num">{_esc(proj.get("n_comps_with_nfl") or "—")}</div><div class="label">Comps w/ NFL data</div></div>
  </div>
  {te_callout}
  <div style="margin-top:14px"><a href="../prospects.html" style="color:var(--header-text);opacity:0.8;font-size:13px">← back to Prospect Rankings</a></div>
</div>"""

    # ---- KTC delta callout ----------------------------------------------
    delta = prospect.get("ktc_delta_overall")
    ktc_rank_sf = ktc.get("ktc_rank_sf")
    ktc_pos_rank_sf = ktc.get("ktc_pos_rank_sf")
    ktc_callout = ""
    if ktc_rank_sf is not None or delta is not None:
        ktc_callout = (
            '<div class="callout" style="margin-top:14px">'
            f'<strong>KTC consensus.</strong> SF rank '
            f'<strong>{_esc(ktc_rank_sf) if ktc_rank_sf is not None else "unranked"}</strong>'
            f'{f" (pos #{_esc(ktc_pos_rank_sf)})" if ktc_pos_rank_sf is not None else ""} — '
            f'model delta {_delta_chip(delta)}. '
            f'Positive delta = model bullish vs consensus; negative = bearish.</div>'
        )
    elif delta is None and ktc_rank_sf is None:
        ktc_callout = (
            '<div class="callout" style="margin-top:14px">'
            '<strong>KTC consensus.</strong> Not currently ranked on KTC. '
            'No delta available.</div>'
        )

    # ---- v3.4 drafted info + projection-source callout ------------------
    drafted = prospect.get("drafted") or {}
    drafted_callout = ""
    if drafted and drafted.get("pick"):
        d_round = drafted.get("round") or "?"
        d_pick = drafted.get("pick")
        d_team = drafted.get("team") or "?"
        d_year = drafted.get("year") or draft_class
        source = drafted.get("source") or "pfr"
        if source == "tankathon_big_board":
            label_word = f"Tankathon 2027 big-board rank"
            value_word = f"#{d_pick}"
        else:
            label_word = f"NFL Draft {d_year}"
            value_word = f"Round {d_round}, Pick #{d_pick} — {d_team}"
        drafted_callout = (
            '<div class="callout" style="margin-top:14px">'
            f'<strong>🏈 {_esc(label_word)}:</strong> {_esc(value_word)}'
            '</div>'
        )
    proj_source = proj.get("projection_source")
    proj_callout = ""
    if proj_source:
        comp_only = proj.get("comp_only_career_fp")
        baseline = proj.get("pick_tier_baseline_fp")
        confidence = proj.get("projection_confidence") or 0.0
        n_meaningful = proj.get("n_meaningful_nfl_comps", 0) or 0
        n_with_nfl = proj.get("n_comps_with_nfl", 0) or 0
        floor_applied = bool(proj.get("floor_applied"))
        # v3.6: a low-meaningful-NFL-comp pool warning chip the user
        # can see at a glance. Phil 2026-05-28: "how is he such a
        # highly rated prospect [with no NFL comps]?" — we should
        # SIGNAL low confidence visually, not pretend otherwise.
        low_conf_warn = (
            '<span class="hit-chip hit-bust" style="margin-left:6px">low-confidence comp pool</span>'
            if n_meaningful < 3 else ""
        )
        if proj_source == "comp_weighted":
            proj_desc = (
                f"Projection is purely comp-weighted (≥{_esc(12)} meaningful comps with "
                f"NFL careers ≥200 fp)."
            )
        elif proj_source.startswith("pick_tier_baseline"):
            proj_desc = (
                f"Projection is the pick-tier baseline for this position. "
                f"No comps with meaningful NFL careers — the historical "
                f"college players most similar to this prospect either "
                f"didn't reach the NFL or only had cup-of-coffee stints "
                f"(<200 career fp). The pick-tier baseline anchors the "
                f"number to what players in this position+pick range "
                f"historically produce. {low_conf_warn}"
            )
        elif proj_source.startswith("floor_"):
            proj_desc = (
                f"Projection is the v3.6 baseline FLOOR (30% of the "
                f"pick-tier baseline). The comp-weighted projection "
                f"({comp_only:,.0f}) was suppressed because the comp pool "
                f"is dominated by college players who didn't reach the NFL. "
                f"The NFL spent a real pick on this player, so the floor "
                f"protects against an obviously-broken projection collapse. "
                f"{low_conf_warn}"
            )
        else:
            proj_desc = (
                f"Projection is a confidence-blend (“{_esc(proj_source)}”). "
                f"With <strong>{n_meaningful}</strong> meaningful comp NFL careers "
                f"(≥200 career fp), confidence is "
                f"<strong>{confidence:.2f}</strong>; we blend the comp-only "
                f"projection (<strong>{comp_only:,.0f}</strong>) with the "
                f"pick-tier baseline (<strong>{baseline:,.0f}</strong>). "
                f"{low_conf_warn}"
            )
        proj_callout = (
            '<div class="callout" style="margin-top:8px">'
            f'<strong>v3.6 projection methodology.</strong> {proj_desc}'
            '</div>'
        )

    # ---- Production panel (college) -------------------------------------
    prod_html = (
        f'<div class="controls" style="margin:14px 0 8px;flex-wrap:wrap;gap:14px">'
        f'<span class="stats">College adj career fp/g: <strong>{_fmt_or_dash(production.get("adj_career_fp_pg"), fmt="{:.2f}")}</strong></span>'
        f'<span class="stats">Final season fp/g: <strong>{_fmt_or_dash(production.get("final_season_fp_pg"), fmt="{:.2f}")}</strong></span>'
        f'<span class="stats">Peak season fp/g: <strong>{_fmt_or_dash(production.get("peak_season_fp_pg"), fmt="{:.2f}")}</strong></span>'
        f'<span class="stats">Conf tier (last): <strong>{_esc(prospect.get("conference_tier_last") or "—")}</strong></span>'
        f'</div>'
    )

    # ---- Comp table -----------------------------------------------------
    comps = prospect.get("comps") or []
    comp_rows = ""
    for c in comps[:25]:
        hit = c.get("hit_label") or "unknown"
        hit_cls = _comp_hit_class(hit)
        sim = c.get("similarity") or 0.0
        nfl = c.get("nfl_career") or {}
        snap_year = c.get("last_season")
        era_badge = ""
        if isinstance(snap_year, (int, float)) and snap_year < 1999:
            era_badge = (
                f' <span class="era-chip" title="Pre-1999 comp — corpus is 2000+ '
                f'for prospects so this rarely fires.">⏳ {int(snap_year)}</span>'
            )
        # Cross-link to veteran page when we have a GSIS id AND that
        # veteran page exists in the build.
        nfl_link_html = _esc(c.get("nfl_display_name") or c.get("name") or "—")
        gsis = c.get("nfl_gsis_id")
        if gsis:
            # Try to match against veteran slug. The veteran slug format is
            # ``re.sub-name + last-6 of pid``. We don't have the veteran's
            # display name here cheaply, so just check whether any veteran
            # slug ends with the last-6 of the GSIS id.
            tail = re.sub(r"[^a-z0-9]+", "", gsis.lower())[-6:]
            matches = [s for s in veteran_slugs if s.endswith(tail)]
            if matches:
                nfl_link_html = f'<a href="{_esc(matches[0])}.html">{nfl_link_html}</a>'

        comp_rows += (
            f'<tr class="comp-row {hit_cls}">'
            f'<td class="name">{_esc(c.get("name","—"))}{era_badge}</td>'
            f'<td class="team">{_esc(c.get("school","—"))}</td>'
            f'<td class="years">{_esc(c.get("class_year") or "—")}</td>'
            f'<td class="score" style="text-align:right">{float(sim):.3f}</td>'
            f'<td><span class="hit-chip hit-{_esc(hit)}">{_esc(hit)}</span></td>'
            f'<td class="name">{nfl_link_html}</td>'
            f'<td class="years" style="text-align:right">{_fmt_or_dash(nfl.get("career_fp"), fmt="{:.0f}")}</td>'
            f'<td class="years" style="text-align:right">{_fmt_or_dash(nfl.get("peak3_fp_pg"), fmt="{:.1f}")}</td>'
            f'<td class="years" style="text-align:right">{_esc(nfl.get("seasons_played") or "—")}</td>'
            f'</tr>'
        )

    body = f"""{header_html}
<div class="container">

{drafted_callout}
{ktc_callout}
{proj_callout}

<h2>Projection <span class="accent">Inputs</span></h2>
<p class="lede">College production summary feeding the v3.0 similarity
vector. The engine weights these features against the prospect corpus
(2000–present) to build the comp grid below.</p>
{prod_html}

<h2>Top-25 <span class="accent">Comparables</span></h2>
<p class="lede">Historical college players whose production curves most
closely match {_esc(name)}. Hit-label colouring tags each comp's eventual
NFL outcome: <span class="hit-chip hit-elite">elite</span>,
<span class="hit-chip hit-starter">starter</span>,
<span class="hit-chip hit-bust">bust</span>,
<span class="hit-chip hit-unknown">unknown</span> (still developing or
never reached the NFL). Click an NFL name to jump to its veteran page when
available.</p>

<table>
<thead><tr>
  <th>College player</th>
  <th>School</th>
  <th>Class</th>
  <th style="text-align:right">Similarity</th>
  <th>NFL outcome</th>
  <th>NFL career</th>
  <th style="text-align:right">Career fp</th>
  <th style="text-align:right">Peak3 fp/g</th>
  <th style="text-align:right">Seasons</th>
</tr></thead>
<tbody>{comp_rows}</tbody>
</table>

<p class="lede" style="margin-top:24px">Methodology details:
<a href="../methodology.html#prospects">v3.0 prospect engine — PR 3 similarity, PR 4 projection, PR 5 back-test</a>.</p>

</div>"""

    return _page(
        f"Kings of Dynasty — {name} (Prospect)",
        _site_header("prospects", latest_ts, label),
        body,
        css_href="../assets/style.css",
    )


# ---------------------------------------------------------------------------
# Player pages
# ---------------------------------------------------------------------------

def _player_header(row: Dict, team: str, league_label: str) -> str:
    # v2.0: surface QB style classification + fantasy-arc metrics.
    qb_style = row.get("qb_style")
    style_badge = ""
    if row.get("position") == "QB" and qb_style:
        style_label = {
            "pocket": "Pocket",
            "mobile": "Mobile",
            "dual_threat": "Dual-Threat",
        }.get(qb_style, qb_style.title())
        rypg = row.get("qb_career_rypg") or 0.0
        style_badge = (
            f" · <span class=\"style-badge style-{qb_style}\">"
            f"{style_label} ({rypg:.1f} ru/g)</span>"
        )
    lift_yr = row.get("career_length_lift") or 1.0
    lift_fp = row.get("career_length_lift_fp") or 1.0
    lift_panel = ""
    if row.get("position") == "QB" and (lift_yr > 1.0 or lift_fp > 1.0):
        era_note = (
            "modern medicine + RPO scheme adjustment"
            if qb_style == "dual_threat"
            else "mobile-QB longevity adjustment"
        )
        lift_panel = (
            f"<div class=\"callout\" style=\"margin-top:14px\">"
            f"<strong>{('Dual-threat' if qb_style=='dual_threat' else 'Mobile')} "
            f"career-length lift: {lift_fp:.2f}× fp / {lift_yr:.2f}× years</strong> "
            f"— {era_note}. v2.0 retains v1.1's correction for short-career "
            f"bias in the historical comp pool. Pocket lift = 1.00× (no lift)."
            f"</div>"
        )
    peak3 = row.get("peak_3yr_fp_per_game") or 0.0
    return f"""<div class="player-header">
  <h1>{_esc(row['name'])}</h1>
  <div class="sub">{_pos_badge(row['position'])} · {_esc(team)} · Rank #{row['overall_rank']} · Tier T{row['tier']}{style_badge}</div>
  <div class="metrics">
    <div class="metric"><div class="num">{row['production_score']:.0f}</div><div class="label">Projected lifetime fp</div></div>
    <div class="metric"><div class="num">{peak3:.1f}</div><div class="label">Peak 3yr fp/g</div></div>
    <div class="metric"><div class="num">{row['age']}</div><div class="label">Age</div></div>
    <div class="metric"><div class="num">{row['projected_years_remaining']:.1f}</div><div class="label">Yrs remaining</div></div>
    <div class="metric"><div class="num">{row['n_comps']}</div><div class="label">Long-arc comps</div></div>
  </div>
  {lift_panel}
  <div style="margin-top:14px"><a href="../rankings.html" style="color:var(--header-text);opacity:0.8;font-size:13px">← back to Similarity Scores</a></div>
</div>"""


def _build_player_page(row: Dict, comps: List[Dict], team: str,
                       league_label: str, latest_ts: datetime,
                       career_stats_html: str = "") -> str:
    # --- Comp table -------------------------------------------------------
    # Show the top-10 comps with similarity, post-age production, and a
    # "washed out" badge for comps whose career ended by age 30 with
    # fewer than 8 NFL seasons. Phil's 2026-05-22 critique on Bo Nix →
    # Aaron Brooks: the model picks vector-similar QBs but a wash-out
    # comp telegraphs that the projection is fragile. Surfacing the flag
    # lets users SEE when a high-similarity comp is a journeyman.
    comp_rows = ""
    sum_sim = 0.0
    sum_sim_x_pts = 0.0
    sum_sim_x_years = 0.0
    n_washed = 0
    n_durable = 0
    for c in comps[:20]:
        sim = float(c.get("similarity", 0.0))
        pts = float(c.get("post_age_projected_pts", 0.0))
        years = float(c.get("post_age_seasons", 0))
        sum_sim += sim
        sum_sim_x_pts += sim * pts
        sum_sim_x_years += sim * years
        if c.get("washed_out"):
            n_washed += 1
        else:
            n_durable += 1
    for c in comps[:10]:
        comp_peak = c.get("peak_3yr_fp_per_game", 0.0)
        sim = float(c.get("similarity", 0.0))
        seasons_played = c.get("seasons_played")
        final_age = c.get("final_age")
        career_note_parts = []
        if seasons_played is not None:
            career_note_parts.append(f"{seasons_played} seasons")
        if final_age is not None:
            career_note_parts.append(f"ended age {final_age}")
        career_note = " · ".join(career_note_parts) if career_note_parts else "—"
        washed_badge = (
            ' <span class="div-chip div-down" title="Career ended by age 30 '
            'with fewer than 8 NFL seasons">washed out</span>'
            if c.get("washed_out") else ""
        )
        # v2.4 PR 4: ⏳ era badge for comps whose snapshot season is
        # pre-1999. The underlying comp pool now includes Walter Payton,
        # Emmitt Smith, Marcus Allen, etc.; pre-1999 comps already carry a
        # 0.9× confidence haircut from PR 3 to acknowledge era-pace
        # uncertainty. The badge surfaces that lineage to the user.
        snap_year = c.get("snapshot_season")
        era_badge = (
            f' <span class="era-chip" title="Pre-1999 comp — stats are '
            f'era-pace-adjusted to modern scoring. Confidence weight: 0.9×.">'
            f'⏳ {snap_year}</span>'
            if c.get("is_pre1999_snapshot") and snap_year else ""
        )
        comp_rows += (
            f"<tr>"
            f"<td class='name'>{_esc(c['name'])}{era_badge}{washed_badge}</td>"
            f"<td>{_pos_badge(c['position'])}</td>"
            f"<td class='years'>{c['last_season']}</td>"
            f"<td class='score' style='text-align:right'>{sim:.3f}</td>"
            f"<td class='score' style='text-align:right'>{comp_peak:.1f}</td>"
            f"<td class='years'>{career_note}</td>"
            f"<td class='years'>{c['post_age_seasons']}</td>"
            f"<td class='years'>{c['career_ppr']:.0f}</td>"
            f"<td class='score' style='text-align:right'>{c['post_age_projected_pts']:.0f}</td>"
            f"</tr>"
        )

    # --- Calculation breakdown -------------------------------------------
    # Surface the EXPLICIT weighted-average so the user can audit the
    # rank. Sourced from the same diagnostic fields the engine stamps on
    # every row (comp_weighted_fp, peak_anchored_fp, projection_path,
    # survival_multiplier, sample_confidence, late_breakout_penalty).
    comp_weighted = float(row.get("comp_weighted_fp", 0.0))
    peak_anchored = float(row.get("peak_anchored_fp", 0.0))
    proven_floor_diag = float(row.get("proven_floor_fp", 0.0))
    projection_path = row.get("projection_path", "—")
    raw_pre_penalty = float(row.get("projection_raw_pre_penalty",
                                     max(comp_weighted, peak_anchored)))
    survival = float(row.get("survival_multiplier", 1.0))
    confidence = float(row.get("sample_confidence", 1.0))
    late_breakout = float(row.get("late_breakout_penalty", 1.0))
    # v3.3 missed-recent-season penalty diagnostics
    missed_mult = float(row.get("missed_season_multiplier", 1.0))
    missed_reason = row.get("missed_season_reason", "") or ""
    missed_last_played = row.get("missed_season_last_played")
    missed_last_played_games = row.get("missed_season_last_played_games")
    final = float(row.get("production_score", 0.0))
    n_comps = int(row.get("n_comps", len(comps)))
    avg_sim = (sum_sim / max(len(comps[:20]), 1)) if comps else 0.0
    pct_washed = (n_washed / max(n_washed + n_durable, 1)) * 100.0

    # Reconstruct the displayed weighted-average from the comp rows so the
    # user can confirm the engine's number from the visible data. If the
    # engine surfaced ``comp_weighted_fp`` use that as the source of truth;
    # otherwise compute from comps.
    if sum_sim > 0:
        recomputed_comp_proj = sum_sim_x_pts / sum_sim
    else:
        recomputed_comp_proj = 0.0

    # The v2.2 penalty stack uses an asymmetric Bayesian pull: if the
    # raw post-survival projection exceeds the position-tier baseline,
    # it shrinks TOWARD baseline; otherwise it multiplies straight.
    # Render the actual applied math.
    after_survival = raw_pre_penalty * survival
    confidence_step = (
        f"= max({after_survival:,.0f} × confidence + baseline × (1−conf), "
        f"or {after_survival:,.0f} × {confidence:.3f}) when below baseline"
    )
    breakdown_html = f"""
<h2>How this <span class="accent">number</span> is built</h2>
<p class="lede">Every component below is sourced from the engine output
(<code>engine_rankings.json</code>) and the displayed comp table. The
final production score is <strong>{final:,.0f}</strong>.</p>

<div class="controls" style="margin:8px 0 14px;flex-wrap:wrap;gap:8px">
  <span class="stats">Engine: <code>{_esc(str(row.get('engine','similarity_v1')))}</code></span>
  <span class="stats">Projection path: <code>{_esc(projection_path)}</code></span>
  <span class="stats">Comps: <strong>{n_comps}</strong></span>
  <span class="stats">Avg similarity (top 20): <strong>{avg_sim:.3f}</strong></span>
  <span class="stats">Comp pool washed-out rate: <strong>{pct_washed:.0f}%</strong> ({n_washed}/{n_washed + n_durable})</span>
</div>

<table style="max-width:780px">
<thead><tr><th>Step</th><th style="text-align:right">Value</th><th>What it is</th></tr></thead>
<tbody>
<tr><td class="name"><strong>Comp-weighted projection (v3.3 primary)</strong></td>
    <td class="score" style="text-align:right"><strong>{comp_weighted:,.0f}</strong></td>
    <td>Σ (sim<sub>i</sub> × post-age-fp<sub>i</sub>) / Σ sim<sub>i</sub>.
    Sanity check from the top-20 row data on this page:
    <strong>{recomputed_comp_proj:,.0f}</strong>. <em>This is the
    “weighted average of the comparable players’ actual realised
    post-age fantasy points” — Phil's 2026-05-28 brief.</em></td></tr>
<tr><td class="name">Peak-anchored projection</td>
    <td class="score" style="text-align:right">{peak_anchored:,.0f}</td>
    <td>The player's own peak-3yr-fp/g × expected games × horizon ×
    5%/yr time discount, capped at 1.25× the top single-comp
    projection. Used as a soft blend for elite-tier producers
    (60/40 comp-weighted / capped peak) so a Mahomes / Allen isn't
    fully dragged to the comp-pool mean.</td></tr>
<tr><td class="name">Banked-credit floor (diagnostic only)</td>
    <td class="score" style="text-align:right">{proven_floor_diag:,.0f}</td>
    <td>The v3.1 “dynasty equity” floor = career_total_fp ×
    yrs-rem-weight + recent rate × short forward window. <strong>v3.3
    no longer lets this override the projection</strong> — banked production
    is already realised, not “remaining.” Shown for transparency.</td></tr>
<tr><td class="name">Raw projection (pre-penalty)</td>
    <td class="score" style="text-align:right">{raw_pre_penalty:,.0f}</td>
    <td>Blended comp-weighted + capped peak-anchored, per the
    v3.3 methodology described above.</td></tr>
<tr><td class="name">× Survival</td>
    <td class="score" style="text-align:right">{survival:.3f}</td>
    <td>Multiplier reflecting how many comps washed out by age 30.
    Today: 1 − 0.5 × bust_rate.</td></tr>
<tr><td class="name">× Sample confidence</td>
    <td class="score" style="text-align:right">{confidence:.3f}</td>
    <td>Shrinks small-sample projections toward the position-tier baseline
    when raw &gt; baseline; multiplies straight otherwise. {_esc(confidence_step)}.</td></tr>
<tr><td class="name">× Late-breakout penalty</td>
    <td class="score" style="text-align:right">{late_breakout:.3f}</td>
    <td>QB-only. Scales by confidence so unproven late-breakout rookies
    only pay a fraction of the discount.</td></tr>
<tr><td class="name">× Missed-season penalty (v3.3)</td>
    <td class="score" style="text-align:right">{missed_mult:.3f}</td>
    <td>Multiplicative haircut for players who didn't play (or only
    played a partial slate) in the most recent NFL season. 0.70 for
    a full missed season, 0.85 for &lt;8 games, 0.45 for two+ full
    missed seasons. <em>{_esc(missed_reason)}</em></td></tr>
<tr><td class="name"><strong>= Final production score</strong></td>
    <td class="score" style="text-align:right"><strong>{final:,.0f}</strong></td>
    <td>Drives the player's rank on the <a href="../rankings.html">Similarity
    Scores</a> page.</td></tr>
</tbody>
</table>
"""

    body = f"""{_player_header(row, team, league_label)}
<div class="container">
{career_stats_html}
<h2>Fantasy-Point Arc <span class="accent">Comparables</span></h2>
<p class="lede">The top-10 most similar <em>long-arc</em> NFL players matched by
<strong>fantasy-point production curve</strong> at this career stage. Each
comp's "Peak 3yr fp/g" is their best 3-season fp/g average under
{_esc(league_label)} (era-pace-adjusted to modern). Similarity is bounded
in (0, 1] — 1.0 means an identical career-stage profile vector. "Career"
notes the comp's NFL longevity; the <span class="div-chip div-down">washed
out</span> badge flags comps whose career ended by age 30 with fewer than
8 NFL seasons (the engine’s bust definition). The <span class="era-chip">⏳ 1985</span>
badge marks comps whose snapshot season predates 1999 — the corpus now
spans 1980-2025, so Payton, Emmitt, Marcus Allen, Rice, Marino-era greats
are eligible matches. Pre-1999 comps carry a 0.9× confidence haircut
because era-pace adjustment is principled but not perfect.</p>

<table>
<thead><tr>
  <th>Comparable</th><th>Pos</th><th>Last season</th>
  <th style="text-align:right">Similarity</th>
  <th style="text-align:right">Peak 3yr fp/g</th>
  <th>Career</th>
  <th>Post-age seasons</th>
  <th>Career fp</th>
  <th style="text-align:right">Projected pts</th>
</tr></thead>
<tbody>{comp_rows}</tbody>
</table>

{breakdown_html}

<p class="lede" style="margin-top:24px">Want this player ranked under your
league's specific scoring + roster rules? Head to
<a href="../league.html">Dynasty Rankings</a>.</p>

</div>"""

    return _page(
        f"Kings of Dynasty — {row['name']}",
        _site_header("rankings", latest_ts, league_label),
        body,
        css_href="../assets/style.css",
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _load_gsis_to_pfr() -> Dict[str, str]:
    """Build a ``gsis_id -> pfr_id`` lookup from ``data/nflverse/players.csv.gz``.

    Falls back to an empty dict if the file isn't present (e.g. fresh
    clone before the first ``refresh_nflverse_corpus`` run). PFR-only
    retired comps whose corpus player_id is ``pfr_<PfrId>`` are mapped
    by stripping the prefix.
    """
    out: Dict[str, str] = {}
    path = Path("data") / "nflverse" / "players.csv.gz"
    if not path.exists():
        # Try repo-root-relative resolution as well.
        alt = Path(__file__).resolve().parents[2] / "data" / "nflverse" / "players.csv.gz"
        if alt.exists():
            path = alt
        else:
            return out
    try:
        import csv
        import gzip
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            for row in csv.DictReader(fh):
                gsis = (row.get("gsis_id") or "").strip()
                pfr = (row.get("pfr_id") or "").strip()
                if gsis and pfr:
                    out[gsis] = pfr
    except Exception:  # noqa: BLE001
        return out
    return out


def _resolve_pfr_id(player_id: str, gsis_to_pfr: Dict[str, str]) -> Optional[str]:
    """Best-effort PFR id resolver for engine rows.

    Engine ``player_id`` values are usually nflverse ``gsis_id`` strings
    (``00-XXXXXXX``). Pre-1999 retired comps use a synthetic
    ``pfr_<PfrId>`` id where the PFR token is the suffix.
    """
    if not player_id:
        return None
    if player_id.startswith("pfr_"):
        return player_id[4:]
    return gsis_to_pfr.get(player_id)


def _load_sleeper_teams() -> Dict[str, str]:
    """Pull current Sleeper player → team map, keyed by GSIS id where possible.

    Falls back to an empty map if the DB isn't initialised or the Player table
    doesn't carry a GSIS-id column. Team rendering degrades to "—".
    """
    try:
        from .db.session import get_session
        from .db.models import Player
        from sqlalchemy import select
        out: Dict[str, str] = {}
        with get_session() as session:
            for p in session.execute(select(Player)).scalars():
                gsis = getattr(p, "gsis_id", None) or getattr(p, "pfr_id", None)
                team = getattr(p, "team", None) or getattr(p, "nfl_team", None)
                if gsis and team:
                    out[gsis] = team
        return out
    except Exception:
        return {}


def generate_site(
    output_dir: str = "dynasty_site",
    league_format: str = "sf_ppr",
    limit: int = 300,
    additional_formats=None,    # kept for backwards-compat; ignored in v1
    engine: Optional[EngineResult] = None,
) -> str:
    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "assets").mkdir(parents=True, exist_ok=True)
    (out_root / "players").mkdir(parents=True, exist_ok=True)

    latest_ts = datetime.now(timezone.utc)

    if engine is None:
        engine = run_engine(persist=True)

    overlays = all_format_overlays(engine)
    team_lookup = _load_sleeper_teams()

    label = PRESETS.get(league_format, PRESETS["sf_ppr"])["label"]

    (out_root / "assets" / "style.css").write_text(_shared_css(), encoding="utf-8")

    # rankings.html — primary landing page (no index.html distinction needed)
    rankings_html = _build_rankings(engine, latest_ts, label, team_lookup, limit=limit)
    (out_root / "rankings.html").write_text(rankings_html, encoding="utf-8")
    (out_root / "index.html").write_text(rankings_html, encoding="utf-8")

    (out_root / "league.html").write_text(
        _build_league(overlays, latest_ts, label, team_lookup, engine=engine),
        encoding="utf-8",
    )
    (out_root / "methodology.html").write_text(
        _build_methodology(engine, latest_ts, label),
        encoding="utf-8",
    )
    (out_root / "sources.html").write_text(
        _build_sources(latest_ts, label),
        encoding="utf-8",
    )
    (out_root / "prospects.html").write_text(
        _build_prospects(latest_ts, label),
        encoding="utf-8",
    )

    # v3.0 PR 6: per-prospect pages. Loaded from the same artifact the
    # prospects.html builder uses. If the artifact is missing/empty,
    # _load_prospects_artifact returns None and we skip the loop — the
    # placeholder prospects.html already explains why.
    _prospects_artifact = _load_prospects_artifact()
    if _prospects_artifact and _prospects_artifact.get("prospects"):
        # Pre-compute the set of veteran slugs we generated above so the
        # comp grid can cross-link safely (without 404s).
        veteran_slugs = {
            _slug(r["name"], r["player_id"]) for r in engine.rankings
        }
        for _prospect in _prospects_artifact["prospects"]:
            _pslug = _prospect_slug(_prospect)
            _page_html = _build_prospect_page(
                _prospect, label, latest_ts, veteran_slugs=veteran_slugs
            )
            (out_root / "players" / f"{_pslug}-prospect.html").write_text(
                _page_html, encoding="utf-8"
            )

    # Per-player pages. v2.3.4 (Phil 2026-05-22): generate a page for
    # EVERY ranked player, not just the top ``limit``, so every row on
    # the Dynasty Rankings consensus tab clicks through to a
    # similarity-score page. The top-N main rankings table still uses
    # ``limit`` for the homepage display.
    #
    # v3.10 (Phil 2026-06-02): each profile also surfaces a Career Stats
    # section sourced from PFR via the cache-first builder in
    # ``sources.pfr_career_stats``. Builder degrades gracefully when a
    # row's PFR id can't be resolved or the scrape fails.
    gsis_to_pfr = _load_gsis_to_pfr()
    skip_career_stats = os.environ.get("DFM_SKIP_CAREER_STATS") == "1"
    for row in engine.rankings:
        slug = _slug(row["name"], row["player_id"])
        comps = engine.comps.get(row["player_id"], [])
        team = team_lookup.get(row["player_id"], "—")
        career_html = ""
        if not skip_career_stats:
            pfr_id = _resolve_pfr_id(row.get("player_id", ""), gsis_to_pfr)
            if pfr_id:
                try:
                    career = _pfr_career.build_career_stats(
                        pfr_id, row.get("position", "")
                    )
                    career_html = _pfr_career.career_stats_html(career)
                except Exception as exc:  # noqa: BLE001
                    # Never let a single bad scrape break the build.
                    import logging
                    logging.getLogger(__name__).warning(
                        "career-stats failed for %s (%s): %s",
                        row.get("name"), pfr_id, exc,
                    )
        page = _build_player_page(
            row, comps, team, label, latest_ts,
            career_stats_html=career_html,
        )
        (out_root / "players" / f"{slug}.html").write_text(page, encoding="utf-8")

    # Also drop the engine's master rankings JSON next to the site so the
    # league-import flow can consume it without re-running the engine.
    (out_root / "engine_rankings.json").write_text(
        json.dumps([dict(r) for r in engine.rankings], indent=2, default=float),
        encoding="utf-8",
    )

    return str(out_root.resolve())
