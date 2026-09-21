// Behavioural assertions against the real reel.py / myteam.py JavaScript.
//
// Appended after dom_stub.js and both page scripts by
// scripts/check_site_behaviour.py, so everything below runs in the same
// lexical scope as the shipped code and can read `HL`, `MT`, `roster`,
// `queue` and call `renderAll`, `rebuildQueue`, `resolvePlayer` directly.
//
// What this can prove: id joins, unranked reasons, window membership and
// ordering, league scoring and standings. What it cannot prove: that any
// of it looks right on screen. There is no browser here.

let failures = 0;
let checks = 0;

function ok(cond, label) {
  checks++;
  if (cond) {
    console.log('  OK   ' + label);
  } else {
    failures++;
    console.log('  FAIL ' + label);
  }
}

function eq(got, want, label) {
  ok(got === want, label + '  (got ' + JSON.stringify(got) +
     ', want ' + JSON.stringify(want) + ')');
}

// ---------------------------------------------------------------- run

(async function run() {
  await MODEL_READY;
  // The real loader, against the real artifact shape, rather than poking
  // HL directly -- so the hl-meta line it writes is under test too.
  const loaded = await loadHighlights();
  eq(loaded, true, 'loadHighlights accepts a windowed highlights.json');
  ok(document.getElementById('hl-meta').textContent
       .indexOf('clips from Sep 10\u201314') >= 0,
     'header reads "clips from Sep 10-14", not "week 2"');
  ok(document.getElementById('hl-meta').textContent.indexOf('(week 1)') >= 0,
     'the week number survives as a parenthetical label');

  console.log('\n-- window labelling (reel.js) --');
  eq(windowLabel(), 'Sep 10\u201314', 'windowLabel reads the resolved window');
  eq(hasWindow(), true, 'hasWindow true when the artifact carries one');
  eq(fmtDay('2026-09-14T22:00:00Z'), 'Sep 14', 'fmtDay formats in UTC');
  eq(inWindow(HL.clips['3294'][0]), true, 'in-window clip recognised');
  eq(inWindow(HL.clips['3294'][1]), false, 'out-of-window clip recognised');

  // An artifact with no window at all (an older highlights.json) must not
  // start hiding every clip.
  const saved = HL;
  HL = { clips: {}, players: {}, by_gsis: {} };
  eq(windowLabel(), '', 'no window -> empty label');
  eq(inWindow({ published_at: '2020-01-01T00:00:00Z' }), true,
     'no window -> every clip treated as current');
  HL = saved;

  console.log('\n-- default queue is the completed slate (reel.js) --');
  const ROSTER_IDS = ['3294', '12001', '7564', '5000', '5001', '5002', '5003',
                      '4046', 'DAL', '999999', '5849'];
  buildRoster(ROSTER_IDS);
  document.getElementById('opt-all').checked = false;
  document.getElementById('opt-team').checked = false;
  rebuildQueue();
  let ids = queue.map(q => q.videoId).sort();
  eq(JSON.stringify(ids), JSON.stringify(['chase_in', 'dak_in']),
     'default queue holds only in-window clips');
  ok(queue.every(q => q.clip.in_window === true),
     'no out-of-window clip leads the default reel');

  // "Show every week" no longer pours older film into the flat queue --
  // it materialises the older week buckets, which render as collapsed
  // <details> beneath the lead week. `queue` stays the lead week, because
  // that is what the "watch all on YouTube" playlist sends.
  document.getElementById('opt-all').checked = true;
  rebuildQueue();
  ids = queue.map(q => q.videoId).sort();
  eq(JSON.stringify(ids), JSON.stringify(['chase_in', 'dak_in']),
     'the flat queue stays the lead week even with every week shown');

  const bucketed = [];
  buckets.forEach(b => b.cutups.concat(b.recaps)
                        .forEach(e => bucketed.push(e.videoId)));
  eq(JSON.stringify(bucketed.sort()),
     JSON.stringify(['chase_in', 'dak_in', 'dak_old', 'dart_old']),
     'older film is still reachable, now via its own week bucket');
  ok(buckets.length > 1, 'older film gets its own bucket, not the lead one');
  ok(buckets[0].lead === true, 'the lead bucket is rendered first');
  ok(buckets.slice(1).every(b => !b.lead),
     'exactly one bucket is the lead');

  const qhtml = document.getElementById('queue-list').innerHTML;
  ok(qhtml.indexOf('<details') >= 0,
     'older weeks render collapsed beneath the lead week');
  ok(qhtml.indexOf('most recent complete week') >= 0,
     'the lead bucket says why it leads');
  ok(qhtml.indexOf('Sep 14') >= 0,
     'queue shows the publish date, not just a week number');
  document.getElementById('opt-all').checked = false;
  rebuildQueue();

  // ---------------------------------------------------- link-out, not embed
  //
  // The reel used to hand these ids to a YouTube IFrame player. It does
  // not any more, and these checks exist so that cannot come back by
  // accident: the owner hit "An error occurred. Please try again later."
  // on the embedded path repeatedly, and linking out is what fixed it.
  console.log('\n-- clips link out to YouTube, nothing is embedded --');

  const html = document.getElementById('queue-list').innerHTML;
  ok(html.indexOf('youtube.com/watch?v=') >= 0,
     'clip cards link to youtube.com/watch');
  ok(html.indexOf('target="_blank"') >= 0, 'clips open in a new tab');
  ok(html.indexOf('rel="noopener noreferrer"') >= 0,
     'link-outs carry rel=noopener noreferrer');
  ok(html.indexOf('i.ytimg.com') >= 0, 'thumbnails are still rendered');
  ok(html.indexOf('<iframe') < 0, 'no iframe is rendered into the queue');
  ok(typeof YT === 'undefined' || !YT,
     'the page never constructs a YouTube IFrame player');
  ok(typeof onYouTubeIframeAPIReady === 'undefined',
     'the IFrame API ready hook is gone');
  ok(typeof startAt === 'undefined' && typeof jumpTo === 'undefined',
     'the embedded playback entry points are gone');
  ok(typeof unplayable === 'undefined',
     'the unplayable/onError bookkeeping is gone');

  // The "whole roster in sequence" affordance, preserved without embedding.
  const watchAll = document.getElementById('play-all');
  const href = watchAll.getAttribute('href') || '';
  ok(href.indexOf('youtube.com/watch_videos?video_ids=') >= 0,
     'watch-all builds a YouTube playlist URL');
  queue.forEach(q => ok(href.indexOf(q.videoId) >= 0,
     'watch-all playlist contains ' + q.videoId));
  ok(watchAll.className.indexOf('btn-disabled') < 0,
     'watch-all is enabled when the queue has clips');

  // YouTube silently plays nothing when handed more than 50 ids, so the
  // cap has to be enforced rather than hoped for.
  const many = [];
  for (let i = 0; i < 80; i++) many.push('id' + i);
  const capped = playlistUrl(many);
  eq(capped.split('video_ids=')[1].split(',').length, 50,
     'the ad-hoc playlist is capped at YouTube\'s 50-video limit');
  eq(playlistUrl([]), '', 'an empty queue produces no playlist URL');

  console.log('\n-- rosterPlayerIds includes IR and taxi (reel.js) --');
  eq(JSON.stringify(rosterPlayerIds(
       { players: ['1', '2'], reserve: ['3'], taxi: ['4', '2'] })),
     JSON.stringify(['1', '2', '3', '4']),
     'reserve and taxi are unioned in, duplicates dropped');

  console.log('\n-- every rostered player resolves (myteam.js) --');
  renderAll(ROSTER_IDS);
  eq(MT.roster.length, ROSTER_IDS.length, 'no rostered player is dropped');
  ok(MT.roster.every(p => p.rank != null || p.unrankedReason),
     'every player has either a rank or a stated reason');

  const by = {};
  MT.roster.forEach(p => { by[p.sid] = p; });

  eq(by['7564'].rank, 1, 'gsis join: Chase ranked #1');
  eq(by['7564'].rankSource, 'gsis', 'Chase joined by gsis id');
  eq(by['3294'].rank, 41, 'gsis join: Dak ranked #41');

  eq(by['12001'].rank, 64, 'name join repairs the empty-gsis gap (Dart #64)');
  eq(by['12001'].rankSource, 'name', 'Dart joined by name, and says so');

  // The whitespace defect, client side. The page is served against
  // whatever artifact is deployed or cached, so it must repair this itself
  // rather than depending on the next site build.
  eq(by['5849'].rank, 12, 'leading-space gsis still joins (Kyler Murray #12)');
  eq(by['5849'].rankSource, 'gsis', 'and joins on the id, not by name');

  eq(by['DAL'].rank, null, 'team defense has no rank');
  eq(by['DAL'].unrankedCode, 'team_defense', 'team defense is labelled as such');
  eq(by['DAL'].name, 'Cowboys defense', 'team defense gets a readable name');

  eq(by['5000'].unrankedCode, 'kicker', 'kicker labelled');
  eq(by['5001'].unrankedCode, 'idp', 'IDP labelled');
  eq(by['5002'].unrankedCode, 'no_arc', 'unranked skill player labelled no_arc');
  eq(by['5003'].unrankedCode, 'ambiguous',
     'two ranked players share a name -> refuse to guess');
  eq(by['999999'].unrankedCode, 'unidentified',
     'id absent from every crosswalk is called out as stale crosswalk');

  const ranked = MT.roster.filter(p => p.rank != null).length;
  const unranked = MT.roster.filter(p => p.rank == null).length;
  eq(ranked + unranked, MT.roster.length, 'ranked + unranked accounts for all');

  const rankingsHtml = document.getElementById('rankings-pane-body').innerHTML;
  ok(rankingsHtml.indexOf('Unranked') >= 0,
     'rankings tab renders an explicit unranked table');
  ok(rankingsHtml.indexOf('Kicker') >= 0,
     'rankings tab prints the kicker reason');
  ok(rankingsHtml.indexOf(ranked + ' ranked + ' + unranked + ' unranked') >= 0,
     'rankings tab reconciles the counts on the page');

  const rosterHtml = document.getElementById('roster-pane-body').innerHTML;
  ok(rosterHtml.indexOf('Why no rank') >= 0,
     'roster tab carries a reason column');
  ok(rosterHtml.indexOf('Cowboys defense') >= 0,
     'roster tab shows the team defense by name');

  console.log('\n-- build fell back to another slate --');
  // loadHighlights re-reads the artifact through fetch, so the fixture is
  // what has to carry the flag -- mutating HL directly would be undone by
  // the very call under test.
  const pristine = FIXTURES['highlights.json'];
  const adjusted = JSON.parse(JSON.stringify(HIGHLIGHTS));
  adjusted.window.adjusted_from = 'Sep 3\u20137';
  FIXTURES['highlights.json'] = adjusted;
  await loadHighlights();
  ok(document.getElementById('reel-status').textContent
       .indexOf('No film is indexed for Sep 3\u20137') >= 0,
     'a fallback window is disclosed, not presented as the current one');
  ok(document.getElementById('reel-status').textContent
       .indexOf('Sep 10\u201314') >= 0,
     'and it names the window actually being shown');
  FIXTURES['highlights.json'] = pristine;
  await loadHighlights();

  console.log('\n-- graceful degradation --');
  const realRankings = MT.rankings;
  MT.rankings = null;
  renderAll(['7564', 'DAL']);
  ok(document.getElementById('rankings-pane-body').innerHTML
       .indexOf('Model rankings unavailable') >= 0,
     'missing engine_rankings.json explains itself');
  eq(MT.roster.length, 2, 'roster still lists players with no rankings file');
  MT.rankings = realRankings;

  const realHL = HL;
  HL = { clips: {}, players: {}, by_gsis: {} };
  renderAll(['7564', 'DAL']);
  ok(document.getElementById('clips-pane-body').innerHTML
       .indexOf('No highlight index yet') >= 0,
     'missing highlights.json explains itself');
  HL = realHL;

  // ------------------------------------------------ current artifact shape
  //
  // Everything above runs against the pre-bucketing fixture, which proves
  // the backward-compatible fallback. This block swaps in an artifact of
  // the shape the build now writes -- weeks[], lead_week_start,
  // bucket_start, view_count, trusted -- and asserts the primary path,
  // including the case the owner specified: Monday 2026-09-21, where
  // week 2 has film but is not finished, so week 1 leads.
  console.log('\n-- week buckets, current artifact shape --');

  const BUCKETED = {
    generated_at: '2026-09-21T16:36:00Z',
    resolved_as_of: '2026-09-21T16:36:00+00:00',
    lead_week: 1,
    lead_week_start: '2026-09-10',
    lead_week_label: 'Sep 10\u201314',
    weeks: [
      { start_date: '2026-09-17', label: 'Sep 17\u201321', week: 2,
        complete: false, lead: false, clip_count: 1 },
      { start_date: '2026-09-10', label: 'Sep 10\u201314', week: 1,
        complete: true, lead: true, clip_count: 2 }
    ],
    players: {
      '7564': { name: "Ja'Marr Chase", position: 'WR', team: 'CIN', rank: 1 },
      '3294': { name: 'Dak Prescott', position: 'QB', team: 'DAL', rank: 41 }
    },
    by_gsis: {},
    clips: {
      '7564': [
        { video_id: 'w1_cut', title: "Ja'Marr Chase Week 1 | Every Target",
          channel_title: 'Curtain Call Replays', published_at: '2026-09-14T22:00:00Z',
          duration_seconds: 260, kind: 'player_cutup', confidence: 0.9,
          bucket_week: 1, bucket_start: '2026-09-10',
          view_count: 1500000, trusted: true },
        { video_id: 'w2_cut', title: "Ja'Marr Chase Week 2 | Every Target",
          channel_title: 'Curtain Call Replays', published_at: '2026-09-21T10:00:00Z',
          duration_seconds: 245, kind: 'player_cutup', confidence: 0.9,
          bucket_week: 2, bucket_start: '2026-09-17', view_count: 4200 }
      ],
      // No cut-up in week 1 -- only a recap. This is the player the
      // "recaps only when there is no cut-up" rule is about.
      '3294': [
        { video_id: 'w1_recap', title: 'Cowboys vs Giants | Week 1 Game Highlights',
          channel_title: 'NFL', published_at: '2026-09-14T23:30:00Z',
          duration_seconds: 1080, kind: 'team_game', confidence: 0.6,
          bucket_week: 1, bucket_start: '2026-09-10', view_count: 900000 }
      ]
    }
  };

  HL = BUCKETED;
  buildRoster(['7564', '3294']);
  document.getElementById('opt-all').checked = false;
  document.getElementById('opt-team').checked = true;
  rebuildQueue();

  eq(leadWeekKey(), '2026-09-10',
     'the build\'s lead week is honoured, not recomputed');

  // Regression guard on a contradiction this page can produce once the
  // window anchor and the completeness rule disagree, which they do on a
  // Monday: the header must caption the week the page is grouped by, not
  // the window the film came from.
  BUCKETED.window = { label: 'Sep 17\u201321', week: 2, window_days: 5,
                      start: '2026-09-17T00:00:00+00:00' };
  BUCKETED.window_start = '2026-09-17T00:00:00+00:00';
  await loadHighlights();
  const hdr = document.getElementById('hl-meta').textContent;
  ok(hdr.indexOf('Sep 10\u201314') >= 0,
     'header captions the lead week, not the resolved window');
  ok(hdr.indexOf('Sep 17\u201321') < 0,
     'header does not advertise the in-progress week as current');
  ok(hdr.indexOf('(week 1)') >= 0, 'header carries the lead week number');
  delete BUCKETED.window;
  delete BUCKETED.window_start;
  HL = BUCKETED;
  eq(weekHeading('2026-09-10'), 'Week 1 \u00b7 Sep 10\u201314',
     'a bucket is labelled with both its week number and its date range');
  eq(weekIsComplete('2026-09-17'), false,
     'week 2 is known to be unfinished');

  const lead = buckets.find(b => b.lead);
  eq(lead.key, '2026-09-10',
     'week 1 leads on Monday even though week 2 film is indexed');
  eq(JSON.stringify(lead.cutups.map(e => e.videoId)), JSON.stringify(['w1_cut']),
     'player cut-ups lead the bucket');
  eq(JSON.stringify(lead.recaps.map(e => e.videoId)), JSON.stringify(['w1_recap']),
     'a recap surfaces for the player with no cut-up that week');

  // Chase has a week 1 cut-up, so no recap may be attached to him.
  ok(lead.recaps.every(e => e.sid !== '7564'),
     'no recap is attached to a player who already has a cut-up');

  const bhtml = document.getElementById('queue-list').innerHTML;
  ok(bhtml.indexOf('Player highlights') >= 0, 'cut-ups get their own group');
  ok(bhtml.indexOf('Game recaps') >= 0, 'recaps get a separate labelled group');
  ok(bhtml.indexOf('Player highlights') < bhtml.indexOf('Game recaps'),
     'player highlights are rendered before game recaps');
  ok(bhtml.indexOf('Week 1 \u00b7 Sep 10\u201314') >= 0,
     'the lead bucket is labelled with week number and dates');
  ok(bhtml.indexOf('1.5M views') >= 0, 'view counts are surfaced');
  ok(bhtml.indexOf('q-trust') >= 0, 'curated-channel clips are badged');

  // Week 2 exists and is reachable, but must not be presented as current.
  document.getElementById('opt-all').checked = true;
  rebuildQueue();
  const wk2 = buckets.find(b => b.key === '2026-09-17');
  ok(!!wk2, 'the in-progress week is still shown');
  eq(wk2.lead, false, 'the in-progress week does not lead');
  ok(document.getElementById('queue-list').innerHTML
       .indexOf('still in progress') >= 0,
     'the in-progress week is labelled as unfinished');
  document.getElementById('opt-all').checked = false;

  // The My Team tab groups the same way, off the same helpers.
  console.log('\n-- My Team highlights tab, same buckets --');
  renderAll(['7564', '3294']);
  const mt = document.getElementById('clips-pane-body').innerHTML;
  ok(mt.indexOf('youtube.com/watch?v=') >= 0,
     'My Team clips link out to YouTube');
  ok(mt.indexOf('<button') < 0 || mt.indexOf('mt-clip" href') >= 0,
     'My Team clip cards are anchors, not buttons');
  ok(mt.indexOf('Week 1 \u00b7 Sep 10\u201314') >= 0,
     'My Team uses the same week heading as the reel');
  ok(mt.indexOf('most recent complete week') >= 0,
     'My Team marks the same lead week');
  ok(mt.indexOf('Player highlights') < mt.indexOf('Game recaps'),
     'My Team also leads with player highlights');

  HL = realHL;

  console.log('\n-- team-strength metric (myteam.js) --');
  eq(Math.round(rankValue(1)), 100, 'rank 1 scores 100');
  ok(rankValue(1) > rankValue(2), 'value strictly decreases with rank');
  ok(rankValue(200) < 3, 'rank 200 is worth almost nothing');
  eq(rankValue(null), 0, 'unranked players contribute zero');

  // Convexity: two elite assets must beat fifteen replacement-level ones.
  const elite = scoreTeam([{ rank: 1 }, { rank: 2 }]);
  const deep = scoreTeam(Array.from({ length: 15 }, (_, i) => ({ rank: 150 + i })));
  ok(elite.raw > deep.raw,
     'two top-2 players outscore fifteen #150s (convex value curve)');

  // The core cap: extra bodies past TS.core cannot raise a score.
  const fifteen = Array.from({ length: 15 }, (_, i) => ({ rank: 20 + i }));
  const thirty = fifteen.concat(
    Array.from({ length: 15 }, (_, i) => ({ rank: 120 + i })));
  eq(scoreTeam(thirty).raw, scoreTeam(fifteen).raw,
     'players beyond the best ' + TS.core + ' do not inflate the score');
  eq(scoreTeam(thirty).nUnranked, 0, 'ranked-only roster reports no unranked');
  eq(scoreTeam([{ rank: 1 }, { rank: null }]).nUnranked, 1,
     'unranked players are counted, not hidden');

  console.log('\n-- league standings (myteam.js) --');
  const fillerIds = [];
  for (let i = 0; i < 20; i++) fillerIds.push('6' + (100 + i));
  const fillerIds2 = [];
  for (let i = 20; i < 40; i++) fillerIds2.push('6' + (100 + i));

  window.DFM_ON_LEAGUE({
    leagueId: 'L1',
    userId: 'u2',
    myRosterId: 2,
    rosters: [
      { roster_id: 1, owner_id: 'u1', players: fillerIds },
      { roster_id: 2, owner_id: 'u2', players: ['7564', '4046', '9509'],
        reserve: ['5001'], taxi: ['12001'] },
      { roster_id: 3, owner_id: 'u3', players: ['3294', '5000', 'DAL'] },
      { roster_id: 4, owner_id: null, players: [] }
    ],
    users: [
      { user_id: 'u1', display_name: 'deepbench', metadata: { team_name: 'The Hoarders' } },
      { user_id: 'u2', display_name: 'pstiehl', metadata: {} },
      { user_id: 'u3', display_name: 'rival', metadata: {} }
    ]
  });
  await MODEL_READY;
  await new Promise(r => setTimeout(r, 0));

  eq(MT.teams.length, 4, 'every roster in the league is scored');
  eq(MT.teams[0].rosterId, 2, 'top-heavy contender leads on strength');
  eq(MT.teams[0].place, 1, 'places are assigned in score order');
  eq(Math.round(MT.teams[0].index), 100, 'leader indexes to 100');
  ok(MT.teams[0].isMine, 'the user\'s own team is identified');
  eq(MT.teams[0].name, 'pstiehl', 'falls back to display name with no team name');
  eq(MT.teams.find(t => t.rosterId === 1).name, 'The Hoarders',
     'Sleeper team_name wins over the handle when set');
  eq(MT.teams.find(t => t.rosterId === 4).name, 'Team 4',
     'an orphan roster still gets a label');
  eq(MT.teams.find(t => t.rosterId === 4).owner, 'no manager',
     'an orphan roster says it has no manager');
  ok(MT.teams[MT.teams.length - 1].score.raw === 0,
     'the empty roster scores zero and sorts last');

  const myTeam = MT.teams.find(t => t.rosterId === 2);
  eq(myTeam.players.length, 5, 'league scoring reads reserve and taxi too');
  eq(myTeam.score.nUnranked, 1, 'the IDP on my roster is counted as unranked');

  const leagueHtml = document.getElementById('league-pane-body').innerHTML;
  ok(leagueHtml.indexOf('How team strength is calculated') >= 0,
     'the metric is documented on the page');
  ok(leagueHtml.indexOf('rival') >= 0, 'other managers are listed by name');
  ok(leagueHtml.indexOf('View roster') >= 0, 'other teams can be opened');
  ok(leagueHtml.indexOf('Your team') >= 0, 'the page states where the user stands');

  MT.openTeamId = 3;
  renderLeagueTab();
  const opened = document.getElementById('league-pane-body').innerHTML;
  ok(opened.indexOf('Harrison Butker') >= 0,
     'opening a rival team shows its full roster');
  ok(opened.indexOf('Kicker') >= 0,
     'a rival roster gets the same unranked reasons');

  console.log('\n-- league view without a league --');
  MT.league = null;
  renderLeagueTab();
  ok(document.getElementById('league-pane-body').innerHTML
       .indexOf('No league loaded') >= 0,
     'custom-roster path explains there is nothing to compare');

  console.log('\n' + (failures ? failures + ' of ' + checks + ' checks FAILED'
                               : 'All ' + checks + ' checks passed.'));
  process.exit(failures ? 1 : 0);
})().catch(e => {
  console.error('harness threw:', e);
  process.exit(1);
});
