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
 *             local build artifacts the page would load over HTTP, serves
 *             completed seasons from the permanent disk cache, and refuses
 *             calls once the budget is spent.
 *   console   passthrough to stderr, so page logging cannot corrupt the
 *             JSON this writes to stdout.
 *
 * The permanent cache (scripts/js/sleeper_disk_cache.js)
 * ------------------------------------------------------
 * A completed NFL season is final, so its transactions, box scores, drafts
 * and rosters are fetched once and kept forever. A cache hit is NOT a call:
 * it does not touch the network, does not consume budget, and does not
 * count toward the rate limit. That is the whole point -- re-scoring an
 * already-indexed league should cost only its live season, so the budget
 * can go to leagues we have never seen.
 *
 * Ordering inside instrumentedFetch is load-bearing and must stay:
 *
 *     local artifact  ->  disk cache  ->  budget check  ->  network
 *
 * The cache is consulted BEFORE the budget check on purpose. A league whose
 * history is already on disk must remain scorable when the budget is nearly
 * spent, because serving it costs Sleeper nothing. Moving the budget check
 * first would make the cache useless in exactly the situation it exists for.
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
const { createDiskCache } = require('./sleeper_disk_cache.js');

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
/* Wall-clock ceiling. The call budget bounds politeness toward Sleeper; this
 * bounds how long a daily job may take. They are different failure modes and
 * both are needed: 3000 throttled calls is ~12 minutes, and a stalled league
 * could otherwise hold the deploy open indefinitely. 0 disables. */
const MAX_SECONDS = req.maxSeconds == null ? 0 : Number(req.maxSeconds);
const STARTED_AT = Date.now();

function secondsLeft() {
  if (!MAX_SECONDS) return Infinity;
  return MAX_SECONDS - (Date.now() - STARTED_AT) / 1000;
}

/* ------------------------------------------------------------- budget/rate */

const budget = {
  calls: 0, refused: 0, served_local: 0, errors: 0, cache_hits: 0
};
let leagueRefused = 0;          /* reset per league; see "Budget honesty" */

const cache = createDiskCache({
  dir: req.cacheDir || null,
  maxBytes: req.maxCacheBytes == null ? undefined : Number(req.maxCacheBytes)
});

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
/* Defaults to the sibling of valuesPath, because write_values_artifact()
 * emits both files into one directory; taking it from the request as well
 * keeps the caller able to override. */
const seriesPath = req.seriesPath || (valuesPath
  ? path.join(path.dirname(valuesPath), 'managerscore_series.json')
  : null);

function localBody(url) {
  /* The page loads these over HTTP relative to itself. Serving them from the
   * build directory keeps the valuation path identical to production without
   * spending a network call on our own artifacts.
   *
   * ALL THREE ARTIFACTS MUST BE SERVED. msLoadValues() fetches
   * managerscore_values.json AND managerscore_series.json, and the second is
   * not optional decoration: values.by_sleeper maps a Sleeper player id to a
   * KTC id, but the actual price of an asset ON THE DATE IT WAS TRADED comes
   * from the series. msCaptureForPlayer() looks the id up in values, then
   * calls msCapture(MSX.series, ...) to price it.
   *
   * When the series was missing this returned 404, msGetJSONSoft turned that
   * into `null`, MSX.series stayed null, and msCapture answered "no value
   * recorded on or before this date" for EVERY asset in EVERY league. The
   * crawl still succeeded, every league still scored, and every manager came
   * out at exactly index 100 with draft/trade/waiver n=0 -- a corpus that
   * looked structurally perfect and contained no measurements at all. That
   * is what "the best managers tab has basically no data" actually was.
   *
   * A missing artifact must therefore be LOUD. Silence here is indis-
   * tinguishable from a league that genuinely made no trades. */
  if (url === 'managerscore_values.json') {
    return valuesPath && fs.existsSync(valuesPath)
      ? fs.readFileSync(valuesPath, 'utf8') : null;
  }
  if (url === 'managerscore_series.json') {
    return seriesPath && fs.existsSync(seriesPath)
      ? fs.readFileSync(seriesPath, 'utf8') : null;
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

  /* Permanent cache, consulted before the budget: a completed season costs
   * neither a call nor a slot in the rate limiter. See the header note on
   * why this ordering cannot be swapped. */
  const cached = cache.read(url);
  if (cached !== undefined) {
    budget.cache_hits++;
    return Promise.resolve(makeResponse(true, 200, cached));
  }

  if (budget.calls >= MAX_CALLS) {
    budget.refused++;
    leagueRefused++;
    return Promise.reject(new Error('api call budget exhausted'));
  }
  if (secondsLeft() <= 0) {
    /* Out of wall clock mid-league. Treated exactly like a refused call, so
     * the league is discarded rather than scored on a truncated read. */
    budget.refused++;
    leagueRefused++;
    return Promise.reject(new Error('wall-clock budget exhausted'));
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
  }).then((r) => r.text().then((t) => {
    /* Only a 2xx is worth keeping. A 404 for "week 18 of a 17-week season"
     * is a real answer to the page but is not a payload, and caching error
     * bodies would turn one bad day into a permanent one. */
    if (r.ok) cache.write(url, t);
    return makeResponse(r.ok, r.status, t);
  }))
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
  /* Populate MSX.values / MSX.series exactly as the page does. */
  await sandbox.msLoadValues();
  const v = sandbox.MSX && sandbox.MSX.values;
  const series = sandbox.MSX && sandbox.MSX.series;

  /* Refuse to build a corpus that cannot price anything.
   *
   * Without the series every asset falls through to "no value recorded on or
   * before this date", every component lands on n=0, and every manager
   * scores exactly 100. That artifact is worse than no artifact: it is
   * indistinguishable from a real corpus of very inactive leagues, so it
   * publishes silently and nobody can tell from the output that the run was
   * broken. Failing here costs one build; shipping it cost a release. */
  if (!series || !series.dates || !series.dates.length) {
    process.stdout.write(JSON.stringify({
      ok: false,
      error: 'managerscore_series.json is missing or empty (' +
             String(seriesPath) + '). Without the KTC price series no asset ' +
             'can be valued on its transaction date, so every manager would ' +
             'score exactly 100 with n=0 on every component. Refusing to ' +
             'publish a corpus that measures nothing.',
      budget: budget
    }));
    process.exit(4);
  }

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
    if (secondsLeft() <= 0) {
      results.push({
        league_id: String(spec.league_id), name: spec.name || null,
        ok: false, reason: 'wall-clock budget exhausted before this league was read',
        calls: 0
      });
      continue;
    }
    results.push(await scoreOne(spec));
    /* Flush per league rather than once at the end: a run killed by a CI
     * timeout should still have banked the seasons it already paid for. */
    cache.flush();
  }

  const cacheStats = cache.flush();

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
    cache: cacheStats,
    leagues: results
  }));
})().catch((e) => {
  try { cache.flush(); } catch (_) { /* best effort */ }
  process.stdout.write(JSON.stringify({
    ok: false, error: String((e && e.stack) || e), budget: budget
  }));
  process.exit(1);
});
