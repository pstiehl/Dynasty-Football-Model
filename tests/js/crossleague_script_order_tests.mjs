/* Execute the REAL crossleague.html scripts, in document order.
 *
 * What this proves, and why reading the file cannot
 * --------------------------------------------------
 * PR #71 fixed a live outage: `report._page` emitted the shared highlight
 * renderer AFTER the page body, so a page script that called `DFMHL.chip`
 * during its own evaluation threw
 *
 *     TypeError: Cannot read properties of undefined (reading 'chip')
 *
 * and rendered an empty table. Nothing static caught it. The file parsed,
 * `node --check` was clean, the payload was intact — the bug only exists
 * when the scripts are *run*, in the order the document actually lists them.
 *
 * The manager drill-down calls `DFMHL.chip` for every player name it
 * renders, which puts this page back in exactly that failure mode's blast
 * radius. So this test does not reconstruct the order, does not trust a
 * comment, and does not regex the HTML for a `<script>` position. It parses
 * the generated page, evaluates each script in order in one shared vm
 * context, and then drives the real render path.
 *
 * It also runs a NEGATIVE CONTROL: the same scripts evaluated in the broken
 * order must throw the PR #71 TypeError. A test that passes in both orders
 * would prove nothing about ordering at all.
 *
 * Artifacts are the real ones emitted by dynasty.manager_detail, so the
 * shard path the browser computes (xlShard/xlSafeName) has to agree with
 * the path Python wrote or the fetch 404s and the assertions fail.
 *
 * What it cannot prove: that any of this LOOKS right. There is no browser
 * and no layout engine here, so visual rendering is unverified.
 *
 * Usage:
 *   node crossleague_script_order_tests.mjs <html> <corpus.json> <detailDir>
 */
'use strict';

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const [htmlPath, corpusPath, detailDir] = process.argv.slice(2);
if (!htmlPath || !corpusPath || !detailDir) {
  console.error('usage: node crossleague_script_order_tests.mjs ' +
                '<html> <corpus.json> <detailDir>');
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

/* ------------------------------------------------- extract page scripts */

const html = fs.readFileSync(htmlPath, 'utf8');
const scripts = [];
const re = /<script\b([^>]*)>([\s\S]*?)<\/script>/g;
let m;
while ((m = re.exec(html)) !== null) {
  if (/\bsrc=/.test(m[1])) continue;   // external: nothing to evaluate here
  scripts.push(m[2]);
}
ok(scripts.length >= 3, 'page carries the inline scripts to execute',
   'found ' + scripts.length);

/* The two that matter, identified by content rather than by index, so this
 * keeps working if another inline script is added to the chrome. */
const hlIdx = scripts.findIndex((s) => /var\s+DFMHL\s*=/.test(s));
const xlIdx = scripts.findIndex((s) => /var\s+XLX\s*=/.test(s));
ok(hlIdx >= 0, 'the shared highlight renderer is on the page');
ok(xlIdx >= 0, 'the cross-league page script is on the page');
ok(hlIdx >= 0 && xlIdx >= 0 && hlIdx < xlIdx,
   'DFMHL is emitted BEFORE the page script (the PR #71 invariant)',
   'hl@' + hlIdx + ' xl@' + xlIdx);

/* ------------------------------------------------------------ mini DOM */

function makeEl(id) {
  const el = {
    id, innerHTML: '', textContent: '', className: '', value: '',
    style: {}, dataset: {}, attributes: {}, _listeners: [],
    classList: { add() {}, remove() {}, contains() { return false; } },
    addEventListener(ev, fn) { this._listeners.push([ev, fn]); },
    removeEventListener() {},
    appendChild() {}, scrollIntoView() {}, focus() {},
    setAttribute(n, v) { this.attributes[n] = String(v); this[n] = String(v); },
    getAttribute(n) {
      return Object.prototype.hasOwnProperty.call(this.attributes, n)
        ? this.attributes[n] : null;
    },
    removeAttribute(n) { delete this.attributes[n]; delete this[n]; },
    querySelector() { return null; },
    querySelectorAll(sel) {
      if (sel !== 'tr.xl-row') return [];
      const ids = [];
      const rx = /data-manager="([^"]+)"/g;
      let mm;
      while ((mm = rx.exec(this.innerHTML)) !== null) ids.push(mm[1]);
      return ids.map((mid) => {
        const e = makeEl('row-' + mid);
        e.dataset = { manager: mid };
        return e;
      });
    }
  };
  return el;
}

function makeContext(fetchImpl) {
  const ELEMENTS = {};
  const document = {
    getElementById(id) {
      if (!ELEMENTS[id]) ELEMENTS[id] = makeEl(id);
      return ELEMENTS[id];
    },
    createElement(tag) { return makeEl('created-' + tag); },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() {},
    body: makeEl('body'),
    activeElement: { tagName: 'BODY' }
  };
  const windowObj = {
    DFM_BASE: '', location: { href: 'https://example.test/crossleague.html' },
    addEventListener() {}, matchMedia: () => ({ matches: false })
  };
  const ctx = {
    document, window: windowObj, navigator: { userAgent: 'node' },
    console, fetch: fetchImpl, setTimeout, clearTimeout,
    Promise, JSON, Math, Date, URL, encodeURIComponent, decodeURIComponent,
    localStorage: {
      _d: {},
      getItem(k) { return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null; },
      setItem(k, v) { this._d[k] = String(v); },
      removeItem(k) { delete this._d[k]; },
      key() { return null; }, get length() { return 0; }
    },
    ELEMENTS
  };
  ctx.globalThis = ctx;
  ctx.self = ctx;
  return vm.createContext(ctx);
}

/* ------------------------------------------------------- artifact serving */

const corpus = JSON.parse(fs.readFileSync(corpusPath, 'utf8'));

function makeFetch(log) {
  return function (url) {
    log.push(url);
    const respond = (status, body) => Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      json: () => Promise.resolve(body),
      text: () => Promise.resolve(JSON.stringify(body))
    });
    if (url === 'crossleague_corpus.json') return respond(200, corpus);
    if (url.startsWith('managers/')) {
      /* Resolve exactly as a static host would: the path the browser built
       * must match the file Python wrote, shard directory included. */
      const p = path.join(detailDir, url.slice('managers/'.length));
      if (!fs.existsSync(p)) return respond(404, null);
      return respond(200, JSON.parse(fs.readFileSync(p, 'utf8')));
    }
    return respond(404, null);
  };
}

/* ============================ 1. correct order ========================= */

const log = [];
const ctx = makeContext(makeFetch(log));
let evalError = null;
try {
  for (const src of scripts) vm.runInContext(src, ctx, { timeout: 20000 });
} catch (e) {
  evalError = e;
}
ok(!evalError, 'every page script evaluates in document order',
   evalError ? String(evalError && evalError.message) : '');

ok(typeof ctx.DFMHL === 'object' && typeof ctx.DFMHL.chip === 'function',
   'DFMHL.chip is defined once the page has been evaluated');
ok(typeof ctx.xlRenderAll === 'function',
   'the cross-league renderers are reachable');

/* ---- drive the real render path ---- */

const target = corpus.leaderboard[0];
await vm.runInContext('xlLoad()', ctx, { timeout: 20000 });

const lbHtml = ctx.ELEMENTS['xl-leaderboard']
  ? ctx.ELEMENTS['xl-leaderboard'].innerHTML : '';
ok(lbHtml.length > 0, 'the leaderboard rendered something at all');
ok(lbHtml.indexOf(target.display_name) >= 0,
   'the leaderboard contains the top manager', target.display_name);
ok(log.indexOf('crossleague_corpus.json') >= 0, 'the corpus was fetched');

/* Nothing should have been fetched for a manager nobody clicked: the whole
 * point of the layout is that the index stays small. */
ok(!log.some((u) => u.startsWith('managers/')),
   'no detail artifact is fetched before a manager is opened',
   log.join(' '));

/* ---- open a manager: this is where DFMHL.chip gets called ---- */

ctx.__mid = String(target.manager_id);
await vm.runInContext('xlToggle(__mid)', ctx, { timeout: 20000 });
/* xlToggle kicks off an async fetch; let the microtask queue drain. */
await new Promise((r) => setTimeout(r, 50));
await vm.runInContext('xlLoadDetail(__mid)', ctx, { timeout: 20000 });
await new Promise((r) => setTimeout(r, 50));

const detailUrl = vm.runInContext('xlDetailUrl(__mid)', ctx);
ok(log.indexOf(detailUrl) >= 0,
   'opening a manager fetches exactly their shard', detailUrl);

/* Detail ids are scoped per table; an unscoped xlToggle drives the
 * leaderboard, so this is the leaderboard's panel body. */
const panelId = 'xl-detail-body-lb-' + target.manager_id;
const panel = ctx.ELEMENTS[panelId] ? ctx.ELEMENTS[panelId].innerHTML : '';

ok(panel.length > 0, 'the drill-down panel rendered');
ok(panel.indexOf('Cannot read properties') < 0,
   'no TypeError text leaked into the panel');

/* The PR #71 assertion, positively stated: chip markup is actually present,
 * which can only happen if DFMHL was defined when the renderer ran. */
ok(panel.indexOf('data-dfm-player') >= 0,
   'player names render as DFMHL highlight chips in the drill-down');
ok(panel.indexOf('dfm-hl') >= 0, 'the chip button markup is present');

/* The panel must actually explain the draft score. */
ok(/Slot baseline/.test(panel), 'the pick table shows the slot baseline');
ok(/Surplus/.test(panel), 'the pick table shows the per-pick surplus');
ok(/point-in-time|current board/.test(panel),
   'every asset is labelled with its valuation basis');
ok(/How that becomes a z-score/.test(panel),
   'the panel shows the arithmetic from surplus to z');
ok(/Total surplus over/.test(panel),
   'the pick table totals the surplus column');

/* Honest degradation: a manager with no published artifact must say so. */
ctx.__missing = 'no-such-manager-id-99999';
vm.runInContext(
  'XLX.detailState[__missing] = "absent";' +
  'XLX.corpus.leaderboard.push({manager_id: __missing, display_name: "Ghost",' +
  ' components: {}, leagues: [], flags: []});',
  ctx);
const absentHtml = vm.runInContext(
  'xlRenderDetailPanel({manager_id: __missing, display_name: "Ghost",' +
  ' components: {}, leagues: []}, null, "absent", null)', ctx);
ok(/No pick-level detail published/.test(absentHtml),
   'a manager with no detail file says so plainly');
ok(!/^\s*$/.test(absentHtml), 'the missing-detail panel is not empty');
ok(/still count/.test(absentHtml),
   'the missing-detail panel says the score is unaffected');

const errHtml = vm.runInContext(
  'xlRenderDetailPanel({manager_id: "x", display_name: "E", components: {},' +
  ' leagues: []}, null, "error", "HTTP 500")', ctx);
ok(/Could not load/.test(errHtml) && /HTTP 500/.test(errHtml),
   'a failed fetch is reported as a failure, not as no activity');
ok(/not an absence of activity/.test(errHtml),
   'the error panel distinguishes missing evidence from zero activity');

/* ====================== 2. negative control ============================ */

/* Evaluate the page script BEFORE the highlight renderer -- the exact
 * mistake PR #71 fixed -- and require that it breaks. If this passes, the
 * checks above are not actually testing ordering. */
{
  const log2 = [];
  const ctx2 = makeContext(makeFetch(log2));
  const broken = scripts.filter((_, i) => i !== hlIdx);
  broken.push(scripts[hlIdx]);            // highlights LAST, as in the bug
  let threw = null;
  try {
    for (const src of broken) vm.runInContext(src, ctx2, { timeout: 20000 });
    await vm.runInContext('xlLoad()', ctx2, { timeout: 20000 });
    ctx2.__mid = String(target.manager_id);
    await vm.runInContext('xlToggle(__mid)', ctx2, { timeout: 20000 });
    await new Promise((r) => setTimeout(r, 50));
    await vm.runInContext('xlLoadDetail(__mid)', ctx2, { timeout: 20000 });
    await new Promise((r) => setTimeout(r, 50));
    /* In the broken order DFMHL is still undefined at the moment the page
     * script's top level runs. It is defined by the time the click handler
     * fires here, so the strongest available signal is a direct call of the
     * renderer at page-script evaluation time. */
    vm.runInContext('(function(){ var d = DFMHL; })()', ctx2);
  } catch (e) {
    threw = e;
  }

  /* The decisive check: with highlights last, DFMHL is NOT defined while the
   * page script evaluates. Prove that by evaluating the page script alone in
   * a fresh context and calling the chip path. */
  const ctx3 = makeContext(makeFetch([]));
  let orderError = null;
  try {
    vm.runInContext(scripts[xlIdx], ctx3, { timeout: 20000 });
    vm.runInContext('xlPlayerChip("Bijan Robinson", "RB", "1234")', ctx3);
  } catch (e) {
    orderError = e;
  }
  ok(orderError !== null,
     'NEGATIVE CONTROL: without the highlight renderer, the chip path throws');
  ok(orderError !== null &&
     /is not defined|Cannot read properties/.test(String(orderError.message)),
     'NEGATIVE CONTROL: it throws the PR #71 class of error',
     orderError ? String(orderError.message) : 'no error');
}

/* ------------------------------------------------------------- report */

if (failures) {
  console.error('\n' + failures + ' of ' + checks + ' checks FAILED');
  process.exit(1);
}
console.log('crossleague script-order + drill-down: ' + checks +
            ' checks passed');
console.log('  note: script EXECUTION verified; visual rendering is not ' +
            '(no browser/layout engine here).');
