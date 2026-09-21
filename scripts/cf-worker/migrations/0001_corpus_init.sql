-- Cross-league corpus backend — initial schema.
--
-- Applied with:
--   cd scripts/cf-worker
--   npx wrangler d1 execute dynasty-corpus --remote --file=migrations/0001_corpus_init.sql
--
-- Design notes that are load-bearing, not decoration:
--
--  * Every row that a browser can cause to exist carries a `status` and a
--    `verified` flag. NOTHING client-submitted is public. The leaderboard
--    view reads `status = 'confirmed'` only, and only the daily GitHub
--    Action (which re-scores from Sleeper authoritatively) may set that.
--    A browser can at best get a league to 'provisional'.
--
--  * `submissions` is append-only and records REJECTED attempts too. It is
--    the abuse audit trail: without the rejects you cannot tell a quiet day
--    from a blocked attack.
--
--  * Only Sleeper's pseudonymous `display_name` is stored for a person, and
--    it is written from what SLEEPER returned during verification, never
--    from the submitted payload. See docs/CORPUS-BACKEND.md §privacy.
--
--  * Client IPs are never stored. `ip_hash` is SHA-256 over
--    (IP + IP_HASH_SALT + UTC date), so it is usable for same-day rate
--    limiting and useless as an identifier afterwards.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- leagues

CREATE TABLE IF NOT EXISTS leagues (
  league_id           TEXT PRIMARY KEY,
  lineage_root        TEXT,
  name                TEXT NOT NULL,
  season              TEXT NOT NULL,
  n_teams             INTEGER NOT NULL,
  n_seasons_scored    INTEGER NOT NULL DEFAULT 1,

  -- 'unverified' : stored, failed worker verification, never public
  -- 'provisional': passed worker verification, awaiting authoritative re-score
  -- 'confirmed'  : the daily job re-scored it from Sleeper and agreed
  -- 'rejected'   : the daily job re-scored it and DISAGREED
  status            TEXT NOT NULL DEFAULT 'unverified'
                      CHECK (status IN ('unverified','provisional','confirmed','rejected')),
  verified          INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0,1)),

  verify_notes        TEXT NOT NULL DEFAULT '[]',   -- JSON array of check results
  first_submitted_at  TEXT NOT NULL,
  last_submitted_at   TEXT NOT NULL,
  submission_count    INTEGER NOT NULL DEFAULT 1,

  reconciled_at       TEXT,
  reconcile_run_id    TEXT,
  reconcile_notes     TEXT,

  -- 'client'    : a browser submitted this
  -- 'daily_job' : the authoritative crawl created or replaced it
  source              TEXT NOT NULL DEFAULT 'client'
                        CHECK (source IN ('client','daily_job'))
);

CREATE INDEX IF NOT EXISTS idx_leagues_status   ON leagues(status);
CREATE INDEX IF NOT EXISTS idx_leagues_lineage  ON leagues(lineage_root);
CREATE INDEX IF NOT EXISTS idx_leagues_pending  ON leagues(status, last_submitted_at);

-- --------------------------------------------------------------- managers

CREATE TABLE IF NOT EXISTS managers (
  -- Sleeper user_id. Synthetic 'roster:<league>:<n>' ids for unowned rosters
  -- are refused at the worker: they cannot be verified against /league/users
  -- and they are not people.
  manager_id    TEXT PRIMARY KEY,
  display_name  TEXT NOT NULL,        -- from Sleeper at verification time
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL
);

-- ------------------------------------------------- per-manager, per-league

CREATE TABLE IF NOT EXISTS league_managers (
  league_id     TEXT NOT NULL REFERENCES leagues(league_id)   ON DELETE CASCADE,
  manager_id    TEXT NOT NULL REFERENCES managers(manager_id) ON DELETE CASCADE,

  league_index  REAL NOT NULL,        -- msScoreLeague() index, 100 +/- 15
  league_rank   INTEGER,
  composite     REAL NOT NULL DEFAULT 0,

  draft_n       INTEGER NOT NULL DEFAULT 0,
  draft_z       REAL    NOT NULL DEFAULT 0,
  draft_mean    REAL    NOT NULL DEFAULT 0,
  draft_shrunk  REAL    NOT NULL DEFAULT 0,

  trade_n       INTEGER NOT NULL DEFAULT 0,
  trade_z       REAL    NOT NULL DEFAULT 0,
  trade_mean    REAL    NOT NULL DEFAULT 0,
  trade_shrunk  REAL    NOT NULL DEFAULT 0,

  waiver_n      INTEGER NOT NULL DEFAULT 0,
  waiver_z      REAL    NOT NULL DEFAULT 0,
  waiver_mean   REAL    NOT NULL DEFAULT 0,
  waiver_shrunk REAL    NOT NULL DEFAULT 0,

  flags         TEXT NOT NULL DEFAULT '[]',
  updated_at    TEXT NOT NULL,

  PRIMARY KEY (league_id, manager_id)
);

CREATE INDEX IF NOT EXISTS idx_lm_manager ON league_managers(manager_id);

-- ------------------------------------------------------ submission audit

CREATE TABLE IF NOT EXISTS submissions (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  league_id      TEXT,                -- null when the payload had no usable id
  submitted_at   TEXT NOT NULL,
  ip_hash        TEXT NOT NULL,       -- salted + date-scoped, see header
  origin         TEXT,
  payload_bytes  INTEGER NOT NULL DEFAULT 0,
  n_managers     INTEGER,
  schema_version TEXT,

  -- accepted | rejected_size | rejected_schema | rejected_consistency
  -- | rejected_verify | rejected_rate | rejected_global_cap | rejected_origin
  -- | duplicate
  outcome        TEXT NOT NULL,
  reason         TEXT,
  external_subrequests INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sub_time    ON submissions(submitted_at);
CREATE INDEX IF NOT EXISTS idx_sub_outcome ON submissions(outcome, submitted_at);

-- --------------------------------------------------------- rate limiting

-- One row per (bucket, window). Buckets:
--   'ip:<hash>:h:<YYYY-MM-DDTHH>'  per-IP hourly
--   'ip:<hash>:d:<YYYY-MM-DD>'     per-IP daily
--   'global:d:<YYYY-MM-DD>'        global daily cap
CREATE TABLE IF NOT EXISTS rate_counters (
  bucket_key TEXT PRIMARY KEY,
  count      INTEGER NOT NULL DEFAULT 0,
  expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rate_expiry ON rate_counters(expires_at);

-- ------------------------------------------------------------ bookkeeping

CREATE TABLE IF NOT EXISTS corpus_meta (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

INSERT OR IGNORE INTO corpus_meta (key, value, updated_at)
VALUES ('schema_version', '1', '1970-01-01T00:00:00Z');
