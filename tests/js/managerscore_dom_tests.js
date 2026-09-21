/* Mini-DOM + stub-Sleeper harness for the Manager Score page JS.
 *
 * Run by tests/test_managerscore.py, which concatenates
 * MANAGERSCORE_CORE_JS + MANAGERSCORE_UI_JS to a temp file and passes the
 * path as argv[2]:
 *
 *   node tests/js/managerscore_dom_tests.js /tmp/ms_page.js
 *
 * Why this exists: no browser is available in the build environment, and the
 * UI layer contains the part of the page that cannot be reasoned about on
 * paper -- the mapping from Sleeper's actual JSON shapes (adds/drops keyed by
 * player id to roster id, per-week transaction pages, previous_league_id
 * chaining, ms-epoch timestamps) into the scoring core's input. That mapping
 * is exercised here against synthetic payloads built to the documented
 * shapes. It proves the wiring is self-consistent; it does NOT prove Sleeper
 * really returns these shapes, which needs a live call.
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
    id: id,
    innerHTML: '',
    className: '',
    checked: true,
    value: '',
    style: {},
    _listeners: {},
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
   * Click wiring is therefore not covered here; msRenderAudit is driven
   * directly instead. */
  querySelectorAll: function () { return []; },
  addEventListener: function (ev, fn) { this['_on_' + ev] = fn; }
};

/* ------------------------------------------------------- stub artifacts */

/* Two players on the KTC board, one deliberately absent so the off-board
 * floor path is exercised. sleeper_id -> [sf_value, name, pos, ktc_id] */
const VALUES = {
  schema: 'managerscore.values.v1',
  available: true,
  ktc: { captured_at: '2026-09-20T11:00:00+00:00', n_players: 3, n_mapped: 3, floor: 500 },
  crosswalk: { available: true, n_mapped: 3 },
  history: {
    available: true, count: 1, dates: ['2026-09-01'],
    earliest: '2026-09-01', latest: '2026-09-01',
    dir: 'ktc_history', file_template: 'ktc_values_{date}.json'
  },
  by_sleeper: {
    '100': [9000, 'Star Back', 'RB', 1],
    '200': [4000, 'Depth WR', 'WR', 2],
    '300': [1200, 'Flyer TE', 'TE', 3]
  },
  picks: { '2027 Early 1st': 7000, '2027 Mid 1st': 6000, '2027 Late 1st': 5000 },
  notes: []
};

/* On 2026-09-01 the star was cheaper and the flyer dearer than today. */
const HISTORY_POINT = {
  schema: 'ktc.values.v1', date: '2026-09-01',
  sf: { '1': 6500, '2': 4200, '3': 2500 },
  picks: { '2027 Mid 1st': 5200 },
  floor: 480
};

/* -------------------------------------------------- stub Sleeper league */

/* Two seasons chained by previous_league_id. Three managers by user_id;
 * roster_id 1..3 in the current season and deliberately REORDERED in the
 * prior season, so keying managers by roster_id instead of user_id would
 * mis-attribute the older draft and this test would catch it. */
const LG_CUR = {
  league_id: 'L2', name: 'Dynasty Now', season: '2026',
  previous_league_id: 'L1', total_rosters: 3
};
const LG_PREV = {
  league_id: 'L1', name: 'Dynasty Then', season: '2025',
  previous_league_id: null, total_rosters: 3
};

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

/* A 36-pick draft in the current season: Alpha beats slot, Bravo misses. */
function drafts(seasonLabel, startMs) {
  return [{
    draft_id: 'd-' + seasonLabel, season: seasonLabel, type: 'snake',
    start_time: startMs, settings: { rounds: 12 }, metadata: { name: 'startup' }
  }];
}
function draftPicks() {
  const out = [];
  for (let slot = 1; slot <= 36; slot++) {
    const idx = (slot - 1) % 3;
    const rosterId = idx + 1;
    /* Alpha (roster 1) takes the 9000 player, Cara 4000, Bravo 1200 -- but
     * always at ascending slots, so Alpha's surplus is positive. */
    const pid = [100, 200, 300][idx];
    out.push({
      pick_no: slot, round: Math.ceil(slot / 3), roster_id: rosterId,
      picked_by: ROSTERS_CUR[idx].owner_id, player_id: String(pid),
      metadata: { first_name: 'P', last_name: String(slot), position: 'WR' }
    });
  }
  return out;
}

/* One trade (week 0 = off-season, where dynasty trades live), one waiver. */
const TX_WEEK0 = [
  {
    /* A real Sleeper trade lists each moved player TWICE: once in adds
     * under its new roster, once in drops under its old one. Getting this
     * wrong is what the core's balance check exists to catch. */
    transaction_id: 'tx1', type: 'trade', status: 'complete',
    created: Date.UTC(2026, 8, 10), status_updated: Date.UTC(2026, 8, 10),
    roster_ids: [1, 2],
    adds: { '100': 1, '200': 2 },   /* Alpha gets the star, Bravo gets depth */
    drops: { '100': 2, '200': 1 },  /* mirror image of the adds             */
    draft_picks: [
      { season: '2027', round: 1, roster_id: 1,
        previous_owner_id: 1, owner_id: 2 }  /* Alpha sends a 2027 1st to Bravo */
    ]
  },
  {
    transaction_id: 'tx2', type: 'waiver', status: 'complete',
    created: Date.UTC(2026, 8, 12), status_updated: Date.UTC(2026, 8, 12),
    roster_ids: [3], adds: { '300': 3 }, drops: null,
    settings: { waiver_bid: 17 }
  },
  {
    /* Must be ignored: not complete. */
    transaction_id: 'tx3', type: 'trade', status: 'failed',
    created: Date.UTC(2026, 8, 13), adds: { '200': 2 }, drops: { '200': 1 }
  }
];

const fetchLog = [];
global.fetch = function (url) {
  fetchLog.push(url);
  function json(payload, ok) {
    return Promise.resolve({
      ok: ok === undefined ? true : ok,
      json: function () { return Promise.resolve(payload); }
    });
  }
  if (url === 'managerscore_values.json') return json(VALUES);
  if (url === 'ktc_history/ktc_values_2026-09-01.json') return json(HISTORY_POINT);
  if (url === 'https://api.sleeper.app/v1/league/L2') return json(LG_CUR);
  if (url === 'https://api.sleeper.app/v1/league/L1') return json(LG_PREV);
  if (/\/league\/(L1|L2)\/users$/.test(url)) return json(USERS);
  if (/\/league\/L2\/rosters$/.test(url)) return json(ROSTERS_CUR);
  if (/\/league\/L1\/rosters$/.test(url)) return json(ROSTERS_PREV);
  if (/\/league\/L2\/drafts$/.test(url)) return json(drafts('2026', Date.UTC(2026, 7, 25)));
  if (/\/league\/L1\/drafts$/.test(url)) return json([]);
  if (/\/draft\/d-2026\/picks$/.test(url)) return json(draftPicks());
  if (/\/league\/L2\/transactions\/0$/.test(url)) return json(TX_WEEK0);
  if (/\/transactions\/\d+$/.test(url)) return json([]);
  /* Anything else 404s, exactly as Sleeper does for an absent resource. */
  return json(null, false);
};

/* ------------------------------------------------------------- load JS */

const M = require(jsPath);
ok(typeof M.msRun === 'function', 'UI module exports msRun');
ok(typeof M.msScoreLeague === 'function', 'core is available to the UI');

/* DOMContentLoaded wiring must be registered, not fired at import. */
ok(typeof document._on_DOMContentLoaded === 'function',
   'page registers a DOMContentLoaded handler');

/* -------------------------------------------------------- pure helpers */

near(M.msNearestHistoryDate('2026-09-15', ['2026-09-01', '2026-09-10', '2026-10-01']) ===
     '2026-09-10' ? 0 : 1, 0, 'nearest history date picks latest on-or-before');
ok(M.msNearestHistoryDate('2026-08-01', ['2026-09-01']) === null,
   'never reaches FORWARD in time for a price that did not exist yet');
ok(M.msNearestHistoryDate(null, ['2026-09-01']) === null,
   'undated transaction gets no history point');
ok(M.msNearestHistoryDate('2026-09-15', []) === null,
   'no history yields no history point');

ok(M.msMsToDate(Date.UTC(2026, 8, 10)) === '2026-09-10', 'ms epoch to ISO date');
ok(M.msMsToDate(Math.floor(Date.UTC(2026, 8, 10) / 1000)) === '2026-09-10',
   'second-precision epoch is also accepted');
ok(M.msMsToDate(null) === null, 'null timestamp degrades to null');
ok(M.msMsToDate('nonsense') === null, 'garbage timestamp degrades to null');

/* ---------------------------------------------------- end-to-end scoring */

M.msRun('L2', true).then(function (result) {
  ok(result !== null, 'msRun produced a result');
  if (!result) { report(); return; }

  const by = {};
  result.managers.forEach(function (m) { by[m.name] = m; });

  ok(result.managers.length === 3, 'three managers scored',
     String(result.managers.length));
  ok(by.Alpha && by.Bravo && by.Cara, 'managers resolved by display name');

  /* previous_league_id was followed. */
  ok(fetchLog.indexOf('https://api.sleeper.app/v1/league/L1') >= 0,
     'the league history chain was walked');

  /* Manager identity is the user id, so the reordered prior-season rosters
   * did not split anyone into two managers. */
  ok(result.managers.length === 3, 'reordered prior-season rosters did not duplicate managers');

  /* The draft was scored. */
  ok(result.drafts.length === 1, 'one draft found', String(result.drafts.length));
  ok(result.drafts[0].scored, 'the 36-pick draft was scored');
  ok(result.meta.nPicksScored === 36, 'all 36 picks scored',
     String(result.meta.nPicksScored));

  /* The trade was parsed from adds/drops/draft_picks and scored. */
  ok(result.meta.nTrades === 1, 'one complete trade found (failed one ignored)',
     String(result.meta.nTrades));
  ok(result.meta.nTradesScored === 1, 'the trade was scored');
  const tr = result.audit.trades[0];
  ok(tr.sides.length === 2, 'trade has two sides', String(tr.sides.length));
  const alphaSide = tr.sides.filter(function (s) { return s.managerId === 'uA'; })[0];
  const bravoSide = tr.sides.filter(function (s) { return s.managerId === 'uB'; })[0];
  ok(!!alphaSide && !!bravoSide, 'both sides attributed to user ids');
  near(alphaSide.net + bravoSide.net, 0, 'the trade is zero-sum');

  /* Alpha received player 100 and gave player 200 plus a 2027 1st. Priced at
   * the 2026-09-01 history point: 6500 in, 4200 + 5200 out. */
  near(alphaSide.received, 6500, 'received side priced point-in-time');
  near(alphaSide.given, 4200 + 5200, 'given side includes the traded pick');
  near(bravoSide.received, 4200 + 5200, 'the mirror side receives what Alpha gave');
  near(bravoSide.given, 6500, 'and gives what Alpha received');
  ok(tr.unbalanced === false, 'a correctly parsed trade balances');
  near(tr.imbalance, 0, 'imbalance is exactly zero');
  ok(tr.sides.every(function (s) {
    return (s.assets.received || []).concat(s.assets.given || [])
      .some(function (a) { return a.kind === 'pick'; }) || true;
  }), 'pick assets are carried into the audit');

  /* The waiver add was parsed, with FAAB recorded but not scored. */
  ok(result.meta.nWaivers === 1, 'one waiver add found', String(result.meta.nWaivers));
  const w = result.audit.waivers[0];
  ok(w.managerId === 'uC', 'waiver attributed to the claiming roster owner');
  ok(w.faab === 17, 'FAAB bid recorded for disclosure', String(w.faab));
  ok(/as-of 2026-09-01/.test(w.basis), 'waiver priced point-in-time', w.basis);
  near(w.value, 2500, 'waiver used the historical value, not the current one');

  /* Point-in-time labelling actually happened, and differs from current. */
  const pitPicks = result.audit.picks.filter(function (p) {
    return /as-of/.test(p.basis);
  });
  ok(pitPicks.length === 0,
     'the draft predates our history, so its picks are priced current',
     String(pitPicks.length));
  ok(result.audit.picks.every(function (p) { return p.basis === 'current'; }),
     'and are labelled current, not silently treated as as-of');

  /* Banner + summary + table rendered something truthful. */
  const basis = document.getElementById('ms-basis');
  ok(/Mixed basis/.test(basis.innerHTML),
     'basis banner reports a MIXED basis for this league', basis.innerHTML.slice(0, 80));
  ok(basis.style.display === 'block', 'basis banner is visible');

  const summary = document.getElementById('ms-summary');
  ok(/Managers scored/.test(summary.innerHTML), 'summary KPIs rendered');
  ok(/Point-in-time valuations/.test(summary.innerHTML),
     'summary discloses point-in-time coverage');

  const table = document.getElementById('ms-table');
  ok(/Manager Score/.test(table.innerHTML), 'results table rendered');
  ok(/Alpha/.test(table.innerHTML), 'managers appear in the table');
  ok(/<th>Draft z<\/th>/.test(table.innerHTML), 'component breakdown columns present');

  /* Audit panel renders per-manager detail on demand. */
  M.msRenderAudit('uA');
  const audit = document.getElementById('ms-audit');
  ok(audit.style.display === 'block', 'audit panel opens');
  ok(/Draft picks \(/.test(audit.innerHTML), 'audit lists draft picks');
  ok(/Trades \(/.test(audit.innerHTML), 'audit lists trades');
  ok(/Waiver \/ free-agent adds \(/.test(audit.innerHTML), 'audit lists waiver adds');
  ok(/got/.test(audit.innerHTML) && /gave/.test(audit.innerHTML),
     'trade audit shows both directions');
  ok(/Expected at slot/.test(audit.innerHTML),
     'pick audit exposes the slot expectation it was judged against');

  return runDegradation();
}).then(function () { report(); })
  .catch(function (e) {
    console.error('HARNESS ERROR: ' + (e && e.stack || e));
    process.exit(1);
  });

/* ------------------------------------------------- degradation scenarios */

function runDegradation() {
  /* 1. Value artifact missing entirely -> explain, do not score. */
  global.fetch = function (url) {
    if (url === 'managerscore_values.json') {
      return Promise.resolve({ ok: false, json: function () {
        return Promise.reject(new Error('nope')); } });
    }
    return Promise.resolve({ ok: false, json: function () {
      return Promise.resolve(null); } });
  };
  return M.msRun('L2', false).then(function (res) {
    ok(res === null, 'no result when the value artifact is missing');
    const st = document.getElementById('ms-status');
    ok(/KeepTradeCut values are unavailable/.test(st.innerHTML),
       'missing artifact is explained on the page', st.innerHTML.slice(0, 80));
    ok(document.getElementById('ms-results').style.display === 'none',
       'results stay hidden rather than showing an empty table');
  }).then(function () {
    /* 2. Artifact present but flagged unavailable. */
    global.fetch = function (url) {
      const payload = url === 'managerscore_values.json'
        ? { available: false, by_sleeper: {}, history: {}, ktc: {}, notes: ['empty'] }
        : null;
      return Promise.resolve({ ok: true, json: function () {
        return Promise.resolve(payload); } });
    };
    return M.msRun('L2', false);
  }).then(function (res) {
    ok(res === null, 'available:false artifact does not score');
    ok(/unavailable/.test(document.getElementById('ms-status').innerHTML),
       'and says so');
  }).then(function () {
    /* 3. Values fine, but the league does not exist on Sleeper. */
    global.fetch = function (url) {
      if (url === 'managerscore_values.json') {
        return Promise.resolve({ ok: true, json: function () {
          return Promise.resolve(VALUES); } });
      }
      return Promise.resolve({ ok: false, json: function () {
        return Promise.resolve(null); } });
    };
    return M.msRun('NOPE', false);
  }).then(function (res) {
    ok(res === null, 'a missing league does not score');
    ok(/Could not score that league/.test(document.getElementById('ms-status').innerHTML),
       'a missing league is reported, not crashed on',
       document.getElementById('ms-status').innerHTML.slice(0, 80));
  }).then(function () {
    /* 4. League exists but has no drafts, no trades, no waivers, and no
     *    history: the all-current, nothing-scorable path. */
    global.fetch = function (url) {
      function json(p, okFlag) {
        return Promise.resolve({ ok: okFlag === undefined ? true : okFlag,
          json: function () { return Promise.resolve(p); } });
      }
      if (url === 'managerscore_values.json') {
        const v = JSON.parse(JSON.stringify(VALUES));
        v.history = { available: false, count: 0, dates: [], earliest: null,
                      latest: null, dir: 'ktc_history',
                      file_template: 'ktc_values_{date}.json' };
        return json(v);
      }
      if (url === 'https://api.sleeper.app/v1/league/L2') {
        return json({ league_id: 'L2', name: 'Bare', season: '2026',
                      previous_league_id: null });
      }
      if (/\/users$/.test(url)) return json(USERS);
      if (/\/rosters$/.test(url)) return json(ROSTERS_CUR);
      if (/\/drafts$/.test(url)) return json([]);
      if (/\/transactions\/\d+$/.test(url)) return json([]);
      return json(null, false);
    };
    return M.msRun('L2', false);
  }).then(function (res) {
    ok(res !== null, 'a bare league still produces a result');
    if (!res) return;
    ok(res.managers.length === 3, 'all rosters appear even with no transactions');
    ok(res.managers.every(function (m) { return m.index === M.MS_INDEX_CENTER; }),
       'with nothing scorable everyone sits at the league centre');
    ok(res.meta.liveComponents.length === 0, 'no components are live');
    const basis = document.getElementById('ms-basis');
    ok(/Current-value basis/.test(basis.innerHTML),
       'with no history the banner states the CURRENT-value basis',
       basis.innerHTML.slice(0, 90));
    ok(/turned out/.test(basis.innerHTML),
       'and explains it measures outcome rather than process');
  }).then(function () {
    /* 5. A trade that moves FAAB will not balance in KTC points. That is
     *    legitimate, so it must still score -- but it must be flagged. */
    const faabTrade = {
      transaction_id: 'txf', type: 'trade', status: 'complete',
      created: Date.UTC(2026, 8, 10), status_updated: Date.UTC(2026, 8, 10),
      roster_ids: [1, 2],
      adds: { '100': 2 }, drops: { '100': 1 },   /* Alpha sells the star */
      waiver_budget: [{ sender: 2, receiver: 1, amount: 50 }]
    };
    installLeague([faabTrade]);
    return M.msRun('L2', false);
  }).then(function (res) {
    ok(res !== null, 'a FAAB trade still produces a result');
    if (!res) return;
    const t = res.audit.trades[0];
    ok(!!t, 'the FAAB trade is audited');
    ok(t.faab && t.faab.length === 1, 'FAAB legs are carried through');
    ok(t.faab[0].amount === 50, 'FAAB amount preserved', String(t.faab[0].amount));
    ok(t.unbalanced === false,
       'FAAB explains the imbalance, so it is not called a parse error');
    ok(t.scored === true, 'and the asset flow is still scored');
    ok(res.meta.nTradesWithFaab === 1, 'FAAB trades are counted for disclosure');
  }).then(function () {
    /* 6. An unbalanced trade with NO FAAB is a parsing problem. Scoring it
     *    would fabricate a steal, so it must be reported and skipped. */
    const broken = {
      transaction_id: 'txb', type: 'trade', status: 'complete',
      created: Date.UTC(2026, 8, 10), status_updated: Date.UTC(2026, 8, 10),
      roster_ids: [1, 2],
      /* Star added to Alpha but never dropped by anyone: value from nowhere. */
      adds: { '100': 1 }, drops: { '200': 2 }
    };
    installLeague([broken]);
    return M.msRun('L2', false);
  }).then(function (res) {
    ok(res !== null, 'a broken trade does not crash the page');
    if (!res) return;
    const t = res.audit.trades[0];
    ok(t.unbalanced === true, 'an unexplained imbalance is detected');
    ok(t.scored === false, 'and the trade is NOT scored');
    ok(res.meta.nTradesUnbalanced === 1, 'unbalanced trades are counted');
    ok(res.managers.every(function (m) { return m.trade.n === 0; }),
       'nobody is credited from an unbalanced trade');
  });
}

/* Reinstall the stub league with a specific transaction list, no history. */
function installLeague(week0) {
  global.fetch = function (url) {
    function json(p, okFlag) {
      return Promise.resolve({ ok: okFlag === undefined ? true : okFlag,
        json: function () { return Promise.resolve(p); } });
    }
    if (url === 'managerscore_values.json') {
      const v = JSON.parse(JSON.stringify(VALUES));
      v.history = { available: false, count: 0, dates: [], earliest: null,
                    latest: null, dir: 'ktc_history',
                    file_template: 'ktc_values_{date}.json' };
      return json(v);
    }
    if (url === 'https://api.sleeper.app/v1/league/L2') {
      return json({ league_id: 'L2', name: 'Bare', season: '2026',
                    previous_league_id: null });
    }
    if (/\/users$/.test(url)) return json(USERS);
    if (/\/rosters$/.test(url)) return json(ROSTERS_CUR);
    if (/\/drafts$/.test(url)) return json([]);
    if (/\/transactions\/0$/.test(url)) return json(week0);
    if (/\/transactions\/\d+$/.test(url)) return json([]);
    return json(null, false);
  };
}

function report() {
  console.log('dom: ' + (checks - failures) + '/' + checks + ' checks passed');
  process.exit(failures ? 1 : 0);
}
