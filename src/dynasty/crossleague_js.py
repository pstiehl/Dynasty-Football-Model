"""Client JS for the cross-league Manager Score board.

Unlike the Manager Score page, this one does **no** scoring and **no**
crawling in the browser. It is a pure renderer over
``crossleague_corpus.json``, a precomputed, committed artifact.

That split is forced, not stylistic. Building the corpus is a bounded crawl of
a rate-limited third-party API — hundreds of calls. Doing that per visitor, on
page load, from a static site with no server to cache behind, would be abusive
to Sleeper and slow for the user. So the crawl runs once a day in CI and the
browser just reads the result.

Everything below is a function of its arguments so the renderers can be
asserted in node with a stub DOM, exactly as the Manager Score page's are.
"""
from __future__ import annotations

CROSSLEAGUE_JS = r"""
/* ============================================================
 * Cross-league Manager Score — renderer.
 * Reads crossleague_corpus.json. No scoring here: the corpus is
 * built by scripts/crawl_cross_league.py, which scores each
 * league with the shipped Manager Score JS so this board and the
 * per-league page are the same metric.
 * ============================================================ */

var XLX = { corpus: null, loadError: null, expanded: {} };

function xlEsc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function xlEl(id) { return document.getElementById(id); }

function xlNum(n, digits) {
  if (n == null || isNaN(n)) return '—';
  return Number(n).toFixed(digits == null ? 0 : digits);
}

function xlSigned(n, digits) {
  if (n == null || isNaN(n)) return '—';
  var v = Number(n);
  return (v >= 0 ? '+' : '') + v.toFixed(digits == null ? 2 : digits);
}

function xlCls(n) {
  if (n == null || isNaN(n)) return '';
  return Number(n) > 0 ? 'xl-pos' : (Number(n) < 0 ? 'xl-neg' : '');
}

/* ------------------------------------------------------------- coverage */

/* The exact claim the owner asked for, and the only claim we are entitled
 * to make: what we indexed, never what exists. Built from the artifact's
 * own counts so the sentence cannot drift from the data. */
function xlCoverageSentence(corpus) {
  var c = (corpus && corpus.coverage) || {};
  var m = c.n_managers || 0, l = c.n_leagues || 0;
  return 'ranked against ' + m.toLocaleString() + ' manager' +
         (m === 1 ? '' : 's') + ' across ' + l.toLocaleString() +
         ' dynasty league' + (l === 1 ? '' : 's') + " we've indexed";
}

/* Persistence is the difference between a growing index and a snapshot.
 * When the daily job cannot commit the corpus, the feature still works --
 * it just describes this run. Saying so is the whole point. */
function xlProvenanceSentence(corpus) {
  if (!corpus) return '';
  if (!corpus.persisted) {
    return 'Indexed in this build only — the corpus is not being retained ' +
           'between runs yet, so it does not grow day to day.';
  }
  var since = String(corpus.first_indexed_at || '').slice(0, 10);
  return 'Indexed since ' + xlEsc(since) + ' over ' +
         xlEsc(corpus.n_runs || 1) + ' daily run(s); the corpus grows as ' +
         'each run reaches further into the league graph.';
}

function xlRenderCoverage(corpus) {
  var box = xlEl('xl-coverage');
  if (!box) return;
  if (!corpus) {
    box.className = 'callout callout-warn';
    box.style.display = 'block';
    box.innerHTML = '<strong>Corpus not available.</strong> This page needs ' +
      '<code>crossleague_corpus.json</code> next to it, written by ' +
      '<code>scripts/crawl_cross_league.py</code> during the daily build. ' +
      'Until then there is nothing to rank' +
      (XLX.loadError ? ' (' + xlEsc(XLX.loadError) + ')' : '') + '.';
    return;
  }
  var cov = corpus.coverage || {};
  box.className = 'callout';
  box.style.display = 'block';
  box.innerHTML =
    '<div class="xl-coverage-head">' + xlEsc(xlCoverageSentence(corpus)) +
    '</div>' +
    '<p class="xl-sub" style="margin:6px 0 0 0">' +
    xlEsc(cov.n_league_seasons_scored || 0) + ' league-seasons scored · ' +
    'seasons ' + xlEsc((cov.seasons || []).join(', ') || '—') + ' · ' +
    xlProvenanceSentence(corpus) + '</p>' +
    '<p class="xl-sub" style="margin:6px 0 0 0"><strong>This is not all of ' +
    'Sleeper.</strong> Sleeper publishes no way to list leagues, so a corpus ' +
    'can only be built by walking from known leagues outward. What you see ' +
    'here is a deliberately bounded sample, not a census.</p>';
}

/* ---------------------------------------------------------- draft board */

/* The owner's explicit question: "who are the best managers at drafting
 * across every dynasty football league". It gets the prominent slot. */
function xlRenderDraftBoard(corpus) {
  var box = xlEl('xl-draft-board');
  if (!box) return;
  var board = (corpus && corpus.draft_board) || [];
  var gate = ((corpus && corpus.components) || {}).min_draft_picks_for_board;
  if (!board.length) {
    box.innerHTML = '<p class="xl-sub">No manager in the corpus yet has the ' +
      xlEsc(gate == null ? 6 : gate) + '+ scored picks needed to rank on ' +
      'drafting.</p>';
    return;
  }
  var rows = board.map(function (r) {
    return '<tr class="xl-row" data-manager="' + xlEsc(r.manager_id) + '">' +
      '<td class="xl-rank">' + xlEsc(r.rank) + '</td>' +
      '<td><span class="xl-name">' + xlEsc(r.display_name) + '</span></td>' +
      '<td class="' + xlCls(r.draft_z) + '">' + xlSigned(r.draft_z, 3) + '</td>' +
      '<td class="xl-n">' + xlEsc(r.n_picks) + '</td>' +
      '<td class="xl-n">' + xlEsc(r.n_leagues) + '</td>' +
      '<td class="xl-n">' + xlNum(r.percentile, 1) + '%</td>' +
      '</tr>';
  }).join('');
  box.innerHTML =
    '<table class="xl-table"><thead><tr>' +
    '<th>#</th><th>Manager</th><th>Draft score</th><th>Picks</th>' +
    '<th>Leagues</th><th>Percentile</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table>' +
    '<p class="xl-sub">Draft score is the evidence-weighted average of this ' +
    "manager's within-league draft z-score, shrunk toward neutral by total " +
    'pick count. A manager needs ' + xlEsc(gate == null ? 6 : gate) +
    '+ scored picks to appear, so one lucky draft cannot top the board.</p>';
}

/* ------------------------------------------------------- overall board */

function xlRenderLeaderboard(corpus) {
  var box = xlEl('xl-leaderboard');
  if (!box) return;
  var rows = (corpus && corpus.leaderboard) || [];
  if (!rows.length) {
    box.innerHTML = '<p class="xl-sub">Nothing indexed yet.</p>';
    return;
  }
  var body = rows.map(function (r) {
    var c = r.components || {};
    return '<tr class="xl-row" data-manager="' + xlEsc(r.manager_id) + '">' +
      '<td class="xl-rank">' + xlEsc(r.rank) + '</td>' +
      '<td><span class="xl-name">' + xlEsc(r.display_name) + '</span>' +
        (r.flags && r.flags.length
          ? '<div class="xl-flags">' + xlEsc(r.flags.join(' · ')) + '</div>'
          : '') +
      '</td>' +
      '<td><strong>' + xlNum(r.cross_index, 1) + '</strong></td>' +
      '<td class="' + xlCls((c.draft || {}).z) + '">' +
        xlSigned((c.draft || {}).z, 2) + '</td>' +
      '<td class="' + xlCls((c.trade || {}).z) + '">' +
        xlSigned((c.trade || {}).z, 2) + '</td>' +
      '<td class="' + xlCls((c.waiver || {}).z) + '">' +
        xlSigned((c.waiver || {}).z, 2) + '</td>' +
      '<td class="xl-n">' + xlEsc(r.n_leagues) + '</td>' +
      '<td class="xl-n">' + xlNum(r.percentile, 1) + '%</td>' +
      '</tr>' +
      '<tr class="xl-detail" id="xl-detail-' + xlEsc(r.manager_id) + '" ' +
        'style="display:none"><td colspan="8">' +
        xlRenderBreakdown(r) + '</td></tr>';
  }).join('');
  box.innerHTML =
    '<table class="xl-table"><thead><tr>' +
    '<th>#</th><th>Manager</th><th>Score</th><th>Draft</th><th>Trade</th>' +
    '<th>Waiver</th><th>Leagues</th><th>Percentile</th>' +
    '</tr></thead><tbody>' + body + '</tbody></table>' +
    '<p class="xl-sub">Click a manager to see every indexed league behind ' +
    'the score. Score is 100 + 15 × the weighted component average, on the ' +
    'same scale as the per-league Manager Score page.</p>';
}

/* A score nobody can take apart is a score nobody should trust: every
 * indexed league a manager appears in, with its own contribution. */
function xlRenderBreakdown(row) {
  var leagues = (row && row.leagues) || [];
  if (!leagues.length) return '<p class="xl-sub">No per-league detail.</p>';
  var rows = leagues.map(function (L) {
    var c = L.components || {};
    function cell(k) {
      var x = c[k] || {};
      return '<td class="' + xlCls(x.z) + '">' + xlSigned(x.z, 2) +
             '<span class="xl-n"> (' + xlEsc(x.n || 0) + ')</span></td>';
    }
    return '<tr>' +
      '<td>' + xlEsc(L.name || L.league_id) + '</td>' +
      '<td class="xl-n">' + xlEsc(L.season || '—') + '</td>' +
      '<td class="xl-n">' + xlEsc(L.n_teams || '—') + '</td>' +
      '<td>' + xlNum(L.index, 1) + '</td>' +
      '<td class="xl-n">' + xlEsc(L.rank == null ? '—' : L.rank) + '</td>' +
      cell('draft') + cell('trade') + cell('waiver') +
      '</tr>';
  }).join('');
  var c = row.components || {};
  return '<div class="xl-breakdown">' +
    '<h4 style="margin-top:0">Per-league breakdown</h4>' +
    '<table class="xl-table xl-inner"><thead><tr>' +
    '<th>League</th><th>Season</th><th>Teams</th><th>League score</th>' +
    '<th>League rank</th><th>Draft z (n)</th><th>Trade z (n)</th>' +
    '<th>Waiver z (n)</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table>' +
    '<p class="xl-sub">Totals: ' +
    xlEsc((c.draft || {}).n || 0) + ' scored picks, ' +
    xlEsc((c.trade || {}).n || 0) + ' scored trades, ' +
    xlEsc((c.waiver || {}).n || 0) + ' scored adds. Each component is the ' +
    'evidence-weighted mean of the z-scores above, then shrunk toward ' +
    'neutral by the totals.</p>' +
    '</div>';
}

function xlToggle(managerId) {
  var row = xlEl('xl-detail-' + managerId);
  if (!row) return;
  var open = XLX.expanded[managerId];
  XLX.expanded[managerId] = !open;
  row.style.display = open ? 'none' : 'table-row';
}

/* ------------------------------------------------------------- leagues */

function xlRenderLeagues(corpus) {
  var box = xlEl('xl-leagues');
  if (!box) return;
  var leagues = (corpus && corpus.leagues) || [];
  if (!leagues.length) { box.innerHTML = ''; return; }
  var rows = leagues.map(function (L) {
    return '<tr>' +
      '<td>' + xlEsc(L.name || L.league_id) + '</td>' +
      '<td class="xl-n">' + xlEsc(L.season || '—') + '</td>' +
      '<td class="xl-n">' + xlEsc(L.n_teams || '—') + '</td>' +
      '<td class="xl-n">' + xlEsc(L.n_seasons_scored || 1) + '</td>' +
      '<td class="xl-n">' + xlEsc(L.n_managers || 0) + '</td>' +
      '<td class="xl-n">' + xlEsc(L.scored_picks == null ? '—' : L.scored_picks) + '</td>' +
      '<td class="xl-n">' + xlEsc(L.hop == null ? '—' : L.hop) + '</td>' +
      '</tr>';
  }).join('');
  box.innerHTML =
    '<table class="xl-table"><thead><tr>' +
    '<th>League</th><th>Season</th><th>Teams</th><th>Seasons scored</th>' +
    '<th>Managers</th><th>Scored picks</th><th>Hops from seed</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table>' +
    '<p class="xl-sub">"Hops from seed" is how far into the league graph we ' +
    'had to walk to find this league. Dynasty leagues are scored across ' +
    'their whole history chain, which is why one league can carry several ' +
    'seasons.</p>';
}

/* ----------------------------------------------------------------- init */

function xlRenderAll(corpus) {
  xlRenderCoverage(corpus);
  xlRenderDraftBoard(corpus);
  xlRenderLeaderboard(corpus);
  xlRenderLeagues(corpus);

  var results = xlEl('xl-results');
  if (results) results.style.display = corpus ? 'block' : 'none';

  var tbl = xlEl('xl-leaderboard');
  if (tbl && tbl.querySelectorAll) {
    var rows = tbl.querySelectorAll('tr.xl-row');
    for (var i = 0; i < rows.length; i++) {
      (function (tr) {
        tr.addEventListener('click', function () {
          xlToggle(tr.dataset ? tr.dataset.manager : null);
        });
      })(rows[i]);
    }
  }
}

function xlLoad() {
  return fetch('crossleague_corpus.json').then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function (c) {
    XLX.corpus = c;
    xlRenderAll(c);
    return c;
  }).catch(function (e) {
    XLX.loadError = String((e && e.message) || e);
    XLX.corpus = null;
    xlRenderAll(null);
    return null;
  });
}

if (typeof document !== 'undefined' && document.addEventListener) {
  document.addEventListener('DOMContentLoaded', xlLoad);
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    XLX: XLX,
    xlCoverageSentence: xlCoverageSentence,
    xlProvenanceSentence: xlProvenanceSentence,
    xlRenderCoverage: xlRenderCoverage,
    xlRenderDraftBoard: xlRenderDraftBoard,
    xlRenderLeaderboard: xlRenderLeaderboard,
    xlRenderBreakdown: xlRenderBreakdown,
    xlRenderLeagues: xlRenderLeagues,
    xlRenderAll: xlRenderAll,
    xlToggle: xlToggle,
    xlLoad: xlLoad
  };
}
"""
