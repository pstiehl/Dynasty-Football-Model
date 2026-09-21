// Dynasty Model proxy + cross-league corpus worker.
//
// Two jobs in one deployment, deliberately:
//
//   1. READ PROXY (unchanged, pre-existing). Proxies myfantasyleague.com so
//      the GitHub Pages site can fetch a user's league data client-side
//      (MFL's CORS only allows their own subdomains), and proxies
//      api.sleeper.app purely to add edge caching to slow endpoints.
//
//   2. CORPUS BACKEND (new). Accepts a Manager Score result that the
//      BROWSER computed, verifies it against Sleeper, and persists it to D1
//      so managers can be compared across leagues.
//
// Why the browser scores and the worker only verifies
// ---------------------------------------------------
// Scoring one dynasty league costs ~97 Sleeper calls: transactions are paged
// per week across every season in the league's history chain. A worker that
// crawled server-side would spend ~97 EXTERNAL subrequests on one request,
// which exceeds the Workers Free ceiling of 50 external subrequests per
// invocation. The browser already does this crawl today (Sleeper is
// CORS-friendly, so no proxy is needed) and already computes the score
// client-side, so the crawl cost lands on the user's own connection where it
// already lived.
//
// This worker therefore spends 3 external subrequests per submission:
// /league/<id>, /league/<id>/users, /league/<id>/rosters. Everything else it
// checks is arithmetic over the payload. See docs/CORPUS-BACKEND.md.
//
// Endpoints:
//   GET  /health
//   GET  /mfl/<year>/export?TYPE=...&L=...&JSON=1
//   GET  /sleeper/v1/<rest...>
//   POST /corpus/submit              <- browser posts a scored league
//   GET  /corpus/leaderboard         <- aggregated cross-league board
//   GET  /corpus/league/<league_id>  <- status of one league (UI polling)
//   GET  /corpus/stats               <- counts, caps, budget (public)
//   GET  /corpus/pending             <- auth: what the daily job should re-score
//   POST /corpus/reconcile           <- auth: daily job's authoritative verdict
//
// Deploy:
//   cd scripts/cf-worker
//   npx wrangler deploy
//
// You'll need a Cloudflare API token with "Workers Scripts: Edit" scope and,
// for the corpus half, a D1 database. See README.md in this directory.
// No credential is ever committed to this repo.

const ALLOWED_ORIGINS = [
  "https://pstiehl.github.io",
  "https://sandpaw-ai.github.io",
  "http://localhost:8000",   // local dev
];

const MFL_HOST = "https://api.myfantasyleague.com";
const SLEEPER_HOST = "https://api.sleeper.app";

const CACHE_TTL_SECONDS = 300; // 5 min

// ===========================================================================
// Scoring constants.
//
// These MIRROR src/dynasty/managerscore_js.py (MS_WEIGHTS, MS_SHRINK_K,
// MS_INDEX_CENTER, MS_INDEX_SCALE) and src/dynasty/crossleague.py. The whole
// point of the cross-league board is that it aggregates the SAME metric the
// per-league page shows, so a drift here silently produces a second, different
// Manager Score. tests/test_corpus_backend.py pins these against the real
// scorer and fails the build if they diverge.
// ===========================================================================

const COMPONENTS = ["draft", "trade", "waiver"];
const MS_WEIGHTS = { draft: 0.50, trade: 0.35, waiver: 0.15 };
const MS_SHRINK_K = { draft: 6, trade: 3, waiver: 5 };
const MS_INDEX_CENTER = 100;
const MS_INDEX_SCALE = 15;
const MIN_DRAFT_PICKS_FOR_BOARD = MS_SHRINK_K.draft;

// ===========================================================================
// Abuse controls.
//
// A public POST that triggers upstream fetches is an amplification vector:
// anyone can make our worker hammer Sleeper on their behalf, and anyone can
// try to stuff the leaderboard. Every number below is a deliberate ceiling,
// and every one of them is documented in docs/CORPUS-BACKEND.md with its
// rationale and its known weakness.
// ===========================================================================

const SUBMISSION_SCHEMA = "dfm.corpus.submission.v1";

const MAX_BODY_BYTES = 65536;       // 64 KB. A 32-team payload is ~12 KB.
const MAX_MANAGERS = 32;            // Sleeper's largest sane league.
const MIN_MANAGERS = 2;             // one manager cannot define a z-distribution
const MAX_LINEAGE_IDS = 30;         // 30 seasons of dynasty history is generous
const MAX_STRING = 120;             // league/manager name cap
const MAX_FLAGS_PER_MANAGER = 8;
const MAX_ABS_Z = 6;                // |z| beyond this is not a real league

const RATE_IP_HOURLY = 10;
const RATE_IP_DAILY = 30;
const GLOBAL_DAILY_CAP = 500;
const DEDUPE_WINDOW_SECONDS = 6 * 3600;   // same league id, 6h cooldown

const LEADERBOARD_MAX_LIMIT = 500;
const LEADERBOARD_ROW_CAP = 20000;        // hard stop on rows aggregated
const PENDING_MAX_LIMIT = 200;

// Tolerances for the internal-consistency checks. Loose enough to absorb
// float64 round-tripping through JSON, tight enough that hand-written numbers
// do not pass.
const TOL_Z_MEAN = 0.02;
const TOL_Z_SD = 0.02;
const TOL_COMPOSITE = 1e-4;
const TOL_INDEX = 0.05;
const TOL_SHRUNK = 1e-3;

// ===========================================================================
// Small pure helpers
// ===========================================================================

function corsHeaders(originHeader, methods) {
  // Echo origin only if it's on the allowlist; otherwise fall back to "null"
  // so misconfigured cross-origin reads don't silently succeed.
  const allowed = ALLOWED_ORIGINS.includes(originHeader) ? originHeader : ALLOWED_ORIGINS[0];
  return {
    "Access-Control-Allow-Origin": allowed,
    "Vary": "Origin",
    "Access-Control-Allow-Methods": methods || "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
  };
}

function jsonResponse(body, status, originHeader, extraHeaders) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      ...corsHeaders(originHeader),
      ...(extraHeaders || {}),
    },
  });
}

function isFiniteNumber(v) {
  return typeof v === "number" && isFinite(v);
}

function mean(xs) {
  if (!xs.length) return 0;
  let s = 0;
  for (const x of xs) s += x;
  return s / xs.length;
}

function pstdev(xs) {
  if (xs.length < 2) return 0;
  const mu = mean(xs);
  let s = 0;
  for (const x of xs) s += (x - mu) * (x - mu);
  return Math.sqrt(s / xs.length);
}

/* n/(n+k) shrinkage toward zero — msShrink() in the page scorer. */
function msShrink(m, n, k) {
  if (!n || n <= 0) return 0;
  return m * (n / (n + k));
}

function round(v, digits) {
  const p = Math.pow(10, digits);
  return Math.round(v * p) / p;
}

/* Sleeper ids are numeric snowflakes. msBuildInput() falls back to a
 * synthetic "roster:<leagueId>:<rosterId>" id for a roster with no owner —
 * those are not people, cannot be checked against /league/users, and are
 * refused rather than indexed. */
function isSleeperId(s) {
  return typeof s === "string" && /^[0-9]{6,24}$/.test(s);
}

function cleanString(s, max) {
  if (typeof s !== "string") return null;
  // Strip control characters. Nothing from a payload is ever interpolated
  // into SQL (every statement is parameterised) or into HTML by the worker,
  // but a name with a newline in it should not reach a log line either.
  const out = s.replace(/[\u0000-\u001f\u007f]/g, "").trim();
  if (!out) return null;
  return out.slice(0, max || MAX_STRING);
}

function utcDayStamp(d) { return d.toISOString().slice(0, 10); }
function utcHourStamp(d) { return d.toISOString().slice(0, 13); }

async function sha256Hex(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

/* Client IPs are never stored. Salted and date-scoped so the hash is usable
 * for same-day rate limiting and useless as a durable identifier. */
async function hashIp(ip, salt, now) {
  return sha256Hex(`${ip || "unknown"}|${salt || "no-salt"}|${utcDayStamp(now)}`);
}

/* Constant-time-ish string compare for the reconcile bearer token. */
function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// ===========================================================================
// Stage 1 — schema validation (pure, no network, no DB)
// ===========================================================================

/* Validate and NORMALISE a submission payload.
 *
 * Returns { ok, errors: [...], value: <normalised> }. Nothing from the
 * payload survives into `value` unless it was explicitly copied here with a
 * type and a bound. Unknown keys are dropped rather than rejected, so an
 * older site build posting an extra field does not break; unknown SHAPE is
 * rejected. Nothing is ever eval'd, Function'd, or interpolated into SQL.
 */
function validateSubmission(payload) {
  const errors = [];
  const fail = (m) => { errors.push(m); return { ok: false, errors, value: null }; };

  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return fail("payload must be a JSON object");
  }
  if (payload.schema !== SUBMISSION_SCHEMA) {
    return fail(`schema must be "${SUBMISSION_SCHEMA}"`);
  }

  const L = payload.league;
  if (!L || typeof L !== "object" || Array.isArray(L)) return fail("league object missing");

  const leagueId = typeof L.league_id === "string" ? L.league_id.trim() : "";
  if (!isSleeperId(leagueId)) return fail("league.league_id must be a numeric Sleeper id");

  const nTeams = L.n_teams;
  if (!Number.isInteger(nTeams) || nTeams < MIN_MANAGERS || nTeams > MAX_MANAGERS) {
    return fail(`league.n_teams must be an integer in [${MIN_MANAGERS}, ${MAX_MANAGERS}]`);
  }

  const season = cleanString(String(L.season == null ? "" : L.season), 8);
  if (!season || !/^[0-9]{4}$/.test(season)) return fail("league.season must be a 4-digit year");

  const name = cleanString(L.name, MAX_STRING) || leagueId;

  let lineage = [];
  if (L.lineage_ids != null) {
    if (!Array.isArray(L.lineage_ids)) return fail("league.lineage_ids must be an array");
    if (L.lineage_ids.length > MAX_LINEAGE_IDS) {
      return fail(`league.lineage_ids exceeds ${MAX_LINEAGE_IDS}`);
    }
    for (const id of L.lineage_ids) {
      if (!isSleeperId(typeof id === "string" ? id.trim() : "")) {
        return fail("league.lineage_ids must all be numeric Sleeper ids");
      }
      lineage.push(String(id).trim());
    }
  }
  if (!lineage.includes(leagueId)) lineage.unshift(leagueId);

  const lineageRoot = isSleeperId(String(L.lineage_root || "").trim())
    ? String(L.lineage_root).trim()
    : lineage[lineage.length - 1];

  let nSeasons = L.n_seasons_scored;
  if (!Number.isInteger(nSeasons) || nSeasons < 1 || nSeasons > MAX_LINEAGE_IDS) {
    nSeasons = lineage.length;
  }

  // ---- managers ----------------------------------------------------------
  const M = payload.managers;
  if (!Array.isArray(M)) return fail("managers must be an array");
  if (M.length < MIN_MANAGERS) return fail(`at least ${MIN_MANAGERS} managers required`);
  if (M.length > MAX_MANAGERS) return fail(`more than ${MAX_MANAGERS} managers`);
  if (M.length > nTeams) return fail("more managers than rosters in the league");

  const seen = new Set();
  const managers = [];
  for (const row of M) {
    if (!row || typeof row !== "object" || Array.isArray(row)) {
      return fail("each manager must be an object");
    }
    const mid = typeof row.manager_id === "string" ? row.manager_id.trim() : "";
    if (!isSleeperId(mid)) {
      // This is also where "roster:<league>:<n>" placeholders are refused.
      return fail("manager_id must be a numeric Sleeper user id");
    }
    if (seen.has(mid)) return fail(`duplicate manager_id ${mid}`);
    seen.add(mid);

    if (!isFiniteNumber(row.index)) return fail(`manager ${mid}: index must be a number`);
    if (!isFiniteNumber(row.composite)) return fail(`manager ${mid}: composite must be a number`);
    if (row.rank != null && !Number.isInteger(row.rank)) {
      return fail(`manager ${mid}: rank must be an integer`);
    }

    const comps = {};
    const src = row.components;
    if (!src || typeof src !== "object" || Array.isArray(src)) {
      return fail(`manager ${mid}: components missing`);
    }
    for (const c of COMPONENTS) {
      const raw = src[c];
      if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
        return fail(`manager ${mid}: components.${c} missing`);
      }
      const n = raw.n;
      if (!Number.isInteger(n) || n < 0 || n > 100000) {
        return fail(`manager ${mid}: components.${c}.n out of range`);
      }
      for (const f of ["z", "mean", "shrunk"]) {
        if (!isFiniteNumber(raw[f])) {
          return fail(`manager ${mid}: components.${c}.${f} must be a finite number`);
        }
      }
      if (Math.abs(raw.z) > MAX_ABS_Z) {
        return fail(`manager ${mid}: components.${c}.z beyond +/-${MAX_ABS_Z}`);
      }
      comps[c] = {
        n,
        z: raw.z,
        mean: raw.mean,
        shrunk: raw.shrunk,
        available: n > 0,
      };
    }

    let flags = [];
    if (row.flags != null) {
      if (!Array.isArray(row.flags)) return fail(`manager ${mid}: flags must be an array`);
      flags = row.flags
        .slice(0, MAX_FLAGS_PER_MANAGER)
        .map((f) => cleanString(f, MAX_STRING))
        .filter(Boolean);
    }

    managers.push({
      manager_id: mid,
      // Kept only to report a mismatch. The name actually STORED is the one
      // Sleeper returns during verification — see verifyAgainstSleeper.
      claimed_name: cleanString(row.display_name, MAX_STRING) || mid,
      index: row.index,
      rank: row.rank == null ? null : row.rank,
      composite: row.composite,
      components: comps,
      flags,
    });
  }

  // A league where half the rosters were dropped from the payload is either
  // broken or cherry-picked. Two unowned/orphan rosters is the realistic
  // ceiling for a live league.
  if (nTeams - managers.length > 2) {
    return fail(`payload covers ${managers.length} of ${nTeams} rosters`);
  }

  return {
    ok: true,
    errors,
    value: {
      league: {
        league_id: leagueId,
        name,
        season,
        n_teams: nTeams,
        n_seasons_scored: nSeasons,
        lineage_ids: lineage,
        lineage_root: lineageRoot,
      },
      managers,
    },
  };
}

// ===========================================================================
// Stage 2 — internal consistency (pure, no network, no DB)
// ===========================================================================

/* Check that the submitted numbers are self-consistent with the scoring model.
 *
 * This is the cheap half of "never trust the client". It cannot prove the
 * underlying transactions are real — only the daily authoritative re-score
 * does that — but it makes fabrication expensive, because a forger has to
 * reproduce the whole scoring model rather than type in numbers:
 *
 *   1. shrunk == mean * n/(n+k) for every component of every manager
 *   2. a component is "live" iff >= 2 managers have evidence in it
 *   3. within each live component, z over the managers WITH evidence has
 *      mean 0 and population sd 1 (that is what msZScores produces)
 *   4. composite == sum over components of (renormalised weight * z)
 *   5. index == 100 + 15 * composite
 *   6. ranks are a permutation of 1..N ordered by index descending
 */
function checkConsistency(value) {
  const errors = [];
  const checks = [];
  const managers = value.managers;

  // ---- 1. shrinkage ------------------------------------------------------
  for (const m of managers) {
    for (const c of COMPONENTS) {
      const comp = m.components[c];
      const expect = msShrink(comp.mean, comp.n, MS_SHRINK_K[c]);
      if (Math.abs(expect - comp.shrunk) > TOL_SHRUNK + Math.abs(expect) * 1e-6) {
        errors.push(
          `${m.manager_id}.${c}: shrunk ${comp.shrunk} != mean*n/(n+k) ${round(expect, 6)}`
        );
      }
    }
  }
  checks.push({ check: "shrinkage", ok: errors.length === 0 });

  // ---- 2. live components ------------------------------------------------
  const live = COMPONENTS.filter(
    (c) => managers.filter((m) => m.components[c].available).length >= 2
  );
  let wsum = 0;
  for (const c of live) wsum += MS_WEIGHTS[c];
  const effective = {};
  for (const c of COMPONENTS) {
    effective[c] = (wsum > 0 && live.includes(c)) ? MS_WEIGHTS[c] / wsum : 0;
  }
  checks.push({ check: "live_components", ok: true, live });

  if (!live.length) {
    errors.push("no component has evidence from two or more managers — nothing to rank");
    return { ok: false, errors, checks, live, effective };
  }

  // ---- 3. z distribution -------------------------------------------------
  let zOk = true;
  for (const c of live) {
    const zs = managers.filter((m) => m.components[c].available)
                       .map((m) => m.components[c].z);
    const mu = mean(zs);
    const sd = pstdev(zs);
    if (Math.abs(mu) > TOL_Z_MEAN) {
      zOk = false;
      errors.push(`${c}: z-scores have mean ${round(mu, 4)}, expected 0`);
    }
    if (Math.abs(sd - 1) > TOL_Z_SD) {
      zOk = false;
      errors.push(`${c}: z-scores have sd ${round(sd, 4)}, expected 1`);
    }
    // A manager with no evidence in a component must carry z = 0, not a
    // number that quietly lifts their composite.
    for (const m of managers) {
      if (!m.components[c].available && m.components[c].z !== 0) {
        zOk = false;
        errors.push(`${m.manager_id}.${c}: z must be 0 without evidence`);
      }
    }
  }
  checks.push({ check: "z_distribution", ok: zOk });

  // ---- 4 + 5. composite and index ---------------------------------------
  let algebraOk = true;
  for (const m of managers) {
    let composite = 0;
    for (const c of COMPONENTS) composite += effective[c] * m.components[c].z;
    if (Math.abs(composite - m.composite) > TOL_COMPOSITE) {
      algebraOk = false;
      errors.push(
        `${m.manager_id}: composite ${round(m.composite, 6)} != ` +
        `weighted z sum ${round(composite, 6)}`
      );
    }
    const index = MS_INDEX_CENTER + MS_INDEX_SCALE * m.composite;
    if (Math.abs(index - m.index) > TOL_INDEX) {
      algebraOk = false;
      errors.push(
        `${m.manager_id}: index ${round(m.index, 3)} != 100 + 15*composite ` +
        `${round(index, 3)}`
      );
    }
  }
  checks.push({ check: "composite_and_index", ok: algebraOk });

  // ---- 6. ranks ----------------------------------------------------------
  let rankOk = true;
  const ranked = managers.filter((m) => m.rank != null);
  if (ranked.length) {
    if (ranked.length !== managers.length) {
      rankOk = false;
      errors.push("some managers carry a rank and some do not");
    } else {
      const byIndex = managers.slice().sort((a, b) => b.index - a.index);
      const seenRanks = new Set();
      for (let i = 0; i < byIndex.length; i++) {
        const r = byIndex[i].rank;
        if (r !== i + 1) {
          // Ties on index can legitimately order either way; only complain
          // when the index actually differs from the neighbour we expected.
          if (!(i > 0 && byIndex[i - 1].index === byIndex[i].index)) {
            rankOk = false;
            errors.push(`${byIndex[i].manager_id}: rank ${r} does not follow index order`);
          }
        }
        if (seenRanks.has(r)) { rankOk = false; errors.push(`duplicate rank ${r}`); }
        seenRanks.add(r);
      }
    }
  }
  checks.push({ check: "ranks", ok: rankOk });

  return { ok: errors.length === 0, errors, checks, live, effective };
}

// ===========================================================================
// Stage 3 — verify against Sleeper (3 external subrequests)
// ===========================================================================

/* Independently confirm the league is real, is a dynasty league, and has the
 * managers the payload claims.
 *
 * Exactly three external subrequests, all to Sleeper's public read-only API:
 *   GET /v1/league/<id>          league exists, settings.type == 2, size
 *   GET /v1/league/<id>/users    the claimed manager ids are members
 *   GET /v1/league/<id>/rosters  roster count agrees
 *
 * `fetchImpl` is injectable so tests can drive this without a network.
 */
async function verifyAgainstSleeper(value, fetchImpl) {
  const doFetch = fetchImpl || fetch;
  const leagueId = value.league.league_id;
  const notes = [];
  const errors = [];
  let external = 0;

  async function getJson(path) {
    external += 1;
    const res = await doFetch(`${SLEEPER_HOST}/v1${path}`, {
      headers: { "User-Agent": "dynasty-model-corpus/1.0 (+https://pstiehl.github.io)" },
    });
    if (!res.ok) throw new Error(`sleeper ${path} -> HTTP ${res.status}`);
    return res.json();
  }

  let league, users, rosters;
  try {
    league = await getJson(`/league/${leagueId}`);
    users = await getJson(`/league/${leagueId}/users`);
    rosters = await getJson(`/league/${leagueId}/rosters`);
  } catch (err) {
    return {
      ok: false,
      external,
      notes,
      errors: [`upstream_unavailable: ${String(err && err.message || err)}`],
      transient: true,
      names: {},
    };
  }

  // ---- league object -----------------------------------------------------
  if (!league || typeof league !== "object" || !league.league_id) {
    errors.push("sleeper does not know that league id");
    return { ok: false, external, notes, errors, transient: false, names: {} };
  }
  if (String(league.league_id) !== leagueId) {
    errors.push("sleeper returned a different league id");
  }

  // The dynasty filter. settings.type == 2 is empirical, not documented —
  // see docs/CROSS-LEAGUE-CORPUS.md §3. A redraft or keeper league is not
  // what this board measures, so it is refused here rather than diluted in.
  const type = league.settings && league.settings.type;
  if (type !== 2) {
    errors.push(`league settings.type is ${type == null ? "absent" : type}, not 2 (dynasty)`);
  } else {
    notes.push("dynasty: settings.type == 2");
  }

  if (Number(league.total_rosters) !== value.league.n_teams) {
    errors.push(
      `claimed ${value.league.n_teams} teams, sleeper says ${league.total_rosters}`
    );
  } else {
    notes.push(`roster count agrees (${league.total_rosters})`);
  }

  if (String(league.season || "") !== value.league.season) {
    errors.push(`claimed season ${value.league.season}, sleeper says ${league.season}`);
  }

  // ---- users -------------------------------------------------------------
  const names = {};
  const memberIds = new Set();
  if (Array.isArray(users)) {
    for (const u of users) {
      if (!u || !u.user_id) continue;
      const uid = String(u.user_id);
      memberIds.add(uid);
      // Pseudonymous display_name only. No attempt is made anywhere in this
      // pipeline to resolve a real identity.
      names[uid] = cleanString(u.display_name || u.username || uid, MAX_STRING) || uid;
    }
  }
  if (!memberIds.size) {
    errors.push("sleeper returned no users for that league");
  }

  const strangers = value.managers
    .map((m) => m.manager_id)
    .filter((id) => !memberIds.has(id));
  if (strangers.length) {
    errors.push(
      `${strangers.length} submitted manager id(s) are not members of that league`
    );
  } else {
    notes.push(`all ${value.managers.length} manager id(s) are league members`);
  }

  const renamed = value.managers.filter(
    (m) => names[m.manager_id] && names[m.manager_id] !== m.claimed_name
  );
  if (renamed.length) {
    // Not an error. People rename themselves, and the browser may have read
    // an older season's name. The STORED name is Sleeper's, always.
    notes.push(`${renamed.length} display name(s) taken from sleeper, not the payload`);
  }

  // ---- rosters -----------------------------------------------------------
  if (!Array.isArray(rosters)) {
    errors.push("sleeper returned no rosters for that league");
  } else {
    if (rosters.length !== value.league.n_teams) {
      errors.push(`claimed ${value.league.n_teams} rosters, sleeper returned ${rosters.length}`);
    }
    const owners = new Set(
      rosters.map((r) => (r && r.owner_id ? String(r.owner_id) : null)).filter(Boolean)
    );
    const unowned = value.managers.filter((m) => !owners.has(m.manager_id));
    if (unowned.length) {
      errors.push(`${unowned.length} submitted manager(s) own no roster in that league`);
    } else {
      notes.push("every submitted manager owns a roster");
    }
  }

  return {
    ok: errors.length === 0,
    external,
    notes,
    errors,
    transient: false,
    names,
    sleeperName: cleanString(league.name, MAX_STRING) || value.league.name,
  };
}

// ===========================================================================
// Stage 4 — aggregation (pure)
// ===========================================================================

/* Aggregate per-league, per-manager rows into a cross-league leaderboard.
 *
 * This is a direct port of dynasty.crossleague.aggregate(). The two are
 * pinned against each other on shared fixtures by tests/test_corpus_backend.py
 * — the board must be the same metric wherever it is computed.
 *
 * `rows` is the flat join of league_managers x leagues:
 *   { manager_id, display_name, league_id, league_name, season, n_teams,
 *     league_index, league_rank, draft_n, draft_z, ... }
 */
function aggregateCorpus(rows, opts) {
  opts = opts || {};
  const minDraftPicks = opts.minDraftPicks == null
    ? MIN_DRAFT_PICKS_FOR_BOARD : opts.minDraftPicks;

  const acc = new Map();
  for (const r of rows || []) {
    const mid = String(r.manager_id);
    let a = acc.get(mid);
    if (!a) {
      a = {
        manager_id: mid,
        display_name: r.display_name || mid,
        components: { draft: { n: 0, wz: 0 }, trade: { n: 0, wz: 0 }, waiver: { n: 0, wz: 0 } },
        leagues: [],
      };
      acc.set(mid, a);
    }
    if (r.display_name) a.display_name = r.display_name;

    const perLeague = {};
    for (const c of COMPONENTS) {
      const n = Number(r[`${c}_n`]) || 0;
      const z = Number(r[`${c}_z`]) || 0;
      if (n > 0) {
        a.components[c].n += n;
        a.components[c].wz += n * z;
      }
      perLeague[c] = {
        n,
        z: round(z, 6),
        mean: round(Number(r[`${c}_mean`]) || 0, 2),
        shrunk: round(Number(r[`${c}_shrunk`]) || 0, 2),
      };
    }

    let flags = [];
    try { flags = JSON.parse(r.flags || "[]"); } catch (_e) { flags = []; }

    a.leagues.push({
      league_id: String(r.league_id),
      name: r.league_name || String(r.league_id),
      season: r.season,
      n_teams: r.n_teams,
      index: round(Number(r.league_index) || 0, 1),
      rank: r.league_rank,
      components: perLeague,
      flags: Array.isArray(flags) ? flags : [],
    });
  }

  const all = Array.from(acc.values());
  const live = COMPONENTS.filter(
    (c) => all.filter((a) => a.components[c].n > 0).length >= 2
  );
  let wsum = 0;
  for (const c of live) wsum += MS_WEIGHTS[c];
  const effective = {};
  for (const c of COMPONENTS) {
    effective[c] = (wsum > 0 && live.includes(c)) ? MS_WEIGHTS[c] / wsum : 0;
  }

  const out = [];
  for (const a of all) {
    const comps = {};
    let composite = 0;
    for (const c of COMPONENTS) {
      const n = a.components[c].n;
      const zbar = n ? a.components[c].wz / n : 0;
      const zShrunk = msShrink(zbar, n, MS_SHRINK_K[c]);
      comps[c] = {
        n,
        zbar: round(zbar, 6),
        z: round(zShrunk, 6),
        available: n > 0,
        weight: round(effective[c], 6),
      };
      composite += effective[c] * zShrunk;
    }

    const flags = [];
    for (const c of COMPONENTS) {
      if (effective[c] > 0 && a.components[c].n === 0) {
        flags.push(`no ${c} activity in any indexed league — scored as corpus average`);
      }
    }
    const nDraft = comps.draft.n;
    if (nDraft > 0 && nDraft < MS_SHRINK_K.draft) {
      flags.push(
        `only ${nDraft} scored picks across all indexed leagues — heavily shrunk`
      );
    }

    const leagues = a.leagues.slice().sort((x, y) => {
      const sx = String(x.season || ""), sy = String(y.season || "");
      if (sx !== sy) return sx < sy ? 1 : -1;
      const nx = String(x.name || ""), ny = String(y.name || "");
      if (nx !== ny) return nx < ny ? 1 : -1;
      return 0;
    });

    out.push({
      manager_id: a.manager_id,
      display_name: a.display_name,
      composite: round(composite, 6),
      cross_index: round(MS_INDEX_CENTER + MS_INDEX_SCALE * composite, 1),
      n_leagues: leagues.length,
      components: comps,
      flags,
      leagues,
    });
  }

  out.sort((a, b) => {
    if (b.cross_index !== a.cross_index) return b.cross_index - a.cross_index;
    const an = a.display_name.toLowerCase(), bn = b.display_name.toLowerCase();
    return an < bn ? -1 : (an > bn ? 1 : 0);
  });
  const n = out.length;
  out.forEach((r, i) => {
    r.rank = i + 1;
    r.percentile = n ? round(100 * (n - i) / n, 1) : null;
  });

  const eligible = out.filter((r) => r.components.draft.n >= minDraftPicks);
  eligible.sort((a, b) => {
    if (b.components.draft.z !== a.components.draft.z) {
      return b.components.draft.z - a.components.draft.z;
    }
    const an = a.display_name.toLowerCase(), bn = b.display_name.toLowerCase();
    return an < bn ? -1 : (an > bn ? 1 : 0);
  });
  const dn = eligible.length;
  const draftBoard = eligible.map((r, i) => ({
    rank: i + 1,
    manager_id: r.manager_id,
    display_name: r.display_name,
    draft_z: r.components.draft.z,
    draft_zbar: r.components.draft.zbar,
    n_picks: r.components.draft.n,
    n_leagues: r.n_leagues,
    percentile: dn ? round(100 * (dn - i) / dn, 1) : null,
  }));

  const weightsOut = {};
  for (const c of COMPONENTS) weightsOut[c] = round(effective[c], 6);

  return {
    managers: out,
    draft_board: draftBoard,
    components: {
      live,
      weights: weightsOut,
      shrink_k: { ...MS_SHRINK_K },
      min_draft_picks_for_board: minDraftPicks,
    },
  };
}

// ===========================================================================
// D1 access
// ===========================================================================

function requireDb(env) {
  if (!env || !env.CORPUS_DB) {
    const e = new Error("corpus_backend_not_configured");
    e.userMessage =
      "This worker has no D1 binding. The corpus backend is inert until the " +
      "owner creates the database and redeploys — see docs/CORPUS-BACKEND.md.";
    throw e;
  }
  return env.CORPUS_DB;
}

/* One batched read that answers every gate at once: per-IP hourly, per-IP
 * daily, global daily, and the dedupe window for this league. */
async function readGates(db, ipHash, leagueId, now) {
  const hourKey = `ip:${ipHash}:h:${utcHourStamp(now)}`;
  const dayKey = `ip:${ipHash}:d:${utcDayStamp(now)}`;
  const globalKey = `global:d:${utcDayStamp(now)}`;

  const res = await db.batch([
    db.prepare(
      "SELECT bucket_key, count FROM rate_counters WHERE bucket_key IN (?, ?, ?)"
    ).bind(hourKey, dayKey, globalKey),
    db.prepare(
      "SELECT league_id, status, last_submitted_at, submission_count " +
      "FROM leagues WHERE league_id = ?"
    ).bind(leagueId),
  ]);

  const counts = { hour: 0, day: 0, global: 0 };
  for (const row of (res[0] && res[0].results) || []) {
    if (row.bucket_key === hourKey) counts.hour = Number(row.count) || 0;
    else if (row.bucket_key === dayKey) counts.day = Number(row.count) || 0;
    else if (row.bucket_key === globalKey) counts.global = Number(row.count) || 0;
  }
  const existing = ((res[1] && res[1].results) || [])[0] || null;
  return { counts, existing, keys: { hourKey, dayKey, globalKey } };
}

/* Decide whether this submission is allowed through. Pure given the gate
 * read, so the whole rate-limit policy is testable without a database. */
function evaluateGates(gates, now) {
  const { counts, existing } = gates;

  if (counts.global >= GLOBAL_DAILY_CAP) {
    return {
      allow: false, status: 503, outcome: "rejected_global_cap",
      reason: `global daily cap of ${GLOBAL_DAILY_CAP} submissions reached`,
      retryAfter: 3600,
    };
  }
  if (counts.hour >= RATE_IP_HOURLY) {
    return {
      allow: false, status: 429, outcome: "rejected_rate",
      reason: `more than ${RATE_IP_HOURLY} submissions from this address this hour`,
      retryAfter: 900,
    };
  }
  if (counts.day >= RATE_IP_DAILY) {
    return {
      allow: false, status: 429, outcome: "rejected_rate",
      reason: `more than ${RATE_IP_DAILY} submissions from this address today`,
      retryAfter: 3600,
    };
  }
  if (existing && existing.last_submitted_at) {
    const last = Date.parse(existing.last_submitted_at);
    if (isFinite(last) && (now.getTime() - last) < DEDUPE_WINDOW_SECONDS * 1000) {
      // Not an error: re-submitting a league we already have is the normal
      // consequence of someone re-running the page. Answer with the state we
      // hold and spend no upstream calls.
      return {
        allow: false, status: 200, outcome: "duplicate",
        reason: "already submitted recently",
        duplicate: true,
        state: existing.status,
      };
    }
  }
  return { allow: true };
}

function bumpCounters(db, keys, now) {
  const hourExp = new Date(now.getTime() + 2 * 3600 * 1000).toISOString();
  const dayExp = new Date(now.getTime() + 48 * 3600 * 1000).toISOString();
  const up = (key, exp) => db.prepare(
    "INSERT INTO rate_counters (bucket_key, count, expires_at) VALUES (?, 1, ?) " +
    "ON CONFLICT(bucket_key) DO UPDATE SET count = count + 1"
  ).bind(key, exp);
  return [
    up(keys.hourKey, hourExp),
    up(keys.dayKey, dayExp),
    up(keys.globalKey, dayExp),
  ];
}

/* Build every write for an accepted (or stored-unverified) submission.
 * Returned as statements so the caller can run them in ONE batch. */
function buildPersistStatements(db, value, verdict, nowIso) {
  const stmts = [];
  const L = value.league;
  const status = verdict.verified ? "provisional" : "unverified";

  stmts.push(db.prepare(
    "INSERT INTO leagues (league_id, lineage_root, name, season, n_teams, " +
    " n_seasons_scored, status, verified, verify_notes, first_submitted_at, " +
    " last_submitted_at, submission_count, source) " +
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'client') " +
    "ON CONFLICT(league_id) DO UPDATE SET " +
    " name = excluded.name, season = excluded.season, n_teams = excluded.n_teams," +
    " n_seasons_scored = excluded.n_seasons_scored," +
    // A league already CONFIRMED by the daily job is not demoted by a new
    // client submission. Client data never overwrites server-confirmed state.
    " status = CASE WHEN leagues.status = 'confirmed' THEN 'confirmed' " +
    "               ELSE excluded.status END," +
    " verified = excluded.verified, verify_notes = excluded.verify_notes," +
    " last_submitted_at = excluded.last_submitted_at," +
    " submission_count = leagues.submission_count + 1"
  ).bind(
    L.league_id, L.lineage_root, verdict.leagueName || L.name, L.season,
    L.n_teams, L.n_seasons_scored, status, verdict.verified ? 1 : 0,
    JSON.stringify(verdict.notes || []), nowIso, nowIso
  ));

  for (const m of value.managers) {
    const name = (verdict.names && verdict.names[m.manager_id]) || m.claimed_name;
    stmts.push(db.prepare(
      "INSERT INTO managers (manager_id, display_name, first_seen_at, last_seen_at) " +
      "VALUES (?, ?, ?, ?) " +
      "ON CONFLICT(manager_id) DO UPDATE SET " +
      " display_name = excluded.display_name, last_seen_at = excluded.last_seen_at"
    ).bind(m.manager_id, name, nowIso, nowIso));
  }

  for (const m of value.managers) {
    const c = m.components;
    stmts.push(db.prepare(
      "INSERT INTO league_managers (league_id, manager_id, league_index, league_rank," +
      " composite, draft_n, draft_z, draft_mean, draft_shrunk," +
      " trade_n, trade_z, trade_mean, trade_shrunk," +
      " waiver_n, waiver_z, waiver_mean, waiver_shrunk, flags, updated_at) " +
      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) " +
      "ON CONFLICT(league_id, manager_id) DO UPDATE SET " +
      " league_index = excluded.league_index, league_rank = excluded.league_rank," +
      " composite = excluded.composite," +
      " draft_n = excluded.draft_n, draft_z = excluded.draft_z," +
      " draft_mean = excluded.draft_mean, draft_shrunk = excluded.draft_shrunk," +
      " trade_n = excluded.trade_n, trade_z = excluded.trade_z," +
      " trade_mean = excluded.trade_mean, trade_shrunk = excluded.trade_shrunk," +
      " waiver_n = excluded.waiver_n, waiver_z = excluded.waiver_z," +
      " waiver_mean = excluded.waiver_mean, waiver_shrunk = excluded.waiver_shrunk," +
      " flags = excluded.flags, updated_at = excluded.updated_at"
    ).bind(
      L.league_id, m.manager_id, m.index, m.rank, m.composite,
      c.draft.n, c.draft.z, c.draft.mean, c.draft.shrunk,
      c.trade.n, c.trade.z, c.trade.mean, c.trade.shrunk,
      c.waiver.n, c.waiver.z, c.waiver.mean, c.waiver.shrunk,
      JSON.stringify(m.flags || []), nowIso
    ));
  }

  return stmts;
}

function auditStatement(db, db_row) {
  return db.prepare(
    "INSERT INTO submissions (league_id, submitted_at, ip_hash, origin, " +
    " payload_bytes, n_managers, schema_version, outcome, reason, " +
    " external_subrequests) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
  ).bind(
    db_row.league_id, db_row.submitted_at, db_row.ip_hash, db_row.origin,
    db_row.payload_bytes, db_row.n_managers, db_row.schema_version,
    db_row.outcome, db_row.reason, db_row.external_subrequests
  );
}

// ===========================================================================
// Handlers
// ===========================================================================

async function handleSubmit(request, env, originHeader) {
  const now = new Date();
  const nowIso = now.toISOString();
  const db = requireDb(env);

  // -- origin. Strict here, unlike the read proxy: a browser POST that can
  //    cause upstream work and a DB write is not something we serve to
  //    arbitrary sites.
  if (!ALLOWED_ORIGINS.includes(originHeader)) {
    return jsonResponse({
      error: "origin_not_allowed",
      message: "Submissions are accepted only from the published site.",
    }, 403, originHeader);
  }

  const ip = request.headers.get("CF-Connecting-IP") || "";
  const ipHash = await hashIp(ip, env.IP_HASH_SALT, now);

  // -- size, before reading the body.
  const declared = Number(request.headers.get("Content-Length") || 0);
  if (declared > MAX_BODY_BYTES) {
    await db.batch([auditStatement(db, {
      league_id: null, submitted_at: nowIso, ip_hash: ipHash, origin: originHeader,
      payload_bytes: declared, n_managers: null, schema_version: null,
      outcome: "rejected_size", reason: `content-length ${declared}`,
      external_subrequests: 0,
    })]).catch(() => {});
    return jsonResponse({
      error: "payload_too_large",
      message: `Maximum submission size is ${MAX_BODY_BYTES} bytes.`,
    }, 413, originHeader);
  }

  const raw = await request.text();
  const bytes = new TextEncoder().encode(raw).length;
  // Content-Length can lie; the body is the authority.
  if (bytes > MAX_BODY_BYTES) {
    return jsonResponse({
      error: "payload_too_large",
      message: `Maximum submission size is ${MAX_BODY_BYTES} bytes.`,
    }, 413, originHeader);
  }

  let payload;
  try {
    payload = JSON.parse(raw);
  } catch (_e) {
    return jsonResponse({ error: "invalid_json" }, 400, originHeader);
  }

  const parsed = validateSubmission(payload);
  const auditBase = {
    league_id: parsed.value ? parsed.value.league.league_id : null,
    submitted_at: nowIso, ip_hash: ipHash, origin: originHeader,
    payload_bytes: bytes,
    n_managers: parsed.value ? parsed.value.managers.length : null,
    schema_version: typeof payload.schema === "string"
      ? payload.schema.slice(0, 64) : null,
    external_subrequests: 0,
  };

  if (!parsed.ok) {
    await db.batch([auditStatement(db, {
      ...auditBase, outcome: "rejected_schema",
      reason: parsed.errors.slice(0, 3).join("; ").slice(0, 500),
    })]).catch(() => {});
    return jsonResponse({
      state: "rejected", reason: "schema", errors: parsed.errors.slice(0, 10),
    }, 400, originHeader);
  }

  const value = parsed.value;

  // -- rate limits and dedupe, BEFORE any upstream call. This ordering is the
  //    amplification defence: a blocked caller costs us one D1 read and zero
  //    Sleeper traffic.
  const gates = await readGates(db, ipHash, value.league.league_id, now);
  const decision = evaluateGates(gates, now);
  if (!decision.allow) {
    await db.batch([auditStatement(db, {
      ...auditBase, outcome: decision.outcome, reason: decision.reason,
    })]).catch(() => {});
    if (decision.duplicate) {
      return jsonResponse({
        state: decision.state || "submitted",
        league_id: value.league.league_id,
        duplicate: true,
        message: "This league was submitted recently; its current state is above.",
      }, 200, originHeader);
    }
    return jsonResponse({
      state: "rejected", reason: decision.outcome, message: decision.reason,
    }, decision.status, originHeader,
      decision.retryAfter ? { "Retry-After": String(decision.retryAfter) } : {});
  }

  // -- internal consistency (no network).
  const consistency = checkConsistency(value);
  if (!consistency.ok) {
    await db.batch([
      auditStatement(db, {
        ...auditBase, outcome: "rejected_consistency",
        reason: consistency.errors.slice(0, 3).join("; ").slice(0, 500),
      }),
      ...bumpCounters(db, gates.keys, now),
    ]).catch(() => {});
    return jsonResponse({
      state: "rejected", reason: "inconsistent",
      message: "The submitted scores are not internally consistent with the " +
               "scoring model.",
      errors: consistency.errors.slice(0, 10),
    }, 422, originHeader);
  }

  // -- verification against Sleeper (3 external subrequests).
  const verify = await verifyAgainstSleeper(value);
  auditBase.external_subrequests = verify.external;

  const verdict = {
    verified: verify.ok,
    notes: verify.ok ? verify.notes : verify.notes.concat(verify.errors),
    names: verify.names,
    leagueName: verify.sleeperName,
  };

  const stmts = [
    ...buildPersistStatements(db, value, verdict, nowIso),
    auditStatement(db, {
      ...auditBase,
      outcome: verify.ok ? "accepted" : "rejected_verify",
      reason: verify.ok ? null : verify.errors.slice(0, 3).join("; ").slice(0, 500),
    }),
    ...bumpCounters(db, gates.keys, now),
    // Opportunistic cleanup; keeps the counter table from growing forever
    // without needing a cron trigger.
    db.prepare("DELETE FROM rate_counters WHERE expires_at < ?").bind(nowIso),
  ];
  await db.batch(stmts);

  if (!verify.ok) {
    // Stored, but unverified and excluded from every public board until the
    // daily authoritative re-score says otherwise.
    return jsonResponse({
      state: "rejected",
      reason: verify.transient ? "upstream_unavailable" : "verification_failed",
      league_id: value.league.league_id,
      errors: verify.errors.slice(0, 10),
      message: verify.transient
        ? "Sleeper did not answer, so this could not be verified. It is stored " +
          "as unverified and the nightly job will re-check it."
        : "This submission could not be verified against Sleeper. It is stored " +
          "as unverified and excluded from the public leaderboard.",
    }, verify.transient ? 503 : 422, originHeader);
  }

  return jsonResponse({
    state: "provisional",
    league_id: value.league.league_id,
    n_managers: value.managers.length,
    verified: true,
    verification: verify.notes,
    message:
      "Verified against Sleeper and indexed as PROVISIONAL. It joins the " +
      "public cross-league board once the nightly job re-scores the league " +
      "from Sleeper itself and agrees with these numbers.",
  }, 202, originHeader);
}

async function handleLeaderboard(request, env, originHeader) {
  const db = requireDb(env);
  const url = new URL(request.url);

  const includeProvisional = url.searchParams.get("include") === "provisional";
  const statuses = includeProvisional
    ? ["confirmed", "provisional"]
    : ["confirmed"];

  let limit = parseInt(url.searchParams.get("limit") || "100", 10);
  if (!isFinite(limit) || limit < 1) limit = 100;
  limit = Math.min(limit, LEADERBOARD_MAX_LIMIT);

  const placeholders = statuses.map(() => "?").join(", ");
  const res = await db.prepare(
    "SELECT lm.manager_id, m.display_name, lm.league_id, l.name AS league_name," +
    " l.season, l.n_teams, l.status, lm.league_index, lm.league_rank," +
    " lm.draft_n, lm.draft_z, lm.draft_mean, lm.draft_shrunk," +
    " lm.trade_n, lm.trade_z, lm.trade_mean, lm.trade_shrunk," +
    " lm.waiver_n, lm.waiver_z, lm.waiver_mean, lm.waiver_shrunk, lm.flags " +
    "FROM league_managers lm " +
    " JOIN leagues l  ON l.league_id  = lm.league_id " +
    " JOIN managers m ON m.manager_id = lm.manager_id " +
    `WHERE l.status IN (${placeholders}) ` +
    "LIMIT ?"
  ).bind(...statuses, LEADERBOARD_ROW_CAP).all();

  const rows = (res && res.results) || [];
  const agg = aggregateCorpus(rows);

  const leagueIds = new Set(rows.map((r) => r.league_id));
  const confirmedIds = new Set(
    rows.filter((r) => r.status === "confirmed").map((r) => r.league_id)
  );

  return jsonResponse({
    schema: "dfm.corpus.leaderboard.v1",
    generated_at: new Date().toISOString(),
    // The distinction the owner asked to be surfaced, in the payload itself
    // so the page cannot render it away.
    trust: {
      included_statuses: statuses,
      n_leagues_total: leagueIds.size,
      n_leagues_confirmed: confirmedIds.size,
      n_leagues_provisional: leagueIds.size - confirmedIds.size,
      note: includeProvisional
        ? "Includes PROVISIONAL leagues: client-submitted, verified against " +
          "Sleeper by the worker, but not yet re-scored server-side."
        : "Server-confirmed leagues only. Client-submitted leagues appear " +
          "here after the nightly job re-scores them and agrees.",
    },
    coverage: {
      n_leagues: leagueIds.size,
      n_managers: agg.managers.length,
      row_cap_hit: rows.length >= LEADERBOARD_ROW_CAP,
    },
    components: agg.components,
    leaderboard: agg.managers.slice(0, limit),
    draft_board: agg.draft_board.slice(0, limit),
  }, 200, originHeader, {
    "Cache-Control": "public, max-age=120",
  });
}

async function handleLeagueStatus(leagueId, env, originHeader) {
  const db = requireDb(env);
  if (!isSleeperId(leagueId)) {
    return jsonResponse({ error: "bad_league_id" }, 400, originHeader);
  }
  const row = await db.prepare(
    "SELECT league_id, name, season, n_teams, status, verified, verify_notes," +
    " first_submitted_at, last_submitted_at, submission_count, reconciled_at," +
    " reconcile_notes FROM leagues WHERE league_id = ?"
  ).bind(leagueId).first();

  if (!row) {
    return jsonResponse({ state: "not_indexed", league_id: leagueId }, 404, originHeader);
  }
  let notes = [];
  try { notes = JSON.parse(row.verify_notes || "[]"); } catch (_e) { notes = []; }

  return jsonResponse({
    state: row.status,
    league_id: row.league_id,
    name: row.name,
    season: row.season,
    n_teams: row.n_teams,
    verified: !!row.verified,
    verification: notes,
    first_submitted_at: row.first_submitted_at,
    last_submitted_at: row.last_submitted_at,
    submission_count: row.submission_count,
    reconciled_at: row.reconciled_at,
    reconcile_notes: row.reconcile_notes,
    public: row.status === "confirmed",
  }, 200, originHeader);
}

async function handleStats(env, originHeader) {
  const db = requireDb(env);
  const res = await db.batch([
    db.prepare(
      "SELECT status, COUNT(*) AS n FROM leagues GROUP BY status"
    ),
    db.prepare("SELECT COUNT(*) AS n FROM managers"),
  ]);
  const byStatus = {};
  for (const r of (res[0] && res[0].results) || []) byStatus[r.status] = Number(r.n);
  const nManagers = Number((((res[1] && res[1].results) || [])[0] || {}).n || 0);

  return jsonResponse({
    schema: "dfm.corpus.stats.v1",
    leagues: byStatus,
    n_managers: nManagers,
    limits: {
      max_body_bytes: MAX_BODY_BYTES,
      max_managers: MAX_MANAGERS,
      per_ip_hourly: RATE_IP_HOURLY,
      per_ip_daily: RATE_IP_DAILY,
      global_daily_cap: GLOBAL_DAILY_CAP,
      dedupe_window_seconds: DEDUPE_WINDOW_SECONDS,
    },
    external_subrequests_per_submission: 3,
  }, 200, originHeader, { "Cache-Control": "public, max-age=300" });
}

function authorised(request, env) {
  const token = env && env.RECONCILE_TOKEN;
  if (!token) return false;
  const header = request.headers.get("Authorization") || "";
  const m = header.match(/^Bearer\s+(.+)$/);
  return !!m && safeEqual(m[1], token);
}

async function handlePending(request, env, originHeader) {
  if (!authorised(request, env)) {
    return jsonResponse({ error: "unauthorised" }, 401, originHeader);
  }
  const db = requireDb(env);
  const url = new URL(request.url);
  let limit = parseInt(url.searchParams.get("limit") || "50", 10);
  if (!isFinite(limit) || limit < 1) limit = 50;
  limit = Math.min(limit, PENDING_MAX_LIMIT);

  const res = await db.prepare(
    "SELECT league_id, name, season, n_teams, status, verified, " +
    " last_submitted_at, reconciled_at FROM leagues " +
    "WHERE status IN ('provisional', 'unverified') " +
    "ORDER BY (reconciled_at IS NOT NULL), last_submitted_at ASC LIMIT ?"
  ).bind(limit).all();

  const pending = (res && res.results) || [];

  // The job needs the numbers it is reconciling AGAINST, or it can only
  // overwrite rather than compare -- and "client-submitted data is
  // provisional until server-confirmed" would be unfalsifiable. Ship the
  // stored per-manager index alongside each pending league.
  if (pending.length) {
    const ids = pending.map((p) => p.league_id);
    const holders = ids.map(() => "?").join(", ");
    const scoreRes = await db.prepare(
      "SELECT league_id, manager_id, league_index FROM league_managers " +
      `WHERE league_id IN (${holders})`
    ).bind(...ids).all();
    const byLeague = new Map();
    for (const r of (scoreRes && scoreRes.results) || []) {
      if (!byLeague.has(r.league_id)) byLeague.set(r.league_id, {});
      byLeague.get(r.league_id)[r.manager_id] = Number(r.league_index);
    }
    for (const p of pending) p.submitted_index = byLeague.get(p.league_id) || {};
  }

  return jsonResponse({
    schema: "dfm.corpus.pending.v1",
    generated_at: new Date().toISOString(),
    pending,
  }, 200, originHeader);
}

/* The daily job's authoritative verdict.
 *
 * The job re-scores a league from Sleeper with the SAME page scorer (via
 * scripts/js/score_league_harness.js) and reports whether it agrees. Only
 * this endpoint can move a league to 'confirmed', and only a holder of
 * RECONCILE_TOKEN can call it.
 */
async function handleReconcile(request, env, originHeader) {
  if (!authorised(request, env)) {
    return jsonResponse({ error: "unauthorised" }, 401, originHeader);
  }
  const db = requireDb(env);

  const declared = Number(request.headers.get("Content-Length") || 0);
  if (declared > MAX_BODY_BYTES * 8) {
    return jsonResponse({ error: "payload_too_large" }, 413, originHeader);
  }
  let payload;
  try {
    payload = JSON.parse(await request.text());
  } catch (_e) {
    return jsonResponse({ error: "invalid_json" }, 400, originHeader);
  }
  if (!payload || payload.schema !== "dfm.corpus.reconcile.v1"
      || !Array.isArray(payload.results)) {
    return jsonResponse({ error: "bad_schema" }, 400, originHeader);
  }

  const runId = cleanString(payload.run_id, 64) || `run-${Date.now()}`;
  const nowIso = new Date().toISOString();
  const stmts = [];
  const applied = [];

  for (const r of payload.results.slice(0, 200)) {
    if (!r || !isSleeperId(String(r.league_id || ""))) continue;
    const verdict = r.verdict;
    if (!["confirmed", "rejected"].includes(verdict)) continue;

    // The authoritative re-score may also carry corrected per-manager rows.
    // When it does, they replace the client's numbers outright.
    if (verdict === "confirmed" && r.managers) {
      const parsed = validateSubmission({
        schema: SUBMISSION_SCHEMA,
        league: r.league || { league_id: r.league_id, name: r.name,
                              season: r.season, n_teams: r.n_teams },
        managers: r.managers,
      });
      if (parsed.ok) {
        const names = {};
        for (const m of parsed.value.managers) names[m.manager_id] = m.claimed_name;
        stmts.push(...buildPersistStatements(
          db, parsed.value,
          { verified: true, notes: ["re-scored by the daily job"], names },
          nowIso
        ));
      }
    }

    stmts.push(db.prepare(
      "UPDATE leagues SET status = ?, verified = ?, source = 'daily_job'," +
      " reconciled_at = ?, reconcile_run_id = ?, reconcile_notes = ? " +
      "WHERE league_id = ?"
    ).bind(
      verdict, verdict === "confirmed" ? 1 : 0, nowIso, runId,
      cleanString(r.note, 500) || null, String(r.league_id)
    ));
    applied.push({ league_id: String(r.league_id), verdict });
  }

  if (stmts.length) await db.batch(stmts);

  return jsonResponse({
    schema: "dfm.corpus.reconcile.result.v1",
    run_id: runId, applied, n_applied: applied.length,
  }, 200, originHeader);
}

// ===========================================================================
// Read proxy (unchanged)
// ===========================================================================

async function proxyJson(targetUrl, request) {
  const originHeader = request.headers.get("Origin") || "";
  const cacheKey = new Request(targetUrl, { method: "GET" });
  const cache = caches.default;

  let cached = await cache.match(cacheKey);
  if (cached) {
    // Re-emit with our CORS headers (the cached response may not have them
    // attached if it was stored as the upstream raw response).
    const body = await cached.text();
    return new Response(body, {
      status: cached.status,
      headers: {
        "Content-Type": "application/json",
        "X-Proxy-Cache": "HIT",
        ...corsHeaders(originHeader),
      },
    });
  }

  let upstream;
  try {
    upstream = await fetch(targetUrl, {
      headers: { "User-Agent": "dynasty-model-proxy/1.0" },
      redirect: "follow",
    });
  } catch (err) {
    return jsonResponse({ error: "upstream_fetch_failed", message: String(err) }, 502, originHeader);
  }

  const contentType = upstream.headers.get("Content-Type") || "";
  const body = await upstream.text();

  // Cache successful JSON responses.
  if (upstream.ok && contentType.includes("json")) {
    const cacheable = new Response(body, {
      status: upstream.status,
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": `public, max-age=${CACHE_TTL_SECONDS}`,
      },
    });
    await cache.put(cacheKey, cacheable.clone());
  }

  return new Response(body, {
    status: upstream.status,
    headers: {
      "Content-Type": contentType || "application/json",
      "X-Proxy-Cache": "MISS",
      ...corsHeaders(originHeader),
    },
  });
}

// ===========================================================================
// Router
// ===========================================================================

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const originHeader = request.headers.get("Origin") || "";

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(originHeader) });
    }

    try {
      // ---- corpus backend ------------------------------------------------
      if (url.pathname === "/corpus/submit") {
        if (request.method !== "POST") {
          return jsonResponse({ error: "method_not_allowed" }, 405, originHeader);
        }
        return await handleSubmit(request, env, originHeader);
      }
      if (url.pathname === "/corpus/reconcile") {
        if (request.method !== "POST") {
          return jsonResponse({ error: "method_not_allowed" }, 405, originHeader);
        }
        return await handleReconcile(request, env, originHeader);
      }
      if (url.pathname === "/corpus/leaderboard" && request.method === "GET") {
        return await handleLeaderboard(request, env, originHeader);
      }
      if (url.pathname === "/corpus/stats" && request.method === "GET") {
        return await handleStats(env, originHeader);
      }
      if (url.pathname === "/corpus/pending" && request.method === "GET") {
        return await handlePending(request, env, originHeader);
      }
      const statusMatch = url.pathname.match(/^\/corpus\/league\/([0-9]{6,24})$/);
      if (statusMatch && request.method === "GET") {
        return await handleLeagueStatus(statusMatch[1], env, originHeader);
      }
    } catch (err) {
      if (err && err.message === "corpus_backend_not_configured") {
        return jsonResponse({
          error: "corpus_backend_not_configured", message: err.userMessage,
        }, 501, originHeader);
      }
      return jsonResponse({
        error: "internal_error", message: String(err && err.message || err),
      }, 500, originHeader);
    }

    // ---- read proxy (GET only) -------------------------------------------
    if (request.method !== "GET") {
      return jsonResponse({ error: "method_not_allowed" }, 405, originHeader);
    }

    if (url.pathname === "/health") {
      return jsonResponse({
        status: "ok",
        time: new Date().toISOString(),
        corpus: (env && env.CORPUS_DB) ? "enabled" : "disabled",
      }, 200, originHeader);
    }

    // /mfl/<year>/export?TYPE=...&L=...&JSON=1
    const mflMatch = url.pathname.match(/^\/mfl\/(\d{4})\/export$/);
    if (mflMatch) {
      const year = mflMatch[1];
      const params = url.searchParams.toString();
      const target = `${MFL_HOST}/${year}/export?${params}`;
      return proxyJson(target, request);
    }

    // /sleeper/v1/<rest...>
    if (url.pathname.startsWith("/sleeper/v1/")) {
      const rest = url.pathname.slice("/sleeper/".length);
      const params = url.searchParams.toString();
      const target = `${SLEEPER_HOST}/${rest}${params ? "?" + params : ""}`;
      return proxyJson(target, request);
    }

    return jsonResponse({
      error: "not_found",
      hint: "GET /mfl/<year>/export?TYPE=...&L=... or /sleeper/v1/... or " +
            "POST /corpus/submit, GET /corpus/leaderboard",
    }, 404, originHeader);
  },
};

// Exported for tests/test_corpus_backend.py, which runs them under node.
// Nothing here touches the network or the database.
export const __test = {
  validateSubmission,
  checkConsistency,
  verifyAgainstSleeper,
  aggregateCorpus,
  evaluateGates,
  msShrink,
  isSleeperId,
  cleanString,
  constants: {
    SUBMISSION_SCHEMA, COMPONENTS, MS_WEIGHTS, MS_SHRINK_K,
    MS_INDEX_CENTER, MS_INDEX_SCALE, MIN_DRAFT_PICKS_FOR_BOARD,
    MAX_BODY_BYTES, MAX_MANAGERS, MIN_MANAGERS, MAX_LINEAGE_IDS,
    RATE_IP_HOURLY, RATE_IP_DAILY, GLOBAL_DAILY_CAP, DEDUPE_WINDOW_SECONDS,
    ALLOWED_ORIGINS,
  },
};
