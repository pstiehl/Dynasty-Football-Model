"""One player-highlights renderer for the whole site.

The owner's requirement: *"no matter where you click in the app, when you
click on a player's name, it should have an ability to link to recent
highlights."* Every player name the site renders — rankings rows, comp
tables, per-player pages, prospect pages, Sleeper rosters, Manager Score
trade ledgers — has to offer that.

Why this is one module and not five renderers
---------------------------------------------
Those names are produced by two different kinds of code. ``report.py``
renders HTML in Python at build time. ``myteam.py`` and
``managerscore_js.py`` render HTML in the browser from artifacts fetched at
runtime. A "highlights link" implemented separately on each side, for each
surface, is five implementations that will drift — and the previous round of
this work already produced two clip-card renderers (``reel.clipCard`` and
``myteam.mtClipCard``) that had to be kept in visual lockstep by hand.

So the split here is by *when*, not by *what*:

* **Python** emits a marker only. :func:`player_chip` wraps a name in a
  span carrying ``data-dfm-player`` with whatever ids that call site knows
  (gsis id, sleeper id, name, position). It renders no clip markup at all.
* **JavaScript** owns every pixel of highlights output. ``PLAYER_HIGHLIGHTS_JS``
  finds those markers on any page, resolves each one against
  ``highlights.json``, and renders clips. It is injected into *every* page by
  ``report._page``, so a new page or a new table gets the behaviour without
  remembering to wire anything up.

That leaves exactly one clip-card renderer (``DFMHL.clipCard``), one
player-to-clips join (``DFMHL.resolve``), and one set of week-bucket helpers
(``DFMHL.leadWeekKey`` and friends) for the site. ``reel.py``'s week helpers
became thin delegates to the ones here rather than a second copy, so the
Input Sleeper Team page and a rankings-row popover cannot label the same
clips differently.

Week bucketing is preserved, not reimplemented
----------------------------------------------
The bucket rules from PR #65 are load-bearing and were ported here verbatim:
the build publishes ``lead_week_start`` / ``weeks[]`` and the client never
re-derives which week leads, because that is a fact about the NFL schedule in
UTC rather than about the viewer's clock. The four-step fallback in
:js:func:`leadWeekKey` is kept intact for artifacts built before bucketing
existed.

Degradation
-----------
Three separate absences, three different honest answers, no broken links and
no thrown errors in any of them:

1. ``highlights.json`` 404s (the normal state until the scheduled refresh job
   has run once with a key) — every chip still renders, and says the index
   has not been generated yet.
2. The index exists but this player has no clips — says so, for that player.
3. The index exists and the player has clips — renders them, grouped by week,
   newest complete week first, every card an anchor to YouTube.

A chip is a ``<button>``, so with JavaScript disabled it is inert rather than
a dead link. The player's name stays a normal anchor to their page either
way: the chip sits beside the name and never replaces it.
"""
from __future__ import annotations

import html
import json
from typing import Optional

#: Attribute the client scans for. Anything carrying it becomes a chip.
PLAYER_ATTR = "data-dfm-player"

#: Attribute for an always-expanded inline block (used on player pages).
INLINE_ATTR = "data-dfm-highlights"


def player_ref(
    name: str,
    *,
    gsis: Optional[str] = None,
    sleeper_id: Optional[str] = None,
    position: Optional[str] = None,
) -> str:
    """The escaped JSON payload for one player marker.

    Every id is optional because the call sites genuinely differ in what
    they hold: ``engine.rankings`` rows know a gsis id, a Sleeper roster
    knows a sleeper id, a comp table knows only a name. The client tries
    them in that order of reliability and falls back to the name matcher,
    which is the same folding ``dynasty.highlights.match_key`` applied at
    index time.
    """
    payload = {"n": name}
    if gsis:
        payload["g"] = str(gsis).strip()
    if sleeper_id:
        payload["s"] = str(sleeper_id).strip()
    if position:
        payload["p"] = position
    return html.escape(json.dumps(payload, separators=(",", ":")), quote=True)


def player_chip(
    name: str,
    *,
    gsis: Optional[str] = None,
    sleeper_id: Optional[str] = None,
    position: Optional[str] = None,
    href: Optional[str] = None,
    extra_html: str = "",
) -> str:
    """A player's name plus its highlights affordance.

    ``href`` keeps the existing link to that player's page when the call
    site has one; the chip is added next to it, never in place of it. Rows
    that navigate on click (``<tr onclick=...>``) still work: the client's
    handler stops propagation so pressing the chip opens highlights instead
    of following the row.

    ``extra_html`` is appended inside the name cell, after the chip, for the
    badges some tables already render next to a name (era chip, washed-out
    chip). It is passed through unescaped and must already be safe.
    """
    label = html.escape(str(name))
    inner = f'<a href="{html.escape(href, quote=True)}">{label}</a>' if href else label
    ref = player_ref(name, gsis=gsis, sleeper_id=sleeper_id, position=position)
    return (
        f'<span class="dfm-player">{inner}'
        f'<button type="button" class="dfm-hl" {PLAYER_ATTR}=\'{ref}\''
        f' title="Recent highlights for {label}"'
        f' aria-label="Recent highlights for {label}">'
        f'<span class="dfm-hl-i" aria-hidden="true"></span></button>'
        f"{extra_html}</span>"
    )


def inline_block(
    name: str,
    *,
    gsis: Optional[str] = None,
    sleeper_id: Optional[str] = None,
    position: Optional[str] = None,
) -> str:
    """An always-expanded highlights section for one player.

    Used on per-player pages, where the player's own film belongs on the
    page rather than behind a click. Rendered by the same
    ``DFMHL.renderPlayer`` the popover uses.
    """
    ref = player_ref(name, gsis=gsis, sleeper_id=sleeper_id, position=position)
    return (
        f'<div class="dfm-hl-inline" {INLINE_ATTR}=\'{ref}\'>'
        f'<div class="dfm-hl-loading">Loading highlights…</div></div>'
    )


# --------------------------------------------------------------------------
# Client-side renderer. Token-substituted rather than f-stringed so the JS
# braces stay readable -- same convention as reel.py / myteam.py.
# --------------------------------------------------------------------------

PLAYER_HIGHLIGHTS_JS = r"""
/* DFMHL -- the site's single player-highlights renderer.
 *
 * Declared with `var` at top level on purpose. This ships as its own
 * <script> tag ahead of reel.js / myteam.js / managerscore.js, and a
 * top-level `var` is reachable as a bare identifier from those scripts in
 * a browser AND from the concatenated bundle the node harness builds. A
 * `const` inside an IIFE assigned to window would work in the browser and
 * break the harness, where `window` is a plain stub object.
 */
var DFMHL = (function (root) {
  'use strict';

  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  /* Substituted for an empty index rather than left null, so every reader
   * below takes its ordinary empty path instead of throwing. */
  var EMPTY = { clips: {}, players: {}, by_gsis: {} };

  var api = {
    /* The loaded artifact, or EMPTY after a failed load. Never null once
     * load() has settled. */
    artifact: null,
    /* Why the load failed, for the honest empty state. */
    loadError: null,
    _promise: null
  };

  /* Pages under players/ are one directory down, so every fetch and every
   * generated link needs the prefix report._page stamps on the page. */
  function base() {
    return (typeof root.DFM_BASE === 'string') ? root.DFM_BASE : '';
  }

  function A(a) { return a || api.artifact || EMPTY; }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /* ------------------------------------------------------------- loading */

  /* Cached by default: a rankings page has hundreds of chips and they must
   * share one fetch. ``force`` bypasses the cache for a caller whose job IS
   * loading (reel.js's loadHighlights), so that function stays honest --
   * call it twice against a changed artifact and you get the changed one. */
  api.load = function (opts) {
    if (api._promise && !(opts && opts.force)) return api._promise;
    api._promise = Promise.resolve()
      .then(function () { return fetch(base() + 'highlights.json'); })
      .then(function (r) {
        if (!r || !r.ok) {
          throw new Error('highlights.json -> HTTP ' + ((r && r.status) || '?'));
        }
        return r.json();
      })
      .then(function (j) {
        api.artifact = (j && typeof j === 'object') ? j : EMPTY;
        api.loadError = null;
        return api.artifact;
      })
      .catch(function (e) {
        /* The index is written by a scheduled job that has to run at least
         * once before the file exists. That is a normal state, not a bug,
         * and it must not surface as a console error or a dead chip. */
        api.artifact = EMPTY;
        api.loadError = String((e && e.message) || e);
        return api.artifact;
      });
    return api._promise;
  };

  /* True when there is any film at all to show. */
  api.hasIndex = function (a) {
    var art = A(a);
    return !!(art && art.clips && Object.keys(art.clips).length);
  };

  /* ------------------------------------------------------ id resolution */

  /* Mirror of dynasty.highlights.match_key, so a name typed on a comp table
   * resolves the way the Python matcher resolved it at index time. */
  var SUFFIX_RE = /(?:^|\s)(jr|sr|ii|iii|iv|v)$/;
  api.matchKey = function (s) {
    var t = String(s == null ? '' : s)
      .normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
    t = t.replace(/[^a-z0-9]+/g, ' ').trim();
    var prev = null;
    while (t !== prev) { prev = t; t = t.replace(SUFFIX_RE, '').trim(); }
    return t;
  };

  /* name-key -> [sleeper_id, ...], built lazily from players{} and cached
   * against the artifact object itself so swapping the artifact (which the
   * test harness does) invalidates it. */
  var _nameIdx = null;
  var _nameIdxFor = null;
  function nameIndex(a) {
    var art = A(a);
    if (_nameIdx && _nameIdxFor === art) return _nameIdx;
    var idx = {};
    var players = (art && art.players) || {};
    Object.keys(players).forEach(function (sid) {
      var k = api.matchKey((players[sid] || {}).name);
      if (!k) return;
      (idx[k] = idx[k] || []).push(sid);
    });
    _nameIdx = idx;
    _nameIdxFor = art;
    return idx;
  }

  /* Resolve one marker payload to {sid, name, pos, clips}.
   *
   * Three keys, in descending order of reliability:
   *   1. sleeper id  -- what clips{} is actually keyed by.
   *   2. gsis id     -- via the by_gsis crosswalk the build publishes.
   *   3. name        -- the folded match key, refused when two indexed
   *                     players share it, because showing one player's film
   *                     under another player's name is worse than showing
   *                     none.
   */
  api.resolve = function (ref, a) {
    var art = A(a);
    var name = (ref && (ref.n || ref.name)) || '';
    var pos = (ref && (ref.p || ref.position)) || '';
    var out = { sid: null, name: name, pos: pos, clips: [], ambiguous: false };
    if (!art) return out;

    var clips = art.clips || {};
    var sid = ref && (ref.s || ref.sleeper_id);
    sid = sid == null ? '' : String(sid).trim();

    if (!sid) {
      var gsis = ref && (ref.g || ref.gsis);
      gsis = gsis == null ? '' : String(gsis).trim();
      if (gsis) {
        var mapped = (art.by_gsis || {})[gsis];
        if (mapped != null) sid = String(mapped).trim();
      }
    }

    if (!sid && name) {
      var hits = nameIndex(art)[api.matchKey(name)] || [];
      if (hits.length === 1) {
        sid = hits[0];
      } else if (hits.length > 1) {
        out.ambiguous = true;
      }
    }

    if (sid) {
      out.sid = sid;
      out.clips = (clips[sid] || []).slice();
      var meta = (art.players || {})[sid];
      if (meta) {
        out.name = name || meta.name || '';
        out.pos = pos || meta.position || '';
      }
    }
    return out;
  };

  /* --------------------------------------------------------- formatting */

  api.watchUrl = function (videoId) {
    return 'https://www.youtube.com/watch?v=' + encodeURIComponent(videoId);
  };

  api.thumbUrl = function (videoId) {
    return 'https://i.ytimg.com/vi/' + encodeURIComponent(videoId) + '/mqdefault.jpg';
  };

  /* YouTube's anonymous-playlist endpoint takes at most 50 ids and plays
   * nothing at all when given more, so the cap is enforced not hoped for. */
  api.PLAYLIST_MAX = 50;
  api.playlistUrl = function (ids) {
    var list = (ids || []).filter(Boolean).slice(0, api.PLAYLIST_MAX);
    if (!list.length) return '';
    return 'https://www.youtube.com/watch_videos?video_ids=' + list.join(',');
  };

  api.fmtDuration = function (sec) {
    if (!sec && sec !== 0) return '';
    var m = Math.floor(sec / 60), s = sec % 60;
    return m + ':' + String(s).padStart(2, '0');
  };

  /* Read in UTC deliberately: the window is resolved in UTC by the build,
   * so formatting in the viewer's zone would render a slate the artifact
   * calls "Sep 10-14" as "Sep 9-13" west of Greenwich, and the page and the
   * JSON would disagree about which week this is. */
  api.fmtDay = function (iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d)) return '';
    return MONTHS[d.getUTCMonth()] + ' ' + d.getUTCDate();
  };

  api.fmtViews = function (n) {
    if (!n && n !== 0) return '';
    if (n >= 1e6) {
      return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1).replace(/\.0$/, '') + 'M views';
    }
    if (n >= 1e3) return Math.round(n / 1e3) + 'K views';
    return n + ' views';
  };

  /* ------------------------------------------------------- game window */

  api.windowLabel = function (a) {
    var art = A(a);
    var w = art && art.window;
    if (w && w.label) return w.label;
    var x = api.fmtDay(art && art.window_start);
    var y = api.fmtDay(art && art.window_end);
    if (!x || !y) return '';
    var yShort = x.split(' ')[0] === y.split(' ')[0] ? y.split(' ')[1] : y;
    return x + '\u2013' + yShort;
  };

  api.hasWindow = function (a) { return !!api.windowLabel(a); };

  /* A clip counts as current when the build said so. With no window in the
   * artifact every clip is current -- the pre-windowing behaviour, which
   * keeps an older highlights.json usable rather than empty. */
  api.inWindow = function (clip, a) {
    if (!api.hasWindow(a)) return true;
    return clip && clip.in_window === true;
  };

  /* ------------------------------------------------------- week buckets
   *
   * Ported from reel.js unchanged. The build decides which week leads and
   * publishes it; no client re-derives it, because the rule is "the week
   * whose last game has been played", a fact about the NFL schedule in UTC
   * and not about the viewer's clock.
   */

  function weekMeta(a) {
    var ws = (A(a).weeks) || [];
    return ws.filter(function (w) { return w && w.start_date; });
  }
  api.weekMeta = weekMeta;

  /* Bucket key for one clip. Prefers what the build computed; falls back to
   * the publish date so a pre-bucketing artifact still groups into
   * something sensible rather than collapsing into one pile. */
  api.clipWeekKey = function (c) {
    if (c && c.bucket_start) return c.bucket_start;
    var ts = (c && c.published_at) ? new Date(c.published_at) : null;
    if (!ts || isNaN(ts)) return '';
    /* Mirror of highlights.slate_start_for: back a day, then the Thursday
     * on or before. */
    var d = new Date(ts.getTime() - 24 * 3600 * 1000);
    var back = (d.getUTCDay() - 4 + 7) % 7;
    d.setUTCDate(d.getUTCDate() - back);
    return d.toISOString().slice(0, 10);
  };

  /* The bucket the pages open on: the most recent COMPLETE week.
   *
   * Four sources, descending authority. The first two are the build's own
   * answer and are what a current artifact hits. The last two exist because
   * the artifact is written by a scheduled job while pages are served from
   * a CDN, so a live page can be running against an artifact built before
   * week bucketing existed and must still group sensibly. */
  api.leadWeekKey = function (a) {
    var art = A(a);
    if (art && art.lead_week_start) return art.lead_week_start;

    var ws = weekMeta(art);
    var lead = ws.find(function (w) { return w.lead; }) ||
               ws.find(function (w) { return w.complete; }) || ws[0];
    if (lead) return lead.start_date;

    if (art && art.window_start) return String(art.window_start).slice(0, 10);

    var newest = '';
    var all = (art && art.clips) || {};
    Object.keys(all).forEach(function (sid) {
      (all[sid] || []).forEach(function (c) {
        var k = api.clipWeekKey(c);
        if (k > newest) newest = k;
      });
    });
    return newest;
  };

  /* "Sep 10-14" from a bucket key, for artifacts whose weeks[] carries no
   * label (or does not exist at all). */
  api.slateRangeLabel = function (key, a) {
    if (!key) return '';
    var art = A(a);
    var start = new Date(key + 'T00:00:00Z');
    if (isNaN(start)) return key;
    var span = (art && art.window && art.window.window_days) || 5;
    var end = new Date(start.getTime() + (span - 1) * 86400000);
    var x = MONTHS[start.getUTCMonth()] + ' ' + start.getUTCDate();
    var y = start.getUTCMonth() === end.getUTCMonth()
      ? String(end.getUTCDate())
      : MONTHS[end.getUTCMonth()] + ' ' + end.getUTCDate();
    return x + '\u2013' + y;
  };

  api.weekLabelFor = function (key, a) {
    var w = weekMeta(a).find(function (x) { return x.start_date === key; });
    if (w && w.label) return w.label;
    return api.slateRangeLabel(key, a) || key || 'Undated';
  };

  api.weekNumberFor = function (key, a) {
    var art = A(a);
    var w = weekMeta(art).find(function (x) { return x.start_date === key; });
    if (w && w.week) return w.week;
    var ws = (art && art.window_start)
      ? String(art.window_start).slice(0, 10) : '';
    if (key && key === ws) {
      return (art.window && art.window.week) || art.expected_week || null;
    }
    return null;
  };

  /* "Week 1 · Sep 10-14". Both halves: the number is what a fantasy manager
   * thinks in, the dates are what makes it unambiguous. */
  api.weekHeading = function (key, a) {
    var lbl = api.weekLabelFor(key, a);
    var wk = api.weekNumberFor(key, a);
    return wk ? 'Week ' + wk + ' \u00b7 ' + lbl : lbl;
  };

  /* Unknown buckets are complete: an artifact with no week metadata should
   * render as ordinary history, not as "still in progress". */
  api.weekIsComplete = function (key, a) {
    var w = weekMeta(a).find(function (x) { return x.start_date === key; });
    return w ? !!w.complete : true;
  };

  /* Order one player's clips inside a single week. Confidence first, then
   * views -- a 400-view cut-up of the player you rostered outranks a
   * 2M-view clip the matcher was less sure about, because being the right
   * player's film matters more than being popular film. */
  api.rankClip = function (a, b) {
    var kind = (a.kind === 'player_cutup' ? 0 : 1) - (b.kind === 'player_cutup' ? 0 : 1);
    if (kind) return kind;
    var conf = (b.confidence || 0) - (a.confidence || 0);
    if (Math.abs(conf) > 1e-9) return conf;
    var trust = (b.trusted ? 1 : 0) - (a.trusted ? 1 : 0);
    if (trust) return trust;
    var views = (b.view_count || 0) - (a.view_count || 0);
    if (views) return views;
    return String(b.published_at || '').localeCompare(String(a.published_at || ''));
  };

  /* --------------------------------------------------------- clip cards
   *
   * The site's ONE clip card. reel.clipCard and myteam.mtClipCard were two
   * renderers of the same thing kept in lockstep by hand; both now call
   * this.
   *
   * An anchor, never a button with a click handler: middle-click, cmd-click
   * and "open in new tab" have to behave the way they do everywhere else on
   * the web, and window.open() breaks all three.
   */
  api.clipCard = function (clip, opts) {
    var o = opts || {};
    var bits = [];
    var day = api.fmtDay(clip.published_at);
    if (day) bits.push(esc(day));
    if (clip.opponent) bits.push('vs ' + esc(clip.opponent));
    if (clip.duration_seconds) bits.push(esc(api.fmtDuration(clip.duration_seconds)));
    if (clip.view_count) bits.push(esc(api.fmtViews(clip.view_count)));
    if (clip.channel_title) bits.push(esc(clip.channel_title));

    var who = '';
    if (o.name) {
      who = '<span class="dfm-clip-who">' +
        (o.pos ? '<em>' + esc(o.pos) + '</em> ' : '') + esc(o.name) +
        (clip.trusted
          ? ' <span class="dfm-trust" title="Curated channel">\u2713</span>' : '') +
        '</span>';
    } else if (clip.trusted) {
      who = '<span class="dfm-clip-who"><span class="dfm-trust" ' +
        'title="Curated channel">\u2713</span></span>';
    }

    return '<a class="dfm-clip" href="' + esc(api.watchUrl(clip.video_id)) + '" ' +
      'target="_blank" rel="noopener noreferrer">' +
      '<span class="dfm-clip-thumb"><img loading="lazy" src="' +
        esc(api.thumbUrl(clip.video_id)) + '" alt=""></span>' +
      '<span class="dfm-clip-body">' + who +
        '<span class="dfm-clip-title">' + esc(clip.title) + '</span>' +
        '<span class="dfm-clip-meta">' + bits.join(' \u00b7 ') + '</span>' +
      '</span></a>';
  };

  /* ------------------------------------------------------- empty states */

  /* Why there is no film, phrased for the actual reason. Never "something
   * went wrong": each of these is a normal state of the pipeline. */
  api.emptyState = function (kind, who) {
    var name = who ? esc(who) : 'this player';
    if (kind === 'no-index') {
      return '<div class="dfm-hl-empty"><strong>No highlight index yet.</strong> ' +
        '<code>highlights.json</code> is written by the scheduled ' +
        '<em>Refresh YouTube highlight index</em> job. Until that has run ' +
        'once, every other part of the site works normally and there is no ' +
        'film to link to.</div>';
    }
    if (kind === 'ambiguous') {
      return '<div class="dfm-hl-empty">Two indexed players share this name, ' +
        'so linking ' + name + ' to either one\u2019s film could show you the ' +
        'wrong player. Open their page from the rankings to disambiguate.</div>';
    }
    return '<div class="dfm-hl-empty">No recent clips indexed for ' + name +
      '. The index covers public YouTube uploads matched to a player, so a ' +
      'quiet week or an unmatched upload both look like this.</div>';
  };

  /* ---------------------------------------------------------- rendering */

  /* Group {player, clip} pairs by week and render, newest COMPLETE week
   * first. Shared by the single-player popover and the whole-roster view so
   * the two cannot disagree about grouping or labels.
   *
   * pairs: [{name, pos, clip}]
   */
  /* Decide what is shown: group by week, apply the cut-up/recap precedence,
   * and order it. ONE implementation, because both the rendered cards and
   * the "watch all" playlist have to agree about what "shown" means -- when
   * they did not, the playlist queued recaps the page had been told to hide.
   *
   * Returns { keys, byWeek, leadKey, ids } where ids is every shown clip in
   * display order, which is exactly what the playlist wants.
   */
  api.selectWeeks = function (pairs, opts) {
    var o = opts || {};
    /* The artifact is a parameter, not a global read. myteam.js owns its own
     * `HL` variable and swaps it; reading api.artifact here instead would
     * render last week's film against this week's index and no test could
     * see the difference. Same reason the week helpers take one. */
    var a = o.artifact;
    var leadKey = api.leadWeekKey(a);
    var byWeek = {};
    var seen = [];

    function bucket(k) {
      if (!byWeek[k]) { byWeek[k] = { cutups: [], recaps: [] }; seen.push(k); }
      return byWeek[k];
    }
    /* Materialise the lead bucket even when empty: "no film indexed for
     * week 1 yet" is a thing the page has to be able to say, and dropping
     * the bucket would silently promote an in-progress week to the top. */
    if (leadKey && o.showEmptyLead !== false) bucket(leadKey);

    /* Per player: a recap stands in only when that player has no cut-up of
     * their own that week. Same rule the reel used. */
    var byPlayer = {};
    var order = [];
    (pairs || []).forEach(function (p) {
      var key = (p.sid || p.name || '') + '|' + (p.pos || '');
      if (!byPlayer[key]) { byPlayer[key] = []; order.push(key); }
      byPlayer[key].push(p);
    });

    order.forEach(function (key) {
      var mine = byPlayer[key];
      var weeks = {};
      mine.forEach(function (p) {
        var k = api.clipWeekKey(p.clip, a);
        if (!k) return;
        (weeks[k] = weeks[k] || []).push(p);
      });
      Object.keys(weeks).forEach(function (k) {
        var sorted = weeks[k].slice().sort(function (x, y) {
          return api.rankClip(x.clip, y.clip);
        });
        var cut = sorted.filter(function (p) { return p.clip.kind === 'player_cutup'; });
        var rec = sorted.filter(function (p) { return p.clip.kind === 'team_game'; });
        var b = bucket(k);
        if (cut.length) {
          cut.forEach(function (p) { b.cutups.push(p); });
        } else if (rec.length && o.includeRecaps !== false) {
          b.recaps.push(rec[0]);
        }
      });
    });

    var rest = seen.filter(function (k) { return k !== leadKey; }).sort().reverse();
    var keys = (leadKey && byWeek[leadKey] ? [leadKey] : []).concat(rest);

    /* Display order, lead week first -- the order the playlist should play. */
    var ids = [];
    keys.forEach(function (k) {
      var b = byWeek[k];
      b.cutups.concat(b.recaps).forEach(function (p) {
        if (p.clip && p.clip.video_id) ids.push(p.clip.video_id);
      });
    });

    return { keys: keys, byWeek: byWeek, leadKey: leadKey, ids: ids };
  };

  api.renderWeeks = function (pairs, opts) {
    var o = opts || {};
    var a = o.artifact;
    var sel = o.selection || api.selectWeeks(pairs, o);
    var byWeek = sel.byWeek;
    var leadKey = sel.leadKey;
    var keys = sel.keys;
    if (!keys.length) return '';

    function group(title, note, rows) {
      if (!rows.length) return '';
      return '<div class="dfm-hl-group">' +
        '<div class="dfm-hl-group-head">' + esc(title) +
          ' <span class="dfm-hl-count">' + rows.length + '</span></div>' +
        (note ? '<div class="dfm-hl-group-note">' + esc(note) + '</div>' : '') +
        '<div class="dfm-hl-clips">' + rows.map(function (r) {
          return api.clipCard(r.clip, { name: o.showNames ? r.name : '', pos: r.pos });
        }).join('') + '</div></div>';
    }

    return keys.map(function (k) {
      var b = byWeek[k];
      var n = b.cutups.length + b.recaps.length;
      var tag = k === leadKey
        ? '<span class="dfm-wk-tag dfm-wk-lead">most recent complete week</span>'
        : (api.weekIsComplete(k, a)
            ? '' : '<span class="dfm-wk-tag dfm-wk-live">still in progress</span>');
      var body = n
        ? group('Player highlights', '', b.cutups) +
          group('Game recaps',
                'Shown only for players with no individual cut-up this week.',
                b.recaps)
        : '<div class="dfm-hl-empty">No film indexed for ' +
          esc(api.weekHeading(k, a)) + ' yet.</div>';

      /* The lead week is open; older weeks collapse. <details> rather than a
       * click handler, so it works with no JS beyond what already ran. */
      if (k === leadKey) {
        return '<section class="dfm-wk dfm-wk-open"><h4 class="dfm-wk-head">' +
          esc(api.weekHeading(k, a)) + tag + '</h4>' + body + '</section>';
      }
      return '<details class="dfm-wk"><summary class="dfm-wk-head">' +
        esc(api.weekHeading(k, a)) + tag + '<span class="dfm-hl-count">' + n +
        '</span></summary>' + body + '</details>';
    }).join('');
  };

  /* One player's highlights, grouped by week, or the right empty state.
   * This is what both the popover and the inline block on a player page
   * render, so they cannot drift. */
  api.renderPlayer = function (ref, opts) {
    var o = opts || {};
    var a = o.artifact;
    if (!api.hasIndex(a)) return api.emptyState('no-index');
    var r = api.resolve(ref, a);
    if (r.ambiguous) return api.emptyState('ambiguous', r.name);
    if (!r.clips.length) return api.emptyState('empty', r.name);

    var pairs = r.clips.map(function (c) {
      return { sid: r.sid, name: r.name, pos: r.pos, clip: c };
    });
    var sel = api.selectWeeks(pairs, { artifact: a });
    var head = '';
    if (o.playlist !== false) {
      var url = api.playlistUrl(sel.ids);
      if (url) {
        head = '<a class="dfm-hl-playall" href="' + esc(url) + '" ' +
          'target="_blank" rel="noopener noreferrer">Watch all on YouTube</a>';
      }
    }
    return head + api.renderWeeks(pairs, {
      showNames: false, artifact: a, selection: sel
    });
  };

  /* A whole roster's highlights: the Input Sleeper Team view, and what
   * replaced the Roster Reel tab.
   *
   * players: [{sid, name, pos, rank, clips}] in the order they should
   * appear -- model rank order, best player first.
   */
  api.renderRoster = function (players, opts) {
    var o = opts || {};
    var a = o.artifact;
    if (!api.hasIndex(a)) return api.emptyState('no-index');
    var withClips = (players || []).filter(function (p) {
      return p.clips && p.clips.length;
    });
    if (!withClips.length) {
      return '<div class="dfm-hl-empty">None of these players have indexed ' +
        'clips right now. The index covers public YouTube uploads matched to ' +
        'a player, so a quiet week looks like this too.</div>';
    }

    /* Default to the lead week only, which is what the reel defaulted to:
     * "my roster's week". o.allWeeks is the "show every week" toggle. */
    var leadOnly = !o.allWeeks;
    var lead = api.leadWeekKey(a);
    var pairs = [];
    withClips.forEach(function (p) {
      (p.clips || []).forEach(function (c) {
        if (leadOnly && lead && api.clipWeekKey(c, a) !== lead) return;
        pairs.push({ sid: p.sid, name: p.name, pos: p.pos, clip: c });
      });
    });
    if (!pairs.length) {
      /* Film exists, just not for the week being shown. Say which, and
       * point at the toggle that widens it -- rather than "no clips",
       * which would be false. */
      return '<div class="dfm-hl-empty">No film indexed for ' +
        esc(api.weekHeading(lead, a)) + ' yet. These players do have film from ' +
        'other weeks — tick “show every week” to see it.</div>';
    }
    /* One selection for both halves: the playlist plays exactly the clips
     * the page is showing, in the order it shows them. Deriving the ids
     * separately is what queued hidden recaps. */
    var sel = api.selectWeeks(pairs, {
      artifact: a,
      includeRecaps: o.includeRecaps !== false
    });
    var head = '';
    if (o.playlist !== false) {
      var url = api.playlistUrl(sel.ids);
      if (url) {
        head = '<a class="dfm-hl-playall" href="' + esc(url) + '" ' +
          'target="_blank" rel="noopener noreferrer">Watch all on YouTube</a>' +
          '<div class="dfm-hl-playnote">Opens the film below as one YouTube ' +
          'playlist, in this order (50 clips maximum).</div>';
      }
    }
    return head + api.renderWeeks(pairs, {
      showNames: true,
      includeRecaps: o.includeRecaps !== false,
      artifact: a,
      selection: sel
    });
  };

  /* --------------------------------------------------------- the marker
   *
   * The client-side twin of ``player_highlights.player_chip``. Two marker
   * emitters exist because the site renders player names with two engines
   * -- Python at build time, JavaScript at runtime -- and neither can call
   * the other. They are kept to a single line of markup each precisely so
   * that this is the only duplication: the join, the week grouping, the
   * clip card and the empty states all live once, below and above.
   *
   * Any JS-rendered surface (league tables, Input Sleeper Team rosters,
   * Manager Score ledgers) calls this rather than hand-rolling a chip.
   */
  api.chip = function (name, opts) {
    var o = opts || {};
    var ref = { n: String(name == null ? '' : name) };
    if (o.gsis) ref.g = String(o.gsis).trim();
    if (o.sid) ref.s = String(o.sid).trim();
    if (o.pos) ref.p = o.pos;
    var label = esc(name);
    var inner = o.href
      ? '<a href="' + esc(o.href) + '">' + label + '</a>'
      : label;
    return '<span class="dfm-player">' + inner +
      '<button type="button" class="dfm-hl" data-dfm-player="' +
        esc(JSON.stringify(ref)) + '"' +
      ' title="Recent highlights for ' + label + '"' +
      ' aria-label="Recent highlights for ' + label + '">' +
      '<span class="dfm-hl-i" aria-hidden="true"></span></button>' +
      (o.extra || '') + '</span>';
  };

  /* ------------------------------------------------------------- the UI
   *
   * Everything below touches the DOM and is skipped entirely when there is
   * no DOM -- the node harness runs this file to assert on the renderers
   * above, and must not need a document to do it.
   */

  function parseRef(el) {
    try {
      return JSON.parse(el.getAttribute('data-dfm-player') ||
                        el.getAttribute('data-dfm-highlights') || '{}');
    } catch (e) {
      return {};
    }
  }

  var panel = null;

  function ensurePanel(doc) {
    if (panel) return panel;
    panel = doc.createElement('div');
    panel.className = 'dfm-hl-modal';
    panel.setAttribute('hidden', 'hidden');
    panel.innerHTML =
      '<div class="dfm-hl-backdrop" data-dfm-close="1"></div>' +
      '<div class="dfm-hl-sheet" role="dialog" aria-modal="true" ' +
        'aria-label="Recent highlights">' +
        '<div class="dfm-hl-sheet-head">' +
          '<h3 class="dfm-hl-sheet-title"></h3>' +
          '<button type="button" class="dfm-hl-x" data-dfm-close="1" ' +
            'aria-label="Close">\u00d7</button>' +
        '</div>' +
        '<div class="dfm-hl-sheet-body"></div>' +
      '</div>';
    doc.body.appendChild(panel);
    return panel;
  }

  function closePanel() {
    if (panel) panel.setAttribute('hidden', 'hidden');
  }

  api.open = function (ref) {
    var doc = root.document;
    if (!doc || !doc.body || !doc.createElement) return;
    var p = ensurePanel(doc);
    var name = (ref && (ref.n || ref.name)) || 'Player';
    p.querySelector('.dfm-hl-sheet-title').textContent = name + ' \u2014 recent highlights';
    var body = p.querySelector('.dfm-hl-sheet-body');
    body.innerHTML = '<div class="dfm-hl-loading">Loading highlights\u2026</div>';
    p.removeAttribute('hidden');
    api.load().then(function () {
      body.innerHTML = api.renderPlayer(ref);
    });
  };

  /* Fill every always-expanded block on the page. */
  api.fillInline = function () {
    var doc = root.document;
    if (!doc || !doc.querySelectorAll) return;
    var nodes = doc.querySelectorAll('[data-dfm-highlights]');
    if (!nodes || !nodes.length) return;
    api.load().then(function () {
      Array.prototype.forEach.call(nodes, function (el) {
        el.innerHTML = api.renderPlayer(parseRef(el));
      });
    });
  };

  api.init = function () {
    var doc = root.document;
    if (!doc || !doc.addEventListener || !doc.createElement) return;

    /* One delegated listener for the whole document rather than a handler
     * per chip. Rankings tables render hundreds of rows and are re-rendered
     * by their own filters, so per-element binding would leak and would
     * miss anything added after load. */
    doc.addEventListener('click', function (ev) {
      var t = ev.target;
      var closest = t && t.closest ? t.closest.bind(t) : null;
      if (!closest) return;

      if (closest('[data-dfm-close]')) {
        closePanel();
        return;
      }
      var chip = closest('[data-dfm-player]');
      if (!chip) return;
      /* Rankings rows navigate on click. Stop here so pressing the chip
       * opens film instead of following the row to the player page. */
      ev.preventDefault();
      ev.stopPropagation();
      api.open(parseRef(chip));
    });

    doc.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') closePanel();
    });

    api.fillInline();
  };

  /* Auto-start, guarded: a document with no body yet waits for it. */
  if (root.document && root.document.addEventListener) {
    if (root.document.readyState === 'loading') {
      root.document.addEventListener('DOMContentLoaded', function () { api.init(); });
    } else {
      api.init();
    }
  }

  return api;
})(typeof window !== 'undefined' ? window : globalThis);
"""


PLAYER_HIGHLIGHTS_CSS = """
/* Player-name highlights affordance + the one clip card. */
.dfm-player { display: inline-flex; align-items: center; gap: 5px; }
.dfm-hl {
  border: 1px solid var(--border); background: var(--card); cursor: pointer;
  border-radius: 999px; padding: 0; width: 20px; height: 20px; line-height: 0;
  display: inline-flex; align-items: center; justify-content: center;
  flex: 0 0 auto; opacity: 0.55; transition: opacity .12s, border-color .12s;
}
.dfm-hl:hover, .dfm-hl:focus-visible { opacity: 1; border-color: var(--accent); }
.dfm-hl-i {
  display: block; width: 0; height: 0;
  border-left: 6px solid var(--accent);
  border-top: 4px solid transparent; border-bottom: 4px solid transparent;
  margin-left: 2px;
}
.player-header .dfm-hl { border-color: rgba(255,255,255,.35); background: transparent; }
.player-header .dfm-hl-i { border-left-color: var(--header-text); }

/* Modal. Fixed-position rather than an inline popover on purpose: chips sit
   inside table cells, list items and page headers, and a fixed sheet needs
   no layout maths in any of them. */
.dfm-hl-modal { position: fixed; inset: 0; z-index: 90; }
.dfm-hl-modal[hidden] { display: none; }
.dfm-hl-backdrop { position: absolute; inset: 0; background: rgba(15,23,42,.55); }
.dfm-hl-sheet {
  position: relative; max-width: 620px; margin: 6vh auto 0;
  max-height: 88vh; overflow: auto; background: var(--card);
  border-radius: 12px; border: 1px solid var(--border);
  box-shadow: 0 18px 48px rgba(15,23,42,.28);
}
.dfm-hl-sheet-head {
  display: flex; align-items: center; gap: 12px; padding: 16px 20px;
  border-bottom: 1px solid var(--border); position: sticky; top: 0;
  background: var(--card);
}
.dfm-hl-sheet-title { margin: 0; font-size: 15px; font-weight: 700; flex: 1; }
.dfm-hl-x {
  border: 0; background: transparent; font-size: 22px; line-height: 1;
  cursor: pointer; color: var(--muted); padding: 0 4px;
}
.dfm-hl-sheet-body { padding: 16px 20px 22px; }

.dfm-hl-inline { margin-top: 10px; }
.dfm-hl-loading { font-size: 13px; color: var(--muted); padding: 8px 0; }
.dfm-hl-empty {
  font-size: 13px; color: var(--muted); background: #f8fafc;
  border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px;
}
.dfm-hl-empty code { font-size: 12px; }

.dfm-hl-playall {
  display: inline-block; background: var(--accent); color: #fff;
  font-size: 13px; font-weight: 600; padding: 8px 16px; border-radius: 999px;
  margin-bottom: 12px;
}
.dfm-hl-playall:hover { background: var(--accent-dark); text-decoration: none; }
.dfm-hl-playnote { font-size: 11px; color: var(--muted); margin: -6px 0 12px; }

.dfm-wk { margin-bottom: 14px; }
.dfm-wk-head {
  font-size: 13px; font-weight: 700; margin: 0 0 8px; cursor: default;
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
}
details.dfm-wk > summary.dfm-wk-head { cursor: pointer; }
.dfm-wk-tag {
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .04em; padding: 2px 8px; border-radius: 999px;
}
.dfm-wk-lead { background: #ecfdf5; color: #047857; }
.dfm-wk-live { background: #fffbeb; color: #92400e; }
.dfm-hl-count {
  font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums;
}
.dfm-hl-group { margin-bottom: 12px; }
.dfm-hl-group-head { font-size: 12px; font-weight: 700; color: var(--muted); }
.dfm-hl-group-note { font-size: 11px; color: var(--muted); margin-bottom: 6px; }
.dfm-hl-clips { display: flex; flex-direction: column; gap: 8px; }

/* The one clip card. */
.dfm-clip {
  display: flex; gap: 10px; align-items: center; text-align: left;
  background: var(--bg); border: 1px solid var(--border); color: inherit;
  border-radius: 8px; padding: 6px; text-decoration: none;
}
.dfm-clip:hover { border-color: var(--accent); text-decoration: none; }
.dfm-clip img {
  width: 76px; height: 43px; object-fit: cover; border-radius: 6px;
  background: #222; display: block;
}
.dfm-clip-body { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.dfm-clip-who { font-size: 12px; font-weight: 700; }
.dfm-clip-who em { font-style: normal; color: var(--muted); }
.dfm-clip-title {
  font-size: 12px; font-weight: 600; line-height: 1.35;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}
.dfm-clip-meta { font-size: 11px; color: var(--muted); }
.dfm-trust { color: #047857; }
"""


def assets() -> "tuple[str, str]":
    """``(js, css)`` for the shared renderer.

    ``report._page`` injects both into every page, which is the reason the
    behaviour cannot be forgotten on a new one.
    """
    return PLAYER_HIGHLIGHTS_JS, PLAYER_HIGHLIGHTS_CSS
