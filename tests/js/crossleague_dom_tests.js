/* Renderer assertions for the cross-league Manager Score board.
 *
 * Run by tests/test_crossleague.py, which writes
 * dynasty.crossleague_js.CROSSLEAGUE_JS to a temp file and passes the path as
 * argv[2]:
 *
 *   node tests/js/crossleague_dom_tests.js /tmp/xl.js
 *
 * What this proves: the renderers turn a corpus artifact into the honest
 * strings and the right ordering, escape what they interpolate, and degrade
 * to an explanation rather than an empty table when the artifact is missing.
 *
 * What it cannot prove: that any of it *looks* right. There is no browser and
 * no layout engine in this environment, so rendering is unverified by eye.
 *
 * Exits 0 on success, 1 with a report on first failure.
 */
'use strict';

const jsPath = process.argv[2];
if (!jsPath) {
  console.error('usage: node crossleague_dom_tests.js <crossleague-js-path>');
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

/* ------------------------------------------------------------- mini DOM */

const ELEMENTS = {};
function makeEl(id) {
  return {
    id: id, innerHTML: '', textContent: '', className: '', style: {},
    dataset: {}, _listeners: [],
    classList: { add() {}, remove() {}, contains() { return false; } },
    addEventListener(ev, fn) { this._listeners.push([ev, fn]); },
    appendChild() {}, scrollIntoView() {},
    querySelector() { return null; },
    querySelectorAll(sel) {
      /* Only the one selector the renderer uses. Rows are synthesised from
       * the HTML just written, so the click wiring can be exercised. */
      if (sel !== 'tr.xl-row') return [];
      const ids = [];
      const re = /data-manager="([^"]+)"/g;
      let m;
      while ((m = re.exec(this.innerHTML)) !== null) ids.push(m[1]);
      return ids.map((mid) => {
        const el = makeEl('row-' + mid);
        el.dataset = { manager: mid };
        return el;
      });
    }
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

const X = require(jsPath);

/* ------------------------------------------------------------- fixtures */

function comp(n, z) { return { n: n, z: z, zbar: z, available: n > 0 }; }

const CORPUS = {
  schema: 'crossleague.corpus.v1',
  generated_at: '2026-09-21T12:00:00+00:00',
  first_indexed_at: '2026-09-01T12:00:00+00:00',
  n_runs: 21,
  persisted: true,
  coverage: {
    n_leagues: 28, n_managers: 340, n_league_seasons_scored: 76,
    seasons: ['2026']
  },
  components: { live: ['draft', 'trade', 'waiver'],
                weights: { draft: 0.5, trade: 0.35, waiver: 0.15 },
                min_draft_picks_for_board: 6 },
  leaderboard: [
    {
      rank: 1, manager_id: 'u1', display_name: 'pjstiehl', cross_index: 118.4,
      composite: 1.226, n_leagues: 2, percentile: 100.0, flags: [],
      components: { draft: comp(35, 1.04), trade: comp(9, 0.42),
                    waiver: comp(12, -0.10) },
      leagues: [
        { league_id: 'L1', name: 'Dallas Kings', season: '2026', n_teams: 12,
          index: 115.2, rank: 2,
          components: { draft: comp(35, 1.04), trade: comp(9, 0.42),
                        waiver: comp(12, -0.10) }, flags: [] },
        { league_id: 'L2', name: 'DLD', season: '2026', n_teams: 12,
          index: 109.0, rank: 4,
          components: { draft: comp(12, 0.55), trade: comp(2, 0.10),
                        waiver: comp(3, 0.0) }, flags: [] }
      ]
    },
    {
      rank: 2, manager_id: 'u2', display_name: 'SimpsDontCry',
      cross_index: 96.1, composite: -0.26, n_leagues: 1, percentile: 50.0,
      flags: ['only 4 scored picks across all indexed leagues — heavily shrunk'],
      components: { draft: comp(4, -0.35), trade: comp(0, 0.0),
                    waiver: comp(5, 0.2) },
      leagues: [
        { league_id: 'L1', name: 'Dallas Kings', season: '2026', n_teams: 12,
          index: 97.0, rank: 9,
          components: { draft: comp(4, -0.35), trade: comp(0, 0.0),
                        waiver: comp(5, 0.2) }, flags: [] }
      ]
    }
  ],
  draft_board: [
    { rank: 1, manager_id: 'u1', display_name: 'pjstiehl', draft_z: 1.043,
      draft_zbar: 1.2, n_picks: 35, n_leagues: 2, percentile: 100.0 }
  ],
  leagues: [
    { league_id: 'L1', name: 'Dallas Kings', season: '2026', n_teams: 12,
      n_seasons_scored: 4, n_managers: 14, scored_picks: 187, hop: 0 },
    { league_id: 'L2', name: 'DLD', season: '2026', n_teams: 12,
      n_seasons_scored: 1, n_managers: 12, scored_picks: 48, hop: 1 }
  ],
  notes: []
};

/* ------------------------------------------------------ coverage claims */

ok(X.xlCoverageSentence(CORPUS) ===
   "ranked against 340 managers across 28 dynasty leagues we've indexed",
   'coverage sentence matches the phrasing the owner asked for',
   X.xlCoverageSentence(CORPUS));

ok(X.xlCoverageSentence({ coverage: { n_managers: 1, n_leagues: 1 } }) ===
   "ranked against 1 manager across 1 dynasty league we've indexed",
   'coverage sentence is singular for one');

/* The claim that must never appear. */
const cov = X.xlCoverageSentence(CORPUS).toLowerCase();
['all of sleeper', 'every league', 'entire', 'complete'].forEach(function (bad) {
  ok(cov.indexOf(bad) === -1, 'coverage sentence avoids "' + bad + '"');
});

X.xlRenderCoverage(CORPUS);
const covHtml = document.getElementById('xl-coverage').innerHTML;
ok(covHtml.indexOf('340 managers across 28 dynasty leagues') >= 0,
   'coverage banner states the indexed counts');
ok(covHtml.indexOf('not all of') >= 0 || covHtml.indexOf('not a census') >= 0,
   'coverage banner explicitly disclaims full Sleeper coverage');
ok(covHtml.indexOf('76 league-seasons') >= 0,
   'coverage banner reports league-seasons scored');
ok(covHtml.indexOf('Indexed since 2026-09-01') >= 0,
   'coverage banner reports provenance when persisted');

/* ---------------------------------------------- degradation: no artifact */

X.xlRenderCoverage(null);
const missing = document.getElementById('xl-coverage').innerHTML;
ok(missing.indexOf('crossleague_corpus.json') >= 0,
   'missing corpus names the artifact it needs');
ok(missing.indexOf('nothing to rank') >= 0,
   'missing corpus explains itself rather than rendering an empty table');

/* ---------------------------------------- degradation: not persisted yet */

const notPersisted = JSON.parse(JSON.stringify(CORPUS));
notPersisted.persisted = false;
const prov = X.xlProvenanceSentence(notPersisted);
ok(prov.indexOf('this build only') >= 0,
   'un-persisted corpus degrades to "indexed in this build only"', prov);
ok(prov.indexOf('does not grow') >= 0,
   'un-persisted corpus says it is not accumulating');
ok(X.xlProvenanceSentence(CORPUS).indexOf('Indexed since') >= 0,
   'persisted corpus reports since-date and run count');

/* -------------------------------------------------------- draft board */

X.xlRenderDraftBoard(CORPUS);
const draftHtml = document.getElementById('xl-draft-board').innerHTML;
ok(draftHtml.indexOf('pjstiehl') >= 0, 'draft board lists the top drafter');
ok(draftHtml.indexOf('+1.043') >= 0, 'draft board shows a signed draft score');
ok(draftHtml.indexOf('>35<') >= 0, 'draft board shows the pick count');
ok(draftHtml.indexOf('6+ scored picks') >= 0,
   'draft board states its entry requirement');

X.xlRenderDraftBoard({ draft_board: [], components: { min_draft_picks_for_board: 6 } });
ok(document.getElementById('xl-draft-board').innerHTML.indexOf('No manager') >= 0,
   'empty draft board explains the gate instead of rendering nothing');

/* --------------------------------------------------------- leaderboard */

X.xlRenderLeaderboard(CORPUS);
const lbHtml = document.getElementById('xl-leaderboard').innerHTML;
ok(lbHtml.indexOf('118.4') >= 0, 'leaderboard shows the cross-league score');
ok(lbHtml.indexOf('pjstiehl') < lbHtml.indexOf('SimpsDontCry'),
   'leaderboard is ordered best-first');
ok(lbHtml.indexOf('heavily shrunk') >= 0,
   'leaderboard surfaces the low-evidence flag');
ok(lbHtml.indexOf('data-manager="u1"') >= 0,
   'leaderboard rows carry the manager id for the detail toggle');

/* ---------------------------------------------- per-league auditability */

const bd = X.xlRenderBreakdown(CORPUS.leaderboard[0]);
ok(bd.indexOf('Dallas Kings') >= 0 && bd.indexOf('DLD') >= 0,
   'breakdown lists every indexed league behind the score');
ok(bd.indexOf('115.2') >= 0, 'breakdown shows the per-league score');
ok(bd.indexOf('35 scored picks') >= 0,
   'breakdown totals the evidence the score rests on');
ok(X.xlRenderBreakdown({ leagues: [] }).indexOf('No per-league detail') >= 0,
   'breakdown with no leagues says so');

/* ------------------------------------------------------- corpus leagues */

X.xlRenderLeagues(CORPUS);
const lgHtml = document.getElementById('xl-leagues').innerHTML;
ok(lgHtml.indexOf('Dallas Kings') >= 0, 'league table lists indexed leagues');
ok(lgHtml.indexOf('>4<') >= 0, 'league table shows seasons scored per league');
ok(lgHtml.indexOf('Hops from seed') >= 0,
   'league table discloses crawl distance');

/* ------------------------------------------------------------ escaping */

const nasty = JSON.parse(JSON.stringify(CORPUS));
nasty.leaderboard[0].display_name = '<img src=x onerror=alert(1)>';
nasty.leaderboard[0].leagues[0].name = '"><script>alert(2)</script>';
X.xlRenderLeaderboard(nasty);
const esc = document.getElementById('xl-leaderboard').innerHTML;
ok(esc.indexOf('<img src=x') === -1, 'display_name is escaped');
ok(esc.indexOf('&lt;img src=x') >= 0, 'display_name is escaped to entities');
ok(esc.indexOf('<script>alert(2)') === -1, 'league name is escaped');

/* A Sleeper handle is untrusted input rendered on a public page; these two
 * assertions are the whole reason xlEsc exists. */

/* ------------------------------------------------------ no shaming board */

X.xlRenderAll(CORPUS);
const all = [
  document.getElementById('xl-coverage').innerHTML,
  document.getElementById('xl-draft-board').innerHTML,
  document.getElementById('xl-leaderboard').innerHTML,
  document.getElementById('xl-leagues').innerHTML
].join(' ').toLowerCase();
['worst manager', 'shame', 'biggest loser'].forEach(function (bad) {
  ok(all.indexOf(bad) === -1, 'page never renders a "' + bad + '" framing');
});

/* --------------------------------------------------------- detail toggle */

X.xlRenderLeaderboard(CORPUS);
X.xlRenderDraftBoard(CORPUS);

/* Detail ids are scoped per table, because u1 here -- like all 1,248
 * draft-board managers in the live corpus -- appears on both boards, and one
 * shared `xl-detail-u1` would be emitted twice in one document.
 *
 * Note the limit of this file: dom_stub.js auto-creates an element on every
 * getElementById, so these checks can only assert that xlToggle drives the
 * id it claims to. Whether that element is actually present in the rendered
 * markup -- the thing that was broken -- needs a DOM that can return null,
 * and is asserted in tests/js/crossleague_scope_tests.mjs. */
const lbDetail = document.getElementById('xl-detail-lb-u1');
lbDetail.style.display = 'none';
X.xlToggle('u1', 'lb');
ok(lbDetail.style.display === 'table-row', 'toggle opens the leaderboard breakdown row');
X.xlToggle('u1', 'lb');
ok(lbDetail.style.display === 'none', 'toggle closes the leaderboard breakdown row');

const dbDetail = document.getElementById('xl-detail-db-u1');
ok(dbDetail !== lbDetail,
   'one manager on both boards gets two distinct detail elements');
dbDetail.style.display = 'none';
X.xlToggle('u1', 'db');
ok(dbDetail.style.display === 'table-row', 'toggle opens the draft-board breakdown row');
ok(lbDetail.style.display === 'none',
   'opening the draft-board panel left the leaderboard panel closed');
X.xlToggle('u1', 'db');
ok(dbDetail.style.display === 'none', 'toggle closes the draft-board breakdown row');

/* A scope-less call still means the leaderboard, which is what it meant
 * before scoping existed. */
lbDetail.style.display = 'none';
X.xlToggle('u1');
ok(lbDetail.style.display === 'table-row', 'a scope-less xlToggle still drives the leaderboard');
X.xlToggle('u1');

X.xlToggle('does-not-exist');
ok(true, 'toggling an unknown manager does not throw');

/* The markup must actually carry what the delegated handler reads. */
const dbMarkup = document.getElementById('xl-draft-board').innerHTML;
ok(dbMarkup.indexOf('data-scope="db"') >= 0, 'draft-board rows carry data-scope="db"');
ok(dbMarkup.indexOf('id="xl-detail-db-u1"') >= 0, 'draft board emits a scoped detail row');
ok(dbMarkup.indexOf('colspan="6"') >= 0, 'draft-board detail cell spans its own 6 columns');
ok(dbMarkup.indexOf('colspan="8"') === -1,
   'draft-board detail cell does not borrow the leaderboard\'s 8 columns');

const lbMarkup = document.getElementById('xl-leaderboard').innerHTML;
ok(lbMarkup.indexOf('data-scope="lb"') >= 0, 'leaderboard rows carry data-scope="lb"');
ok(lbMarkup.indexOf('id="xl-detail-lb-u1"') >= 0, 'leaderboard emits a scoped detail row');
ok(lbMarkup.indexOf('id="xl-detail-u1"') === -1,
   'the old unscoped detail id is gone from the leaderboard');

/* ------------------------------------------------------------- report */

if (failures) {
  console.error('\n' + failures + ' of ' + checks + ' checks FAILED');
  process.exit(1);
}
console.log('crossleague dom: ' + checks + '/' + checks + ' checks passed');
