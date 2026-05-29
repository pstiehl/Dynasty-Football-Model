"""Engine entry point — v2.0 fantasy-point-arc methodology.

NOTE: the module name remains ``similarity_v1`` for back-compat with all
existing callers (``format_overlay``, ``report``, sources, tests). The
IMPLEMENTATION is the v2.0 fantasy-point-arc engine; the v1.x per-stat
z-score machinery has been removed.

Pipeline:

    1.  Load player careers from nflverse season CSV.
    2.  Classify each player as ``long_arc`` (retired before
        ``LONG_ARC_THROUGH_SEASON``, or 8+ NFL seasons, or 33+ years old
        with 6+ seasons).
    3.  Build a corpus-derived era-pace table (era 1→4, 2→4, 3→4
        multipliers per (position, stat)).
    4.  Build a FANTASY-POINT ARC for every player under every supported
        scoring format (sf_ppr, 1qb_ppr, 2qb_ppr, half_ppr, std,
        sf_te_premium). Each season's stat line is era-pace-adjusted
        BEFORE scoring, so every fp value in the arc is in
        modern-fp-equivalent units.
    5.  Build a per-(position, career_stage) percentile table from the
        long-arc corpus' career-total fp.
    6.  For each ACTIVE player, build their 10-dim arc vector at the
        current age, cosine-match against same-position long-arc players
        in an age ±1 / career-stage ±1 window, take top-20 by cosine.
    7.  Project each comp's realised post-age fantasy points
        (modern-fp-equivalent), discount 5%/yr, similarity-weight, sum.
    8.  For QBs, apply v1.1's career-length era lift on
        projected_remaining_seasons AND projected_remaining_fp (mobile /
        dual-threat only; pocket lift = 1.0). This is the ONE piece of
        v1.x machinery v2.0 keeps — modern medicine extends mobile-QB
        careers and the long-arc comp pool is still half-retired-from-
        era-3 dual-threats.

No z-scoring. No style cohort. No per-stat-shape vectors. The 10-dim
vector lives in raw fantasy-point space.
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .era_pace import (
    ERA_BOUNDS,
    EraPaceTable,
    FALLBACK_MULTIPLIERS,
    era_for_season,
    fallback_table,
    load_empirical_table,
)
from .career_length_era import (
    CareerLengthEraTable,
    STYLE_DUAL_THREAT,
    STYLE_MOBILE,
    STYLE_POCKET,
    apply_lift,
    build_career_length_era_table,
    career_rushing_rate,
    style_for_career,
)
from .fantasy_arc import (
    apply_retired_early_flag,
    load_retired_early_overrides,
    CareerArc,
    SUPPORTED_FORMATS,
    build_career_arc,
    persist_corpus,
)
from .fantasy_arc_similarity import (
    AGE_WINDOW,
    BASE_FORMAT,
    CAREER_STAGE_WINDOW,
    CompMatch,
    CareerStagePercentileTable,
    DISCOUNT_PER_YEAR,
    MIN_GAMES_PER_SEASON,
    TOP_K_COMPS,
    build_arc_vector,
    build_career_stage_percentile_table,
    find_comps as arc_find_comps,
    project_remaining as arc_project_remaining,
    project_player as arc_project_player,
    _proven_production_floor,
    _recent_1yr_target,
    _recent_2yr_target,
    _recent_3yr_target,
)
from .v2_2_penalties import (
    SURVIVAL_BUST_AGE,
    SURVIVAL_BUST_MAX_SEASONS,
    apply_penalty_stack,
    compute_confidence,
    compute_late_breakout,
    compute_missed_recent_season,
    compute_position_tier_baselines,
    compute_survival,
)
from .rookie_nfl_fp_arc import (
    FULL_CONFIDENCE_GAMES,
    POSITION_ENCODING as ROOKIE_POSITIONS,
    RookieCompMatch,
    RookieProfile,
    RookieProjectionResult,
    build_rookie_corpus,
    project_rookie,
)
from ..scoring_rules import LEAGUE_SCORING

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATA_ROOT = Path("data/nflverse")
OUT_ROOT = Path("data/engine_v1")
OUT_ROOT.mkdir(parents=True, exist_ok=True)
OUT_ROOT_V2 = Path("data/engine_v2")
OUT_ROOT_V2.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# v2.4: pre-1999 corpus extension (feature-flagged)
# ---------------------------------------------------------------------------
#
# When ``USE_PRE1999_CORPUS=True`` (env var or call-site override), the
# corpus loader concatenates ``player_stats_season_pre1999.csv.gz``
# (1980-1998, scraped from Pro-Football-Reference in PR 1) with the
# canonical 1999+ nflverse file. Pre-1999 rows use ``player_id`` of the
# form ``pfr_SmitEm00``. Where a row's ``pfr_id`` suffix maps to a
# gsis_id (via the nflverse PFR↔gsis crosswalk in ``players.csv.gz``),
# the loader rewrites the ``player_id`` to that gsis_id so crossover
# players (Emmitt Smith, Jerry Rice, Brett Favre, ...) stitch into ONE
# continuous career arc instead of two short ones.
#
# The flag defaults to False for PR 2 — the unification logic is built,
# tested, and ready to flip in PR 4 after the validation snapshot.
PRE1999_STATS_FILENAME = "player_stats_season_pre1999.csv.gz"
PRE1999_PLAYERS_SIDECAR_FILENAME = "players_pre1999.csv.gz"
PRE1999_BIRTH_DATES_PATH = Path("data/pfr_birth_dates.csv")


def _pre1999_enabled(override: Optional[bool] = None) -> bool:
    """Resolve the ``USE_PRE1999_CORPUS`` feature flag.

    Precedence:
      1. Explicit ``override`` argument (highest priority — used by tests).
      2. ``USE_PRE1999_CORPUS`` environment variable (truthy = 1/true/yes/on;
         falsey = 0/false/no/off; anything else falls through to default).
      3. Default: True since v2.4 PR 4. Pre-1999 legends (Payton, Smith,
         Allen, etc.) are now part of the comp pool by default. Set
         ``USE_PRE1999_CORPUS=false`` to opt back into the 1999+-only
         behaviour.
    """
    if override is not None:
        return bool(override)
    raw = os.environ.get("USE_PRE1999_CORPUS", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return True

# Long-arc corpus thresholds — UNCHANGED from v1.1.
RETIRED_THROUGH_SEASON = 2022
LONG_ARC_THROUGH_SEASON = 2022
LONG_ARC_MIN_SEASONS = 8
LONG_ARC_VETERAN_AGE = 33
LONG_ARC_VETERAN_SEASONS = 6

SKILL_POSITIONS: Tuple[str, ...] = ("QB", "RB", "WR", "TE")

# v3.8 (Phil 2026-05-29) — post-stack comp-pool career-arc floor for
# rookies. After the v2.2 penalty stack runs (survival, confidence,
# late-breakout, missed-season), rookie projections cannot fall below
# POST_STACK_COMP_POOL_FRACTION × median(top-5 non-bust comp post-rookie
# career fp). This is the HARD floor that ensures Jeanty (LT-tier comps)
# and Judkins (Knowshon-tier comps) don't get crushed below their comp
# pool's realised career arc just because RBs as a position have high
# bust rates. Set conservatively (0.40) so it only bites for rookies
# whose top-5 NON-BUST comps clear ~1,500 career fp — about 600 fp floor.
POST_STACK_COMP_POOL_FRACTION = 0.40
# Only fire the post-stack comp-pool floor when survival is below this
# threshold. Above 0.60 the v2.2 stack is operating in calibrated band
# and we don't want to flatten projections with a comp-pool override.
FLOOR_TRIGGER_SURVIVAL = 0.60
# Top-N similarity comps to consider when building the floor anchor.
# Restricting to top-similarity comps prevents low-sim outlier non-bust
# comps from anchoring a bust-profile rookie's floor (Shedeur Sanders →
# Matt Ryan/McNabb tail).
POST_STACK_FLOOR_TOPSIM_N = 10

# Fantasy scoring (sf_ppr default) — used only as the "default scoring"
# parameter callers expect. The real scoring lives in scoring_rules.LEAGUE_SCORING.
DEFAULT_SCORING = dict(LEAGUE_SCORING[BASE_FORMAT])


# ---------------------------------------------------------------------------
# Data containers (kept for back-compat with format_overlay + tests)
# ---------------------------------------------------------------------------

@dataclass
class PlayerSeason:
    player_id: str
    season: int
    age: int
    position: str
    games: int
    stats: Dict[str, float]
    fantasy_points_ppr: float

    @property
    def era(self) -> int:
        return era_for_season(self.season)


@dataclass
class PlayerCareer:
    player_id: str
    name: str
    position: str
    birth_year: Optional[int]
    rookie_season: Optional[int]
    last_season: Optional[int]
    seasons: List[PlayerSeason] = field(default_factory=list)
    # Full birth date when available (YYYY-MM-DD from nflverse, which agrees
    # with Pro-Football-Reference). Used for displayed/current age. The
    # season-grained ``birth_year`` is kept for engine internals (comping,
    # cohort windows) and for players whose meta row lacks a full date.
    birth_date: Optional[date] = None

    def is_retired(self, through: int = RETIRED_THROUGH_SEASON) -> bool:
        return self.last_season is not None and self.last_season <= through

    def is_long_arc(
        self,
        through: int = LONG_ARC_THROUGH_SEASON,
        min_seasons: int = LONG_ARC_MIN_SEASONS,
        veteran_age: int = LONG_ARC_VETERAN_AGE,
        veteran_seasons: int = LONG_ARC_VETERAN_SEASONS,
    ) -> bool:
        if self.is_retired(through=through):
            return True
        n_seasons = len(self.seasons)
        if n_seasons >= min_seasons:
            return True
        last_age = 0
        for s in self.seasons:
            if s.age is not None and s.age > last_age:
                last_age = s.age
        if last_age >= veteran_age and n_seasons >= veteran_seasons:
            return True
        return False

    def career_total(self, stat: str) -> float:
        return float(sum(s.stats.get(stat, 0.0) for s in self.seasons))

    def career_ppr(self) -> float:
        return float(sum(s.fantasy_points_ppr for s in self.seasons))

    def seasons_through_age(self, age_cap: int) -> List[PlayerSeason]:
        return [s for s in self.seasons if s.age is not None and s.age <= age_cap]

    def seasons_after_age(self, age_floor: int) -> List[PlayerSeason]:
        return [s for s in self.seasons if s.age is not None and s.age > age_floor]

    def with_completed_seasons_only(self, through_season: int) -> "PlayerCareer":
        kept = [s for s in self.seasons if s.season <= through_season]
        last = kept[-1].season if kept else None
        return PlayerCareer(
            player_id=self.player_id,
            name=self.name,
            position=self.position,
            birth_year=self.birth_year,
            rookie_season=self.rookie_season,
            last_season=last,
            seasons=kept,
            birth_date=self.birth_date,
        )

    def current_age(self, as_of: Optional[date] = None) -> Optional[int]:
        """Return the player's age as of ``as_of`` (default: today).

        Matches Pro-Football-Reference's displayed age semantics:
        whole years between birth_date and the reference date. Falls back
        to ``as_of.year - birth_year`` (year-grained) when only the
        birth year is known.
        """
        ref = as_of or date.today()
        bd = self.birth_date
        if bd is not None:
            years = ref.year - bd.year
            if (ref.month, ref.day) < (bd.month, bd.day):
                years -= 1
            return years
        if self.birth_year is not None:
            return ref.year - self.birth_year
        return None


# ---------------------------------------------------------------------------
# Loaders (unchanged from v1.x)
# ---------------------------------------------------------------------------

def _load_players_meta(
    path: Path,
    sidecar_path: Optional[Path] = None,
    birth_date_overrides_path: Optional[Path] = None,
) -> Dict[str, Dict[str, str]]:
    """Load nflverse ``players.csv.gz`` keyed by ``gsis_id``.

    v2.4 additions:
      * Concatenates an optional ``sidecar_path`` (e.g.
        ``players_pre1999.csv.gz``) onto the main file. Rows from the
        sidecar use the same column schema; their ``gsis_id`` is a
        synthetic ``pfr_X`` token matching the corpus ``player_id``.
      * Also indexes every row by ``pfr_id`` under a synthetic
        ``pfr_{pfr_id}`` key, so the corpus loader can resolve
        ``player_id = pfr_SmitEm00`` rows to their nflverse meta
        (birth_date, rookie_season, last_season) without rewriting the
        id. PFR-only retirees like Walter Payton are reachable this way.
      * Optionally overlays ``birth_date`` from
        ``birth_date_overrides_path`` (CSV with ``pfr_id, birth_date``)
        when the nflverse row is missing one or carries a different
        value. Partial-file friendly — it reads whatever rows are
        present, doesn't block on completeness.
    """
    out: Dict[str, Dict[str, str]] = {}

    def _ingest(fh):
        r = csv.DictReader(fh)
        for row in r:
            gid = row.get("gsis_id") or ""
            if not gid:
                continue
            existing = out.get(gid)
            pos = (row.get("position") or "").upper()
            if existing is None or pos in SKILL_POSITIONS:
                out[gid] = row

    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        _ingest(fh)

    if sidecar_path is not None and sidecar_path.exists():
        with gzip.open(sidecar_path, "rt", encoding="utf-8", newline="") as fh:
            _ingest(fh)

    # Optional birth-date overlay (PFR scrape). Honoured per-pfr_id;
    # only fills in when the nflverse row is missing one or empty.
    if birth_date_overrides_path is not None and birth_date_overrides_path.exists():
        try:
            with open(birth_date_overrides_path, "rt", encoding="utf-8", newline="") as fh:
                r = csv.DictReader(fh)
                bd_overrides: Dict[str, str] = {}
                for row in r:
                    pfr = (row.get("pfr_id") or "").strip()
                    bd = (row.get("birth_date") or "").strip()
                    if pfr and bd:
                        bd_overrides[pfr] = bd
            if bd_overrides:
                for meta in out.values():
                    pfr = (meta.get("pfr_id") or "").strip()
                    if not pfr:
                        continue
                    existing_bd = (meta.get("birth_date") or "").strip()
                    if existing_bd and len(existing_bd) >= 10:
                        continue  # nflverse already has a full date
                    if pfr in bd_overrides:
                        meta["birth_date"] = bd_overrides[pfr]
        except OSError:
            pass

    # Second pass: also expose meta under ``pfr_{pfr_id}`` synthetic keys
    # so the corpus loader can resolve PFR-keyed ``player_id`` values
    # without rewriting them. We don't overwrite an existing real
    # ``pfr_X`` key (sidecar rows already use that as their gsis_id).
    for gid, meta in list(out.items()):
        pfr = (meta.get("pfr_id") or "").strip()
        if not pfr:
            continue
        synth = f"pfr_{pfr}"
        if synth not in out:
            out[synth] = meta

    return out


def _birth_year(meta_row: Optional[Dict[str, str]]) -> Optional[int]:
    if not meta_row:
        return None
    bd = (meta_row.get("birth_date") or "").strip()
    if not bd or len(bd) < 4:
        return None
    try:
        return int(bd[:4])
    except ValueError:
        return None


def _birth_date(meta_row: Optional[Dict[str, str]]) -> Optional[date]:
    """Parse the full birth_date from a nflverse meta row.

    Returns ``None`` if the row is missing, the field is blank, or the
    format is not ``YYYY-MM-DD``. Used to compute current age on a
    day-precision basis (matches Pro-Football-Reference).
    """
    if not meta_row:
        return None
    raw = (meta_row.get("birth_date") or "").strip()
    if not raw or len(raw) < 10:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _age_for_season(birth_year: Optional[int], season: int) -> Optional[int]:
    if birth_year is None:
        return None
    return season - birth_year


def _safe_float(v) -> float:
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _build_pfr_to_gsis_map(meta: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    """Build a ``pfr_id -> gsis_id`` mapping from a loaded meta dict.

    Only includes rows whose ``gsis_id`` is a real nflverse id (starts
    with ``00-`` or is otherwise not a synthetic ``pfr_X`` placeholder),
    AND whose ``pfr_id`` is non-empty. The sidecar's synthetic gsis_ids
    (``pfr_AndeKe00`` style) are skipped — those rows aren't stitched
    because they never appear in the post-1999 file by definition.
    """
    out: Dict[str, str] = {}
    for gid, row in meta.items():
        # Skip the synthetic ``pfr_X`` reverse-lookup keys we added.
        if gid.startswith("pfr_"):
            continue
        pfr = (row.get("pfr_id") or "").strip()
        real_gid = (row.get("gsis_id") or "").strip()
        if pfr and real_gid:
            out[pfr] = real_gid
    return out


def _iter_stat_rows(
    stats_path: Path,
    pre1999_stats_path: Optional[Path],
    pfr_to_gsis: Dict[str, str],
) -> Tuple[Iterable[Dict[str, str]], int]:
    """Yield stat rows from the canonical 1999+ file, optionally followed
    by stitched rows from the pre-1999 file.

    Crossover stitching is RESTRICTED to true crossover players — those
    whose gsis_id (resolved via the nflverse PFR↔gsis crosswalk) ALSO
    appears in the post-1999 stat file. Players whose gsis_id never
    shows up in the post-1999 file (Walter Payton, Earl Campbell, etc.
    — they retired before 1999) keep their ``pfr_X`` ``player_id`` so
    their pre-1999 arc stays its own thing.

    Why restrict it: only crossover players gain from id rewriting. For
    a strictly pre-1999 retiree, rewriting ``pfr_PaytWa00`` to the
    synthetic gsis_id ``PAY738296`` would change the corpus key for
    something that adds no new connections — and noise the
    ``test_walter_payton_no_crossover`` invariant.

    Returns (sorted_rows, stitched_count). The stitched_count is
    reported in summary logs so anyone running this can see how many
    crossover players got merged.
    """
    rows: List[Dict[str, str]] = []

    post_ids: set[str] = set()
    with gzip.open(stats_path, "rt", encoding="utf-8", newline="") as fh:
        r = csv.DictReader(fh)
        for row in r:
            rows.append(row)
            pid = row.get("player_id") or ""
            if pid:
                post_ids.add(pid)

    stitched = 0
    stitched_players: set[str] = set()
    if pre1999_stats_path is not None and pre1999_stats_path.exists():
        with gzip.open(pre1999_stats_path, "rt", encoding="utf-8", newline="") as fh:
            r = csv.DictReader(fh)
            for row in r:
                pid = row.get("player_id") or ""
                if pid.startswith("pfr_"):
                    pfr_suffix = pid[4:]
                    gid = pfr_to_gsis.get(pfr_suffix)
                    if gid and gid in post_ids:
                        # Stitch: rewrite to gsis_id so this row joins the
                        # crossover player's post-1999 career arc.
                        row["player_id"] = gid
                        stitched += 1
                        stitched_players.add(gid)
                rows.append(row)

    rows.sort(key=lambda r: (r.get("player_id") or "", int(r.get("season") or 0)))
    return rows, len(stitched_players)


def load_unified_player_stats(
    stats_path: Path = DATA_ROOT / "player_stats_season.csv.gz",
    players_path: Path = DATA_ROOT / "players.csv.gz",
    use_pre1999: Optional[bool] = None,
    pre1999_stats_path: Optional[Path] = None,
    pre1999_players_sidecar_path: Optional[Path] = None,
    birth_date_overrides_path: Optional[Path] = None,
) -> Tuple[List[Dict[str, str]], Dict[str, Dict[str, str]]]:
    """Return (stat_rows, meta) for the configured corpus.

    Single entry point for v2.4's unified corpus loading. Returns the
    list of stat rows (post-stitching) AND the merged player meta dict
    in one call so callers don't have to coordinate the two reads.

    Arguments:
      stats_path: canonical 1999+ nflverse stat file
      players_path: canonical nflverse players.csv.gz
      use_pre1999: override for the ``USE_PRE1999_CORPUS`` env flag
      pre1999_stats_path: optional override (defaults to the path next
        to ``stats_path``). Only consulted when the feature flag is on.
      pre1999_players_sidecar_path: optional override for the players
        sidecar; defaults to ``players_pre1999.csv.gz`` next to
        ``players_path``.
      birth_date_overrides_path: optional CSV with ``pfr_id, birth_date``
        rows to overlay onto nflverse meta entries. Defaults to
        ``data/pfr_birth_dates.csv`` when the flag is on.

    When the flag is OFF, behaves exactly as the 1.x loader: returns
    only 1999+ rows and the unmodified nflverse meta (still indexed by
    pfr_id for free — the index is harmless when no pre-1999 rows ask
    about it).
    """
    enabled = _pre1999_enabled(use_pre1999)

    if enabled:
        sidecar = pre1999_players_sidecar_path or (players_path.parent / PRE1999_PLAYERS_SIDECAR_FILENAME)
        bd_overrides = birth_date_overrides_path or PRE1999_BIRTH_DATES_PATH
        pre_stats = pre1999_stats_path or (stats_path.parent / PRE1999_STATS_FILENAME)
    else:
        sidecar = None
        bd_overrides = None
        pre_stats = None

    meta = _load_players_meta(
        players_path,
        sidecar_path=sidecar,
        birth_date_overrides_path=bd_overrides,
    )
    pfr_to_gsis = _build_pfr_to_gsis_map(meta)
    rows, _stitched_count = _iter_stat_rows(stats_path, pre_stats, pfr_to_gsis)
    return rows, meta


def load_corpus(
    stats_path: Path = DATA_ROOT / "player_stats_season.csv.gz",
    players_path: Path = DATA_ROOT / "players.csv.gz",
    use_pre1999: Optional[bool] = None,
) -> Dict[str, PlayerCareer]:
    rows, meta = load_unified_player_stats(
        stats_path=stats_path,
        players_path=players_path,
        use_pre1999=use_pre1999,
    )
    careers: Dict[str, PlayerCareer] = {}

    for row in rows:
        if (row.get("season_type") or "REG") != "REG":
            continue
        pid = row.get("player_id") or ""
        if not pid:
            continue
        position = (row.get("position") or "").upper()
        if position not in SKILL_POSITIONS:
            continue
        try:
            season = int(row.get("season") or 0)
        except ValueError:
            continue
        if season < 1980:
            continue
        games = int(_safe_float(row.get("games") or 0))
        if games < MIN_GAMES_PER_SEASON:
            continue

        stats = {
            "passing_yards":   _safe_float(row.get("passing_yards")),
            "passing_tds":     _safe_float(row.get("passing_tds")),
            "interceptions":   _safe_float(row.get("interceptions")),
            "rushing_yards":   _safe_float(row.get("rushing_yards")),
            "rushing_tds":     _safe_float(row.get("rushing_tds")),
            "receptions":      _safe_float(row.get("receptions")),
            "receiving_yards": _safe_float(row.get("receiving_yards")),
            "receiving_tds":   _safe_float(row.get("receiving_tds")),
            "games":           float(games),
        }
        fp_ppr = _safe_float(row.get("fantasy_points_ppr"))
        m = meta.get(pid)
        by = _birth_year(m)
        age = _age_for_season(by, season)
        if age is None:
            rs = None
            if m and (m.get("rookie_season") or "").isdigit():
                rs = int(m["rookie_season"])
            if rs is not None:
                age = 22 + (season - rs)
        if age is None:
            continue
        ps = PlayerSeason(
            player_id=pid,
            season=season,
            age=age,
            position=position,
            games=games,
            stats=stats,
            fantasy_points_ppr=fp_ppr,
        )

        c = careers.get(pid)
        if c is None:
            name = (
                (m.get("display_name") if m else None)
                or row.get("player_display_name")
                or row.get("player_name")
                or pid
            )
            rookie_season = None
            last_season = None
            if m:
                if (m.get("rookie_season") or "").isdigit():
                    rookie_season = int(m["rookie_season"])
                if (m.get("last_season") or "").isdigit():
                    last_season = int(m["last_season"])
            bdate = _birth_date(m)
            c = PlayerCareer(
                player_id=pid,
                name=name,
                position=position,
                birth_year=by,
                rookie_season=rookie_season,
                last_season=last_season,
                seasons=[],
                birth_date=bdate,
            )
            careers[pid] = c
        c.seasons.append(ps)

    for c in careers.values():
        c.seasons.sort(key=lambda s: s.season)
        if c.last_season is None and c.seasons:
            c.last_season = c.seasons[-1].season
        if c.rookie_season is None and c.seasons:
            c.rookie_season = c.seasons[0].season
        # v2.4: for stitched crossover players, recompute rookie_season
        # from the earliest season we actually observed. The nflverse meta
        # row for, e.g., Emmitt Smith says rookie_season=1990, which is
        # correct — but if the pre-1999 stitch goes wrong we want the
        # tests to surface it.
        if c.seasons:
            observed_first = c.seasons[0].season
            if c.rookie_season is None or observed_first < c.rookie_season:
                c.rookie_season = observed_first

    return careers


# ---------------------------------------------------------------------------
# Era-pace calibration (corpus-derived) — unchanged from v1.x
# ---------------------------------------------------------------------------

def build_era_pace_table(
    careers: Dict[str, PlayerCareer],
    *,
    use_pre1999: Optional[bool] = None,
    prefer_snapshot: bool = True,
) -> EraPaceTable:
    """Build the era-pace multiplier table.

    v2.4 behaviour:

      * When ``USE_PRE1999_CORPUS`` is on (or ``use_pre1999=True``) AND
        ``prefer_snapshot=True`` AND the JSON snapshot at
        ``EMPIRICAL_MULTIPLIERS_PATH`` exists, return the snapshotted
        empirical table. This makes era-pace changes reviewable in PRs
        (the JSON file is diffable) instead of silently drifting with
        the corpus.
      * Otherwise: derive multipliers from ``careers`` exactly as v1.x
        did. This path remains active for the flag-OFF default (so the
        v2.0+v2.3.5 calibration is byte-for-byte unchanged) and for any
        caller that wants a freshly-computed table (tests).

    For both paths, ``EraPaceTable.get`` falls back to FALLBACK_MULTIPLIERS
    cell-by-cell for any missing (position, stat, era) entry — the
    fallback table is the safety net, not a wholesale replacement.
    """
    enabled = _pre1999_enabled(use_pre1999)
    if enabled and prefer_snapshot:
        snapshot = load_empirical_table()
        if snapshot is not None:
            return snapshot

    samples: Dict[Tuple[str, str, int], List[float]] = defaultdict(list)
    for c in careers.values():
        for s in c.seasons:
            if s.games < MIN_GAMES_PER_SEASON:
                continue
            for stat in FALLBACK_MULTIPLIERS.get(c.position, {}).keys():
                per_game = s.stats.get(stat, 0.0) / max(s.games, 1)
                samples[(c.position, stat, s.era)].append(per_game)

    def median(xs: List[float]) -> Optional[float]:
        if not xs:
            return None
        sx = sorted(xs)
        m = len(sx) // 2
        if len(sx) % 2 == 1:
            return sx[m]
        return 0.5 * (sx[m - 1] + sx[m])

    mults: Dict[str, Dict[str, Dict[int, float]]] = {}
    for pos, stat_map in FALLBACK_MULTIPLIERS.items():
        mults.setdefault(pos, {})
        for stat in stat_map:
            mults[pos].setdefault(stat, {})
            med4 = median(samples.get((pos, stat, 4), []))
            for era_from in (1, 2, 3, 4):
                med_from = median(samples.get((pos, stat, era_from), []))
                if med4 is None or med_from is None or med_from <= 1e-6:
                    mults[pos][stat][era_from] = FALLBACK_MULTIPLIERS[pos][stat].get(era_from, 1.0)
                    continue
                ratio = med4 / med_from
                ratio = max(0.6, min(2.0, ratio))
                mults[pos][stat][era_from] = ratio
    return EraPaceTable(multipliers=mults, source="corpus")


# ---------------------------------------------------------------------------
# CareerArc construction
# ---------------------------------------------------------------------------

def _career_to_arc_seasons(career: PlayerCareer) -> List[Dict]:
    """Convert a PlayerCareer's seasons into the dict-of-dicts shape that
    ``fantasy_arc.build_career_arc`` expects."""
    out: List[Dict] = []
    for s in career.seasons:
        if s.games < MIN_GAMES_PER_SEASON:
            continue
        out.append({
            "season": s.season,
            "age": s.age,
            "games": s.games,
            "era": s.era,
            "stats": s.stats,
        })
    return out


def _build_arcs(
    careers: Iterable[PlayerCareer],
    pace: EraPaceTable,
    retired_early_overrides: Optional[Mapping[str, Dict]] = None,
) -> Dict[str, CareerArc]:
    """Build career arcs and apply the v3.8 retired-early flag.

    ``retired_early_overrides`` comes from
    ``fantasy_arc.load_retired_early_overrides`` (sidecar YAML). When None,
    the flag is not applied (back-compat).
    """
    arcs: Dict[str, CareerArc] = {}
    ovr = retired_early_overrides or {}
    for c in careers:
        seasons = _career_to_arc_seasons(c)
        if not seasons:
            continue
        retired = c.is_retired()
        is_long_arc = c.is_long_arc()
        arc = build_career_arc(
            player_id=c.player_id,
            name=c.name,
            position=c.position,
            last_season=c.last_season,
            rookie_season=c.rookie_season,
            retired=retired,
            is_long_arc=is_long_arc,
            seasons=seasons,
            pace=pace,
            formats=SUPPORTED_FORMATS,
        )
        if ovr:
            apply_retired_early_flag(arc, ovr)
        arcs[c.player_id] = arc
    return arcs


# ---------------------------------------------------------------------------
# Top-level engine
# ---------------------------------------------------------------------------

@dataclass
class EngineResult:
    rankings: List[Dict]
    comps: Dict[str, List[Dict]]
    era_pace: EraPaceTable
    careers: Dict[str, PlayerCareer]
    long_arc_corpus: List[PlayerCareer]
    active_players: List[PlayerCareer]
    career_length_era: Optional[CareerLengthEraTable] = None
    # v2.0 — fantasy-arc data
    arcs: Dict[str, CareerArc] = field(default_factory=dict)
    long_arc_arcs: List[CareerArc] = field(default_factory=list)
    # v3.2 — broad comp pool (long_arc_arcs + short-career arcs); the
    # set find_comps actually iterates over, with the career-stage gate
    # applied per-target.
    comp_pool_arcs: List[CareerArc] = field(default_factory=list)
    percentile_table: Optional[CareerStagePercentileTable] = None
    base_format: str = BASE_FORMAT
    # Removed-in-v2 fields kept as None for downstream readers that hardcode them.
    znorm: object = None
    cohort_index: object = None
    cohort_diag: object = None

    @property
    def retired_corpus(self) -> List[PlayerCareer]:
        return self.long_arc_corpus

    def as_player_dict(self, pid: str) -> Optional[Dict]:
        for row in self.rankings:
            if row["player_id"] == pid:
                return row
        return None


def _is_active(career: PlayerCareer, current_season: int) -> bool:
    if not career.seasons:
        return False
    if career.last_season is None:
        return False
    return career.last_season >= (current_season - 1)


def _comp_tier_label(top_comp: CareerArc, top_comp_career_fp: float) -> str:
    """Compact label like 'elite (Megatron)' or 'above-avg (Boldin)'.

    Tiers re-anchored to fp_total (sf_ppr) since v2.0 ranks by projected
    fantasy points, not PPR-only points.
    """
    tier = "deep"
    if top_comp_career_fp >= 1800:
        tier = "elite"
    elif top_comp_career_fp >= 1100:
        tier = "above-avg"
    elif top_comp_career_fp >= 600:
        tier = "starter"
    return f"{tier} ({top_comp.name})"


def _completed_nfl_seasons(career: PlayerCareer) -> int:
    """Number of completed NFL seasons (games >= MIN_GAMES_PER_SEASON).

    A "partial current season" (the in-progress year, or an injury-shortened
    rookie year < 4 games) is NOT counted as completed. This drives the v2.1
    cohort dispatcher: 1 = rookie engine, 2+ = v2.0 cumulative engine.

    Note: the v2.0 corpus loader (load_corpus) already filters seasons with
    games < MIN_GAMES_PER_SEASON=4, so every season on the career counts.
    Travis Hunter (7G) and Malik Nabers (4G in 2024) DO count as 1
    completed rookie season because they meet the 4-game floor.
    """
    return sum(1 for s in career.seasons if s.games >= MIN_GAMES_PER_SEASON)


def _comp_washed_out(
    *,
    final_age: Optional[int],
    seasons_played: int,
    last_season: Optional[int],
    current_season: int,
) -> bool:
    """True only when the comp is no longer in the NFL AND fits the bust
    profile (career ended by ``SURVIVAL_BUST_AGE`` with fewer than
    ``SURVIVAL_BUST_MAX_SEASONS`` NFL seasons).

    Phil's 2026-05-22 critique: previously every short-career comp got
    flagged, including active 1–3-year players like James Cook, Zach
    Charbonnet, and Roschon Johnson — wrong, they haven't washed out
    yet, they just haven't played long enough. Use the same
    "still on a roster within the last full season" rule the engine
    uses elsewhere (``_is_active``).
    """
    if last_season is None or last_season >= current_season - 1:
        # Still active (played in current_season or the prior season).
        return False
    if final_age is None:
        return False
    return (
        final_age <= SURVIVAL_BUST_AGE
        and seasons_played < SURVIVAL_BUST_MAX_SEASONS
    )


def _raw_stats_by_pid_season(careers: Dict[str, PlayerCareer]) -> Dict[Tuple[str, int], Dict[str, float]]:
    """Pid+season → raw stat-line dict. Used by rookie_nfl_fp_arc to read
    rookie-year per-category yards/TDs (the fp/G dimension comes from the
    era-pace-adjusted arc corpus, but the per-category vector dims come
    from raw stats).
    """
    out: Dict[Tuple[str, int], Dict[str, float]] = {}
    for c in careers.values():
        for s in c.seasons:
            out[(c.player_id, s.season)] = s.stats
    return out


def _rookie_comp_records(
    comps: Sequence[RookieCompMatch],
    league_format: str,
    *,
    current_season: int,
) -> List[Dict]:
    """Convert RookieCompMatch list to the comp-record dict shape that
    format_overlay + report.py expect. Sets snapshot_age = comp's
    rookie_age so that format_overlay's re-projection sums fp for
    seasons at age > rookie_age (i.e. year 2+).
    """
    records: List[Dict] = []
    for m in comps:
        arc = m.profile.arc
        # post-age stats projected from comp's year-2+
        pts = 0.0
        n_seasons = 0
        for s in arc.career_arc:
            if s.season <= m.profile.rookie_season:
                continue
            if s.games < MIN_GAMES_PER_SEASON:
                continue
            decay = (1.0 - DISCOUNT_PER_YEAR) ** n_seasons
            pts += s.fp_total.get(league_format, 0.0) * decay
            n_seasons += 1
        # Career-length classification used on the per-player comp table to
        # flag wash-outs (Phil's Bo Nix → Aaron Brooks complaint). The
        # rookie engine's corpus is full retired careers, so ``career_arc``
        # already encodes career length and final age.
        seasons_played = sum(
            1 for s in arc.career_arc if s.games >= MIN_GAMES_PER_SEASON
        )
        final_age = max(
            (s.age for s in arc.career_arc if s.games >= MIN_GAMES_PER_SEASON),
            default=None,
        )
        washed_out = _comp_washed_out(
            final_age=final_age,
            seasons_played=seasons_played,
            last_season=arc.last_season,
            current_season=current_season,
        )
        records.append({
            "player_id": arc.player_id,
            "name": arc.name,
            "position": arc.position,
            "last_season": arc.last_season,
            # ``similarity`` is the user-facing score: raw vector
            # similarity in (0, 1]. The ranking-internal similarity (with
            # breakout/recency bias) is preserved under
            # ``ranking_similarity`` for diagnostic transparency.
            "similarity": round(float(m.display_similarity or m.similarity), 4),
            "ranking_similarity": round(float(m.similarity), 4),
            "career_ppr": round(arc.career_total_fp.get(league_format, 0.0), 1),
            "post_age_projected_pts": round(pts, 1),
            "post_age_seasons": n_seasons,
            "seasons_played": seasons_played,
            "final_age": final_age,
            # ``washed_out`` flags comps whose career ended by age 30 with
            # fewer than 8 NFL seasons — the same "bust" definition the
            # survival multiplier uses. Surfaced on player pages so users
            # can see when a high-similarity comp is a journeyman who
            # didn't last (e.g. Bo Nix → Aaron Brooks).
            "washed_out": bool(washed_out),
            # snapshot_age = comp's rookie age so format_overlay re-projects
            # year-2+ correctly under any format.
            "snapshot_age": m.profile.rookie_age,
            "rookie_year": m.profile.rookie_season,
            "peak_3yr_fp_per_game": round(arc.peak_3yr_fp_per_game.get(league_format, 0.0), 2),
            "peak_season_fp_per_game": round(arc.peak_season_fp_per_game.get(league_format, 0.0), 2),
            "career_arc_fp_per_game": [
                {"age": s.age, "fp_per_game": round(s.fp_per_game.get(league_format, 0.0), 2)}
                for s in arc.career_arc
            ],
        })
    return records


def run_engine(
    current_season: int = 2025,
    retired_through: int = RETIRED_THROUGH_SEASON,
    scoring: Optional[Dict[str, float]] = None,
    top_k: int = TOP_K_COMPS,
    persist: bool = True,
) -> EngineResult:
    # NOTE: the ``scoring`` parameter is retained for API back-compat but is
    # not used by the v2.0 engine — the fantasy arc corpus is pre-computed
    # across SUPPORTED_FORMATS and the ranking always uses BASE_FORMAT.
    # Per-format ranks are produced downstream by format_overlay.
    careers = load_corpus()
    pace = build_era_pace_table(careers)

    # v1.1 long-arc corpus selection. Short-career retirees (Tim Tebow,
    # EJ Manuel, Desmond Ridder, Christian Ponder, Tyler Thigpen) are
    # KEPT IN the corpus deliberately — their presence as a comp is a
    # NEGATIVE signal about the target. Phil 2026-05-22 v2.3.3 (final):
    # "If you are being compared to a player like Aaron Brooks or
    # Desmond Ridder or Tim Tebow you should be heavily de-ranked for
    # that comparison. You are being compared to players who stopped
    # accumulating stats because teams stopped playing them."
    #
    # The survival_multiplier (see ``v2_2_penalties.compute_survival``)
    # is the mechanism that punishes the target for having wash-out
    # comps. Removing the busts from the corpus would have hidden
    # exactly the signal Phil wants amplified.
    # v3.5 (Phil 2026-05-28): EXCLUDE currently-active players from the
    # long-arc corpus and the broad comp pool. Comping Puka Nacua to
    # Ja'Marr Chase / CeeDee Lamb / Justin Jefferson / Amon-Ra St. Brown
    # systematically truncates Puka's projection because those comps'
    # "careers" are only what they've accumulated through season-N —
    # they're 25-26 with their best years still ahead. Phil's directive
    # (verbatim): "if their 'last season' is 2025 or 'the most recent
    # year of data' then that player should be omitted from the
    # similarity score or comparison."
    #
    # "Active" here = ``last_season >= current_season - 1`` (i.e. played
    # this year or last year). Anyone who actually retired (or hasn't
    # played in 2+ seasons) is fair game as a comp. This is
    # symmetrically more permissive than RETIRED_THROUGH_SEASON=2022,
    # because the corpus is constantly aging — we don't want to
    # permanently rule out 2023-and-earlier retirees just because the
    # hardcoded constant lags.
    def _is_active_for_comp(career: PlayerCareer) -> bool:
        if career.last_season is None:
            return False
        return career.last_season >= (current_season - 1)

    long_arc_corpus: List[PlayerCareer] = []
    for c in careers.values():
        if len(c.seasons) < 2:
            continue
        if _is_active_for_comp(c):
            continue  # v3.5 — actives never go into the comp pool
        if not c.is_long_arc(through=retired_through):
            continue
        long_arc_corpus.append(c)

    # v3.2 — BROAD comp pool (survivorship-bias fix). The "long-arc"
    # corpus above is used as the high-information anchor for the
    # percentile table and career-length-era multipliers (we want stable
    # 95th-percentile bands and only well-sampled careers feeding them).
    # The COMP POOL itself is broader: every skill-position career with
    # ≥2 completed seasons. ``find_comps`` then applies a career-stage
    # gate (comp must have at least max(target_n_seasons, 3) seasons)
    # so short-career arcs only show up against career-stage-matched
    # targets. Without this, ~18% of pro-football-reference skill-pos
    # careers (715 players, mostly the 2-7 season "didn't pan out" tier)
    # never appear as a comp — every active player's projection skews
    # optimistic because the bust outcomes are missing from their
    # comp distribution. Phil's flag, 2026-05.
    broad_comp_pool: List[PlayerCareer] = []
    for c in careers.values():
        if c.position not in SKILL_POSITIONS:
            continue
        if len(c.seasons) < 2:
            continue
        if _is_active_for_comp(c):
            continue  # v3.5 — actives never go into the comp pool
        broad_comp_pool.append(c)

    # Career-length era multipliers (corpus-derived). Keep this on the
    # long-arc set — short-career arcs would corrupt the mobile/dual
    # threat lift signal.
    career_length_era = build_career_length_era_table(
        long_arc_corpus, era_for_season,
    )

    # Build fantasy-point arcs for the entire corpus + actives.
    # v3.8 — load the retired-early sidecar so the matching arcs are
    # tagged at build time and the downstream projection paths can
    # extrapolate their truncated careers (Phil 2026-05-29).
    retired_early_overrides = load_retired_early_overrides()
    arcs = _build_arcs(careers.values(), pace, retired_early_overrides)
    # The long-arc-arc list. For long-arc-but-still-active members, we must
    # use a TRIMMED arc (completed seasons only). Build a parallel set keyed
    # by player_id → trimmed arc.
    long_arc_arcs: List[CareerArc] = []
    for c in long_arc_corpus:
        # ``c`` is already trimmed for active veterans in the loop above.
        seasons = _career_to_arc_seasons(c)
        if not seasons:
            continue
        arc = build_career_arc(
            player_id=c.player_id,
            name=c.name,
            position=c.position,
            last_season=c.last_season,
            rookie_season=c.rookie_season,
            retired=c.is_retired(through=retired_through),
            is_long_arc=True,
            seasons=seasons,
            pace=pace,
            formats=SUPPORTED_FORMATS,
        )
        if retired_early_overrides:
            apply_retired_early_flag(arc, retired_early_overrides)
        long_arc_arcs.append(arc)

    # v3.2 — build the BROAD comp-pool arcs. Includes long-arc arcs
    # plus the short-career (2-7 season) arcs that the v1.1 gate rejected.
    # Tagged with ``is_long_arc=False`` for the short-career ones so
    # downstream consumers can distinguish them (e.g. for diagnostics).
    long_arc_ids = {a.player_id for a in long_arc_arcs}
    comp_pool_arcs: List[CareerArc] = list(long_arc_arcs)
    for c in broad_comp_pool:
        if c.player_id in long_arc_ids:
            continue
        seasons = _career_to_arc_seasons(c)
        if not seasons:
            continue
        arc = build_career_arc(
            player_id=c.player_id,
            name=c.name,
            position=c.position,
            last_season=c.last_season,
            rookie_season=c.rookie_season,
            retired=c.is_retired(through=retired_through),
            is_long_arc=False,
            seasons=seasons,
            pace=pace,
            formats=SUPPORTED_FORMATS,
        )
        if retired_early_overrides:
            apply_retired_early_flag(arc, retired_early_overrides)
        comp_pool_arcs.append(arc)

    # Percentile table from long-arc corpus (UNCHANGED — keeps high-info
    # career-stage percentile bands; short-career arcs would dilute them).
    percentile_table = build_career_stage_percentile_table(
        long_arc_arcs, league_format=BASE_FORMAT,
    )

    active_players = [
        c for c in careers.values()
        if _is_active(c, current_season=current_season)
    ]

    # v2.1: build the historical rookie corpus from the FULL arc set
    # (long-arc/retired vets + active vets with completed rookie years).
    # Each entry is a player's actual rookie-year profile vector +
    # reference to their full v2.0 arc (for year-2+ projection).
    #
    # Exclude the current 2025 draft class from the corpus — we don't
    # want current 1-season rookies comping against each other (they
    # have no year-2+ realised career yet).
    raw_stats = _raw_stats_by_pid_season(careers)
    rookie_season_by_pid = {
        pid: c.rookie_season for pid, c in careers.items()
        if c.rookie_season is not None
    }
    rookie_corpus = build_rookie_corpus(
        arcs=arcs.values(),
        raw_stats_by_pid_season=raw_stats,
        rookie_season_by_pid=rookie_season_by_pid,
        league_format=BASE_FORMAT,
        exclude_rookie_seasons={current_season},
        # v3.7 (Phil 2026-05-28): apply the v3.5 retired-only mandate
        # to the rookie engine path too. Without this, J.J. McCarthy's
        # rookie-corpus comps were dominated by Lawrence / Tua / Lamar /
        # Fields / Purdy / Stafford / Maye / Zach Wilson / etc. — all
        # 2025-active QBs whose careers haven't played out. Phil's
        # mandate: "if their 'last season' is 2025 or 'the most recent
        # year of data' then that player should be omitted from the
        # similarity score or comparison."
        exclude_active_last_season_at_or_after=current_season - 1,
    )

    rankings: List[Dict] = []
    comps_map: Dict[str, List[Dict]] = {}
    CURRENT_ERA = 4

    # Reference date for the displayed "age" column. We match
    # Pro-Football-Reference's player-page semantics: whole years between
    # the player's birth_date and today. Used only for the report ``age``
    # field; ``last_season.age`` (year-of-season minus birth_year) is
    # still used for engine internals (comping, age-window selection).
    age_ref_date = date.today()

    for ap in active_players:
        target_arc = arcs.get(ap.player_id)
        if target_arc is None or not target_arc.career_arc:
            continue
        last_season = ap.seasons[-1]
        age_now = last_season.age
        # Current age for display (PFR-style). Falls back to ``age_now``
        # when birth metadata is unavailable.
        display_age = ap.current_age(as_of=age_ref_date)
        if display_age is None:
            display_age = age_now

        # ------------------------------------------------------------------
        # v2.1 cohort dispatcher — by completed NFL seasons.
        #   0 seasons: 2026 draft class (drafted, not yet played). EXCLUDED
        #              from main rankings; deferred to v2.2's college chain.
        #   1 season:  2025 draft class (one NFL season under their belt).
        #              Use the 1-NFL-season rookie engine (rookie_nfl_fp_arc).
        #   2+ seasons: 2024 class and earlier. Use the v2.0 cumulative-arc
        #              engine — their data is rich enough to comp against
        #              full-career retired veterans.
        # ------------------------------------------------------------------
        n_completed = _completed_nfl_seasons(ap)
        if n_completed == 0:
            # Should not happen — load_corpus already filters seasons with
            # games < MIN_GAMES_PER_SEASON, so any active player with at
            # least one row in careers has >= 1 completed season. Kept as
            # a safety net for future schema changes.
            continue

        # v2.1 rookie-engine eligibility: must have EXACTLY 1 completed
        # season AND that season must be the player's actual rookie
        # year (i.e. the draft class within the last ~2 NFL years). This
        # avoids routing perennial backups (Sam Howell: 17G in 2023,
        # benched 2024-25) into the rookie engine — their "1 completed
        # season" isn't a recent rookie campaign.
        is_recent_rookie = False
        if n_completed == 1:
            rookie_year = ap.rookie_season
            first_completed = target_arc.career_arc[0].season
            # Treat the player as a v2.1 rookie if EITHER:
            #   * their first completed season is the current or previous
            #     NFL season (most common case — 2025 draft class), OR
            #   * their actual draft year is within the last 2 seasons
            #     AND they only played 1 completed year (handles late
            #     bloomers who debuted in year 2 like Brock Purdy did,
            #     though Purdy played enough that he's not in this bucket).
            if first_completed >= current_season - 1:
                is_recent_rookie = True
            elif rookie_year is not None and rookie_year >= current_season - 1:
                is_recent_rookie = True

        if is_recent_rookie:
            # ---- v2.1 1-NFL-season rookie engine ----
            rookie_season = target_arc.career_arc[0]
            rookie_stats = raw_stats.get(
                (ap.player_id, rookie_season.season), {}
            )
            rproj = project_rookie(
                target_arc=target_arc,
                target_rookie_stats=rookie_stats,
                target_rookie_age=rookie_season.age,
                target_rookie_games=rookie_season.games,
                rookie_corpus=rookie_corpus,
                league_format=BASE_FORMAT,
                k=top_k,
            )
            if not rproj.comps:
                continue
            weighted_points = rproj.projected_year_2_plus_fp
            weighted_seasons = rproj.projected_year_2_plus_seasons

            # No career-length lift for rookies — too small a sample
            # to know if they're mobile/dual-threat (also, the comp pool
            # already implicitly captures style: a mobile rookie's comps
            # are mobile rookies).
            qb_style = STYLE_POCKET
            qb_rypg = 0.0
            lift_fp = 1.0
            lift_years = 1.0
            if ap.position == "QB":
                qb_style = style_for_career(ap)
                qb_rypg = career_rushing_rate(ap)

            top_comp = rproj.comps[0].profile.arc
            comp_records = _rookie_comp_records(
                rproj.comps, BASE_FORMAT, current_season=current_season,
            )
            rankings.append({
                "player_id": ap.player_id,
                "name": ap.name,
                "position": ap.position,
                # ``age`` is the player's CURRENT age (PFR-style, day-precision
                # via ``PlayerCareer.current_age``). ``last_season.age`` is
                # still used internally for comping/age windows.
                "age": display_age,
                "last_season": ap.last_season,
                "production_score": round(weighted_points, 1),
                "projected_years_remaining": round(weighted_seasons, 1),
                "top_comp": top_comp.name,
                "top_comp_id": top_comp.player_id,
                "comp_tier": _comp_tier_label(
                    top_comp, top_comp.career_total_fp.get(BASE_FORMAT, 0.0),
                ),
                "n_comps": len(rproj.comps),
                "qb_style": qb_style if ap.position == "QB" else None,
                "qb_career_rypg": round(qb_rypg, 1) if ap.position == "QB" else None,
                "career_length_lift": round(lift_years, 3),
                "career_length_lift_fp": round(lift_fp, 3),
                # v2.0 projection diagnostics — rookie engine has its own
                # comp-weighted vs peak-anchored split (peak-anchored =
                # rookie_rate × expected_horizon × discount).
                "comp_weighted_fp": round(rproj.comp_weighted_fp * rproj.confidence_factor, 1),
                "peak_anchored_fp": round(rproj.peak_anchored_fp * rproj.confidence_factor, 1),
                "projection_path": (
                    "rookie_peak_anchored"
                    if rproj.peak_anchored_fp > rproj.comp_weighted_fp
                    else "rookie_comp_weighted"
                ),
                # v2.1 rookie-engine fields
                "engine": "rookie_nfl_fp_arc",
                "rookie_year": rproj.rookie_year,
                "rookie_games": rproj.rookie_games,
                "rookie_fp_per_game": round(rproj.rookie_fp_per_game, 2),
                "rookie_confidence_factor": round(rproj.confidence_factor, 3),
                # v2.3.5: fraction of top-K rookie comps that washed out
                # after year 1 (no realised year-2+ season). High value
                # means the rookie's profile most resembles year-1-only
                # busts — confidence indicator complementing the v2.3.3
                # wash-out penalty.
                "bust_rate_in_comps": round(rproj.bust_rate_in_comps, 3),
                # Arc-summary fields for the UI (rookies have only 1 season,
                # so peak == single-season == career_avg).
                "peak_3yr_fp_per_game": round(target_arc.peak_3yr_fp_per_game.get(BASE_FORMAT, 0.0), 2),
                "peak_season_fp_per_game": round(target_arc.peak_season_fp_per_game.get(BASE_FORMAT, 0.0), 2),
                "career_avg_fp_per_game": round(target_arc.career_avg_fp_per_game.get(BASE_FORMAT, 0.0), 2),
                "career_total_fp_to_date": round(target_arc.career_total_fp.get(BASE_FORMAT, 0.0), 1),
                # cohort fields retained for downstream readers
                "style_cohort": None,
                "cohort_pool_size": None,
                "cohort_widened": None,
                "cohort_styles_used": None,
                # v2.2 tracking fields (consumed by the penalty
                # post-processing pass; removed before returning).
                "_v22_engine": "rookie",
                "_v22_target_arc": target_arc,
                "_v22_comps": list(rproj.comps),
                "_v22_raw_projection": float(weighted_points),
            })
            comps_map[ap.player_id] = comp_records
            continue

        # ---- v2.0 cumulative-arc engine (2+ NFL seasons) ----
        # v3.2 — use the broader comp_pool_arcs (includes short-career
        # "didn't pan out" players) and pass the target's completed
        # season count so find_comps applies the career-stage gate.
        target_n_seasons = len(target_arc.career_arc)
        proj = arc_project_player(
            target=target_arc,
            long_arc_corpus=comp_pool_arcs,
            target_age=age_now,
            league_format=BASE_FORMAT,
            percentile_table=percentile_table,
            k=top_k,
            target_n_seasons=target_n_seasons,
        )
        if not proj.comps:
            continue

        weighted_points = proj.projected_remaining_fp
        weighted_seasons = proj.projected_remaining_seasons

        # v1.1.0 career-length era lift — v2.0 keeps it as a MILD lift on
        # projected fantasy points for mobile/dual-threat QBs only.
        #
        # Why milder than v1.1: the v2.0 fantasy-arc methodology already
        # surfaces long-career comps (Manning, Brees, Rodgers, Stafford,
        # Cam) for any high-fp dual-threat target via their actual
        # similarity score — we are no longer comp-pool-starved. The
        # 1.50× multiplier from v1.1 was correcting for a v1.x sample-
        # bias bug that this methodology doesn't have. We retain a
        # smaller lift (1.10 dual-threat, 1.05 mobile) to acknowledge
        # that modern medicine + rule changes do extend mobile careers
        # AT THE TAIL, but we don't over-compound it on top of an
        # already-fantasy-points-anchored projection.
        #
        # The display ``projected_years_remaining`` keeps the FULL v1.1
        # lift so the UI accurately reflects "these QBs play longer".
        qb_style = STYLE_POCKET
        qb_rypg = 0.0
        lift_fp = 1.00     # applied to fp projection (mild)
        lift_years = 1.00  # applied to display years_remaining (full v1.1 lift)
        # v3.1 — QB decline gate diagnostic
        qb_decline_gate_applied = False
        if ap.position == "QB":
            qb_style = style_for_career(ap)
            qb_rypg = career_rushing_rate(ap)
            lift_years = career_length_era.get_lift(qb_style, CURRENT_ERA)
            if qb_style == STYLE_DUAL_THREAT:
                lift_fp = 1.10
            elif qb_style == STYLE_MOBILE:
                lift_fp = 1.05

            # v3.1 QB decline gate — if a mobile / dual-threat QB age 27+
            # has recent-2yr fp/g visibly below their all-time peak3yr
            # (< 0.85×), strip the dual-threat / mobile lift back to
            # pocket. The lift exists to credit "modern dual-threats
            # live longer than Cam Newton did" — a player who is no
            # longer producing at peak hasn't earned that bonus.
            #
            # Mahomes (peak3 23.5, recent ~21-23) → ratio > 0.85, lift kept.
            # Allen / Lamar / Hurts → still producing at peak, lift kept.
            # Fields (peak3 17.3, recent fp/g materially lower across a
            # backup/spot-start role) → ratio < 0.85, lift stripped.
            if (
                qb_style in (STYLE_DUAL_THREAT, STYLE_MOBILE)
                and display_age is not None and display_age >= 27
            ):
                recent_pg = _recent_2yr_target(target_arc, BASE_FORMAT)
                peak_pg = target_arc.peak_3yr_fp_per_game.get(BASE_FORMAT, 0.0)
                if peak_pg > 0 and recent_pg < peak_pg * 0.85:
                    lift_years = career_length_era.get_lift(
                        STYLE_POCKET, CURRENT_ERA,
                    )
                    lift_fp = 1.00
                    qb_decline_gate_applied = True

        # v3.1 RB late-career-still-producing boost — a 30+ RB who is
        # CURRENTLY producing top-12 fp/g (>= 16) deserves an extra year
        # of runway. The comp-pool floor for older RBs collapses to ~2
        # years (Henry got 2.1) which fails to credit ongoing top-tier
        # production. This boost only applies to RBs who are
        # demonstrably still elite (Henry case) — a 30+ RB whose recent
        # rate is mid-tier won't trigger it.
        rb_late_career_boost_applied = False
        if ap.position == "RB" and display_age is not None and display_age >= 30:
            current_pg = _recent_1yr_target(target_arc, BASE_FORMAT)
            if current_pg >= 16.0:
                weighted_seasons += 1.0
                rb_late_career_boost_applied = True

        weighted_points = apply_lift(weighted_points, lift_fp)
        weighted_seasons = apply_lift(weighted_seasons, lift_years)

        # v3.1 — compute the proven floor using the post-lift
        # weighted_seasons so it reflects the QB-decline-gate /
        # RB-late-career-boost-adjusted dynasty window. The floor is
        # APPLIED downstream of the v2.2 penalty stack so survival /
        # late-breakout penalties shape the forward projection without
        # undermining the banked-production floor.
        proven_floor_fp = _proven_production_floor(
            target=target_arc,
            league_format=BASE_FORMAT,
            weighted_seasons=weighted_seasons,
        )
        production_score_pre_floor = weighted_points  # post-lift, pre-floor

        top_comp = proj.comps[0].arc
        # Record comp list — keep the same shape as v1.x for format_overlay
        # / report.py. The v2.0 path already produces vector similarity
        # in (0, 1] (see ``fantasy_arc_similarity._weighted_similarity``);
        # we surface ``washed_out``/``seasons_played`` here so the player
        # page can flag comps whose careers ended early.
        comp_records: List[Dict] = []
        for c in proj.comps:
            pts, n_seasons = arc_project_remaining(
                c.arc, age_floor=c.snapshot_age, league_format=BASE_FORMAT,
            )
            seasons_played = sum(
                1 for s in c.arc.career_arc if s.games >= MIN_GAMES_PER_SEASON
            )
            final_age = max(
                (s.age for s in c.arc.career_arc if s.games >= MIN_GAMES_PER_SEASON),
                default=None,
            )
            washed_out = _comp_washed_out(
                final_age=final_age,
                seasons_played=seasons_played,
                last_season=c.arc.last_season,
                current_season=current_season,
            )
            # v2.4 PR 4: tag pre-1999-snapshot comps so the player page
            # can render an ⏳ era badge for transparency. The 0.9x
            # confidence haircut from PR 3 is already applied upstream
            # in find_comps; this field is purely for UI display.
            snapshot_season = next(
                (s.season for s in c.arc.career_arc if s.age == c.snapshot_age),
                None,
            )
            is_pre1999_snapshot = (
                snapshot_season is not None and snapshot_season < 1999
            )
            comp_records.append({
                "player_id": c.arc.player_id,
                "name": c.arc.name,
                "position": c.arc.position,
                "last_season": c.arc.last_season,
                "snapshot_season": snapshot_season,
                "is_pre1999_snapshot": is_pre1999_snapshot,
                # Already in (0, 1] for the v2.0 cumulative-arc engine —
                # no breakout boost is applied here. Mirror the rookie
                # engine schema for downstream consumers.
                "similarity": round(float(c.similarity), 4),
                "ranking_similarity": round(float(c.similarity), 4),
                "career_ppr": round(c.arc.career_total_fp.get(BASE_FORMAT, 0.0), 1),
                "post_age_projected_pts": round(pts, 1),
                "post_age_seasons": n_seasons,
                "seasons_played": seasons_played,
                "final_age": final_age,
                "washed_out": bool(washed_out),
                # v2.0 extras for the player page UI
                "snapshot_age": c.snapshot_age,
                "peak_3yr_fp_per_game": round(c.arc.peak_3yr_fp_per_game.get(BASE_FORMAT, 0.0), 2),
                "peak_season_fp_per_game": round(c.arc.peak_season_fp_per_game.get(BASE_FORMAT, 0.0), 2),
                "career_arc_fp_per_game": [
                    {"age": s.age, "fp_per_game": round(s.fp_per_game.get(BASE_FORMAT, 0.0), 2)}
                    for s in c.arc.career_arc
                ],
            })

        rankings.append({
            "player_id": ap.player_id,
            "name": ap.name,
            "position": ap.position,
            # ``age`` is the player's CURRENT age (PFR-style, day-precision
            # via ``PlayerCareer.current_age``). ``last_season.age`` is
            # still used internally for comping/age windows.
            "age": display_age,
            "last_season": ap.last_season,
            "production_score": round(weighted_points, 1),
            "projected_years_remaining": round(weighted_seasons, 1),
            "top_comp": top_comp.name,
            "top_comp_id": top_comp.player_id,
            "comp_tier": _comp_tier_label(top_comp, top_comp.career_total_fp.get(BASE_FORMAT, 0.0)),
            "n_comps": len(proj.comps),
            "qb_style": qb_style if ap.position == "QB" else None,
            "qb_career_rypg": round(qb_rypg, 1) if ap.position == "QB" else None,
            "career_length_lift": round(lift_years, 3),
            "career_length_lift_fp": round(lift_fp, 3),
            # v2.0 projection diagnostics
            "comp_weighted_fp": round(proj.comp_weighted_fp, 1),
            "peak_anchored_fp": round(proj.peak_anchored_fp, 1),
            "projection_path": (
                "peak_anchored" if proj.peak_anchored_fp > proj.comp_weighted_fp
                else "comp_weighted"
            ),
            # v3.1 — proven-production floor + decline / late-career
            # diagnostics. ``production_path`` extends ``projection_path``
            # with "proven_floor" when the v3.1 floor wins (resolved
            # downstream of the v2.2 penalty stack). The legacy
            # ``projection_path`` field is kept unchanged so the v2 UI
            # continues to work; new consumers should read
            # ``production_path``.
            "proven_floor_fp": round(proven_floor_fp, 1),
            "production_score_pre_floor": round(production_score_pre_floor, 1),
            "production_path": (
                "peak_anchored" if proj.peak_anchored_fp > proj.comp_weighted_fp
                else "comp_weighted"
            ),
            "qb_decline_gate_applied": bool(qb_decline_gate_applied),
            "rb_late_career_boost_applied": bool(rb_late_career_boost_applied),
            # v2.0 player-arc metrics
            "peak_3yr_fp_per_game": round(target_arc.peak_3yr_fp_per_game.get(BASE_FORMAT, 0.0), 2),
            "peak_season_fp_per_game": round(target_arc.peak_season_fp_per_game.get(BASE_FORMAT, 0.0), 2),
            "career_avg_fp_per_game": round(target_arc.career_avg_fp_per_game.get(BASE_FORMAT, 0.0), 2),
            "career_total_fp_to_date": round(target_arc.career_total_fp.get(BASE_FORMAT, 0.0), 1),
            # v2.1 dispatcher field: which engine produced this row
            "engine": "fantasy_arc_v2",
            # v1.2 cohort fields retained as None for downstream readers
            "style_cohort": None,
            "cohort_pool_size": None,
            "cohort_widened": None,
            "cohort_styles_used": None,
            # v2.2 tracking fields (consumed by the penalty
            # post-processing pass; removed before returning).
            "_v22_engine": "v2",
            "_v22_target_arc": target_arc,
            "_v22_comps": list(proj.comps),
            "_v22_raw_projection": float(weighted_points),
        })
        comps_map[ap.player_id] = comp_records

    # ------------------------------------------------------------------
    # v2.2.0 — survival / confidence / late-breakout penalty pass.
    #
    # Pre-pass: compute position-tier baselines from the RAW (pre-
    # penalty) projections. These are used as the Bayesian prior for
    # the confidence-shrinkage pull.
    # ------------------------------------------------------------------
    raw_rankings = [
        {"position": r["position"], "production_score": r["_v22_raw_projection"]}
        for r in rankings
    ]
    position_baselines = compute_position_tier_baselines(raw_rankings, top_n=50)

    survival_diag: Dict[str, Dict] = {}
    confidence_diag: Dict[str, Dict] = {}
    late_breakout_diag: Dict[str, Dict] = {}

    for row in rankings:
        target_arc = row["_v22_target_arc"]
        comps_v22 = row["_v22_comps"]
        raw_proj = row["_v22_raw_projection"]
        engine_kind = row["_v22_engine"]

        surv = compute_survival(
            name=row["name"], position=row["position"], comps=comps_v22,
        )
        baseline = position_baselines.get(row["position"], 0.0)
        conf = compute_confidence(
            arc=target_arc,
            position_tier_baseline=baseline,
            current_season=current_season,
        )
        late = compute_late_breakout(
            arc=target_arc, raw_stats_by_pid_season=raw_stats,
        )
        # v3.3 — missed-recent-season penalty. Skip the v2.1 rookie
        # engine path: 1-NFL-season rookies just played, the rookie
        # engine handles their thin sample via its own confidence /
        # games-played factor. The cumulative-arc engine path needs
        # the explicit missed-season hit to ensure (e.g.) Joe Mixon
        # — who did not play in 2025 — takes a haircut for absence.
        if engine_kind == "rookie":
            missed = compute_missed_recent_season(
                arc=target_arc, corpus_last_season=current_season,
            )
            missed_mult_used = 1.0
        else:
            missed = compute_missed_recent_season(
                arc=target_arc, corpus_last_season=current_season,
            )
            missed_mult_used = missed.missed_season_multiplier

        # For 1-NFL-season rookies routed through the v2.1 rookie
        # engine, the engine ALREADY applies its own games-played
        # confidence factor (FULL_CONFIDENCE_GAMES=8). For RB/WR/TE
        # rookies layering both would double-penalize them and break
        # the v2.1 invariants (Jeanty top 25, Tetairoa top 30). Skip
        # v2.2 confidence shrinkage for non-QB rookies. QB rookies
        # still take the v2.2 confidence haircut because their projection
        # is driven by extrapolation of fp/G to a 10+ year QB career
        # — a much longer horizon than RB/WR/TE — and Phil's brief
        # specifically calls out Shedeur Sanders (~5-8 starts) as
        # needing a deep haircut despite playing in 8 games.
        if engine_kind == "rookie" and target_arc.position != "QB":
            effective_conf_for_stack = 1.0
        else:
            effective_conf_for_stack = conf.confidence

        stack = apply_penalty_stack(
            projection_raw=raw_proj,
            survival_multiplier=surv.survival_multiplier,
            confidence=effective_conf_for_stack,
            position_tier_baseline=baseline,
            late_breakout_penalty=late.late_breakout_penalty,
            # Stale-data flag disables the Bayesian pull-toward-baseline
            # for backups / journeymen whose recent NFL exposure is
            # below the starter threshold (see ConfidenceDiagnostics).
            # 1-NFL-season rookies (rookie engine path) are exempt by
            # design — they're actively in the league, just new.
            is_stale_data=conf.is_stale_data and engine_kind != "rookie",
            # v3.3 missed-recent-season penalty (Phil 2026-05-28).
            missed_season_multiplier=missed_mult_used,
        )

        # Overwrite the production_score with the post-penalty value.
        row["production_score"] = round(stack.projection_final, 1)

        # v3.8 (Phil 2026-05-29) — ROOKIE comp-pool career-arc floor.
        # Phil's specific examples (Ashton Jeanty top comp = LT, Quinshon
        # Judkins top comp = Knowshon Moreno) call out that elite-comp
        # rookies should project to a meaningful fraction of their comp
        # pool's CAREER fp. The v2.2 survival_multiplier is calibrated to
        # punish bust-heavy comp pools, but for HIGH-DRAFT-CAPITAL rookies
        # whose top-5 NON-BUST comps include LT-tier careers, the
        # multiplier slashes the projection BELOW the comp pool floor.
        # We re-apply the comp-pool floor here as a HARD post-stack
        # floor so Jeanty/Judkins/Hampton can't be crushed below
        # POST_STACK_COMP_POOL_FRACTION × median(top-5 non-bust comp
        # post-rookie career fp).
        # v3.8 — post-stack comp-pool floor for rookies whose penalty
        # stack collapsed the projection below their comp pool's
        # realised career arc. Phil's brief calls out Jeanty (top comp
        # = LT, 3,277 career fp) and Judkins (Knowshon-tier) as
        # specifically needing this protection — the v2.2 survival
        # multiplier was over-discounting them for RB-position bust
        # rates.
        #
        # Gate: only apply when the stack haircut was steep
        # (survival_multiplier < FLOOR_TRIGGER_SURVIVAL). Above this
        # threshold the stack is operating in its calibrated band and
        # we don't want to override the natural projection ordering
        # (Fannin > Gadsden because Fannin's stats are better) with a
        # comp-pool-driven floor that flattens ties.
        if (
            engine_kind == "rookie"
            and surv.survival_multiplier < FLOOR_TRIGGER_SURVIVAL
        ):
            # v3.8 — anchor = mean of TOP-3-by-post-rookie-fp NON-BUST
            # comps drawn from the TOP-N most similar. The two-stage
            # filter prevents low-similarity outlier comps from
            # dominating the floor:
            #
            #   * Shedeur Sanders has Matt Ryan (sim 0.718) and McNabb
            #     (sim 0.876) in his non-bust comp list. v3.8 first
            #     restricts to top-TOPSIM_TOP_N similarity comps, which
            #     keeps McNabb but drops Matt Ryan; then takes top-3
            #     by post-rookie career fp. Anchor stays modest.
            #   * Ashton Jeanty has LT (sim 0.94) and other LT-tier
            #     RBs all in top-similarity. Anchor stays elite.
            #
            # The two-stage filter is the principled answer to "how do
            # I separate elite-comp rookies from busts whose comp pools
            # happen to include a McNabb-tier tail?"
            top_sim_subset = sorted(
                comps_v22, key=lambda m: m.similarity, reverse=True,
            )[:POST_STACK_FLOOR_TOPSIM_N]
            non_bust_in_subset = [
                m for m in top_sim_subset
                if m.profile.post_rookie_total_fp > 0
            ]
            top3 = sorted(
                non_bust_in_subset,
                key=lambda m: m.profile.post_rookie_total_fp,
                reverse=True,
            )[:3]
            if len(top3) >= 3:
                comp_pool_anchor = sum(
                    m.profile.post_rookie_total_fp for m in top3
                ) / 3
                # Top-3 comps' rookie-year fp/G (from their stored
                # rookie vector v[0]). v[0] is fp_per_game.
                comp_rookie_rates = [m.profile.vector[0] for m in top3 if m.profile.vector]
                target_rookie_rate = float(row.get("rookie_fp_per_game", 0.0))
                if comp_rookie_rates and target_rookie_rate > 0:
                    comp_median_rate = sorted(comp_rookie_rates)[
                        len(comp_rookie_rates) // 2
                    ]
                    if comp_median_rate > 0:
                        # Scale the floor by rate ratio, capped at 1.0.
                        # A target whose rookie rate matches the comp
                        # pool median gets full floor; below median
                        # gets scaled down.
                        rate_scale = min(
                            1.0,
                            target_rookie_rate / comp_median_rate,
                        )
                    else:
                        rate_scale = 1.0
                else:
                    rate_scale = 1.0
                post_stack_floor = (
                    POST_STACK_COMP_POOL_FRACTION
                    * comp_pool_anchor
                    * rate_scale
                )
                # Cap at the rookie's own peak-anchored projection.
                peak_cap = float(row.get("peak_anchored_fp", 0.0))
                if peak_cap > 0:
                    post_stack_floor = min(post_stack_floor, peak_cap)
                if post_stack_floor > row["production_score"]:
                    row["post_stack_comp_pool_floor"] = round(post_stack_floor, 1)
                    row["post_stack_comp_pool_anchor"] = round(comp_pool_anchor, 1)
                    row["post_stack_comp_pool_rate_scale"] = round(rate_scale, 3)
                    row["production_score"] = round(post_stack_floor, 1)
                    row["projection_path"] = "rookie_comp_pool_floor"
                    row["production_path"] = "rookie_comp_pool_floor"

        # v3.3 — the v3.1 proven-production floor NO LONGER OVERRIDES
        # production_score. Phil's 2026-05-28 brief: "The projected
        # remaining fantasy points should be some sort of weighted
        # average of the comparable players applied to the player."
        # The proven_floor was injecting BANKED (already-realised)
        # production into a number labelled "projected remaining FP",
        # which caused Derrick Henry to read 2,103 even though none of
        # his comps exceeded ~445 projected fp post-age-32.
        #
        # proven_floor_fp is RETAINED on the row as a diagnostic
        # ("banked credit floor") so the player page can still surface
        # it, but it does NOT win the projection any more.
        row.setdefault("production_path", row.get("projection_path", "comp_weighted"))
        # Carry penalty diagnostics on the row for the UI / tests.
        row["survival_multiplier"] = round(surv.survival_multiplier, 3)
        row["comp_durable_rate"] = round(surv.durable_career_rate, 3)
        row["comp_bust_rate"] = round(surv.bust_rate, 3)
        row["top5_bust_count"] = surv.top5_bust_count
        row["comp_short_career_rate"] = round(surv.short_career_rate, 3)
        row["comp_weighted_career_length"] = round(surv.weighted_career_length, 2)
        row["sample_confidence"] = round(conf.confidence, 3)
        row["career_nfl_starts"] = conf.career_nfl_starts
        row["recent_games_two_year"] = conf.recent_games
        row["is_stale_data"] = bool(conf.is_stale_data)
        row["position_tier_baseline"] = round(baseline, 1)
        row["late_breakout_penalty"] = round(late.late_breakout_penalty, 3)
        row["breakout_age"] = late.breakout_age
        row["projection_raw_pre_penalty"] = round(raw_proj, 1)
        row["projection_after_survival"] = round(stack.projection_after_survival, 1)
        row["projection_after_confidence"] = round(stack.projection_after_confidence, 1)
        # v3.3 missed-recent-season diagnostics.
        row["missed_season_multiplier"] = round(missed.missed_season_multiplier, 3)
        row["missed_season_reason"] = missed.reason
        row["missed_season_last_played"] = missed.last_played_season
        row["missed_season_last_played_games"] = missed.last_played_games
        row["missed_season_seasons_since"] = missed.seasons_since_played

        survival_diag[row["player_id"]] = {
            "name": surv.name,
            "position": surv.position,
            "bust_rate": surv.bust_rate,
            "short_career_rate": surv.short_career_rate,
            "weighted_career_length": surv.weighted_career_length,
            "durable_career_rate": surv.durable_career_rate,
            "survival_multiplier": surv.survival_multiplier,
        }
        confidence_diag[row["player_id"]] = {
            "name": conf.name,
            "position": conf.position,
            "career_nfl_starts": conf.career_nfl_starts,
            "confidence": conf.confidence,
            "position_tier_baseline": conf.position_tier_baseline,
        }
        late_breakout_diag[row["player_id"]] = {
            "name": late.name,
            "position": late.position,
            "breakout_age": late.breakout_age,
            "late_breakout_penalty": late.late_breakout_penalty,
        }

        # Strip private tracking fields before sorting / returning.
        del row["_v22_engine"]
        del row["_v22_target_arc"]
        del row["_v22_comps"]
        del row["_v22_raw_projection"]

    # Persist diagnostics so the UI / users can see WHY a player got
    # penalized. Best-effort: silently swallow filesystem errors so a
    # read-only deploy still ranks correctly.
    try:
        import json as _json
        import os as _os
        diag_dir = _os.path.join("data", "diagnostics")
        _os.makedirs(diag_dir, exist_ok=True)
        with open(_os.path.join(diag_dir, "v2.2_survival.json"), "w") as f:
            _json.dump(survival_diag, f, indent=2)
        with open(_os.path.join(diag_dir, "v2.2_confidence.json"), "w") as f:
            _json.dump(confidence_diag, f, indent=2)
        with open(_os.path.join(diag_dir, "v2.2_late_breakout.json"), "w") as f:
            _json.dump(late_breakout_diag, f, indent=2)
    except Exception:
        pass

    rankings.sort(key=lambda r: r["production_score"], reverse=True)
    n = len(rankings)
    for i, row in enumerate(rankings):
        row["overall_rank"] = i + 1
        thresholds = [12, 24, 48, 72, 108, 144, 200, 260]
        tier = 9
        for t_idx, th in enumerate(thresholds, start=1):
            if i + 1 <= th:
                tier = t_idx
                break
        row["tier"] = tier

    result = EngineResult(
        rankings=rankings,
        comps=comps_map,
        era_pace=pace,
        careers=careers,
        long_arc_corpus=long_arc_corpus,
        active_players=active_players,
        career_length_era=career_length_era,
        arcs=arcs,
        long_arc_arcs=long_arc_arcs,
        comp_pool_arcs=comp_pool_arcs,
        percentile_table=percentile_table,
        base_format=BASE_FORMAT,
    )

    if persist:
        _persist(result, current_season)

    return result


def _persist(result: EngineResult, current_season: int) -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at_season": current_season,
        "engine_version": "v2.1",
        "methodology": "three-tier cohort dispatcher (rookie-NFL + cumulative-arc)",
        "n_active": len(result.active_players),
        "n_long_arc": len(result.long_arc_corpus),
        "n_retired": len(result.long_arc_corpus),  # back-compat
        "n_ranked": len(result.rankings),
        "era_pace_source": result.era_pace.source,
        "era_pace": result.era_pace.multipliers,
        "career_length_era_source": (
            result.career_length_era.source if result.career_length_era else None
        ),
        "career_length_era_lift": (
            result.career_length_era.lift if result.career_length_era else None
        ),
        "career_length_era_median_seasons": (
            result.career_length_era.median_seasons if result.career_length_era else None
        ),
        "rankings": result.rankings,
    }
    (OUT_ROOT / "rankings.json").write_text(
        json.dumps(payload, indent=2, default=float), encoding="utf-8"
    )
    (OUT_ROOT / "comps.json").write_text(
        json.dumps(result.comps, indent=2, default=float), encoding="utf-8"
    )
    long_arc = [
        {
            "player_id": c.player_id,
            "name": c.name,
            "position": c.position,
            "last_season": c.last_season,
            "career_ppr": round(c.career_ppr(), 1),
            "n_seasons": len(c.seasons),
        }
        for c in result.long_arc_corpus
    ]
    (OUT_ROOT / "retired_corpus.json").write_text(
        json.dumps(long_arc, indent=2, default=float), encoding="utf-8"
    )
    (OUT_ROOT / "long_arc_corpus.json").write_text(
        json.dumps(long_arc, indent=2, default=float), encoding="utf-8"
    )
    # v2.0 fantasy-arc corpus sidecar.
    persist_corpus(result.long_arc_arcs, out_path=OUT_ROOT_V2 / "fantasy_arc_corpus.json")


# Convenience accessor for tests / debugging.
def comp_names_for(result: EngineResult, player_name: str) -> List[str]:
    pid: Optional[str] = None
    for ap in result.active_players:
        if ap.name == player_name:
            pid = ap.player_id
            break
    if pid is None:
        return []
    return [c["name"] for c in result.comps.get(pid, [])]
