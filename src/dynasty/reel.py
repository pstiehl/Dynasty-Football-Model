"""reel.html — "watch my whole roster's week in 9 minutes".

Takes a Sleeper username (or a raw league id, or a hand-typed roster), joins
the roster against ``highlights.json``, and plays every matched clip back to
back in one YouTube IFrame player.

Why a playlist rather than one embed per player
-----------------------------------------------
``player.loadPlaylist({playlist: [id, id, ...]})`` hands YouTube an ordered
list of arbitrary video ids and it handles continuation, buffering and the
next/prev controls natively. One iframe, one network warm-up, and the user
never clicks between clips. Rendering an iframe per player instead would
mean 15+ simultaneous embeds, each pulling its own player bundle — the page
would crawl and nothing would autoplay in sequence.

Two failure modes this handles explicitly, because both look like "the site
is broken" to a user:

1. **Non-embeddable videos.** Error 150/101 mid-playlist stalls everything.
   The ingest script drops known-bad videos, but embeddability can change
   after we index it, so ``onError`` also skips forward at runtime.
2. **Autoplay blocking.** Browsers refuse programmatic playback with sound
   unless it follows a user gesture, so the playlist is only ever loaded
   from inside a click handler — never on page load.

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

let HL = null;           // highlights.json
let NAME_LOOKUP = null;  // match-key -> sleeper_id, built lazily
let userId = null;
let roster = [];         // [{sid, name, pos, team, rank, clips:[]}]
let queue = [];          // [{videoId, sid, name, clip}]
let ytPlayer = null, ytReady = false, pendingLoad = null;
let currentIdx = -1;
const unplayable = new Set();

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
  const lbl = windowLabel();
  // The date window leads. A week number is a label the channels type and
  // frequently get wrong or omit, so it trails in parentheses when the
  // build was able to derive one at all.
  const wk = (HL.window && HL.window.week) || HL.expected_week;
  const win = lbl
    ? ' · clips from ' + lbl + (wk ? ' (week ' + wk + ')' : '')
    : (wk ? ' · week ' + wk : '');
  document.getElementById('hl-meta').textContent =
    n.toLocaleString() + ' players with clips' + win +
    (HL.generated_at ? ' · indexed ' + HL.generated_at.slice(0, 10) : '');
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

  const total = sleeperIds.length;
  const inWin = roster.filter(p => p.clips.some(inWindow)).length;
  const onlyOld = roster.length - inWin;
  const lbl = windowLabel();
  const summaryEl = document.getElementById('roster-summary');
  if (summaryEl) {
    summaryEl.textContent = lbl
      ? (inWin + ' of ' + total + ' rostered players have film from ' + lbl +
         (onlyOld ? ' · ' + onlyOld + ' more have older clips — tick “every ' +
                    'clip per player” to queue them' : ''))
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

function rebuildQueue() {
  const includeTeam = document.getElementById('opt-team').checked;
  const allClips = document.getElementById('opt-all').checked;
  queue = [];
  roster.forEach(p => {
    let clips = p.clips.filter(c => includeTeam || c.kind === 'player_cutup');
    // Default view is the completed slate only. Clips from earlier windows
    // stay in the index and stay reachable — they are exactly what the
    // "every clip per player" toggle is for — but a reel that silently
    // mixes in a three-week-old cut-up is the bug this replaces.
    if (!allClips) {
      clips = clips.filter(inWindow).slice(0, 1);
    }
    clips.forEach(c => queue.push({ videoId: c.video_id, sid: p.sid, name: p.name, pos: p.pos, clip: c }));
  });

  const secs = queue.reduce((a, q) => a + (q.clip.duration_seconds || 0), 0);
  const btn = document.getElementById('play-all');
  if (queue.length) {
    btn.disabled = false;
    btn.textContent = '▶  Play all ' + queue.length + ' clips' + (secs ? ' · ' + fmtTotal(secs) : '');
  } else {
    btn.disabled = true;
    btn.textContent = 'No clips for this roster yet';
  }
  renderQueue();
}

function renderQueue() {
  const box = document.getElementById('queue-list');
  if (!queue.length) {
    const lbl = windowLabel();
    const older = roster.some(p => p.clips.some(c => !inWindow(c)));
    box.innerHTML = '<div class="empty">' + (lbl
      ? 'Nothing indexed for these players from ' + escapeHtml(lbl) + '. '
      : 'Nothing indexed for these players yet. ') +
      (older
        ? 'They do have older film — tick “every clip per player” to queue it.'
        : 'Try enabling game recaps above.') + '</div>';
    return;
  }
  box.innerHTML = queue.map((q, i) => {
    const c = q.clip;
    const bits = [];
    // Published date first: it is the field selection and ordering are
    // built on, and it is present on every clip. The title's week number
    // follows it as a label where the channel bothered to type one.
    const day = fmtDay(c.published_at);
    if (day) bits.push(day);
    if (c.week) bits.push('Wk ' + c.week);
    if (!inWindow(c)) bits.push('older');
    if (c.opponent) bits.push('vs ' + c.opponent);
    if (c.duration_seconds) bits.push(fmtDuration(c.duration_seconds));
    if (c.kind === 'team_game') bits.push('game recap');
    const cls = 'q-item' + (i === currentIdx ? ' active' : '') +
                (unplayable.has(q.videoId) ? ' dead' : '') +
                (inWindow(c) ? '' : ' stale');
    return '<button class="' + cls + '" data-idx="' + i + '">' +
      '<img loading="lazy" src="' + THUMB(q.videoId) + '" alt="">' +
      '<span class="q-body"><span class="q-name">' + escapeHtml(q.name) +
      (q.pos ? ' <em>' + q.pos + '</em>' : '') + '</span>' +
      '<span class="q-meta">' + bits.join(' · ') + '</span></span></button>';
  }).join('');
  box.querySelectorAll('.q-item').forEach(b => {
    b.addEventListener('click', () => jumpTo(+b.dataset.idx));
  });
}

// ---------------------------------------------------------------- yt player

function onYouTubeIframeAPIReady() {
  ytPlayer = new YT.Player('yt-frame', {
    // The IFrame API is served from youtube.com. Constructing the player
    // against the youtube-nocookie host while loading the API from
    // youtube.com mixes origins, and the player reports it as the generic
    // "An error occurred. Please try again later." Keep both on one host.
    //
    // origin is what the API asks embedders to send; omitting it is the
    // other common cause of that same message.
    playerVars: {
      rel: 0,
      modestbranding: 1,
      playsinline: 1,
      origin: window.location.origin
    },
    events: {
      onReady: () => {
        ytReady = true;
        if (pendingLoad) { const p = pendingLoad; pendingLoad = null; startAt(p); }
      },
      onStateChange: e => {
        if (e.data === YT.PlayerState.PLAYING || e.data === YT.PlayerState.CUED) {
          const i = ytPlayer.getPlaylistIndex();
          if (i >= 0 && i !== currentIdx) { currentIdx = i; renderQueue(); updateNowPlaying(); }
        }
      },
      onError: e => {
        // 101/150 = embedding disabled by the uploader, 100 = removed.
        // Skip rather than stall; the index can go stale between refreshes.
        const q = queue[currentIdx];
        if (q) unplayable.add(q.videoId);
        renderQueue();
        if (currentIdx < queue.length - 1) {
          setTimeout(() => ytPlayer.nextVideo(), 250);
        } else {
          status('Last clip could not be embedded (error ' + e.data + ').', true);
        }
      }
    }
  });
}
window.onYouTubeIframeAPIReady = onYouTubeIframeAPIReady;

function startAt(idx) {
  if (!queue.length) return;
  if (!ytReady) { pendingLoad = idx; return; }
  show('player-wrap', true);
  currentIdx = idx;
  // loadPlaylist autoplays — only ever called from a click handler, so the
  // browser's gesture requirement is satisfied and audio isn't blocked.
  ytPlayer.loadPlaylist({ playlist: queue.map(q => q.videoId), index: idx });
  renderQueue();
  updateNowPlaying();
}

function jumpTo(idx) {
  if (!ytReady || currentIdx < 0) { startAt(idx); return; }
  ytPlayer.playVideoAt(idx);
  currentIdx = idx;
  renderQueue();
  updateNowPlaying();
}

function updateNowPlaying() {
  const q = queue[currentIdx];
  const el = document.getElementById('now-playing');
  if (!q) { el.textContent = ''; return; }
  el.innerHTML = '<strong>' + escapeHtml(q.name) + '</strong> · ' +
    escapeHtml(q.clip.title) + ' <span class="np-idx">' +
    (currentIdx + 1) + ' of ' + queue.length + '</span>';
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
  document.getElementById('play-all').addEventListener('click', () => startAt(0));
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

  // Keyboard: the reel is meant to be watched, not clicked through.
  document.addEventListener('keydown', e => {
    if (currentIdx < 0 || /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)) return;
    if (e.key === 'ArrowRight' && currentIdx < queue.length - 1) jumpTo(currentIdx + 1);
    if (e.key === 'ArrowLeft' && currentIdx > 0) jumpTo(currentIdx - 1);
  });
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
.reel-grid { display: grid; grid-template-columns: minmax(0,1fr) 320px; gap: 20px;
  align-items: start; margin-top: 16px; }
@media (max-width: 900px) { .reel-grid { grid-template-columns: 1fr; } }
.player-box { position: relative; width: 100%; aspect-ratio: 16 / 9;
  background: #000; border-radius: 12px; overflow: hidden; }
.player-box iframe { position: absolute; inset: 0; width: 100%; height: 100%; border: 0; }
#now-playing { font-size: 13px; margin-top: 10px; line-height: 1.5; }
.np-idx { opacity: .55; font-size: 12px; margin-left: 6px; }
#queue-list { display: flex; flex-direction: column; gap: 6px;
  max-height: 560px; overflow-y: auto; }
.q-item { display: flex; gap: 10px; align-items: center; text-align: left;
  background: var(--card); border: 1px solid var(--border); color: inherit;
  border-radius: 10px; padding: 6px; cursor: pointer; }
.q-item:hover { border-color: var(--accent); }
.q-item.active { border-color: var(--accent); background: rgba(127,127,127,.10); }
.q-item.dead { opacity: .4; }
.q-item.stale { opacity: .72; border-style: dashed; }
.q-item img { width: 76px; height: 43px; object-fit: cover; border-radius: 6px;
  background: #222; flex: none; }
.q-body { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.q-name { font-size: 13px; font-weight: 600; }
.q-name em { font-style: normal; opacity: .55; font-size: 11px; }
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
<p class="lede reel-intro">Import a Sleeper roster and watch every rostered
player's cut-up from the most recently completed slate, back to back. One
player, one queue, no clicking between clips.</p>
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
    <label><input type="checkbox" id="opt-all"> every clip per player, including older windows</label>
  </div>

  <button class="btn btn-lg" id="play-all" disabled>Play all</button>

  <div class="reel-grid">
    <div>
      <div class="player-box" id="player-wrap" style="display:none"><div id="yt-frame"></div></div>
      <div id="now-playing"></div>
    </div>
    <div id="queue-list"></div>
  </div>
</div>

<p style="font-size:12px;opacity:.55;margin-top:28px">The reel defaults to the
most recently <em>completed</em> Thursday-to-Monday slate, so on a Monday you
still get last week's finished film rather than a week nothing has been
uploaded for yet. Clips are matched to players automatically from public
YouTube uploads and embedded via the YouTube player, so views and ad revenue
stay with the original uploader. Rosters are read live from Sleeper's public
API in your browser — nothing is sent to this site.</p>

</div>
<style>__CSS__</style>
<script src="https://www.youtube.com/iframe_api"></script>
<script>__JS__</script>
""".replace("__CSS__", _REEL_CSS).replace("__JS__", _REEL_JS)

    return _page(
        "Kings of Dynasty — Roster Reel",
        _site_header("reel", latest_ts, league_label),
        body,
    )
