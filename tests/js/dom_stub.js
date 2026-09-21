// Minimal browser stand-ins, prepended to the real page scripts by
// scripts/check_site_behaviour.py.
//
// This is deliberately not a DOM implementation. It is the smallest set of
// objects that lets reel.py's and myteam.py's actual shipped JavaScript run
// under node so its *logic* -- rank resolution, unranked reasons, window
// filtering, league scoring -- can be asserted on. Rendering is not being
// tested here and cannot be: there is no layout engine and no browser.
//
// Elements are auto-created on first getElementById, so a page script can
// write to any id without the harness having to declare it up front.

const ELEMENTS = {};

function makeEl(id) {
  const el = {
    id: id,
    innerHTML: '',
    textContent: '',
    value: '',
    checked: false,
    className: '',
    disabled: false,
    dataset: {},
    style: {},
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      contains(c) { return this._set.has(c); }
    },
    addEventListener() {},
    removeEventListener() {},
    scrollIntoView() {},
    appendChild() {},
    querySelector() { return null; },
    querySelectorAll() { return []; }
  };
  return el;
}

const document = {
  getElementById(id) {
    if (!ELEMENTS[id]) ELEMENTS[id] = makeEl(id);
    return ELEMENTS[id];
  },
  querySelectorAll() { return []; },
  querySelector() { return null; },
  addEventListener() {},
  activeElement: { tagName: 'BODY' }
};

const window = {
  open() {},
  addEventListener() {}
};

const localStorage = {
  _v: {},
  getItem(k) { return Object.prototype.hasOwnProperty.call(this._v, k) ? this._v[k] : null; },
  setItem(k, v) { this._v[k] = String(v); }
};

// Fetch is backed by a fixture table the assertions fill in. An unknown URL
// 404s, which is the path both pages' "artifact missing" branches take.
const FIXTURES = {};

function fetch(url) {
  const key = String(url).split('/').pop();
  if (Object.prototype.hasOwnProperty.call(FIXTURES, key)) {
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve(FIXTURES[key])
    });
  }
  return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve(null) });
}

// The IFrame API is never constructed here; the reel only touches YT from
// inside a click handler, and no assertion drives playback.
const YT = { Player: function () {}, PlayerState: { PLAYING: 1, CUED: 5 } };
