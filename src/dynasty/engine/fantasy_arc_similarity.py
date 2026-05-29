"""Fantasy-point-arc similarity engine (v2.0.0).

REPLACES ``similarity_v1`` and ``style_cohort`` as the single source of truth
for player rankings. The methodology change vs v1.x:

    v1.0  per-stat z-score cosine on (passing_yards, passing_tds, ...) →
          flattened Allen's fantasy advantage because raw passing volume
          isn't elite for him.
    v1.1  +dual-threat career-length lift on projected_remaining_years
          (multiplicative, capped at 1.5×) → still didn't reach Allen
          top 5 because the BASE projection was z-scored.
    v1.2  per-fantasy-point-category z-scoring + style cohort → still
          z-scoring, still scale-invariant within era, still buried
          Allen.

    v2.0  COMPARE PLAYERS BY THE FANTASY POINTS THEY ACTUALLY PRODUCE.
          The vector is in RAW fantasy-point units. Two players with
          similar fp/g production curves under modern scoring are
          similar — regardless of how they earn those points (passing
          vs rushing).

The vector is 11-dim, all components in fantasy-point units (era-pace
pre-adjusted at corpus build → all values are "modern fantasy-points
equivalent"):

    v[0]  = fp_per_game at the current age (recent-season weight 1.0)
    v[1]  = fp_per_game at age-1 (weight 0.7)
    v[2]  = fp_per_game at age-2 (weight 0.5)
    v[3]  = career-avg fp_per_game through current age
    v[4]  = peak-3yr-avg fp_per_game through current age
    v[5]  = peak-single-season fp_per_game (any age through current)
    v[6]  = career-total fp through current age (scaled by /100)
    v[7]  = trajectory slope (linear regression of fp_per_game vs
            career-season-index, in fp/g per season)
    v[8]  = durability (fraction of possible games played = career_games
            / (n_seasons * 17))
    v[9]  = career-stage fp percentile within position (0-1; computed
            against long-arc corpus at the same career-season-index)
    v[10] = current_age * AGE_SCALE (added in v2.3.5 — age is a PRIMARY
            feature, not a tie-breaker; previously the engine ignored
            age in distance entirely, causing 24-yo rookies to comp
            with 22-yo late-bloomers at the same fp tier)

Similarity is computed via INVERSE-DISTANCE (1 / (1 + d/scale)) over a
feature-importance-weighted Euclidean distance, NOT cosine. We need
absolute magnitude to matter: Allen (peak fp/g ≈ 28) is NOT similar to
Daniel Jones (peak fp/g ≈ 16) even if their proportions match.
Feature weights emphasise peak / current / 3yr-peak / career-avg
(the magnitude-bearing components) and de-weight slope / durability /
percentile (noisy on small-sample careers).

The top neighbour for Josh Allen is another QB whose career-arc-to-date
actually PRODUCED similar fp/G under modern scoring. That naturally
pulls in peak-Cam-Newton, peak-Steve-Young, peak-Vick, peak-Manning,
peak-Brees regardless of running/passing split.

Per-position eligibility:
    * same position
    * is_long_arc=True
    * age within ±1 of target's current age
    * career-stage (NFL season index) within ±1 of target's
    * top-K=20 by cosine, projection is similarity-weighted

Projection:
    * For each comp, take their realised post-age fantasy points
      under the target format (already in modern-fp units).
    * Time-discount 5%/year out.
    * Weighted sum → projected_remaining_career_fantasy_points.
    * v1.1's career_length_era lift kept as a FINAL multiplier on
      projected_remaining for QBs (mobile/dual-threat) — modern medicine
      and rules continue to extend careers and the comp pool can still
      under-represent that.

The output is the SOLE dynasty value. No composite blend with v0/v1.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .fantasy_arc import (
    SUPPORTED_FORMATS,
    CareerArc,
    SeasonArcPoint,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TOP_K_COMPS = 25  # v3.3: widened from 20 (Phil 2026-05-28 — comp pool felt thin)
AGE_WINDOW = 1
CAREER_STAGE_WINDOW = 1
DISCOUNT_PER_YEAR = 0.05
MIN_GAMES_PER_SEASON = 4

# Career-total scale factor for the v[6] component. The peak components
# (v[0..5]) sit in the 10-30 range; career totals sit in the 1000-4000
# range. /100 brings v[6] into the 10-40 range, in line with the per-game
# components.
CAREER_TOTAL_SCALE = 100.0

# v2.3.5: scale factor applied to ``current_age`` for v[10]. With
# FEATURE_WEIGHTS[10]=5.0, a 3-year age gap contributes
# 5.0 * (AGE_SCALE * 3)^2 = 5.0 * (1.5)^2 = 11.25 to squared distance —
# about one peak_3yr-unit of "distance" per 3 age-years. This is enough
# to push Steve Smith Sr.'s rookie (age 22) out of Johnny Wilson's
# (age 24) top-10 comp list when their fp/G profiles are similar, while
# leaving same-age comps unchanged. Calibrated empirically against the
# Phil-pinned comp lists; see docs/V2.3.5-VALIDATION.md.
AGE_SCALE = 0.5

VECTOR_DIM = 11

BASE_FORMAT = "sf_ppr"

# Per-dimension importance weights for the weighted Euclidean distance.
# Higher weight = that dimension contributes more to distance, making it
# harder for two players to be "similar" if they differ on it.
#
# Rationale:
#   * v[0..5] (current fp, recent-arc, career-avg, peak-3yr, peak-1yr) are
#     the MAGNITUDE-BEARING components that capture "how much fantasy
#     does this player produce". Weighted highest.
#   * v[6] (career-total scaled) captures cumulative volume; matters for
#     long-career comps but rookies have ~0 → don't let it dominate.
#   * v[7] (slope) is noisy on small samples — weighted low.
#   * v[8] (durability) — small differentiator; weighted low.
#   * v[9] (career-stage percentile) — already encoded in career-total +
#     peak via the magnitude components; weighted moderate.
# v2.3.5: v[10] current_age (scaled by AGE_SCALE) is weighted STRONG
# (5.0). Pre-v2.3.5, age was absent from the cumulative-engine distance
# entirely — the field existed on FantasyArcVector but was never
# iterated in _weighted_distance. Phil identified the bug after Johnny
# Wilson (WR, age 24 rookie, ~0.6 fp/G) was comped to Steve Smith Sr.
# and Santana Moss (both age 22 rookies who later broke out). Prior
# research consistently ranks age as one of the strongest predictors of
# NFL skill-position outcomes, so it deserves first-class weight here.
FEATURE_WEIGHTS: Tuple[float, ...] = (
    2.0,   # v[0] fp_now (current season per-game)
    1.5,   # v[1] fp_age-1
    1.0,   # v[2] fp_age-2
    3.0,   # v[3] career_avg_fp_per_game   — magnitude anchor
    4.0,   # v[4] peak_3yr_fp_per_game     — STRONGEST magnitude anchor
    3.0,   # v[5] peak_single_season       — magnitude anchor
    0.8,   # v[6] career_total / 100
    0.2,   # v[7] slope            — noisy on small samples
    0.3,   # v[8] durability (scaled 0-10)
    0.5,   # v[9] career-stage percentile (scaled 0-30)
    5.0,   # v[10] current_age (scaled by AGE_SCALE) — STRONG: age is a
           #       primary feature (v2.3.5 fix for the age-blind bug).
)

# Distance-to-similarity conversion: sim = 1 / (1 + d / SIMILARITY_SCALE).
# Tuned so a same-tier comp scores ~0.6-0.85 and an off-tier comp scores
# ~0.2-0.4. With magnitude-anchor weights cranked up, distance between
# different-tier producers (Allen peak 25 vs Jones peak 16) is large
# enough that they don't comp together.
SIMILARITY_SCALE = 20.0


# ---------------------------------------------------------------------------
# Vector construction
# ---------------------------------------------------------------------------

@dataclass
class FantasyArcVector:
    """The 11-dim arc vector + its position and metadata for filtering.

    v2.3.5: vector grew from 10-dim to 11-dim with the addition of v[10] =
    current_age * AGE_SCALE. ``current_age`` is also kept as a separate
    field for snapshot-window filtering; the v[10] entry is what costs
    distance when two players differ on age.
    """

    values: List[float]
    position: str
    career_stage: int             # # of completed NFL seasons through the snapshot
    current_age: int

    @property
    def dim(self) -> int:
        return len(self.values)


def _trajectory_slope(arc: Sequence[SeasonArcPoint], league_format: str) -> float:
    """Linear-regression slope of fp/g vs career-season-index.

    Career-season-index is 0..N-1 across the chronologically-sorted seasons.
    Returns 0.0 if fewer than 2 qualifying seasons.
    """
    qual = [s for s in arc if s.games >= MIN_GAMES_PER_SEASON]
    n = len(qual)
    if n < 2:
        return 0.0
    xs = list(range(n))
    ys = [s.fp_per_game.get(league_format, 0.0) for s in qual]
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    num = sum((xs[i] - x_mean) * (ys[i] - y_mean) for i in range(n))
    den = sum((x - x_mean) ** 2 for x in xs)
    if den <= 1e-9:
        return 0.0
    return num / den


def _durability(arc: Sequence[SeasonArcPoint]) -> float:
    """Fraction of possible games played, capped to [0, 1].

    "Possible" = n_seasons * 17 (the modern season length). For pre-2021
    seasons the NFL ran 16 games; using 17 as the universal denominator
    nudges older players slightly DOWN on durability — acceptable since
    the comparison is "what's the modern arc curve" anyway.
    """
    if not arc:
        return 0.0
    n_seasons = len(arc)
    games = sum(s.games for s in arc)
    return min(1.0, games / max(1, n_seasons * 17))


def _peak_3yr_through_age(
    arc: Sequence[SeasonArcPoint], league_format: str, through_age: int,
) -> float:
    qual = [s for s in arc if s.age <= through_age]
    n = len(qual)
    if n == 0:
        return 0.0
    best = 0.0
    for i in range(n):
        j = min(n, i + 3)
        window = qual[i:j]
        gtot = sum(s.games for s in window)
        if gtot <= 0:
            continue
        ptot = sum(s.fp_per_game.get(league_format, 0.0) * s.games for s in window)
        avg = ptot / gtot
        if avg > best:
            best = avg
    return best


def _career_avg_through_age(
    arc: Sequence[SeasonArcPoint], league_format: str, through_age: int,
) -> float:
    qual = [s for s in arc if s.age <= through_age]
    gtot = sum(s.games for s in qual)
    if gtot <= 0:
        return 0.0
    ptot = sum(s.fp_per_game.get(league_format, 0.0) * s.games for s in qual)
    return ptot / gtot


def _fp_at_age(
    arc: Sequence[SeasonArcPoint], league_format: str, age: int,
) -> float:
    """fp/g for the season at exactly ``age``. If no qualifying season, 0."""
    for s in arc:
        if s.age == age and s.games >= MIN_GAMES_PER_SEASON:
            return s.fp_per_game.get(league_format, 0.0)
    return 0.0


def _peak_single_through_age(
    arc: Sequence[SeasonArcPoint], league_format: str, through_age: int,
) -> float:
    qual = [s for s in arc if s.age <= through_age and s.games >= MIN_GAMES_PER_SEASON]
    if not qual:
        return 0.0
    return max(s.fp_per_game.get(league_format, 0.0) for s in qual)


def _career_total_through_age(
    arc: Sequence[SeasonArcPoint], league_format: str, through_age: int,
) -> float:
    return sum(
        s.fp_total.get(league_format, 0.0)
        for s in arc if s.age <= through_age
    )


def _career_stage_index(arc: Sequence[SeasonArcPoint], through_age: int) -> int:
    """How many completed NFL seasons through ``through_age``."""
    return sum(1 for s in arc if s.age <= through_age and s.games >= MIN_GAMES_PER_SEASON)


# ---------------------------------------------------------------------------
# Career-stage percentile (v[9])
# ---------------------------------------------------------------------------

@dataclass
class CareerStagePercentileTable:
    """Per-(position, career_stage_index) → sorted list of long-arc career
    totals at that stage. Used to compute the v[9] percentile for any
    target/comp."""

    by_pos_stage: Dict[Tuple[str, int], List[float]]

    def percentile(self, position: str, stage: int, value: float) -> float:
        bucket = self.by_pos_stage.get((position, stage)) or []
        if not bucket:
            return 0.5
        # bucket is sorted ascending. Binary-search insertion index gives
        # percentile.
        lo, hi = 0, len(bucket)
        while lo < hi:
            mid = (lo + hi) // 2
            if bucket[mid] < value:
                lo = mid + 1
            else:
                hi = mid
        return lo / len(bucket)


def build_career_stage_percentile_table(
    corpus: Sequence[CareerArc], league_format: str = BASE_FORMAT,
) -> CareerStagePercentileTable:
    """For each (position, career_stage_index s), collect the career-total
    fp through stage s among long-arc players, sorted ascending."""
    raw: Dict[Tuple[str, int], List[float]] = defaultdict(list)
    for arc in corpus:
        if not arc.is_long_arc:
            continue
        qual = [s for s in arc.career_arc if s.games >= MIN_GAMES_PER_SEASON]
        running = 0.0
        for idx, s in enumerate(qual, start=1):
            running += s.fp_total.get(league_format, 0.0)
            raw[(arc.position, idx)].append(running)
    for k in raw:
        raw[k].sort()
    return CareerStagePercentileTable(by_pos_stage=raw)


# ---------------------------------------------------------------------------
# The main vector builder
# ---------------------------------------------------------------------------

def build_arc_vector(
    arc: CareerArc,
    through_age: int,
    league_format: str,
    percentile_table: CareerStagePercentileTable,
) -> Optional[FantasyArcVector]:
    """Build the 10-dim fantasy-arc vector for ``arc`` snapshotted at
    ``through_age``. Returns None if there are no qualifying seasons.
    """
    qual = [s for s in arc.career_arc if s.age <= through_age and s.games >= MIN_GAMES_PER_SEASON]
    if not qual:
        return None

    stage_idx = len(qual)
    fp_now = _fp_at_age(arc.career_arc, league_format, through_age)
    # Fallback: if no season at exact through_age, use the latest qualifying
    # season at age <= through_age. This handles cases where a player missed
    # the season at age=through_age but has prior arc.
    if fp_now == 0.0:
        fp_now = qual[-1].fp_per_game.get(league_format, 0.0)
    fp_a1 = _fp_at_age(arc.career_arc, league_format, through_age - 1)
    fp_a2 = _fp_at_age(arc.career_arc, league_format, through_age - 2)

    career_avg = _career_avg_through_age(arc.career_arc, league_format, through_age)
    peak_3yr = _peak_3yr_through_age(arc.career_arc, league_format, through_age)
    peak_1yr = _peak_single_through_age(arc.career_arc, league_format, through_age)
    career_total = _career_total_through_age(arc.career_arc, league_format, through_age)
    slope = _trajectory_slope(qual, league_format)
    durability = _durability(qual)
    pct = percentile_table.percentile(arc.position, stage_idx, career_total)

    values = [
        fp_now * 1.0,
        fp_a1 * 0.7,
        fp_a2 * 0.5,
        career_avg,
        peak_3yr,
        peak_1yr,
        career_total / CAREER_TOTAL_SCALE,
        slope,
        durability * 10.0,    # bring to ~0-10 range, similar to per-game fp
        pct * 30.0,           # bring to ~0-30 range (peak fp scale)
        through_age * AGE_SCALE,  # v[10] v2.3.5: age dimension
    ]
    return FantasyArcVector(
        values=values,
        position=arc.position,
        career_stage=stage_idx,
        current_age=through_age,
    )


# ---------------------------------------------------------------------------
# KNN + projection
# ---------------------------------------------------------------------------

def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Retained for back-compat / tests; v2.0's KNN uses weighted
    inverse-distance similarity (see ``_weighted_similarity``)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na <= 1e-9 or nb <= 1e-9:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def _weighted_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """Feature-importance-weighted Euclidean distance."""
    n = min(len(a), len(b), len(FEATURE_WEIGHTS))
    s = 0.0
    for i in range(n):
        d = a[i] - b[i]
        s += FEATURE_WEIGHTS[i] * d * d
    return math.sqrt(s)


def _weighted_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Inverse-distance similarity from the weighted Euclidean distance.
    Returns a value in (0, 1]; identical vectors → 1.0.
    """
    d = _weighted_distance(a, b)
    return 1.0 / (1.0 + d / SIMILARITY_SCALE)


@dataclass
class CompMatch:
    arc: CareerArc
    similarity: float
    snapshot_age: int
    # v2.4 diagnostics: the pre-haircut similarity and whether the
    # 0.9× pre-1999 confidence haircut was applied to ``similarity``.
    # Default to no-haircut so existing constructor sites and tests
    # that build CompMatch directly remain compatible.
    raw_similarity: float = 0.0
    pre1999_haircut_applied: bool = False

    def __post_init__(self):
        if self.raw_similarity == 0.0:
            # Default ``raw_similarity`` to ``similarity`` when not
            # explicitly set — keeps old call sites consistent.
            self.raw_similarity = self.similarity


# v2.4 — 0.9× confidence haircut for pre-1999 comps.
#
# Phil's decision (V2.4-PRE1999-LEGENDS section 10.3): "Conservative;
# era-pace is principled but not perfect." Comps whose SNAPSHOT season
# (the season the comp's age matched the target's age) lies before 1999
# get their similarity weight multiplied by 0.9 in the weighted comp
# average. This shrinks the influence of pre-1999 comps relative to
# 1999+ comps WITHOUT removing them from the pool.
#
# CRITICAL nuance: the haircut is about pre-1999 DATA QUALITY
# UNCERTAINTY, not about the player. When an Emmitt-Smith-style
# crossover player's snapshot age lands in his 1999+ years (age 30+),
# we do NOT apply the haircut — a 2000-season comp is a 2000-season
# comp, regardless of who lived through 1990-1998 to reach it.
PRE1999_COMP_WEIGHT_HAIRCUT = 0.9
PRE1999_SNAPSHOT_CUTOFF = 1999


def snapshot_season_for_comp(arc: CareerArc, snapshot_age: int) -> Optional[int]:
    """Return the comp's NFL season that lines up with ``snapshot_age``.

    We look for a qualifying season (``games >= MIN_GAMES_PER_SEASON``)
    whose age exactly matches ``snapshot_age``. Returns ``None`` if the
    snapshot age isn't present in the arc — the caller should treat
    this as "no pre-1999 haircut applies" (we couldn't resolve it; be
    conservative and don't penalise the comp).
    """
    for s in arc.career_arc:
        if s.age == snapshot_age and s.games >= MIN_GAMES_PER_SEASON:
            return s.season
    # Fallback: closest qualifying season (used only when snapshot age
    # was widened by the AGE_WINDOW slack inside ``find_comps`` and we
    # need the closest available year).
    candidates = [s for s in arc.career_arc if s.games >= MIN_GAMES_PER_SEASON]
    if not candidates:
        return None
    return min(candidates, key=lambda s: abs(s.age - snapshot_age)).season


def is_pre1999_comp(arc: CareerArc, snapshot_age: int) -> bool:
    """True iff the comp's snapshot season is strictly before 1999.

    The haircut is gated on the SNAPSHOT season (the season whose age
    matched the target's), NOT on the comp's overall career window.
    This is intentional: an Emmitt-Smith crossover comp used at a
    1999+ snapshot age (30+) is a modern-data comp and gets no
    haircut; the same player used at a 1995 snapshot age (Emmitt at 26)
    is a pre-1999 comp and DOES get the haircut.
    """
    season = snapshot_season_for_comp(arc, snapshot_age)
    if season is None:
        return False
    return season < PRE1999_SNAPSHOT_CUTOFF


def pre1999_haircut_weight(arc: CareerArc, snapshot_age: int) -> float:
    """Return the multiplicative weight haircut for this comp.

    ``PRE1999_COMP_WEIGHT_HAIRCUT`` (0.9) for pre-1999-snapshot comps,
    1.0 otherwise. Designed to be applied to the raw similarity weight
    BEFORE comp-pool aggregation in ``project_player``.
    """
    return PRE1999_COMP_WEIGHT_HAIRCUT if is_pre1999_comp(arc, snapshot_age) else 1.0


# v3.2 — survivorship-bias fix. Floor on the career-stage-matched comp
# gate. A current player with N completed seasons is comp'd against
# historical players with ≥ max(N, COMP_POOL_MIN_SEASONS) seasons so
# that:
#   - For early-career targets (N ≤ 3), we still require 3-season
#     comps — 1-2 season "played-one-year-then-disappeared" arcs are
#     too thin to project from.
#   - For mid-career targets (N=4..7), the gate widens to N and pulls
#     in the previously-excluded 4-7 season "didn't pan out" pool.
#   - For veterans (N≥8), the gate roughly matches the pre-v3.2
#     LONG_ARC_MIN_SEASONS=8 cutoff — their comp pool is approximately
#     unchanged.
COMP_POOL_MIN_SEASONS = 3

# v3.3 — long-arc relax-on-veterans. Phil's 2026-05-28 brief noted the
# long-arc comp pool "is still low compared to how many players have
# played in the NFL historically." For deep-career targets (9+ seasons),
# the strict "comp must have ≥ N seasons" gate excludes meaningful
# comparables who happened to retire one or two seasons earlier than
# the target. We allow comps to be UP TO ``LONG_ARC_RELAX_SEASONS``
# shorter than the target so a 9-year vet (Henry) can be comped
# against 7-year vets too, broadening the eligible pool significantly
# without dragging in 3-season busts. Floor still respects
# COMP_POOL_MIN_SEASONS.
LONG_ARC_RELAX_SEASONS = 2             # how many shorter-career seasons we accept for 9+yr targets
LONG_ARC_RELAX_TRIGGER_SEASONS = 9     # target career length at which relax kicks in


def find_comps(
    target: CareerArc,
    long_arc_corpus: Sequence[CareerArc],
    target_age: int,
    league_format: str,
    percentile_table: CareerStagePercentileTable,
    k: int = TOP_K_COMPS,
    age_window: int = AGE_WINDOW,
    stage_window: int = CAREER_STAGE_WINDOW,
    target_n_seasons: Optional[int] = None,
) -> List[CompMatch]:
    """Find top-K career-stage-matched comps for ``target``.

    ``target_n_seasons`` (v3.2): the target's number of completed NFL
    seasons. Comp candidates must have ≥ max(target_n_seasons,
    COMP_POOL_MIN_SEASONS) seasons in their arc — i.e. they have
    actually crossed the same career-stage threshold the target has
    reached. When None, falls back to len(target.career_arc).
    """
    tv = build_arc_vector(target, target_age, league_format, percentile_table)
    if tv is None:
        return []

    if target_n_seasons is None:
        target_n_seasons = len(target.career_arc)
    # v3.3: for deep-career veterans, relax the lower bound so the
    # eligible comp pool actually reflects "NFL careers similar in
    # length to this player" rather than "only players who outlasted
    # this player."
    if target_n_seasons >= LONG_ARC_RELAX_TRIGGER_SEASONS:
        min_comp_seasons = max(
            target_n_seasons - LONG_ARC_RELAX_SEASONS,
            COMP_POOL_MIN_SEASONS,
        )
    else:
        min_comp_seasons = max(target_n_seasons, COMP_POOL_MIN_SEASONS)

    candidates: List[CompMatch] = []
    for comp in long_arc_corpus:
        if comp.position != target.position:
            continue
        if comp.player_id == target.player_id:
            continue
        # v3.2 career-stage gate: comp must have at least as many
        # completed seasons as the target (with a 3-season floor).
        if len(comp.career_arc) < min_comp_seasons:
            continue
        # Require that comp actually played past target_age (we need a
        # post-age career to project from).
        if not comp.seasons_after_age(target_age):
            continue
        # Snapshot age: try target_age first; widen by ±age_window if no
        # qualifying season at exactly target_age.
        snapshot_age = None
        for delta in range(0, age_window + 1):
            for sign in (0,) if delta == 0 else (-1, +1):
                candidate_age = target_age + sign * delta
                if any(
                    s.age == candidate_age and s.games >= MIN_GAMES_PER_SEASON
                    for s in comp.career_arc
                ):
                    snapshot_age = candidate_age
                    break
            if snapshot_age is not None:
                break
        if snapshot_age is None:
            continue

        comp_v = build_arc_vector(comp, snapshot_age, league_format, percentile_table)
        if comp_v is None:
            continue

        # Career-stage filter (±stage_window).
        if abs(comp_v.career_stage - tv.career_stage) > stage_window:
            continue

        sim = _weighted_similarity(tv.values, comp_v.values)
        if sim <= 0:
            continue
        # v2.4: bake the pre-1999 confidence haircut INTO the similarity
        # weight here so every downstream consumer (project_player
        # weighted average, compute_survival sim-weighted bust rate,
        # the top-K sort) sees one consistent set of haircut-adjusted
        # weights. ``raw_similarity`` is preserved on the CompMatch
        # for diagnostics; ``similarity`` is the effective weight.
        haircut = pre1999_haircut_weight(comp, snapshot_age)
        adj_sim = sim * haircut
        if adj_sim <= 0:
            continue
        candidates.append(CompMatch(
            arc=comp,
            similarity=adj_sim,
            snapshot_age=snapshot_age,
            raw_similarity=sim,
            pre1999_haircut_applied=haircut < 1.0,
        ))

    candidates.sort(key=lambda m: m.similarity, reverse=True)
    return candidates[:k]


def project_remaining(
    comp: CareerArc,
    age_floor: int,
    league_format: str,
    discount_per_year: float = DISCOUNT_PER_YEAR,
) -> Tuple[float, int]:
    """Sum a comp's realised post-age fantasy points under ``league_format``,
    time-discounting by years out from the snapshot.

    The comp's per-season fp values are ALREADY in modern-era-equivalent
    units (era-pace was applied at corpus build time), so no additional
    era-pace multiplier is needed here.

    v3.8 (Phil 2026-05-29) — if the comp is flagged ``retired_early``,
    the realised arc is extended with SYNTHETIC seasons at the comp's
    final-3yr fp/G rate, 17 games per season, up to
    ``retired_early_extrapolate_to_age``. Synthetic seasons inherit the
    same time-discount as realised seasons. This stops Andrew Luck (who
    walked away at 29 due to injuries) from dragging down Caleb Williams
    just because his arc happens to be short.
    """
    total = 0.0
    n = 0
    last_real_age: Optional[int] = None
    for s in comp.seasons_after_age(age_floor):
        if s.games < MIN_GAMES_PER_SEASON:
            continue
        season_pts = s.fp_total.get(league_format, 0.0)
        season_pts *= (1.0 - discount_per_year) ** n
        total += season_pts
        n += 1
        last_real_age = s.age

    # v3.8 — retired-early extrapolation.
    extrapolate_to = getattr(comp, "retired_early_extrapolate_to_age", None)
    if (
        getattr(comp, "retired_early", False)
        and extrapolate_to is not None
        and extrapolate_to > 0
    ):
        # Last actual age in the arc (whether or not it was post-snapshot).
        last_actual_age = max(
            (s.age for s in comp.career_arc if s.games >= MIN_GAMES_PER_SEASON),
            default=None,
        )
        if last_actual_age is not None and last_actual_age < extrapolate_to:
            # Final-3yr fp/G rate from the player's last 3 qualifying
            # seasons. This is their "healthy at retirement" rate, NOT
            # an all-time peak — we don't want to over-extrapolate.
            tail = [
                s for s in comp.career_arc if s.games >= MIN_GAMES_PER_SEASON
            ][-3:]
            g_tail = sum(s.games for s in tail)
            if g_tail > 0:
                rate = sum(
                    s.fp_per_game.get(league_format, 0.0) * s.games
                    for s in tail
                ) / g_tail
                # Synthesize one season at a time from
                # (last_real_age + 1) [if we projected anything from
                # the realised arc, else age_floor + 1] up to the cap.
                start_age = (last_actual_age + 1)
                # Each synthetic season counts as 17 games at ``rate``.
                synth_pts_per_season = rate * 17
                for age in range(start_age, extrapolate_to + 1):
                    # Skip synthetic seasons that would occur AT OR
                    # BEFORE the snapshot age — we only ever project
                    # forward of the target's current age.
                    if age <= age_floor:
                        continue
                    season_pts = synth_pts_per_season * (
                        (1.0 - discount_per_year) ** n
                    )
                    total += season_pts
                    n += 1
    return total, n


# Games-per-season anchor used by the peak-anchored projection. The NFL
# moved to 17 games in 2021. Pre-2021 seasons are 16 games but we keep 17
# as the projection denominator because all projected seasons are FUTURE
# (modern) seasons.
PROJECTION_GAMES_PER_SEASON = 17

# Discount factor applied to a target's projection when using the
# peak-anchored path. Mirrors the per-season discount applied to comps
# but folded into a single average factor since we anchor on a single
# per-game rate.
PEAK_ANCHORED_DISCOUNT = 0.85   # ~ midpoint discount over projected
                                # remaining seasons

# Position-specific elite-tier thresholds for the peak-anchored path. A
# target's peak-3yr fp/g must clear the position's threshold to qualify
# for the peak-anchored projection (which would otherwise inflate
# non-elite players whose comp pool happens to include a few
# elite-career retired comps). Below the threshold the projection falls
# back to the comp-weighted sum (the brief's literal spec).
#
# Calibration (sf_ppr):
#   QB tier (modern starter+):      peak_3yr >= 17  (Stroud, Tua, Love,
#                                                    Purdy, Lawrence,
#                                                    Burrow, Herbert,
#                                                    Hurts, Allen,
#                                                    Lamar, Mahomes,
#                                                    Daniels, Kyler)
#                                  Below 17 = backup-tier
#   RB tier I:                      peak_3yr >= 15  (Bijan, Gibbs,
#                                                    Saquon, CMC)
#   WR tier I:                      peak_3yr >= 16  (Chase, Jefferson,
#                                                    Nacua, ARSB)
#   TE tier I:                      peak_3yr >= 12  (Bowers, McBride,
#                                                    Kelce-era retired)
#
# Rationale for the QB threshold drop from 20 → 17: at threshold 20 only
# the elite dual-threats qualified, and modern pocket starters (Stroud,
# Tua, Love, Purdy) fell off into comp-weighted-only with very low
# scores. The brief invariant says Stroud / Tua / Love / Purdy /
# Herbert / Burrow all stay top 25. At threshold 17 modern starters
# qualify (their peak fp/g IS legitimate ~17-19 under sf_ppr) while
# bench / spot-starter QBs (Bryce Young peak 14, Drake Maye peak 14)
# correctly stay in comp-weighted-only.
ELITE_PEAK_THRESHOLDS: Dict[str, float] = {
    "QB": 18.0,
    "RB": 15.0,
    "WR": 16.0,
    "TE": 12.0,
}

# Soft-blend window below the elite threshold. Within ``peak3yr in
# [threshold - SOFT_BAND, threshold]`` we interpolate between the
# comp-weighted projection and the peak-anchored projection to avoid a
# cliff at the threshold. Wide enough that modern starting pocket QBs
# (Stroud / Tua / Love peak 16-17) catch a meaningful blend, while
# pure backup QBs (peak < 13) fall through to comp-weighted only.
SOFT_BAND = 5.0


@dataclass
class ProjectionResult:
    projected_remaining_fp: float       # production_score (peak-anchored or comp-weighted)
    projected_remaining_seasons: float
    comp_weighted_fp: float             # raw similarity-weighted comp projection
    peak_anchored_fp: float             # target_peak3yr * games * years * discount
    target_peak_3yr_fp_per_game: float
    n_comps: int
    comps: List[CompMatch]
    # v3.1 — proven-production floor diagnostics. ``proven_floor_fp`` is
    # the banked career fp + a short forward window of the player's own
    # recent rate. ``floor_won`` is True when the floor exceeded both
    # comp_weighted and peak_anchored and was used as projected_remaining_fp.
    proven_floor_fp: float = 0.0
    floor_won: bool = False


def _peak_3yr_target(target: CareerArc, league_format: str) -> float:
    """Peak 3-year average fp/g for the target across COMPLETED seasons."""
    arc = target.career_arc
    n = len(arc)
    if n == 0:
        return 0.0
    best = 0.0
    for i in range(n):
        j = min(n, i + 3)
        window = arc[i:j]
        gtot = sum(s.games for s in window)
        if gtot <= 0:
            continue
        ptot = sum(s.fp_per_game.get(league_format, 0.0) * s.games for s in window)
        avg = ptot / gtot
        if avg > best:
            best = avg
    return best


def _recent_3yr_target(target: CareerArc, league_format: str) -> float:
    """Most-recent 3-year average fp/g for the target. Captures decline
    on aging players (Rodgers' last 3 seasons average 16.7 fp/g even
    though his all-time peak3yr is 24.5)."""
    arc = target.career_arc
    if not arc:
        return 0.0
    recent = arc[-3:]
    gtot = sum(s.games for s in recent)
    if gtot <= 0:
        return 0.0
    ptot = sum(s.fp_per_game.get(league_format, 0.0) * s.games for s in recent)
    return ptot / gtot


def _recent_2yr_target(target: CareerArc, league_format: str) -> float:
    """Most-recent 2-year fp/g for the target — used by the v3.1 proven
    floor as the conservative current-form rate."""
    arc = target.career_arc
    if not arc:
        return 0.0
    recent = arc[-2:]
    gtot = sum(s.games for s in recent)
    if gtot <= 0:
        return 0.0
    ptot = sum(s.fp_per_game.get(league_format, 0.0) * s.games for s in recent)
    return ptot / gtot


def _recent_1yr_target(target: CareerArc, league_format: str) -> float:
    """Most-recent season fp/g for the target (the most current form).
    Used by the v3.1 RB late-career-still-producing boost."""
    arc = target.career_arc
    if not arc:
        return 0.0
    last = arc[-1]
    if last.games <= 0:
        return 0.0
    return last.fp_per_game.get(league_format, 0.0)


def _projection_rate(target: CareerArc, league_format: str) -> float:
    """The fp/g rate used by the peak-anchored projection.

    Blends all-time peak (signals ceiling) with recent-3yr (signals
    current form). For an aging player (Rodgers), recent_3yr is much
    lower than all-time peak — the blend pulls the anchor down. For a
    young rising player (Daniels, Allen at 28), recent ~= peak so the
    blend stays near peak.

    Formula: max(recent_3yr * 1.10, peak_3yr * 0.90)
        - 1.10×recent: small upward bias — we assume current form
          slightly understates what they'll produce next year (one
          down year shouldn't crash the projection).
        - 0.90×peak: a soft floor — a player who sustained an elite
          3-year peak retains most of that ceiling potential. A 10%
          discount acknowledges the typical year-on-year regression.
        - max() of the two: if the player is at peak form (Allen,
          Hurts, Daniels), recent×1.1 dominates. If the player has
          declined recently (Mahomes 2023-24, Rodgers 2024), 0.9×peak
          floors the rate so the engine doesn't over-punish a single
          down year or a brief KC offense rebuild.

    For Rodgers (41yo, 1.5 projected remaining seasons via comp pool),
    even a high anchor rate yields a small total projection because the
    PEAK_ANCHOR_MIN_COMPS=3 filter and small remaining-years multiplier
    keep his total dynasty value low.
    """
    peak = _peak_3yr_target(target, league_format)
    recent = _recent_3yr_target(target, league_format)
    return max(recent * 1.10, peak * 0.90)


# Minimum number of comps required for the peak-anchored projection to
# kick in. Sample-of-1 or sample-of-2 comp pools (typically aging stars
# whose only same-age comp is Brady-at-41) get the comp-weighted
# projection instead.
PEAK_ANCHOR_MIN_COMPS = 3


# v3.8 (Phil 2026-05-29) — age-weighted remaining-career runway.
#
# Phil's Caleb Williams (24) > Justin Fields (27) > Deshaun Watson (30)
# example: younger QBs with comparable production should rank materially
# higher because their runway is longer. The pre-v3.8 comp-pool
# weighted_seasons can be 7-9 for all three QBs because their comp pool
# (Burrow/Manning/etc.) is dominated by mid-career snapshots. The runway
# floor below ensures each player's projection horizon respects the
# years they ACTUALLY have left to age out of the league.
#
# Calibration: typical position retirement ages from PFR /
# pro-football-reference research. Conservatively low so we don't
# inflate borderline-cut players.
POSITION_TYPICAL_RETIREMENT_AGE: Dict[str, int] = {
    "QB": 36,
    "RB": 30,
    "WR": 32,
    "TE": 33,
}
# Minimum runway floor (years remaining at current age). Caps the
# minimum so a hyper-aged player doesn't get zero.
RUNWAY_FLOOR_MIN_YEARS = 1.0
# Blend factor between comp-pool weighted_seasons and runway-floor. The
# runway is a CEILING that we use as a soft floor: the engine blends the
# higher of (weighted_seasons, RUNWAY_BLEND * runway_years). Below 1.0
# this is conservative — we don't fully extend everyone's horizon to
# retirement age, just close some of the gap.
RUNWAY_BLEND = 0.70


def _runway_floor_years(position: str, current_age: int) -> float:
    """Position-typical remaining-career years at ``current_age``.

    Returns ``max(RUNWAY_FLOOR_MIN_YEARS, retirement_age - current_age)
    * RUNWAY_BLEND`` — a conservative floor that prevents young players'
    projection horizons from being arbitrarily capped by the comp pool's
    average snapshot age.
    """
    retire = POSITION_TYPICAL_RETIREMENT_AGE.get(position, 32)
    runway = max(retire - current_age, 0.0)
    return max(RUNWAY_FLOOR_MIN_YEARS, runway * RUNWAY_BLEND)


# v3.1 — proven-production floor parameters.
#
# The floor expresses "dynasty equity" — the floor under what a
# player should be ranked at, given their banked production AND the
# years they have left to keep producing. It has two components:
#
#   banked_component  = career_total_fp × (yrs_rem / DYNASTY_HORIZON)
#                       — banked production discounted by how much
#                         dynasty runway is left. A retiring player
#                         with 1 year remaining can't cash in their
#                         banked production; a mid-prime vet with 5
#                         years left should get most of it as dynasty
#                         credit. Capped at 1.0 so a young rookie's
#                         banked never inflates beyond face value.
#
#   forward_component = recent_2yr_fp_per_game × 17 × min(yrs_rem, 3)
#                       × 0.90 discount
#                       — a SHORT forward window using the player's own
#                         recent form. Bounded to 3 years so we don't
#                         speculate on long runways here — the
#                         peak-anchored / comp-weighted projections
#                         already do that work above.
#
# proven_floor = banked_component + forward_component
#
# A thin-sample young player (Stroud: 719 banked, recent ~14 fp/g) gets
# a small floor (~700 + ~640 = ~1340) and the existing peak-anchored
# projection wins. A heavily-banked veteran in mid-prime (Dak: 2598
# banked at yrs_rem=4.9, recent ~17 fp/g) gets a strong floor and rises.
# A retiring veteran (Stafford: 3985 banked at yrs_rem=2.1) gets the
# banked component heavily discounted (×0.26) so the floor doesn't
# accidentally promote them above active mid-prime stars.
FLOOR_RECENCY_WINDOW = 3        # seasons of forward window
FLOOR_RECENCY_DISCOUNT = 0.90   # avg discount applied to the forward window
FLOOR_DYNASTY_HORIZON = 6.0     # typical remaining-career for banked weighting
# Banked credit also requires the player to still be PRODUCING close
# to their peak. The ``still_producing`` factor tapers linearly from
# 0.0 at recent_2yr/peak3 = 0.55 up to 1.0 at recent_2yr/peak3 = 0.80.
# A retiring QB (Rodgers recent ~14 vs peak 24, ratio 0.60) gets
# ~20% banked credit; an active mid-prime vet (Dak ratio 0.82) gets
# 100%. This keeps banked production from inflating players whose
# dynasty value has already evaporated.
# Position-specific producing-ratio curves. QBs have the longest
# typical careers, so a QB whose recent_2yr/peak3 ratio has fallen
# materially is most likely actually finished as a dynasty asset.
# RB/WR/TE decline more gradually and retain real fantasy value even
# at a 0.55–0.65 ratio of their all-time peak.
FLOOR_PRODUCING_FLOOR_RATIO = {
    "QB": 0.65,
    "RB": 0.50,
    "WR": 0.50,
    "TE": 0.50,
}
FLOOR_PRODUCING_FULL_RATIO = {
    "QB": 0.85,
    "RB": 0.75,
    "WR": 0.75,
    "TE": 0.75,
}


def _proven_production_floor(
    target: CareerArc,
    league_format: str,
    weighted_seasons: float,
) -> float:
    """v3.1 proven-production floor.

    Dynasty equity = banked credit (career_total × yrs_rem-weighted) +
    a short forward window of the player's own recent rate. See the
    FLOOR_* constants for the calibration rationale.
    """
    banked = float(target.career_total_fp.get(league_format, 0.0))
    recent_pg = _recent_2yr_target(target, league_format)
    if recent_pg <= 0.0:
        # No qualifying games in the last 2 seasons; fall back to the
        # 3yr window so we still anchor on real production rather than
        # zero.
        recent_pg = _recent_3yr_target(target, league_format)
    # Banked weight is linear in (yrs_rem / horizon) capped at 1.0, then
    # additionally gated by a ``still_producing`` factor based on the
    # ratio of recent_2yr fp/g to all-time peak_3yr fp/g. A player who
    # has materially fallen off their peak (Rodgers 14.7/24.4 = 0.60)
    # gets only a small slice of their banked credit; a player still
    # at-or-near peak (Dak 17.2/21.0 = 0.82) gets it all. This keeps
    # heavily-banked-but-retiring QBs from inflating into the top tier
    # on past-production credit they can't cash in dynasty.
    raw_weight = min(weighted_seasons / FLOOR_DYNASTY_HORIZON, 1.0)
    raw_weight = max(raw_weight, 0.0)
    peak3 = target.peak_3yr_fp_per_game.get(league_format, 0.0)
    if peak3 > 0:
        producing_ratio = recent_pg / peak3
        floor_r = FLOOR_PRODUCING_FLOOR_RATIO.get(target.position, 0.55)
        full_r = FLOOR_PRODUCING_FULL_RATIO.get(target.position, 0.80)
        denom = full_r - floor_r
        producing_factor = (producing_ratio - floor_r) / denom if denom > 0 else 1.0
        producing_factor = max(0.0, min(producing_factor, 1.0))
    else:
        producing_factor = 1.0
    banked_weight = raw_weight * producing_factor
    banked_component = banked * banked_weight
    forward_window = min(weighted_seasons, FLOOR_RECENCY_WINDOW)
    forward_component = (
        recent_pg * PROJECTION_GAMES_PER_SEASON
        * forward_window * FLOOR_RECENCY_DISCOUNT
    )
    return banked_component + forward_component


def project_player(
    target: CareerArc,
    long_arc_corpus: Sequence[CareerArc],
    target_age: int,
    league_format: str,
    percentile_table: CareerStagePercentileTable,
    k: int = TOP_K_COMPS,
    target_n_seasons: Optional[int] = None,
) -> ProjectionResult:
    """Build a comp-pool-driven projection of the target's remaining career.

    v2.0 produces a HYBRID projection:

        comp_weighted_fp   = similarity-weighted sum of comps' realised
                             post-age fantasy points (modern-era-
                             equivalent, time-discounted 5%/yr). This is
                             the brief's spec verbatim.

        peak_anchored_fp   = target's peak-3yr fp/g × 17 games ×
                             comp-weighted projected years remaining ×
                             average discount. This anchors the
                             projection on what the target has ACTUALLY
                             produced — critical for proven elite
                             producers (Allen, Mahomes, Lamar) whose
                             realised production exceeds the comp pool
                             average.

    We take the MAX of the two so that:
        - Established elite producers can't be dragged down by a
          comp-pool that includes short-career or sample-of-1 retired
          QBs (e.g. Daunte Culpepper post-29 = 232 fp would otherwise
          haircut Allen's projection).
        - Unproven young players still get the comp-pool projection
          (their peak-3yr is small, so max() falls back to comps).

    The result is the dynasty production score for the player.
    """
    comps = find_comps(
        target=target,
        long_arc_corpus=long_arc_corpus,
        target_age=target_age,
        league_format=league_format,
        percentile_table=percentile_table,
        k=k,
        target_n_seasons=target_n_seasons,
    )
    target_peak = _peak_3yr_target(target, league_format)
    projection_rate = _projection_rate(target, league_format)
    if not comps:
        return ProjectionResult(
            projected_remaining_fp=0.0,
            projected_remaining_seasons=0.0,
            comp_weighted_fp=0.0,
            peak_anchored_fp=0.0,
            target_peak_3yr_fp_per_game=target_peak,
            n_comps=0,
            comps=[],
        )
    # v2.4 note: the 0.9× pre-1999 confidence haircut is applied INSIDE
    # ``find_comps`` (the haircut is baked into ``c.similarity`` for
    # pre-1999-snapshot comps), so this comp-pool aggregation already
    # sees haircut-adjusted weights. The same haircut therefore flows
    # consistently into ``compute_survival`` (sim-weighted bust rate)
    # and the top-K sort — every downstream consumer sees one
    # consistent set of weights instead of having to re-apply the haircut.
    total_sim = sum(c.similarity for c in comps) or 1.0
    weighted_pts = 0.0
    weighted_seasons = 0.0
    for c in comps:
        pts, n_seasons = project_remaining(
            c.arc, age_floor=c.snapshot_age, league_format=league_format,
        )
        w = c.similarity / total_sim
        weighted_pts += pts * w
        weighted_seasons += n_seasons * w

    # Peak-anchored projection: projection_rate × 17 games × expected
    # years × discount. The discount approximates a 5%/yr decay over
    # the projected remaining career (geometric mean discount over
    # weighted_seasons ≈ (1 - 0.05)^(weighted_seasons/2)).
    #
    # v3.8 (Phil 2026-05-29) — use the larger of (comp-weighted_seasons,
    # position-typical runway at target_age). Stops young players from
    # being capped at their comp pool's average snapshot horizon when
    # they personally have a longer dynasty window. Doesn't help
    # 30+ players (whose runway floor is lower than their comp horizon).
    runway_floor = _runway_floor_years(target.position, target_age)
    effective_seasons = max(weighted_seasons, runway_floor)
    if (
        weighted_seasons > 0 and projection_rate > 0
        and len(comps) >= PEAK_ANCHOR_MIN_COMPS
    ):
        avg_discount = (1.0 - DISCOUNT_PER_YEAR) ** (effective_seasons / 2.0)
        peak_anchored = (
            projection_rate * PROJECTION_GAMES_PER_SEASON
            * effective_seasons * avg_discount
        )
    else:
        peak_anchored = 0.0

    # v3.3 PROJECTION METHODOLOGY (Phil's brief, 2026-05-28):
    # "The projected remaining fantasy points should be some sort of
    #  weighted average of the comparable players applied to the player.
    #  Apply the new methodology across the entire player base."
    #
    # Comp-weighted is now the PRIMARY projection across all positions and
    # career stages. Peak-anchored is a SOFT BLEND for elite-tier producers
    # (so a Mahomes / Allen / Henry isn't dragged all the way to the
    # comp-pool average), and the blend itself is CAPPED at 1.25× the
    # highest single-comp projected pts — i.e. the engine cannot project
    # a player to exceed what their best historical comp actually did by
    # more than 25%. This directly fixes the Henry case where the player
    # page reported 2,103 remaining fp despite no comp exceeding ~445.
    threshold = ELITE_PEAK_THRESHOLDS.get(target.position, 0.0)
    # Soft top-cap on peak-anchored: don't let the engine extrapolate
    # beyond what comps actually produced post-snapshot.
    comp_projected_pts = []
    for c in comps:
        pts, _n = project_remaining(
            c.arc, age_floor=c.snapshot_age, league_format=league_format,
        )
        comp_projected_pts.append(pts)
    top_comp_proj = max(comp_projected_pts) if comp_projected_pts else 0.0
    # v3.8 (Phil 2026-05-29) — loosen the cap on peak-anchored from 1.25×
    # to 1.50× the top single-comp projected total. The cap exists to
    # prevent fantasy explosions; 1.50× still bounds the projection while
    # giving elite young producers (Caleb Williams, Bo Nix) credit for
    # their actual stats rather than the comp pool's truncated arcs.
    peak_anchored_capped = min(peak_anchored, 1.50 * top_comp_proj) if top_comp_proj > 0 else peak_anchored

    if target_peak >= threshold and peak_anchored_capped > weighted_pts:
        # v3.8 — elite-tier producer with a viable peak anchor: blend
        # 40% comp-weighted + 60% capped peak-anchored. Phil's brief:
        # "the actual stats should be weighted a bit higher than the
        #  similarity scores and their projections...creating undue noise".
        # The shift from v3.3's 60/40 → 40/60 means a young elite
        # producer's own production rate now drives the majority of
        # their projection, with comps as a (still meaningful)
        # contextualising prior.
        projected_fp = 0.40 * weighted_pts + 0.60 * peak_anchored_capped
    elif target_peak >= threshold - SOFT_BAND and peak_anchored_capped > weighted_pts:
        # Soft-band: linearly fade from pure comp-weighted to the 40/60
        # blend (also v3.8 — was 60/40 in v3.3).
        t = (target_peak - (threshold - SOFT_BAND)) / SOFT_BAND
        blended_anchor = 0.40 * weighted_pts + 0.60 * peak_anchored_capped
        projected_fp = (1 - t) * weighted_pts + t * blended_anchor
    else:
        # Default for everyone else: pure comp-weighted (Phil's mandate).
        projected_fp = weighted_pts

    # ------------------------------------------------------------------
    # v3.1 — proven-production floor (see FLOOR_* constants above).
    #
    # Dynasty equity = banked_credit + short forward window. The floor
    # is computed inside the projection for diagnostics but is APPLIED
    # downstream of the v2.2 penalty stack (in similarity_v1.run_engine)
    # so survival / late-breakout penalties can shape the forward
    # projection without undermining the banked-production floor.
    # ------------------------------------------------------------------
    proven_floor = _proven_production_floor(
        target=target,
        league_format=league_format,
        weighted_seasons=weighted_seasons,
    )
    floor_won = proven_floor > projected_fp

    return ProjectionResult(
        projected_remaining_fp=projected_fp,
        projected_remaining_seasons=weighted_seasons,
        comp_weighted_fp=weighted_pts,
        peak_anchored_fp=peak_anchored,
        target_peak_3yr_fp_per_game=target_peak,
        n_comps=len(comps),
        comps=comps,
        proven_floor_fp=proven_floor,
        floor_won=floor_won,
    )
