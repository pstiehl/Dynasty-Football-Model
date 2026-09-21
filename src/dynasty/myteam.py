"""myteam.html — "my Sleeper team, ranked by the model, against my league".

Four views, as tabs:

1. **Roster** — every player Sleeper says you own, grouped by position.
   Every one of them carries either a model rank or a stated reason it has
   none; nobody is silently dropped.
2. **Rankings** — the ranked players ordered by the model's Dynasty
   Rankings, followed by an explicit *Unranked* table giving the reason for
   each of the rest.
3. **League** — every other team in the selected Sleeper league, scored on
   model rank, with each manager's display name and a roster you can open.
   The scoring method is documented on the tab itself.
4. **Highlights** — each player's clips from the most recently completed
   slate, plus the full Roster Reel player.

Why this page embeds the reel instead of reimplementing it
----------------------------------------------------------
``reel.py`` already owns the hard part: username -> leagues -> rosters ->
team picker, localStorage persistence, the YouTube playlist player, and the
name matcher mirrored from ``dynasty.highlights``. This page renders the
reel's markup verbatim inside its Highlights tab and pulls in
``reel.reel_assets()``, so there is exactly one implementation of that
plumbing. The reel side gains two hooks only: ``DFM_ON_ROSTER``, which
hands this page the roster the reel just fetched, and ``DFM_ON_LEAGUE``,
which hands it every roster plus the league's users so the League tab costs
no extra Sleeper requests.

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

The crosswalk is not sufficient on its own, which is what made rankings go
missing in practice: it stores ``""`` when the player table has no gsis id,
and the gsis join then cannot fire even though the engine ranks that player.
:func:`resolvePlayer` therefore falls back to a normalized name+position
join, and refuses the join outright when two ranked players collide. See
the comment above it for the full list of ways a rostered player can end up
without a rank, and which of them are bugs versus correct answers.
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
  byNameKey: {},       // match-key -> [ranking row, ...]
  sidToGsis: {},       // sleeper_id -> gsis_id
  roster: [],          // resolved roster, model order
  league: null,        // {leagueId, rosters, users, userId, myRosterId}
  teams: [],           // scored league teams, strongest first
  openTeamId: null,    // roster_id of the team expanded in the league view
  loadErrors: []
};

// ------------------------------------------------- team-strength metric
//
// Dynasty value is steeply convex in rank: the gap between the #1 and #10
// assets dwarfs the gap between #200 and #210, so averaging raw ordinals
// would call a roster of twenty #150s stronger than one built around two
// top-five players. Both halves of this metric exist to avoid that.
//
//   value(rank) = 100 * e^-((rank-1)/DECAY)
//
// An exponential decay is the shape published dynasty trade-value charts
// actually take. DECAY = 50 puts #1 at 100.0, #25 at 61.9, #50 at 37.5,
// #100 at 13.9 and #200 at 1.9 -- i.e. roughly a halving every 35 ranks.
//
// Summing only the best CORE players, rather than the whole roster, stops
// a team that hoards fifty marginal bodies from out-scoring a contender on
// depth alone. 15 is a superflex-ish starting lineup plus a bench spot.
//
// Unranked players contribute zero. That is the honest treatment -- the
// model has no opinion on them -- but it does mean a team carrying many
// rookies is scored on a floor, so the table reports each team's unranked
// count next to its score and the page says this in plain language.
const TS = { decay: 50, core: 15 };

function rankValue(rank) {
  if (rank == null) return 0;
  return 100 * Math.exp(-(Math.max(1, rank) - 1) / TS.decay);
}

function scoreTeam(players) {
  const ranked = players.filter(p => p.rank != null)
                        .sort((a, b) => a.rank - b.rank);
  const core = ranked.slice(0, TS.core);
  const raw = core.reduce((a, p) => a + rankValue(p.rank), 0);
  return {
    raw: raw,
    counted: core.length,
    nRanked: ranked.length,
    nUnranked: players.length - ranked.length,
    best: ranked.length ? ranked[0] : null,
    medianCoreRank: median(core.map(p => p.rank))
  };
}

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

// Positions the engine deliberately does not rank. It scores offensive
// skill players off an NFL production arc; there is no arc to build for a
// kicker, a linebacker or a team defense, so "no rank" there is a correct
// answer rather than a join failure, and the page must say which it is.
const KICKER_POS = new Set(['K', 'PK']);
const IDP_POS = new Set([
  'DEF', 'DL', 'DE', 'DT', 'NT', 'EDGE', 'LB', 'OLB', 'ILB', 'MLB',
  'DB', 'CB', 'S', 'FS', 'SS', 'P', 'LS', 'OL', 'OT', 'OG', 'G', 'T', 'C'
]);

// Sleeper stores a rostered team defense as the club's abbreviation in the
// same ``players`` array as numeric player ids -- "DAL" sits next to
// "4046". Those ids hit nothing in any crosswalk, so they used to render
// as "Sleeper #DAL" with a blank rank and no explanation.
const TEAM_CODES = new Set([
  'ARI', 'ATL', 'BAL', 'BUF', 'CAR', 'CHI', 'CIN', 'CLE', 'DAL', 'DEN',
  'DET', 'GB', 'HOU', 'IND', 'JAX', 'KC', 'LAC', 'LAR', 'LV', 'MIA',
  'MIN', 'NE', 'NO', 'NYG', 'NYJ', 'PHI', 'PIT', 'SEA', 'SF', 'TB',
  'TEN', 'WAS', 'OAK', 'SD', 'STL'
]);

const TEAM_NAMES = {
  ARI: 'Cardinals', ATL: 'Falcons', BAL: 'Ravens', BUF: 'Bills',
  CAR: 'Panthers', CHI: 'Bears', CIN: 'Bengals', CLE: 'Browns',
  DAL: 'Cowboys', DEN: 'Broncos', DET: 'Lions', GB: 'Packers',
  HOU: 'Texans', IND: 'Colts', JAX: 'Jaguars', KC: 'Chiefs',
  LAC: 'Chargers', LAR: 'Rams', LV: 'Raiders', MIA: 'Dolphins',
  MIN: 'Vikings', NE: 'Patriots', NO: 'Saints', NYG: 'Giants',
  NYJ: 'Jets', PHI: 'Eagles', PIT: 'Steelers', SEA: 'Seahawks',
  SF: '49ers', TB: 'Buccaneers', TEN: 'Titans', WAS: 'Commanders',
  OAK: 'Raiders', SD: 'Chargers', STL: 'Rams'
};

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
    if (!r) return;
    if (r.player_id) MT.byGsis[r.player_id] = r;
    // Secondary index for the id-join gap below. ``matchKey`` comes from
    // reel.js and mirrors dynasty.names.normalize, so "Kenneth Walker III"
    // in the crosswalk and "Kenneth Walker" in the engine collapse to one
    // key -- the same folding the highlight matcher already relies on.
    const k = matchKey(r.name);
    if (k) (MT.byNameKey[k] = MT.byNameKey[k] || []).push(r);
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
//
// Every rostered id must come out of here with either a rank or a stated
// reason there isn't one. Dropping a player, or showing a bare dash, is
// what made the page look like it had lost half the roster.
//
// Four ways a player used to end up silently rankless:
//
//   1. Sleeper's roster carries team defenses as club abbreviations
//      ("DAL"), which match nothing anywhere.
//   2. ``roster_index.json`` writes ``""`` for a player with no gsis id,
//      so ``sidToGsis`` never gets an entry and the gsis join cannot fire
//      even when the engine ranks that player by name.
//   3. The engine genuinely does not rank kickers, IDPs or anyone with no
//      NFL production arc yet.
//   4. The crosswalk is stale relative to the Sleeper roster.
//
// (1), (3) and (4) are now labelled; (2) is repaired by a name join.

function lookupByName(name, pos) {
  const key = matchKey(name);
  if (!key) return { row: null, ambiguous: false };
  const hits = MT.byNameKey[key] || [];
  if (hits.length === 1) return { row: hits[0], ambiguous: false };
  if (hits.length > 1 && pos) {
    const samePos = hits.filter(r => (r.position || '') === pos);
    if (samePos.length === 1) return { row: samePos[0], ambiguous: false };
  }
  // Two ranked players share this name and position and nothing separates
  // them. Guessing would put another player's rank on this row, which is
  // worse than saying so.
  return { row: null, ambiguous: hits.length > 1 };
}

function resolvePlayer(sid) {
  sid = String(sid);
  const cw = (MT.crosswalk && MT.crosswalk.players && MT.crosswalk.players[sid]) || null;
  const hp = (typeof HL !== 'undefined' && HL && HL.players && HL.players[sid]) || null;
  const clips = (typeof HL !== 'undefined' && HL && HL.clips && HL.clips[sid]) || [];

  const code = sid.toUpperCase();
  const isTeamDef = !cw && !hp && TEAM_CODES.has(code);

  let gsis = MT.sidToGsis[sid] || null;
  let row = gsis ? (MT.byGsis[gsis] || null) : null;
  let rankSource = row ? 'gsis' : null;

  let name = (cw && cw[0]) || (hp && hp.name) || (row && row.name) || '';
  let pos = (cw && cw[1]) || (hp && hp.position) || (row && row.position) || '';
  let team = (cw && cw[2]) || (hp && hp.team) || '';

  if (isTeamDef) {
    name = (TEAM_NAMES[code] || code) + ' defense';
    pos = pos || 'DEF';
    team = team || code;
  }

  let ambiguousName = false;
  if (!row && !isTeamDef && name && MT.rankings) {
    const hit = lookupByName(name, pos);
    if (hit.row) {
      row = hit.row;
      gsis = gsis || row.player_id || null;
      rankSource = 'name';
    } else {
      ambiguousName = hit.ambiguous;
    }
  }

  let rank = null;
  if (row && row.overall_rank != null) {
    rank = row.overall_rank;
  } else if (hp && hp.rank != null) {
    // Last resort: the highlight index carries the rank the build saw.
    rank = hp.rank;
    rankSource = rankSource || 'highlights';
  }

  const p = {
    sid: sid,
    name: name || ('Sleeper #' + sid),
    pos: pos,
    team: team,
    gsis: gsis,
    row: row,
    rank: rank,
    rankSource: rank == null ? null : rankSource,
    clips: clips,
    isTeamDef: isTeamDef,
    ambiguousName: ambiguousName,
    identified: !!(cw || hp || row || isTeamDef)
  };
  const why = unrankedReason(p);
  p.unrankedCode = why[0];
  p.unrankedReason = why[1];
  return p;
}

// ``[code, sentence]`` for a player the model gives no rank, or
// ``[null, null]`` when it does. The sentence is written to be read by
// someone who does not know how the engine works.
function unrankedReason(p) {
  if (p.rank != null) return [null, null];
  if (!MT.rankings) {
    return ['no_artifact',
            'Rankings artifact has not been built yet'];
  }
  if (p.isTeamDef) {
    return ['team_defense',
            'Team defense \u2014 the model ranks individual players only'];
  }
  if (!p.identified) {
    return ['unidentified',
            'Not in the player crosswalk \u2014 roster_index.json is stale ' +
            'relative to Sleeper; re-run the site build'];
  }
  if (KICKER_POS.has(p.pos)) {
    return ['kicker', 'Kicker \u2014 outside the model\u2019s scope'];
  }
  if (IDP_POS.has(p.pos)) {
    return ['idp',
            'Defensive player \u2014 the model projects offensive skill ' +
            'positions only'];
  }
  if (p.ambiguousName) {
    return ['ambiguous',
            'Two ranked players share this name and position \u2014 not ' +
            'guessing which one you own'];
  }
  return ['no_arc',
          'No NFL production arc yet \u2014 rookie, UDFA or practice squad'];
}

function rankCell(p) {
  if (p.rank != null) {
    const star = p.rankSource === 'name' ? '<span class="mt-approx" ' +
      'title="Matched to the model by name; this player carries no gsis id ' +
      'in the crosswalk">~</span>' : '';
    return star + p.rank;
  }
  return '<span class="mt-unranked" title="' + esc(p.unrankedReason) +
         '">unranked</span>';
}

// ---------------------------------------------------------------- render

const POS_ORDER = ['QB', 'RB', 'WR', 'TE', 'K', 'DEF'];

function renderAll(sleeperIds) {
  MT.roster = (sleeperIds || []).map(resolvePlayer);
  renderSummary();
  renderRosterTab();
  renderRankingsTab();
  renderHighlightsTab();
  renderLeagueTab();
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

  const winLbl = (typeof windowLabel === 'function') ? windowLabel() : '';

  const kpis = [
    ['' + r.length, 'Players rostered'],
    ['' + ranked.length, 'Ranked by the model'],
    [best ? '#' + best.rank : '—', best ? 'Best: ' + best.name : 'Best ranked player'],
    [ranks.length ? '#' + median(ranks) : '—', 'Median model rank'],
    ['' + top24, 'Inside the top 24'],
    ['' + withClips.length, winLbl ? 'With film from ' + winLbl : 'With film indexed']
  ];

  // Every rostered player is accounted for here or in the unranked table:
  // ranked + each reason bucket sums to the roster size, by construction.
  const buckets = {};
  r.filter(p => p.rank == null).forEach(p => {
    buckets[p.unrankedCode] = (buckets[p.unrankedCode] || 0) + 1;
  });
  const LABEL = {
    team_defense: 'team defense', kicker: 'kicker', idp: 'defensive player',
    no_arc: 'no production arc yet', unidentified: 'not in the crosswalk',
    ambiguous: 'ambiguous name', no_artifact: 'rankings not built'
  };
  const bucketText = Object.keys(buckets).sort().map(k =>
    buckets[k] + ' ' + (LABEL[k] || k)).join(', ');

  document.getElementById('team-summary').innerHTML =
    '<div class="kpi-row">' + kpis.map(k =>
      '<div class="kpi"><div class="num">' + esc(k[0]) + '</div>' +
      '<div class="label">' + esc(k[1]) + '</div></div>'
    ).join('') + '</div>' +
    '<div class="mt-sub">' + esc(ranked.length) + ' of ' + esc(r.length) +
    ' rostered players carry a model rank' +
    (bucketText ? ' · the other ' + esc(r.length - ranked.length) + ': ' +
                  esc(bucketText) : '') + '. ' +
    esc(totalClips) + ' clip' + (totalClips === 1 ? '' : 's') +
    ' indexed across ' + esc(withClips.length) + ' of them' +
    (winLbl ? ' for ' + esc(winLbl) : '') + '.</div>';
}

function playerCell(p) {
  const label = esc(p.name);
  if (p.row && p.gsis) {
    return '<a href="players/' + esc(slugFor(p.row.name, p.gsis)) + '.html">' + label + '</a>';
  }
  return label;
}

function renderRosterTab() {
  document.getElementById('roster-pane-body').innerHTML =
    rosterTableHtml(MT.roster);
}

// Shared by the Roster tab and by any league team the user opens, so an
// opponent's roster is rendered by exactly the same code -- including the
// unranked reasons -- as the user's own.
function rosterTableHtml(players) {
  if (!players.length) return '<div class="empty">That roster came back empty.</div>';

  const groups = {};
  players.forEach(p => {
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
      '<th>NFL</th><th>Clips</th><th>Why no rank</th></tr></thead><tbody>' +
      list.map(p =>
        '<tr><td class="rank">' + rankCell(p) + '</td>' +
        '<td class="name">' + playerCell(p) + '</td>' +
        '<td>' + posBadge(p.pos) + '</td>' +
        '<td class="team">' + esc(p.team || '—') + '</td>' +
        '<td class="years">' + (p.clips.length || '—') + '</td>' +
        '<td class="mt-why">' + (p.rank == null ? esc(p.unrankedReason) : '') +
        '</td></tr>'
      ).join('') + '</tbody></table>';
  });
  return html;
}

function renderRankingsTab() {
  const box = document.getElementById('rankings-pane-body');
  const ranked = MT.roster.filter(p => p.rank != null)
    .sort((a, b) => a.rank - b.rank);
  const unranked = MT.roster.filter(p => p.rank == null);

  let html = '';
  if (!MT.rankings) {
    html += note('<strong>Model rankings unavailable.</strong> ' +
      '<code>engine_rankings.json</code> has not been generated next to this ' +
      'page yet, so rostered players are listed but cannot be ranked. It is ' +
      'written by the site build on every run.');
  } else if (!ranked.length) {
    html += note('<strong>None of these players are in the model.</strong> ' +
      'The engine ranks offensive skill players it has an NFL production arc ' +
      'for. Every player below says which reason applies to them.');
  }

  const fmt = (v, digits) => (v == null || isNaN(v)) ? '—' : Number(v).toFixed(digits);
  if (ranked.length) {
    html +=
      '<table><thead><tr><th>Rank</th><th>Player</th><th>Pos</th><th>NFL</th>' +
      '<th>Age</th><th>Tier</th><th>Comp tier</th><th class="score">Score</th>' +
      '</tr></thead><tbody>' +
      ranked.map(p => {
        const r = p.row || {};
        return '<tr><td class="rank">' + rankCell(p) + '</td>' +
          '<td class="name">' + playerCell(p) + '</td>' +
          '<td>' + posBadge(p.pos) + '</td>' +
          '<td class="team">' + esc(p.team || '—') + '</td>' +
          '<td class="years">' + (r.age == null ? '—' : esc(r.age)) + '</td>' +
          '<td class="tier">' + (r.tier == null ? '—' : 'T' + esc(r.tier)) + '</td>' +
          '<td class="years">' + esc(r.comp_tier || '—') + '</td>' +
          '<td class="score">' + fmt(r.production_score, 0) + '</td></tr>';
      }).join('') + '</tbody></table>';
  }

  // Unranked players get a table of their own rather than a trailing
  // sentence. Listing them by name only told the user something was
  // missing without telling them whether it was their roster, the model or
  // a broken join -- which is the complaint this page had.
  if (unranked.length) {
    html += '<h3>Unranked <span class="mt-count">' + unranked.length +
      '</span></h3>' +
      '<p class="mt-sub">These players are on your roster and are not ' +
      'dropped from any count on this page. The model gives each of them no ' +
      'rank for the stated reason.</p>' +
      '<table><thead><tr><th>Player</th><th>Pos</th><th>NFL</th>' +
      '<th>Why no rank</th></tr></thead><tbody>' +
      unranked.map(p =>
        '<tr><td class="name">' + playerCell(p) + '</td>' +
        '<td>' + posBadge(p.pos) + '</td>' +
        '<td class="team">' + esc(p.team || '—') + '</td>' +
        '<td class="mt-why">' + esc(p.unrankedReason) + '</td></tr>'
      ).join('') + '</tbody></table>';
  }

  html += '<p class="mt-sub">' + esc(ranked.length) + ' ranked + ' +
    esc(unranked.length) + ' unranked = ' + esc(MT.roster.length) +
    ' rostered. Ranks marked <span class="mt-approx">~</span> were joined ' +
    'to the model by name because the crosswalk carries no gsis id for that ' +
    'player.</p>';

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
  const winLbl = (typeof windowLabel === 'function') ? windowLabel() : '';
  const withClips = MT.roster.filter(p => p.clips.length)
    .sort((a, b) => (a.rank == null ? 1e9 : a.rank) - (b.rank == null ? 1e9 : b.rank));
  if (!withClips.length) {
    box.innerHTML = '<div class="empty">None of your players have indexed ' +
      'clips right now. Enable game recaps above to fall back to team film.</div>';
    return;
  }
  const intro = winLbl
    ? '<p class="mt-sub" style="margin:0 0 14px">Film from <strong>' +
      esc(winLbl) + '</strong>, the most recently completed Thursday-to-Monday ' +
      'slate. Clips marked <em>older</em> come from an earlier window and are ' +
      'kept out of the reel unless you ask for every clip.</p>'
    : '';
  box.innerHTML = intro + withClips.map(p =>
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
            (typeof fmtDay === 'function' ? fmtDay(c.published_at) : ''),
            c.week ? 'Wk ' + esc(c.week) : '',
            (typeof inWindow === 'function' && !inWindow(c)) ? 'older' : '',
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

// ---------------------------------------------------------------- league

function teamLabel(roster, usersById, idx) {
  const u = usersById[roster.owner_id];
  if (!u) return 'Team ' + (idx + 1);
  const meta = u.metadata || {};
  // Sleeper lets a manager name the team separately from their handle.
  // Show the team name when they set one, and always show who owns it.
  return meta.team_name || u.display_name || u.username || ('Team ' + (idx + 1));
}

function ownerLabel(roster, usersById) {
  const u = usersById[roster.owner_id];
  if (!u) return 'no manager';
  return u.display_name || u.username || 'no manager';
}

// Resolve, score and order every roster in the league. Runs off the same
// resolvePlayer as the user's own team, so an opponent's kicker is
// explained the same way theirs is.
function buildLeagueTeams() {
  const L = MT.league;
  if (!L) { MT.teams = []; return; }

  const usersById = {};
  (L.users || []).forEach(u => { usersById[u.user_id] = u; });

  const teams = (L.rosters || []).map((r, i) => {
    const ids = (typeof rosterPlayerIds === 'function')
      ? rosterPlayerIds(r)
      : (r.players || []).map(String);
    const players = ids.map(resolvePlayer);
    return {
      rosterId: r.roster_id,
      ownerId: r.owner_id || null,
      name: teamLabel(r, usersById, i),
      owner: ownerLabel(r, usersById),
      players: players,
      score: scoreTeam(players),
      isMine: !!(L.myRosterId != null && r.roster_id === L.myRosterId) ||
              !!(L.userId && r.owner_id === L.userId)
    };
  });

  // Index the raw core sum against the league leader, so the numbers read
  // as "how close to the strongest roster in this league" rather than as
  // an arbitrary point total.
  const top = teams.reduce((m, t) => Math.max(m, t.score.raw), 0);
  teams.forEach(t => { t.index = top > 0 ? (100 * t.score.raw / top) : 0; });
  teams.sort((a, b) => b.score.raw - a.score.raw);
  teams.forEach((t, i) => { t.place = i + 1; });
  MT.teams = teams;
}

function methodNote() {
  return '<details class="mt-method"><summary>How team strength is ' +
    'calculated</summary><div>' +
    '<p>Each player\u2019s model rank is converted to a value on a curve ' +
    'that falls away exponentially: <code>value = 100 \u00d7 ' +
    'e<sup>\u2212(rank\u22121)/' + TS.decay + '</sup></code>. The ' +
    '#1 player is worth 100, #25 about 62, #50 about 38, #100 about 14 and ' +
    '#200 about 2. Dynasty value really does fall away like that \u2014 ' +
    'averaging plain rank numbers would say a roster of twenty #150s beats ' +
    'one built on two top-five players.</p>' +
    '<p>A team\u2019s score is the sum of its <strong>best ' + TS.core +
    '</strong> players on that curve, not its whole roster, so hoarding ' +
    'marginal bodies does not inflate it. Scores are then indexed so the ' +
    'strongest roster in the league reads 100.</p>' +
    '<p>Players the model does not rank count as zero. That is deliberate ' +
    '\u2014 the model has no opinion on kickers, defenses or players with ' +
    'no NFL production arc yet \u2014 but it means a team carrying a lot of ' +
    'them is being measured on a floor. The <em>unranked</em> column tells ' +
    'you how much of each roster the score is silent about.</p>' +
    '</div></details>';
}

function renderLeagueTab() {
  const box = document.getElementById('league-pane-body');
  if (!box) return;

  if (!MT.league) {
    box.innerHTML = note('<strong>No league loaded.</strong> Find your ' +
      'leagues by Sleeper username, or enter a league ID, and the other ' +
      'teams will be scored here. A hand-typed custom roster has no league ' +
      'attached, so there is nothing to compare it against.');
    return;
  }

  buildLeagueTeams();
  if (!MT.teams.length) {
    box.innerHTML = note('<strong>That league returned no rosters.</strong>');
    return;
  }

  let html = '';
  if (!MT.rankings) {
    html += note('<strong>Model rankings unavailable.</strong> ' +
      '<code>engine_rankings.json</code> has not been generated, so teams ' +
      'are listed with their rosters but every strength score is zero. ' +
      'Re-run the site build.');
  }
  if (!(MT.league.users || []).length) {
    html += note('<strong>Owner names unavailable.</strong> Sleeper\u2019s ' +
      'users endpoint did not respond, so teams are numbered rather than ' +
      'named. The scores are unaffected.');
  }

  html += methodNote();

  const mine = MT.teams.find(t => t.isMine) || null;
  if (mine) {
    html += '<p class="mt-sub mt-standing">Your team, <strong>' +
      esc(mine.name) + '</strong>, is <strong>#' + mine.place + ' of ' +
      MT.teams.length + '</strong> on model strength \u2014 index ' +
      mine.index.toFixed(1) + ' against the league leader.</p>';
  }

  html += '<table class="mt-league"><thead><tr><th>#</th><th>Team</th>' +
    '<th>Manager</th><th class="score">Strength</th><th>Ranked</th>' +
    '<th>Unranked</th><th>Best</th><th>Median of top ' + TS.core + '</th>' +
    '<th></th></tr></thead><tbody>' +
    MT.teams.map(t =>
      '<tr class="' + (t.isMine ? 'mt-mine' : '') + '">' +
      '<td class="rank">' + t.place + '</td>' +
      '<td class="name">' + esc(t.name) + (t.isMine ? ' <em>(you)</em>' : '') + '</td>' +
      '<td class="team">' + esc(t.owner) + '</td>' +
      '<td class="score">' + t.index.toFixed(1) + '</td>' +
      '<td class="years">' + t.score.nRanked + '</td>' +
      '<td class="years">' + t.score.nUnranked + '</td>' +
      '<td class="years">' + (t.score.best ? '#' + t.score.best.rank + ' ' +
        esc(t.score.best.name) : '\u2014') + '</td>' +
      '<td class="years">' + (t.score.medianCoreRank == null ? '\u2014' :
        '#' + t.score.medianCoreRank) + '</td>' +
      '<td><button class="mt-open" data-roster="' + esc(t.rosterId) + '">' +
      (MT.openTeamId === t.rosterId ? 'Hide' : 'View roster') + '</button></td>' +
      '</tr>'
    ).join('') + '</tbody></table>';

  const open = MT.teams.find(t => t.rosterId === MT.openTeamId);
  if (open) {
    html += '<div class="mt-team-detail"><h3>' + esc(open.name) +
      ' <span class="mt-count">' + esc(open.owner) + '</span></h3>' +
      '<p class="mt-sub">Strength index ' + open.index.toFixed(1) +
      ' \u00b7 ' + open.score.nRanked + ' ranked, ' + open.score.nUnranked +
      ' unranked of ' + open.players.length + ' rostered.</p>' +
      rosterTableHtml(open.players) + '</div>';
  }

  box.innerHTML = html;
  box.querySelectorAll('.mt-open').forEach(b => {
    b.addEventListener('click', () => {
      // Roster ids arrive as numbers from Sleeper and as strings from the
      // dataset, so match loosely on purpose.
      const hit = MT.teams.find(t => String(t.rosterId) === String(b.dataset.roster));
      MT.openTeamId = (hit && MT.openTeamId === hit.rosterId)
        ? null : (hit ? hit.rosterId : null);
      renderLeagueTab();
    });
  });
}

// ---------------------------------------------------------------- wiring

window.DFM_ON_ROSTER = function (sleeperIds) {
  MODEL_READY.then(() => renderAll(sleeperIds))
             .catch(e => status('Could not build your team view: ' + e.message, true));
};

// Fires before DFM_ON_ROSTER, from the same Sleeper fetch. Stashing the
// league here means renderAll() can score it without a second round of
// requests, and the tab still renders if the user never picks a team.
window.DFM_ON_LEAGUE = function (league) {
  MT.league = league || null;
  MT.openTeamId = null;
  MODEL_READY.then(() => {
    if (document.getElementById('league-pane-body')) renderLeagueTab();
  }).catch(e => console.warn('league view failed', e));
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
.mt-unranked { color: var(--muted); font-size: 12px; font-style: italic;
  border-bottom: 1px dotted var(--border); cursor: help; }
.mt-approx { color: var(--muted); margin-right: 2px; cursor: help; }
.mt-why { font-size: 12px; color: var(--muted); }
.mt-note { margin: 0 0 16px; }
.mt-standing { font-size: 14px; margin: 14px 0 4px; }
.mt-method { margin: 4px 0 16px; border: 1px solid var(--border);
  border-radius: 10px; background: var(--card); padding: 10px 14px; }
.mt-method summary { cursor: pointer; font-size: 13px; font-weight: 600; }
.mt-method div { font-size: 13px; color: var(--muted); line-height: 1.55; }
.mt-method p { margin: 10px 0 0; }
.mt-league td, .mt-league th { white-space: nowrap; }
.mt-league tr.mt-mine { background: rgba(127,127,127,.10); }
.mt-league tr.mt-mine .name { font-weight: 700; }
.mt-open { font: inherit; font-size: 12px; padding: 4px 10px;
  border-radius: 999px; background: var(--card); border: 1px solid var(--border);
  color: var(--text); cursor: pointer; }
.mt-open:hover { border-color: var(--accent); }
.mt-team-detail { margin-top: 22px; padding-top: 6px;
  border-top: 1px solid var(--border); }
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
player you own, compare your team against the rest of your league, and watch
their film from the most recently completed slate. Rosters are read live from
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
    <button class="view-tab" data-view="view-league">League</button>
    <button class="view-tab" data-view="view-highlights">Highlights</button>
  </div>

  <div class="view-pane" id="view-roster">
    <div id="roster-pane-body"></div>
  </div>

  <div class="view-pane" id="view-rankings" style="display:none">
    <div id="rankings-pane-body"></div>
  </div>

  <div class="view-pane" id="view-league" style="display:none">
    <div id="league-pane-body"></div>
  </div>

  <div class="view-pane" id="view-highlights" style="display:none">
    <div id="clips-pane-body"></div>

    <h3>Roster Reel</h3>
    <p class="mt-sub">Every clip above, queued back to back in model order.</p>

    <div id="step-reel" style="display:none">
      <div id="roster-summary" style="font-size:13px;opacity:.75;margin-top:10px"></div>

      <div class="reel-opts">
        <label><input type="checkbox" id="opt-team"> include game recaps when no cut-up exists</label>
        <label><input type="checkbox" id="opt-all"> every clip per player, including older windows</label>
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
Model ranks come from the same engine that powers the Dynasty Rankings tab.
League standings are computed in your browser from the rosters Sleeper
returns; the method is documented on the League tab.</p>

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
