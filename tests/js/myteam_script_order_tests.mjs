/* myteam.html — script ORDER and corpus wiring, executed rather than read.
 *
 *   node tests/js/myteam_script_order_tests.mjs <configured.html> <unconfigured.html>
 *
 * Exits 0 on success, 1 with a report.
 *
 * Why this file exists
 * --------------------
 * PR #71: `report._page` emits the shared highlight renderer AFTER the page
 * body, so a page script that touched `DFMHL` while being *evaluated* threw
 * `TypeError: Cannot read properties of undefined` and took the whole page
 * with it. That bug is invisible to `node --check` — the file parses fine —
 * and invisible to reading, because the order that matters is the order the
 * built HTML puts the blobs in, not the order the Python source mentions
 * them.
 *
 * Manager Score moved into myteam.html (PR #69) and the corpus submit script
 * moved with it, so the corpus blob is now emitted on a page that has this
 * hazard. The only way to know it is safe is to run it in that order.
 *
 * So: take the REAL rendered page, pull every <script> out in document
 * order, and evaluate them one at a time in one shared context over a DOM
 * shim. A blob that throws during evaluation fails here the way it would
 * fail in a browser.
 *
 * The shim is deliberately small. It is not a browser and does not lay
 * anything out — it cannot tell you the page LOOKS right. It answers one
 * question: does this script, in this position, run.
 */

import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const paths = process.argv.slice(2);
if (paths.length < 2) {
  console.error('usage: node myteam_script_order_tests.mjs <configured.html> <unconfigured.html>');
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

/* Seed a registry from the ids and classes the real page actually contains,
 * so getElementById answers for the real element set rather than a guess. */
function makeDocument(html) {
  const byId = new Map();
  const byClass = new Map();

  for (const m of html.matchAll(/<(\w+)[^>]*\sid="([^"]+)"[^>]*>/g)) {
    const tag = m[1];
    const id = m[2];
    if (byId.has(id)) continue;
    const cls = /\sclass="([^"]*)"/.exec(m[0]);
    byId.set(id, makeElement(tag, id, cls ? cls[1] : ''));
  }
  for (const m of html.matchAll(/<(\w+)[^>]*\sclass="([^"]+)"[^>]*>/g)) {
    const tag = m[1];
    const classes = m[2].split(/\s+/).filter(Boolean);
    const idm = /\sid="([^"]+)"/.exec(m[0]);
    const el = idm && byId.has(idm[1]) ? byId.get(idm[1]) : makeElement(tag, '', m[2]);
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
    // Take the last simple class token: '.a .b' and '.a.b' both land on 'b'.
    const cls = s.split(/[\s>]+/).pop();
    if (cls && cls.startsWith('.')) return byClass.get(cls.split('.').filter(Boolean).pop()) || [];
    return [];
  };

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
    location: { href: 'https://pstiehl.github.io/myteam.html', hash: '' },
    cookie: '',
    _byId: byId,
    _listeners: docListeners,
  };
  return document;
}

function makeContext(html, fetchImpl) {
  const document = makeDocument(html);
  const sandbox = {
    document,
    console,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    Promise,
    JSON,
    Math,
    Date,
    Object,
    Array,
    String,
    Number,
    Boolean,
    RegExp,
    Error,
    TypeError,
    Map,
    Set,
    isNaN,
    isFinite,
    parseInt,
    parseFloat,
    encodeURIComponent,
    decodeURIComponent,
    URL,
    AbortController,
    fetch: fetchImpl,
    localStorage: {
      _d: {},
      getItem(k) { return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null; },
      setItem(k, v) { this._d[k] = String(v); },
      removeItem(k) { delete this._d[k]; },
    },
    location: document.location,
    navigator: { userAgent: 'node-dom-shim' },
    history: { pushState() {}, replaceState() {} },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  sandbox.window.addEventListener = (type, fn) => document.addEventListener(type, fn);
  vm.createContext(sandbox);
  return sandbox;
}

/* A bland 404-ish answer for the page's own artifact fetches. The page is
 * built to degrade when an artifact is missing, so this exercises that path
 * rather than inventing fixture data the site would never see. */
function artifactResponse() {
  return {
    status: 404,
    ok: false,
    async json() { return {}; },
    async text() { return ''; },
  };
}

function scriptBlobs(html) {
  return [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)]
    .map((m) => m[1]);
}

/* --------------------------------------------------------- the real run */

function runPage(label, html, fetchImpl) {
  const ctx = makeContext(html, fetchImpl);
  const blobs = scriptBlobs(html);
  ok(blobs.length >= 6, `${label}: page carries every script blob`, `got ${blobs.length}`);

  blobs.forEach((code, i) => {
    let threw = null;
    try {
      vm.runInContext(code, ctx, { filename: `${label}-blob-${i}.js`, timeout: 20000 });
    } catch (e) {
      threw = e;
    }
    ok(!threw, `${label}: script blob ${i} evaluates without throwing`,
       threw ? `${threw.name}: ${threw.message}` : '');
  });

  // The event every one of these scripts binds its wiring to.
  ctx.document.readyState = 'interactive';
  let domThrew = null;
  try {
    ctx.document.dispatchEvent({ type: 'DOMContentLoaded' });
  } catch (e) {
    domThrew = e;
  }
  ok(!domThrew, `${label}: DOMContentLoaded handlers run without throwing`,
     domThrew ? `${domThrew.name}: ${domThrew.message}` : '');

  return ctx;
}

/* Pin the emission order of the shared highlight renderer against the corpus
 * script, and pin the property that makes the corpus script safe regardless.
 *
 * PR #71 moved the renderer from the END of the body to the TOP, because a
 * page script calling DFMHL.chip during evaluation threw and left the
 * Dynasty Rankings table empty. This asserts that fix still holds on the
 * page the corpus script ships on -- if anyone moves it back, this fails
 * here rather than silently on the live site.
 *
 * The second assertion is the independent one: the corpus script does not
 * touch DFMHL at all, so it survives either order. Both are kept, because
 * the first protects the page and the second protects this feature. */
function assertRendererOrder(label, html) {
  const blobs = scriptBlobs(html);
  const corpusIdx = blobs.findIndex((b) => /function csOnScored/.test(b));
  const hlIdx = blobs.findIndex((b, i) => i !== corpusIdx && /DFMHL\s*=/.test(b));
  ok(corpusIdx >= 0, `${label}: corpus submit script is on the page`);
  ok(hlIdx >= 0, `${label}: shared highlight renderer is on the page`);
  if (corpusIdx >= 0 && hlIdx >= 0) {
    ok(hlIdx < corpusIdx,
       `${label}: highlight renderer is defined BEFORE the page scripts (PR #71)`,
       `hl=${hlIdx} corpus=${corpusIdx}`);
    ok(!/DFMHL/.test(blobs[corpusIdx]),
       `${label}: corpus script never references DFMHL, so it is order-independent`);
  }
}

/* ------------------------------------------------ synthetic scored league */

/* Same league shape as tests/js/corpus_backend_tests.mjs, built and scored by
 * the page's OWN msScoreLeague — the copy that was just evaluated in context,
 * not a second import. */
const FIXTURE = `
(function () {
  var MGR_IDS = [];
  for (var i = 1; i <= 12; i++) {
    MGR_IDS.push('8600000000000000' + (i < 10 ? '0' + i : String(i)));
  }
  var managers = MGR_IDS.map(function (id, i) {
    return { id: id, name: 'manager' + (i + 1) };
  });
  var picks = [];
  for (var round = 0; round < 6; round++) {
    for (var j = 0; j < MGR_IDS.length; j++) {
      var slot = round * MGR_IDS.length + j + 1;
      picks.push({
        managerId: MGR_IDS[j], slot: slot, name: 'Player ' + slot, pos: 'WR',
        evaluable: true,
        capture: Math.max(200, 9000 - slot * 260) + (j % 4 === 0 ? 1400 : 0)
                   - (j % 3 === 0 ? 700 : 0),
        vAt: 1000, peak: 2000,
      });
    }
  }
  function asset(label, cap) {
    return { kind: 'player', label: label, evaluable: true, capture: cap,
             vAt: 1000, peak: 1000 + cap };
  }
  var trades = [
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
  var waivers = [];
  for (var k = 0; k < MGR_IDS.length; k++) {
    for (var w = 0; w < 2; w++) {
      waivers.push({
        managerId: MGR_IDS[k], date: '2026-09-0' + (w + 1),
        name: 'Waiver ' + k + '-' + w, pos: 'RB', evaluable: true,
        capture: (k * 137 + w * 91) % 900, vAt: 100, peak: 300,
      });
    }
  }
  var scored = msScoreLeague({ managers: managers, drafts: [{
    id: 'd1', season: '2026', label: '2026 Startup', picks: picks,
  }], trades: trades, waivers: waivers }, {});
  /* settings.type == 2 is the dynasty marker csEligibility requires; a
   * redraft league is refused before it can cost the worker a round trip. */
  var chain = [{ league_id: '1316222126914539520', name: 'Dallas Kings',
                 season: '2026', total_rosters: 12,
                 settings: { type: 2 } }];
  return { scored: scored, chain: chain };
})()
`;

/* =========================================================== unconfigured */

{
  const html = readFileSync(paths[1], 'utf8');
  // The page fetches its own artifacts (engine_rankings.json, highlights.json
  // …) on DOMContentLoaded. Those are not what this file is about, so they
  // are answered blandly and only /corpus/ traffic is counted.
  const corpusCalls = [];
  const ctx = runPage('unconfigured', html, async (url) => {
    if (/\/corpus\//.test(String(url))) corpusCalls.push(String(url));
    return artifactResponse();
  });

  assertRendererOrder('unconfigured', html);

  ok(typeof ctx.csOnScored === 'function',
     'unconfigured: csOnScored is still defined (the hook exists, inert)');
  ok(ctx.CS_URL === '', 'unconfigured: CS_URL is empty', String(ctx.CS_URL));
  ok(ctx.csEnabled() === false, 'unconfigured: csEnabled() is false');

  const fx = vm.runInContext(FIXTURE, ctx, { timeout: 30000 });
  ok(fx.scored.managers.length === 12,
     'unconfigured: the page\'s own scorer scored 12 managers');

  const box = ctx.document.getElementById('ms-corpus-state');
  ok(box !== null, 'unconfigured: ms-corpus-state exists in the DOM');

  corpusCalls.length = 0;
  const res = ctx.csOnScored('1316222126914539520', fx.scored, fx.chain);
  await Promise.resolve(res);
  ok(corpusCalls.length === 0,
     'unconfigured: scoring a league makes NO corpus network call',
     corpusCalls.join(','));
  /* The box is SHOWN and says the score was not recorded.
   *
   * This assertion used to be the exact opposite: the box stayed hidden,
   * on the reasoning that the page should say nothing about an index that
   * does not exist. Saying nothing turned out not to be neutral. The
   * section still scores the league and still draws a full table, so a
   * visitor who clicks "Score this league" sees something obviously happen
   * and reasonably concludes their league is now indexed. The owner hit
   * exactly that: submitted a league, clicked the button, and nothing was
   * recorded anywhere with nothing on the page to say so.
   *
   * Silence about a missing backend is indistinguishable from success, so
   * the unconfigured build now states the outcome plainly. */
  ok(box.style.display === 'block',
     'unconfigured: the corpus box is SHOWN, so the page states the outcome',
     `display=${box.style.display}`);
  ok(/not recorded/i.test(box.innerHTML),
     'unconfigured: the copy says the score was not recorded',
     String(box.innerHTML).slice(0, 120));
  ok(!/callout-warn/.test(box.className),
     'unconfigured: it is not styled as an error — nothing failed',
     box.className);
  ok(/league-submission\.yml/.test(box.innerHTML),
     'unconfigured: the copy points at the issue path that actually works',
     String(box.innerHTML).slice(0, 200));

  // The opt-in must not exist at all when there is nowhere to submit.
  ok(!/id="ms-corpus-optin"/.test(html),
     'unconfigured: no opt-in checkbox is rendered');
  ok(!/cross-league index<\/a>/.test(html),
     'unconfigured: no opt-in prose is rendered');
}

/* ============================================================= configured */

{
  const html = readFileSync(paths[0], 'utf8');
  const calls = [];
  const fetchImpl = async (url, opts) => {
    if (!/\/corpus\//.test(String(url))) return artifactResponse();
    calls.push({ url: String(url), opts: opts || {} });
    const body = { state: 'provisional', league_id: '1316222126914539520' };
    return {
      status: 202,
      ok: true,
      async json() { return body; },
      async text() { return JSON.stringify(body); },
    };
  };
  const ctx = runPage('configured', html, fetchImpl);

  assertRendererOrder('configured', html);

  ok(typeof ctx.csOnScored === 'function', 'configured: csOnScored is defined');
  ok(ctx.csEnabled() === true, 'configured: csEnabled() is true', String(ctx.CS_URL));

  // The Manager Score section really is where the opt-in lives now.
  const pane = html.slice(html.indexOf('id="view-managerscore"'));
  const paneEnd = pane.indexOf('<style>');
  const paneHtml = pane.slice(0, paneEnd > 0 ? paneEnd : pane.length);
  ok(/id="ms-corpus-optin"/.test(paneHtml),
     'configured: the opt-in checkbox renders inside the embedded Manager Score section');
  ok(/id="ms-corpus-state"/.test(paneHtml),
     'configured: the corpus status box renders inside the embedded Manager Score section');
  ok(/id="ms-username"/.test(paneHtml),
     'configured: the section really is the Manager Score section');

  const fx = vm.runInContext(FIXTURE, ctx, { timeout: 30000 });
  ok(fx.scored.managers.length === 12, 'configured: scorer produced 12 managers');

  const optin = ctx.document.getElementById('ms-corpus-optin');
  ok(optin !== null, 'configured: opt-in element is in the DOM');
  if (optin) optin.checked = true;

  await ctx.csOnScored('1316222126914539520', fx.scored, fx.chain);

  ok(calls.length === 1, 'configured: exactly one submit call', `calls=${calls.length}`);
  if (calls.length) {
    ok(/\/corpus\/submit$/.test(calls[0].url),
       'configured: it posts to /corpus/submit', calls[0].url);
    ok((calls[0].opts.method || '').toUpperCase() === 'POST',
       'configured: it is a POST', String(calls[0].opts.method));
    const payload = JSON.parse(calls[0].opts.body);
    ok(payload.schema === 'dfm.corpus.submission.v1',
       'configured: payload carries the versioned schema', payload.schema);
    ok(payload.managers.length === 12,
       'configured: payload carries every scored manager');
    ok(payload.league.league_id === '1316222126914539520',
       'configured: payload carries the league id');
    const hasIp = JSON.stringify(payload).includes('ip');
    ok(!hasIp, 'configured: payload contains no ip field');
  }

  const box = ctx.document.getElementById('ms-corpus-state');
  ok(box && box.style.display === 'block',
     'configured: the corpus box becomes visible after submit');
  ok(box && /provisional/i.test(box.innerHTML),
     'configured: it reports provisional, not confirmed',
     box ? String(box.innerHTML).slice(0, 120) : 'no box');

  // Opting out must stop the submission dead.
  calls.length = 0;
  if (optin) optin.checked = false;
  await ctx.csOnScored('1316222126914539520', fx.scored, fx.chain);
  ok(calls.length === 0, 'configured: unticking the opt-in makes NO corpus call',
     `calls=${calls.length}`);
}

/* -------------------------------------------------------------- summary */

if (failures) {
  console.error(`\n${failures} failed of ${checks} checks`);
  process.exit(1);
}
console.log(`myteam script order: ${checks} checks passed`);
