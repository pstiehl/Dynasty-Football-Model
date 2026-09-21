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

var XLX = {
  corpus: null, loadError: null, expanded: {},
  /* manager_id -> detail document, and -> 'loading'|'ok'|'error'|'absent'.
   * Detail is fetched on demand: the corpus index stays small and a
   * visitor who never opens a manager downloads none of it. */
  detail: {}, detailState: {}, detailError: {}
};

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

/* Points, with thousands separators. Capture values are KTC points and run
 * to four figures, which is unreadable unseparated. */
function xlPts(n, digits) {
  if (n == null || isNaN(n)) return '—';
  return Number(n).toLocaleString(undefined, {
    minimumFractionDigits: digits == null ? 0 : digits,
    maximumFractionDigits: digits == null ? 0 : digits
  });
}

function xlSignedPts(n, digits) {
  if (n == null || isNaN(n)) return '—';
  return (Number(n) >= 0 ? '+' : '') + xlPts(n, digits);
}

/* ------------------------------------------------- detail artifact paths */

/* FNV-1a/32. Must agree byte-for-byte with dynasty.manager_detail.fnv1a32:
 * the browser computes the shard directory to build a fetch URL, so a
 * disagreement is a 404 on a file that exists. tests/js asserts the two
 * implementations agree across the real corpus's manager ids.
 *
 * Math.imul is what keeps the multiply in 32-bit space; a plain `*` loses
 * precision past 2^53 and silently diverges from Python. */
function xlFnv1a32(str) {
  var h = 0x811c9dc5;
  var s = String(str == null ? '' : str);
  for (var i = 0; i < s.length; i++) {
    var cp = s.charCodeAt(i);
    /* UTF-8 bytes, to match Python's .encode('utf-8'). Manager ids are
     * ASCII today; doing this properly costs four lines and removes a
     * latent divergence if that ever stops being true. */
    var bytes;
    if (cp < 0x80) bytes = [cp];
    else if (cp < 0x800) bytes = [0xc0 | (cp >> 6), 0x80 | (cp & 0x3f)];
    else bytes = [0xe0 | (cp >> 12), 0x80 | ((cp >> 6) & 0x3f),
                  0x80 | (cp & 0x3f)];
    for (var b = 0; b < bytes.length; b++) {
      h ^= bytes[b];
      h = Math.imul(h, 0x01000193) >>> 0;
    }
  }
  return h >>> 0;
}

function xlShard(managerId) {
  var v = xlFnv1a32(managerId) & 255;
  return (v < 16 ? '0' : '') + v.toString(16);
}

/* Mirrors dynasty.manager_detail.safe_name. 66 of the corpus's manager ids
 * are synthetic `roster:<league>:<slot>` seats with no Sleeper user, and a
 * colon is not a safe filename or URL path segment. */
function xlSafeName(managerId) {
  var s = String(managerId == null ? '' : managerId).split('~').join('~7E');
  return s.replace(/[^A-Za-z0-9._-]/g, function (ch) {
    var hex = ch.charCodeAt(0).toString(16).toUpperCase();
    return '~' + (hex.length < 2 ? '0' + hex : hex);
  });
}

function xlDetailUrl(managerId) {
  return 'managers/' + xlShard(managerId) + '/' +
         xlSafeName(managerId) + '.json';
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

/* How much is QUEUED, not just how much is done.
 *
 * "12 leagues indexed" and "12 indexed, 488 found and waiting" describe
 * very different systems: one looks finished, the other is visibly still
 * filling up. Discovery is cheap and scoring is expensive, so the queue is
 * normally far larger than the index, and hiding that would make a working
 * crawl look like a stalled one. */
function xlQueueSentence(corpus) {
  var c = (corpus && corpus.crawl) || {};
  var queued = c.n_never_scored || 0;
  if (!queued) return '';
  return xlEsc(queued.toLocaleString()) + ' more dynasty league' +
         (queued === 1 ? '' : 's') + ' discovered and queued — the daily ' +
         'crawl scores a bounded number per run, so the board keeps filling ' +
         'in.';
}

/* The link that gets a league into the corpus from a static page.
 *
 * There is no backend here, so nothing typed into this page can be saved.
 * A pre-filled GitHub issue is the one inbox a static site can offer, and
 * the label that routes it is applied by the issue TEMPLATE rather than a
 * ?labels= parameter, because that parameter silently fails for anyone
 * without write access to the repository. */
function xlSubmitUrl(corpus) {
  var repo = (corpus && corpus.submission && corpus.submission.repo) ||
             'pstiehl/Dynasty-Football-Model';
  return 'https://github.com/' + repo +
         '/issues/new?template=league-submission.yml';
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
  var queue = xlQueueSentence(corpus);
  box.className = 'callout';
  box.style.display = 'block';
  box.innerHTML =
    '<div class="xl-coverage-head">' + xlEsc(xlCoverageSentence(corpus)) +
    '</div>' +
    '<p class="xl-sub" style="margin:6px 0 0 0">' +
    xlEsc(cov.n_league_seasons_scored || 0) + ' league-seasons scored · ' +
    'seasons ' + xlEsc((cov.seasons || []).join(', ') || '—') + ' · ' +
    xlProvenanceSentence(corpus) + '</p>' +
    (queue ? '<p class="xl-sub" style="margin:6px 0 0 0">' + queue + '</p>'
           : '') +
    '<p class="xl-sub" style="margin:6px 0 0 0"><strong>This is not all of ' +
    'Sleeper, and it is not a random sample of it.</strong> Sleeper ' +
    'publishes no way to list leagues, so a corpus can only be built by ' +
    'walking outward from leagues we already know: a league tells us its ' +
    'members, and a member tells us their other leagues. That reaches ' +
    'leagues <em>socially near the ones we started from</em> and nothing ' +
    'else. Treat these ranks as a ranking within this sample, never as a ' +
    'census of Sleeper.</p>' +
    '<p class="xl-sub" style="margin:8px 0 0 0">' +
    '<a href="' + xlEsc(xlSubmitUrl(corpus)) + '" target="_blank" ' +
    'rel="noopener">Add your dynasty league to the corpus →</a> ' +
    'Opens a pre-filled GitHub issue; a daily job checks the id against ' +
    'Sleeper and queues it.</p>';
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
        'style="display:none"><td colspan="8" ' +
        'id="xl-detail-body-' + xlEsc(r.manager_id) + '">' +
        xlRenderBreakdown(r) + '</td></tr>';
  }).join('');
  box.innerHTML =
    '<table class="xl-table"><thead><tr>' +
    '<th>#</th><th>Manager</th><th>Score</th><th>Draft</th><th>Trade</th>' +
    '<th>Waiver</th><th>Leagues</th><th>Percentile</th>' +
    '</tr></thead><tbody>' + body + '</tbody></table>' +
    '<p class="xl-sub">Click a manager to see every indexed league behind ' +
    'the score, and every pick, trade and waiver add that earned it. Score ' +
    'is 100 + 15 × the weighted component average, on the same scale as ' +
    'the per-league Manager Score page.</p>';
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

/* ------------------------------------------------- pick-level evidence */

/* The valuation basis, spelled out rather than abbreviated.
 *
 * PR #63 established the distinction and it is load-bearing here: a
 * point-in-time price is what the asset was actually worth on the day,
 * read from the dated KTC history. A "current" price means no dated value
 * existed for that asset, so it is priced at today's board -- a number it
 * did not have when the decision was made. Labelling every asset is the
 * difference between evidence and a number that merely looks precise. */
function xlBasisLabel(basis, date) {
  if (basis === 'current') {
    return '<span class="xl-basis xl-basis-cur" title="No dated value ' +
      'existed for this asset, so it is priced at today\'s board rather ' +
      'than its value on the day. Treat it as weaker evidence.">current ' +
      'board</span>';
  }
  return '<span class="xl-basis" title="Priced from the dated KTC history ' +
    'as it stood on the transaction date.">point-in-time' +
    (date ? ' · ' + xlEsc(String(date)) : '') + '</span>';
}

/* A pick's label. The audit carries the overall slot and, since this
 * change, the round; pick-in-round is derived only when the league size is
 * known, and omitted rather than guessed when it is not. */
function xlPickLabel(pick, nTeams) {
  var slot = pick.slot;
  if (slot == null) return '—';
  var rd = pick.round;
  if (rd && nTeams) {
    var inRound = slot - (rd - 1) * nTeams;
    if (inRound >= 1 && inRound <= nTeams) {
      return xlEsc(rd + '.' + (inRound < 10 ? '0' : '') + inRound) +
        '<span class="xl-n"> (#' + xlEsc(slot) + ')</span>';
    }
  }
  return (rd ? 'Rd ' + xlEsc(rd) + ' ' : '') +
    '<span class="xl-n">#' + xlEsc(slot) + '</span>';
}

/* Player name as a highlight chip -- the same renderer every other surface
 * on the site uses, so a name here behaves exactly as it does in the
 * rankings table. Called directly rather than behind a typeof guard: the
 * shared renderer is emitted before the page body (PR #71), and
 * tests/js/crossleague_script_order_tests.mjs executes the real generated
 * page to prove it. A guard here would hide a regression of that bug
 * instead of surfacing it. */
function xlPlayerChip(name, pos, sleeperId) {
  if (!name) return '—';
  return DFMHL.chip(name, { sid: sleeperId || null, pos: pos || null });
}

/* The draft component for one league, shown so the arithmetic closes.
 *
 * Every row ends in a surplus; the surpluses sum to the total; the total
 * over n is the mean; the mean shrunk is compared against the league's own
 * pool to give the z the board published. A reader can add up the column
 * and arrive at the score. */
function xlRenderDraftEvidence(league) {
  var det = league.detail || {};
  var comp = (det.components || {}).draft || {};
  var picks = det.picks || [];
  if (!comp.n) {
    return '<p class="xl-sub">No scored picks in this league.</p>';
  }
  if (!picks.length) {
    return '<p class="xl-warn">This league contributed ' + xlEsc(comp.n) +
      ' scored pick(s) to the draft score, but the pick-level record is ' +
      'not in this build. The score above still counts — the evidence for ' +
      'it is missing, not zero.</p>';
  }
  var rows = picks.map(function (p) {
    return '<tr>' +
      '<td class="xl-n">' + xlPickLabel(p, league.n_teams) + '</td>' +
      '<td>' + xlPlayerChip(p.player, p.pos, p.sleeper_id) + '</td>' +
      '<td class="xl-n">' + xlEsc(p.pos || '—') + '</td>' +
      '<td class="xl-n">' + xlBasisLabel(p.basis, p.value_at_date) + '</td>' +
      '<td class="xl-n">' + xlPts(p.value_at) + '</td>' +
      '<td class="xl-n">' + xlPts(p.peak) +
        (p.peak_date ? '<span class="xl-n"> · ' + xlEsc(String(p.peak_date).slice(0, 10)) + '</span>' : '') +
      '</td>' +
      '<td class="xl-n">' + xlPts(p.capture) + '</td>' +
      '<td class="xl-n">' + xlPts(p.expected) + '</td>' +
      '<td class="' + xlCls(p.surplus) + '">' + xlSignedPts(p.surplus, 1) +
      '</td>' +
      '</tr>';
  }).join('');

  var pool = comp.pool || {};
  return '<table class="xl-table xl-inner xl-picks"><thead><tr>' +
    '<th>Pick</th><th>Player</th><th>Pos</th><th>Priced</th>' +
    '<th>Value at pick</th><th>Peak after</th><th>Captured</th>' +
    '<th>Slot baseline</th><th>Surplus</th>' +
    '</tr></thead><tbody>' + rows + '</tbody>' +
    '<tfoot><tr>' +
    '<td colspan="8"><strong>Total surplus over ' + xlEsc(comp.n) +
      ' scored pick(s)</strong></td>' +
    '<td class="' + xlCls(comp.total) + '"><strong>' +
      xlSignedPts(comp.total, 1) + '</strong></td>' +
    '</tr></tfoot></table>' +
    '<p class="xl-sub xl-math">' +
    '<strong>How that becomes a z-score.</strong> ' +
    'Mean surplus per pick = ' + xlSignedPts(comp.total, 1) + ' ÷ ' +
      xlEsc(comp.n) + ' = <strong>' + xlSignedPts(comp.mean, 2) +
      '</strong>. Shrunk toward neutral by n/(n+k) = ' + xlEsc(comp.n) +
      '/(' + xlEsc(comp.n) + '+' + xlEsc(comp.k) + '): <strong>' +
      xlSignedPts(comp.shrunk, 2) + '</strong>. ' +
    (pool.n_managers >= 2 && pool.sd > 0
      ? 'Against this league\'s own drafters (' + xlEsc(pool.n_managers) +
        ' with picks, mean ' + xlSignedPts(pool.mean, 2) + ', sd ' +
        xlPts(pool.sd, 2) + '): z = (' + xlSignedPts(comp.shrunk, 2) + ' − ' +
        xlSignedPts(pool.mean, 2) + ') ÷ ' + xlPts(pool.sd, 2) +
        ' = <strong class="' + xlCls(comp.z) + '">' + xlSigned(comp.z, 3) +
        '</strong>.'
      : 'Fewer than two drafters in this league had scored picks, so there ' +
        'is no distribution to score against and z stays 0.') +
    '</p>';
}

/* Trade and waiver detail. Included because it is cheap once the artifact
 * is fetched, and deliberately more compact than the draft block: the
 * owner's ask was the draft score. */
function xlRenderTradeEvidence(league) {
  var det = league.detail || {};
  var comp = (det.components || {}).trade || {};
  var trades = det.trades || [];
  var unscored = det.trades_unscored || [];
  var out = '';
  if (comp.n && trades.length) {
    var rows = trades.map(function (t) {
      function side(list) {
        if (!list || !list.length) return '—';
        return list.map(function (a) {
          return a.kind === 'pick'
            ? xlEsc(a.label || 'pick')
            : xlPlayerChip(a.label, a.pos, a.sleeper_id);
        }).join(', ');
      }
      return '<tr>' +
        '<td class="xl-n">' + xlEsc(String(t.date || '—').slice(0, 10)) + '</td>' +
        '<td>' + side(t.received) + '</td>' +
        '<td>' + side(t.given) + '</td>' +
        '<td class="xl-n">' + xlBasisLabel(t.basis, t.date) + '</td>' +
        '<td class="xl-n">' + xlPts(t.capture_received) + '</td>' +
        '<td class="xl-n">' + xlPts(t.capture_given) + '</td>' +
        '<td class="' + xlCls(t.surplus) + '">' + xlSignedPts(t.surplus, 1) +
        '</td></tr>';
    }).join('');
    out += '<table class="xl-table xl-inner"><thead><tr>' +
      '<th>Date</th><th>Received</th><th>Given</th><th>Priced</th>' +
      '<th>Capture in</th><th>Capture out</th><th>Surplus</th>' +
      '</tr></thead><tbody>' + rows + '</tbody>' +
      '<tfoot><tr><td colspan="6"><strong>Total over ' + xlEsc(comp.n) +
      ' scored trade(s)</strong></td><td class="' + xlCls(comp.total) +
      '"><strong>' + xlSignedPts(comp.total, 1) + '</strong></td>' +
      '</tr></tfoot></table>';
  } else if (comp.n) {
    out += '<p class="xl-warn">' + xlEsc(comp.n) + ' scored trade(s) ' +
      'contributed here, but the per-trade record is not in this build.</p>';
  } else {
    out += '<p class="xl-sub">No scored trades in this league.</p>';
  }
  if (unscored.length) {
    out += '<p class="xl-sub">' + xlEsc(unscored.length) + ' further ' +
      'trade(s) are shown nowhere in the total: they contained an asset ' +
      'with no published value, or the legs did not balance, so scoring ' +
      'them would invent a result. They are excluded from the score, not ' +
      'counted as zero.</p>';
  }
  return out;
}

function xlRenderWaiverEvidence(league) {
  var det = league.detail || {};
  var comp = (det.components || {}).waiver || {};
  var adds = det.waivers || [];
  if (!comp.n) return '<p class="xl-sub">No scored waiver adds here.</p>';
  if (!adds.length) {
    return '<p class="xl-warn">' + xlEsc(comp.n) + ' scored add(s) ' +
      'contributed here, but the per-add record is not in this build.</p>';
  }
  var rows = adds.map(function (w) {
    return '<tr>' +
      '<td class="xl-n">' + xlEsc(String(w.date || '—').slice(0, 10)) + '</td>' +
      '<td>' + xlPlayerChip(w.player, w.pos, w.sleeper_id) + '</td>' +
      '<td class="xl-n">' + xlEsc(w.pos || '—') + '</td>' +
      '<td class="xl-n">' + xlBasisLabel(w.basis, w.value_at_date) + '</td>' +
      '<td class="xl-n">' + (w.faab == null ? '—' : '$' + xlEsc(w.faab)) + '</td>' +
      '<td class="xl-n">' + xlPts(w.value_at) + '</td>' +
      '<td class="' + xlCls(w.surplus) + '">' + xlSignedPts(w.surplus, 1) +
      '</td></tr>';
  }).join('');
  return '<table class="xl-table xl-inner"><thead><tr>' +
    '<th>Date</th><th>Player</th><th>Pos</th><th>Priced</th><th>FAAB</th>' +
    '<th>Value at add</th><th>Surplus</th>' +
    '</tr></thead><tbody>' + rows + '</tbody>' +
    '<tfoot><tr><td colspan="6"><strong>Total over ' + xlEsc(comp.n) +
    ' scored add(s)</strong></td><td class="' + xlCls(comp.total) +
    '"><strong>' + xlSignedPts(comp.total, 1) + '</strong></td>' +
    '</tr></tfoot></table>';
}

/* The cross-league roll-up: how the per-league z-scores above become the
 * one number on the board. */
function xlRenderRollup(doc) {
  var comps = doc.components || {};
  var order = ['draft', 'trade', 'waiver'];
  var rows = order.map(function (c) {
    var x = comps[c] || {};
    var contrib = (Number(x.weight) || 0) * (Number(x.z) || 0);
    return '<tr>' +
      '<td style="text-transform:capitalize">' + xlEsc(c) + '</td>' +
      '<td class="xl-n">' + xlEsc(x.n || 0) + '</td>' +
      '<td class="' + xlCls(x.zbar) + '">' + xlSigned(x.zbar, 3) + '</td>' +
      '<td class="xl-n">' + xlEsc(x.n || 0) + '/(' + xlEsc(x.n || 0) + '+' +
        xlEsc((doc.shrink_k || {})[c]) + ')</td>' +
      '<td class="' + xlCls(x.z) + '">' + xlSigned(x.z, 3) + '</td>' +
      '<td class="xl-n">' + xlNum(x.weight, 2) + '</td>' +
      '<td class="' + xlCls(contrib) + '">' + xlSigned(contrib, 3) + '</td>' +
      '</tr>';
  }).join('');
  return '<h4>From the leagues above to the score on the board</h4>' +
    '<table class="xl-table xl-inner"><thead><tr>' +
    '<th>Component</th><th>Events</th><th>Evidence-weighted z̄</th>' +
    '<th>Shrink</th><th>Z</th><th>Weight</th><th>Contribution</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table>' +
    '<p class="xl-sub xl-math">Composite = sum of contributions = ' +
    '<strong>' + xlSigned(doc.composite, 3) + '</strong>. ' +
    'Score = 100 + 15 × ' + xlSigned(doc.composite, 3) + ' = ' +
    '<strong>' + xlNum(doc.cross_index, 1) + '</strong>.</p>';
}

/* The whole panel for one manager. */
function xlRenderDetailPanel(row, doc, state, err) {
  var head = xlRenderBreakdown(row);
  if (state === 'loading') {
    return head + '<p class="xl-sub">Loading pick-level detail…</p>';
  }
  if (state === 'absent') {
    return head +
      '<p class="xl-warn"><strong>No pick-level detail published for this ' +
      'manager.</strong> The per-league scores above are real and still ' +
      'count; what is missing is the transaction-by-transaction record ' +
      'behind them. The daily build publishes detail for the leagues it ' +
      'scored most recently, so this usually fills in on a later run.</p>';
  }
  if (state === 'error') {
    return head +
      '<p class="xl-warn"><strong>Could not load the detail for this ' +
      'manager</strong>' + (err ? ' (' + xlEsc(err) + ')' : '') + '. This is ' +
      'a failure to fetch the evidence, not an absence of activity — the ' +
      'scores above are unaffected.</p>';
  }
  if (!doc) return head;

  var cov = doc.coverage || {};
  var banner = '';
  if (!cov.n_leagues_with_evidence) {
    banner = '<p class="xl-warn"><strong>No transaction-level evidence is ' +
      'retained for any of this manager\'s leagues yet.</strong> Their ' +
      'score is unaffected; only the explanation below is missing.</p>';
  } else if (!cov.complete) {
    banner = '<p class="xl-warn">Pick-level evidence is available for ' +
      xlEsc(cov.n_leagues_with_evidence) + ' of ' + xlEsc(cov.n_leagues) +
      ' indexed league(s). The rest are marked below; their scores still ' +
      'count toward the totals.</p>';
  }

  var flags = (doc.flags || []).length
    ? '<p class="xl-sub xl-flags">' + xlEsc(doc.flags.join(' · ')) + '</p>'
    : '';

  var leagues = (doc.leagues || []).map(function (L) {
    var title = '<h4>' + xlEsc(L.name || L.league_id) + ' · ' +
      xlEsc(L.season || '—') + ' · ' + xlEsc(L.n_teams || '?') + ' teams' +
      (L.index != null ? ' · league score ' + xlNum(L.index, 1) : '') +
      (L.rank != null ? ' (rank ' + xlEsc(L.rank) + ')' : '') + '</h4>';
    if (L.evidence !== 'complete') {
      return title + '<p class="xl-warn">' +
        xlEsc(L.evidence_why ||
          'The pick-level audit for this league is not in this build.') +
        '</p>';
    }
    return title +
      '<h5 class="xl-h5">Draft — what earned the draft score</h5>' +
      xlRenderDraftEvidence(L) +
      '<h5 class="xl-h5">Trades</h5>' + xlRenderTradeEvidence(L) +
      '<h5 class="xl-h5">Waiver adds</h5>' + xlRenderWaiverEvidence(L);
  }).join('');

  return head + '<div class="xl-evidence">' + banner + flags +
    xlRenderRollup(doc) + leagues + '</div>';
}

/* Re-render just one manager's open panel. */
function xlPaintDetail(managerId) {
  var cell = xlEl('xl-detail-body-' + managerId);
  if (!cell) return;
  var row = null;
  var all = ((XLX.corpus || {}).leaderboard) || [];
  for (var i = 0; i < all.length; i++) {
    if (String(all[i].manager_id) === String(managerId)) { row = all[i]; break; }
  }
  if (!row) return;
  cell.innerHTML = xlRenderDetailPanel(
    row, XLX.detail[managerId], XLX.detailState[managerId],
    XLX.detailError[managerId]);
}

/* Fetch one manager's detail artifact, once. */
function xlLoadDetail(managerId) {
  if (!managerId) return Promise.resolve(null);
  var st = XLX.detailState[managerId];
  if (st === 'ok' || st === 'loading' || st === 'absent' || st === 'error') {
    return Promise.resolve(XLX.detail[managerId] || null);
  }
  XLX.detailState[managerId] = 'loading';
  xlPaintDetail(managerId);
  return fetch(xlDetailUrl(managerId)).then(function (r) {
    /* A 404 is the expected shape of "not published yet", and must read as
     * that rather than as an error the visitor should worry about. */
    if (r.status === 404) {
      XLX.detailState[managerId] = 'absent';
      return null;
    }
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function (doc) {
    if (doc) {
      XLX.detail[managerId] = doc;
      XLX.detailState[managerId] = 'ok';
    }
    xlPaintDetail(managerId);
    return doc;
  }).catch(function (e) {
    XLX.detailState[managerId] = 'error';
    XLX.detailError[managerId] = String((e && e.message) || e);
    xlPaintDetail(managerId);
    return null;
  });
}

function xlToggle(managerId) {
  var row = xlEl('xl-detail-' + managerId);
  if (!row) return;
  var open = XLX.expanded[managerId];
  XLX.expanded[managerId] = !open;
  row.style.display = open ? 'none' : 'table-row';
  /* Fetch on open, never on page load: the corpus index stays small and a
   * visitor who opens nobody downloads no detail at all. */
  if (!open) {
    xlPaintDetail(managerId);
    xlLoadDetail(managerId);
  }
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
    xlLoad: xlLoad,
    xlFnv1a32: xlFnv1a32,
    xlShard: xlShard,
    xlSafeName: xlSafeName,
    xlDetailUrl: xlDetailUrl,
    xlBasisLabel: xlBasisLabel,
    xlPickLabel: xlPickLabel,
    xlRenderDraftEvidence: xlRenderDraftEvidence,
    xlRenderTradeEvidence: xlRenderTradeEvidence,
    xlRenderWaiverEvidence: xlRenderWaiverEvidence,
    xlRenderRollup: xlRenderRollup,
    xlRenderDetailPanel: xlRenderDetailPanel,
    xlLoadDetail: xlLoadDetail,
    xlPaintDetail: xlPaintDetail
  };
}
"""
