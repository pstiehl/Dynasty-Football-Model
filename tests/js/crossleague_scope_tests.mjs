// Drill-down detail panels: does clicking a row open *that table's* panel?
//
// The owner reported the cross-league drill-down broken three times. PR #74
// and PR #75 both claimed a fix and both failed in the browser, because the
// existing DOM stub auto-creates any element asked for by id and so cannot
// fail the two checks that matter:
//
//   1. #xl-draft-board emitted rows but no detail row at all, so
//      xlToggle's `if (!row) return;` silently did nothing.
//   2. Every draft-board manager is also a leaderboard manager, so one
//      unscoped `xl-detail-<id>` would be emitted twice in one document.
//      getElementById returns the first, so a click in one table toggles a
//      hidden row belonging to the other -- indistinguishable, from the
//      visitor's seat, from nothing happening.
//
// This suite runs the shipped page JS against tests/js/mini_dom.mjs, which
// parses the rendered markup and returns null for ids nobody emitted.
//
// Usage:
//   node crossleague_scope_tests.mjs <page.js> [corpus.json] [detail-dir]
//
// With no corpus it uses the built-in fixture, which reproduces the
// both-tables overlap, so CI runs it without network. Pass the real
// crossleague_corpus.json to run the identical assertions against the real
// corpus.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createDocument } from './mini_dom.mjs';

const argv = process.argv.slice(2);
const JS_PATH = argv[0];
const CORPUS_PATH = argv[1] || null;
const DETAIL_DIR = argv[2] || null;

if (!JS_PATH) {
  console.error('usage: node crossleague_scope_tests.mjs <page.js> [corpus.json] [detail-dir]');
  process.exit(2);
}

let checks = 0;
let failures = 0;
function ok(cond, label, detail) {
  checks++;
  if (cond) {
    console.log('  ok   ' + label);
  } else {
    failures++;
    console.error('  FAIL ' + label + (detail !== undefined ? '\n         got: ' + detail : ''));
  }
}
function section(name) { console.log('\n' + name); }

/* ------------------------------------------------------------- fixture */

function comp(n, z) { return { n, z, zbar: z, w: 1 }; }

function fixtureCorpus() {
  const league = (id, name) => ({
    league_id: id, name, season: '2026', n_teams: 12, index: 104.2, rank: 3,
    components: { draft: comp(9, 0.6), trade: comp(2, 0.1), waiver: comp(4, 0.2) },
    flags: []
  });
  const mk = (id, name, rank) => ({
    rank, manager_id: id, display_name: name, cross_index: 110 - rank,
    composite: 0.5, n_leagues: 1, percentile: 90, flags: [],
    components: { draft: comp(9, 0.6), trade: comp(2, 0.1), waiver: comp(4, 0.2) },
    leagues: [league('L1', 'Dallas Kings')]
  });
  return {
    coverage: { n_managers: 3, n_leagues: 2, n_league_seasons_scored: 4, seasons: ['2026'] },
    persisted: true, n_runs: 3, first_indexed_at: '2026-09-01T00:00:00+00:00',
    components: { min_draft_picks_for_board: 6 },
    // 'both' is in each table; 'lbonly' is leaderboard-only (below the pick
    // gate) and 'dbonly' is draft-board-only. All three cases must work.
    leaderboard: [mk('both', 'BothTables', 1), mk('lbonly', 'LeaderOnly', 2)],
    draft_board: [
      { rank: 1, manager_id: 'both', display_name: 'BothTables', draft_z: 1.1,
        draft_zbar: 1.2, n_picks: 30, n_leagues: 1, percentile: 99 },
      { rank: 2, manager_id: 'dbonly', display_name: 'DraftOnly', draft_z: 0.9,
        draft_zbar: 1.0, n_picks: 22, n_leagues: 1, percentile: 80 }
    ],
    leagues: [{ league_id: 'L1', name: 'Dallas Kings', season: '2026', n_teams: 12,
      n_seasons_scored: 2, n_managers: 12, scored_picks: 90, hop: 0 }],
    notes: []
  };
}

const corpus = CORPUS_PATH
  ? JSON.parse(fs.readFileSync(CORPUS_PATH, 'utf8'))
  : fixtureCorpus();

console.log('corpus: ' + (CORPUS_PATH ? CORPUS_PATH : '(built-in fixture)'));
console.log('  leaderboard entries: ' + (corpus.leaderboard || []).length);
console.log('  draft_board entries: ' + (corpus.draft_board || []).length);

/* ------------------------------------------------------ context set-up */

const doc = createDocument();
doc.mountContainers(['xl-coverage', 'xl-draft-board', 'xl-leaderboard',
  'xl-leagues', 'xl-results']);

const fetchLog = [];
function makeFetch() {
  return function fetchImpl(url) {
    const u = String(url);
    fetchLog.push(u);
    if (u === 'crossleague_corpus.json') {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(corpus) });
    }
    if (DETAIL_DIR && u.startsWith('managers/')) {
      const p = path.join(DETAIL_DIR, u.replace(/^managers\//, ''));
      if (fs.existsSync(p)) {
        const doc2 = JSON.parse(fs.readFileSync(p, 'utf8'));
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(doc2) });
      }
    }
    // 404 is the real shape of "detail not published yet".
    return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve(null) });
  };
}

const sandbox = {
  document: doc,
  console,
  fetch: makeFetch(),
  setTimeout,
  clearTimeout,
  Promise,
  Math,
  Number,
  String,
  JSON,
  Object,
  Array,
  isNaN,
  localStorage: {
    _v: {},
    getItem(k) { return Object.prototype.hasOwnProperty.call(this._v, k) ? this._v[k] : null; },
    setItem(k, v) { this._v[k] = String(v); }
  }
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
const ctx = vm.createContext(sandbox);

// Real page order: the highlights renderer is evaluated before the board
// script (PR #71). That order is also what puts the chip's click handler
// ahead of the row handler, which is what lets stopPropagation work.
const HL_PY = path.join(path.dirname(new URL(import.meta.url).pathname),
  '..', '..', 'src', 'dynasty', 'player_highlights.py');
let highlightsJs = null;
try {
  const py = fs.readFileSync(HL_PY, 'utf8');
  const m = /^PLAYER_HIGHLIGHTS_JS = r"""([\s\S]*?)"""$/m.exec(py);
  if (m) highlightsJs = m[1];
} catch (e) { /* fall through to the reported skip below */ }

if (highlightsJs) {
  vm.runInContext(highlightsJs, ctx, { timeout: 30000 });
}
vm.runInContext(fs.readFileSync(JS_PATH, 'utf8'), ctx, { timeout: 30000 });

/* --------------------------------------------------------------- helpers */

function rowsFor(containerId) {
  const box = doc.getElementById(containerId);
  return box ? box.querySelectorAll('tr.xl-row') : [];
}
function rowFor(containerId, managerId) {
  return rowsFor(containerId)
    .filter((r) => String(r.dataset.manager) === String(managerId))[0] || null;
}
function nameCellIn(row) {
  return row.querySelector('span.xl-name') || row;
}
function displayOf(id) {
  const el = doc.getElementById(id);
  return el ? (el.style.display === undefined ? '(unset)' : el.style.display) : '(no such element)';
}
const drain = () => new Promise((r) => setTimeout(r, 30));

/* =================================================================== */

await vm.runInContext('xlLoad()', ctx, { timeout: 60000 });
await drain();

section('render — the real corpus produced both tables');

const dbRows = rowsFor('xl-draft-board');
const lbRows = rowsFor('xl-leaderboard');
ok(dbRows.length === (corpus.draft_board || []).length,
   'draft board rendered one clickable row per draft_board entry',
   dbRows.length + ' rows for ' + (corpus.draft_board || []).length + ' entries');
ok(lbRows.length === (corpus.leaderboard || []).length,
   'leaderboard rendered one clickable row per leaderboard entry',
   lbRows.length + ' rows for ' + (corpus.leaderboard || []).length + ' entries');

ok(dbRows.every((r) => r.dataset.scope === 'db'),
   'every draft-board row carries data-scope="db"');
ok(lbRows.every((r) => r.dataset.scope === 'lb'),
   'every leaderboard row carries data-scope="lb"');

// colspan must match each table's own column count, or the panel breaks the
// table layout. Verified from the rendered <thead>, not from memory.
// Count only the outer table's own header cells. A descendant-wide 'th'
// sweep also picks up the per-league breakdown tables nested inside every
// detail cell, which is how this helper first reported 14 and 24 columns.
function headerCount(containerId) {
  const box = doc.getElementById(containerId);
  if (!box) return -1;
  const table = box.querySelector('table');
  if (!table) return -1;
  const thead = table.querySelector('thead');
  if (!thead) return -1;
  const tr = thead.querySelector('tr');
  if (!tr) return -1;
  return tr.children.filter((c) => c.localName === 'th').length;
}
const dbCols = headerCount('xl-draft-board');
const lbCols = headerCount('xl-leaderboard');
ok(dbCols === 6, 'draft board has 6 columns', String(dbCols));
ok(lbCols === 8, 'leaderboard has 8 columns', String(lbCols));

const dbDetailCells = doc.getElementById('xl-draft-board')
  .querySelectorAll('tr.xl-detail').map((tr) => tr.querySelector('td'));
const lbDetailCells = doc.getElementById('xl-leaderboard')
  .querySelectorAll('tr.xl-detail').map((tr) => tr.querySelector('td'));
ok(dbDetailCells.length === dbRows.length,
   'draft board emits one detail row per manager row',
   dbDetailCells.length + ' detail vs ' + dbRows.length + ' rows');
ok(dbDetailCells.every((td) => td.getAttribute('colspan') === '6'),
   'every draft-board detail cell spans 6 columns');
ok(lbDetailCells.every((td) => td.getAttribute('colspan') === '8'),
   'every leaderboard detail cell spans 8 columns');

section('fetch-on-open — nothing per-manager is fetched at page load');
ok(fetchLog.filter((u) => u.startsWith('managers/')).length === 0,
   'no detail artifact fetched before any row is clicked',
   JSON.stringify(fetchLog.slice(0, 4)));

/* ------------------------------------- pick subjects from the real data */

const lbIds = new Set((corpus.leaderboard || []).map((r) => String(r.manager_id)));
const bothIds = (corpus.draft_board || [])
  .map((r) => String(r.manager_id)).filter((id) => lbIds.has(id));
const dbOnlyIds = (corpus.draft_board || [])
  .map((r) => String(r.manager_id)).filter((id) => !lbIds.has(id));
const lbOnlyIds = [...lbIds].filter(
  (id) => !(corpus.draft_board || []).some((r) => String(r.manager_id) === id));

console.log('\nsubjects: ' + bothIds.length + ' managers in BOTH tables, ' +
  dbOnlyIds.length + ' draft-board-only, ' + lbOnlyIds.length + ' leaderboard-only');

const BOTH = process.env.XL_BOTH_ID && bothIds.includes(process.env.XL_BOTH_ID)
  ? process.env.XL_BOTH_ID : bothIds[0];
ok(!!BOTH, 'found a manager present in both tables to test with', String(BOTH));

section('(d) no element id appears twice anywhere in the document');
const dupes = doc._duplicateIds();
ok(dupes.length === 0,
   'the full rendered document contains no duplicate element id',
   dupes.length ? JSON.stringify(dupes.slice(0, 5)) : '0 duplicates across ' +
     doc._allIds().length + ' ids');

// Independent second opinion: count id="..." occurrences in the serialized
// page rather than trusting the index the parser built.
const pageHtml = doc.body.innerHTML;
const idCounts = new Map();
const idRe = /\sid="([^"]*)"/g;
let mm;
while ((mm = idRe.exec(pageHtml))) {
  idCounts.set(mm[1], (idCounts.get(mm[1]) || 0) + 1);
}
const textDupes = [...idCounts].filter(([, n]) => n > 1);
ok(textDupes.length === 0,
   'raw markup scan also finds no repeated id="..."',
   textDupes.length ? JSON.stringify(textDupes.slice(0, 5)) : String(idCounts.size) + ' ids scanned');

const dbRowId = `xl-detail-db-${BOTH}`;
const lbRowId = `xl-detail-lb-${BOTH}`;
ok(doc._idCount(dbRowId) === 1, 'the draft-board panel id exists exactly once', String(doc._idCount(dbRowId)));
ok(doc._idCount(lbRowId) === 1, 'the leaderboard panel id exists exactly once', String(doc._idCount(lbRowId)));
ok(doc.getElementById(`xl-detail-${BOTH}`) === null,
   'the old unscoped id is gone entirely (nothing can collide on it)');

section('(a) clicking a DRAFT-BOARD row opens THAT table\'s panel');
ok(displayOf(dbRowId) === 'none', 'draft-board panel starts closed (from the markup)', displayOf(dbRowId));
ok(displayOf(lbRowId) === 'none', 'leaderboard panel starts closed', displayOf(lbRowId));

const dbRow = rowFor('xl-draft-board', BOTH);
ok(!!dbRow, 'located the draft-board row for the subject');
const r1 = doc._fire('click', nameCellIn(dbRow));
ok(r1.errors.length === 0, 'the click handler did not throw', JSON.stringify(r1.errors));
ok(displayOf(dbRowId) === 'table-row',
   'the DRAFT-BOARD panel is now OPEN', displayOf(dbRowId));
ok(displayOf(lbRowId) === 'none',
   'the leaderboard panel for the same manager stayed closed', displayOf(lbRowId));

await drain();
const dbBody = doc.getElementById(`xl-detail-body-db-${BOTH}`);
ok(!!dbBody && dbBody.innerHTML.length > 0,
   'the draft-board panel has rendered content',
   dbBody ? dbBody.innerHTML.length + ' chars' : '(missing)');
ok(!!dbBody && dbBody.innerHTML.indexOf('Cannot read properties') < 0,
   'no TypeError text leaked into the draft-board panel');

section('(b) clicking a LEADERBOARD row opens THAT table\'s panel');
const lbRow = rowFor('xl-leaderboard', BOTH);
ok(!!lbRow, 'located the leaderboard row for the subject');
const r2 = doc._fire('click', nameCellIn(lbRow));
ok(r2.errors.length === 0, 'the click handler did not throw', JSON.stringify(r2.errors));
ok(displayOf(lbRowId) === 'table-row',
   'the LEADERBOARD panel is now OPEN', displayOf(lbRowId));
await drain();
const lbBody = doc.getElementById(`xl-detail-body-lb-${BOTH}`);
ok(!!lbBody && lbBody.innerHTML.length > 0,
   'the leaderboard panel has rendered content',
   lbBody ? lbBody.innerHTML.length + ' chars' : '(missing)');

section('(c) the two panels for one manager are independent');
ok(displayOf(dbRowId) === 'table-row' && displayOf(lbRowId) === 'table-row',
   'both panels can be open at once',
   'db=' + displayOf(dbRowId) + ' lb=' + displayOf(lbRowId));

doc._fire('click', nameCellIn(dbRow));
ok(displayOf(dbRowId) === 'none',
   'closing the draft-board panel closed it', displayOf(dbRowId));
ok(displayOf(lbRowId) === 'table-row',
   'closing the draft-board panel LEFT THE LEADERBOARD PANEL OPEN',
   displayOf(lbRowId));

doc._fire('click', nameCellIn(lbRow));
ok(displayOf(lbRowId) === 'none', 'closing the leaderboard panel closed it', displayOf(lbRowId));
ok(displayOf(dbRowId) === 'none', 'draft-board panel still closed', displayOf(dbRowId));

ok(doc.getElementById(dbRowId) !== doc.getElementById(lbRowId),
   'the two scoped ids resolve to two different elements');

section('one fetch, both panels repainted');
const detailUrl = vm.runInContext('xlDetailUrl(' + JSON.stringify(String(BOTH)) + ')', ctx);
ok(fetchLog.filter((u) => u === detailUrl).length >= 1,
   'opening a row fetched exactly that manager\'s shard', detailUrl);
// xlLoadDetail is keyed by manager, not by table; after it resolves both
// scopes must show the loaded document.
vm.runInContext('xlPaintDetail(' + JSON.stringify(String(BOTH)) + ')', ctx);
const dbTxt = doc.getElementById(`xl-detail-body-db-${BOTH}`).innerHTML;
const lbTxt = doc.getElementById(`xl-detail-body-lb-${BOTH}`).innerHTML;
ok(dbTxt.length > 0 && lbTxt.length > 0,
   'a scope-less repaint filled both panels',
   'db=' + dbTxt.length + ' lb=' + lbTxt.length);

section('absent scopes are a harmless no-op');
if (lbOnlyIds.length) {
  const only = lbOnlyIds[0];
  let threw = null;
  try {
    vm.runInContext('xlPaintDetail(' + JSON.stringify(String(only)) + ')', ctx);
    vm.runInContext('xlToggle(' + JSON.stringify(String(only)) + ', "db")', ctx);
  } catch (e) { threw = String((e && e.message) || e); }
  ok(threw === null,
     'painting/toggling the db scope of a leaderboard-only manager does not throw', String(threw));
  ok(doc.getElementById('xl-detail-db-' + only) === null,
     'that manager genuinely has no draft-board panel (so the no-op was real)');
} else {
  ok(true, 'no leaderboard-only manager in this corpus to test (skipped)');
}
if (dbOnlyIds.length) {
  const only = dbOnlyIds[0];
  let threw = null;
  try {
    vm.runInContext('xlToggle(' + JSON.stringify(String(only)) + ', "lb")', ctx);
  } catch (e) { threw = String((e && e.message) || e); }
  ok(threw === null, 'toggling the lb scope of a draft-board-only manager does not throw', String(threw));
} else {
  ok(true, 'no draft-board-only manager in this corpus (all are on the leaderboard)');
}

section('keyboard parity — Enter and Space');
const kRow = rowFor('xl-draft-board', BOTH);
doc._fire('keydown', kRow, { key: 'Enter' });
ok(displayOf(dbRowId) === 'table-row', 'Enter opened the draft-board panel', displayOf(dbRowId));
doc._fire('keydown', kRow, { key: 'Enter' });
ok(displayOf(dbRowId) === 'none', 'Enter closed it again', displayOf(dbRowId));
doc._fire('keydown', kRow, { key: ' ' });
ok(displayOf(dbRowId) === 'table-row', 'Space opened it', displayOf(dbRowId));
doc._fire('keydown', kRow, { key: ' ' });
ok(displayOf(dbRowId) === 'none', 'Space closed it', displayOf(dbRowId));

const kLbRow = rowFor('xl-leaderboard', BOTH);
doc._fire('keydown', kLbRow, { key: 'Enter' });
ok(displayOf(lbRowId) === 'table-row',
   'Enter on the leaderboard row opened the LEADERBOARD panel', displayOf(lbRowId));
ok(displayOf(dbRowId) === 'none', 'and not the draft-board one', displayOf(dbRowId));
doc._fire('keydown', kLbRow, { key: 'Enter' });

section('chips still do not toggle the row they sit in');
if (!highlightsJs) {
  ok(false, 'could not load PLAYER_HIGHLIGHTS_JS, chip check not run');
} else {
  // Open a panel so its chips exist, then press one.
  doc._fire('click', nameCellIn(lbRow));
  await drain();
  vm.runInContext('xlPaintDetail(' + JSON.stringify(String(BOTH)) + ')', ctx);
  const openBody = doc.getElementById(`xl-detail-body-lb-${BOTH}`);
  const chips = openBody ? openBody.querySelectorAll('[data-dfm-player]') : [];
  if (!chips.length) {
    console.log('  note: this manager\'s panel rendered no player chips; ' +
                'asserting the mechanism on a synthetic chip instead');
    const probe = doc.createElement('button');
    probe.setAttribute('data-dfm-player', '{"n":"Probe"}');
    lbRow.appendChild(probe);
    const before = displayOf(lbRowId);
    const rc = doc._fire('click', probe);
    ok(rc.propagationStopped, 'the chip handler called stopPropagation');
    ok(displayOf(lbRowId) === before,
       'pressing a chip inside a row did not toggle that row',
       'before=' + before + ' after=' + displayOf(lbRowId));
  } else {
    const before = displayOf(lbRowId);
    const rc = doc._fire('click', chips[0]);
    ok(rc.propagationStopped, 'the chip handler called stopPropagation');
    ok(displayOf(lbRowId) === before,
       'pressing a real film chip inside the panel did not toggle the row',
       'before=' + before + ' after=' + displayOf(lbRowId));
  }
}

section('the harness itself can still see the original bug');
// A guard on the guard: if mini_dom ever starts auto-creating elements the
// way dom_stub does, every check above turns into a tautology.
ok(doc.getElementById('xl-detail-db-definitely-not-a-manager') === null,
   'getElementById returns null for an id nobody rendered');
ok(doc._handlerCount('click') >= 1, 'a delegated click handler is installed',
   String(doc._handlerCount('click')));

/* ------------------------------------------------------------- report */

console.log('');
if (failures) {
  console.error(failures + ' of ' + checks + ' checks FAILED');
  process.exit(1);
}
console.log('crossleague scope: ' + checks + '/' + checks + ' checks passed');
