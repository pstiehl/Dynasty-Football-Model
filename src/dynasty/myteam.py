"""myteam.html — "my Sleeper team, ranked by the model, with this week's film".

Three views over one roster, as tabs:

1. **Roster** — every player Sleeper says you own, grouped by position,
   including the ones the model doesn't rank and the ones with no film.
2. **Rankings** — the same players ordered by the model's Dynasty Rankings,
   with the columns the rankings page uses, linking through to each
   player's similarity page.
3. **Highlights** — each player's most recent clips, plus the full Roster
   Reel player so the whole roster still plays back to back.

Why this page embeds the reel instead of reimplementing it
----------------------------------------------------------
``reel.py`` already owns the hard part: username -> leagues -> rosters ->
team picker, localStorage persistence, the YouTube playlist player, and the
name matcher mirrored from ``dynasty.highlights``. This page renders the
reel's markup verbatim inside its Highlights tab and pulls in
``reel.reel_assets()``, so there is exactly one implementation of that
plumbing. The only addition on the reel side is the ``DFM_ON_ROSTER`` hook,
which hands this page the roster the reel just fetched.

The id join, which is the actual new work
-----------------------------------------
Three artifacts, three different keys:

* ``engine_rankings.json`` — the model's rankings, keyed by **gsis id**.
* ``highlights.json`` — clips keyed by **sleeper id**.
* a Sleeper roster — a list of **sleeper ids** and nothing else.

``highlights.json`` ships a ``by_gsis`` crosswalk (gsis -> sleeper), which
bridges rankings to clips. But it only covers players that *have* clips, so
on its own a ranked player with no film this week would vanish from your
roster, and with ``highlights.json`` absent the rankings view would be
empty — which is the normal state until the nightly job has run once.

So ``report.generate_site`` also writes ``roster_index.json``: a compact
sleeper_id -> [name, position, team, gsis_id] crosswalk for every canonical
player. That is the primary bridge; ``by_gsis`` is the fallback that keeps
the page useful even when the crosswalk artifact is missing. Either one
alone degrades to a narrower but still working page, and neither being
present degrades to an explanation rather than a blank screen.
"""
from __future__ import annotations

from datetime import datetime


# --------------------------------------------------------------------------
# Client-side script. Token-substituted rather than f-stringed so the JS
# braces stay readable — same convention as reel.py.
# --------------------------------------------------------------------------

_MYTEAM_JS = r"""
// The reel scrolls itself into view on load; here it lives behind a tab.
window.DFM_SUPPRESS_REEL_SCROLL = true;

const MT = {
  rankings: null,      // engine_rankings.json (array, gsis-keyed rows)
  crosswalk: null,     // roster_index.json
  byGsis: {},          // gsis_id -> ranking row
  sidToGsis: {},       // sleeper_id -> gsis_id
  roster: [],          // resolved roster, model order
  loadErrors: []
};

// ---------------------------------------------------------------- helpers

// Mirror of report._slug so roster rows can link at the per-player pages
// generate_site writes. Kept in lockstep with that function by hand; a
// mismatch shows up as a dead link, never as a wrong player.
function slugFor(name, playerId) {
  const base = String(name == null ? '' : name)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return base + '-' + String(playerId == null ? '' : playerId)
    .replace(/-/g, '').slice(-6);
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

const POS_COLOR = __POS_COLOR__;

function posBadge(pos) {
  if (!pos) return '<span class="pos-badge" style="background:#9ca3af">—</span>';
  const c = POS_COLOR[pos] || '#9ca3af';
  return '<span class="pos-badge" style="background:' + c + '">' + esc(pos) + '</span>';
}

function median(nums) {
  if (!nums.length) return null;
  const s = nums.slice().sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid] : Math.round((s[mid - 1] + s[mid]) / 2);
}

function note(html) {
  return '<div class="callout callout-warn mt-note">' + html + '</div>';
}

// ---------------------------------------------------------------- data load

// Kicked off at parse time, not on DOMContentLoaded: the roster hook can
// fire as soon as the user clicks, and it awaits this.
const MODEL_READY = loadModelData();

async function loadModelData() {
  try {
    MT.rankings = await getJSON('engine_rankings.json');
  } catch (e) {
    MT.rankings = null;
    MT.loadErrors.push('engine_rankings.json');
  }
  try {
    MT.crosswalk = await getJSON('roster_index.json');
  } catch (e) {
    MT.crosswalk = null;
  }

  (MT.rankings || []).forEach(r => {
    if (r && r.player_id) MT.byGsis[r.player_id] = r;
  });

  // Primary bridge: the build-time crosswalk covers every canonical player,
  // ranked or not, with or without film.
  const cw = (MT.crosswalk && MT.crosswalk.players) || {};
  for (const sid of Object.keys(cw)) {
    const g = cw[sid] && cw[sid][3];
    if (g) MT.sidToGsis[sid] = g;
  }
  // Fallback bridge: highlights.json's gsis -> sleeper map. Only covers
  // players with clips, which is exactly the gap it needs to cover when
  // roster_index.json is missing.
  const byg = (typeof HL !== 'undefined' && HL && HL.by_gsis) || {};
  for (const g of Object.keys(byg)) {
    const sid = String(byg[g]);
    if (!MT.sidToGsis[sid]) MT.sidToGsis[sid] = g;
  }
  return true;
}

// ---------------------------------------------------------------- resolve

function resolvePlayer(sid) {
  sid = String(sid);
  const cw = (MT.crosswalk && MT.crosswalk.players && MT.crosswalk.players[sid]) || null;
  const hp = (typeof HL !== 'undefined' && HL && HL.players && HL.players[sid]) || null;
  const gsis = MT.sidToGsis[sid] || null;
  const row = gsis ? (MT.byGsis[gsis] || null) : null;
  const clips = (typeof HL !== 'undefined' && HL && HL.clips && HL.clips[sid]) || [];

  let rank = null;
  if (row && row.overall_rank != null) rank = row.overall_rank;
  else if (hp && hp.rank != null) rank = hp.rank;

  return {
    sid: sid,
    name: (cw && cw[0]) || (hp && hp.name) || (row && row.name) || ('Sleeper #' + sid),
    pos: (cw && cw[1]) || (hp && hp.position) || (row && row.position) || '',
    team: (cw && cw[2]) || (hp && hp.team) || '',
    gsis: gsis,
    row: row,
    rank: rank,
    clips: clips,
    identified: !!(cw || hp || row)
  };
}

// ---------------------------------------------------------------- render

const POS_ORDER = ['QB', 'RB', 'WR', 'TE', 'K', 'DEF'];

function renderAll(sleeperIds) {
  MT.roster = (sleeperIds || []).map(resolvePlayer);
  renderSummary();
  renderRosterTab();
  renderRankingsTab();
  renderHighlightsTab();
  show('team-views', true);
  document.getElementById('team-views')
    .scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderSummary() {
  const r = MT.roster;
  const ranked = r.filter(p => p.rank != null);
  const ranks = ranked.map(p => p.rank);
  const best = ranked.slice().sort((a, b) => a.rank - b.rank)[0] || null;
  const withClips = r.filter(p => p.clips.length);
  const totalClips = r.reduce((a, p) => a + p.clips.length, 0);
  const top24 = ranks.filter(x => x <= 24).length;

  const kpis = [
    ['' + r.length, 'Players rostered'],
    ['' + ranked.length, 'Ranked by the model'],
    [best ? '#' + best.rank : '—', best ? 'Best: ' + best.name : 'Best ranked player'],
    [ranks.length ? '#' + median(ranks) : '—', 'Median model rank'],
    ['' + top24, 'Inside the top 24'],
    ['' + withClips.length, 'With film this week']
  ];

  document.getElementById('team-summary').innerHTML =
    '<div class="kpi-row">' + kpis.map(k =>
      '<div class="kpi"><div class="num">' + esc(k[0]) + '</div>' +
      '<div class="label">' + esc(k[1]) + '</div></div>'
    ).join('') + '</div>' +
    '<div class="mt-sub">' + esc(totalClips) + ' clip' + (totalClips === 1 ? '' : 's') +
    ' indexed across ' + esc(withClips.length) + ' of ' + esc(r.length) +
    ' rostered players' + (MT.roster.some(p => !p.identified)
      ? ' · ' + MT.roster.filter(p => !p.identified).length +
        ' player(s) could not be identified from the crosswalk'
      : '') + '</div>';
}

function playerCell(p) {
  const label = esc(p.name);
  if (p.row && p.gsis) {
    return '<a href="players/' + esc(slugFor(p.row.name, p.gsis)) + '.html">' + label + '</a>';
  }
  return label;
}

function renderRosterTab() {
  const box = document.getElementById('roster-pane-body');
  if (!MT.roster.length) {
    box.innerHTML = '<div class="empty">That roster came back empty.</div>';
    return;
  }
  const groups = {};
  MT.roster.forEach(p => {
    const k = POS_ORDER.indexOf(p.pos) >= 0 ? p.pos : (p.pos || 'Other');
    (groups[k] = groups[k] || []).push(p);
  });
  const keys = Object.keys(groups).sort((a, b) => {
    const ia = POS_ORDER.indexOf(a), ib = POS_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
  });

  let html = '';
  keys.forEach(k => {
    const list = groups[k].slice().sort((a, b) =>
      (a.rank == null ? 1e9 : a.rank) - (b.rank == null ? 1e9 : b.rank));
    html += '<h3>' + esc(k) + ' <span class="mt-count">' + list.length + '</span></h3>' +
      '<table><thead><tr><th>Model rank</th><th>Player</th><th>Pos</th>' +
      '<th>NFL</th><th>Clips</th></tr></thead><tbody>' +
      list.map(p =>
        '<tr><td class="rank">' + (p.rank == null ? '<span class="mt-unranked">—</span>' : p.rank) + '</td>' +
        '<td class="name">' + playerCell(p) + '</td>' +
        '<td>' + posBadge(p.pos) + '</td>' +
        '<td class="team">' + esc(p.team || '—') + '</td>' +
        '<td class="years">' + (p.clips.length || '—') + '</td></tr>'
      ).join('') + '</tbody></table>';
  });
  box.innerHTML = html;
}

function renderRankingsTab() {
  const box = document.getElementById('rankings-pane-body');
  if (!MT.rankings) {
    box.innerHTML = note('<strong>Model rankings unavailable.</strong> ' +
      '<code>engine_rankings.json</code> has not been generated next to this ' +
      'page yet, so rostered players can be listed but not ranked. It is ' +
      'written by the site build on every run.');
    return;
  }
  const ranked = MT.roster.filter(p => p.rank != null)
    .sort((a, b) => a.rank - b.rank);
  const unranked = MT.roster.filter(p => p.rank == null);

  if (!ranked.length) {
    box.innerHTML = note('<strong>None of these players are in the model.</strong> ' +
      'The engine ranks active players it has a production arc for — rookies ' +
      'and deep bench pieces often have none yet.');
    return;
  }

  const fmt = (v, digits) => (v == null || isNaN(v)) ? '—' : Number(v).toFixed(digits);
  let html =
    '<table><thead><tr><th>Rank</th><th>Player</th><th>Pos</th><th>NFL</th>' +
    '<th>Age</th><th>Tier</th><th>Comp tier</th><th class="score">Score</th>' +
    '</tr></thead><tbody>' +
    ranked.map(p => {
      const r = p.row || {};
      return '<tr><td class="rank">' + p.rank + '</td>' +
        '<td class="name">' + playerCell(p) + '</td>' +
        '<td>' + posBadge(p.pos) + '</td>' +
        '<td class="team">' + esc(p.team || '—') + '</td>' +
        '<td class="years">' + (r.age == null ? '—' : esc(r.age)) + '</td>' +
        '<td class="tier">' + (r.tier == null ? '—' : 'T' + esc(r.tier)) + '</td>' +
        '<td class="years">' + esc(r.comp_tier || '—') + '</td>' +
        '<td class="score">' + fmt(r.production_score, 0) + '</td></tr>';
    }).join('') + '</tbody></table>';

  if (unranked.length) {
    html += '<p class="mt-sub">' + unranked.length + ' rostered player' +
      (unranked.length === 1 ? '' : 's') + ' not ranked by the model: ' +
      unranked.map(p => esc(p.name)).join(', ') + '.</p>';
  }
  box.innerHTML = html;
}

function renderHighlightsTab() {
  const box = document.getElementById('clips-pane-body');
  const haveIndex = typeof HL !== 'undefined' && HL && HL.clips &&
                    Object.keys(HL.clips).length;
  if (!haveIndex) {
    box.innerHTML = note('<strong>No highlight index yet.</strong> ' +
      '<code>highlights.json</code> is written by the scheduled ' +
      '<em>Refresh YouTube highlight index</em> job. Until it has run once ' +
      'with a <code>YOUTUBE_API_KEY</code>, the roster and rankings tabs ' +
      'work normally and this tab stays empty.');
    return;
  }
  const withClips = MT.roster.filter(p => p.clips.length)
    .sort((a, b) => (a.rank == null ? 1e9 : a.rank) - (b.rank == null ? 1e9 : b.rank));
  if (!withClips.length) {
    box.innerHTML = '<div class="empty">None of your players have indexed ' +
      'clips right now. Enable game recaps above to fall back to team film.</div>';
    return;
  }
  box.innerHTML = withClips.map(p =>
    '<div class="mt-player"><div class="mt-player-head">' +
      posBadge(p.pos) +
      '<span class="mt-player-name">' + playerCell(p) + '</span>' +
      '<span class="mt-player-meta">' + esc(p.team || '') +
      (p.rank == null ? '' : ' · model #' + p.rank) + '</span></div>' +
    '<div class="mt-clips">' + p.clips.map(c =>
      '<button class="mt-clip" data-video="' + esc(c.video_id) + '">' +
        '<img loading="lazy" src="' + THUMB(c.video_id) + '" alt="">' +
        '<span class="mt-clip-body">' +
          '<span class="mt-clip-title">' + esc(c.title) + '</span>' +
          '<span class="mt-clip-meta">' + [
            c.week ? 'Wk ' + esc(c.week) : '',
            c.opponent ? 'vs ' + esc(c.opponent) : '',
            c.duration_seconds ? fmtDuration(c.duration_seconds) : '',
            c.kind === 'team_game' ? 'game recap' : '',
            esc(c.channel_title || '')
          ].filter(Boolean).join(' · ') + '</span>' +
        '</span></button>'
    ).join('') + '</div></div>'
  ).join('');

  box.querySelectorAll('.mt-clip').forEach(b => {
    b.addEventListener('click', () => playClip(b.dataset.video));
  });
}

// Hand off to the embedded reel when the clip is in its queue; otherwise
// the clip exists in the index but is filtered out of the current queue
// (a recap with recaps switched off, say), so open it on YouTube instead
// of silently doing nothing.
function playClip(videoId) {
  const q = (typeof queue !== 'undefined' && queue) || [];
  const idx = q.findIndex(x => x.videoId === videoId);
  if (idx >= 0 && typeof startAt === 'function') {
    startAt(idx);
    document.getElementById('step-reel')
      .scrollIntoView({ behavior: 'smooth', block: 'start' });
  } else {
    window.open('https://www.youtube.com/watch?v=' + encodeURIComponent(videoId),
                '_blank', 'noopener');
  }
}

// ---------------------------------------------------------------- wiring

window.DFM_ON_ROSTER = function (sleeperIds) {
  MODEL_READY.then(() => renderAll(sleeperIds))
             .catch(e => status('Could not build your team view: ' + e.message, true));
};

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.view-tab').forEach(b => {
    b.addEventListener('click', () => {
      document.querySelectorAll('.view-tab').forEach(x => x.classList.remove('on'));
      document.querySelectorAll('.view-pane').forEach(x => { x.style.display = 'none'; });
      b.classList.add('on');
      document.getElementById(b.dataset.view).style.display = '';
    });
  });

  MODEL_READY.then(() => {
    if (!MT.rankings) {
      const el = document.getElementById('mt-artifact-note');
      el.innerHTML = note('<strong>Model rankings artifact missing.</strong> ' +
        'This page falls back to whatever the highlight index knows. Re-run ' +
        'the site build to produce <code>engine_rankings.json</code>.');
      el.style.display = '';
    }
  });
});
"""


_MYTEAM_CSS = """
.mt-sub { font-size: 13px; color: var(--muted); margin: 10px 0 0; }
.mt-count { font-size: 12px; color: var(--muted); font-weight: 500; }
.mt-unranked { color: var(--muted); }
.mt-note { margin: 0 0 16px; }
.view-tabs { display: flex; gap: 6px; flex-wrap: wrap; margin: 26px 0 16px; }
.view-tab { font: inherit; font-size: 13px; font-weight: 600; padding: 9px 18px;
  border-radius: 999px; background: var(--card); border: 1px solid var(--border);
  color: var(--text); cursor: pointer; }
.view-tab:hover { background: var(--hover); }
.view-tab.on { background: var(--accent); color: #fff; border-color: var(--accent); }
.view-pane h3:first-child { margin-top: 4px; }
.view-pane table { margin-bottom: 20px; }
.mt-player { border: 1px solid var(--border); border-radius: 10px;
  background: var(--card); padding: 14px 16px; margin-bottom: 12px; }
.mt-player-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  margin-bottom: 10px; }
.mt-player-name { font-weight: 600; font-size: 15px; }
.mt-player-meta { font-size: 12px; color: var(--muted); }
.mt-clips { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 8px; }
.mt-clip { display: flex; gap: 10px; align-items: center; text-align: left;
  background: var(--bg); border: 1px solid var(--border); color: inherit;
  border-radius: 8px; padding: 6px; cursor: pointer; font: inherit; }
.mt-clip:hover { border-color: var(--accent); }
.mt-clip img { width: 76px; height: 43px; object-fit: cover; border-radius: 6px;
  background: #222; flex: none; }
.mt-clip-body { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.mt-clip-title { font-size: 12px; font-weight: 600; line-height: 1.35;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden; }
.mt-clip-meta { font-size: 11px; color: var(--muted); }
"""


# Mirrors report.POSITION_COLOR so client-rendered badges match the
# server-rendered ones on every other page.
def _pos_color_json() -> str:
    import json

    from .report import POSITION_COLOR

    return json.dumps(POSITION_COLOR, sort_keys=True)


def build_my_team(latest_ts: datetime, league_label: str) -> str:
    """Render myteam.html. Imported lazily by report.generate_site."""
    from .report import _page, _site_header  # local: avoids a cycle
    from .reel import reel_assets

    reel_js, reel_css = reel_assets()

    body = """<div class="container">

<h2>My <span class="accent">Team</span></h2>
<p class="lede">Pull your Sleeper roster in, see where the model ranks every
player you own, and watch their most recent film. Rosters are read live from
Sleeper's public API in your browser — nothing is sent to this site.</p>
<div class="hl-meta" id="hl-meta">Loading highlight index…</div>
<div id="mt-artifact-note" style="display:none"></div>

<div class="tabs">
  <button class="tab-btn on" data-pane="pane-user">Sleeper username</button>
  <button class="tab-btn" data-pane="pane-league">League ID</button>
  <button class="tab-btn" data-pane="pane-custom">Custom roster</button>
</div>

<div class="tab-pane" id="pane-user">
  <div class="reel-input">
    <input id="sleeper-user" type="text" placeholder="Sleeper username" autocomplete="off">
    <button class="btn" id="load-user">Find my leagues</button>
  </div>
</div>

<div class="tab-pane" id="pane-league" style="display:none">
  <div class="reel-input">
    <input id="league-id" type="text" placeholder="Sleeper league ID" autocomplete="off">
    <button class="btn" id="load-league">Load league</button>
  </div>
</div>

<div class="tab-pane" id="pane-custom" style="display:none">
  <div class="reel-input">
    <textarea id="custom-names" placeholder="One player per line&#10;Ja'Marr Chase&#10;Bijan Robinson&#10;Brock Bowers"></textarea>
  </div>
  <div style="margin-top:8px"><button class="btn" id="load-custom">Build my team</button></div>
</div>

<div class="reel-status" id="reel-status" style="display:none"></div>
<div id="step-league"><div id="league-list"></div></div>

<div id="team-views" style="display:none">
  <div id="team-summary"></div>

  <div class="view-tabs">
    <button class="view-tab on" data-view="view-roster">Roster</button>
    <button class="view-tab" data-view="view-rankings">Dynasty Rankings</button>
    <button class="view-tab" data-view="view-highlights">Highlights</button>
  </div>

  <div class="view-pane" id="view-roster">
    <div id="roster-pane-body"></div>
  </div>

  <div class="view-pane" id="view-rankings" style="display:none">
    <div id="rankings-pane-body"></div>
  </div>

  <div class="view-pane" id="view-highlights" style="display:none">
    <div id="clips-pane-body"></div>

    <h3>Roster Reel</h3>
    <p class="mt-sub">Every clip above, queued back to back in model order.</p>

    <div id="step-reel" style="display:none">
      <div id="roster-summary" style="font-size:13px;opacity:.75;margin-top:10px"></div>

      <div class="reel-opts">
        <label><input type="checkbox" id="opt-team"> include game recaps when no cut-up exists</label>
        <label><input type="checkbox" id="opt-all"> every clip per player (not just the newest)</label>
      </div>

      <button class="btn btn-lg" id="play-all" disabled>Play all</button>

      <div class="reel-grid">
        <div>
          <div class="player-box" id="player-wrap" style="display:none"><div id="yt-frame"></div></div>
          <div id="now-playing"></div>
        </div>
        <div id="queue-list"></div>
      </div>
    </div>
  </div>
</div>

<p style="font-size:12px;opacity:.55;margin-top:28px">Clips are matched to
players automatically from public YouTube uploads and embedded via the
YouTube player, so views and ad revenue stay with the original uploader.
Model ranks come from the same engine that powers the Dynasty Rankings tab.</p>

</div>
<style>__REEL_CSS____MYTEAM_CSS__</style>
<script src="https://www.youtube.com/iframe_api"></script>
<script>__REEL_JS__</script>
<script>__MYTEAM_JS__</script>
"""
    body = (body
            .replace("__REEL_CSS__", reel_css)
            .replace("__MYTEAM_CSS__", _MYTEAM_CSS)
            .replace("__REEL_JS__", reel_js)
            .replace("__MYTEAM_JS__",
                     _MYTEAM_JS.replace("__POS_COLOR__", _pos_color_json())))

    return _page(
        "Kings of Dynasty — My Team",
        _site_header("myteam", latest_ts, league_label),
        body,
    )
