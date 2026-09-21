"""reel.html — "watch my whole roster's week", as a link-out queue.

Takes a Sleeper username (or a raw league id, or a hand-typed roster), joins
the roster against ``highlights.json``, and renders an ordered, scannable
queue of clip cards. Every card opens on YouTube in a new tab.

Why this page no longer embeds anything
---------------------------------------
It used to play the whole roster back to back in one YouTube IFrame player
via ``loadPlaylist``. On paper that is the better experience; in practice
the owner hit *"An error occurred. Please try again later."* repeatedly,
while the highlights section of the site worked reliably for the single
reason that it sent the user to YouTube instead.

Embedded playback fails for causes we do not control and cannot detect in
advance: the uploader disables embedding (error 101/150), the video is
made private or removed (100), rights-holder restrictions apply to a
region, or the item is a Short, which reports ``embeddable: true`` and
then fails anyway inside a playlist. Each one was patched around
individually — an ingest-time embeddability drop, a 75-second duration
floor, a runtime ``onError`` skip-forward, an ``unplayable`` set. The
errors kept arriving, because the list of reasons is open-ended.

So the decision, from the owner directly: if embedding is restricted, stop
embedding. Linking out removes the entire class of failure rather than
another instance of it, and it deletes the three workarounds above with
it. Views and ad revenue still land with the original uploader, which was
always the reason for using the official player.

What replaces "watch it all in sequence"
----------------------------------------
Two things. The queue itself stays ordered and scannable — best player
first, one card per player per week — so clicking through it in order is
the same journey it always was. And ``watch_videos?video_ids=...`` hands
YouTube the whole roster as an anonymous playlist, so the sequence can
still be watched end to end, on YouTube's side, where playback works.

The page is built as a string here rather than in ``report.py`` purely to
keep that 2,000-line module from growing another 300 lines of JS.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional


# --------------------------------------------------------------------------
# Client-side script. Kept out of an f-string so the JS braces stay readable;
# the few build-time values are substituted by token replacement below.
# --------------------------------------------------------------------------

_REEL_JS = r"""
const SLEEPER = 'https://api.sleeper.app/v1';
const THUMB = id => 'https://i.ytimg.com/vi/' + id + '/mqdefault.jpg';
const WATCH = id => 'https://www.youtube.com/watch?v=' + encodeURIComponent(id);

// YouTube's anonymous-playlist endpoint. Hands an ordered list of ids to
// youtube.com and plays them in sequence there -- the "whole roster in one
// go" behaviour, on the side of the wire where playback actually works.
// The endpoint takes at most 50 ids, and silently plays nothing at all if
// given more, so the cap is enforced rather than hoped for.
const YT_PLAYLIST_MAX = 50;
function playlistUrl(ids) {
  const list = (ids || []).filter(Boolean).slice(0, YT_PLAYLIST_MAX);
  if (!list.length) return '';
  return 'https://www.youtube.com/watch_videos?video_ids=' + list.join(',');
}

let HL = null;           // highlights.json
let NAME_LOOKUP = null;  // match-key -> sleeper_id, built lazily
let userId = null;
let roster = [];         // [{sid, name, pos, team, rank, clips:[]}]
let queue = [];          // flat lead-week queue: [{videoId, sid, name, clip}]
let buckets = [];        // [{key, label, week, complete, lead, cutups, recaps}]

// ---------------------------------------------------------------- utilities

// Mirror of dynasty.highlights.match_key so hand-typed names resolve the
// same way the Python matcher resolved them at index time.
const SUFFIX_RE = /(?:^|\s)(jr|sr|ii|iii|iv|v)$/;
function matchKey(s) {
  let t = (s || '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
  t = t.replace(/[^a-z0-9]+/g, ' ').trim();
  let prev = null;
  while (t !== prev) { prev = t; t = t.replace(SUFFIX_RE, '').trim(); }
  return t;
}

function fmtDuration(sec) {
  if (!sec && sec !== 0) return '';
  const m = Math.floor(sec / 60), s = sec % 60;
  return m + ':' + String(s).padStart(2, '0');
}

function fmtTotal(sec) {
  if (sec < 60) return sec + ' sec';
  const m = Math.round(sec / 60);
  return m + (m === 1 ? ' minute' : ' minutes');
}

const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

// Date parts are read in UTC deliberately. The window is resolved in UTC by
// the build, so formatting it in the viewer's local zone would render a
// window labelled "Sep 10-14" in the artifact as "Sep 9-13" west of
// Greenwich -- the page and the JSON would disagree about which slate this
// is.
function fmtDay(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  return MONTHS[d.getUTCMonth()] + ' ' + d.getUTCDate();
}

// "Sep 10-14" for the resolved game window, or '' when the index predates
// windowing / was built without one.
function windowLabel() {
  const w = HL && HL.window;
  if (w && w.label) return w.label;
  const a = fmtDay(HL && HL.window_start), b = fmtDay(HL && HL.window_end);
  if (!a || !b) return '';
  // Same month -> "Sep 10-14"; across a boundary -> "Sep 30-Oct 4".
  const bShort = a.split(' ')[0] === b.split(' ')[0] ? b.split(' ')[1] : b;
  return a + '\u2013' + bShort;
}

function hasWindow() {
  return !!windowLabel();
}

// ------------------------------------------------------------ week buckets
//
// The build decides which week leads and publishes it on the artifact.
// Neither page recomputes it. That matters for two reasons: the rule is
// "the week whose last game has been played", which is a fact about the
// NFL schedule in UTC and not about the viewer's clock; and reel.js and
// myteam.js both read these helpers, so a single answer keeps the two
// pages from labelling the same clips differently.

function weekMeta() {
  const ws = (HL && HL.weeks) || [];
  return ws.filter(w => w && w.start_date);
}

// The bucket the pages open on: the most recent COMPLETE week.
//
// Four sources, in descending order of authority. The first two are the
// build's own answer and are what a current artifact hits. The last two
// exist because highlights.json is generated by a scheduled job and the
// pages are served from a CDN: a page can be live against an artifact
// built before week bucketing existed, and it has to degrade to sensible
// grouping rather than to an empty reel.
function leadWeekKey() {
  // 1. Published outright by the build.
  if (HL && HL.lead_week_start) return HL.lead_week_start;

  // 2. Flagged on the bucket list.
  const ws = weekMeta();
  const lead = ws.find(w => w.lead) || ws.find(w => w.complete) || ws[0];
  if (lead) return lead.start_date;

  // 3. Pre-bucketing artifact: the resolved window's start is a slate
  //    Thursday, which is exactly a bucket key.
  if (HL && HL.window_start) return String(HL.window_start).slice(0, 10);

  // 4. Nothing at all to go on -- group on the newest slate any indexed
  //    clip belongs to, so the page still shows film.
  let newest = '';
  const all = (HL && HL.clips) || {};
  for (const sid of Object.keys(all)) {
    (all[sid] || []).forEach(c => {
      const k = clipWeekKey(c);
      if (k > newest) newest = k;
    });
  }
  return newest;
}

// "Sep 10–14" from a bucket key, for artifacts whose weeks[] does not
// carry a label (or does not exist at all).
function slateRangeLabel(key) {
  if (!key) return '';
  const start = new Date(key + 'T00:00:00Z');
  if (isNaN(start)) return key;
  const span = (HL && HL.window && HL.window.window_days) || 5;
  const end = new Date(start.getTime() + (span - 1) * 86400000);
  const a = MONTHS[start.getUTCMonth()] + ' ' + start.getUTCDate();
  const b = start.getUTCMonth() === end.getUTCMonth()
    ? String(end.getUTCDate())
    : MONTHS[end.getUTCMonth()] + ' ' + end.getUTCDate();
  return a + '\u2013' + b;
}

function weekLabelFor(key) {
  const w = weekMeta().find(x => x.start_date === key);
  if (w && w.label) return w.label;
  return slateRangeLabel(key) || key || 'Undated';
}

// NFL week number for a bucket, or null.
function weekNumberFor(key) {
  const w = weekMeta().find(x => x.start_date === key);
  if (w && w.week) return w.week;
  // Pre-bucketing artifact: the resolved window carries the number for
  // its own slate, and that slate is the fallback lead bucket.
  const ws = (HL && HL.window_start)
    ? String(HL.window_start).slice(0, 10) : '';
  if (key && key === ws) {
    return (HL.window && HL.window.week) || HL.expected_week || null;
  }
  return null;
}

// "Week 1 · Sep 10–14". Both halves, per the spec: the number is what a
// fantasy manager thinks in, the dates are what makes it unambiguous.
function weekHeading(key) {
  const lbl = weekLabelFor(key);
  const wk = weekNumberFor(key);
  return wk ? 'Week ' + wk + ' \u00b7 ' + lbl : lbl;
}

function weekIsComplete(key) {
  const w = weekMeta().find(x => x.start_date === key);
  // Unknown buckets are treated as complete: an artifact with no week
  // metadata should render as ordinary history, not as "in progress".
  return w ? !!w.complete : true;
}

// Bucket key for one clip. Prefers what the build computed; falls back to
// the publish date so a pre-bucketing artifact still groups into
// something sensible rather than collapsing into one pile.
function clipWeekKey(c) {
  if (c && c.bucket_start) return c.bucket_start;
  const ts = c && c.published_at ? new Date(c.published_at) : null;
  if (!ts || isNaN(ts)) return '';
  // Mirror of highlights.slate_start_for: shift back a day, then take the
  // Thursday on or before.
  const d = new Date(ts.getTime() - 24 * 3600 * 1000);
  const back = (d.getUTCDay() - 4 + 7) % 7;
  d.setUTCDate(d.getUTCDate() - back);
  return d.toISOString().slice(0, 10);
}

// A clip counts as current when the build said so. With no window in the
// artifact every clip is treated as current, which is the pre-windowing
// behaviour and keeps an older highlights.json usable.
function inWindow(clip) {
  if (!hasWindow()) return true;
  return clip.in_window === true;
}

function status(msg, isError) {
  const el = document.getElementById('reel-status');
  el.textContent = msg || '';
  el.className = 'reel-status' + (isError ? ' err' : '');
  el.style.display = msg ? 'block' : 'none';
}

function show(id, on) {
  const el = document.getElementById(id);
  if (el) el.style.display = on ? '' : 'none';
}

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url.split('/').slice(-2).join('/') + ' -> HTTP ' + r.status);
  return r.json();
}

// ---------------------------------------------------------------- data load

async function loadHighlights() {
  try {
    HL = await getJSON('highlights.json');
  } catch (e) {
    // The index is generated by a scheduled job that has to run at least
    // once before this file exists. Substitute an empty index rather than
    // leaving HL null: every reader below then takes its normal empty
    // path, so the page explains itself instead of throwing.
    HL = { clips: {}, players: {}, by_gsis: {} };
    status('No highlight index yet — highlights.json has not been generated. ' +
           'Rosters still load; there is just no film to queue.', true);
    const meta = document.getElementById('hl-meta');
    if (meta) meta.textContent = 'Highlight index unavailable';
    return false;
  }
  const n = Object.keys(HL.clips || {}).length;
  // Label the header with the LEAD WEEK, not the resolved window.
  //
  // Those are the same slate most days and deliberately differ on a
  // Monday: the window tracks where the film is, the lead week tracks
  // which week has finished, and the page is grouped by the latter.
  // Sourcing this line from the window would caption a page whose first
  // section reads "Week 1 · Sep 10–14" with a different week entirely.
  const leadKey = leadWeekKey();
  const lbl = weekLabelFor(leadKey) || windowLabel();
  const wk = weekNumberFor(leadKey);
  const win = lbl
    ? ' · clips from ' + lbl + (wk ? ' (week ' + wk + ')' : '')
    : (wk ? ' · week ' + wk : '');
  document.getElementById('hl-meta').textContent =
    n.toLocaleString() + ' players with clips' + win +
    (HL.generated_at ? ' · indexed ' + HL.generated_at.slice(0, 10) : '');

  // The build fell back because the slate it should be showing had no film
  // indexed at all. Say so: silently presenting a different week as "this
  // week" is how the reel looked broken in the first place.
  const from = HL.window && HL.window.adjusted_from;
  if (from) {
    status('No film is indexed for ' + from + ' yet, so this reel is ' +
           'showing ' + lbl + ' instead.', false);
  }
  return true;
}

function nameLookup() {
  if (NAME_LOOKUP) return NAME_LOOKUP;
  NAME_LOOKUP = {};
  for (const [sid, p] of Object.entries((HL && HL.players) || {})) {
    NAME_LOOKUP[matchKey(p.name)] = sid;
  }
  return NAME_LOOKUP;
}

// ---------------------------------------------------------------- sleeper

async function loadFromUsername() {
  const username = document.getElementById('sleeper-user').value.trim();
  if (!username) { status('Enter your Sleeper username first.', true); return; }
  status('Looking up ' + username + '…');
  try {
    const user = await getJSON(SLEEPER + '/user/' + encodeURIComponent(username));
    if (!user || !user.user_id) throw new Error('no such user');
    userId = user.user_id;
    try { localStorage.setItem('dfm_sleeper_user', username); } catch (e) {}

    // In-season Sleeper uses the current year; before kickoff the current
    // year has no leagues yet, so fall back one season.
    const thisYear = new Date().getFullYear();
    let leagues = await getJSON(SLEEPER + '/user/' + userId + '/leagues/nfl/' + thisYear);
    if (!leagues || !leagues.length) {
      leagues = await getJSON(SLEEPER + '/user/' + userId + '/leagues/nfl/' + (thisYear - 1));
    }
    if (!leagues || !leagues.length) {
      status('No NFL leagues found for that username.', true); return;
    }
    renderLeagues(leagues);
    status('');
  } catch (e) {
    status('Sleeper lookup failed: ' + e.message, true);
  }
}

function renderLeagues(leagues) {
  const box = document.getElementById('league-list');
  box.innerHTML = leagues.map(L =>
    '<button class="league-btn" data-league="' + L.league_id + '">' +
    '<span class="lg-name">' + escapeHtml(L.name) + '</span>' +
    '<span class="lg-meta">' + L.total_rosters + ' teams · ' + L.season + '</span>' +
    '</button>'
  ).join('');
  box.querySelectorAll('.league-btn').forEach(b => {
    b.addEventListener('click', () => loadRoster(b.dataset.league));
  });
  show('step-league', true);
}

// Sleeper keeps IR and taxi-squad players out of ``roster.players`` in some
// league configurations. Reading only that field silently drops players the
// user definitely owns, which is one of the ways a roster came back short.
function rosterPlayerIds(r) {
  const out = [], seen = new Set();
  [r && r.players, r && r.reserve, r && r.taxi].forEach(list => {
    (list || []).forEach(id => {
      const s = String(id);
      if (!seen.has(s)) { seen.add(s); out.push(s); }
    });
  });
  return out;
}

async function loadRoster(leagueId) {
  status('Pulling rosters…');
  try {
    const rosters = await getJSON(SLEEPER + '/league/' + leagueId + '/rosters');
    // Owner display names are needed for the league table and for the team
    // picker below, so they are fetched once here rather than twice.
    let users = [];
    try {
      users = await getJSON(SLEEPER + '/league/' + leagueId + '/users');
    } catch (e) {
      // Non-fatal: the reel only needs rosters. The league view degrades to
      // "Team 1..N" and says so.
      console.warn('league users fetch failed', e);
    }

    let mine = userId ? rosters.find(r => r.owner_id === userId) : null;
    if (!mine && rosters.length === 1) mine = rosters[0];

    // Extension point, same contract as DFM_ON_ROSTER: hand the whole
    // league to any page that wants it, before the single-roster path
    // narrows to one team. A throwing hook must never take the reel down.
    if (typeof window.DFM_ON_LEAGUE === 'function') {
      try {
        window.DFM_ON_LEAGUE({
          leagueId: leagueId,
          rosters: rosters,
          users: users,
          userId: userId,
          myRosterId: mine ? mine.roster_id : null
        });
      } catch (err) {
        console.warn('DFM_ON_LEAGUE failed', err);
      }
    }

    if (!mine) {
      // League id entered directly, no user context — let them pick a team.
      renderTeamPicker(rosters, users);
      status('');
      return;
    }
    buildRoster(rosterPlayerIds(mine));
  } catch (e) {
    status('Could not load that league: ' + e.message, true);
  }
}

function renderTeamPicker(rosters, users) {
  const byId = {};
  users.forEach(u => { byId[u.user_id] = u.display_name || u.username || 'Team'; });
  const box = document.getElementById('league-list');
  box.innerHTML = '<div class="pick-label">Which team is yours?</div>' + rosters.map((r, i) =>
    '<button class="league-btn" data-idx="' + i + '">' +
    '<span class="lg-name">' + escapeHtml(byId[r.owner_id] || ('Team ' + (i + 1))) + '</span>' +
    '<span class="lg-meta">' + rosterPlayerIds(r).length + ' players</span></button>'
  ).join('');
  box.querySelectorAll('.league-btn').forEach(b => {
    b.addEventListener('click', () => buildRoster(rosterPlayerIds(rosters[+b.dataset.idx])));
  });
}

// ---------------------------------------------------------------- custom roster

function loadCustomRoster() {
  const raw = document.getElementById('custom-names').value || '';
  const lines = raw.split(/[\n,]/).map(s => s.trim()).filter(Boolean);
  if (!lines.length) { status('Type at least one player name.', true); return; }
  const lk = nameLookup();
  const sids = [], missed = [];
  lines.forEach(n => {
    const sid = lk[matchKey(n)];
    if (sid) sids.push(sid); else missed.push(n);
  });
  if (missed.length) {
    status('No clips indexed for: ' + missed.join(', '), false);
  } else {
    status('');
  }
  buildRoster(sids);
}

// ---------------------------------------------------------------- reel build

function buildRoster(sleeperIds) {
  const clips = (HL && HL.clips) || {};
  const meta = (HL && HL.players) || {};
  roster = [];
  for (const sid of sleeperIds) {
    const c = clips[String(sid)];
    if (!c || !c.length) continue;
    const m = meta[String(sid)] || {};
    roster.push({
      sid: String(sid),
      name: m.name || ('Player ' + sid),
      pos: m.position || '',
      team: m.team || '',
      rank: (m.rank == null ? 99999 : m.rank),
      clips: c
    });
  }
  // Best players first — a reel that opens on your WR5 feels wrong.
  roster.sort((a, b) => a.rank - b.rank);

  // Summarise against the lead week -- the bucket the page opens on --
  // rather than the film-availability window. Those are the same slate
  // most days and deliberately differ on a Monday, and quoting the
  // window here would caption a page grouped by completed week with a
  // different week's label.
  const total = sleeperIds.length;
  const leadKey = leadWeekKey();
  const leadHeading = weekHeading(leadKey);
  const inLead = roster.filter(
    p => (p.clips || []).some(c => clipWeekKey(c) === leadKey)
  ).length;
  const otherWeeks = roster.length - inLead;
  const summaryEl = document.getElementById('roster-summary');
  if (summaryEl) {
    summaryEl.textContent = leadKey
      ? (inLead + ' of ' + total + ' rostered players have film from ' +
         leadHeading +
         (otherWeeks ? ' · ' + otherWeeks + ' more have film from other ' +
                       'weeks — tick “show every week”' : ''))
      : (roster.length + ' of ' + total + ' rostered players have clips');
  }

  rebuildQueue();
  show('step-reel', true);

  // Extension point. The My Team page embeds this whole reel and adds its
  // own roster/rankings tabs; it hangs a hook here so it can render off the
  // same Sleeper fetch instead of duplicating the username -> leagues ->
  // rosters plumbing. ``roster`` holds only the players that have clips, so
  // the raw id list is passed too. A throwing hook must never take the reel
  // down with it.
  if (typeof window.DFM_ON_ROSTER === 'function') {
    try {
      window.DFM_ON_ROSTER(sleeperIds, roster);
    } catch (err) {
      console.warn('DFM_ON_ROSTER failed', err);
    }
  }

  // The reel scrolls itself into view when it is the whole point of the
  // page. On My Team the reel lives behind a tab, so scrolling to a hidden
  // element would jump the page for no reason.
  if (!window.DFM_SUPPRESS_REEL_SCROLL) {
    document.getElementById('step-reel')
      .scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

// -------------------------------------------------------------- queue build
//
// Two structures come out of here:
//
//   ``buckets``  every week the roster has film for, lead week first,
//                each split into player cut-ups and game recaps.
//   ``queue``    the lead week's cards, flat and ordered. Kept because the
//                My Team page and the "watch all" playlist both want one
//                ordered list, and because a flat queue is what "watch my
//                whole roster" means.

// Order one player's clips inside a single week.
//
// Confidence first, then view count. That ordering is the whole point of
// treating views as a *secondary* signal: a 400-view cut-up of the player
// you rostered outranks a 2-million-view clip the matcher was less sure
// about, because being the right player's film matters more than being
// popular film. Views only separate clips we are equally confident in.
function rankClip(a, b) {
  const kind = (a.kind === 'player_cutup' ? 0 : 1) - (b.kind === 'player_cutup' ? 0 : 1);
  if (kind) return kind;
  const conf = (b.confidence || 0) - (a.confidence || 0);
  if (Math.abs(conf) > 1e-9) return conf;
  const trust = (b.trusted ? 1 : 0) - (a.trusted ? 1 : 0);
  if (trust) return trust;
  const views = (b.view_count || 0) - (a.view_count || 0);
  if (views) return views;
  return String(b.published_at || '').localeCompare(String(a.published_at || ''));
}

function buildBuckets(includeTeam, allWeeks) {
  const leadKey = leadWeekKey();
  const byKey = {};
  const order = [];
  function bucketFor(key) {
    if (!byKey[key]) { byKey[key] = { cutups: [], recaps: [] }; order.push(key); }
    return byKey[key];
  }

  // Materialise the lead bucket even when it is empty. "No film indexed
  // for week 1 yet" is a thing the page has to be able to say; dropping
  // the bucket would silently promote an in-progress week to the top.
  bucketFor(leadKey);

  // roster is already in model-rank order, so pushing in iteration order
  // gives each bucket best-player-first without a second sort.
  roster.forEach(p => {
    const byWeek = {};
    (p.clips || []).forEach(c => {
      const k = clipWeekKey(c);
      if (!k) return;
      (byWeek[k] = byWeek[k] || []).push(c);
    });
    Object.keys(byWeek).forEach(k => {
      if (!allWeeks && k !== leadKey) return;
      const mine = byWeek[k].slice().sort(rankClip);
      const cutups = mine.filter(c => c.kind === 'player_cutup');
      const recaps = mine.filter(c => c.kind === 'team_game');
      const b = bucketFor(k);
      if (cutups.length) {
        (allWeeks ? cutups : cutups.slice(0, 1))
          .forEach(c => b.cutups.push(entryFor(p, c)));
      } else if (includeTeam && recaps.length) {
        // A recap surfaces for a player ONLY when that player has no
        // cut-up that week. With a cut-up present the recap is strictly
        // worse film for this roster slot, and mixing the two is what
        // made the old reel feel like it was padding.
        b.recaps.push(entryFor(p, recaps[0]));
      }
    });
  });

  const rest = order.filter(k => k !== leadKey).sort().reverse();
  return [leadKey].concat(rest).map(k => ({
    key: k,
    heading: weekHeading(k),
    complete: weekIsComplete(k),
    lead: k === leadKey,
    cutups: byKey[k].cutups,
    recaps: byKey[k].recaps
  }));
}

function entryFor(p, c) {
  return { videoId: c.video_id, sid: p.sid, name: p.name, pos: p.pos, clip: c };
}

function rebuildQueue() {
  const includeTeam = document.getElementById('opt-team').checked;
  const allWeeks = document.getElementById('opt-all').checked;

  buckets = buildBuckets(includeTeam, allWeeks);
  const lead = buckets.find(b => b.lead) || buckets[0] || { cutups: [], recaps: [] };
  queue = lead.cutups.concat(lead.recaps);

  updateWatchAll(lead);
  renderBuckets();
}

// The "watch the whole roster" affordance, rebuilt as a link-out. It hands
// YouTube the lead week's ids in order; sequencing then happens there,
// where it works, instead of here, where it did not.
function updateWatchAll(lead) {
  const el = document.getElementById('play-all');
  if (!el) return;
  const ids = queue.map(q => q.videoId);
  const url = playlistUrl(ids);
  const secs = queue.reduce((a, q) => a + (q.clip.duration_seconds || 0), 0);
  if (!url) {
    el.removeAttribute('href');
    el.className = 'btn btn-lg btn-disabled';
    el.textContent = 'No clips for this roster yet';
    return;
  }
  el.setAttribute('href', url);
  el.className = 'btn btn-lg';
  const capped = ids.length > YT_PLAYLIST_MAX;
  el.textContent = '\u25B6  Watch all ' +
    Math.min(ids.length, YT_PLAYLIST_MAX) + ' on YouTube' +
    (secs && !capped ? ' \u00b7 ' + fmtTotal(secs) : '');
  const note = document.getElementById('play-all-note');
  if (note) {
    note.textContent = capped
      ? 'YouTube caps an ad-hoc playlist at ' + YT_PLAYLIST_MAX +
        ' videos, so the first ' + YT_PLAYLIST_MAX + ' are queued.'
      : 'Opens ' + (lead && lead.heading ? lead.heading : 'this week') +
        ' as a playlist on YouTube, in this order.';
  }
}

// ------------------------------------------------------------------ render

function fmtViews(n) {
  if (!n && n !== 0) return '';
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1).replace(/\.0$/, '') + 'M views';
  if (n >= 1e3) return Math.round(n / 1e3) + 'K views';
  return n + ' views';
}

// One clip card. An anchor, not a button: middle-click, cmd-click and
// "open in new tab" all have to behave the way they do everywhere else,
// and a click handler calling window.open would break all three.
function clipCard(e) {
  const c = e.clip;
  const bits = [];
  const day = fmtDay(c.published_at);
  if (day) bits.push(escapeHtml(day));
  if (c.opponent) bits.push('vs ' + escapeHtml(c.opponent));
  if (c.duration_seconds) bits.push(escapeHtml(fmtDuration(c.duration_seconds)));
  if (c.view_count) bits.push(escapeHtml(fmtViews(c.view_count)));
  if (c.channel_title) bits.push(escapeHtml(c.channel_title));

  return '<a class="q-item" href="' + escapeHtml(WATCH(e.videoId)) + '" ' +
    'target="_blank" rel="noopener noreferrer">' +
    '<span class="q-thumb"><img loading="lazy" src="' +
      escapeHtml(THUMB(e.videoId)) + '" alt=""></span>' +
    '<span class="q-body">' +
      '<span class="q-name">' + escapeHtml(e.name) +
        (e.pos ? ' <em>' + escapeHtml(e.pos) + '</em>' : '') +
        (c.trusted ? ' <span class="q-trust" title="Curated channel">\u2713</span>' : '') +
      '</span>' +
      '<span class="q-title">' + escapeHtml(c.title) + '</span>' +
      '<span class="q-meta">' + bits.join(' \u00b7 ') + '</span>' +
    '</span></a>';
}

function groupHtml(title, note, entries) {
  if (!entries.length) return '';
  return '<div class="q-group">' +
    '<div class="q-group-head">' + escapeHtml(title) +
      ' <span class="q-count">' + entries.length + '</span></div>' +
    (note ? '<div class="q-group-note">' + escapeHtml(note) + '</div>' : '') +
    '<div class="q-list">' + entries.map(clipCard).join('') + '</div></div>';
}

// Player highlights lead; recaps are a separate, labelled group underneath
// and are never interleaved with cut-ups.
function bucketBody(b) {
  if (!b.cutups.length && !b.recaps.length) {
    return '<div class="empty">No film indexed for ' + escapeHtml(b.heading) +
      ' yet.</div>';
  }
  return groupHtml('Player highlights', '', b.cutups) +
    groupHtml('Game recaps', 'Shown only for players with no individual ' +
      'cut-up this week.', b.recaps);
}

function renderBuckets() {
  const box = document.getElementById('queue-list');
  if (!box) return;
  if (!buckets.length) { box.innerHTML = ''; return; }

  const html = buckets.map(b => {
    const status = b.lead
      ? '<span class="wk-tag wk-lead">most recent complete week</span>'
      : (b.complete ? '' : '<span class="wk-tag wk-live">still in progress</span>');
    const n = b.cutups.length + b.recaps.length;
    // The lead week is open; everything else is a collapsed <details>, so
    // older weeks are present without burying the week being asked about.
    if (b.lead) {
      return '<section class="wk wk-open"><h3 class="wk-head">' +
        escapeHtml(b.heading) + status + '</h3>' + bucketBody(b) + '</section>';
    }
    return '<details class="wk"><summary class="wk-head">' +
      escapeHtml(b.heading) + status +
      '<span class="q-count">' + n + '</span></summary>' +
      bucketBody(b) + '</details>';
  }).join('');

  box.innerHTML = html;
}

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ---------------------------------------------------------------- wiring

document.addEventListener('DOMContentLoaded', async () => {
  // Deliberately not aborting when the index is missing. Bailing out here
  // left every control on the page wired to nothing — the user clicks
  // "Find my leagues" and gets silence, which reads as a broken site.
  // loadHighlights has already installed an empty index and said so, so
  // the roster flow still runs and simply finds no clips. It also keeps
  // the My Team page's roster/rankings tabs alive, since they depend on
  // this wiring but not on highlights.json.
  await loadHighlights();

  try {
    const saved = localStorage.getItem('dfm_sleeper_user');
    if (saved) document.getElementById('sleeper-user').value = saved;
  } catch (e) {}

  document.getElementById('load-user').addEventListener('click', loadFromUsername);
  document.getElementById('sleeper-user').addEventListener('keydown', e => {
    if (e.key === 'Enter') loadFromUsername();
  });
  document.getElementById('load-league').addEventListener('click', () => {
    const id = document.getElementById('league-id').value.trim();
    if (id) { userId = null; loadRoster(id); }
  });
  document.getElementById('load-custom').addEventListener('click', loadCustomRoster);
  // No handler for #play-all: it is an anchor to YouTube now, and its
  // href is rewritten by updateWatchAll whenever the queue changes.
  document.getElementById('opt-team').addEventListener('change', rebuildQueue);
  document.getElementById('opt-all').addEventListener('change', rebuildQueue);

  document.querySelectorAll('.tab-btn').forEach(b => {
    b.addEventListener('click', () => {
      document.querySelectorAll('.tab-btn').forEach(x => x.classList.remove('on'));
      document.querySelectorAll('.tab-pane').forEach(x => x.style.display = 'none');
      b.classList.add('on');
      document.getElementById(b.dataset.pane).style.display = '';
    });
  });

  // The arrow-key handler went with the embedded player. Playback is on
  // YouTube now, so next/previous are YouTube's controls; binding the
  // arrows here would only fight the page's own scrolling.
});
"""


_REEL_CSS = """
.reel-intro { max-width: 720px; }
.hl-meta { font-size: 12px; opacity: .65; margin: 4px 0 18px; }
.tabs { display: flex; gap: 6px; margin-bottom: 14px; flex-wrap: wrap; }
.tab-btn { background: var(--card); border: 1px solid var(--border); color: inherit;
  padding: 7px 14px; border-radius: 999px; cursor: pointer; font-size: 13px; }
.tab-btn.on { background: var(--accent); color: #fff; border-color: var(--accent); }
.reel-input { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.reel-input input, .reel-input textarea {
  background: var(--card); border: 1px solid var(--border); color: inherit;
  border-radius: 8px; padding: 9px 12px; font-size: 14px; font-family: inherit; }
.reel-input input { min-width: 240px; }
.reel-input textarea { width: 100%; max-width: 480px; min-height: 90px; }
.btn { background: var(--accent); color: #fff; border: 0; border-radius: 8px;
  padding: 10px 18px; font-size: 14px; font-weight: 600; cursor: pointer; }
.btn:disabled { opacity: .45; cursor: default; }
.btn-lg { font-size: 15px; padding: 12px 22px; }
.reel-status { margin: 12px 0; font-size: 13px; padding: 9px 12px;
  border-radius: 8px; background: var(--card); border: 1px solid var(--border); }
.reel-status.err { border-color: #b4453c; }
#league-list { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.pick-label { width: 100%; font-size: 13px; opacity: .7; }
.league-btn { display: flex; flex-direction: column; align-items: flex-start; gap: 2px;
  background: var(--card); border: 1px solid var(--border); color: inherit;
  padding: 10px 14px; border-radius: 10px; cursor: pointer; text-align: left; }
.league-btn:hover { border-color: var(--accent); }
.lg-name { font-weight: 600; font-size: 14px; }
.lg-meta { font-size: 11px; opacity: .6; }
.reel-opts { display: flex; gap: 18px; flex-wrap: wrap; align-items: center;
  margin: 14px 0; font-size: 13px; opacity: .85; }
/* The player column is gone, so the queue is the page rather than a
   320px sidebar next to an embed. Cards get real width and the grid
   becomes responsive columns of clips. */
#queue-list { display: flex; flex-direction: column; gap: 18px; margin-top: 16px; }
#play-all-note { font-size: 12px; opacity: .6; margin-top: 6px; }
.btn-disabled { opacity: .45; pointer-events: none; }
a.btn, a.btn:hover { text-decoration: none; display: inline-block; }

/* ---- week buckets ---- */
.wk { border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px;
  background: rgba(127,127,127,.04); }
.wk-open { border-color: var(--accent); }
.wk-head { font-size: 15px; font-weight: 700; margin: 0 0 10px;
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap; cursor: pointer; }
.wk-open .wk-head { cursor: default; }
details.wk > summary { list-style: none; }
details.wk > summary::-webkit-details-marker { display: none; }
details.wk > summary::before { content: '\25B8'; opacity: .5; font-size: 12px; }
details.wk[open] > summary::before { content: '\25BE'; }
.wk-tag { font-size: 10px; font-weight: 700; letter-spacing: .04em;
  text-transform: uppercase; padding: 3px 8px; border-radius: 999px; }
.wk-lead { background: var(--accent); color: #fff; }
.wk-live { background: rgba(127,127,127,.18); opacity: .8; }

/* ---- groups: cut-ups lead, recaps are visibly a different thing ---- */
.q-group { margin-top: 12px; }
.q-group-head { font-size: 12px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .05em; opacity: .7; display: flex; align-items: center; gap: 8px; }
.q-group-note { font-size: 11px; opacity: .55; margin-top: 2px; }
.q-count { font-size: 11px; font-weight: 600; opacity: .55;
  background: rgba(127,127,127,.15); border-radius: 999px; padding: 1px 7px; }
.q-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 8px; margin-top: 8px; }

/* ---- clip card: an anchor, styled as a card ---- */
.q-item { display: flex; gap: 10px; align-items: center; text-align: left;
  background: var(--card); border: 1px solid var(--border); color: inherit;
  border-radius: 10px; padding: 6px; text-decoration: none; }
.q-item:hover { border-color: var(--accent); }
.q-thumb { position: relative; flex: none; }
/* Play glyph on the thumbnail: the card opens YouTube, and it should
   look like it does before it is clicked. */
.q-thumb::after { content: '\25B6'; position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-size: 15px; text-shadow: 0 1px 4px rgba(0,0,0,.8); opacity: .85; }
.q-item img { width: 76px; height: 43px; object-fit: cover; border-radius: 6px;
  background: #222; display: block; }
.q-body { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.q-name { font-size: 13px; font-weight: 600; }
.q-name em { font-style: normal; opacity: .55; font-size: 11px; }
.q-trust { color: var(--accent); font-size: 11px; }
.q-title { font-size: 11px; opacity: .75; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.q-meta { font-size: 11px; opacity: .6; }
.empty { font-size: 13px; opacity: .6; padding: 16px; }
"""


def reel_assets() -> "tuple[str, str]":
    """``(js, css)`` for the reel, for pages that embed it.

    The My Team page reuses the reel wholesale rather than reimplementing
    the Sleeper plumbing, the queue builder or the YouTube playlist player.
    Exposed as a function so the module-private constants stay private.
    """
    return _REEL_JS, _REEL_CSS


def build_reel(latest_ts: datetime, league_label: str) -> str:
    """Render reel.html. Imported lazily by report.generate_site."""
    from .report import _page, _site_header, _footer  # local: avoids a cycle

    body = """<div class="container">

<h2>Roster <span class="accent">Reel</span></h2>
<p class="lede reel-intro">Import a Sleeper roster and get every rostered
player's cut-up from the most recently completed week, in one ordered queue.
Every clip opens on YouTube — or send the whole week there as a playlist and
watch it end to end.</p>
<div class="hl-meta" id="hl-meta">Loading highlight index…</div>

<div class="tabs">
  <button class="tab-btn on" data-pane="pane-user">Sleeper username</button>
  <button class="tab-btn" data-pane="pane-league">League ID</button>
  <button class="tab-btn" data-pane="pane-custom">Custom roster</button>
</div>

<div class="tab-pane" id="pane-user">
  <div class="reel-input">
    <input id="sleeper-user" type="text" placeholder="Sleeper username" autocomplete="off">
    <button class="btn" id="load-user">Find my leagues</button>
  </div>
</div>

<div class="tab-pane" id="pane-league" style="display:none">
  <div class="reel-input">
    <input id="league-id" type="text" placeholder="Sleeper league ID" autocomplete="off">
    <button class="btn" id="load-league">Load league</button>
  </div>
</div>

<div class="tab-pane" id="pane-custom" style="display:none">
  <div class="reel-input">
    <textarea id="custom-names" placeholder="One player per line&#10;Ja'Marr Chase&#10;Bijan Robinson&#10;Brock Bowers"></textarea>
  </div>
  <div style="margin-top:8px"><button class="btn" id="load-custom">Build reel</button></div>
</div>

<div class="reel-status" id="reel-status" style="display:none"></div>
<div id="step-league" style="display:none"><div id="league-list"></div></div>

<div id="step-reel" style="display:none">
  <hr style="border:0;border-top:1px solid var(--border);margin:24px 0">
  <div id="roster-summary" style="font-size:13px;opacity:.75"></div>

  <div class="reel-opts">
    <label><input type="checkbox" id="opt-team"> include game recaps when no cut-up exists</label>
    <label><input type="checkbox" id="opt-all"> show every week, not just the latest complete one</label>
  </div>

  <a class="btn btn-lg btn-disabled" id="play-all" target="_blank"
     rel="noopener noreferrer">Watch all on YouTube</a>
  <div id="play-all-note"></div>

  <div id="queue-list"></div>
</div>

<p style="font-size:12px;opacity:.55;margin-top:28px">Clips open on YouTube in
a new tab rather than playing in an embedded player. Embedded playback fails
for reasons outside this site's control — uploaders disable embedding,
rights-holders restrict regions, Shorts refuse to play in a playlist — and
each one surfaced as <em>“An error occurred. Please try again later.”</em>
Linking out removes that failure entirely, and <em>Watch all on YouTube</em>
still queues the whole week in order. Weeks are grouped newest
<em>complete</em> week first: a week becomes eligible only once its final game
has been played, so on a Monday you get last week's finished film rather than
a week that is still being played. Clips are matched to players automatically
from public YouTube uploads; views and ad revenue stay with the original
uploader. Rosters are read live from Sleeper's public API in your browser —
nothing is sent to this site.</p>

</div>
<style>__CSS__</style>
<script>__JS__</script>
""".replace("__CSS__", _REEL_CSS).replace("__JS__", _REEL_JS)

    return _page(
        "Kings of Dynasty — Roster Reel",
        _site_header("reel", latest_ts, league_label),
        body,
    )
