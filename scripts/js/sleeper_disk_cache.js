/* Permanent on-disk cache for IMMUTABLE Sleeper responses.
 *
 * Why this exists
 * ---------------
 * Discovery is cheap (~16 calls for a couple of hops). SCORING is not: the
 * Manager Score read path walks a dynasty chain season by season and pages
 * transactions AND matchups one NFL week at a time, so a single four-season
 * league costs roughly:
 *
 *     4 x ( league + users + rosters + drafts + draft picks
 *           + 19 transaction weeks + up to 18 matchup weeks )  ~= 160 calls
 *
 * Under a per-run call budget that is the entire ballgame. Re-scoring the
 * five leagues already in the corpus consumed the whole 600-call budget on
 * the run that built it, which left exactly nothing for finding new ones.
 * The corpus could not grow because it kept buying the same data every day.
 *
 * The fix is not a bigger budget, it is not paying twice. A completed NFL
 * season does not change: the 2023 transaction log, the 2023 box scores and
 * the 2023 draft board are final. Cache them once, forever, and the daily
 * budget goes to leagues we have never seen.
 *
 * What is cacheable, and how that is decided
 * ------------------------------------------
 * One rule, applied at the fetch layer: **a response is immutable iff the
 * league season it belongs to is strictly older than the season Sleeper's
 * own /state/nfl says is current.** Nothing else qualifies.
 *
 * That is the same judgement the shipped page already makes -- see
 * `knownPast` in msFetchTransactions/msFetchMatchups, which marks exactly
 * these responses immutable in sessionStorage. This is that cache made
 * durable and shared across runs, not a second opinion about what is safe
 * to keep.
 *
 * It FAILS CLOSED in every direction:
 *
 *   * /state/nfl unreadable  -> current season unknown -> cache nothing.
 *   * league season unknown  -> cache nothing for that league.
 *   * season >= current      -> cache nothing (an in-progress season must
 *                               never be pinned; that is how you serve a
 *                               week-3 box score in week 14).
 *   * a bundle on disk whose season is not strictly past the current season
 *                            -> discarded on load, not served. This
 *                               self-heals a bundle written by an earlier
 *                               run that misjudged the season, and it is
 *                               what makes the rollover into a new NFL year
 *                               safe without a migration.
 *
 * `/user/...` and `/state/nfl` are never cached at all: league membership
 * and the live week change under us by design.
 *
 * Layout
 * ------
 *   <dir>/drafts.json          draft_id -> league_id (drafts carry no league
 *                              in their own URL, so this is the only way to
 *                              route /draft/{id}/picks to a bundle)
 *   <dir>/<league_id>.json.gz  one bundle per league SEASON
 *
 * A Sleeper league id is already season-scoped -- a dynasty chain is a
 * linked list of one league id per season -- so "keyed by (league_id,
 * season, week)" collapses to bundle(league_id) -> entries[url]. The season
 * is stored in the bundle anyway, because a key you cannot audit is a key
 * you cannot trust.
 *
 * Bundles are write-once and gzipped. Measured on real 2024 payloads:
 * 55,468 bytes of transaction + matchup JSON compresses to 9,609 (5.8x).
 * A full past league-season is ~235 KB raw, ~40 KB on disk. Committing
 * them is what makes the saving survive to tomorrow's run -- the site is
 * static, so a committed artifact is the only durable storage there is.
 *
 * Raw response TEXT is stored, never a re-serialised object, so what the
 * scorer parses on a hit is byte-identical to what it parsed on the miss.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const zlib = require('zlib');

const BUNDLE_SCHEMA = 'sleeper-immutable-cache/1';

/* Which bundle does this URL belong to, if any?
 *
 * Returns { kind, bundleId } where bundleId is a league id, or null when the
 * URL is not cacheable in principle (user lookups, /state/nfl, anything off
 * the Sleeper v1 API). Draft URLs resolve only once the draft->league link
 * has been observed.
 */
function classify(url, draftIndex) {
  const m = /^https?:\/\/api\.sleeper\.app\/v1\/(.+)$/.exec(String(url));
  if (!m) return { kind: 'other', bundleId: null, key: null };
  const suffix = m[1];

  let g = /^league\/([0-9]+)(\/.*)?$/.exec(suffix);
  if (g) return { kind: 'league', bundleId: g[1], key: suffix };

  g = /^draft\/([0-9]+)(\/.*)?$/.exec(suffix);
  if (g) {
    const lid = draftIndex.get(g[1]) || null;
    return { kind: 'draft', bundleId: lid, key: suffix };
  }

  /* user/*, state/nfl, players/* -- mutable or irrelevant. */
  return { kind: 'other', bundleId: null, key: null };
}

function createDiskCache(opts) {
  const dir = opts && opts.dir ? String(opts.dir) : null;
  const maxBytes = opts && opts.maxBytes != null
    ? Number(opts.maxBytes) : 512 * 1024 * 1024;

  const stats = {
    enabled: !!dir,
    hits: 0,
    misses: 0,
    stored: 0,
    skipped_mutable: 0,
    skipped_unknown_season: 0,
    bundles_loaded: 0,
    bundles_written: 0,
    bundles_rejected_not_past: 0,
    bytes_on_disk: 0,
    over_size_cap: false
  };

  /* league_id -> season (string). Learned from every /league/{id} body we
   * see, whether it came from the network or from this cache. */
  const leagueSeason = new Map();
  const draftIndex = new Map();          /* draft_id -> league_id */
  const loaded = new Map();              /* league_id -> {season, entries} */
  const dirty = new Set();
  let currentSeason = null;
  let draftIndexDirty = false;

  if (dir) {
    fs.mkdirSync(dir, { recursive: true });
    try {
      const raw = fs.readFileSync(path.join(dir, 'drafts.json'), 'utf8');
      const obj = JSON.parse(raw);
      for (const k of Object.keys(obj || {})) draftIndex.set(k, String(obj[k]));
    } catch (e) { /* absent or corrupt: rebuilt from this run's reads */ }
  }

  function bundlePath(leagueId) {
    return path.join(dir, String(leagueId) + '.json.gz');
  }

  /* A bundle is only trustworthy if its season is strictly past the season
   * Sleeper currently reports. Anything else is discarded rather than
   * served -- see "fails closed" above. */
  function bundleUsable(b) {
    if (!b || b.schema !== BUNDLE_SCHEMA) return false;
    if (currentSeason == null) return false;
    const s = Number(b.season);
    const c = Number(currentSeason);
    if (!Number.isFinite(s) || !Number.isFinite(c)) return false;
    return s < c;
  }

  function loadBundle(leagueId) {
    const id = String(leagueId);
    if (loaded.has(id)) return loaded.get(id);
    if (!dir) { loaded.set(id, null); return null; }
    let b = null;
    try {
      const gz = fs.readFileSync(bundlePath(id));
      b = JSON.parse(zlib.gunzipSync(gz).toString('utf8'));
      stats.bundles_loaded++;
    } catch (e) {
      b = null;                       /* absent or unreadable: treat as cold */
    }
    if (b && !bundleUsable(b)) {
      stats.bundles_rejected_not_past++;
      b = null;
    }
    if (b && b.season != null) leagueSeason.set(id, String(b.season));
    loaded.set(id, b);
    return b;
  }

  /* Record what a response body teaches us about seasons and draft->league
   * links. Called for BOTH cache hits and network responses, because a run
   * served entirely from cache still has to know which bundles to route
   * later URLs to. */
  function observe(url, text) {
    if (!text) return;
    const m = /^https?:\/\/api\.sleeper\.app\/v1\/(.+)$/.exec(String(url));
    if (!m) return;
    const suffix = m[1];
    let body;
    try { body = JSON.parse(text); } catch (e) { return; }

    if (suffix === 'state/nfl') {
      if (body && body.season != null) currentSeason = String(body.season);
      return;
    }
    let g = /^league\/([0-9]+)$/.exec(suffix);
    if (g && body && body.season != null) {
      leagueSeason.set(g[1], String(body.season));
      return;
    }
    g = /^league\/([0-9]+)\/drafts$/.exec(suffix);
    if (g && Array.isArray(body)) {
      for (const d of body) {
        const did = d && (d.draft_id || d.id);
        if (!did) continue;
        const lid = String((d && d.league_id) || g[1]);
        if (draftIndex.get(String(did)) !== lid) {
          draftIndex.set(String(did), lid);
          draftIndexDirty = true;
        }
      }
    }
  }

  function read(url) {
    if (!dir) return undefined;
    const c = classify(url, draftIndex);
    if (!c.bundleId) return undefined;
    const b = loadBundle(c.bundleId);
    if (!b || !b.entries) { stats.misses++; return undefined; }
    const hit = Object.prototype.hasOwnProperty.call(b.entries, c.key)
      ? b.entries[c.key] : undefined;
    if (hit === undefined) { stats.misses++; return undefined; }
    stats.hits++;
    observe(url, hit);
    return hit;
  }

  function write(url, text) {
    if (!dir || text == null) return false;
    observe(url, text);

    const c = classify(url, draftIndex);
    if (!c.bundleId) return false;

    if (currentSeason == null) { stats.skipped_unknown_season++; return false; }
    const season = leagueSeason.get(String(c.bundleId));
    if (season == null) { stats.skipped_unknown_season++; return false; }

    const s = Number(season);
    const cur = Number(currentSeason);
    if (!Number.isFinite(s) || !Number.isFinite(cur)) {
      stats.skipped_unknown_season++;
      return false;
    }
    if (!(s < cur)) { stats.skipped_mutable++; return false; }
    if (stats.over_size_cap) return false;

    let b = loadBundle(c.bundleId);
    if (!b) {
      b = {
        schema: BUNDLE_SCHEMA,
        league_id: String(c.bundleId),
        season: String(season),
        cached_at: new Date().toISOString(),
        entries: {}
      };
      loaded.set(String(c.bundleId), b);
    }
    if (Object.prototype.hasOwnProperty.call(b.entries, c.key)) return false;
    b.entries[c.key] = text;
    dirty.add(String(c.bundleId));
    stats.stored++;
    return true;
  }

  function dirBytes() {
    let total = 0;
    try {
      for (const f of fs.readdirSync(dir)) {
        try { total += fs.statSync(path.join(dir, f)).size; } catch (e) { /* race */ }
      }
    } catch (e) { /* absent */ }
    return total;
  }

  /* Write dirty bundles. Temp + rename so an interrupted run cannot leave a
   * truncated bundle that would then be served as if it were complete. */
  function flush() {
    if (!dir) return stats;
    for (const id of dirty) {
      const b = loaded.get(id);
      if (!b) continue;
      try {
        const gz = zlib.gzipSync(Buffer.from(JSON.stringify(b), 'utf8'), { level: 9 });
        const p = bundlePath(id);
        const tmp = p + '.tmp';
        fs.writeFileSync(tmp, gz);
        fs.renameSync(tmp, p);
        stats.bundles_written++;
      } catch (e) {
        /* A cache that cannot be written must not break a crawl. */
      }
    }
    dirty.clear();
    if (draftIndexDirty) {
      try {
        const obj = {};
        for (const [k, v] of draftIndex) obj[k] = v;
        const p = path.join(dir, 'drafts.json');
        fs.writeFileSync(p + '.tmp', JSON.stringify(obj, null, 0));
        fs.renameSync(p + '.tmp', p);
        draftIndexDirty = false;
      } catch (e) { /* non-fatal */ }
    }
    stats.bytes_on_disk = dirBytes();
    stats.over_size_cap = stats.bytes_on_disk > maxBytes;
    return stats;
  }

  function checkSizeCap() {
    if (!dir) return;
    stats.bytes_on_disk = dirBytes();
    stats.over_size_cap = stats.bytes_on_disk > maxBytes;
  }

  checkSizeCap();

  return {
    read: read,
    write: write,
    observe: observe,
    flush: flush,
    stats: () => Object.assign({}, stats, {
      current_season: currentSeason,
      n_leagues_known: leagueSeason.size,
      n_drafts_indexed: draftIndex.size
    })
  };
}

module.exports = { createDiskCache, classify, BUNDLE_SCHEMA };
