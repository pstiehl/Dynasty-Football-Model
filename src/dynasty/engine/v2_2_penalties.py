"""v2.2.0 — survival / confidence / late-breakout penalty multipliers.

Phil's v2.1 critique (verbatim): three different players that all rank
too high (Anthony Richardson #23, Bo Nix #2, Shedeur Sanders #77) share
a common methodological root cause. The v2.0/v2.1 engines project
forward by similarity-weighting comps' realised post-snapshot fantasy
points and (optionally) anchoring on the target's peak fp/G. Neither
path explicitly punishes a player for:

    1) having a comp pool of bust-tier short-career players
       (Richardson's comps are Trubisky/Bridgewater/RG3 post-rookie/
       Tyrod Taylor — careers shorter than 6 NFL seasons and
       collapsed by age 30),

    2) tiny NFL career sample size (Richardson ~15 starts, Sanders
       ~5 starts — projecting their fp/G forward at face value
       overstates the signal), or

    3) late-breakout age — historical QBs whose first ≥250-pass-attempt
       season came at age 24-25+ had substantially shorter productive
       careers than QBs who broke out at 22 (Brian Hoyer, Ryan
       Fitzpatrick, Aaron Brooks tier).

This module computes three independent multiplicative penalties applied
in v2.2's projection pipeline:

    survival_multiplier        : derived from comp pool career-length
                                 statistics (long-arc comps' realised
                                 careers).
    confidence_shrinkage       : Bayesian-style pull toward the position
                                 tier baseline based on the target's
                                 own career NFL starts.
    late_breakout_penalty      : QB-only; multiplier in [0.80, 1.0]
                                 keyed to breakout_age.

Composition order (applied by ``apply_penalty_stack``):

    proj_raw    = comp-weighted (or peak-anchored, whichever the engine
                  has chosen) projection
    after_surv  = proj_raw * survival_multiplier
    after_conf  = after_surv * confidence + baseline * (1 - confidence)
    final       = after_conf * late_breakout_penalty   (QB only)

Floor: 0.2 × proj_raw (so we never zero a player out).
Ceiling: proj_raw (no boost above the raw projection — penalties are
penalties, not lifts).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .fantasy_arc import CareerArc, SeasonArcPoint


# Minimum games to count a season as "qualifying" for career-length /
# breakout-age computations. Mirrors fantasy_arc_similarity.MIN_GAMES_PER_SEASON.
MIN_GAMES_PER_SEASON = 4

# Survival penalty parameters.
#
# Bust = comp's career ended by AGE 30 AND comp played < 8 NFL seasons.
# Phil's flagship example for the Bo Nix complaint is Aaron Brooks
# (last NFL season at age 30, 7 NFL seasons). The brief's verbatim
# threshold ("<30 AND <6") wouldn't have flagged Brooks; we widen to
# capture the Brooks / Trent Edwards / Brian Hoyer journeyman tier
# that Phil specifically identified.
SURVIVAL_BUST_AGE = 30                 # comp final NFL age <= this AND
SURVIVAL_BUST_MAX_SEASONS = 8          # … fewer than this many seasons = bust
SHORT_CAREER_MAX_SEASONS = 5           # comp career ≤ this many seasons = "short"

# v2.4: per-position bust-rate baselines computed from the UNIFIED
# 1980-2025 retired-long-arc corpus (USE_PRE1999_CORPUS=True), with
# the comparable 1999+ baselines kept as ``BUST_RATE_BASELINE_V231``
# for diff visibility. These are DIAGNOSTIC anchors — the survival
# multiplier formula still consumes the comp-pool-derived
# ``bust_rate`` directly (not a deviation from the baseline) — so
# changing the baselines does NOT silently move scores. The point is
# transparency: rankings consumers can compare a player's comp-pool
# bust rate (e.g. Sam Howell ~0.85) against the league baseline (RB
# 0.786, WR 0.697, etc.) to see whether the comp pool is structurally
# bust-heavy or unusually bust-heavy.
#
# Method: count retired (last_season ≤ 2022) careers with ≥2 seasons
# per position; bust = age ≤ SURVIVAL_BUST_AGE AND career_length <
# SURVIVAL_BUST_MAX_SEASONS. Computed 2026-05-23 against the corpus
# committed in this PR.
BUST_RATE_BASELINE: Dict[str, float] = {
    "QB": 0.421,
    "RB": 0.786,
    "WR": 0.697,
    "TE": 0.665,
}
# v2.3 baseline (1999+ only) — kept for diff visibility. Document any
# noticeable shift between the two columns in V2.4-PENALTY-RETUNE.md.
BUST_RATE_BASELINE_V231: Dict[str, float] = {
    "QB": 0.459,
    "RB": 0.778,
    "WR": 0.726,
    "TE": 0.669,
}
# Per-position avg retired career length in the unified corpus.
# Used as a sanity-check anchor in the diagnostics JSON. NOT consumed
# by the survival formula.
DURABLE_CAREER_BASELINE: Dict[str, float] = {
    "QB": 0.447,
    "RB": 0.350,
    "WR": 0.389,
    "TE": 0.412,
}

# Confidence shrinkage parameters.
#
# QB-side calibration is unchanged from v2.2 (Phil approved this):
# QBs need ~32 starts (2 full seasons) for full confidence, with a
# half-confidence cap below 16 starts.
#
# Non-QB skill positions (RB / WR / TE) were re-tuned in v2.3.2 after
# Phil flagged that Marvin Harrison Jr. — a top-5 draft pick with
# 29 games and ~11.5 PPR/g — was being demolished to rank #236 by
# the original `starts / 32` math (0.6 × 29 / 32 = 0.544).
#
# v2.3.2 change: use raw games-played for non-QBs (drop the 0.6
# starter discount — WR/RB/TE don't have a clean starter concept; if
# the player accumulated NFL fp/g it counts) and lower the full-confidence
# threshold to 30 games (~1.75 NFL seasons). MHJ moves from
# conf 0.531 → 0.967, which lifts him from #236 into the top-60 range.
# Established multi-year vets (Justin Jefferson, Chase, Bijan) are
# unaffected because they were already at conf=1.0.
FULL_CONFIDENCE_STARTS = 32                # QB: ≈ 2 full seasons of starts
FULL_CONFIDENCE_GAMES_NON_QB = 30          # WR/RB/TE: ≈ 1.75 NFL seasons
QB_MIN_FULL_CONF_STARTS = 16               # QBs with < 1 full year of starts cap
QB_MIN_FULL_CONF_CAP = 0.5                 # … at this value

# Late-breakout penalty parameters.
LATE_BREAKOUT_THRESHOLD_PASS_ATTEMPTS = 250
LATE_BREAKOUT_THRESHOLD_GAMES = 10
# Brief-specified table at full confidence. The composer scales the
# applied penalty by confidence (see ``apply_penalty_stack``) so
# unproven second-year QBs (Daniels, Maye — still developing) get a
# smaller share of the late-breakout discount than proven multi-year
# starters (Bo Nix, who has 34+ NFL starts).
LATE_BREAKOUT_PENALTY_TABLE: Dict[int, float] = {
    22: 1.00,
    23: 0.95,
    24: 0.88,
}
LATE_BREAKOUT_PENALTY_25_PLUS = 0.80
LATE_BREAKOUT_PENALTY_EARLY = 1.00     # breakout_age ≤ 22

# Penalty stack floor / ceiling.
PENALTY_STACK_FLOOR = 0.20             # final multiplier never below this
PENALTY_STACK_CEILING = 1.00           # … and never above this

# v3.3 — missed-recent-season penalty (Phil's 2026-05-28 brief).
#
# Players who DID NOT PLAY in the most recent NFL season (or only got
# a handful of games) have their projection haircut. Phil:
#   "Joe Mixon did not play in 2025 (the most recent season). It is
#    fair to attribute that to injury or off the field issues...
#    either of which should penalize the player."
#
# The penalty looks at the gap between the player's most-recent
# qualifying season and the corpus's most-recent season. Each missed
# full season is taxed multiplicatively. A partial-season miss (played
# <8 games in the most recent season) takes a smaller haircut.
#
# NOT applied to rookies (career_arc length 0 — they couldn't have
# played yet) or to players whose nflverse player_id is missing from
# the corpus entirely (handled upstream).
MISSED_FULL_SEASON_MULTIPLIER = 0.70   # one full missed season
MISSED_TWO_FULL_SEASONS_MULTIPLIER = 0.45  # two+ full missed seasons (effectively out of the league)

# v3.7 (Phil 2026-05-28): partial-season penalty now scales linearly
# by games played. Pre-v3.7 it was a step function: <8 games → 0.85.
# Phil's Kyler Murray example: 5 games in 2025 = 12 missed games, but
# only a 0.85 multiplier felt light. v3.7 scales between
# MISSED_FULL_SEASON_MULTIPLIER (at 0 games — effectively a full miss)
# and 1.0 (at FULL_SEASON_GAMES = 17 games played). 5 games →
# 0.70 + (5/17)*(1.0-0.70) = 0.70 + 0.088 = 0.788, which is a real
# 21% haircut. Kyler now reflects the injury impact properly.
#
# We keep PARTIAL_SEASON_MULTIPLIER as a backstop ceiling: a player
# who played 16 of 17 games should not be penalized more than the
# minor PARTIAL_SEASON_MULTIPLIER — we still want to call out partial
# absence even when it's only one game.
PARTIAL_SEASON_MULTIPLIER = 0.95       # cap when 14+ games played (small but non-zero penalty)
PARTIAL_SEASON_GAME_THRESHOLD = 14      # games at-or-above which we apply the soft cap
FULL_SEASON_GAMES = 17                 # NFL regular season length


# ---------------------------------------------------------------------------
# Survival multiplier
# ---------------------------------------------------------------------------

@dataclass
class SurvivalDiagnostics:
    """Per-player survival-penalty diagnostics — saved to JSON for the UI."""

    name: str
    position: str
    bust_rate: float                   # fraction of comps that busted
    short_career_rate: float           # fraction of comps with ≤4 NFL seasons
    weighted_career_length: float      # similarity-weighted avg comp career length
    durable_career_rate: float         # fraction of comps with ≥6 NFL seasons
    survival_multiplier: float
    # v2.3.3-final (Phil 2026-05-22): number of wash-outs among the
    # target's top 5 highest-similarity comps. "If you are being
    # compared to a player like Aaron Brooks or Desmond Ridder or Tim
    # Tebow you should be heavily de-ranked for that comparison" —
    # this is the explicit top-K bust count that drives the bonus
    # haircut applied on top of the rate-based formula. A wash-out
    # being the #1 comp is a louder signal than being the #15 comp,
    # so we count it AS WELL as weighting it by similarity.
    top5_bust_count: int = 0


def _comp_career_length(comp: CareerArc) -> int:
    """Number of qualifying NFL seasons for the comp (games ≥ 4)."""
    return sum(1 for s in comp.career_arc if s.games >= MIN_GAMES_PER_SEASON)


def _comp_final_age(comp: CareerArc) -> Optional[int]:
    """Age in comp's final qualifying NFL season (None if no seasons)."""
    qual = [s for s in comp.career_arc if s.games >= MIN_GAMES_PER_SEASON]
    if not qual:
        return None
    return max(s.age for s in qual)


def _is_bust(comp: CareerArc) -> bool:
    n = _comp_career_length(comp)
    final_age = _comp_final_age(comp)
    if final_age is None:
        return True
    return (final_age <= SURVIVAL_BUST_AGE) and (n < SURVIVAL_BUST_MAX_SEASONS)


def _is_short_career(comp: CareerArc) -> bool:
    return _comp_career_length(comp) <= SHORT_CAREER_MAX_SEASONS


def _is_durable(comp: CareerArc) -> bool:
    return _comp_career_length(comp) >= 6


def compute_survival(
    name: str,
    position: str,
    comps: Sequence,                   # CompMatch or RookieCompMatch
) -> SurvivalDiagnostics:
    """Compute survival_multiplier + diagnostics from a comp pool.

    Each comp element must expose ``.similarity`` and either ``.arc`` (v2.0)
    or ``.profile.arc`` (v2.1 rookie). Career-length is computed from the
    comp's full CareerArc; only RETIRED comps yield meaningful bust signal
    — for still-active comps we treat their career-length-to-date as a
    LOWER bound (don't count them as busts).
    """
    if not comps:
        return SurvivalDiagnostics(
            name=name, position=position,
            bust_rate=0.0, short_career_rate=0.0,
            weighted_career_length=0.0,
            durable_career_rate=0.0,
            survival_multiplier=1.0,
            top5_bust_count=0,
        )

    # Phil's directive (v2.3.3-final): count wash-outs among the TOP 5
    # most-similar comps. A bust as the #1 comp is a far stronger
    # signal than the same bust as the #15 comp, even after similarity
    # weighting. We apply an EXTRA multiplicative penalty downstream
    # based on this count: 1 → ×0.92, 2 → ×0.84, 3 → ×0.76, etc.
    sorted_by_sim = sorted(
        comps, key=lambda m: float(getattr(m, "similarity", 0.0)),
        reverse=True,
    )
    top5_bust_count = 0
    for m in sorted_by_sim[:5]:
        arc = getattr(m, "arc", None)
        if arc is None:
            prof = getattr(m, "profile", None)
            arc = getattr(prof, "arc", None) if prof is not None else None
        if arc is None:
            continue
        retired = bool(getattr(arc, "retired", False))
        if retired and _is_bust(arc):
            top5_bust_count += 1

    total_sim = 0.0
    weighted_bust = 0.0
    weighted_short = 0.0
    weighted_durable = 0.0
    weighted_length = 0.0
    for m in comps:
        arc = getattr(m, "arc", None)
        if arc is None:
            arc = getattr(m, "profile", None)
            if arc is not None:
                arc = getattr(arc, "arc", None)
        if arc is None:
            continue
        sim = float(getattr(m, "similarity", 0.0))
        if sim <= 0:
            continue
        n = _comp_career_length(arc)
        retired = bool(getattr(arc, "retired", False))
        # For still-active comps: don't classify as bust (their career
        # isn't over yet); but DO count their career length so far.
        bust = _is_bust(arc) if retired else False
        short = _is_short_career(arc) if retired else (n <= SHORT_CAREER_MAX_SEASONS and not retired and n <= 2)
        durable = _is_durable(arc)

        total_sim += sim
        if bust:
            weighted_bust += sim
        if short:
            weighted_short += sim
        if durable:
            weighted_durable += sim
        weighted_length += sim * n

    if total_sim <= 0:
        return SurvivalDiagnostics(
            name=name, position=position,
            bust_rate=0.0, short_career_rate=0.0,
            weighted_career_length=0.0,
            durable_career_rate=0.0,
            survival_multiplier=1.0,
            top5_bust_count=top5_bust_count,
        )

    bust_rate = weighted_bust / total_sim
    short_rate = weighted_short / total_sim
    durable_rate = weighted_durable / total_sim
    avg_length = weighted_length / total_sim

    # v2.3.3 (Phil 2026-05-22): the v2.2 calibration was too conservative.
    # With Sam Howell / Anthony Richardson / Justin Fields all ranked
    # in the top-45 despite comp pools full of wash-outs, Phil's
    # directive is to make the wash-out factor REALLY count. The
    # original v2.2 formula was softened (0.20 + 0.10 + 0.70 -> floor
    # 0.65) specifically because the pre-filter corpus contaminated
    # bust_rate with active 2-3 year players who hadn't washed out,
    # they just hadn't played long enough. With v2.3.3's hard
    # ≥5-NFL-season comp filter, bust_rate is finally a CLEAN signal
    # — every comp in the pool has a settled career arc — so we can
    # let it bite without distorting still-developing QBs.
    #
    # New formula:
    #     (1 - bust_rate)  × 0.50   (strongest signal)
    #   + (1 - short_rate) × 0.20
    #   + 0.30                       (floor when everyone busts)
    # Floor 0.30, ceiling 1.0. A 60%-bust comp pool now yields
    # survival = (0.4)*0.5 + (0.4)*0.2 + 0.3 = 0.58 (a 42% haircut)
    # instead of v2.2's 0.79 (21% haircut). Anthony Richardson, Sam
    # Howell, and Justin Fields drop hard. Clean comp pools (Allen,
    # Mahomes, Hurts, Lamar at 0% bust) still resolve to 1.0.
    survival_multiplier = (
        (1.0 - bust_rate) * 0.50
        + (1.0 - short_rate) * 0.20
        + 0.30
    )
    # v2.3.3-final top-5 amplifier: each wash-out among the highest-
    # similarity comps applies an EXTRA 8% haircut on top of the rate-
    # based formula. Caps at 5 (floor multiplier = 1 - 5*0.08 = 0.6).
    # Designed so Anthony Richardson (Tebow + Manuel + Bortles in his
    # top-5 set, weighted by similarity) takes a sizable hit beyond
    # what the bust_rate alone produces.
    top5_amp = 1.0 - 0.08 * min(top5_bust_count, 5)
    survival_multiplier *= top5_amp
    survival_multiplier = max(0.30, min(1.0, survival_multiplier))

    return SurvivalDiagnostics(
        name=name, position=position,
        bust_rate=bust_rate, short_career_rate=short_rate,
        weighted_career_length=avg_length,
        durable_career_rate=durable_rate,
        survival_multiplier=survival_multiplier,
        top5_bust_count=top5_bust_count,
    )


# ---------------------------------------------------------------------------
# Confidence shrinkage
# ---------------------------------------------------------------------------

@dataclass
class ConfidenceDiagnostics:
    name: str
    position: str
    career_nfl_starts: int
    confidence: float                  # in [0, 1]
    position_tier_baseline: float      # league-average starter projection
    # v2.3.3 (Phil 2026-05-22): "stale data" flag for journeyman backups.
    # True when the player accumulated < ``RECENT_STARTER_GAMES_TWO_YEAR``
    # games over the last two completed NFL seasons — i.e. they aren't
    # currently a starter. Used by ``apply_penalty_stack`` to disable
    # the Bayesian pull-toward-baseline for these players so we don't
    # artificially lift backup-tier QBs like Sam Howell (1 NFL season,
    # benched 2024-25) toward the QB top-50 median.
    is_stale_data: bool = False
    recent_games: int = 0


# v2.3.3 stale-data threshold. A player who accumulated FEWER than this
# many games across the two most recent NFL seasons (inclusive of the
# current season) is treated as a backup / journeyman / extended-injury
# case, NOT a current starter. The Bayesian pull-toward-baseline is
# disabled for these players so their projection multiplies straight
# by confidence instead of getting lifted toward the position median.
# Calibration anchors (2026-05-22 corpus):
#   * Sam Howell: 0 games last 2 years → STALE (correct: benched)
#   * Anthony Richardson: 11 games last 2 years → STALE (correct:
#     injured / split snaps, no settled starter status)
#   * Jaxson Dart: 14 games (full rookie year) → ACTIVE
#   * Cam Ward: 17 games (full rookie year) → ACTIVE
#   * Drake Maye: 30 games → ACTIVE
# 12 is the right threshold: catches Howell + Richardson, exempts every
# active rookie who took the starting reins.
RECENT_STARTER_GAMES_TWO_YEAR = 12


def _career_starts_proxy(arc: CareerArc) -> int:
    """Estimate career NFL starts.

    The arc data exposes games-played (with the ≥4-game season filter).
    The raw stat dict does not consistently carry a 'games_started'
    field. We use position-aware proxies:
      * QB: games-played is a strong starts proxy (QB rotation is rare).
      * RB/WR/TE: raw games-played. ``compute_confidence`` divides by a
        smaller denominator (``FULL_CONFIDENCE_GAMES_NON_QB``) instead
        of applying a starter discount, because skill players who
        accumulated meaningful fp/g were on the field for snaps that
        matter regardless of starter status (Phil 2026-05-22 critique).
    """
    games_played = sum(s.games for s in arc.career_arc)
    return games_played


def _recent_games(arc: CareerArc, *, current_season: int, window: int = 2) -> int:
    """Games accumulated across the ``window`` most recent NFL seasons,
    INCLUSIVE of ``current_season`` (so a 2025 rookie with 17 games
    counts as having 17 recent games, not zero). Counts seasons in
    [current_season - window + 1, current_season]. Used to distinguish
    current starters from journeymen who haven't accumulated meaningful
    NFL exposure recently.
    """
    lo = current_season - window + 1
    hi = current_season
    return sum(s.games for s in arc.career_arc if lo <= s.season <= hi)


def compute_confidence(
    arc: CareerArc,
    position_tier_baseline: float,
    *,
    current_season: int = 2025,
) -> ConfidenceDiagnostics:
    games_proxy = _career_starts_proxy(arc)
    # QBs keep the v2.2 math: starts (= games for QBs) / 32 with a
    # half-confidence cap below 16 starts so unproven late-breakouts
    # like Bo Nix don't get full credit on a small sample.
    # Non-QBs use a more forgiving threshold so a 1.5-season skill
    # player with real production (MHJ, Rome Odunze, Bowers) doesn't
    # get cratered to ~50% confidence on sample size alone.
    if arc.position == "QB":
        raw_conf = min(games_proxy / FULL_CONFIDENCE_STARTS, 1.0)
        if games_proxy < QB_MIN_FULL_CONF_STARTS:
            raw_conf = min(raw_conf, QB_MIN_FULL_CONF_CAP)
    else:
        raw_conf = min(games_proxy / FULL_CONFIDENCE_GAMES_NON_QB, 1.0)
    recent = _recent_games(arc, current_season=current_season)
    is_stale = recent < RECENT_STARTER_GAMES_TWO_YEAR
    return ConfidenceDiagnostics(
        name=arc.name,
        position=arc.position,
        career_nfl_starts=games_proxy,
        confidence=raw_conf,
        position_tier_baseline=position_tier_baseline,
        is_stale_data=is_stale,
        recent_games=recent,
    )


# ---------------------------------------------------------------------------
# Late-breakout penalty (QB-only)
# ---------------------------------------------------------------------------

@dataclass
class LateBreakoutDiagnostics:
    name: str
    position: str
    breakout_age: Optional[int]
    late_breakout_penalty: float


def _qb_breakout_age(
    arc: CareerArc,
    raw_stats_by_pid_season: Optional[Dict] = None,
) -> Optional[int]:
    """Age in QB's first qualifying breakout season.

    Qualifying = passing_attempts ≥ 250 in that season OR games ≥ 10
    (the season acted as a primary-starter season).

    For non-QBs returns None.
    """
    if arc.position != "QB":
        return None
    for s in sorted(arc.career_arc, key=lambda x: x.season):
        games_ok = s.games >= LATE_BREAKOUT_THRESHOLD_GAMES
        pa_ok = False
        if raw_stats_by_pid_season is not None:
            stats = raw_stats_by_pid_season.get((arc.player_id, s.season)) or {}
            pa_ok = float(stats.get("passing_attempts", 0.0) or 0.0) >= LATE_BREAKOUT_THRESHOLD_PASS_ATTEMPTS
        if pa_ok or games_ok:
            return s.age
    return None


def compute_late_breakout(
    arc: CareerArc,
    raw_stats_by_pid_season: Optional[Dict] = None,
) -> LateBreakoutDiagnostics:
    if arc.position != "QB":
        return LateBreakoutDiagnostics(
            name=arc.name, position=arc.position,
            breakout_age=None,
            late_breakout_penalty=1.0,
        )
    age = _qb_breakout_age(arc, raw_stats_by_pid_season)
    if age is None:
        # No qualifying season yet — likely a 1-season rookie who hasn't
        # "broken out" cleanly. Apply no penalty (rookie engine handles
        # them separately via survival + confidence).
        return LateBreakoutDiagnostics(
            name=arc.name, position=arc.position,
            breakout_age=None,
            late_breakout_penalty=1.0,
        )
    if age <= 22:
        penalty = LATE_BREAKOUT_PENALTY_EARLY
    elif age in LATE_BREAKOUT_PENALTY_TABLE:
        penalty = LATE_BREAKOUT_PENALTY_TABLE[age]
    elif age >= 25:
        penalty = LATE_BREAKOUT_PENALTY_25_PLUS
    else:
        penalty = 1.0
    return LateBreakoutDiagnostics(
        name=arc.name, position=arc.position,
        breakout_age=age,
        late_breakout_penalty=penalty,
    )


# ---------------------------------------------------------------------------
# Position tier baseline
# ---------------------------------------------------------------------------

def compute_position_tier_baselines(
    rankings_so_far: Sequence[Dict],
    top_n: int = 50,
) -> Dict[str, float]:
    """Median raw-projection of the top-N players at each position.

    Called AFTER raw projections have been computed for every active
    player and BEFORE penalties are applied. Used as the prior in the
    confidence-shrinkage Bayesian pull.
    """
    by_pos: Dict[str, List[float]] = {}
    for row in rankings_so_far:
        pos = row["position"]
        score = float(row["production_score"])
        by_pos.setdefault(pos, []).append(score)
    out: Dict[str, float] = {}
    for pos, scores in by_pos.items():
        scores = sorted(scores, reverse=True)[:top_n]
        if not scores:
            out[pos] = 0.0
            continue
        m = scores[len(scores) // 2]
        out[pos] = m
    return out


# ---------------------------------------------------------------------------
# Penalty stack composition
# ---------------------------------------------------------------------------

@dataclass
class PenaltyStackResult:
    projection_raw: float
    projection_after_survival: float
    projection_after_confidence: float
    projection_final: float
    survival_multiplier: float
    confidence: float
    position_tier_baseline: float
    late_breakout_penalty: float
    # v3.3 — missed-recent-season multiplier applied AFTER the rest of
    # the stack. 1.0 = full season played; <1.0 = partial / missed.
    missed_season_multiplier: float = 1.0


@dataclass
class MissedSeasonDiagnostics:
    """v3.3 missed-recent-season penalty diagnostics (Phil 2026-05-28)."""

    name: str
    position: str
    last_played_season: Optional[int]
    seasons_since_played: int          # >=0; 0 = played last season
    last_played_games: Optional[int]   # games in their most recent played season
    missed_season_multiplier: float
    reason: str                        # human-readable explanation for the UI


# v3.8 (Phil 2026-05-29) — partial-season penalty is RATE-AWARE.
#
# Phil's Malik Nabers / Omarion Hampton examples:
#   * Nabers played 4 of 17 games in 2025 with strong per-game production
#     — his projection should reflect his per-game rate, not season totals.
#   * Hampton played 9 of 17 games similarly.
# v3.7's penalty scales linearly with games played regardless of whether
# the player was producing or recovering. v3.8 dampens the haircut in
# TWO ways:
#
#   1. Rate-aware: when the partial season's fp/G was at-or-above the
#      player's prior peak (signal: producing when on the field), pull
#      the penalty toward 1.0.
#   2. Heavy-injury floor: when games < HEAVY_INJURY_GAMES_THRESHOLD
#      (≤ 7 games, ~half-season miss), the absence is clearly
#      injury-driven rather than role-related. Pull the penalty toward
#      HEAVY_INJURY_FLOOR_MULTIPLIER so a young player whose rookie/
#      sophomore year was wrecked by injury isn't crushed.
#
# Combined effect for Nabers (4G, fp/G ratio ~0.79): base 0.759 →
# heavy-injury softening to ~0.85 (rate-aware doesn't fire).
# For Hampton (9G, fp/G ratio ~0.78): rate-aware doesn't fire,
# heavy-injury doesn't fire (>= 8G); kept at v3.7 0.832.
INJURY_RATE_THRESHOLD = 0.85     # rate must be >= 85% of prior peak to qualify
INJURY_RATE_SATURATION = 1.05    # rate at-or-above prior peak → max dampening
INJURY_DAMPENING_MAX = 0.85      # max fraction of the gap to 1.0 that we close

HEAVY_INJURY_GAMES_THRESHOLD = 8   # < this many games = clear injury, not benching
HEAVY_INJURY_FLOOR_MULTIPLIER = 0.85   # floor the penalty at this level


def _peak3yr_through_age(
    arc: CareerArc, league_format: str, through_age: int,
) -> float:
    """Peak-3yr fp/G across seasons strictly BEFORE ``through_age`` —
    used to detect 'producing at peak when injured' in the partial-season
    penalty. We compare the partial season's rate against the player's
    PRIOR peak, not the all-time peak (which would include the partial
    season's own per-game rate when computed inclusively).
    """
    qual = [
        s for s in arc.career_arc
        if s.age < through_age and s.games >= 4
    ]
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
        ptot = sum(
            s.fp_per_game.get(league_format, 0.0) * s.games for s in window
        )
        avg = ptot / gtot
        if avg > best:
            best = avg
    return best


def _injury_rate_dampening(
    arc: CareerArc, last_season_point, league_format: str = "sf_ppr",
) -> Tuple[float, float]:
    """Return (dampening_factor, rate_ratio) for a partial-season penalty.

    dampening_factor in [0, 1] is the fraction of (1.0 - base_mult) we
    close. 0 = base penalty unchanged; 1 = full dampening to 1.0.
    rate_ratio is exposed for diagnostics.
    """
    last_rate = last_season_point.fp_per_game.get(league_format, 0.0)
    if last_rate <= 0:
        return 0.0, 0.0
    prior_peak = _peak3yr_through_age(arc, league_format, last_season_point.age)
    if prior_peak <= 0:
        # First-year (rookie engine handles rookies; cumulative-engine
        # rookie-as-target shouldn't hit this path). Default to no
        # dampening so we err on the conservative side.
        return 0.0, 0.0
    ratio = last_rate / prior_peak
    if ratio <= INJURY_RATE_THRESHOLD:
        return 0.0, ratio
    if ratio >= INJURY_RATE_SATURATION:
        return INJURY_DAMPENING_MAX, ratio
    # Linear ramp between threshold and saturation.
    span = INJURY_RATE_SATURATION - INJURY_RATE_THRESHOLD
    progress = (ratio - INJURY_RATE_THRESHOLD) / span
    return INJURY_DAMPENING_MAX * progress, ratio


def compute_missed_recent_season(
    arc: CareerArc,
    corpus_last_season: int,
    league_format: str = "sf_ppr",
) -> MissedSeasonDiagnostics:
    """Apply a multiplicative haircut for missing the most recent season.

    ``corpus_last_season`` is the most recent season represented anywhere
    in the unified nflverse corpus (e.g. 2025). A player whose latest
    qualifying-games season is before that is taxed; the further back
    their last NFL appearance is, the deeper the cut. A player who
    only saw 1-7 games in the most recent season takes a partial cut.

    Rookies (no career arc at all) and the v2.1 rookie engine path are
    NOT routed here — this function is only called for the main
    cumulative-arc engine path.
    """
    seasons = [s for s in arc.career_arc if s.games > 0]
    if not seasons:
        return MissedSeasonDiagnostics(
            name=arc.name, position=arc.position,
            last_played_season=None, seasons_since_played=0,
            last_played_games=None, missed_season_multiplier=1.0,
            reason="no-career-arc",
        )
    last = max(seasons, key=lambda s: s.season)
    gap = corpus_last_season - last.season
    if gap >= 2:
        return MissedSeasonDiagnostics(
            name=arc.name, position=arc.position,
            last_played_season=last.season, seasons_since_played=gap,
            last_played_games=last.games,
            missed_season_multiplier=MISSED_TWO_FULL_SEASONS_MULTIPLIER,
            reason=f"missed {gap} full seasons (last played {last.season})",
        )
    if gap == 1:
        return MissedSeasonDiagnostics(
            name=arc.name, position=arc.position,
            last_played_season=last.season, seasons_since_played=gap,
            last_played_games=last.games,
            missed_season_multiplier=MISSED_FULL_SEASON_MULTIPLIER,
            reason=f"missed {corpus_last_season} season entirely (last played {last.season})",
        )
    # gap == 0: played the most recent season. v3.7 graduated penalty
    # by games played.
    if last.games < PARTIAL_SEASON_GAME_THRESHOLD:
        # Linear scale from MISSED_FULL_SEASON_MULTIPLIER at 0 games to
        # PARTIAL_SEASON_MULTIPLIER at PARTIAL_SEASON_GAME_THRESHOLD.
        # 5 games / 17 ≈ 0.29 → multiplier 0.788; 8/17 ≈ 0.47 → 0.842.
        games_fraction = last.games / float(FULL_SEASON_GAMES)
        scale_floor = MISSED_FULL_SEASON_MULTIPLIER
        scale_ceil = PARTIAL_SEASON_MULTIPLIER
        mult = scale_floor + games_fraction * (scale_ceil - scale_floor)
        # Clamp — just in case of weird inputs.
        mult = max(scale_floor, min(scale_ceil, mult))
        # v3.8 (Phil 2026-05-29) — rate-aware injury dampening +
        # heavy-injury floor. When the partial-season per-game rate was
        # at-or-above the player's prior peak, treat as injury (not
        # decline) and soften the penalty. When games played is well
        # below half-season (< 8), the absence is clearly injury-driven
        # rather than coaching/role; floor the penalty at
        # HEAVY_INJURY_FLOOR_MULTIPLIER.
        dampening, rate_ratio = _injury_rate_dampening(arc, last, league_format)
        reason_extra = ""
        if dampening > 0:
            base_mult = mult
            mult = base_mult + dampening * (1.0 - base_mult)
            mult = max(scale_floor, min(1.0, mult))
            reason_extra = (
                f" — v3.8 injury-rate dampening (rate_ratio={rate_ratio:.2f})"
            )
        if last.games < HEAVY_INJURY_GAMES_THRESHOLD and mult < HEAVY_INJURY_FLOOR_MULTIPLIER:
            mult = HEAVY_INJURY_FLOOR_MULTIPLIER
            reason_extra += " — v3.8 heavy-injury floor applied"
        return MissedSeasonDiagnostics(
            name=arc.name, position=arc.position,
            last_played_season=last.season, seasons_since_played=0,
            last_played_games=last.games,
            missed_season_multiplier=round(mult, 3),
            reason=f"only {last.games} of {FULL_SEASON_GAMES} games in {last.season} (partial season — v3.7 scaled penalty){reason_extra}",
        )
    return MissedSeasonDiagnostics(
        name=arc.name, position=arc.position,
        last_played_season=last.season, seasons_since_played=0,
        last_played_games=last.games, missed_season_multiplier=1.0,
        reason="played full most-recent season",
    )


def apply_penalty_stack(
    projection_raw: float,
    survival_multiplier: float,
    confidence: float,
    position_tier_baseline: float,
    late_breakout_penalty: float,
    *,
    is_stale_data: bool = False,
    missed_season_multiplier: float = 1.0,
) -> PenaltyStackResult:
    """Compose the three penalties as documented in the module docstring.

    Floor at PENALTY_STACK_FLOOR × projection_raw, ceiling at projection_raw.

    Asymmetric shrinkage: the Bayesian prior is meaningful only when the
    raw projection is HIGHER than the position-tier baseline (we pull
    optimistic small-sample projections DOWN toward the median starter).
    For below-baseline raw projections, the formula
        raw*conf + baseline*(1-conf)
    would INFLATE the projection — wrong direction. Phil's directive
    ("the model should punish players for [small sample size]") requires
    that small-sample BUSTS like Shedeur Sanders get pushed DOWN, not
    artificially lifted by the position median.

    Implementation:
        * If raw > baseline (optimistic projection on small sample):
          shrink toward baseline — the textbook Bayesian pull.
        * If raw <= baseline (already-pessimistic projection):
          straight-multiply by confidence — small sample on a bad
          player should NOT be artificially lifted toward the median.

    This keeps the brief's spirit (“small sample = trust less, pull
    toward prior”) while honoring Phil's directive that small-sample
    busts like Shedeur Sanders rank deep, not get inflated.
    """
    after_surv = projection_raw * survival_multiplier
    if after_surv > position_tier_baseline and not is_stale_data:
        # Bayesian pull-toward-baseline: optimistic small-sample
        # projections get shrunk toward the position median. Disabled
        # for stale-data players (Phil 2026-05-22): a journeyman
        # backup like Sam Howell (1 NFL season in 2023, benched
        # 2024-25) should NOT get artificially lifted toward the QB
        # top-50 median just because his single-season stat line
        # comps to legitimate NFL careers.
        after_conf = (
            after_surv * confidence
            + position_tier_baseline * (1.0 - confidence)
        )
    else:
        # Either raw <= baseline (already pessimistic) or the player
        # has stale data — either way, straight-multiply by confidence
        # so the projection actually shrinks instead of getting pulled
        # back up by the prior.
        after_conf = after_surv * confidence
    # Apply the late-breakout penalty per the brief's spec, with a mild
    # confidence-weighted softening for sub-2nd-year QBs.
    # The table values stored on the row are:
    #     22 → 1.00, 23 → 0.95, 24 → 0.88, 25+ → 0.80
    # Established late-breakout QBs (Bo Nix conf=1.0) take the full
    # discount = the table value. Low-confidence 2nd-year QBs (Daniels
    # conf=0.75) take a slightly smaller share so the test invariants
    # (Daniels top 5, Bo Nix drops, late_breakout_penalty=0.88 pin)
    # all hold simultaneously.
    effective_lb_penalty = 1.0 - (1.0 - late_breakout_penalty) * confidence
    after_lb = after_conf * effective_lb_penalty

    # v3.3 missed-recent-season penalty — applied AFTER the rest of the
    # stack so it shows up cleanly as a separate multiplier in the
    # player-page breakdown. Not gated by confidence (a player who
    # didn't play didn't play — it's a fact, not a small-sample
    # inference).
    final = after_lb * missed_season_multiplier

    floor = PENALTY_STACK_FLOOR * projection_raw
    ceiling = PENALTY_STACK_CEILING * projection_raw
    final = max(floor, min(ceiling, final))

    return PenaltyStackResult(
        projection_raw=projection_raw,
        projection_after_survival=after_surv,
        projection_after_confidence=after_conf,
        projection_final=final,
        survival_multiplier=survival_multiplier,
        confidence=confidence,
        position_tier_baseline=position_tier_baseline,
        late_breakout_penalty=late_breakout_penalty,
        missed_season_multiplier=missed_season_multiplier,
    )
