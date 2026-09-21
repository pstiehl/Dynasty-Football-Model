/* The permanent Sleeper cache: what it keeps, what it refuses, what it heals.
 *
 * Run directly:  node tests/js/sleeper_cache_tests.js
 * Also run from: tests/test_crossleague_growth.py
 *
 * No network. Every response is a fixture, which is the point: the property
 * under test is a DECISION ("is this season final?"), and a decision should
 * be provable against hand-written inputs rather than whatever Sleeper
 * happens to return today.
 *
 * The dangerous failure this file exists to prevent is not a cache miss. It
 * is a cache HIT that should have been a miss -- serving a half-finished
 * season from disk forever, so the board silently scores a league on week-3
 * data in week 14 and looks completely normal while doing it.
 */
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { createDiskCache, classify } = require('../../scripts/js/sleeper_disk_cache.js');

let failures = 0;
let checks = 0;

function ok(cond, label) {
  checks++;
  if (cond) { console.log('  OK   ' + label); }
  else { failures++; console.log('  FAIL ' + label); }
}

function eq(got, want, label) {
  ok(got === want, label + '  (got ' + JSON.stringify(got) +
     ', want ' + JSON.stringify(want) + ')');
}

function tmpdir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'xl-cache-test-'));
}

const API = 'https://api.sleeper.app/v1';
const STATE_2026 = JSON.stringify({ season: '2026', week: 3 });
const LEAGUE_2024 = JSON.stringify({ league_id: '111', season: '2024' });
const LEAGUE_2026 = JSON.stringify({ league_id: '999', season: '2026' });
const TXNS = JSON.stringify([{ type: 'trade', status: 'complete' }]);

/* -------------------------------------------------- URL classification */

(function classification() {
  console.log('\nclassify()');
  const idx = new Map([['d1', '111']]);
  eq(classify(API + '/league/111/transactions/4', idx).bundleId, '111',
     'league sub-resources route to the league bundle');
  eq(classify(API + '/league/111', idx).bundleId, '111',
     'the league object routes to its own bundle');
  eq(classify(API + '/draft/d1/picks', idx).bundleId, null,
     'a non-numeric draft id is not routed');
  eq(classify(API + '/state/nfl', idx).bundleId, null,
     '/state/nfl is never cacheable: the live week changes hourly');
  eq(classify(API + '/user/abc/leagues/nfl/2026', idx).bundleId, null,
     'user league lists are never cacheable: membership changes');
  eq(classify('https://example.com/league/111', idx).bundleId, null,
     'non-Sleeper URLs are out of scope');
})();

/* ------------------------------------------- completed seasons are kept */

(function keepsPastSeasons() {
  console.log('\na COMPLETED season is cached and served');
  const dir = tmpdir();
  let c = createDiskCache({ dir: dir });

  c.write(API + '/state/nfl', STATE_2026);          // current season = 2026
  c.write(API + '/league/111', LEAGUE_2024);        // this league is 2024
  const stored = c.write(API + '/league/111/transactions/4', TXNS);
  eq(stored, true, 'a 2024 transaction page is stored while 2026 is live');
  c.flush();

  ok(fs.existsSync(path.join(dir, '111.json.gz')),
     'the bundle is written as gzip, one file per league-season');

  /* A NEW cache instance: this is the across-runs path, which is the only
   * one that saves anything. An in-process hit would prove nothing. */
  c = createDiskCache({ dir: dir });
  c.write(API + '/state/nfl', STATE_2026);
  eq(c.read(API + '/league/111/transactions/4'), TXNS,
     'a later RUN reads the same page back byte-for-byte');
  eq(c.stats().hits, 1, 'and counts it as a hit');

  /* Byte-identical, not merely equal-when-parsed: the scorer must parse on
   * a hit exactly what it parsed on the miss. */
  eq(typeof c.read(API + '/league/111/transactions/4'), 'string',
     'raw response TEXT is stored, never a re-serialised object');
})();

/* ------------------------------------ in-progress seasons are NOT kept */

(function refusesCurrentSeason() {
  console.log('\nan IN-PROGRESS season is never cached');
  const dir = tmpdir();
  const c = createDiskCache({ dir: dir });

  c.write(API + '/state/nfl', STATE_2026);
  c.write(API + '/league/999', LEAGUE_2026);        // league season == live
  const stored = c.write(API + '/league/999/transactions/4', TXNS);
  eq(stored, false, 'a 2026 transaction page is refused while 2026 is live');
  eq(c.stats().skipped_mutable >= 1, true, 'and is counted as skipped');
  c.flush();
  eq(c.read(API + '/league/999/transactions/4'), undefined,
     'so it can never be served from disk');
})();

/* ------------------------------------------------- fail-closed on doubt */

(function failsClosed() {
  console.log('\nunknown seasons fail CLOSED');

  let dir = tmpdir();
  let c = createDiskCache({ dir: dir });
  /* No /state/nfl at all: we do not know what "current" means. */
  c.write(API + '/league/111', LEAGUE_2024);
  eq(c.write(API + '/league/111/transactions/4', TXNS), false,
     'without /state/nfl nothing is cached, even a plainly old season');

  dir = tmpdir();
  c = createDiskCache({ dir: dir });
  c.write(API + '/state/nfl', STATE_2026);
  /* League object never seen, so the season of league 222 is unknown. */
  eq(c.write(API + '/league/222/transactions/4', TXNS), false,
     'a league whose season was never observed is not cached');
  eq(c.stats().skipped_unknown_season >= 2 || c.stats().skipped_unknown_season >= 1,
     true, 'and the refusal is counted, not silent');
})();

/* --------------------------------------------- self-healing on rollover */

(function healsStaleBundle() {
  console.log('\na bundle that is no longer in the past is DISCARDED');
  const dir = tmpdir();

  /* Hand-write a bundle claiming season 2026 -- the shape an earlier run
   * would leave behind if it had misjudged the season, and exactly what a
   * new NFL year turns yesterday's "past" bundle into. */
  const zlib = require('zlib');
  fs.writeFileSync(path.join(dir, '999.json.gz'), zlib.gzipSync(Buffer.from(
    JSON.stringify({
      schema: 'sleeper-immutable-cache/1',
      league_id: '999', season: '2026',
      entries: { 'league/999/transactions/4': TXNS }
    }), 'utf8')));

  const c = createDiskCache({ dir: dir });
  c.write(API + '/state/nfl', STATE_2026);          // 2026 is LIVE
  eq(c.read(API + '/league/999/transactions/4'), undefined,
     'a bundle whose season is not strictly past is not served');
  eq(c.stats().bundles_rejected_not_past, 1,
     'it is rejected on load and the rejection is reported');
})();

/* ------------------------------------------------------ draft routing */

(function draftRouting() {
  console.log('\ndraft picks route to their league bundle');
  const dir = tmpdir();
  let c = createDiskCache({ dir: dir });

  c.write(API + '/state/nfl', STATE_2026);
  c.write(API + '/league/111', LEAGUE_2024);
  /* The drafts listing is what teaches the cache draft -> league. */
  c.write(API + '/league/111/drafts',
          JSON.stringify([{ draft_id: '777', league_id: '111' }]));
  eq(c.write(API + '/draft/777/picks', JSON.stringify([{ pick_no: 1 }])), true,
     'a completed draft is stored against its league-season');
  c.flush();

  c = createDiskCache({ dir: dir });
  c.write(API + '/state/nfl', STATE_2026);
  ok(c.read(API + '/draft/777/picks') !== undefined,
     'and is found on a later run via the persisted draft index');
})();

/* -------------------------------------------------------- size ceiling */

(function sizeCeiling() {
  console.log('\nthe cache has a size ceiling');
  const dir = tmpdir();
  const c = createDiskCache({ dir: dir, maxBytes: 1 });   // already over
  c.write(API + '/state/nfl', STATE_2026);
  c.write(API + '/league/111', LEAGUE_2024);
  /* Nothing on disk yet, so the first write lands; after a flush the cap
   * bites and further additions stop. Serving is never disabled. */
  c.write(API + '/league/111/transactions/4', TXNS);
  c.flush();
  eq(c.stats().over_size_cap, true, 'the cap is reported once exceeded');
  eq(c.write(API + '/league/111/transactions/5', TXNS), false,
     'and no further entries are added');
  eq(c.read(API + '/league/111/transactions/4'), TXNS,
     'but everything already cached is still SERVED');
})();

/* ------------------------------------------------------------ disabled */

(function disabled() {
  console.log('\nno cache dir = a no-op, not an error');
  const c = createDiskCache({ dir: null });
  eq(c.read(API + '/league/111/transactions/4'), undefined, 'read is undefined');
  eq(c.write(API + '/league/111/transactions/4', TXNS), false, 'write is false');
  eq(c.stats().enabled, false, 'and it reports itself disabled');
})();

console.log('\n' + (failures ? 'FAILED ' : 'passed ') +
            (checks - failures) + '/' + checks);
process.exit(failures ? 1 : 0);
