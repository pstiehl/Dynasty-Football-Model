/* The evidence gate, asserted on the strings a visitor actually sees.
 *
 * Phil, 2026-09-22: only list managers whose score can be taken apart, and
 * FantasyticBeast is the named case.
 *
 * tests/test_manager_evidence_gate.py proves the *data* layer withholds the
 * right rows. This proves the two things only the renderer can prove:
 *
 *   1. A withheld manager's name does not appear anywhere in the HTML the
 *      board writes into the document -- not in a row, not in a data
 *      attribute, not in a hidden detail panel. That last one matters:
 *      every leaderboard row also emits a collapsed <tr class="xl-detail">,
 *      so "not visible" and "not in the markup" are different claims and
 *      the weaker one is not enough.
 *   2. The page SAYS how many managers were withheld. A quietly shorter
 *      table is indistinguishable from a broken crawl.
 *
 * Run from tests/test_crossleague.py, or directly:
 *   node tests/js/crossleague_evidence_tests.mjs /tmp/xl.js
 *
 * Exits 0 on success, 1 with a report.
 */
'use strict';

import { createRequire } from 'node:module';

const jsPath = process.argv[2];
if (!jsPath) {
  console.error('usage: node crossleague_evidence_tests.mjs <crossleague-js-path>');
  process.exit(2);
}

let failures = 0;
let checks = 0;
function ok(cond, label, detail) {
  checks++;
  if (!cond) {
    failures++;
    console.error('FAIL: ' + label + (detail ? '  [' + String(detail).slice(0, 400) + ']' : ''));
  }
}

/* ------------------------------------------------------------- mini DOM */

const ELEMENTS = {};
function makeEl(id) {
  return {
    id, innerHTML: '', textContent: '', className: '', style: {},
    dataset: {}, _listeners: [],
    classList: { add() {}, remove() {}, contains() { return false; } },
    addEventListener(ev, fn) { this._listeners.push([ev, fn]); },
    appendChild() {}, scrollIntoView() {},
    querySelector() { return null; },
    querySelectorAll() { return []; }
  };
}
global.document = {
  getElementById(id) {
    if (!ELEMENTS[id]) ELEMENTS[id] = makeEl(id);
    return ELEMENTS[id];
  },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {}
};

const require = createRequire(import.meta.url);
const X = require(jsPath);

/* ------------------------------------------------------------- fixtures */

function comp(n, z) { return { n, z, zbar: z, available: n > 0 }; }

/* Shaped on the real corpus row for the named case: rank 4, 118.7, a single
 * league, 20 scored picks, nothing retained behind it. */
const WITHHELD = {
  rank: 4, manager_id: '459214359927713792', display_name: 'FantasyticBeast',
  cross_index: 118.7, composite: 1.25, n_leagues: 1, percentile: 99.8,
  flags: [],
  components: { draft: comp(20, 2.159), trade: comp(2, 0.471),
                waiver: comp(0, 0.0) },
  leagues: [
    { league_id: '1365183586830938112', name: 'National Thug League',
      season: '2026', n_teams: 12, index: 118.7, rank: 1,
      components: { draft: comp(20, 2.159), trade: comp(2, 0.471),
                    waiver: comp(0, 0.0) }, flags: [] }
  ]
};

const SHOWN = {
  rank: 1, manager_id: 'u1', display_name: 'pjstiehl', cross_index: 120.1,
  composite: 1.34, n_leagues: 1, percentile: 100.0, flags: [],
  components: { draft: comp(35, 1.04), trade: comp(9, 0.42),
                waiver: comp(12, -0.10) },
  leagues: [
    { league_id: 'L1', name: 'Dallas Kings', season: '2026', n_teams: 12,
      index: 115.2, rank: 2,
      components: { draft: comp(35, 1.04), trade: comp(9, 0.42),
                    waiver: comp(12, -0.10) }, flags: [] }
  ]
};

function corpusWith(leaderboard, draftBoard, gate) {
  return {
    schema: 'crossleague.corpus.v1',
    generated_at: '2026-09-22T12:00:00+00:00',
    first_indexed_at: '2026-09-01T12:00:00+00:00',
    n_runs: 22, persisted: true,
    coverage: { n_leagues: 211, n_managers: 2302,
                n_league_seasons_scored: 640, seasons: ['2026'] },
    components: { live: ['draft', 'trade', 'waiver'],
                  weights: { draft: 0.5, trade: 0.35, waiver: 0.15 },
                  min_draft_picks_for_board: 6 },
    leaderboard, draft_board: draftBoard, leagues: [], crawl: {},
    evidence_gate: gate
  };
}

/* The published artifact as apply_evidence_gate leaves it: the withheld row
 * is GONE from leaderboard/draft_board, and the counts remain. */
const GATED = corpusWith(
  [SHOWN],
  [{ rank: 1, manager_id: 'u1', display_name: 'pjstiehl', draft_z: 1.043,
     draft_zbar: 1.2, n_picks: 35, n_leagues: 1, percentile: 100.0 }],
  { applied: true, n_scored: 2302, n_shown: 1, n_withheld: 2301,
    n_draft_scored: 1676, n_draft_shown: 1, n_draft_withheld: 1675,
    why: 'a manager is listed only when at least one of their leagues\u2019 audits is available; withheld otherwise' }
);

/* What the page must do when NOTHING was retained: no filtering, and an
 * explicit admission rather than a silent full board. */
const UNGATED = corpusWith(
  [SHOWN, WITHHELD],
  [], 
  { applied: false, n_scored: 2302, n_shown: 2302, n_withheld: 0,
    why: 'no per-league audits were retained for this build' }
);

function reset() {
  for (const k of Object.keys(ELEMENTS)) delete ELEMENTS[k];
}

function html(id) { return (ELEMENTS[id] && ELEMENTS[id].innerHTML) || ''; }

/* ------------------------------------------------------------- the checks */

console.log('withheld managers are absent from the rendered board');
reset();
X.xlRenderLeaderboard(GATED);
X.xlRenderDraftBoard(GATED);
const board = html('xl-leaderboard') + html('xl-draft-board');

ok(board.indexOf('FantasyticBeast') === -1,
   'FantasyticBeast appears nowhere in the board markup', board.slice(0, 200));
ok(board.indexOf('459214359927713792') === -1,
   'nor does the withheld manager id (data attribute / detail panel id)');
ok(board.indexOf('National Thug League') === -1,
   'nor their league, which the collapsed detail panel would have carried');
ok(board.indexOf('pjstiehl') !== -1,
   'a manager WITH evidence still appears', board.slice(0, 200));

console.log('the page states how many were withheld');
ok(/2,301/.test(board),
   'the withheld count is rendered under the board', board.slice(-500));
ok(/skip them rather than being renumbered/.test(board),
   'and explains that ranks are not renumbered');

reset();
X.xlRenderCoverage(GATED);
const cov = html('xl-coverage');
ok(/Only managers whose score can be explained/.test(cov),
   'the coverage banner leads with the rule', cov.slice(0, 300));
ok(/2,301/.test(cov) && /2,302/.test(cov),
   'the banner carries both the shown and the scored totals');
ok(cov.indexOf('FantasyticBeast') === -1,
   'the banner names no withheld manager: this is not a shame list');

console.log('rank gaps are preserved, not renumbered');
reset();
X.xlRenderLeaderboard(GATED);
const rankCells = html('xl-leaderboard').match(/class="xl-rank">(\d+)</g) || [];
ok(rankCells.length === 1, 'one row rendered', rankCells.join(','));
ok(/>1</.test(rankCells[0] || ''),
   'the surviving row keeps its corpus rank');

console.log('an empty detail store fails open and admits it');
reset();
X.xlRenderCoverage(UNGATED);
X.xlRenderLeaderboard(UNGATED);
const ucov = html('xl-coverage');
ok(/Drill-down evidence is unavailable in this build/.test(ucov),
   'the page says evidence is unavailable for everyone', ucov.slice(0, 300));
ok(html('xl-leaderboard').indexOf('FantasyticBeast') !== -1,
   'and does not hide rows it has no basis to single out');
ok(!/were scored but are not listed/.test(html('xl-leaderboard')),
   'and claims no withholding it did not do');

console.log('a corpus with no gate block at all still renders');
reset();
const legacy = corpusWith([SHOWN], [], undefined);
delete legacy.evidence_gate;
X.xlRenderCoverage(legacy);
X.xlRenderLeaderboard(legacy);
ok(html('xl-coverage').length > 0,
   'an artifact predating the gate must not blank the banner');
ok(!/withheld/.test(html('xl-leaderboard')),
   'and must not claim a withheld count it does not have');

if (failures) {
  console.error('\ncrossleague evidence: ' + failures + ' of ' + checks + ' checks FAILED');
  process.exit(1);
}
console.log('\ncrossleague evidence: ' + checks + '/' + checks + ' checks passed');
