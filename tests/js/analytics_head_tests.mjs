/* The analytics beacon, checked by EXECUTING the page rather than reading it.
 *
 *   node tests/js/analytics_head_tests.mjs <configured.html> <unconfigured.html>
 *
 * Exits 0 on success, 1 with a report.
 *
 * Why this file exists
 * --------------------
 * PR #71 fixed a live outage in exactly the function this change touches:
 * `report._page` emitted the shared highlight renderer AFTER the page body,
 * so a page script that read `DFMHL` while being *evaluated* threw
 * `TypeError: Cannot read properties of undefined` and left the table empty.
 * That bug is invisible to `node --check` (the file parses) and invisible to
 * reading the Python (the order that matters is the order the built HTML puts
 * the blobs in). Adding anything to `_page` therefore has to be proven by
 * running the page, not by inspecting it.
 *
 * The pages under test carry a deliberate copy of that hazard: a blob that
 * calls `DFMHL.chip` at evaluation time, emitted before the real Manager
 * Score and corpus-submit scripts. See tests/support/render_analytics.py.
 *
 * What is asserted, in one sentence: the inline execution sequence is
 * byte-for-byte the same with the beacon as without it, and the beacon is
 * a deferred external script in <head>, which is the only reason that
 * equivalence is structural rather than a coincidence of this build.
 *
 * The shim is deliberately small. It is not a browser and does not lay
 * anything out -- it cannot tell you the page LOOKS right, and it does not
 * fetch or run Cloudflare's beacon.min.js. It answers one question: do the
 * page's own scripts, in this order, run.
 */

import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const paths = process.argv.slice(2);
if (paths.length < 2) {
  console.error('usage: node analytics_head_tests.mjs <configured.html> <unconfigured.html>');
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

/* ------------------------------------------------------------------ DOM */

function makeElement(tag, id, className) {
  const el = {
    tagName: String(tag || 'div').toUpperCase(),
    id: id || '',
    className: className || '',
    style: {},
    dataset: {},
    _attrs: {},
    children: [],
    innerHTML: '',
    textContent: '',
    value: '',
    checked: false,
    disabled: false,
    _listeners: {},
  };
  el.classList = {
    add(c) { if (!el.className.split(/\s+/).includes(c)) el.className = (el.className + ' ' + c).trim(); },
    remove(c) { el.className = el.className.split(/\s+/).filter((x) => x && x !== c).join(' '); },
    contains(c) { return el.className.split(/\s+/).includes(c); },
    toggle(c) { if (el.classList.contains(c)) el.classList.remove(c); else el.classList.add(c); },
  };
  el.addEventListener = (type, fn) => { (el._listeners[type] ||= []).push(fn); };
  el.removeEventListener = (type, fn) => {
    el._listeners[type] = (el._listeners[type] || []).filter((f) => f !== fn);
  };
  el.dispatchEvent = (ev) => {
    (el._listeners[ev.type] || []).forEach((fn) => fn.call(el, ev));
    return true;
  };
  el.click = () => el.dispatchEvent({ type: 'click', preventDefault() {}, stopPropagation() {} });
  el.appendChild = (c) => { el.children.push(c); return c; };
  el.removeChild = (c) => { el.children = el.children.filter((x) => x !== c); return c; };
  el.insertBefore = (c) => { el.children.unshift(c); return c; };
  el.setAttribute = (k, v) => {
    el._attrs[k] = String(v);
    if (k === 'id') el.id = String(v);
    if (k === 'class') el.className = String(v);
  };
  el.getAttribute = (k) => (k === 'id' ? el.id : k === 'class' ? el.className
    : Object.prototype.hasOwnProperty.call(el._attrs, k) ? el._attrs[k] : null);
  el.hasAttribute = (k) => el.getAttribute(k) !== null;
  el.removeAttribute = (k) => { delete el._attrs[k]; };
  el.querySelector = () => null;
  el.querySelectorAll = () => [];
  el.closest = () => null;
  el.scrollIntoView = () => {};
  el.focus = () => {};
  el.remove = () => {};
  Object.defineProperty(el, 'firstChild', { get: () => el.children[0] || null });
  Object.defineProperty(el, 'parentNode', { get: () => null, configurable: true });
  return el;
}

function makeDocument(html) {
  const byId = new Map();
  const byClass = new Map();

  for (const m of html.matchAll(/<(\w+)[^>]*\sid="([^"]+)"[^>]*>/g)) {
    const id = m[2];
    if (byId.has(id)) continue;
    const cls = /\sclass="([^"]*)"/.exec(m[0]);
    byId.set(id, makeElement(m[1], id, cls ? cls[1] : ''));
  }
  for (const m of html.matchAll(/<(\w+)[^>]*\sclass="([^"]+)"[^>]*>/g)) {
    const classes = m[2].split(/\s+/).filter(Boolean);
    const idm = /\sid="([^"]+)"/.exec(m[0]);
    const el = idm && byId.has(idm[1]) ? byId.get(idm[1]) : makeElement(m[1], '', m[2]);
    for (const c of classes) {
      const list = byClass.get(c) || [];
      if (!list.includes(el)) list.push(el);
      byClass.set(c, list);
    }
  }

  const body = makeElement('body');
  const docListeners = {};

  const select = (sel) => {
    const s = String(sel || '').trim();
    if (s.startsWith('#')) {
      const el = byId.get(s.slice(1));
      return el ? [el] : [];
    }
    const cls = s.split(/[\s>]+/).pop();
    if (cls && cls.startsWith('.')) return byClass.get(cls.split('.').filter(Boolean).pop()) || [];
    return [];
  };

  // Bound to a name rather than returned as a literal: dispatchEvent calls
  // its handlers with the document as `this`, so it needs a reference to
  // the object it is being defined on.
  const document = {
    readyState: 'loading',
    getElementById: (id) => byId.get(id) || null,
    querySelector: (sel) => select(sel)[0] || null,
    querySelectorAll: (sel) => select(sel),
    createElement: (tag) => makeElement(tag),
    createTextNode: (t) => ({ nodeValue: String(t) }),
    createDocumentFragment: () => makeElement('fragment'),
    addEventListener: (type, fn) => { (docListeners[type] ||= []).push(fn); },
    removeEventListener: (type, fn) => {
      docListeners[type] = (docListeners[type] || []).filter((f) => f !== fn);
    },
    dispatchEvent: (ev) => {
      (docListeners[ev.type] || []).forEach((fn) => fn.call(document, ev));
      return true;
    },
    body,
    documentElement: makeElement('html'),
    head: makeElement('head'),
    location: { href: 'https://pstiehl.github.io/Dynasty-Football-Model/rankings.html', hash: '' },
    cookie: '',
    _byId: byId,
    _listeners: docListeners,
  };
  return document;
}

function makeContext(html, fetchImpl) {
  const document = makeDocument(html);
  const sandbox = {
    document, console, setTimeout, clearTimeout, setInterval, clearInterval,
    Promise, JSON, Math, Date, Object, Array, String, Number, Boolean, RegExp,
    Error, TypeError, Map, Set, isNaN, isFinite, parseInt, parseFloat,
    encodeURIComponent, decodeURIComponent, URL, AbortController,
    fetch: fetchImpl,
    localStorage: {
      _d: {},
      getItem(k) { return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null; },
      setItem(k, v) { this._d[k] = String(v); },
      removeItem(k) { delete this._d[k]; },
    },
    location: document.location,
    navigator: { userAgent: 'node-dom-shim', sendBeacon: () => true },
    history: { pushState() {}, replaceState() {} },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  sandbox.window.addEventListener = (type, fn) => document.addEventListener(type, fn);
  vm.createContext(sandbox);
  return sandbox;
}

/* A bland 404-ish answer for the page's own artifact fetches -- the pages
 * are built to degrade when an artifact is missing, so this exercises the
 * real path rather than inventing fixture data. */
function artifactResponse() {
  return { status: 404, ok: false, async json() { return {}; }, async text() { return ''; } };
}

/* INLINE scripts only, in document order: these are the ones that execute
 * during parse and can therefore collide with each other. */
function inlineBlobs(html) {
  return [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
}

function externalScripts(html) {
  return [...html.matchAll(/<script[^>]*\bsrc=[^>]*>[\s\S]*?<\/script>/g)].map((m) => m[0]);
}

function headOf(html) { return html.slice(0, html.indexOf('</head>')); }

/* --------------------------------------------------------- the real run */

function runPage(label, html) {
  const network = [];
  const ctx = makeContext(html, async (url) => {
    network.push(String(url));
    return artifactResponse();
  });
  const blobs = inlineBlobs(html);
  ok(blobs.length >= 4, `${label}: page carries its inline script blobs`, `got ${blobs.length}`);

  blobs.forEach((code, i) => {
    let threw = null;
    try {
      vm.runInContext(code, ctx, { filename: `${label}-blob-${i}.js`, timeout: 20000 });
    } catch (e) {
      threw = e;
    }
    ok(!threw, `${label}: inline script blob ${i} evaluates without throwing`,
       threw ? `${threw.name}: ${threw.message}` : '');
  });

  ctx.document.readyState = 'interactive';
  let domThrew = null;
  try {
    ctx.document.dispatchEvent({ type: 'DOMContentLoaded' });
  } catch (e) {
    domThrew = e;
  }
  ok(!domThrew, `${label}: DOMContentLoaded handlers run without throwing`,
     domThrew ? `${domThrew.name}: ${domThrew.message}` : '');

  return { ctx, blobs, network };
}

/* The PR #71 assertion, restated for this change: the renderer is defined
 * before the page script that consumes it during evaluation, and the probe
 * actually reached DFMHL rather than being skipped. */
function assertHazardSurvived(label, ctx, html) {
  const blobs = inlineBlobs(html);
  const hlIdx = blobs.findIndex((b) => /DFMHL\s*=/.test(b));
  const probeIdx = blobs.findIndex((b) => /DFM_ORDER_PROBE\s*=/.test(b));
  ok(hlIdx >= 0, `${label}: shared highlight renderer is on the page`);
  ok(probeIdx >= 0, `${label}: the PR #71 hazard probe is on the page`);
  ok(hlIdx >= 0 && probeIdx >= 0 && hlIdx < probeIdx,
     `${label}: renderer is defined BEFORE the page script that uses it (PR #71)`,
     `hl=${hlIdx} probe=${probeIdx}`);
  ok(ctx.DFM_ORDER_PROBE === 'ok',
     `${label}: the probe really called DFMHL.chip during evaluation and got markup`,
     String(ctx.DFM_ORDER_PROBE));
  ok(typeof ctx.DFMHL === 'object' && typeof ctx.DFMHL.chip === 'function',
     `${label}: DFMHL is live in the page context after evaluation`);
  ok(typeof ctx.msScoreLeague === 'function',
     `${label}: the real Manager Score scorer evaluated too`);
  ok(typeof ctx.csOnScored === 'function',
     `${label}: the real corpus-submit script evaluated too`);
}

/* ============================================================ configured */

const configuredHtml = readFileSync(paths[0], 'utf8');
const unconfiguredHtml = readFileSync(paths[1], 'utf8');

const configured = runPage('configured', configuredHtml);
assertHazardSurvived('configured', configured.ctx, configuredHtml);

/* The beacon must be an EXTERNAL DEFERRED script in <head>. That is the
 * whole safety argument: a deferred external script is specified to run
 * after the document is parsed, so it cannot interleave with any inline
 * blob above -- the ordering PR #71 fixed is untouched by construction,
 * not by luck. */
{
  const beacons = externalScripts(configuredHtml)
    .filter((s) => /cloudflareinsights\.com/.test(s));
  ok(beacons.length === 1, 'configured: exactly one beacon script tag',
     `count=${beacons.length}`);
  if (beacons.length === 1) {
    const tag = beacons[0];
    ok(/\bdefer\b/.test(tag), 'configured: the beacon is deferred', tag.slice(0, 100));
    ok(/<script[^>]*><\/script>$/.test(tag),
       'configured: the beacon tag carries no inline body', tag.slice(-40));
    ok(headOf(configuredHtml).includes(tag),
       'configured: the beacon lives in <head>, out of the body execution sequence');

    const payload = /data-cf-beacon='([^']*)'/.exec(tag);
    ok(payload !== null, 'configured: the beacon carries a data-cf-beacon payload');
    if (payload) {
      let parsed = null;
      try { parsed = JSON.parse(payload[1]); } catch (e) { parsed = null; }
      ok(parsed !== null, 'configured: the payload is valid JSON a browser can read',
         payload[1].slice(0, 60));
      ok(parsed && typeof parsed.token === 'string' && parsed.token.length > 0,
         'configured: the payload carries a non-empty token');
      ok(parsed && Object.keys(parsed).length === 1,
         'configured: the payload carries the token and nothing else');
    }
  }
}

/* No inline blob mentions the beacon: it is not part of page logic, so
 * nothing on the page can come to depend on analytics being configured. */
ok(!configured.blobs.some((b) => /cloudflareinsights|cf-beacon/i.test(b)),
   'configured: no inline page script references the beacon');

/* Executing the page must not have contacted Cloudflare. The shim does not
 * load external scripts, which is the point -- it proves the site's own
 * behaviour does not route through the beacon. */
ok(!configured.network.some((u) => /cloudflareinsights/.test(u)),
   'configured: running the page makes no Cloudflare request of its own',
   configured.network.join(','));

/* ========================================================== unconfigured */

const unconfigured = runPage('unconfigured', unconfiguredHtml);
assertHazardSurvived('unconfigured', unconfigured.ctx, unconfiguredHtml);

ok(!/cloudflareinsights/.test(unconfiguredHtml),
   'unconfigured: no beacon markup anywhere on the page');
ok(!/data-cf-beacon/.test(unconfiguredHtml),
   'unconfigured: no leftover beacon attribute');
ok(externalScripts(unconfiguredHtml).filter((s) => /src=["']{2}/.test(s)).length === 0,
   'unconfigured: no script tag with an empty src');
ok(!/<script[^>]*>\s*<\/script>/.test(headOf(unconfiguredHtml)),
   'unconfigured: no empty script tag left in <head>');

/* ====================================================== the equivalence */

/* The strongest statement available without a browser: the two builds
 * execute the SAME inline code in the SAME order. Whatever analytics does,
 * it does not change what the page runs. */
{
  const a = inlineBlobs(configuredHtml);
  const b = inlineBlobs(unconfiguredHtml);
  ok(a.length === b.length,
     'both builds carry the same number of inline scripts', `${a.length} vs ${b.length}`);
  let identical = a.length === b.length;
  for (let i = 0; identical && i < a.length; i++) identical = a[i] === b[i];
  ok(identical, 'every inline script is byte-identical in both builds');
  ok(configured.ctx.DFM_ORDER_PROBE === unconfigured.ctx.DFM_ORDER_PROBE,
     'the hazard probe reaches the same result in both builds',
     `${configured.ctx.DFM_ORDER_PROBE} vs ${unconfigured.ctx.DFM_ORDER_PROBE}`);
}

/* -------------------------------------------------------------- summary */

if (failures) {
  console.error(`\n${failures} failed of ${checks} checks`);
  process.exit(1);
}
console.log(`analytics head/order: ${checks} checks passed`);
