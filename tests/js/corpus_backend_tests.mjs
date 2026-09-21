/* Corpus backend — worker contract assertions.
 *
 * Run by tests/test_corpus_backend.py:
 *
 *   node tests/js/corpus_backend_tests.mjs <core-js> <worker.js> <migration.sql>
 *
 * Exits 0 on success, 1 with a report on the first failures.
 *
 * Why this file scores a league instead of hand-writing one
 * ---------------------------------------------------------
 * The worker's central defence is that a submission's numbers must be
 * internally consistent with the scoring model: shrunk == mean*n/(n+k), each
 * component's z-scores distributed mean 0 / sd 1, composite == the weighted
 * z sum, index == 100 + 15*composite. Testing that against numbers I typed
 * by hand would prove only that I can satisfy my own checker.
 *
 * So the happy-path fixture is produced by running the REAL scorer --
 * dynasty.managerscore_js.MANAGERSCORE_CORE_JS, the same code the browser
 * runs -- over a synthetic league, and converting its output exactly as
 * dynasty.corpus_submit_js does. If the worker and the shipped scorer ever
 * disagree about what a valid submission looks like, this fails.
 *
 * The D1 shim
 * -----------
 * `node:sqlite` backs a binding with D1's prepare/bind/first/all/run/batch
 * surface, and the REAL migration SQL is applied to it. So the HTTP-contract
 * tests exercise the worker's actual statements against actual SQLite rather
 * than a mock that agrees with me.
 */

import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { DatabaseSync } from 'node:sqlite';

const require = createRequire(import.meta.url);

const corePath = process.argv[2];
const workerPath = process.argv[3];
const migrationPath = process.argv[4];
if (!corePath || !workerPath || !migrationPath) {
  console.error('usage: node corpus_backend_tests.mjs <core-js> <worker.js> <migration.sql>');
  process.exit(2);
}

const C = require(corePath);
const workerMod = await import(workerPath);
const worker = workerMod.default;
const T = workerMod.__test;

let failures = 0;
let checks = 0;
const failed = [];

function ok(cond, label, detail) {
  checks++;
  if (!cond) {
    failures++;
    failed.push(label + (detail ? '  [' + detail + ']' : ''));
    console.error('FAIL: ' + label + (detail ? '  [' + detail + ']' : ''));
  }
}
function near(a, b, label, eps) {
  ok(Math.abs(a - b) < (eps == null ? 1e-9 : eps), label, a + ' vs ' + b);
}

// ===========================================================================
// A genuine scored league, from the real scorer
// ===========================================================================

const MGR_IDS = [
  '860000000000000001', '860000000000000002', '860000000000000003',
  '860000000000000004', '860000000000000005', '860000000000000006',
  '860000000000000007', '860000000000000008', '860000000000000009',
  '860000000000000010', '860000000000000011', '860000000000000012',
];
const LEAGUE_ID = '1316222126914539520';

const managers = MGR_IDS.map((id, i) => ({ id, name: 'manager' + (i + 1) }));

/* A draft with enough priced picks for the slot-cost curve to fit, with
 * deliberately uneven capture so managers separate. Six rounds, so every
 * manager clears MIN_DRAFT_PICKS_FOR_BOARD (6) and the draft board is
 * actually exercised rather than trivially empty. */
const picks = [];
for (let round = 0; round < 6; round++) {
  for (let i = 0; i < MGR_IDS.length; i++) {
    const slot = round * MGR_IDS.length + i + 1;
    picks.push({
      managerId: MGR_IDS[i],
      slot,
      name: 'Player ' + slot,
      pos: 'WR',
      evaluable: true,
      // base value decays with slot; a few managers beat their slot badly
      capture: Math.max(200, 9000 - slot * 260) + (i % 4 === 0 ? 1400 : 0)
                 - (i % 3 === 0 ? 700 : 0),
      vAt: 1000,
      peak: 2000,
    });
  }
}

function asset(label, cap) {
  return { kind: 'player', label, evaluable: true, capture: cap,
           vAt: 1000, peak: 1000 + cap };
}
const trades = [
  { id: 't1', date: '2026-03-01', sides: [
    { managerId: MGR_IDS[0], received: [asset('Stud', 9000)], given: [asset('Depth', 4000)] },
    { managerId: MGR_IDS[1], received: [asset('Depth', 4000)], given: [asset('Stud', 9000)] },
  ] },
  { id: 't2', date: '2026-04-01', sides: [
    { managerId: MGR_IDS[2], received: [asset('Wr1', 6000)], given: [asset('Rb2', 3000)] },
    { managerId: MGR_IDS[3], received: [asset('Rb2', 3000)], given: [asset('Wr1', 6000)] },
  ] },
  { id: 't3', date: '2026-05-01', sides: [
    { managerId: MGR_IDS[4], received: [asset('Qb1', 7000)], given: [asset('Te1', 5000)] },
    { managerId: MGR_IDS[5], received: [asset('Te1', 5000)], given: [asset('Qb1', 7000)] },
  ] },
];

const waivers = [];
for (let i = 0; i < MGR_IDS.length; i++) {
  for (let k = 0; k < 2; k++) {
    waivers.push({
      managerId: MGR_IDS[i], date: '2026-09-0' + (k + 1),
      name: 'Waiver ' + i + '-' + k, pos: 'RB',
      evaluable: true, capture: (i * 137 + k * 91) % 900,
      vAt: 100, peak: 300,
    });
  }
}

const scored = C.msScoreLeague({ managers, drafts: [{
  id: 'd1', season: '2026', label: '2026 Startup', picks,
}], trades, waivers }, {});

ok(scored.managers.length === 12, 'fixture: real scorer produced 12 managers');
ok(scored.managers.some((m) => m.draft.n > 0), 'fixture: draft component has evidence');
ok(scored.managers.some((m) => m.trade.n > 0), 'fixture: trade component has evidence');
ok(scored.managers.some((m) => m.waiver.n > 0), 'fixture: waiver component has evidence');

/* The exact conversion dynasty.corpus_submit_js performs in the browser. */
function toSubmission(result, overrides) {
  const comps = ['draft', 'trade', 'waiver'];
  return Object.assign({
    schema: T.constants.SUBMISSION_SCHEMA,
    league: {
      league_id: LEAGUE_ID,
      name: 'Dallas Kings',
      season: '2026',
      n_teams: 12,
      n_seasons_scored: 1,
      lineage_ids: [LEAGUE_ID],
      lineage_root: LEAGUE_ID,
    },
    managers: result.managers.map((m) => {
      const out = {
        manager_id: String(m.id),
        display_name: String(m.name),
        index: m.index,
        rank: m.rank,
        composite: m.composite,
        components: {},
        flags: (m.flags || []).slice(0, 8),
      };
      comps.forEach((c) => {
        out.components[c] = {
          n: m[c].n, z: m[c].z, mean: m[c].mean, shrunk: m[c].shrunk,
        };
      });
      return out;
    }),
  }, overrides || {});
}

const GOOD = toSubmission(scored);

// ===========================================================================
// Stage 1 — schema validation
// ===========================================================================

const v = T.validateSubmission(GOOD);
ok(v.ok, 'validate: a genuinely scored league passes', (v.errors || []).join('; '));
ok(v.value && v.value.managers.length === 12, 'validate: all 12 managers survive');

function rejects(payload, label) {
  const r = T.validateSubmission(payload);
  ok(!r.ok, 'validate rejects: ' + label,
     r.ok ? 'ACCEPTED' : '');
  return r;
}

const clone = (o) => JSON.parse(JSON.stringify(o));

rejects(null, 'null payload');
rejects([], 'array payload');
rejects({}, 'empty object');
rejects(Object.assign(clone(GOOD), { schema: 'something.else' }), 'wrong schema version');

let bad = clone(GOOD); bad.league.league_id = 'abc';
rejects(bad, 'non-numeric league id');
bad = clone(GOOD); bad.league.league_id = "1' OR 1=1 --";
rejects(bad, 'sql-looking league id');
bad = clone(GOOD); bad.league.n_teams = 500;
rejects(bad, 'absurd team count');
bad = clone(GOOD); bad.league.season = 'soon';
rejects(bad, 'non-year season');
bad = clone(GOOD); bad.managers = [];
rejects(bad, 'no managers');
bad = clone(GOOD); bad.managers = bad.managers.slice(0, 1);
rejects(bad, 'one manager cannot define a distribution');
bad = clone(GOOD); bad.managers[3].manager_id = 'roster:123:4';
rejects(bad, 'synthetic unowned-roster id');
bad = clone(GOOD); bad.managers[3].manager_id = bad.managers[2].manager_id;
rejects(bad, 'duplicate manager id');
bad = clone(GOOD); bad.managers[0].index = 'lots';
rejects(bad, 'non-numeric index');
bad = clone(GOOD); bad.managers[0].index = Infinity;
rejects(bad, 'infinite index');
bad = clone(GOOD); delete bad.managers[0].components.trade;
rejects(bad, 'missing component');
bad = clone(GOOD); bad.managers[0].components.draft.n = -5;
rejects(bad, 'negative n');
bad = clone(GOOD); bad.managers[0].components.draft.z = 99;
rejects(bad, 'z beyond the plausible band');
bad = clone(GOOD); bad.league.lineage_ids = new Array(80).fill(LEAGUE_ID);
rejects(bad, 'oversized lineage chain');
bad = clone(GOOD); bad.managers = bad.managers.slice(0, 6);
rejects(bad, 'half the rosters missing');

/* Unknown extra keys are dropped, not rejected: an older or newer site build
 * must not be a hard failure. */
const extra = clone(GOOD);
extra.managers[0].favourite_colour = 'blue';
extra.league.secret = { nested: true };
const ev = T.validateSubmission(extra);
ok(ev.ok, 'validate: unknown keys are tolerated');
ok(ev.value.managers[0].favourite_colour === undefined,
   'validate: unknown keys are dropped from the normalised value');
ok(ev.value.league.secret === undefined,
   'validate: unknown league keys are dropped');

/* Nothing is executed, and control characters do not survive. */
const nasty = clone(GOOD);
nasty.league.name = 'Evil\u0000<script>alert(1)</script>\nLeague';
const nv = T.validateSubmission(nasty);
ok(nv.ok, 'validate: hostile name does not crash');
ok(nv.value.league.name.indexOf('\u0000') === -1, 'validate: NUL stripped');
ok(nv.value.league.name.indexOf('\n') === -1, 'validate: newline stripped');

const longName = clone(GOOD);
longName.league.name = 'x'.repeat(5000);
const lv = T.validateSubmission(longName);
ok(lv.ok && lv.value.league.name.length <= 120, 'validate: long name truncated');

// ===========================================================================
// Stage 2 — internal consistency
// ===========================================================================

const cons = T.checkConsistency(v.value);
ok(cons.ok, 'consistency: real scorer output is self-consistent',
   (cons.errors || []).join('; '));

function inconsistent(mutate, label) {
  const p = clone(GOOD);
  mutate(p);
  const parsed = T.validateSubmission(p);
  if (!parsed.ok) { ok(true, 'consistency (rejected earlier): ' + label); return; }
  const r = T.checkConsistency(parsed.value);
  ok(!r.ok, 'consistency rejects: ' + label, r.ok ? 'ACCEPTED' : '');
}

/* The core fabrication case the owner asked about: a user inflates their own
 * score and posts it. */
inconsistent((p) => { p.managers[0].index = 180; },
             'a manager inflates only their index');
inconsistent((p) => { p.managers[0].composite = 5; },
             'a manager inflates only their composite');
inconsistent((p) => { p.managers[0].composite = 5; p.managers[0].index = 100 + 15 * 5; },
             'index and composite inflated together but z left alone');
inconsistent((p) => { p.managers[0].components.draft.z = 3.5; },
             'a z bumped without the distribution following');
inconsistent((p) => { p.managers[0].components.draft.shrunk *= 2; },
             'shrunk no longer equals mean*n/(n+k)');
inconsistent((p) => { p.managers[0].components.draft.mean *= 3; },
             'mean changed without shrunk following');
inconsistent((p) => {
  // every z scaled: distribution keeps mean 0 but sd is no longer 1
  p.managers.forEach((m) => { m.components.draft.z *= 1.5; });
}, 'all draft z inflated so sd != 1');
inconsistent((p) => {
  p.managers.forEach((m) => { m.components.draft.z += 0.5; });
}, 'all draft z shifted so mean != 0');
inconsistent((p) => {
  const m = p.managers.find((x) => x.components.trade.n === 0);
  if (m) m.components.trade.z = 2.0;
}, 'a manager with no evidence claims a z');
inconsistent((p) => { p.managers[0].rank = 12; p.managers[11].rank = 1; },
             'ranks do not follow index order');

/* A wholesale fabrication: plausible-looking numbers invented from nothing. */
const forged = {
  schema: T.constants.SUBMISSION_SCHEMA,
  league: clone(GOOD.league),
  managers: MGR_IDS.map((id, i) => ({
    manager_id: id, display_name: 'm' + i,
    index: 130 - i, rank: i + 1, composite: (130 - i - 100) / 15,
    components: {
      draft: { n: 10, z: 1.5 - i * 0.2, mean: 100, shrunk: 62.5 },
      trade: { n: 2, z: 1.0 - i * 0.15, mean: 50, shrunk: 20 },
      waiver: { n: 3, z: 0.5 - i * 0.1, mean: 30, shrunk: 11.25 },
    },
    flags: [],
  })),
};
const fp = T.validateSubmission(forged);
ok(fp.ok, 'forged payload is schema-valid (so schema alone is not the defence)');
const fc = T.checkConsistency(fp.value);
ok(!fc.ok, 'consistency rejects a wholesale fabrication',
   fc.ok ? 'ACCEPTED' : '');

// ===========================================================================
// Stage 3 — Sleeper verification (injected fetch, no network)
// ===========================================================================

function sleeperStub(overrides) {
  const o = overrides || {};
  const users = o.users || MGR_IDS.map((id, i) => ({
    user_id: id, display_name: 'manager' + (i + 1),
  }));
  const rosters = o.rosters || MGR_IDS.map((id, i) => ({
    roster_id: i + 1, owner_id: id,
  }));
  const league = Object.assign({
    league_id: LEAGUE_ID, name: 'Dallas Kings', season: '2026',
    total_rosters: 12, settings: { type: 2 },
  }, o.league || {});

  const calls = [];
  const fetchImpl = async (url) => {
    calls.push(url);
    if (o.throwOn && url.includes(o.throwOn)) throw new Error('network down');
    let body = null;
    if (/\/league\/\d+$/.test(url)) body = league;
    else if (url.endsWith('/users')) body = users;
    else if (url.endsWith('/rosters')) body = rosters;
    if (o.status && o.status !== 200) {
      return { ok: false, status: o.status, json: async () => null };
    }
    return { ok: true, status: 200, json: async () => body };
  };
  return { fetchImpl, calls };
}

{
  const s = sleeperStub();
  const r = await T.verifyAgainstSleeper(v.value, s.fetchImpl);
  ok(r.ok, 'verify: a real dynasty league passes', (r.errors || []).join('; '));
  ok(r.external === 3, 'verify: costs exactly 3 external subrequests',
     String(r.external));
  ok(s.calls.length === 3, 'verify: made exactly 3 calls');
}
{
  const s = sleeperStub({ league: { settings: { type: 0 } } });
  const r = await T.verifyAgainstSleeper(v.value, s.fetchImpl);
  ok(!r.ok, 'verify: a redraft league (type 0) is refused');
  ok(r.errors.join(' ').includes('not 2'), 'verify: says why it was refused');
}
{
  const s = sleeperStub({ league: { settings: { type: 1 } } });
  const r = await T.verifyAgainstSleeper(v.value, s.fetchImpl);
  ok(!r.ok, 'verify: a keeper league (type 1) is refused');
}
{
  const s = sleeperStub({ league: { total_rosters: 10 } });
  const r = await T.verifyAgainstSleeper(v.value, s.fetchImpl);
  ok(!r.ok, 'verify: claimed team count must match Sleeper');
}
{
  const s = sleeperStub({ league: { season: '2024' } });
  const r = await T.verifyAgainstSleeper(v.value, s.fetchImpl);
  ok(!r.ok, 'verify: claimed season must match Sleeper');
}
{
  // Managers who are not in the league at all.
  const s = sleeperStub({
    users: MGR_IDS.slice(0, 8).map((id, i) => ({ user_id: id, display_name: 'm' + i })),
  });
  const r = await T.verifyAgainstSleeper(v.value, s.fetchImpl);
  ok(!r.ok, 'verify: managers who are not league members are refused');
  ok(r.errors.join(' ').includes('not members'), 'verify: names the problem');
}
{
  // Real members, but nobody owns a roster.
  const s = sleeperStub({ rosters: MGR_IDS.map((id, i) => ({ roster_id: i + 1 })) });
  const r = await T.verifyAgainstSleeper(v.value, s.fetchImpl);
  ok(!r.ok, 'verify: managers must own a roster');
}
{
  // Display name is taken from Sleeper, never from the payload.
  const s = sleeperStub({
    users: MGR_IDS.map((id, i) => ({ user_id: id, display_name: 'REAL' + i })),
  });
  const r = await T.verifyAgainstSleeper(v.value, s.fetchImpl);
  ok(r.ok, 'verify: renames are not an error');
  ok(r.names[MGR_IDS[0]] === 'REAL0',
     'verify: the stored name comes from Sleeper, not the payload');
}
{
  const s = sleeperStub({ throwOn: '/users' });
  const r = await T.verifyAgainstSleeper(v.value, s.fetchImpl);
  ok(!r.ok && r.transient, 'verify: an upstream outage is transient, not a rejection');
}
{
  const s = sleeperStub({ status: 404 });
  const r = await T.verifyAgainstSleeper(v.value, s.fetchImpl);
  ok(!r.ok && r.transient, 'verify: a 404 from Sleeper does not confirm anything');
}

// ===========================================================================
// Rate limiting / gates
// ===========================================================================

const now = new Date('2026-09-21T12:00:00Z');
function gates(counts, existing) {
  return { counts: Object.assign({ hour: 0, day: 0, global: 0 }, counts),
           existing: existing || null,
           keys: { hourKey: 'h', dayKey: 'd', globalKey: 'g' } };
}

ok(T.evaluateGates(gates({}), now).allow, 'gates: a first submission is allowed');
ok(!T.evaluateGates(gates({ hour: T.constants.RATE_IP_HOURLY }), now).allow,
   'gates: per-IP hourly cap blocks');
ok(T.evaluateGates(gates({ hour: T.constants.RATE_IP_HOURLY }), now).status === 429,
   'gates: hourly cap returns 429');
ok(!T.evaluateGates(gates({ day: T.constants.RATE_IP_DAILY }), now).allow,
   'gates: per-IP daily cap blocks');
ok(!T.evaluateGates(gates({ global: T.constants.GLOBAL_DAILY_CAP }), now).allow,
   'gates: global daily cap blocks');
ok(T.evaluateGates(gates({ global: T.constants.GLOBAL_DAILY_CAP }), now).status === 503,
   'gates: global cap returns 503');
{
  // The global cap must win even when the caller personally has quota.
  const d = T.evaluateGates(gates({ hour: 0, global: T.constants.GLOBAL_DAILY_CAP }), now);
  ok(d.outcome === 'rejected_global_cap', 'gates: global cap outranks per-IP quota');
}
{
  const recent = new Date(now.getTime() - 60 * 1000).toISOString();
  const d = T.evaluateGates(gates({}, { status: 'provisional', last_submitted_at: recent }), now);
  ok(!d.allow && d.duplicate, 'gates: re-submitting the same league is deduped');
  ok(d.status === 200, 'gates: a duplicate is not an error, it returns current state');
}
{
  const old = new Date(now.getTime() - 7 * 3600 * 1000).toISOString();
  const d = T.evaluateGates(gates({}, { status: 'provisional', last_submitted_at: old }), now);
  ok(d.allow, 'gates: the dedupe window expires');
}

// ===========================================================================
// Aggregation
// ===========================================================================

{
  const rows = [];
  for (const m of v.value.managers) {
    rows.push({
      manager_id: m.manager_id, display_name: m.claimed_name,
      league_id: LEAGUE_ID, league_name: 'Dallas Kings', season: '2026',
      n_teams: 12, league_index: m.index, league_rank: m.rank,
      draft_n: m.components.draft.n, draft_z: m.components.draft.z,
      draft_mean: m.components.draft.mean, draft_shrunk: m.components.draft.shrunk,
      trade_n: m.components.trade.n, trade_z: m.components.trade.z,
      trade_mean: m.components.trade.mean, trade_shrunk: m.components.trade.shrunk,
      waiver_n: m.components.waiver.n, waiver_z: m.components.waiver.z,
      waiver_mean: m.components.waiver.mean, waiver_shrunk: m.components.waiver.shrunk,
      flags: '[]',
    });
  }
  const agg = T.aggregateCorpus(rows);
  ok(agg.managers.length === 12, 'aggregate: every manager appears');
  ok(agg.managers[0].rank === 1, 'aggregate: ranks are assigned');
  ok(agg.managers[0].cross_index >= agg.managers[11].cross_index,
     'aggregate: sorted by cross index descending');
  ok(agg.draft_board.length > 0, 'aggregate: draft board is populated');
  ok(agg.managers.every((m) => m.n_leagues === 1), 'aggregate: one league each');

  // Dump for the Python side to compare against crossleague.aggregate().
  process.stdout.write(JSON.stringify({
    kind: 'aggregate_fixture',
    rows,
    aggregate: agg,
    submission: GOOD,
  }) + '\n');
}

// ===========================================================================
// Full HTTP contract, against real SQLite through a D1 shim
// ===========================================================================

function makeD1(migrationSql) {
  const db = new DatabaseSync(':memory:');
  db.exec(migrationSql);

  function mkStatement(sql) {
    return {
      sql,
      args: [],
      bind(...args) { this.args = args; return this; },
      run() {
        const r = db.prepare(this.sql).run(...this.args);
        return { success: true, meta: { changes: Number(r.changes || 0) } };
      },
      all() {
        return { success: true, results: db.prepare(this.sql).all(...this.args) };
      },
      first() {
        const r = db.prepare(this.sql).get(...this.args);
        return r === undefined ? null : r;
      },
    };
  }

  return {
    _db: db,
    prepare(sql) { return mkStatement(sql); },
    async batch(stmts) {
      // D1 batch is transactional; so is this.
      db.exec('BEGIN');
      try {
        const out = stmts.map((s) => {
          const isRead = /^\s*SELECT/i.test(s.sql);
          if (isRead) return { success: true, results: db.prepare(s.sql).all(...s.args) };
          db.prepare(s.sql).run(...s.args);
          return { success: true, results: [] };
        });
        db.exec('COMMIT');
        return out;
      } catch (e) {
        db.exec('ROLLBACK');
        throw e;
      }
    },
  };
}

const migrationSql = readFileSync(migrationPath, 'utf8');

function makeEnv(d1) {
  return { CORPUS_DB: d1, IP_HASH_SALT: 'test-salt', RECONCILE_TOKEN: 'test-token' };
}

const ORIGIN = 'https://pstiehl.github.io';

function submitRequest(payload, opts) {
  const o = opts || {};
  const body = typeof payload === 'string' ? payload : JSON.stringify(payload);
  return new Request('https://w.example/corpus/submit', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Origin': o.origin === undefined ? ORIGIN : o.origin,
      'CF-Connecting-IP': o.ip || '203.0.113.7',
      'Content-Length': String(o.contentLength == null
        ? new TextEncoder().encode(body).length : o.contentLength),
    },
    body,
  });
}

/* Install the Sleeper stub as the global fetch the worker uses. */
function installFetch(overrides) {
  const s = sleeperStub(overrides);
  globalThis.fetch = s.fetchImpl;
  return s;
}

// ---- happy path -----------------------------------------------------------
{
  const d1 = makeD1(migrationSql);
  const env = makeEnv(d1);
  installFetch();

  const res = await worker.fetch(submitRequest(GOOD), env);
  const body = await res.json();
  ok(res.status === 202, 'submit: a verified league returns 202', String(res.status));
  ok(body.state === 'provisional',
     'submit: it is PROVISIONAL, never immediately public', JSON.stringify(body).slice(0, 200));

  const league = d1._db.prepare('SELECT * FROM leagues WHERE league_id = ?').get(LEAGUE_ID);
  ok(!!league, 'submit: league row written');
  ok(league.status === 'provisional', 'submit: stored status is provisional');
  ok(league.verified === 1, 'submit: stored as verified');

  const n = d1._db.prepare('SELECT COUNT(*) c FROM league_managers').get().c;
  ok(n === 12, 'submit: 12 per-manager rows written', String(n));

  const audit = d1._db.prepare("SELECT * FROM submissions ORDER BY id DESC").get();
  ok(audit.outcome === 'accepted', 'submit: audit row records acceptance');
  ok(audit.external_subrequests === 3, 'submit: audit records 3 external subrequests');
  ok(!/203\.0\.113\.7/.test(audit.ip_hash), 'submit: the raw IP is NOT stored');
  ok(audit.ip_hash.length === 64, 'submit: ip_hash is a sha-256 hex digest');

  // The public board must NOT include a provisional league.
  const lb = await worker.fetch(
    new Request('https://w.example/corpus/leaderboard', { headers: { Origin: ORIGIN } }), env);
  const lbBody = await lb.json();
  ok(lb.status === 200, 'leaderboard: 200');
  ok(lbBody.leaderboard.length === 0,
     'leaderboard: provisional leagues are EXCLUDED from the public board',
     String(lbBody.leaderboard.length));
  ok(lbBody.trust.n_leagues_confirmed === 0, 'leaderboard: reports zero confirmed');

  // ...but is visible when explicitly asked for, clearly labelled.
  const lb2 = await worker.fetch(
    new Request('https://w.example/corpus/leaderboard?include=provisional',
                { headers: { Origin: ORIGIN } }), env);
  const lb2Body = await lb2.json();
  ok(lb2Body.leaderboard.length === 12,
     'leaderboard: provisional visible only on explicit request');
  ok(lb2Body.trust.n_leagues_provisional === 1, 'leaderboard: labels it provisional');

  // League status endpoint
  const st = await worker.fetch(
    new Request('https://w.example/corpus/league/' + LEAGUE_ID,
                { headers: { Origin: ORIGIN } }), env);
  const stBody = await st.json();
  ok(stBody.state === 'provisional', 'status: reports provisional');
  ok(stBody.public === false, 'status: explicitly says it is not public');

  // ---- dedupe --------------------------------------------------------------
  const dupe = await worker.fetch(submitRequest(GOOD), env);
  const dupeBody = await dupe.json();
  ok(dupe.status === 200 && dupeBody.duplicate === true,
     'submit: an immediate re-submit is deduped, not re-verified');
  const count2 = d1._db.prepare("SELECT COUNT(*) c FROM submissions WHERE outcome='duplicate'").get().c;
  ok(count2 === 1, 'submit: the duplicate is audited');

  // ---- reconcile promotes to confirmed ------------------------------------
  const rec = await worker.fetch(new Request('https://w.example/corpus/reconcile', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer test-token' },
    body: JSON.stringify({
      schema: 'dfm.corpus.reconcile.v1', run_id: 'test-run',
      results: [{ league_id: LEAGUE_ID, verdict: 'confirmed', note: 'agrees' }],
    }),
  }), env);
  ok(rec.status === 200, 'reconcile: accepted with a valid token');
  const after = d1._db.prepare('SELECT status FROM leagues WHERE league_id = ?').get(LEAGUE_ID);
  ok(after.status === 'confirmed', 'reconcile: league promoted to confirmed');

  const lb3 = await worker.fetch(
    new Request('https://w.example/corpus/leaderboard', { headers: { Origin: ORIGIN } }), env);
  const lb3Body = await lb3.json();
  ok(lb3Body.leaderboard.length === 12,
     'leaderboard: confirmed leagues ARE public');
}

// ---- auth -----------------------------------------------------------------
{
  const d1 = makeD1(migrationSql);
  const env = makeEnv(d1);
  for (const hdrs of [{}, { Authorization: 'Bearer wrong' }, { Authorization: 'test-token' }]) {
    const r = await worker.fetch(new Request('https://w.example/corpus/pending',
      { headers: hdrs }), env);
    ok(r.status === 401, 'pending: refuses without the right bearer token');
  }
  const r2 = await worker.fetch(new Request('https://w.example/corpus/reconcile', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema: 'dfm.corpus.reconcile.v1', results: [] }),
  }), env);
  ok(r2.status === 401, 'reconcile: refuses without a token');

  const good = await worker.fetch(new Request('https://w.example/corpus/pending',
    { headers: { Authorization: 'Bearer test-token' } }), env);
  ok(good.status === 200, 'pending: accepted with the right token');
}

// ---- abuse controls -------------------------------------------------------
{
  const d1 = makeD1(migrationSql);
  const env = makeEnv(d1);
  installFetch();

  // Origin
  const bad = await worker.fetch(submitRequest(GOOD, { origin: 'https://evil.example' }), env);
  ok(bad.status === 403, 'abuse: a foreign origin cannot submit', String(bad.status));

  // Oversized, declared
  const big = await worker.fetch(
    submitRequest(GOOD, { contentLength: T.constants.MAX_BODY_BYTES + 1 }), env);
  ok(big.status === 413, 'abuse: oversized Content-Length is refused');

  // Oversized, lying Content-Length (body is the authority)
  const padded = clone(GOOD);
  padded.league.pad = 'x'.repeat(T.constants.MAX_BODY_BYTES + 1000);
  const lying = await worker.fetch(submitRequest(padded, { contentLength: 100 }), env);
  ok(lying.status === 413, 'abuse: a lying Content-Length does not get through');

  // Malformed JSON
  const junk = await worker.fetch(submitRequest('{not json', {}), env);
  ok(junk.status === 400, 'abuse: malformed JSON is refused');

  // Wrong method
  const wrongMethod = await worker.fetch(new Request('https://w.example/corpus/submit',
    { method: 'GET', headers: { Origin: ORIGIN } }), env);
  ok(wrongMethod.status === 405, 'abuse: GET on the submit endpoint is refused');

  // A rejected submission must spend NO Sleeper calls.
  const spy = installFetch();
  await worker.fetch(submitRequest({ schema: 'nope' }, {}), env);
  ok(spy.calls.length === 0,
     'abuse: a schema-invalid payload costs zero upstream calls',
     String(spy.calls.length));

  const spy2 = installFetch();
  await worker.fetch(submitRequest(forged, {}), env);
  ok(spy2.calls.length === 0,
     'abuse: a fabricated payload is caught before any upstream call',
     String(spy2.calls.length));
}

// ---- per-IP rate limit, end to end ---------------------------------------
{
  const d1 = makeD1(migrationSql);
  const env = makeEnv(d1);
  installFetch();

  let blocked = 0, allowed = 0;
  for (let i = 0; i < T.constants.RATE_IP_HOURLY + 4; i++) {
    // A different league id each time so dedupe is not what stops us.
    //
    // Built by string concatenation, NOT arithmetic: a 19-digit Sleeper
    // snowflake is far past Number.MAX_SAFE_INTEGER, so `base + i` silently
    // returns the same float for every i and every league would collide on
    // the dedupe gate instead of reaching the rate limiter.
    const p = clone(GOOD);
    const lid = '131622212691453' + String(9000 + i);
    p.league.league_id = lid;
    p.league.lineage_ids = [lid];
    p.league.lineage_root = lid;
    installFetch({ league: { league_id: lid, name: 'L' + i, season: '2026',
                             total_rosters: 12, settings: { type: 2 } } });
    const r = await worker.fetch(submitRequest(p, { ip: '198.51.100.9' }), env);
    if (r.status === 429) blocked++; else if (r.status === 202) allowed++;
  }
  ok(allowed === T.constants.RATE_IP_HOURLY,
     'rate limit: exactly the hourly quota is accepted', String(allowed));
  ok(blocked === 4, 'rate limit: the rest are blocked with 429', String(blocked));

  const audited = d1._db.prepare(
    "SELECT COUNT(*) c FROM submissions WHERE outcome='rejected_rate'").get().c;
  ok(audited === 4, 'rate limit: blocked attempts are audited');
}

// ---- unverified submissions are stored but never public -------------------
{
  const d1 = makeD1(migrationSql);
  const env = makeEnv(d1);
  // League is real and self-consistent, but it is a REDRAFT league.
  installFetch({ league: { settings: { type: 0 } } });

  const r = await worker.fetch(submitRequest(GOOD), env);
  ok(r.status === 422, 'unverified: a redraft league is refused', String(r.status));

  const row = d1._db.prepare('SELECT * FROM leagues WHERE league_id = ?').get(LEAGUE_ID);
  ok(!!row, 'unverified: it is still STORED (as the brief requires)');
  ok(row.status === 'unverified', 'unverified: stored with status unverified');
  ok(row.verified === 0, 'unverified: verified flag is 0');

  const lb = await worker.fetch(
    new Request('https://w.example/corpus/leaderboard?include=provisional',
                { headers: { Origin: ORIGIN } }), env);
  const lbBody = await lb.json();
  ok(lbBody.leaderboard.length === 0,
     'unverified: excluded even from the provisional board');
}

// ---- worker runs fine with NO D1 binding ----------------------------------
{
  const env = { IP_HASH_SALT: 's' };
  const r = await worker.fetch(submitRequest(GOOD), env);
  ok(r.status === 501, 'no-D1: submit answers 501, not a crash', String(r.status));

  const h = await worker.fetch(new Request('https://w.example/health'), env);
  const hb = await h.json();
  ok(h.status === 200 && hb.status === 'ok', 'no-D1: /health still works');
  ok(hb.corpus === 'disabled', 'no-D1: health reports corpus disabled');
}

// ---- the pre-existing proxy is untouched ----------------------------------
{
  const env = { IP_HASH_SALT: 's' };
  const r = await worker.fetch(new Request('https://w.example/nope'), env);
  ok(r.status === 404, 'proxy: unknown paths still 404');
  const opt = await worker.fetch(new Request('https://w.example/corpus/submit',
    { method: 'OPTIONS', headers: { Origin: ORIGIN } }), env);
  ok(opt.status === 204, 'proxy: CORS preflight still answered');
  ok(opt.headers.get('Access-Control-Allow-Origin') === ORIGIN,
     'proxy: preflight echoes the allowed origin');
}

console.error(`\n${checks - failures}/${checks} checks passed`);
if (failures) {
  console.error(`${failures} FAILED`);
  process.exit(1);
}
