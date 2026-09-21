/* Mini-DOM + stub-Sleeper harness for the Manager Score page JS.
 *
 * Run by tests/test_managerscore.py, which concatenates
 * MANAGERSCORE_CORE_JS + MANAGERSCORE_UI_JS to a temp file and passes the
 * path as argv[2]:
 *
 *   node tests/js/managerscore_dom_tests.js /tmp/ms_page.js
 *
 * Why this exists: no browser is available in the build environment, and the
 * UI layer contains the part that cannot be reasoned about on paper -- the
 * mapping from Sleeper's actual JSON shapes (adds/drops keyed by player id to
 * roster id, per-week transaction pages, previous_league_id chaining,
 * ms-epoch timestamps) into the scoring core's input, and the join from
 * sleeper_id through ktc_id into the dated value series.
 *
 * It proves the wiring is self-consistent. It does NOT prove Sleeper really
 * returns these shapes -- that needs a live call.
 *
 * Exits 0 on success, 1 with a report on first failure.
 */
'use strict';

const jsPath = process.argv[2];
if (!jsPath) {
  console.error('usage: node managerscore_dom_tests.js <page-js-path>');
  process.exit(2);
}

let failures = 0;
let checks = 0;
function ok(cond, label, detail) {
  checks++;
  if (!cond) {
    failures++;
    console.error('FAIL: ' + label + (detail ? '  [' + detail + ']' : ''));
  }
}
function near(a, b, label, eps) {
  ok(Math.abs(a - b) < (eps == null ? 1e-9 : eps), label, a + ' vs ' + b);
}

/* ------------------------------------------------------------- mini DOM */

function makeEl(id) {
  return {
    id: id, innerHTML: '', className: '', checked: true, value: '',
    style: {}, _listeners: {},
    addEventListener: function (ev, fn) { this._listeners[ev] = fn; },
    scrollIntoView: function () { this._scrolled = true; },
    getAttribute: function (k) { return this['_attr_' + k] || null; },
    setAttribute: function (k, v) { this['_attr_' + k] = v; }
  };
}
const els = {};
global.document = {
  getElementById: function (id) {
    if (!els[id]) els[id] = makeEl(id);
    return els[id];
  },
  /* The stub does not parse innerHTML, so selector queries return nothing.
   * Click wiring is not covered here; msRenderAudit is driven directly. */
  querySelectorAll: function () { return []; },
  addEventListener: function (ev, fn) { this['_on_' + ev] = fn; }
};

/* ------------------------------------------------------- stub artifacts */

/* sleeper_id -> [current_value, name, position, ktc_id] */
const VALUES = {
  schema: 'managerscore.values.v1',
  available: true,
  ktc: { captured_at: '2026-06-01T11:00:00+00:00', n_players: 3, n_mapped: 3, floor: 500 },
  crosswalk: { available: true, n_mapped: 3 },
  history: { available: true, count: 5, dates: [], earliest: '2025-01-01', latest: '2026-12-01' },
  by_sleeper: {
    '100': [6000, 'Riser Back', 'RB', 1],
    '200': [6000, 'Fading WR', 'WR', 2],
    '300': [1000, 'Wire Gem', 'TE', 3],
    /* Filler RBs exist only so the realized lens has a positional
     * population to take a median against. A single RB in the league would
     * make every PAR trivially zero and prove nothing. */
    '101': [500, 'Filler RB One', 'RB', 101],
    '201': [500, 'Filler RB Two', 'RB', 201],
    '301': [500, 'Filler RB Three', 'RB', 301]
  },
  picks: {},
  notes: []
};

/* Five dated boards. Player 1 rises throughout, player 2 only declines,
 * player 3 spikes late. These are the numbers every capture assertion
 * below is derived from. */
const SERIES = {
  schema: 'ktc.series.v1',
  dates: ['2025-01-01', '2025-06-01', '2026-01-01', '2026-06-01', '2026-12-01'],
  floors: [400, 400, 450, 500, 500],
  sf: {
    '1': [1000, 2000, 5000, 6000, 9000],
    '2': [9000, 8000, 7000, 6000, 5000],
    '3': [500, 500, 500, 1000, 3000]
  },
  picks: { '2027 Mid 1st': [1000, 1500, 2000, 2500, 4000] }
};

/* -------------------------------------------------- stub Sleeper league */

/* Two seasons chained by previous_league_id. Roster ids are deliberately
 * REORDERED in the prior season, so keying managers by roster_id instead of
 * user_id would mis-attribute the older draft and this test would catch it. */
const LG_CUR = { league_id: 'L2', name: 'Dynasty Now', season: '2026',
                 previous_league_id: 'L1', total_rosters: 3 };
const LG_PREV = { league_id: 'L1', name: 'Dynasty Then', season: '2025',
                  previous_league_id: null, total_rosters: 3 };
const USERS = [
  { user_id: 'uA', display_name: 'Alpha' },
  { user_id: 'uB', display_name: 'Bravo' },
  { user_id: 'uC', display_name: 'Cara' }
];
const ROSTERS_CUR = [
  { roster_id: 1, owner_id: 'uA' },
  { roster_id: 2, owner_id: 'uB' },
  { roster_id: 3, owner_id: 'uC' }
];
const ROSTERS_PREV = [
  { roster_id: 1, owner_id: 'uC' },
  { roster_id: 2, owner_id: 'uA' },
  { roster_id: 3, owner_id: 'uB' }
];

/* Draft on 2025-01-15 -> priced from the 2025-01-01 board, with four later
 * boards to measure the peak against. */
const DRAFTS = [{
  draft_id: 'd-2026', season: '2026', type: 'snake',
  start_time: Date.UTC(2025, 0, 15), settings: { rounds: 12 },
  metadata: { name: 'startup' }
}];
function draftPicks() {
  const out = [];
  for (let slot = 1; slot <= 36; slot++) {
    const idx = (slot - 1) % 3;
    out.push({
      pick_no: slot, round: Math.ceil(slot / 3), roster_id: idx + 1,
      picked_by: ROSTERS_CUR[idx].owner_id,
      player_id: String([100, 200, 300][idx]),
      metadata: { first_name: 'P', last_name: String(slot), position: 'WR' }
    });
  }
  return out;
}

/* Trade on 2026-09-10 -> priced from the 2026-06-01 board, peak from
 * 2026-12-01. Alpha takes the riser, sends the fader plus a 2027 1st. */
const TX_WEEK0 = [
  {
    transaction_id: 'tx1', type: 'trade', status: 'complete',
    created: Date.UTC(2026, 8, 10), status_updated: Date.UTC(2026, 8, 10),
    roster_ids: [1, 2],
    adds: { '100': 1, '200': 2 },
    drops: { '100': 2, '200': 1 },
    draft_picks: [
      { season: '2027', round: 1, roster_id: 1, previous_owner_id: 1, owner_id: 2 }
    ]
  },
  {
    transaction_id: 'tx2', type: 'waiver', status: 'complete',
    created: Date.UTC(2026, 8, 12), status_updated: Date.UTC(2026, 8, 12),
    roster_ids: [3], adds: { '300': 3 }, drops: null,
    settings: { waiver_bid: 17 }
  },
  {
    transaction_id: 'tx3', type: 'trade', status: 'failed',
    created: Date.UTC(2026, 8, 13), adds: { '200': 2 }, drops: { '200': 1 }
  }
];

/* ---------------------------------------------- weekly matchup records */

/* Shaped exactly as the live Sleeper API returns them, which was verified
 * against api.sleeper.app before this fixture was written: a list of
 * per-roster objects with `players_points` (the league's OWN scoring,
 * bench included), `starters`, `players` and `points` (starters only).
 *
 * Week 3 is present but all-zero. That is not padding -- it is exactly what
 * Sleeper returns for a week that has not been played yet, and the ledger
 * must drop it rather than count it as a week held. */
function mu(rosterId, pts, starters) {
  return {
    roster_id: rosterId, players: Object.keys(pts), starters: starters,
    players_points: pts,
    points: starters.reduce(function (a, s) { return a + (pts[s] || 0); }, 0)
  };
}
const MATCHUPS_2026 = {
  1: [mu(1, { '100': 30, '101': 10 }, ['100', '101']),
      mu(2, { '200': 4, '201': 20 }, ['200', '201']),
      mu(3, { '300': 10, '301': 10 }, ['300', '301'])],
  2: [mu(1, { '100': 20, '101': 10 }, ['100', '101']),
      mu(2, { '200': 6, '201': 20 }, ['200', '201']),
      mu(3, { '300': 10, '301': 10 }, ['300', '301'])],
  /* not played yet */
  3: [mu(1, { '100': 0, '101': 0 }, ['100', '101']),
      mu(2, { '200': 0, '201': 0 }, ['200', '201']),
      mu(3, { '300': 0, '301': 0 }, ['300', '301'])]
};

const fetchLog = [];
function jsonResp(payload, okFlag) {
  return Promise.resolve({
    ok: okFlag === undefined ? true : okFlag,
    json: function () { return Promise.resolve(payload); }
  });
}
function installFullLeague(week0) {
  global.fetch = function (url) {
    fetchLog.push(url);
    if (url === 'managerscore_values.json') return jsonResp(VALUES);
    if (url === 'managerscore_series.json') return jsonResp(SERIES);
    if (url === 'https://api.sleeper.app/v1/league/L2') return jsonResp(LG_CUR);
    if (url === 'https://api.sleeper.app/v1/league/L1') return jsonResp(LG_PREV);
    if (/\/league\/(L1|L2)\/users$/.test(url)) return jsonResp(USERS);
    if (/\/league\/L2\/rosters$/.test(url)) return jsonResp(ROSTERS_CUR);
    if (/\/league\/L1\/rosters$/.test(url)) return jsonResp(ROSTERS_PREV);
    if (/\/league\/L2\/drafts$/.test(url)) return jsonResp(DRAFTS);
    if (/\/league\/L1\/drafts$/.test(url)) return jsonResp([]);
    if (/\/draft\/d-2026\/picks$/.test(url)) return jsonResp(draftPicks());
    if (/\/league\/L2\/transactions\/0$/.test(url)) {
      return jsonResp(week0 === undefined ? TX_WEEK0 : week0);
    }
    if (/\/transactions\/\d+$/.test(url)) return jsonResp([]);
    const mm = url.match(/\/league\/L2\/matchups\/(\d+)$/);
    if (mm) return jsonResp(MATCHUPS_2026[mm[1]] || []);
    if (/\/league\/L1\/matchups\/\d+$/.test(url)) return jsonResp([]);
    return jsonResp(null, false);
  };
}
installFullLeague();

/* ------------------------------------------------------------- load JS */

const M = require(jsPath);
ok(typeof M.msRun === 'function', 'UI module exports msRun');
ok(typeof M.msScoreLeague === 'function', 'core is available to the UI');
ok(typeof M.msCapture === 'function', 'capture helper is available');
ok(typeof document._on_DOMContentLoaded === 'function',
   'page registers a DOMContentLoaded handler rather than firing at import');

/* -------------------------------------------------------- pure helpers */

ok(M.msMsToDate(Date.UTC(2026, 8, 10)) === '2026-09-10', 'ms epoch to ISO date');
ok(M.msMsToDate(Math.floor(Date.UTC(2026, 8, 10) / 1000)) === '2026-09-10',
   'second-precision epoch is also accepted');
ok(M.msMsToDate(null) === null, 'null timestamp degrades to null');
ok(M.msMsToDate('nonsense') === null, 'garbage timestamp degrades to null');

/* ---------------------------------------------------- end-to-end scoring */

M.msRun('L2', true).then(function (result) {
  ok(result !== null, 'msRun produced a result');
  if (!result) { return; }

  const by = {};
  result.managers.forEach(function (m) { by[m.name] = m; });
  ok(result.managers.length === 3, 'three managers scored',
     String(result.managers.length));

  ok(fetchLog.indexOf('managerscore_series.json') >= 0,
     'the dated value series was fetched');
  ok(fetchLog.indexOf('https://api.sleeper.app/v1/league/L1') >= 0,
     'the league history chain was walked');
  ok(result.managers.length === 3,
     'reordered prior-season rosters did not split anyone into two managers');

  /* ---- draft, priced from the 2025-01-01 board ---- */
  ok(result.drafts.length === 1, 'one draft found');
  ok(result.drafts[0].scored, 'the 36-pick draft was scored');
  ok(result.meta.nPicksScored === 36, 'all 36 picks scored',
     String(result.meta.nPicksScored));
  const aPick = result.audit.picks.filter(function (p) {
    return p.managerId === 'uA';
  })[0];
  ok(!!aPick, 'Alpha has audited picks');
  ok(aPick.vAtDate === '2025-01-01',
     'picks priced from the board on or before draft day', String(aPick.vAtDate));
  near(aPick.vAt, 1000, 'Alpha drafted the riser at its 2025-01 price');
  near(aPick.peak, 9000, 'against its later peak');
  near(aPick.capture, 8000, 'capturing the whole rise');
  /* Alpha took the riser, Bravo the decliner, at interleaved slots. */
  ok(by.Alpha.draft.mean > by.Bravo.draft.mean,
     'drafting the riser beats drafting the decliner');

  /* ---- trade, priced from the 2026-06-01 board ---- */
  ok(result.meta.nTrades === 1, 'one complete trade found (failed one ignored)',
     String(result.meta.nTrades));
  ok(result.meta.nTradesScored === 1, 'the trade was scored');
  const tr = result.audit.trades[0];
  const aSide = tr.sides.filter(function (s) { return s.managerId === 'uA'; })[0];
  const bSide = tr.sides.filter(function (s) { return s.managerId === 'uB'; })[0];
  ok(!!aSide && !!bSide, 'both sides attributed to user ids');

  /* riser: 6000 -> 9000 = 3000 captured. fader: 6000 -> peak 5000, so peak
   * floors at vAt and capture is 0. pick: 2500 -> 4000 = 1500. */
  near(aSide.received, 3000, 'Alpha received 3000 of capture (the riser)');
  near(aSide.given, 1500, 'Alpha gave 1500 (fader captures 0, pick captures 1500)');
  near(aSide.net, 1500, 'Alpha nets +1500');
  near(bSide.net, -1500, 'Bravo nets the mirror');
  near(aSide.net + bSide.net, 0, 'the trade is exactly zero-sum');
  ok(tr.unbalanced === false, 'a correctly parsed trade balances');

  const faderAsset = (aSide.assets.given || []).filter(function (a) {
    return a.label === 'Fading WR';
  })[0];
  ok(!!faderAsset, 'the fader appears on the given side');
  near(faderAsset.capture, 0,
       'an asset that only declined captures zero, not a negative');

  const pickAsset = (aSide.assets.given || []).filter(function (a) {
    return a.kind === 'pick';
  })[0];
  ok(!!pickAsset, 'the traded pick is carried as an asset');
  near(pickAsset.capture, 1500, 'the pick captured 2500 -> 4000');

  /* ---- waiver ---- */
  ok(result.meta.nWaivers === 1, 'one waiver add found');
  const w = result.audit.waivers[0];
  ok(w.managerId === 'uC', 'waiver attributed to the claiming roster owner');
  ok(w.faab === 17, 'FAAB bid recorded for disclosure', String(w.faab));
  near(w.vAt, 1000, 'waiver priced from the 2026-06-01 board');
  near(w.peak, 3000, 'against the 2026-12-01 peak');
  near(w.surplus, 2000, 'capturing 2000');

  /* ---- rendering ---- */
  const basis = document.getElementById('ms-basis');
  ok(/Point-in-time value capture/.test(basis.innerHTML),
     'banner states the point-in-time capture basis', basis.innerHTML.slice(0, 70));
  ok(/decays as he ages/.test(basis.innerHTML),
     'banner explains why it is not "value then vs value today"');
  ok(/already peaked/.test(basis.innerHTML),
     'banner explains why it is not "highest ever"');
  ok(/sparse/.test(basis.innerHTML), 'banner discloses that the archive is sparse');
  ok(/5 dated boards/.test(basis.innerHTML.replace(/<[^>]+>/g, '')),
     'banner reports how many dated boards back the score');

  const summary = document.getElementById('ms-summary');
  ok(/Dated boards in archive/.test(summary.innerHTML), 'summary shows archive depth');
  ok(/too recent to judge/i.test(summary.innerHTML),
     'summary exposes the unevaluable count');

  const table = document.getElementById('ms-table');
  ok(/Manager Score/.test(table.innerHTML), 'results table rendered');
  ok(/<th>Draft z<\/th>/.test(table.innerHTML), 'component breakdown columns present');

  M.msRenderAudit('uA');
  const audit = document.getElementById('ms-audit');
  ok(audit.style.display === 'block', 'audit panel opens');
  ok(/Value at pick/.test(audit.innerHTML), 'pick audit shows the price on the day');
  ok(/Peak after/.test(audit.innerHTML), 'pick audit shows the later peak');
  ok(/Captured/.test(audit.innerHTML), 'pick audit shows what was captured');
  ok(/2025-01-01/.test(audit.innerHTML),
     'audit names the board date actually used');
  ok(/got/.test(audit.innerHTML) && /gave/.test(audit.innerHTML),
     'trade audit shows both directions');

  /* ---- realized production: the second lens ---- */

  ok(fetchLog.indexOf('https://api.sleeper.app/v1/league/L2/matchups/1') >= 0,
     'weekly matchup records were fetched');
  ok(M.MSX.ledger && M.MSX.ledger.order.length === 2,
     'only played weeks entered the ledger (week 3 was all zeroes)',
     M.MSX.ledger && M.MSX.ledger.order.join(','));

  const rz = tr.realized;
  ok(!!rz, 'the realized lens is attached to the trade audit row');
  ok(rz.measurable, 'the trade is measurable against the weekly records');
  const aRz = rz.sides.filter(function (s) { return s.managerId === 'uA'; })[0];
  const bRz = rz.sides.filter(function (s) { return s.managerId === 'uB'; })[0];

  /* Alpha received the riser: 30 + 20 = 50 points over two played weeks.
   * Started RBs are 30/10/20/10 in wk1 (median 15) and 20/10/20/10 in wk2
   * (median 15), so PAR is +15 and +5. */
  near(aRz.ptsTotal, 50, 'Alpha realized 50 points from the player acquired');
  near(aRz.par, 20, 'PAR is measured against the weekly positional median');
  near(aRz.parPerWeek, 10, 'PAR per week normalises for the span held');
  near(bRz.ptsTotal, 10, 'Bravo realized 10 points from his side');
  near(aRz.netPar, 20, 'Alpha nets +20 PAR on the realized lens');

  /* The defining difference from the market lens. */
  near(aRz.netPar + bRz.netPar, 0,
       'this particular trade happens to net symmetrically');
  ok(bRz.picksUnattributed === 1,
     'the traded pick is counted as unattributed, not silently valued');
  ok(rz.partial, 'a trade containing a pick is partial for the realized lens');

  const aAsset = aRz.assets[0];
  ok(aAsset.rank && aAsset.rank.rank === 1 && aAsset.rank.of === 4,
     'the acquired player is ranked against every RB rostered in the span',
     aAsset.rank && (aAsset.rank.rank + '/' + aAsset.rank.of));
  ok(aAsset.truncated,
     'a player still rostered at the end of the record is flagged ongoing');

  /* ---- realized production renders ---- */

  ok(/Realized production/.test(audit.innerHTML),
     'the audit panel renders a realized production block');
  ok(/points actually scored/.test(audit.innerHTML),
     'the realized block states what it is measuring');
  ok(/Riser Back/.test(audit.innerHTML), 'the acquired player is named');
  ok(/RB1 of 4 in that span/.test(audit.innerHTML),
     'the positional rank is rendered, which is what makes a total legible');
  ok(/still rostered/.test(audit.innerHTML),
     'an ongoing tenure is disclosed rather than presented as settled');
  ok(/market/.test(audit.innerHTML) && /realized/.test(audit.innerHTML),
     'both lenses are labelled on the page');
  ok(/Draft picks in this trade carry no realized figure/.test(audit.innerHTML),
     'the page explains why the pick has no realized number');

  /* The two lenses must never be silently merged into the index. */
  const alphaRow = result.managers.filter(function (m) { return m.id === 'uA'; })[0];
  ok(alphaRow.realized && alphaRow.realized.n === 1,
     'each manager carries a realized rollup beside the index');
  near(alphaRow.realized.netPar, 20, 'the rollup matches the per-trade figure');

  return runDegradation();
}).then(function () { report(); })
  .catch(function (e) {
    console.error('HARNESS ERROR: ' + (e && e.stack || e));
    process.exit(1);
  });

/* ------------------------------------------------- degradation scenarios */

function runDegradation() {
  /* 1. Transactions after the last dated board cannot be judged. */
  installFullLeague([{
    transaction_id: 'txr', type: 'trade', status: 'complete',
    created: Date.UTC(2027, 5, 1), status_updated: Date.UTC(2027, 5, 1),
    roster_ids: [1, 2],
    adds: { '100': 1 }, drops: { '100': 2 }
  }]);
  return M.msRun('L2', false).then(function (res) {
    ok(res !== null, 'a too-recent trade does not crash the page');
    if (!res) return;
    ok(res.meta.nTradesScored === 0,
       'a trade with nothing recorded after it is not scored',
       String(res.meta.nTradesScored));
    ok(res.audit.trades[0].partial === true, 'it is flagged partial');
    ok(res.meta.notEvaluableAssets > 0, 'and its assets counted as unevaluable');
    const basis = document.getElementById('ms-basis');
    ok(/too recent to judge/.test(basis.innerHTML),
       'the banner says some assets are too recent',
       basis.innerHTML.replace(/<[^>]+>/g, '').slice(-140));
  }).then(function () {
    /* 2. FAAB in a trade: legitimate imbalance, scored but flagged. */
    installFullLeague([{
      transaction_id: 'txf', type: 'trade', status: 'complete',
      created: Date.UTC(2026, 8, 10), status_updated: Date.UTC(2026, 8, 10),
      roster_ids: [1, 2],
      adds: { '100': 2 }, drops: { '100': 1 },
      waiver_budget: [{ sender: 2, receiver: 1, amount: 50 }]
    }]);
    return M.msRun('L2', false);
  }).then(function (res) {
    ok(res !== null, 'a FAAB trade produces a result');
    if (!res) return;
    const t = res.audit.trades[0];
    ok(t.faab && t.faab.length === 1, 'FAAB legs are carried through');
    ok(t.faab[0].amount === 50, 'FAAB amount preserved');
    ok(t.unbalanced === false,
       'FAAB explains the imbalance, so it is not called a parse error');
    ok(t.scored === true, 'and the asset flow is still scored');
    ok(res.meta.nTradesWithFaab === 1, 'FAAB trades are counted for disclosure');
  }).then(function () {
    /* 3. Unbalanced with NO FAAB is a parsing problem: report, do not score. */
    installFullLeague([{
      transaction_id: 'txb', type: 'trade', status: 'complete',
      created: Date.UTC(2026, 8, 10), status_updated: Date.UTC(2026, 8, 10),
      roster_ids: [1, 2],
      adds: { '100': 1 }, drops: { '300': 2 }
    }]);
    return M.msRun('L2', false);
  }).then(function (res) {
    if (!res) { ok(false, 'broken trade produced no result'); return; }
    const t = res.audit.trades[0];
    ok(t.unbalanced === true, 'an unexplained imbalance is detected');
    ok(t.scored === false, 'and the trade is NOT scored');
    ok(res.meta.nTradesUnbalanced === 1, 'unbalanced trades are counted');
    ok(res.managers.every(function (m) { return m.trade.n === 0; }),
       'nobody is credited from an unbalanced trade');
  }).then(function () {
    /* 4. Value artifact missing entirely -> explain, do not score. */
    global.fetch = function (url) {
      if (url === 'managerscore_values.json') {
        return Promise.resolve({ ok: false, json: function () {
          return Promise.reject(new Error('nope')); } });
      }
      return jsonResp(null, false);
    };
    return M.msRun('L2', false);
  }).then(function (res) {
    ok(res === null, 'no result when the value artifact is missing');
    ok(/KeepTradeCut values are unavailable/.test(
         document.getElementById('ms-status').innerHTML),
       'missing artifact is explained on the page');
    ok(document.getElementById('ms-results').style.display === 'none',
       'results stay hidden rather than showing an empty table');
  }).then(function () {
    /* 5. Series missing while values exist: nothing is priceable. */
    global.fetch = function (url) {
      if (url === 'managerscore_values.json') return jsonResp(VALUES);
      if (url === 'managerscore_series.json') return jsonResp(null, false);
      if (url === 'https://api.sleeper.app/v1/league/L2') return jsonResp(LG_CUR);
      if (/\/users$/.test(url)) return jsonResp(USERS);
      if (/\/rosters$/.test(url)) return jsonResp(ROSTERS_CUR);
      if (/\/drafts$/.test(url)) return jsonResp(DRAFTS);
      if (/\/draft\/d-2026\/picks$/.test(url)) return jsonResp(draftPicks());
      if (/\/transactions\/\d+$/.test(url)) return jsonResp([]);
      return jsonResp(null, false);
    };
    return M.msRun('L2', false);
  }).then(function (res) {
    ok(res !== null, 'a missing series does not crash the page');
    if (!res) return;
    ok(res.meta.nPicksScored === 0, 'nothing is scored without the series');
    ok(res.managers.every(function (m) { return m.index === M.MS_INDEX_CENTER; }),
       'everyone sits at the league centre');
    const basis = document.getElementById('ms-basis');
    ok(/No dated value history available/.test(basis.innerHTML),
       'and the banner says the history is missing',
       basis.innerHTML.slice(0, 80));
  }).then(function () {
    /* 6. League not found on Sleeper. */
    global.fetch = function (url) {
      if (url === 'managerscore_values.json') return jsonResp(VALUES);
      if (url === 'managerscore_series.json') return jsonResp(SERIES);
      return jsonResp(null, false);
    };
    return M.msRun('NOPE', false);
  }).then(function (res) {
    ok(res === null, 'a missing league does not score');
    ok(/Could not score that league/.test(
         document.getElementById('ms-status').innerHTML),
       'a missing league is reported, not crashed on');
  });
}

function report() {
  console.log('dom: ' + (checks - failures) + '/' + checks + ' checks passed');
  process.exit(failures ? 1 : 0);
}
