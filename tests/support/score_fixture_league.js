/* Score a deterministic synthetic league with the REAL Manager Score core.
 *
 * Why this exists
 * ---------------
 * tests/test_manager_detail.py asserts that the per-manager drill-down
 * artifacts reconcile to the leaderboard's component z-scores. That claim is
 * only worth making against output from the *shipped scorer*: a Python
 * re-implementation of msScoreLeague would reconcile against itself by
 * construction and prove nothing.
 *
 * So this loads MANAGERSCORE_CORE_JS as a module (it exports msScoreLeague)
 * and scores a synthetic league with it. The league is synthetic because the
 * real corpus is built by a bounded crawl of a rate-limited third-party API
 * and no test may depend on network access — but every number below this
 * point is computed by the real thing, including the slot-cost curve, the
 * shrinkage, the z-score pool and the weight renormalisation.
 *
 * The fixture is deterministic (a fixed-seed LCG, no Date.now, no Math.random)
 * so a reconciliation failure is always reproducible.
 *
 * Usage:  node score_fixture_league.js <core-js-path> [seed]
 * Output: one JSON object on stdout, in the shape crawl_cross_league.py
 *         hands to crossleague.compact_result / manager_detail.
 */
'use strict';

const path = require('path');

const corePath = process.argv[2];
if (!corePath) {
  console.error('usage: node score_fixture_league.js <core-js-path> [seed]');
  process.exit(2);
}
const SEED = Number(process.argv[3] || 7);

const core = require(path.resolve(corePath));
const msScoreLeague = core.msScoreLeague;
if (typeof msScoreLeague !== 'function') {
  console.error('core JS did not export msScoreLeague');
  process.exit(2);
}

/* ---------------------------------------------------- deterministic rand */

let _x = SEED >>> 0;
function rnd() {
  _x = (Math.imul(1103515245, _x) + 12345) >>> 0;
  return (_x % 100000) / 100000;
}

/* ------------------------------------------------------------- the league */

const N_TEAMS = 12;
const POS = ['QB', 'RB', 'WR', 'TE'];

const managers = [];
for (let i = 0; i < N_TEAMS; i++) {
  managers.push({ id: 'u' + (100 + i), name: 'Manager ' + (i + 1) });
}
/* One seat with no Sleeper user, so the synthetic `roster:<league>:<slot>`
 * id shape that 66 of the live corpus's managers actually carry is exercised
 * end to end -- including through the filename encoder. */
managers.push({ id: 'roster:900100200300400500:13', name: 'Team 13' });

/* Per-manager drafting skill, in capture points added to every pick. This is
 * what makes the z-scores spread; the scorer still has to recover it. */
const skill = {};
managers.forEach(function (m, i) { skill[m.id] = (i - 6) * 40; });

function buildDraft(id, season, label, rounds) {
  const picks = [];
  const nTeams = managers.length;
  for (let r = 1; r <= rounds; r++) {
    for (let s = 1; s <= nTeams; s++) {
      /* Snake, like a real dynasty draft. */
      const seat = (r % 2 === 1) ? s : (nTeams - s + 1);
      const m = managers[seat - 1];
      const slot = (r - 1) * nTeams + s;
      /* Value decays with slot; capture is value gained after the pick. */
      const vAt = Math.round(9000 * Math.pow(0.97, slot) + 400);
      const base = 1200 * Math.pow(0.95, slot);
      const noise = (rnd() - 0.5) * 300;
      const capture = Math.max(0, base + skill[m.id] + noise);
      picks.push({
        managerId: m.id,
        slot: slot,
        round: r,
        playerId: String(3000 + slot + (season === '2025' ? 500 : 0)),
        name: 'Player ' + season + '-' + slot,
        pos: POS[slot % POS.length],
        date: season + '-05-01',
        /* Alternate the pricing basis so the drill-down's point-in-time vs
         * current labelling (PR #63) has both cases to render. */
        basis: (slot % 5 === 0) ? 'current' : 'point-in-time',
        evaluable: true,
        vAt: vAt,
        vAtDate: season + '-05-01',
        peak: Math.round(vAt + capture),
        peakDate: season + '-11-01',
        capture: capture
      });
    }
  }
  /* A pick we could not price at all: the scorer must skip it rather than
   * score it as zero, and the drill-down must not list it as evidence. */
  picks.push({
    managerId: managers[0].id, slot: rounds * managers.length + 1,
    round: rounds + 1, playerId: '9999', name: 'Unpriced Rookie', pos: 'WR',
    date: season + '-05-01', basis: 'point-in-time',
    evaluable: false, capture: null, vAt: null, peak: null
  });
  return { id: id, season: season, label: label, picks: picks };
}

const drafts = [
  buildDraft('d-startup', '2024', '2024 startup draft', 3),
  buildDraft('d-rookie', '2025', '2025 rookie draft', 2)
];

/* ---- trades: some scored, one unbalanced, one with an unpriced asset ---- */

function playerAsset(pid, label, pos, capture, evaluable) {
  return {
    kind: 'player', playerId: String(pid), label: label, pos: pos,
    evaluable: evaluable !== false,
    capture: evaluable === false ? null : capture,
    vAt: 5000, peak: 5000 + (capture || 0)
  };
}

const trades = [];
for (let t = 0; t < 8; t++) {
  const a = managers[t % N_TEAMS];
  const b = managers[(t + 5) % N_TEAMS];
  const gain = Math.round((rnd() - 0.4) * 900);
  trades.push({
    id: 'tr' + t,
    date: '2025-0' + ((t % 8) + 1) + '-15',
    basis: 'point-in-time',
    sides: [
      { managerId: a.id,
        received: [playerAsset(7000 + t, 'Traded In ' + t, 'RB', 800 + gain)],
        given: [playerAsset(8000 + t, 'Traded Out ' + t, 'WR', 800)] },
      { managerId: b.id,
        received: [playerAsset(8000 + t, 'Traded Out ' + t, 'WR', 800)],
        given: [playerAsset(7000 + t, 'Traded In ' + t, 'RB', 800 + gain)] }
    ]
  });
}
/* Contains an asset with no price: reported by the scorer, not counted.
 * The drill-down must show it as unscored, not silently drop it. */
trades.push({
  id: 'tr-partial', date: '2025-09-01', basis: 'current',
  sides: [
    { managerId: managers[2].id,
      received: [playerAsset(7777, 'Unpriced Asset', 'TE', null, false)],
      given: [playerAsset(8888, 'Known Asset', 'QB', 500)] },
    { managerId: managers[3].id,
      received: [playerAsset(8888, 'Known Asset', 'QB', 500)],
      given: [playerAsset(7777, 'Unpriced Asset', 'TE', null, false)] }
  ]
});

/* ---- waivers ---- */

const waivers = [];
for (let w = 0; w < 40; w++) {
  const m = managers[w % managers.length];
  waivers.push({
    managerId: m.id,
    date: '2025-1' + (w % 2) + '-0' + ((w % 9) + 1),
    playerId: String(6000 + w),
    name: 'Waiver Add ' + w,
    pos: POS[w % POS.length],
    basis: 'point-in-time',
    evaluable: true,
    vAt: 600,
    vAtDate: '2025-10-01',
    peak: 600 + Math.max(0, Math.round((rnd() - 0.3) * 700 + skill[m.id] / 8)),
    peakDate: '2025-12-01',
    faab: (w % 3 === 0) ? (w % 17) : null,
    capture: Math.max(0, Math.round((rnd() - 0.3) * 700 + skill[m.id] / 8))
  });
}

const input = {
  managers: managers, drafts: drafts, trades: trades, waivers: waivers,
  floor: 400
};

const result = msScoreLeague(input);

process.stdout.write(JSON.stringify({
  league_id: '900100200300400500',
  name: 'Reconciliation Test League',
  season: '2025',
  n_teams: N_TEAMS,
  lineage_ids: ['900100200300400500'],
  lineage_root: '900100200300400500',
  discovered_via: 'fixture',
  hop: 0,
  result: result
}));
