/* Scoring-core assertions for the Manager Score page.
 *
 * Run by tests/test_managerscore.py, which writes
 * dynasty.managerscore_js.MANAGERSCORE_CORE_JS to a temp file and passes the
 * path as argv[2]. The scoring maths lives only in JavaScript (the page reads
 * Sleeper live in the browser), so this is where the arithmetic is pinned --
 * there is no Python reimplementation to assert against.
 *
 *   node tests/js/managerscore_core_tests.js /tmp/ms_core.js
 *
 * Exits 0 on success, 1 with a report on first failure.
 */
'use strict';

const path = process.argv[2];
if (!path) {
  console.error('usage: node managerscore_core_tests.js <core-js-path>');
  process.exit(2);
}
const C = require(path);

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

/* ------------------------------------------------------------------ math */

near(C.msMean([1, 2, 3]), 2, 'mean');
near(C.msMedian([5, 1, 3]), 3, 'median odd');
near(C.msMedian([4, 1, 3, 2]), 2.5, 'median even');
near(C.msPstdev([2, 2, 2]), 0, 'pstdev of constant is 0');
ok(C.msPstdev([1]) === 0, 'pstdev of single sample is 0');

/* ------------------------------------------------------- shrinkage rules */

/* The contract that makes the metric defensible: shrinkage only ever moves a
 * manager TOWARD zero. It never overshoots and never flips a sign. */
[[400, 6, 6], [30, 40, 6], [-200, 10, 6], [-5, 1, 3], [1000, 1, 6]]
  .forEach(function (t) {
    const mean = t[0], n = t[1], k = t[2];
    const s = C.msShrink(mean, n, k);
    ok(Math.abs(s) <= Math.abs(mean) + 1e-12,
       'shrink never exceeds raw mean', 'mean=' + mean + ' n=' + n);
    ok(s === 0 || (s > 0) === (mean > 0),
       'shrink preserves sign', 'mean=' + mean + ' -> ' + s);
  });
near(C.msShrink(400, 6, 6), 200, 'shrink halves at n == k');
ok(C.msShrink(500, 0, 6) === 0, 'no transactions means no credit');

/* Volume must not automatically win: a pile of mediocre transactions must
 * lose to a handful of excellent ones. */
const fewGreat = C.msShrink(400, 6, 6);
const manyMeh = C.msShrink(30, 40, 6);
ok(fewGreat > manyMeh, 'few excellent picks beat many mediocre ones',
   fewGreat.toFixed(1) + ' vs ' + manyMeh.toFixed(1));

/* ...but a genuinely better rate at equal volume must still win. */
ok(C.msShrink(300, 20, 6) > C.msShrink(100, 20, 6),
   'higher mean wins at equal volume');

/* ------------------------------------------------------------- z-scores */

const z = C.msZScores({ a: 10, b: 20, c: 30 });
near(z.b, 0, 'middle of symmetric pool is z=0');
ok(z.a < 0 && z.c > 0, 'z ordering follows values');
const zFlat = C.msZScores({ a: 5, b: 5 });
ok(zFlat.a === 0 && zFlat.b === 0, 'identical pool yields all-zero z');

/* --------------------------------------------------------- slot-cost curve */

const pairs = [];
for (let s = 1; s <= 48; s++) pairs.push({ slot: s, value: 9000 - s * 150 + (s % 3) * 200 });
const curve = C.msExpectedCurve(pairs, {});
ok(curve.ok, 'curve fits with 48 picks');
let mono = true;
for (let i = 1; i < curve.bins.length; i++) {
  if (curve.bins[i].value > curve.bins[i - 1].value) mono = false;
}
ok(mono, 'binned curve is non-increasing in slot');
ok(curve.at(1) > curve.at(24) && curve.at(24) > curve.at(48),
   'expected value falls as slots get later');
ok(curve.at(-5) === curve.at(1), 'curve is flat before the first bin');
ok(curve.at(9999) === curve.at(48), 'curve is flat after the last bin');

/* A noisy draft where later picks happen to return more must still not
 * produce a rising curve. */
const noisy = [];
for (let s = 1; s <= 36; s++) noisy.push({ slot: s, value: s < 18 ? 1000 : 8000 });
ok(C.msExpectedCurve(noisy, {}).bins.every(function (b, i, arr) {
  return i === 0 || b.value <= arr[i - 1].value;
}), 'monotonicity is enforced even against the data');

/* Degradation: too few priced picks must refuse rather than fit noise. */
const sparse = C.msExpectedCurve([{ slot: 1, value: 5 }, { slot: 2, value: 3 }], {});
ok(!sparse.ok, 'sparse draft refuses to fit a curve');
ok(sparse.at(1) === null, 'refused curve returns null, not a number');
ok(!C.msExpectedCurve([], {}).ok, 'empty draft refuses to fit a curve');
ok(!C.msExpectedCurve(null, {}).ok, 'null picks refuse to fit a curve');

/* Non-numeric values must be filtered, not coerced. */
const dirty = C.msExpectedCurve(
  [{ slot: 1, value: null }, { slot: 2, value: 'x' }, { slot: 3 }], {});
ok(!dirty.ok, 'unpriced picks do not count toward the curve minimum');

/* ------------------------------------------------------- pick valuation */

const picks = {
  '2027 Early 1st': 7114, '2027 Mid 1st': 5677, '2027 Late 1st': 4940,
  '2027 Mid 2nd': 3457, '2026 Early 1st': 5648
};
ok(C.msOrdinal(1) === '1st' && C.msOrdinal(2) === '2nd' &&
   C.msOrdinal(3) === '3rd' && C.msOrdinal(4) === '4th', 'ordinals');

const mid = C.msResolvePickValue(picks, '2027', 1);
ok(mid.value === 5677 && mid.basis === 'mid-tier',
   'a future pick is priced at Mid, not Early');

/* Season on the board but no Mid row: average the tiers that exist. */
const noMid = C.msResolvePickValue({ '2028 Early 1st': 100, '2028 Late 1st': 200 }, '2028', 1);
near(noMid.value, 150, 'tiers averaged when Mid is absent');

/* Season absent entirely: fall back to that round across seasons. */
const far = C.msResolvePickValue(picks, '2031', 1);
near(far.value, (7114 + 5677 + 4940 + 5648) / 4, 'round average across seasons');
ok(/round average/.test(far.basis), 'fallback basis is labelled', far.basis);

/* Nothing to go on: refuse, do not invent. */
const none = C.msResolvePickValue(picks, '2031', 7);
ok(none.value === null && none.basis === 'unvalued',
   'an unpriceable pick returns null rather than a guess');
ok(C.msResolvePickValue(null, '2027', 1).value === null,
   'missing pick table degrades to unvalued');

/* -------------------------------------------------------- trade scoring */

const mgrs = [{ id: 'A', name: 'Alpha' }, { id: 'B', name: 'Bravo' },
              { id: 'C', name: 'Cara' }];

function trade(id, aGot, aGave) {
  return {
    id: id, date: '2026-03-01',
    sides: [
      { managerId: 'A', received: aGot, given: aGave },
      { managerId: 'B', received: aGave, given: aGot }
    ]
  };
}
const P = function (label, cap) {
  return { kind: 'player', label: label, evaluable: true, capture: cap,
           vAt: 1000, peak: 1000 + cap };
};

const t1 = C.msScoreLeague({
  managers: mgrs, drafts: [], waivers: [],
  trades: [trade('t1', [P('Stud', 9000)], [P('Depth', 4000)])]
}, {});
const netSum = t1.managers.reduce(function (a, m) { return a + m.trade.total; }, 0);
near(netSum, 0, 'trade nets sum to zero across the league');
const byName = {};
t1.managers.forEach(function (m) { byName[m.name] = m; });
near(byName.Alpha.trade.total, 5000, 'winner nets +5000');
near(byName.Bravo.trade.total, -5000, 'loser nets -5000');
ok(byName.Cara.trade.n === 0, 'uninvolved manager has no trades');
ok(byName.Alpha.rank < byName.Bravo.rank, 'winning the trade ranks better');
ok(byName.Cara.index === C.MS_INDEX_CENTER,
   'a manager with no activity sits at exactly the league centre',
   String(byName.Cara.index));

/* An unpriceable asset must not hand the other side a fake steal. */
const partial = C.msScoreLeague({
  managers: mgrs, drafts: [], waivers: [],
  trades: [{
    id: 'tp', date: '2026-03-01',
    sides: [
      { managerId: 'A', received: [{ kind: 'pick', label: '2031 7th', evaluable: false }], given: [] },
      { managerId: 'B', received: [], given: [{ kind: 'pick', label: '2031 7th', evaluable: false }] }
    ]
  }]
}, {});
ok(partial.meta.nTradesScored === 0, 'a trade with an unpriced asset is not scored');
ok(partial.audit.trades[0].partial === true, 'that trade is flagged partial');
ok(partial.audit.trades[0].scored === false, 'and marked unscored');
ok(partial.managers.every(function (m) { return m.trade.n === 0; }),
   'no manager is credited from an unscored trade');
ok(partial.meta.notEvaluableAssets > 0, 'unevaluable assets are counted for disclosure');

/* Three-way trades must stay zero-sum, using given/received directly. */
const t3 = C.msScoreLeague({
  managers: mgrs, drafts: [], waivers: [],
  trades: [{
    id: 't3', date: '2026-03-01',
    sides: [
      { managerId: 'A', received: [P('x', 100)], given: [P('y', 900)] },
      { managerId: 'B', received: [P('y', 900)], given: [P('z', 500)] },
      { managerId: 'C', received: [P('z', 500)], given: [P('x', 100)] }
    ]
  }]
}, {});
near(t3.managers.reduce(function (a, m) { return a + m.trade.total; }, 0), 0,
     'three-way trade is zero-sum');

/* ------------------------------------------------------- draft scoring */

/* Build a 36-pick draft where Alpha systematically beats slot and Bravo
 * systematically misses, with Cara exactly at expectation. */
function draftPicks() {
  const out = [];
  for (let slot = 1; slot <= 36; slot++) {
    const base = 9000 - slot * 200;
    const owner = ['A', 'B', 'C'][(slot - 1) % 3];
    const bump = owner === 'A' ? 900 : (owner === 'B' ? -900 : 0);
    out.push({ managerId: owner, slot: slot, evaluable: true,
               capture: base + bump, vAt: 500, peak: 500 + base + bump,
               name: 'P' + slot, pos: 'WR' });
  }
  return out;
}
const dres = C.msScoreLeague({
  managers: mgrs, trades: [], waivers: [],
  drafts: [{ id: 'd1', season: '2026', label: '2026 startup', picks: draftPicks() }]
}, {});
const dByName = {};
dres.managers.forEach(function (m) { dByName[m.name] = m; });
ok(dres.drafts[0].scored, 'a 36-pick draft is scored');
ok(dByName.Alpha.draft.mean > 0, 'beating slot yields positive surplus');
ok(dByName.Bravo.draft.mean < 0, 'missing slot yields negative surplus');
ok(dByName.Alpha.rank < dByName.Cara.rank &&
   dByName.Cara.rank < dByName.Bravo.rank,
   'draft ranking orders Alpha > Cara > Bravo');
ok(dres.meta.nPicksScored === 36, 'every pick is audited', String(dres.meta.nPicksScored));
ok(dres.audit.picks.length === 36, 'audit trail covers every scored pick');
ok(dres.audit.picks.every(function (p) {
  return typeof p.expected === 'number' && typeof p.surplus === 'number';
}), 'each audited pick carries its expectation and surplus');

/* The baseline is fitted from the draft itself, so surplus must centre near
 * zero across the whole room -- that is what stops the metric from drifting
 * with league size or format. */
const totalSurplus = dres.audit.picks.reduce(function (a, p) { return a + p.surplus; }, 0);
ok(Math.abs(totalSurplus / 36) < 400,
   'league-wide mean surplus stays near zero by construction',
   String(totalSurplus / 36));

/* A draft below the minimum is skipped entirely, not scored badly. */
const tiny = C.msScoreLeague({
  managers: mgrs, trades: [], waivers: [],
  drafts: [{ id: 'd2', season: '2026', label: 'tiny', picks: [
    { managerId: 'A', slot: 1, value: 9000 },
    { managerId: 'B', slot: 2, value: 100 }
  ] }]
}, {});
ok(!tiny.drafts[0].scored, 'a 2-pick draft is not scored');
ok(tiny.drafts[0].reason === 'too few valued picks', 'and says why',
   String(tiny.drafts[0].reason));
ok(tiny.managers.every(function (m) { return m.draft.n === 0; }),
   'no draft credit from a skipped draft');
ok(tiny.managers.every(function (m) { return m.index === C.MS_INDEX_CENTER; }),
   'with nothing scorable every manager sits at the centre');

/* ------------------------------------------------------ waiver scoring */

const wres = C.msScoreLeague({
  managers: mgrs, drafts: [], trades: [],
  waivers: [
    { managerId: 'A', date: '2026-09-01', evaluable: true, capture: 4000, name: 'Breakout' },
    { managerId: 'A', date: '2026-09-08', evaluable: true, capture: 3000, name: 'Useful' },
    { managerId: 'B', date: '2026-09-01', evaluable: true, capture: 0, name: 'Never rose' },
    { managerId: 'B', date: '2026-09-02', evaluable: true, capture: 0, name: 'Another' }
  ]
}, {});
const wByName = {};
wres.managers.forEach(function (m) { wByName[m.name] = m; });
near(wByName.Bravo.waiver.total, 0, 'adds that never appreciated score zero');
ok(wByName.Alpha.waiver.total > 0, 'finding real value on the wire scores');
ok(wByName.Alpha.rank < wByName.Bravo.rank, 'waiver skill affects ranking');
ok(wres.audit.waivers.length === 4, 'every add is audited');

/* An add we cannot evaluate must be excluded and counted, never scored. */
const unev = C.msScoreLeague({
  managers: mgrs, drafts: [], trades: [],
  waivers: [{ managerId: 'A', date: '2026-09-01', evaluable: false,
              reason: 'too recent', name: 'Last week' }]
}, {});
ok(unev.audit.waivers.length === 0, 'an unevaluable add is not audited as scored');
ok(unev.meta.notEvaluableAssets === 1, 'and is counted as not evaluable');
ok(unev.managers.every(function (m) { return m.waiver.n === 0; }),
   'nobody is credited for an unevaluable add');

/* ------------------------------------------------- weight renormalisation */

/* A league that never trades must not be scored on a component nobody
 * played: the live weights must renormalise to sum to 1. */
const draftOnly = C.msScoreLeague({
  managers: mgrs, trades: [], waivers: [],
  drafts: [{ id: 'd1', season: '2026', label: 'x', picks: draftPicks() }]
}, {});
near(draftOnly.weights.draft, 1, 'draft-only league puts all weight on draft');
ok(draftOnly.weights.trade === 0 && draftOnly.weights.waiver === 0,
   'absent components carry no weight');
ok(draftOnly.meta.liveComponents.join(',') === 'draft',
   'live components reported', draftOnly.meta.liveComponents.join(','));

const mixed = C.msScoreLeague({
  managers: mgrs,
  drafts: [{ id: 'd1', season: '2026', label: 'x', picks: draftPicks() }],
  trades: [trade('t1', [P('Stud', 9000)], [P('Depth', 4000)])],
  waivers: [
    { managerId: 'A', date: '2026-09-01', evaluable: true, capture: 4000 },
    { managerId: 'B', date: '2026-09-01', evaluable: true, capture: 600 }
  ]
}, {});
near(mixed.weights.draft + mixed.weights.trade + mixed.weights.waiver, 1,
     'live weights always sum to 1');
near(mixed.weights.draft, 0.50, 'full league keeps the documented draft weight');
near(mixed.weights.trade, 0.35, 'full league keeps the documented trade weight');
near(mixed.weights.waiver, 0.15, 'full league keeps the documented waiver weight');

/* Flags must explain an abstention rather than hiding it. */
const abstain = C.msScoreLeague({
  managers: mgrs, drafts: [], waivers: [],
  trades: [trade('t1', [P('Stud', 9000)], [P('Depth', 4000)])]
}, {});
const cara = abstain.managers.filter(function (m) { return m.name === 'Cara'; })[0];
ok(cara.flags.some(function (f) { return /no trade activity/.test(f); }),
   'abstaining is flagged on the row', cara.flags.join('|'));

/* --------------------------------------------- total degradation cases */

const empty = C.msScoreLeague({ managers: [], drafts: [], trades: [], waivers: [] }, {});
ok(empty.managers.length === 0, 'an empty league yields no managers, not a crash');

const noData = C.msScoreLeague({ managers: mgrs }, {});
ok(noData.managers.length === 3, 'managers survive with no transactions at all');
ok(noData.managers.every(function (m) { return m.index === C.MS_INDEX_CENTER; }),
   'and all sit at the league centre');
ok(noData.meta.liveComponents.length === 0, 'no live components reported');

/* Unpriced players everywhere (KTC artifact missing) must not score. */
const unpriced = C.msScoreLeague({
  managers: mgrs, trades: [], waivers: [], floor: 0,
  drafts: [{ id: 'd', season: '2026', label: 'x', picks: draftPicks().map(function (p) {
    return { managerId: p.managerId, slot: p.slot, evaluable: false,
             reason: 'too recent' };
  }) }]
}, {});
ok(!unpriced.drafts[0].scored, 'a draft with no priced picks is not scored');
ok(unpriced.managers.every(function (m) { return m.index === C.MS_INDEX_CENTER; }),
   'missing KTC values degrade to a flat board, not to garbage');

/* A single participant cannot define a distribution. */
const solo = C.msScoreLeague({
  managers: [{ id: 'A', name: 'Alpha' }, { id: 'B', name: 'Bravo' }],
  drafts: [], trades: [],
  waivers: [{ managerId: 'A', date: '2026-09-01', evaluable: true, capture: 5000 }]
}, {});
ok(solo.managers.every(function (m) { return m.waiver.z === 0; }),
   'one active manager yields no z-spread');

/* ============================================================
 * Point-in-time series + value capture
 *
 * This is the basis the whole page rests on, so it is pinned hard.
 * Series: 5 dated boards. Player R rises then falls (peaks mid-series),
 * player D only declines, player L joins the board late.
 * ============================================================ */

const SERIES = {
  dates: ['2024-01-01', '2024-06-01', '2025-01-01', '2025-06-01', '2026-01-01'],
  sf: {
    R: [3000, 5000, 9000, 6000, 4000],   /* rises to a 2025-01 peak, then fades */
    D: [9000, 8000, 7000, 6000, 5000],   /* monotonic decline */
    L: [null, null, 2000, 4000, 8000]    /* not on the board until 2025 */
  },
  picks: { '2026 Mid 1st': [1000, 2000, 3000, 4000, 5000] }
};

/* ---- value at a date: never looks forward ---- */
near(C.msSeriesValueAt(SERIES, 'R', '2024-06-01').value, 5000, 'exact date hit');
near(C.msSeriesValueAt(SERIES, 'R', '2024-08-15').value, 5000,
     'between boards uses the one before');
ok(C.msSeriesValueAt(SERIES, 'R', '2024-08-15').asOf === '2024-06-01',
   'and reports which board it used');
ok(C.msSeriesValueAt(SERIES, 'R', '2023-12-31') === null,
   'before the archive begins there is no value -- it does not reach forward');
ok(C.msSeriesValueAt(SERIES, 'L', '2024-06-01') === null,
   'a player not yet on the board has no value');
near(C.msSeriesValueAt(SERIES, 'L', '2025-03-01').value, 2000,
     'once on the board, the last on-board day is used');
ok(C.msSeriesValueAt(SERIES, 'NOPE', '2025-01-01') === null,
   'unknown asset yields no value');

/* ---- peak after a date ---- */
near(C.msSeriesPeakAfter(SERIES, 'R', '2024-01-01').value, 9000,
     'peak after the start is the global max');
near(C.msSeriesPeakAfter(SERIES, 'R', '2025-06-01').value, 6000,
     'peak after the peak excludes the earlier high');
ok(C.msSeriesPeakAfter(SERIES, 'R', '2026-06-01') === null,
   'no board after the date means it cannot be judged yet');

/* ---- capture: the metric itself ---- */
const capEarly = C.msCapture(SERIES, 'R', '2024-01-01');
ok(capEarly.evaluable, 'an early acquisition is evaluable');
near(capEarly.vAt, 3000, 'priced on its own date');
near(capEarly.peak, 9000, 'against the later peak');
near(capEarly.capture, 6000, 'capture is the gap');
ok(capEarly.peakDate === '2025-01-01', 'and names the peak date');

/* THE point of the owner's design: buying before the rise beats buying at
 * the top, even though it is the same player. */
const capLate = C.msCapture(SERIES, 'R', '2025-01-01');
near(capLate.capture, 0, 'buying exactly at the peak captures nothing');
ok(capEarly.capture > capLate.capture,
   'acquiring before the rise scores above acquiring at the top');

/* A purely declining asset captures zero rather than a large negative --
 * the comparison is always relative, so age decay cannot dominate. */
const capDecl = C.msCapture(SERIES, 'D', '2024-01-01');
near(capDecl.capture, 0, 'an asset that only declined captures zero');
ok(capDecl.capture >= 0, 'capture is never negative');

/* Every asset/date combination in the fixture must respect capture >= 0. */
['R', 'D', 'L'].forEach(function (k) {
  SERIES.dates.forEach(function (d) {
    const c = C.msCapture(SERIES, k, d);
    if (c.evaluable) ok(c.capture >= 0, 'capture >= 0 for ' + k + ' @ ' + d);
  });
});

/* Not-evaluable paths must be explicit, never silently zero. */
const tooEarly = C.msCapture(SERIES, 'R', '2023-01-01');
ok(!tooEarly.evaluable, 'a transaction before the archive is not evaluable');
ok(/no value recorded/.test(tooEarly.reason), 'and says why', tooEarly.reason);
const tooRecent = C.msCapture(SERIES, 'R', '2026-09-01');
ok(!tooRecent.evaluable, 'a transaction after the last board is not evaluable');
ok(/too recent/.test(tooRecent.reason), 'and says why', tooRecent.reason);
ok(tooRecent.vAt === 4000,
   'a too-recent transaction still knows its price, just not its outcome');

/* Picks use their own table. */
const capPick = C.msCapture(SERIES, '2026 Mid 1st', '2024-01-01', 'picks');
ok(capPick.evaluable, 'pick capture works off the picks table');
near(capPick.capture, 4000, 'pick captured 1000 -> 5000');
ok(!C.msCapture(SERIES, '2026 Mid 1st', '2024-01-01').evaluable,
   'a pick key is not found in the player table');

/* Degenerate series must not throw. */
ok(!C.msCapture(null, 'R', '2025-01-01').evaluable, 'null series degrades');
ok(!C.msCapture({}, 'R', '2025-01-01').evaluable, 'empty series degrades');
ok(!C.msCapture({ dates: [], sf: {} }, 'R', '2025-01-01').evaluable,
   'series with no dates degrades');

/* ---- capture feeds trades zero-sum ---- */
const capTrade = C.msScoreLeague({
  managers: mgrs, drafts: [], waivers: [],
  trades: [{
    id: 'tc', date: '2024-01-01',
    sides: [
      { managerId: 'A',
        received: [Object.assign({ kind: 'player', label: 'Riser' }, C.msCapture(SERIES, 'R', '2024-01-01'))],
        given: [Object.assign({ kind: 'player', label: 'Decliner' }, C.msCapture(SERIES, 'D', '2024-01-01'))] },
      { managerId: 'B',
        received: [Object.assign({ kind: 'player', label: 'Decliner' }, C.msCapture(SERIES, 'D', '2024-01-01'))],
        given: [Object.assign({ kind: 'player', label: 'Riser' }, C.msCapture(SERIES, 'R', '2024-01-01'))] }
    ]
  }]
}, {});
const ctByName = {};
capTrade.managers.forEach(function (m) { ctByName[m.name] = m; });
near(ctByName.Alpha.trade.total, 6000, 'taking the riser for the decliner nets +6000');
near(ctByName.Bravo.trade.total, -6000, 'the other side nets the mirror');
near(ctByName.Alpha.trade.total + ctByName.Bravo.trade.total, 0,
     'capture-based trades remain exactly zero-sum');
ok(ctByName.Alpha.rank < ctByName.Bravo.rank,
   'the manager who acquired the riser ranks higher');

/* ==================================================================
 * REALIZED PRODUCTION LENS
 * ==================================================================
 *
 * Fixtures are synthetic but shaped exactly like Sleeper's matchup
 * response, which was verified against the live API before these were
 * written: a list of per-roster objects carrying `roster_id`, `players`,
 * `starters`, `players_points` (player_id -> points, bench included) and
 * `points` (starters only).
 */

/* Two managers, two seasons, four weeks each. Player P1 starts on roster 2
 * and is traded to roster 1 in week 3 of 2024. */
function wk(season, week, rosters) {
  return { season: season, week: week, leagueId: 'L' + season, matchups: rosters };
}
function rs(rosterId, pointsMap, starters) {
  const players = Object.keys(pointsMap);
  return { roster_id: rosterId, players: players, starters: starters || [],
           players_points: pointsMap,
           points: (starters || []).reduce(function (a, s) { return a + (pointsMap[s] || 0); }, 0) };
}

const R2U = { 'L2024:1': 'A', 'L2024:2': 'B', 'L2025:1': 'A', 'L2025:2': 'B' };
const POS = C.msPosLookup({ P1: [0, 'P One', 'RB'], P2: [0, 'P Two', 'RB'],
                           P3: [0, 'P Three', 'RB'], Q1: [0, 'Q One', 'QB'] });

const LED = C.msBuildLedger([
  /* 2024 */
  wk('2024', 1, [rs(1, { P2: 10, Q1: 20 }, ['P2', 'Q1']), rs(2, { P1: 30, P3: 5 }, ['P1', 'P3'])]),
  wk('2024', 2, [rs(1, { P2: 10, Q1: 20 }, ['P2', 'Q1']), rs(2, { P1: 30, P3: 5 }, ['P1', 'P3'])]),
  /* week 3: P1 moves to roster 1 (manager A) */
  wk('2024', 3, [rs(1, { P1: 40, P2: 10, Q1: 20 }, ['P1', 'P2', 'Q1']), rs(2, { P3: 5 }, ['P3'])]),
  wk('2024', 4, [rs(1, { P1: 20, P2: 10, Q1: 20 }, ['P1', 'P2', 'Q1']), rs(2, { P3: 5 }, ['P3'])]),
  /* 2025 continues the dynasty chain; P1 still with A */
  wk('2025', 1, [rs(1, { P1: 12, P2: 10 }, ['P1', 'P2']), rs(2, { P3: 5 }, ['P3'])])
], R2U);

ok(LED.order.length === 5, 'ledger has one entry per fetched week');
ok(LED.order[0] === '2024:1' && LED.order[4] === '2025:1',
   'ledger orders weeks chronologically across the season boundary',
   LED.order.join(','));
ok(LED.weeks['2024:3'].owner.P1 === 'A',
   'ownership is resolved to the manager, not the roster id');
ok(LED.weeks['2024:2'].owner.P1 === 'B', 'pre-trade ownership is the old manager');

const BASE = C.msBaselines(LED, POS);
/* Week 1 2024 rostered RBs: P2=10, P1=30, P3=5 -> median 10 */
near(BASE['2024:1'].RB, 10,
     'replacement level is the median score among ROSTERED players at the position');
ok(BASE['2024:1'].QB === 20, 'baselines are computed per position');

/* ---- unplayed weeks must not exist in the ledger ---- */

/* Sleeper answers matchups/{week} for the entire season as soon as it
 * opens, returning a full set of rosters with every score at 0. Verified
 * live: mid-September 2026, weeks 3-18 each returned twelve rosters and no
 * non-zero score. Counting those as weeks held diluted every per-week rate
 * threefold, so the ledger drops them. */
const LED_FUTURE = C.msBuildLedger([
  wk('2026', 1, [rs(1, { F1: 20 }, ['F1']), rs(2, { F2: 10 }, ['F2'])]),
  wk('2026', 2, [rs(1, { F1: 10 }, ['F1']), rs(2, { F2: 10 }, ['F2'])]),
  /* not played yet - Sleeper still answers, with zeroes throughout */
  wk('2026', 3, [rs(1, { F1: 0 }, ['F1']), rs(2, { F2: 0 }, ['F2'])]),
  wk('2026', 4, [rs(1, { F1: 0 }, ['F1']), rs(2, { F2: 0 }, ['F2'])])
], { 'L2026:1': 'A', 'L2026:2': 'B' });
ok(LED_FUTURE.order.length === 2,
   'unplayed all-zero weeks are excluded from the ledger',
   LED_FUTURE.order.join(','));
ok(LED_FUTURE.weeks['2026:3'] === undefined,
   'an unplayed week is not addressable in the ledger either');
const POSF = C.msPosLookup({ F1: [0, 'F1', 'RB'], F2: [0, 'F2', 'RB'] });
const fut = C.msRealizedAsset(LED_FUTURE, C.msBaselines(LED_FUTURE, POSF), POSF,
                              0, 'F1', 'A', {});
ok(fut.weeksHeld === 2,
   'weeks held counts played weeks only', String(fut.weeksHeld));
near(fut.ppw, 15, 'per-week rates are not diluted by weeks that have not happened');

/* ---- a degenerate pool has no replacement level ----
 *
 * Measured on four real seasons: in bye-heavy weeks most rostered QBs are
 * backups who never play, so the median goes to 0.00 and PAR would
 * silently become raw points -- switching the positional normalisation
 * off in exactly the weeks a star looks most impressive. 8 of 56 weeks
 * were affected, carrying 13% of all production. */
const LED_DEGEN = C.msBuildLedger([
  wk('2024', 1, [rs(1, { STAR: 26, BK1: 0 }, ['STAR']),
                 rs(2, { BK2: 0, BK3: 0 }, ['BK2'])])
], R2U);
const POSQ = C.msPosLookup({ STAR: [0, 'Star QB', 'QB'], BK1: [0, 'B1', 'QB'],
                            BK2: [0, 'B2', 'QB'], BK3: [0, 'B3', 'QB'] });
const BDEG = C.msBaselines(LED_DEGEN, POSQ);
ok(BDEG['2024:1'].QB === undefined,
   'a positional median below the floor yields NO baseline, not a zero one');
const starQb = C.msRealizedAsset(LED_DEGEN, BDEG, POSQ, 0, 'STAR', 'A', {});
near(starQb.ptsTotal, 26, 'production in a degenerate week still counts in full');
near(starQb.par, 0, 'but it contributes nothing to PAR');
ok(!starQb.parAvailable,
   'PAR is reported unavailable rather than silently equal to raw points');
ok(starQb.weeksHeld === 1 && starQb.parWeeks === 0,
   'weeks held and PAR-covered weeks are tracked separately for disclosure',
   starQb.weeksHeld + '/' + starQb.parWeeks);

/* ---- the league says where its own season ends ---- */

ok(C.msLeagueLastWeek({ settings: { playoff_week_start: 15, playoff_teams: 6 } }) === 17,
   'a 6-team playoff starting week 15 ends at week 17',
   String(C.msLeagueLastWeek({ settings: { playoff_week_start: 15, playoff_teams: 6 } })));
ok(C.msLeagueLastWeek({ settings: { playoff_week_start: 15, playoff_teams: 4 } }) === 16,
   'a 4-team playoff ends a week earlier');
ok(C.msLeagueLastWeek({ settings: { playoff_week_start: 14, playoff_teams: 8 } }) === 16,
   'an 8-team playoff is three rounds');
ok(C.msLeagueLastWeek({}) === 18, 'absent settings fall back to the full season');
ok(C.msLeagueLastWeek(null) === 18, 'so does a missing league object');
ok(C.msLeagueLastWeek({ settings: { playoff_week_start: 17, playoff_teams: 12 } }) === 18,
   'and the result is never past week 18');

/* ---- tenure: only weeks actually rostered by the acquirer count ---- */

const idx3 = C.msWeekIndex(LED, '2024', 3);
ok(idx3 === 2, 'week index locates the trade week in the chronological order');

const gotP1 = C.msRealizedAsset(LED, BASE, POS, idx3, 'P1', 'A', {});
near(gotP1.ptsTotal, 72, 'counts P1 only from the trade week on (40+20+12), not his pre-trade 60');
ok(gotP1.weeksHeld === 3, 'three weeks held', String(gotP1.weeksHeld));
ok(gotP1.arrived, 'arrival on the acquiring roster is detected');
ok(gotP1.truncated, 'a player still rostered at the end of the record is flagged ongoing');

/* The whole point: the pre-trade production belongs to the old manager. */
const preTrade = C.msRealizedAsset(LED, BASE, POS, 0, 'P1', 'B', {});
near(preTrade.ptsTotal, 60, 'the selling manager keeps only the weeks he actually held him');

/* ---- a player traded on again stops accruing ---- */

const LED2 = C.msBuildLedger([
  wk('2024', 1, [rs(1, { X: 10 }, ['X']), rs(2, { Y: 9 }, ['Y'])]),
  wk('2024', 2, [rs(1, { X: 10 }, ['X']), rs(2, { Y: 9 }, ['Y'])]),
  wk('2024', 3, [rs(1, { Y: 100 }, ['Y']), rs(2, { X: 1 }, ['X'])])  /* swapped back */
], R2U);
const POS2 = C.msPosLookup({ X: [0, 'X', 'RB'], Y: [0, 'Y', 'RB'] });
const heldThenSold = C.msRealizedAsset(LED2, C.msBaselines(LED2, POS2), POS2, 0, 'X', 'A', {});
near(heldThenSold.ptsTotal, 20, 'accrual stops the week the player leaves the roster');
ok(heldThenSold.departedWeek === '2024:3',
   'the departure week is recorded', String(heldThenSold.departedWeek));
ok(!heldThenSold.truncated, 'a departed player is not marked ongoing');

/* ---- grace window: a trade agreed after lineup lock lands next week ---- */

const LED3 = C.msBuildLedger([
  wk('2024', 1, [rs(1, { Z: 0 }, []), rs(2, { W: 7 }, ['W'])]),
  wk('2024', 2, [rs(1, { W: 25, Z: 0 }, ['W']), rs(2, {}, [])])
], R2U);
const POS3 = C.msPosLookup({ W: [0, 'W', 'RB'], Z: [0, 'Z', 'RB'] });
const late = C.msRealizedAsset(LED3, C.msBaselines(LED3, POS3), POS3, 0, 'W', 'A', {});
near(late.ptsTotal, 25, 'a player who lands a week after the trade is still credited');
ok(late.firstWeek === '2024:2', 'tenure starts when he actually appears');

/* ...but a player who never arrives is never credited. */
const never = C.msRealizedAsset(LED3, C.msBaselines(LED3, POS3), POS3, 0, 'NOPE', 'A', {});
ok(!never.arrived && never.ptsTotal === 0,
   'a player who never appears on the acquiring roster earns no credit');

/* ---- bench production is SCORED, and separately disclosed ----
 *
 * Owner decision: the acquisition is being graded, not the lineup card.
 * Trading for a producer is one skill; benching him is a different one,
 * and it is not measured on this page at all. So bench points count in
 * full, and the started figure is carried alongside so the gap stays
 * visible. */

const LED4 = C.msBuildLedger([
  wk('2024', 1, [rs(1, { B1: 30, S1: 10 }, ['S1']), rs(2, { S2: 10 }, ['S2'])])
], R2U);
const POS4 = C.msPosLookup({ B1: [0, 'Benched', 'RB'], S1: [0, 'S1', 'RB'], S2: [0, 'S2', 'RB'] });
const benched = C.msRealizedAsset(LED4, C.msBaselines(LED4, POS4), POS4, 0, 'B1', 'A', {});
near(benched.ptsTotal, 30, 'bench production is counted in full');
near(benched.ptsStarted, 0, 'and is still reported separately as not started');
near(benched.benchPts, 30, 'the bench/started gap is exposed for disclosure');
/* rostered RBs that week: 30, 10, 10 -> median 10 */
near(benched.par, 20, 'PAR credits a benched week against the ROSTERED median');
ok(benched.parAvailable, 'PAR is available for a player who never started');

/* The baseline population must match the scored population. A started-only
 * baseline would charge a benched player a starter's replacement level for
 * a week he was never asked to play. */
const LED4b = C.msBuildLedger([
  wk('2024', 1, [rs(1, { HI: 30, MID: 12, BEN: 0 }, ['HI', 'MID']),
                 rs(2, { OK: 8, BEN2: 0 }, ['OK'])])
], R2U);
const POS4b = C.msPosLookup({ HI: [0, 'Hi', 'RB'], MID: [0, 'Mid', 'RB'],
                             OK: [0, 'Ok', 'RB'], BEN: [0, 'Ben', 'RB'],
                             BEN2: [0, 'Ben2', 'RB'] });
const B4b = C.msBaselines(LED4b, POS4b);
/* rostered RBs: 30, 12, 8, 0, 0 -> median 8.
 * started-only would have been median(30, 12, 8) = 12. */
near(B4b['2024:1'].RB, 8,
     'non-playing rostered players drag replacement level below the starter bar');
const zeroWeek = C.msRealizedAsset(LED4b, B4b, POS4b, 0, 'BEN', 'A', {});
near(zeroWeek.par, -8,
     'a rostered player who produced nothing is charged the rostered bar, not a starter bar');
const heldHi = C.msRealizedAsset(LED4b, B4b, POS4b, 0, 'HI', 'A', {});
near(heldHi.par, 22, 'and a producer is credited against that same bar');

/* ---- the normalisation contract ---- */

/* Two identical players at identical per-week quality, acquired ten weeks
 * apart. Raw totals MUST differ (the early one accrues more); the per-week
 * rate and PAR-per-week MUST NOT meaningfully differ. This is the property
 * that stops a week-2 trade beating a week-12 trade purely on span. */
const manyWeeks = [];
for (let w = 1; w <= 12; w++) {
  manyWeeks.push(wk('2024', w, [
    rs(1, { EARLY: 20, LATE: 20 }, ['EARLY', 'LATE']),
    rs(2, { FILL1: 10, FILL2: 10 }, ['FILL1', 'FILL2'])
  ]));
}
const LED5 = C.msBuildLedger(manyWeeks, R2U);
const POS5 = C.msPosLookup({ EARLY: [0, 'E', 'RB'], LATE: [0, 'L', 'RB'],
                            FILL1: [0, 'F1', 'RB'], FILL2: [0, 'F2', 'RB'] });
const B5 = C.msBaselines(LED5, POS5);
const early = C.msRealizedAsset(LED5, B5, POS5, 0, 'EARLY', 'A', {});
const lateAcq = C.msRealizedAsset(LED5, B5, POS5, 9, 'LATE', 'A', {});
ok(early.ptsTotal > lateAcq.ptsTotal * 3,
   'raw totals do favour the earlier acquisition, as expected',
   early.ptsTotal + ' vs ' + lateAcq.ptsTotal);
near(early.ppw, lateAcq.ppw, 'points-per-week is span-independent');
near(early.parPerWeek, lateAcq.parPerWeek, 'PAR-per-week is span-independent');
ok(early.par > lateAcq.par,
   'total PAR still rewards holding a producer longer, which is intended');

/* PAR is position-relative: a QB scoring the positional median earns 0,
 * even though his raw points dwarf an RB above the RB median. */
const LED6 = C.msBuildLedger([
  wk('2024', 1, [
    rs(1, { QB_A: 25, RB_A: 12 }, ['QB_A', 'RB_A']),
    rs(2, { QB_B: 25, RB_B: 8 }, ['QB_B', 'RB_B'])
  ])
], R2U);
const POS6 = C.msPosLookup({ QB_A: [0, 'QA', 'QB'], QB_B: [0, 'QB', 'QB'],
                            RB_A: [0, 'RA', 'RB'], RB_B: [0, 'RB', 'RB'] });
const B6 = C.msBaselines(LED6, POS6);
const qb = C.msRealizedAsset(LED6, B6, POS6, 0, 'QB_A', 'A', {});
const rb = C.msRealizedAsset(LED6, B6, POS6, 0, 'RB_A', 'A', {});
near(qb.par, 0, 'a QB at the QB median earns no PAR despite 25 raw points');
near(rb.par, 2, 'an RB above the RB median earns PAR on the difference');
ok(rb.par > qb.par,
   'PAR does not reward acquiring the higher-scoring POSITION, only the better player');

/* ---- positional rank in span, the thing that makes a total legible ---- */

const rank = C.msPosRankInSpan(LED5, POS5, LED5.order, 'RB', 'EARLY');
ok(rank.rank === 1 || rank.rank === 2,
   'the 20-per-week RB ranks at the top of the RBs over the span',
   'rank=' + rank.rank + ' of ' + rank.of);
ok(rank.of === 4, 'the comparison set is every RB rostered in the span', String(rank.of));

/* ---- trade-level: realized is NOT zero-sum, unlike the market lens ---- */

const tradeBoth = C.msRealizedTrade({
  id: 't1', season: '2024', week: 1,
  sides: [
    { managerId: 'A', received: [{ kind: 'player', playerId: 'GOOD_A', label: 'Good A' }] },
    { managerId: 'B', received: [{ kind: 'player', playerId: 'GOOD_B', label: 'Good B' }] }
  ]
}, C.msBuildLedger([
  wk('2024', 1, [rs(1, { GOOD_A: 30, F: 10 }, ['GOOD_A', 'F']),
                 rs(2, { GOOD_B: 30, G: 10 }, ['GOOD_B', 'G'])])
], R2U), null, C.msPosLookup({ GOOD_A: [0, 'a', 'RB'], GOOD_B: [0, 'b', 'RB'],
                              F: [0, 'f', 'RB'], G: [0, 'g', 'RB'] }), {});
ok(tradeBoth.measurable, 'a trade with weekly records is measurable');
near(tradeBoth.sides[0].ptsTotal, 30, 'each side gets credited its own acquisition');
near(tradeBoth.sides[1].ptsTotal, 30, 'and so does the other');
near(tradeBoth.sides[0].netPar + tradeBoth.sides[1].netPar, 0,
     'symmetric outcomes still net to zero');

/* Both sides genuinely winning is possible and must NOT be forced to zero:
 * points are produced, not exchanged. */
const bothWin = C.msRealizedTrade({
  id: 't2', season: '2024', week: 1,
  sides: [
    { managerId: 'A', received: [{ kind: 'player', playerId: 'WA', label: 'WA' }] },
    { managerId: 'B', received: [{ kind: 'player', playerId: 'WB', label: 'WB' }] }
  ]
}, C.msBuildLedger([
  wk('2024', 1, [rs(1, { WA: 40, F: 5 }, ['WA', 'F']), rs(2, { WB: 38, G: 5 }, ['WB', 'G'])])
], R2U), null, C.msPosLookup({ WA: [0, 'wa', 'RB'], WB: [0, 'wb', 'RB'],
                              F: [0, 'f', 'RB'], G: [0, 'g', 'RB'] }), {});
ok(bothWin.sides[0].par > 0 && bothWin.sides[1].par > 0,
   'both sides can post positive PAR - production is created, not conserved');

/* Picks are reported as unattributed rather than guessed at. */
const withPick = C.msRealizedTrade({
  id: 't3', season: '2024', week: 1,
  sides: [
    { managerId: 'A', received: [{ kind: 'pick', label: '2025 Mid 1st' }] },
    { managerId: 'B', received: [{ kind: 'player', playerId: 'P2', label: 'P Two' }] }
  ]
}, LED, null, POS, {});
ok(withPick.partial, 'a trade containing a pick is flagged partial for this lens');
ok(withPick.sides[0].picksUnattributed === 1,
   'the unattributed pick is counted, not silently dropped');
ok(withPick.sides[0].assets.length === 0,
   'no fabricated production is attached to a draft pick');

/* ---- attach: the index must not move ---- */

const mgrs2 = [{ id: 'A', name: 'Alpha' }, { id: 'B', name: 'Bravo' }];
const inputRz = {
  managers: mgrs2, drafts: [], waivers: [],
  trades: [{
    id: 'tz', date: '2024-01-01', season: '2024', week: 3,
    sides: [
      { managerId: 'A', received: [{ kind: 'player', playerId: 'P1', label: 'P One', evaluable: true, capture: 100 }], given: [] },
      { managerId: 'B', received: [], given: [{ kind: 'player', playerId: 'P1', label: 'P One', evaluable: true, capture: 100 }] }
    ]
  }]
};
const scored = C.msScoreLeague(inputRz, {});
const beforeIdx = scored.managers.map(function (m) { return m.id + ':' + m.index; }).join(',');
C.msAttachRealized(scored, inputRz, LED, POS, {});
const afterIdx = scored.managers.map(function (m) { return m.id + ':' + m.index; }).join(',');
ok(beforeIdx === afterIdx,
   'attaching the realized lens does not alter the Manager Score index');
ok(scored.audit.trades[0].realized != null,
   'the realized view is attached to the audit row');
near(scored.audit.trades[0].realized.sides[0].ptsTotal, 72,
     'the attached realized figure matches the standalone computation');
const aRow = scored.managers.filter(function (m) { return m.id === 'A'; })[0];
ok(aRow.realized && aRow.realized.n === 1,
   'each manager carries a realized rollup alongside the index');

console.log('core: ' + (checks - failures) + '/' + checks + ' checks passed');
process.exit(failures ? 1 : 0);
