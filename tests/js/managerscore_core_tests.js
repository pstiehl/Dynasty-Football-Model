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
const P = function (label, value) { return { kind: 'player', label: label, value: value }; };

const t1 = C.msScoreLeague({
  managers: mgrs, drafts: [], waivers: [], floor: 491,
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
  managers: mgrs, drafts: [], waivers: [], floor: 491,
  trades: [{
    id: 'tp', date: '2026-03-01',
    sides: [
      { managerId: 'A', received: [{ kind: 'pick', label: '2031 7th', value: null }], given: [] },
      { managerId: 'B', received: [], given: [{ kind: 'pick', label: '2031 7th', value: null }] }
    ]
  }]
}, {});
ok(partial.meta.nTradesScored === 0, 'a trade with an unpriced asset is not scored');
ok(partial.audit.trades[0].partial === true, 'that trade is flagged partial');
ok(partial.audit.trades[0].scored === false, 'and marked unscored');
ok(partial.managers.every(function (m) { return m.trade.n === 0; }),
   'no manager is credited from an unscored trade');
ok(partial.meta.unvaluedAssets > 0, 'unvalued assets are counted for disclosure');

/* Three-way trades must stay zero-sum, using given/received directly. */
const t3 = C.msScoreLeague({
  managers: mgrs, drafts: [], waivers: [], floor: 491,
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
    out.push({ managerId: owner, slot: slot, value: base + bump,
               name: 'P' + slot, pos: 'WR' });
  }
  return out;
}
const dres = C.msScoreLeague({
  managers: mgrs, trades: [], waivers: [], floor: 491,
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
  managers: mgrs, trades: [], waivers: [], floor: 491,
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
  managers: mgrs, drafts: [], trades: [], floor: 491,
  waivers: [
    { managerId: 'A', date: '2026-09-01', value: 4000, name: 'Breakout' },
    { managerId: 'A', date: '2026-09-08', value: 3000, name: 'Useful' },
    { managerId: 'B', date: '2026-09-01', value: 491, name: 'Floor guy' },
    { managerId: 'B', date: '2026-09-02', value: 491, name: 'Another' }
  ]
}, {});
const wByName = {};
wres.managers.forEach(function (m) { wByName[m.name] = m; });
near(wByName.Bravo.waiver.total, 0, 'claiming floor-value players scores zero');
ok(wByName.Alpha.waiver.total > 0, 'finding real value on the wire scores');
ok(wByName.Alpha.rank < wByName.Bravo.rank, 'waiver skill affects ranking');
ok(wres.audit.waivers.length === 4, 'every add is audited');

/* Value below the floor must clamp at zero rather than go negative. */
const below = C.msScoreLeague({
  managers: mgrs, drafts: [], trades: [], floor: 1000,
  waivers: [{ managerId: 'A', date: '2026-09-01', value: 200, name: 'Sub-floor' }]
}, {});
ok(below.audit.waivers[0].surplus === 0, 'sub-floor add clamps to zero surplus');

/* ------------------------------------------------- weight renormalisation */

/* A league that never trades must not be scored on a component nobody
 * played: the live weights must renormalise to sum to 1. */
const draftOnly = C.msScoreLeague({
  managers: mgrs, trades: [], waivers: [], floor: 491,
  drafts: [{ id: 'd1', season: '2026', label: 'x', picks: draftPicks() }]
}, {});
near(draftOnly.weights.draft, 1, 'draft-only league puts all weight on draft');
ok(draftOnly.weights.trade === 0 && draftOnly.weights.waiver === 0,
   'absent components carry no weight');
ok(draftOnly.meta.liveComponents.join(',') === 'draft',
   'live components reported', draftOnly.meta.liveComponents.join(','));

const mixed = C.msScoreLeague({
  managers: mgrs, floor: 491,
  drafts: [{ id: 'd1', season: '2026', label: 'x', picks: draftPicks() }],
  trades: [trade('t1', [P('Stud', 9000)], [P('Depth', 4000)])],
  waivers: [
    { managerId: 'A', date: '2026-09-01', value: 4000 },
    { managerId: 'B', date: '2026-09-01', value: 600 }
  ]
}, {});
near(mixed.weights.draft + mixed.weights.trade + mixed.weights.waiver, 1,
     'live weights always sum to 1');
near(mixed.weights.draft, 0.50, 'full league keeps the documented draft weight');
near(mixed.weights.trade, 0.35, 'full league keeps the documented trade weight');
near(mixed.weights.waiver, 0.15, 'full league keeps the documented waiver weight');

/* Flags must explain an abstention rather than hiding it. */
const abstain = C.msScoreLeague({
  managers: mgrs, floor: 491, drafts: [], waivers: [],
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
    return { managerId: p.managerId, slot: p.slot, value: null };
  }) }]
}, {});
ok(!unpriced.drafts[0].scored, 'a draft with no priced picks is not scored');
ok(unpriced.managers.every(function (m) { return m.index === C.MS_INDEX_CENTER; }),
   'missing KTC values degrade to a flat board, not to garbage');

/* A single participant cannot define a distribution. */
const solo = C.msScoreLeague({
  managers: [{ id: 'A', name: 'Alpha' }, { id: 'B', name: 'Bravo' }],
  drafts: [], trades: [], floor: 0,
  waivers: [{ managerId: 'A', date: '2026-09-01', value: 5000 }]
}, {});
ok(solo.managers.every(function (m) { return m.waiver.z === 0; }),
   'one active manager yields no z-spread');

console.log('core: ' + (checks - failures) + '/' + checks + ' checks passed');
process.exit(failures ? 1 : 0);
