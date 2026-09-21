// A small *parsing* DOM, for assertions that depend on element identity.
//
// This exists because tests/js/dom_stub.js cannot express the bug this file
// was written to catch. That stub auto-creates an element on every
// getElementById, so `var row = xlEl(id); if (!row) return;` can never take
// its early-return branch and a missing detail row is indistinguishable from
// a present one. Both PR #74 and PR #75 were green against it and still
// shipped a drill-down that did nothing in the browser.
//
// So: real tag parsing, a real id index built from the rendered markup, and a
// getElementById that returns **null** for an id nobody emitted. Duplicate
// ids are retained rather than collapsed, because "the same id was emitted by
// two tables" is itself a defect this harness has to be able to see.
//
// Still not a browser: no layout, no CSS cascade, no real hit-testing. It
// answers "which element does this id resolve to, and what did the handler do
// to it", which is exactly the class of bug at issue. Appearance is out of
// scope here and cannot be checked without a browser.

const VOID = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img',
  'input', 'link', 'meta', 'param', 'source', 'track', 'wbr']);

const TAG_RE = new RegExp(
  '<!--[\\s\\S]*?-->' +
  '|<\\/([A-Za-z][\\w:-]*)\\s*>' +
  '|<([A-Za-z][\\w:-]*)((?:\\s+[^\\s"\'>\\/=]+(?:\\s*=\\s*(?:"[^"]*"|\'[^\']*\'|[^\\s"\'`=<>]+))?)*)\\s*(\\/?)>',
  'g');

const ATTR_RE =
  /([^\s"'>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'`=<>]+)))?/g;

function decode(s) {
  return String(s)
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'")
    .replace(/&amp;/g, '&');
}

function parseAttrs(src) {
  const out = {};
  if (!src) return out;
  ATTR_RE.lastIndex = 0;
  let m;
  while ((m = ATTR_RE.exec(src))) {
    const name = m[1];
    if (!name) continue;
    const raw = m[2] !== undefined ? m[2]
      : m[3] !== undefined ? m[3]
      : m[4] !== undefined ? m[4] : '';
    out[name.toLowerCase()] = decode(raw);
  }
  return out;
}

function camel(name) {
  return name.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
}

class TextNode {
  constructor(text) {
    this.nodeType = 3;
    this.data = text;
    this.parentNode = null;
  }
  get textContent() { return decode(this.data); }
  get outerHTML() { return this.data; }
}

class El {
  constructor(tag, attrs, doc) {
    this.nodeType = 1;
    this.tagName = String(tag).toUpperCase();
    this.localName = String(tag).toLowerCase();
    this.attributes = attrs || {};
    this.childNodes = [];
    this.parentNode = null;
    this.ownerDocument = doc;

    this.dataset = {};
    this.style = {};
    this._classes = new Set();
    this._syncFromAttrs();
  }

  _syncFromAttrs() {
    this.dataset = {};
    for (const k of Object.keys(this.attributes)) {
      if (k.startsWith('data-')) {
        this.dataset[camel(k.slice(5))] = this.attributes[k];
      }
    }
    this.id = this.attributes.id || '';
    this._classes = new Set(
      String(this.attributes.class || '').split(/\s+/).filter(Boolean));
    // Inline style is parsed, not assumed: the renderer ships the detail row
    // as style="display:none", and the initial closed state has to be read
    // from the markup rather than taken on trust.
    this.style = {};
    const sty = this.attributes.style || '';
    for (const part of sty.split(';')) {
      const i = part.indexOf(':');
      if (i < 0) continue;
      this.style[camel(part.slice(0, i).trim())] = part.slice(i + 1).trim();
    }
    if (Object.prototype.hasOwnProperty.call(this.attributes, 'hidden')) {
      this.hidden = true;
    }
  }

  get className() { return [...this._classes].join(' '); }
  set className(v) {
    this._classes = new Set(String(v).split(/\s+/).filter(Boolean));
  }

  get classList() {
    const self = this;
    return {
      add(c) { self._classes.add(c); },
      remove(c) { self._classes.delete(c); },
      contains(c) { return self._classes.has(c); },
      toggle(c) {
        if (self._classes.has(c)) self._classes.delete(c);
        else self._classes.add(c);
      }
    };
  }

  setAttribute(name, value) {
    this.attributes[String(name).toLowerCase()] = String(value);
    this._syncFromAttrs();
  }
  getAttribute(name) {
    const k = String(name).toLowerCase();
    return Object.prototype.hasOwnProperty.call(this.attributes, k)
      ? this.attributes[k] : null;
  }
  removeAttribute(name) {
    const k = String(name).toLowerCase();
    delete this.attributes[k];
    if (k === 'hidden') this.hidden = false;
    this._syncFromAttrs();
  }
  hasAttribute(name) {
    return Object.prototype.hasOwnProperty.call(
      this.attributes, String(name).toLowerCase());
  }

  appendChild(node) {
    node.parentNode = this;
    this.childNodes.push(node);
    if (this.ownerDocument) this.ownerDocument._register(node);
    return node;
  }

  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }

  get innerHTML() {
    return this.childNodes.map((n) => n.outerHTML).join('');
  }

  // Replacing innerHTML retires the old subtree's ids and registers the new
  // one's, the same way a browser would. xlPaintDetail writes whole panels
  // through this path, so an id that only appears after a repaint still has
  // to become resolvable.
  set innerHTML(html) {
    for (const child of this.childNodes) {
      if (this.ownerDocument) this.ownerDocument._unregister(child);
    }
    this.childNodes = [];
    const nodes = parseFragment(String(html), this.ownerDocument);
    for (const n of nodes) this.appendChild(n);
  }

  get outerHTML() {
    const attrs = Object.keys(this.attributes)
      .map((k) => ` ${k}="${this.attributes[k]}"`).join('');
    if (VOID.has(this.localName)) return `<${this.localName}${attrs}>`;
    return `<${this.localName}${attrs}>${this.innerHTML}</${this.localName}>`;
  }

  get textContent() {
    return this.childNodes.map((n) => n.textContent).join('');
  }
  set textContent(v) {
    for (const child of this.childNodes) {
      if (this.ownerDocument) this.ownerDocument._unregister(child);
    }
    this.childNodes = [new TextNode(String(v))];
    this.childNodes[0].parentNode = this;
  }

  matches(sel) { return matchesSelector(this, sel); }

  closest(sel) {
    let node = this;
    while (node && node.nodeType === 1) {
      if (matchesSelector(node, sel)) return node;
      node = node.parentNode;
    }
    return null;
  }

  querySelectorAll(sel) { return queryAll(this, sel); }
  querySelector(sel) {
    const all = queryAll(this, sel);
    return all.length ? all[0] : null;
  }

  addEventListener() {}
  removeEventListener() {}
  scrollIntoView() {}
  focus() { if (this.ownerDocument) this.ownerDocument.activeElement = this; }
}

// Selector support is deliberately only what the shipped page actually uses:
// 'tr.xl-row', '[data-dfm-player]', '.dfm-hl-sheet-body', '#id'. An
// unsupported selector throws rather than silently matching nothing, so a
// future selector cannot quietly void an assertion.
function parseSelector(sel) {
  const s = String(sel).trim();
  const m = /^([a-zA-Z][\w:-]*)?((?:[.#][\w-]+|\[[\w-]+\])*)$/.exec(s);
  if (!m) throw new Error('mini_dom: unsupported selector ' + JSON.stringify(sel));
  const out = { tag: m[1] ? m[1].toLowerCase() : null, classes: [], id: null, attrs: [] };
  const rest = m[2] || '';
  const partRe = /[.#][\w-]+|\[[\w-]+\]/g;
  let p;
  while ((p = partRe.exec(rest))) {
    const tok = p[0];
    if (tok[0] === '.') out.classes.push(tok.slice(1));
    else if (tok[0] === '#') out.id = tok.slice(1);
    else out.attrs.push(tok.slice(1, -1).toLowerCase());
  }
  return out;
}

function matchesSelector(el, sel) {
  if (!el || el.nodeType !== 1) return false;
  const q = parseSelector(sel);
  if (q.tag && el.localName !== q.tag) return false;
  if (q.id && el.id !== q.id) return false;
  for (const c of q.classes) if (!el._classes.has(c)) return false;
  for (const a of q.attrs) if (!el.hasAttribute(a)) return false;
  return true;
}

function walk(root, fn) {
  for (const child of root.childNodes || []) {
    if (child.nodeType === 1) {
      fn(child);
      walk(child, fn);
    }
  }
}

function queryAll(root, sel) {
  const out = [];
  walk(root, (el) => { if (matchesSelector(el, sel)) out.push(el); });
  return out;
}

function parseFragment(html, doc) {
  const root = { childNodes: [], nodeType: 1 };
  const stack = [root];
  let last = 0;
  TAG_RE.lastIndex = 0;
  let m;

  const pushText = (text) => {
    if (!text) return;
    const node = new TextNode(text);
    const parent = stack[stack.length - 1];
    node.parentNode = parent === root ? null : parent;
    parent.childNodes.push(node);
  };

  while ((m = TAG_RE.exec(html))) {
    pushText(html.slice(last, m.index));
    last = m.index + m[0].length;

    if (m[0].startsWith('<!--')) continue;

    if (m[1]) {
      // Closing tag: unwind to the nearest matching open element, tolerating
      // the implicit closes real HTML allows.
      const name = m[1].toLowerCase();
      for (let i = stack.length - 1; i > 0; i--) {
        if (stack[i].localName === name) {
          stack.length = i;
          break;
        }
      }
      continue;
    }

    const tag = m[2].toLowerCase();
    const el = new El(tag, parseAttrs(m[3]), doc);
    const parent = stack[stack.length - 1];
    el.parentNode = parent === root ? null : parent;
    parent.childNodes.push(el);
    if (!VOID.has(tag) && !m[4]) stack.push(el);
  }
  pushText(html.slice(last));
  return root.childNodes;
}

export function createDocument() {
  const idIndex = new Map();
  const handlers = { click: [], keydown: [] };

  const doc = {
    readyState: 'complete',
    activeElement: null,

    // The point of the whole file: an id nobody rendered resolves to null,
    // and an id two tables both rendered resolves to the *first*, exactly as
    // a browser does.
    getElementById(id) {
      const hits = idIndex.get(String(id));
      return hits && hits.length ? hits[0] : null;
    },

    // Diagnostics the assertions use; not part of the DOM API.
    _idCount(id) {
      const hits = idIndex.get(String(id));
      return hits ? hits.length : 0;
    },
    _duplicateIds() {
      const dupes = [];
      for (const [id, els] of idIndex) if (els.length > 1) dupes.push([id, els.length]);
      return dupes;
    },
    _allIds() { return [...idIndex.keys()]; },

    _register(node) {
      if (!node || node.nodeType !== 1) return;
      if (node.id) {
        if (!idIndex.has(node.id)) idIndex.set(node.id, []);
        idIndex.get(node.id).push(node);
      }
      walk(node, (el) => {
        if (!el.id) return;
        if (!idIndex.has(el.id)) idIndex.set(el.id, []);
        idIndex.get(el.id).push(el);
      });
    },
    _unregister(node) {
      if (!node || node.nodeType !== 1) return;
      const drop = (el) => {
        if (!el.id) return;
        const hits = idIndex.get(el.id);
        if (!hits) return;
        const i = hits.indexOf(el);
        if (i >= 0) hits.splice(i, 1);
        if (!hits.length) idIndex.delete(el.id);
      };
      drop(node);
      walk(node, drop);
    },

    createElement(tag) { return new El(tag, {}, doc); },
    querySelectorAll(sel) { return queryAll(doc, sel); },
    querySelector(sel) {
      const all = queryAll(doc, sel);
      return all.length ? all[0] : null;
    },

    addEventListener(type, fn) {
      if (!handlers[type]) handlers[type] = [];
      handlers[type].push(fn);
    },
    removeEventListener(type, fn) {
      if (!handlers[type]) return;
      const i = handlers[type].indexOf(fn);
      if (i >= 0) handlers[type].splice(i, 1);
    },
    _handlerCount(type) { return (handlers[type] || []).length; },

    // Dispatch in registration order, honouring stopPropagation. That is the
    // mechanism the chip relies on to open film without also toggling the row
    // it sits inside, so the harness has to model it rather than assume it.
    _fire(type, target, extra) {
      const rec = { defaultPrevented: false, propagationStopped: false, errors: [] };
      const ev = Object.assign({
        type,
        target,
        preventDefault() { rec.defaultPrevented = true; },
        stopPropagation() { rec.propagationStopped = true; },
        stopImmediatePropagation() { rec.propagationStopped = true; }
      }, extra || {});
      for (const fn of (handlers[type] || []).slice()) {
        try {
          fn(ev);
        } catch (e) {
          // A throw in an unrelated listener must not be mistaken for the
          // page choosing not to act; record it and carry on.
          rec.errors.push(String((e && e.message) || e));
        }
        if (rec.propagationStopped) break;
      }
      return rec;
    }
  };

  doc.documentElement = new El('html', {}, doc);
  doc.body = new El('body', {}, doc);
  doc.documentElement.appendChild(doc.body);

  // Container ids the real page ships in static HTML. Everything else has to
  // be produced by a renderer to become resolvable.
  doc.mountContainers = (ids) => {
    const made = {};
    for (const id of ids) {
      const el = new El('div', { id }, doc);
      doc.body.appendChild(el);
      made[id] = el;
    }
    return made;
  };

  return doc;
}

export { El, TextNode, parseFragment, queryAll, matchesSelector };
