// Artifact fixtures, registered with the stub fetch BEFORE the page scripts
// run. Ordering matters: myteam.js kicks off loadModelData() at parse time,
// exactly as it does in the browser, so anything registered after the blobs
// would arrive too late and every join would be tested against a 404.
//
// Prepended by scripts/check_site_behaviour.py, after tests/js/dom_stub.js.

// engine_rankings.json rows are keyed by gsis id, as the site build writes
// them. Two "Chris Olave" rows at the same position exist to exercise the
// refuse-to-guess path.
const RANKINGS = [
  { player_id: '00-0036900', name: "Ja'Marr Chase", position: 'WR',
    overall_rank: 1, age: 26, tier: 1, comp_tier: 'elite', production_score: 1900 },
  { player_id: '00-0033873', name: 'Patrick Mahomes', position: 'QB',
    overall_rank: 2, age: 31, tier: 1, comp_tier: 'elite', production_score: 1850 },
  { player_id: '00-0038542', name: 'Bijan Robinson', position: 'RB',
    overall_rank: 5, age: 24, tier: 1, comp_tier: 'elite', production_score: 1700 },
  // The join-gap case: rostered, ranked, but the crosswalk has no gsis id.
  { player_id: '00-0039163', name: 'Jaxson Dart', position: 'QB',
    overall_rank: 64, age: 23, tier: 4, comp_tier: 'solid', production_score: 720 },
  { player_id: '00-0033077', name: 'Dak Prescott', position: 'QB',
    overall_rank: 41, age: 33, tier: 3, comp_tier: 'solid', production_score: 980 },
  { player_id: '00-0090001', name: 'Chris Olave', position: 'WR',
    overall_rank: 70, age: 26, tier: 4, comp_tier: 'solid', production_score: 690 },
  { player_id: '00-0090002', name: 'Chris Olave', position: 'WR',
    overall_rank: 300, age: 24, tier: 9, comp_tier: 'fringe', production_score: 90 }
];

for (let i = 0; i < 40; i++) {
  RANKINGS.push({
    player_id: '00-01' + String(10000 + i),
    name: 'Filler Player ' + i,
    position: 'WR',
    overall_rank: 150 + i,
    age: 26, tier: 7, comp_tier: 'depth', production_score: 300 - i
  });
}

// roster_index.json: sleeper_id -> [name, position, team, gsis_id].
// "" in the gsis slot is exactly what report._load_sleeper_player_index
// writes for a player the DB has no gsis id for.
const CROSSWALK = {
  generated_at: '2026-09-21T11:00:00+00:00',
  available: true,
  fields: ['name', 'position', 'team', 'gsis_id'],
  players: {
    '7564': ["Ja'Marr Chase", 'WR', 'CIN', '00-0036900'],
    '4046': ['Patrick Mahomes', 'QB', 'KC', '00-0033873'],
    '9509': ['Bijan Robinson', 'RB', 'ATL', '00-0038542'],
    '12001': ['Jaxson Dart', 'QB', 'NYG', ''],          // gsis gap
    '3294': ['Dak Prescott', 'QB', 'DAL', '00-0033077'],
    '5000': ['Harrison Butker', 'K', 'KC', '00-0033433'],
    '5001': ['Micah Parsons', 'LB', 'GB', '00-0036612'],
    '5002': ['Rookie Nobody', 'WR', 'SEA', '00-0099999'], // ranked by nobody
    '5003': ['Chris Olave', 'WR', 'NO', '']              // ambiguous by name
  }
};
for (let i = 0; i < 40; i++) {
  CROSSWALK.players['6' + (100 + i)] =
    ['Filler Player ' + i, 'WR', 'BUF', '00-01' + String(10000 + i)];
}

FIXTURES['engine_rankings.json'] = RANKINGS;
FIXTURES['roster_index.json'] = CROSSWALK;

// highlights.json, with the resolved window this PR adds. Monday's build
// for 2026-09-21 resolves to the Sep 10-14 slate.
const HIGHLIGHTS = {
  generated_at: '2026-09-21T11:05:00+00:00',
  resolved_as_of: '2026-09-21T12:41:00+00:00',
  expected_week: 1,
  window: {
    start: '2026-09-10T00:00:00+00:00',
    end: '2026-09-14T23:59:59+00:00',
    publish_cutoff: '2026-09-16T11:59:59+00:00',
    opens_at: '2026-09-15T12:00:00+00:00',
    grace_hours: 36,
    window_days: 5,
    label: 'Sep 10\u201314',
    week: 1
  },
  window_start: '2026-09-10T00:00:00+00:00',
  window_end: '2026-09-14T23:59:59+00:00',
  clips: {
    // Dak: one clip inside the window, one from a fortnight earlier.
    '3294': [
      { video_id: 'dak_in', title: 'Dak Prescott Every Throw', channel_title: 'Cuts',
        published_at: '2026-09-14T22:00:00Z', duration_seconds: 300,
        kind: 'player_cutup', confidence: 0.85, week: 1, in_window: true },
      { video_id: 'dak_old', title: 'Dak Prescott Preseason', channel_title: 'Cuts',
        published_at: '2026-08-30T22:00:00Z', duration_seconds: 280,
        kind: 'player_cutup', confidence: 0.8, in_window: false }
    ],
    // Jaxson Dart: only older film. Must not lead, must stay reachable.
    '12001': [
      { video_id: 'dart_old', title: 'Jaxson Dart Highlights', channel_title: 'Cuts',
        published_at: '2026-09-04T18:00:00Z', duration_seconds: 240,
        kind: 'player_cutup', confidence: 0.8, in_window: false }
    ],
    '7564': [
      { video_id: 'chase_in', title: "Ja'Marr Chase Week 1", channel_title: 'Cuts',
        published_at: '2026-09-13T20:00:00Z', duration_seconds: 260,
        kind: 'player_cutup', confidence: 0.9, week: 1, in_window: true }
    ]
  },
  by_gsis: { '00-0033077': '3294', '00-0036900': '7564' },
  players: {
    '3294': { name: 'Dak Prescott', position: 'QB', team: 'DAL', rank: 41 },
    '12001': { name: 'Jaxson Dart', position: 'QB', team: 'NYG' },
    '7564': { name: "Ja'Marr Chase", position: 'WR', team: 'CIN', rank: 1 }
  },
  stats: {}
};


FIXTURES['highlights.json'] = HIGHLIGHTS;
