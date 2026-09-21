/* Score a Sleeper league with the REAL Manager Score page code, in node.
 *
 * Why this exists
 * ---------------
 * The cross-league board must aggregate the *same* metric the Manager Score
 * page shows, not a second opinion that happens to look similar. The scoring
 * maths and the Sleeper-JSON-to-scorer-input mapping both live only in
 * JavaScript (the page reads Sleeper live in the browser, because the site is
 * a static GitHub Pages build with no server). So rather than port either of
 * them to Python — which would immediately start drifting — this harness runs
 * the shipped page scripts under node.
 *
 * How
 * ---
 * `dynasty.managerscore_js` emits MANAGERSCORE_CORE_JS + MANAGERSCORE_UI_JS.
 * Those are page scripts, not modules: the UI layer's exports deliberately
 * cover only what its DOM tests need, and do not include the Sleeper fetch
 * chain. Evaluating both in a `vm` context instead makes every top-level
 * declaration reachable — msFetchLeagueData (previous_league_id chaining),
 * msFetchTransactions (per-week paging) and msBuildInput (the shape mapping)
 * included — so the crawl reuses the browser's exact read path.
 *
 * Three things are injected into that context:
 *
 *   document  a no-op stub. The page registers msInit on DOMContentLoaded,
 *             which never fires here, so no rendering is attempted.
 *   fetch     instrumented: counts every call, rate-limits, serves the two
 *             local build artifacts the page would load over HTTP, and
 *             refuses calls once the budget is spent.
 *   console   passthrough to stderr, so page logging cannot corrupt the
 *             JSON this writes to stdout.
 *
 * Budget honesty
 * --------------
 * msGetJSONSoft() turns any failed fetch into an empty fallback, which is
 * correct for a browser (a league with no drafts 404s) but dangerous here: a
 * league whose transaction pages were refused mid-read would score against
 * half its history and look precise. So a refused call sets `refused` for
 * that league and the caller DISCARDS the result rather than publishing a
 * partial score. A league is scored on all of its data or none of it.
 *
 * Usage (request on stdin, result on stdout, both JSON):
 *   node scripts/js/score_league_harness.js <core+ui.js> <request.json>
 *
 * Request: { leagues: [{league_id, name, season, n_teams, discovered_via,
 *                       hop}],
 *            valuesPath, historyDir, maxCalls, minDelayMs, concurrency,
 *            includeHistory }
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const jsPath = process.argv[2];
const reqPath = process.argv[3];
if (!jsPath || !reqPath) {
  console.error('usage: node score_league_harness.js <page-js> <request-json>');
  process.exit(2);
}

const req = JSON.parse(fs.readFileSync(reqPath, 'utf8'));
const MAX_CALLS = req.maxCalls == null ? 400 : req.maxCalls;
const MIN_DELAY_MS = req.minDelayMs == null ? 120 : req.minDelayMs;
const CONCURRENCY = req.concurrency == null ? 3 : req.concurrency;
const INCLUDE_HISTORY = req.includeHistory !== false;

/* ------------------------------------------------------------- budget/rate */

const budget = { calls: 0, refused: 0, served_local: 0, errors: 0 };
let leagueRefused = 0;          /* reset per league; see "Budget honesty" */

let active = 0;
const queue = [];
let lastStart = 0;

function schedule(fn) {
  return new Promise((resolve, reject) => {
    queue.push({ fn, resolve, reject });
    pump();
  });
}

function pump() {
  if (active >= CONCURRENCY || !queue.length) return;
  const job = queue.shift();
  active++;
  /* Sleeper's published guidance is to stay under 1000 calls/minute. This
   * paces well under that: a floor on the gap between call starts, and a
   * concurrency cap. Deliberately conservative — being a good citizen on a
   * free read-only API costs us nothing but wall time. */
  const wait = Math.max(0, lastStart + MIN_DELAY_MS - Date.now());
  setTimeout(() => {
    lastStart = Date.now();
    Promise.resolve()
      .then(job.fn)
      .then(job.resolve, job.reject)
      .finally(() => { active--; pump(); });
  }, wait);
}

/* ------------------------------------------------------------------ fetch */

const valuesPath = req.valuesPath;
const historyDir = req.historyDir;

function localBody(url) {
  /* The page loads these two over HTTP relative to itself. Serving them from
   * the build directory keeps the valuation path identical to production
   * without spending a network call on our own artifacts. */
  if (url === 'managerscore_values.json') {
    return valuesPath && fs.existsSync(valuesPath)
      ? fs.readFileSync(valuesPath, 'utf8') : null;
  }
  const m = /^ktc_history\/(ktc_values_[0-9-]+\.json)$/.exec(url);
  if (m && historyDir) {
    const p = path.join(historyDir, m[1]);
    return fs.existsSync(p) ? fs.readFileSync(p, 'utf8') : null;
  }
  return null;
}

function makeResponse(ok, status, text) {
  return {
    ok: ok,
    status: status,
    json: () => Promise.resolve(text == null ? null : JSON.parse(text)),
    text: () => Promise.resolve(text)
  };
}

function instrumentedFetch(url) {
  url = String(url);

  const local = localBody(url);
  if (local != null) {
    budget.served_local++;
    return Promise.resolve(makeResponse(true, 200, local));
  }
  if (!/^https?:\/\//.test(url)) {
    /* A relative URL we do not serve locally: the artifact is genuinely
     * absent. 404 is the truthful answer and the page degrades on it. */
    return Promise.resolve(makeResponse(false, 404, null));
  }

  if (budget.calls >= MAX_CALLS) {
    budget.refused++;
    leagueRefused++;
    return Promise.reject(new Error('api call budget exhausted'));
  }
  budget.calls++;

  return schedule(() => globalThis.fetch(url, {
    headers: {
      /* Honest, self-identifying UA. This project does not spoof browsers to
       * get around access controls. */
      'User-Agent': 'Dynasty-Football-Model/cross-league-corpus ' +
                    '(+https://github.com/pstiehl/Dynasty-Football-Model)',
      'Accept': 'application/json'
    }
  }).then((r) => r.text().then((t) => makeResponse(r.ok, r.status, t)))
    .catch((e) => {
      budget.errors++;
      leagueRefused++;      /* a network error is also incomplete data */
      throw e;
    }));
}

/* ------------------------------------------------------- load page scripts */

const pageJs = fs.readFileSync(jsPath, 'utf8');

const sandbox = {
  console: {
    log: (...a) => console.error('[page]', ...a),
    warn: (...a) => console.error('[page]', ...a),
    error: (...a) => console.error('[page]', ...a)
  },
  fetch: instrumentedFetch,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  document: {
    /* Auto-vivifying no-op elements: the page may touch any id. Nothing is
     * rendered and nothing is asserted about rendering here. */
    _els: {},
    getElementById(id) {
      if (!this._els[id]) {
        this._els[id] = {
          id, innerHTML: '', textContent: '', value: '', checked: false,
          className: '', disabled: false, dataset: {}, style: {},
          classList: { add() {}, remove() {}, contains() { return false; } },
          addEventListener() {}, appendChild() {}, scrollIntoView() {},
          querySelector() { return null; }, querySelectorAll() { return []; }
        };
      }
      return this._els[id];
    },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() {}     /* DOMContentLoaded never fires: no msInit */
  }
};
sandbox.globalThis = sandbox;
sandbox.window = sandbox;

vm.createContext(sandbox);
vm.runInContext(pageJs, sandbox, { filename: 'managerscore_page.js' });

for (const fn of ['msFetchLeagueData', 'msBuildInput', 'msScoreLeague',
                  'msLoadValues']) {
  if (typeof sandbox[fn] !== 'function') {
    console.error('FATAL: ' + fn + ' not found in the page scripts. The ' +
      'harness reuses the shipped page code by name; if it was renamed, ' +
      'this must be updated rather than reimplemented.');
    process.exit(3);
  }
}

/* ---------------------------------------------------------------- scoring */

async function scoreOne(spec) {
  leagueRefused = 0;
  const callsBefore = budget.calls;
  const out = {
    league_id: String(spec.league_id),
    name: spec.name || null,
    season: spec.season || null,
    n_teams: spec.n_teams || null,
    discovered_via: spec.discovered_via || null,
    hop: spec.hop == null ? null : spec.hop
  };

  try {
    const seasons = await sandbox.msFetchLeagueData(
      String(spec.league_id), INCLUDE_HISTORY);
    out.lineage_ids = (seasons || []).map((s) => String(s.league.league_id));
    out.lineage_root = out.lineage_ids.length
      ? out.lineage_ids[out.lineage_ids.length - 1] : null;
    out.n_seasons = out.lineage_ids.length;

    const built = await sandbox.msBuildInput(seasons);
    const result = sandbox.msScoreLeague(built.input, {});
    out.coverage = built.coverage;

    if (leagueRefused > 0) {
      /* See "Budget honesty" above: partial reads are discarded, never
       * published as a score. */
      out.ok = false;
      out.reason = 'incomplete read (' + leagueRefused + ' call(s) refused ' +
                   'or failed) — discarded rather than scored on partial data';
      out.calls = budget.calls - callsBefore;
      return out;
    }

    out.ok = true;
    out.result = result;
    out.calls = budget.calls - callsBefore;
    return out;
  } catch (e) {
    out.ok = false;
    out.reason = String((e && e.message) || e);
    out.calls = budget.calls - callsBefore;
    return out;
  }
}

(async () => {
  /* Populate MSX.values / MSX.history exactly as the page does. */
  await sandbox.msLoadValues();
  const v = sandbox.MSX && sandbox.MSX.values;

  const results = [];
  for (const spec of req.leagues || []) {
    if (budget.calls >= MAX_CALLS) {
      results.push({
        league_id: String(spec.league_id), name: spec.name || null,
        ok: false, reason: 'api call budget exhausted before this league was read',
        calls: 0
      });
      continue;
    }
    results.push(await scoreOne(spec));
  }

  process.stdout.write(JSON.stringify({
    ok: true,
    values: {
      available: !!(v && v.available),
      floor: v && v.ktc ? v.ktc.floor : null,
      n_mapped: v && v.ktc ? v.ktc.n_mapped : null,
      captured_at: v && v.ktc ? v.ktc.captured_at : null,
      history_days: v && v.history ? v.history.count : 0
    },
    budget: budget,
    leagues: results
  }));
})().catch((e) => {
  process.stdout.write(JSON.stringify({
    ok: false, error: String((e && e.stack) || e), budget: budget
  }));
  process.exit(1);
});
