/* Renderer assertions for the site-wide player-highlights renderer.
 *
 * Run by tests/test_player_highlights.py, which writes
 * dynasty.player_highlights.PLAYER_HIGHLIGHTS_JS to a temp file and passes
 * the path as argv[2]:
 *
 *   node tests/js/player_highlights_tests.js /tmp/hl.js
 *
 * What this proves: the id joins (sleeper / gsis / folded name), that the
 * week bucketing from PR #65 survived the move into this module including
 * its fallbacks, that every absence produces an honest empty state rather
 * than a throw or a dead link, that clips always link out to YouTube, and
 * that everything interpolated is escaped.
 *
 * What it cannot prove: that any of it *looks* right. There is no browser
 * and no layout engine in this environment, so rendering is unverified by
 * eye. It also cannot prove the popover opens, since that needs a real
 * document; the chip markup and the delegated-handler wiring are asserted
 * structurally instead.
 *
 * Exits 0 on success, 1 with a report of every failure.
 */
'use strict';

const fs = require('fs');

const jsPath = process.argv[2];
if (!jsPath) {
  console.error('usage: node player_highlights_tests.js <player-highlights-js-path>');
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
function eq(got, want, label) {
  ok(got === want, label,
     'got ' + JSON.stringify(got) + ', want ' + JSON.stringify(want));
}

/* ----------------------------------------------------------------- fixture
 *
 * Shaped like what highlights.build_index actually writes: clips keyed by
 * sleeper id, a by_gsis crosswalk, players{} metadata, weeks[] buckets and
 * a resolved window. Two weeks, the later one deliberately incomplete --
 * the owner's Monday case, where week 2 has film but has not finished, so
 * week 1 must lead.
 */
function fixture() {
  return {
    clips: {
      '3294': [
        { video_id: 'cut1', title: 'Josh Allen Every Throw Week 1',
          channel_title: 'Bills Film', published_at: '2026-09-14T22:00:00Z',
          duration_seconds: 300, kind: 'player_cutup', confidence: 0.92,
          bucket_start: '2026-09-10', bucket_week: 1, in_window: true,
          view_count: 120000, trusted: true, opponent: 'NYJ' },
        { video_id: 'cut2', title: 'Josh Allen Week 2 Highlights',
          channel_title: 'Bills Film', published_at: '2026-09-21T22:00:00Z',
          duration_seconds: 280, kind: 'player_cutup', confidence: 0.9,
          bucket_start: '2026-09-17', bucket_week: 2, in_window: false,
          view_count: 4000 }
      ],
      /* Cut-up AND a recap in the same week: the recap must be suppressed. */
      '4046': [
        { video_id: 'cut3', title: 'Mahomes Week 1', channel_title: 'Chiefs',
          published_at: '2026-09-13T20:00:00Z', duration_seconds: 240,
          kind: 'player_cutup', confidence: 0.8, bucket_start: '2026-09-10',
          in_window: true },
        { video_id: 'rec1', title: 'Chiefs vs Broncos Recap',
          channel_title: 'NFL', published_at: '2026-09-13T23:00:00Z',
          duration_seconds: 600, kind: 'team_game', confidence: 0.5,
          bucket_start: '2026-09-10', in_window: true }
      ],
      /* Recap ONLY: must surface when recaps are enabled, and only then. */
      '5849': [
        { video_id: 'rec2', title: 'Eagles vs Giants Recap',
          channel_title: 'NFL', published_at: '2026-09-14T01:00:00Z',
          duration_seconds: 700, kind: 'team_game', confidence: 0.5,
          bucket_start: '2026-09-10', in_window: true }
      ],
      /* Two players folding to the same match key -- see the ambiguity test. */
      '9001': [
        { video_id: 'dup1', title: 'Mike Williams TD', channel_title: 'X',
          published_at: '2026-09-14T02:00:00Z', duration_seconds: 100,
          kind: 'player_cutup', confidence: 0.7, bucket_start: '2026-09-10' }
      ],
      '9002': [
        { video_id: 'dup2', title: 'Mike Williams Catch', channel_title: 'Y',
          published_at: '2026-09-14T03:00:00Z', duration_seconds: 100,
          kind: 'player_cutup', confidence: 0.7, bucket_start: '2026-09-10' }
      ]
    },
    players: {
      '3294': { name: 'Josh Allen', position: 'QB', team: 'BUF' },
      '4046': { name: 'Patrick Mahomes', position: 'QB', team: 'KC' },
      '5849': { name: "Ja'Marr Chase", position: 'WR', team: 'CIN' },
      '9001': { name: 'Mike Williams', position: 'WR', team: 'NYJ' },
      '9002': { name: 'Mike Williams', position: 'WR', team: 'LAC' }
    },
    by_gsis: {
      '00-0034857': '3294',
      '00-0033873': '4046',
      '00-0036900': '5849'
    },
    weeks: [
      { start_date: '2026-09-17', label: 'Sep 17\u201321', week: 2,
        complete: false, lead: false },
      { start_date: '2026-09-10', label: 'Sep 10\u201314', week: 1,
        complete: true, lead: true }
    ],
    lead_week: 1,
    lead_week_start: '2026-09-10',
    lead_week_label: 'Sep 10\u201314',
    window: { label: 'Sep 10\u201314', week: 1, window_days: 5 },
    window_start: '2026-09-10T00:00:00Z',
    window_end: '2026-09-14T23:59:59Z'
  };
}

let FIX = fixture();

/* --------------------------------------------------------------- stubs
 *
 * No `document` on purpose. The module's UI half must stay dormant without
 * one -- that is what lets the renderers be asserted here, and what keeps
 * the module safe to load on a page mid-parse.
 */
global.window = {};
global.fetch = function () {
  if (FIX === null) {
    return Promise.resolve({ ok: false, status: 404,
                             json: () => Promise.resolve(null) });
  }
  return Promise.resolve({ ok: true, json: () => Promise.resolve(FIX) });
};

/* Load the shipped module verbatim into this scope. */
const src = fs.readFileSync(jsPath, 'utf8');
/* eslint-disable no-eval */
const DFMHL = eval(src + '\n;DFMHL');

(async function run() {
  /* ------------------------------------------------- dormant without DOM */
  ok(typeof DFMHL === 'object' && DFMHL !== null,
     'the module exposes DFMHL as a bare top-level binding');
  eq(DFMHL.artifact, null, 'no artifact is fetched until load() is called');

  /* --------------------------------------- honest state before any load */
  ok(DFMHL.renderPlayer({ n: 'Josh Allen' }).indexOf('No highlight index yet') >= 0,
     'renderPlayer before load explains itself instead of throwing');

  /* ----------------------------------------------------------- id joins */
  await DFMHL.load();
  eq(DFMHL.resolve({ n: 'Josh Allen', s: '3294' }).clips.length, 2,
     'sleeper id is joined directly');
  eq(DFMHL.resolve({ n: 'Josh Allen', g: '00-0034857' }).clips.length, 2,
     'gsis id is joined through the by_gsis crosswalk');
  eq(DFMHL.resolve({ n: 'Josh Allen' }).clips.length, 2,
     'a bare name is joined through the folded match key');
  eq(DFMHL.resolve({ n: '  josh   ALLEN Jr. ' }).clips.length, 2,
     'match key folds case, punctuation, spacing and name suffixes');
  eq(DFMHL.resolve({ n: "Ja'Marr Chase" }).clips.length, 1,
     'apostrophes fold the same way the Python matcher folds them');
  /* A trailing space on a gsis id cost 70 ranking rows once (see
   * myteam.gsisKey); the join must trim on read. */
  eq(DFMHL.resolve({ n: 'Josh Allen', g: ' 00-0034857 ' }).clips.length, 2,
     'a gsis id stored with stray whitespace still joins');

  /* Precedence: a correct sleeper id must win over a wrong name. */
  eq(DFMHL.resolve({ n: 'Not A Real Person', s: '3294' }).clips.length, 2,
     'sleeper id takes precedence over the name');

  /* ---------------------------------------------------------- ambiguity */
  const amb = DFMHL.resolve({ n: 'Mike Williams' });
  eq(amb.ambiguous, true, 'two indexed players sharing a name is ambiguous');
  eq(amb.clips.length, 0, 'an ambiguous name resolves to no clips');
  ok(DFMHL.renderPlayer({ n: 'Mike Williams' }).indexOf('share this name') >= 0,
     'an ambiguous name says so rather than guessing a player');

  /* --------------------------------------------- unknown / absent player */
  const unknown = DFMHL.resolve({ n: 'Nobody Indexed' });
  eq(unknown.clips.length, 0, 'an unknown name resolves to no clips');
  eq(unknown.ambiguous, false, 'an unknown name is not reported as ambiguous');
  ok(DFMHL.renderPlayer({ n: 'Nobody Indexed' })
       .indexOf('No recent clips indexed for Nobody Indexed') >= 0,
     'a player with no film gets a named empty state');
  eq(DFMHL.resolve({}).clips.length, 0,
     'an empty marker payload resolves to nothing, without throwing');

  /* ------------------------------- week bucketing, preserved from PR #65 */
  eq(DFMHL.leadWeekKey(), '2026-09-10',
     "the build's lead week is honoured, not recomputed");
  eq(DFMHL.weekHeading('2026-09-10'), 'Week 1 \u00b7 Sep 10\u201314',
     'a week is labelled with both its number and its date range');
  eq(DFMHL.weekIsComplete('2026-09-10'), true, 'week 1 is complete');
  eq(DFMHL.weekIsComplete('2026-09-17'), false,
     'week 2 is still in progress and is not presented as complete');
  eq(DFMHL.windowLabel(), 'Sep 10\u201314', 'the resolved window is labelled');
  eq(DFMHL.fmtDay('2026-09-14T22:00:00Z'), 'Sep 14',
     'dates are formatted in UTC, matching the artifact');

  /* Fallback chain, for an artifact built before bucketing existed. */
  const noWeeks = fixture();
  delete noWeeks.lead_week_start;
  delete noWeeks.weeks;
  eq(DFMHL.leadWeekKey(noWeeks), '2026-09-10',
     'with no weeks[] the resolved window start is the lead bucket');
  const bare = { clips: fixture().clips, players: {}, by_gsis: {} };
  eq(DFMHL.leadWeekKey(bare), '2026-09-17',
     'with nothing to go on, the newest slate any clip belongs to leads');
  eq(DFMHL.weekIsComplete('2026-09-10', bare), true,
     'an artifact with no week metadata reads as history, not in-progress');
  eq(DFMHL.clipWeekKey({ published_at: '2026-09-14T22:00:00Z' }), '2026-09-10',
     'a clip with no bucket_start is bucketed from its publish date');
  eq(DFMHL.clipWeekKey({}), '',
     'an undatable clip yields no bucket rather than a bogus one');
  eq(DFMHL.windowLabel(bare), '', 'no window -> empty label');
  eq(DFMHL.inWindow({ published_at: '2020-01-01T00:00:00Z' }, bare), true,
     'no window -> every clip treated as current, not hidden');

  /* ------------------------------------------------------- renderPlayer */
  const solo = DFMHL.renderPlayer({ n: 'Josh Allen', s: '3294' });
  ok(solo.indexOf('https://www.youtube.com/watch?v=cut1') >= 0,
     'a clip links out to YouTube');
  ok(solo.indexOf('target="_blank"') >= 0 && solo.indexOf('rel="noopener') >= 0,
     'link-outs open in a new tab safely');
  ok(solo.indexOf('<a class="dfm-clip"') >= 0,
     'a clip card is an anchor, so cmd-click and middle-click behave');
  ok(solo.indexOf('Week 1 \u00b7 Sep 10\u201314') >= 0,
     'clips are grouped under their week heading');
  ok(solo.indexOf('most recent complete week') >= 0,
     'the lead week is tagged as the most recent complete one');
  ok(solo.indexOf('still in progress') >= 0,
     'the in-progress week is labelled, not presented as current');
  ok(solo.indexOf('watch_videos?video_ids=') >= 0,
     'a playlist link is offered for the player');
  ok(solo.indexOf('dfm-wk-open') < solo.indexOf('<details'),
     'the lead week renders open, older weeks collapse below it');

  /* Recap suppression, per player per week. */
  const mahomes = DFMHL.renderPlayer({ n: 'Patrick Mahomes', s: '4046' });
  ok(mahomes.indexOf('Game recaps') < 0,
     'a recap is suppressed for a player who has a cut-up that week');
  ok(mahomes.indexOf('cut3') >= 0, 'the cut-up itself is rendered');
  const chase = DFMHL.renderPlayer({ n: "Ja'Marr Chase", s: '5849' });
  ok(chase.indexOf('Game recaps') >= 0,
     'a recap stands in for a player with no cut-up that week');
  ok(chase.indexOf('no individual cut-up') >= 0,
     'and the page says why the recap is there');

  /* -------------------------------------------------------- renderRoster */
  const roster = [
    { sid: '3294', name: 'Josh Allen', pos: 'QB', rank: 1, clips: FIX.clips['3294'] },
    { sid: '4046', name: 'Patrick Mahomes', pos: 'QB', rank: 2, clips: FIX.clips['4046'] },
    { sid: '5849', name: "Ja'Marr Chase", pos: 'WR', rank: 3, clips: FIX.clips['5849'] },
    { sid: '7777', name: 'No Film Rookie', pos: 'WR', rank: 400, clips: [] }
  ];
  const all = DFMHL.renderRoster(roster, { artifact: FIX, allWeeks: true });
  ok(all.indexOf('Josh Allen') >= 0 && all.indexOf('Patrick Mahomes') >= 0,
     'every player with film appears in the roster view');
  ok(all.indexOf('No Film Rookie') < 0,
     'a player with no film is omitted from the film view, not shown empty');
  ok(all.indexOf('Watch all on YouTube') >= 0,
     'the roster view offers the whole week as one playlist');
  ok(all.indexOf('Josh Allen') < all.indexOf('Patrick Mahomes'),
     'roster order is preserved, so the best player leads');

  /* The lead-week default is what the reel defaulted to. */
  const leadOnly = DFMHL.renderRoster(roster, { artifact: FIX });
  ok(leadOnly.indexOf('cut2') < 0,
     'by default the roster view shows only the most recent complete week');
  ok(all.indexOf('cut2') >= 0,
     'the "show every week" option widens it to every week');

  /* Recaps off must drop the recap-only player, not error. */
  const noRecaps = DFMHL.renderRoster(roster, {
    artifact: FIX, includeRecaps: false
  });
  ok(noRecaps.indexOf('rec2') < 0,
     'with recaps disabled a recap-only player contributes nothing');
  ok(noRecaps.indexOf('Josh Allen') >= 0,
     'and the rest of the roster still renders');

  /* Film exists, but not for the week on screen: say which, not "none". */
  const week2Only = {
    clips: { '3294': [FIX.clips['3294'][1]] },
    players: FIX.players, by_gsis: FIX.by_gsis,
    weeks: FIX.weeks, lead_week_start: '2026-09-10'
  };
  const r2 = DFMHL.renderRoster(
    [{ sid: '3294', name: 'Josh Allen', pos: 'QB', clips: week2Only.clips['3294'] }],
    { artifact: week2Only }
  );
  ok(r2.indexOf('show every week') >= 0,
     'film in another week points at the toggle rather than claiming none');

  /* ------------------------------------------------- the artifact is a param */
  const empty = { clips: {}, players: {}, by_gsis: {} };
  ok(DFMHL.renderRoster(roster, { artifact: empty })
       .indexOf('No highlight index yet') >= 0,
     'an explicitly passed empty artifact wins over the loaded one');
  ok(DFMHL.hasIndex(empty) === false, 'hasIndex reads the passed artifact');
  ok(DFMHL.hasIndex(FIX) === true, 'and recognises a populated one');

  /* ------------------------------------------------------------ the chip */
  const chip = DFMHL.chip('Josh Allen', {
    gsis: '00-0034857', pos: 'QB', href: 'players/josh-allen-034857.html'
  });
  ok(chip.indexOf('data-dfm-player=') >= 0,
     'the chip carries the marker the delegated handler looks for');
  ok(chip.indexOf('href="players/josh-allen-034857.html"') >= 0,
     'the chip keeps the name a link to the player page');
  ok(chip.indexOf('<button') >= 0,
     'the affordance is a button, so it is inert without JS rather than a dead link');
  ok(chip.indexOf('aria-label="Recent highlights for Josh Allen"') >= 0,
     'the affordance is labelled for screen readers');
  const parsed = JSON.parse(
    chip.match(/data-dfm-player="([^"]*)"/)[1]
        .replace(/&quot;/g, '"').replace(/&#39;/g, "'")
        .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
        .replace(/&amp;/g, '&')
  );
  eq(parsed.g, '00-0034857', 'the marker round-trips the gsis id');
  eq(parsed.n, 'Josh Allen', 'the marker round-trips the name');
  /* A chip must resolve through the same join as everything else. */
  eq(DFMHL.resolve(parsed).clips.length, 2,
     'a chip marker resolves back to that player\u2019s clips');

  /* --------------------------------------------------------- escaping */
  const nasty = '<script>alert(1)</script>';
  const card = DFMHL.clipCard(
    { video_id: nasty, title: nasty, channel_title: nasty },
    { name: nasty, pos: nasty }
  );
  ok(card.indexOf('<script>') < 0, 'clip fields are escaped in the card');
  const nastyChip = DFMHL.chip(nasty, { href: '"><img onerror=1>' });
  ok(nastyChip.indexOf('<script>') < 0, 'the chip escapes the player name');
  ok(nastyChip.indexOf('<img onerror') < 0, 'the chip escapes the href');
  ok(DFMHL.renderPlayer({ n: nasty }).indexOf('<script>') < 0,
     'the empty state escapes the name it reports');

  /* ------------------------------------------------ degradation on a 404 */
  FIX = null;
  DFMHL._promise = null;
  await DFMHL.load({ force: true });
  ok(DFMHL.loadError !== null, 'a missing artifact is recorded as a load error');
  ok(DFMHL.artifact !== null,
     'and substitutes an empty index rather than leaving null to throw on');
  eq(DFMHL.hasIndex(), false, 'hasIndex is false with no index');
  ok(DFMHL.renderPlayer({ n: 'Josh Allen' }).indexOf('No highlight index yet') >= 0,
     'a 404 degrades to the honest no-index state');
  ok(DFMHL.renderPlayer({ n: 'Josh Allen' }).indexOf('youtube.com') < 0,
     'and offers no link at all rather than a broken one');
  eq(DFMHL.renderWeeks([], {}), '',
     'renderWeeks on nothing returns empty, without throwing');
  eq(DFMHL.renderRoster([], {}).indexOf('No highlight index yet') >= 0, true,
     'renderRoster on nothing degrades too');

  /* The playlist cap is enforced, not hoped for: YouTube silently plays
   * nothing when handed more than 50 ids. */
  const many = [];
  for (let i = 0; i < 80; i++) many.push('v' + i);
  const url = DFMHL.playlistUrl(many);
  eq(url.split('video_ids=')[1].split(',').length, 50,
     'an ad-hoc playlist is capped at 50 ids');
  eq(DFMHL.playlistUrl([]), '', 'no ids -> no playlist link');

  if (failures) {
    console.error('\n' + failures + ' of ' + checks + ' checks FAILED');
    process.exit(1);
  }
  console.log('All ' + checks + ' player-highlights checks passed.');
})().catch((e) => {
  console.error('harness threw: ' + (e && e.stack || e));
  process.exit(1);
});
