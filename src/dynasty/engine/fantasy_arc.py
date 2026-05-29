"""Fantasy-point arc corpus (v2.0.0).

This is the heart of the v2.0 methodology rewrite. Instead of vectorising
players by their per-stat z-score shape (v1.x), we represent every
player-season as the FANTASY POINTS PRODUCED under modern scoring rules,
after era-pace adjusting the raw stat line to the current era.

Why it matters: a player producing 26 fp/G under sf_ppr is fantasy-elite
regardless of HOW they get there. Josh Allen's "passing yards z-score" looks
modest because his raw passing volume isn't elite — but his fantasy points
per game (~28 at peak under sf_ppr) is top-tier in NFL history when you
account for rushing TDs scoring 6 pts. v1.x's stat-shape z-scoring buried
that advantage. v2.0 measures it directly.

Pipeline:

    1.  Load the long-arc corpus from ``player_stats_season.csv.gz``
        (1980-present; nflverse actually starts 1999 but we keep the
        1980+ floor in case the corpus gets back-filled).
    2.  For each player-season, multiply the raw stat line by
        era_pace.era_for_season → era 4 multipliers from
        ``era_pace.build_era_pace_table`` (corpus-derived) so a 2010
        Peyton Manning passing-yards total is normalised to
        "what would these stats look like in 2024".
    3.  For each supported scoring format (sf_ppr, 1qb_ppr, 2qb_ppr,
        half_ppr, std, sf_te_premium), apply ``LEAGUE_SCORING`` to the
        era-adjusted stat line to get
        ``total_season_fantasy_points`` and
        ``fp_per_game = total / games``.
    4.  Persist as ``data/engine_v2/fantasy_arc_corpus.json`` with a
        structure that supports fast lookup by (player_id, age, format).

The fantasy_arc_similarity module (v2.0) builds its 10-dim KNN vector
directly from this corpus, in RAW fantasy-point units. No z-scoring.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .era_pace import EraPaceTable, era_for_season, fallback_table
from ..scoring_rules import LEAGUE_SCORING, TE_PREMIUM_BONUS_PER_REC

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OUT_ROOT = Path("data/engine_v2")

# The scoring formats we pre-compute fantasy arcs for. The KNN engine reads
# from this set. New formats added here become available to format_overlay
# automatically.
SUPPORTED_FORMATS: Tuple[str, ...] = (
    "sf_ppr", "1qb_ppr", "2qb_ppr", "half_ppr", "std", "sf_te_premium",
)

# 2QB has identical per-stat-line scoring to sf_ppr — it diverges only at
# the roster/replacement-baseline layer in format_overlay.
_FORMAT_SCORING_ALIASES = {
    "2qb_ppr": "sf_ppr",
}

# Coarse stat categories — used for diagnostic display only (the similarity
# vector does NOT split by category).
_CATEGORY_STATS: Dict[str, Tuple[str, ...]] = {
    "passing":   ("passing_yards", "passing_tds", "interceptions"),
    "rushing":   ("rushing_yards", "rushing_tds"),
    "receiving": ("receptions", "receiving_yards", "receiving_tds"),
}


# ---------------------------------------------------------------------------
# Per-season + per-career arc representation
# ---------------------------------------------------------------------------

@dataclass
class SeasonArcPoint:
    """One year on a player's fantasy-point arc.

    All ``fp_*`` values are EXPRESSED IN MODERN-ERA-EQUIVALENT FANTASY POINTS:
    the raw stat line has already been multiplied by the era-pace table
    before scoring. So a 1999 Peyton Manning season's fp_per_game_sf_ppr is
    "what would Manning's 1999 season produce if it happened today, scored
    under sf_ppr".
    """

    season: int
    age: int
    games: int
    era: int
    # Map format -> total / per-game fantasy points (era-adjusted).
    fp_total: Dict[str, float] = field(default_factory=dict)
    fp_per_game: Dict[str, float] = field(default_factory=dict)
    # Map (format, category) -> fp share for diagnostic display.
    fp_by_category: Dict[Tuple[str, str], float] = field(default_factory=dict)


@dataclass
class CareerArc:
    """A player's full fantasy-point arc across all completed seasons.

    ``career_arc`` is sorted chronologically. Long-arc-but-still-active
    players have only their COMPLETED seasons in ``career_arc`` — the
    in-progress season is excluded so the comp pool never leaks.
    """

    player_id: str
    name: str
    position: str
    last_season: Optional[int]
    rookie_season: Optional[int]
    retired: bool
    is_long_arc: bool
    career_arc: List[SeasonArcPoint] = field(default_factory=list)

    # Pre-computed totals/peaks per format (filled at build time).
    career_total_fp: Dict[str, float] = field(default_factory=dict)
    peak_season_fp_per_game: Dict[str, float] = field(default_factory=dict)
    peak_3yr_fp_per_game: Dict[str, float] = field(default_factory=dict)
    career_avg_fp_per_game: Dict[str, float] = field(default_factory=dict)

    # v3.8 (Phil 2026-05-29) — retired-early flag. When True, this comp
    # left the NFL due to injury/health rather than talent decline. The
    # projection engine treats their realised arc as TRUNCATED and
    # extrapolates remaining years at their final-3yr fp/G rate so they
    # don't drag down young targets that comp to them (Phil's Andrew
    # Luck → Caleb Williams worked example).
    retired_early: bool = False
    # Position-typical retirement-age cap for the extrapolation. None
    # if not flagged. Engine extends synthetic seasons up to this age.
    retired_early_extrapolate_to_age: Optional[int] = None

    # ------------------------------------------------------------------
    def seasons_through_age(self, age_cap: int) -> List[SeasonArcPoint]:
        return [s for s in self.career_arc if s.age <= age_cap]

    def seasons_after_age(self, age_floor: int) -> List[SeasonArcPoint]:
        return [s for s in self.career_arc if s.age > age_floor]

    def games_through_age(self, age_cap: int) -> int:
        return sum(s.games for s in self.seasons_through_age(age_cap))

    def games_total(self) -> int:
        return sum(s.games for s in self.career_arc)


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _era_adjusted_stat(
    pace: EraPaceTable, position: str, stat: str, era_from: int, raw: float,
) -> float:
    """Multiply a raw stat by the (position, stat, era_from→4) multiplier.

    Stats without an era-pace entry (e.g. receptions for a QB) pass through
    unchanged.
    """
    return raw * pace.get(position, stat, era_from)


def _score_era_adjusted_season(
    season_stats: Mapping[str, float],
    games: int,
    position: str,
    era: int,
    pace: EraPaceTable,
    league_format: str,
) -> Tuple[float, float, Dict[str, float]]:
    """Compute (total_fp, fp_per_game, fp_by_category) for one season under
    a given scoring format, after era-pace adjusting the raw stats.

    The fp_by_category dict is keyed by coarse category ('passing',
    'rushing', 'receiving') and only contains entries with non-zero
    contribution. Used for diagnostic display only.
    """
    scoring_key = _FORMAT_SCORING_ALIASES.get(league_format, league_format)
    coefs = LEAGUE_SCORING.get(scoring_key) or LEAGUE_SCORING["sf_ppr"]

    total = 0.0
    by_category: Dict[str, float] = defaultdict(float)
    # Walk the scoring coefficients; era-adjust the raw stat; apply weight.
    for stat, weight in coefs.items():
        raw = float(season_stats.get(stat, 0.0))
        if raw == 0.0:
            continue
        adj = _era_adjusted_stat(pace, position, stat, era, raw)
        pts = adj * weight
        total += pts
        # Attribute to a coarse category for diagnostics.
        for cat, stats in _CATEGORY_STATS.items():
            if stat in stats:
                by_category[cat] += pts
                break

    # TE premium reception bonus.
    if league_format == "sf_te_premium" and position == "TE":
        recs = float(season_stats.get("receptions", 0.0))
        adj = _era_adjusted_stat(pace, position, "receptions", era, recs)
        bonus = adj * TE_PREMIUM_BONUS_PER_REC
        total += bonus
        by_category["receiving"] += bonus

    fp_per_game = total / max(games, 1)
    return total, fp_per_game, dict(by_category)


def _peak_3yr_avg(arc: Sequence[SeasonArcPoint], league_format: str) -> float:
    """Best rolling-3-season weighted fp/game (weighted by games)."""
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


def _career_avg_fp_per_game(arc: Sequence[SeasonArcPoint], league_format: str) -> float:
    gtot = sum(s.games for s in arc)
    if gtot <= 0:
        return 0.0
    ptot = sum(s.fp_per_game.get(league_format, 0.0) * s.games for s in arc)
    return ptot / gtot


def _peak_single_season_fp_per_game(
    arc: Sequence[SeasonArcPoint], league_format: str,
) -> float:
    if not arc:
        return 0.0
    return max(s.fp_per_game.get(league_format, 0.0) for s in arc)


# ---------------------------------------------------------------------------
# Public build entry point
# ---------------------------------------------------------------------------

def build_career_arc(
    player_id: str,
    name: str,
    position: str,
    last_season: Optional[int],
    rookie_season: Optional[int],
    retired: bool,
    is_long_arc: bool,
    seasons: Sequence[Mapping],  # iterable of dicts with: season, age, games, era, stats
    pace: EraPaceTable,
    formats: Sequence[str] = SUPPORTED_FORMATS,
) -> CareerArc:
    """Build a CareerArc from raw season dicts.

    Each ``seasons`` element must expose ``season``, ``age``, ``games``,
    ``era`` (int) and ``stats`` (Mapping[str, float]).
    """
    arc_points: List[SeasonArcPoint] = []
    chronological = sorted(seasons, key=lambda s: int(s["season"]))
    for s in chronological:
        games = int(s["games"])
        era = int(s["era"])
        stats = s["stats"]
        ap = SeasonArcPoint(
            season=int(s["season"]),
            age=int(s["age"]),
            games=games,
            era=era,
        )
        for fmt in formats:
            total, per_game, by_cat = _score_era_adjusted_season(
                stats, games, position, era, pace, fmt,
            )
            ap.fp_total[fmt] = total
            ap.fp_per_game[fmt] = per_game
            for cat, pts in by_cat.items():
                ap.fp_by_category[(fmt, cat)] = pts
        arc_points.append(ap)

    arc = CareerArc(
        player_id=player_id,
        name=name,
        position=position,
        last_season=last_season,
        rookie_season=rookie_season,
        retired=retired,
        is_long_arc=is_long_arc,
        career_arc=arc_points,
    )
    # Pre-compute per-format aggregates.
    for fmt in formats:
        arc.career_total_fp[fmt] = sum(s.fp_total.get(fmt, 0.0) for s in arc_points)
        arc.peak_season_fp_per_game[fmt] = _peak_single_season_fp_per_game(arc_points, fmt)
        arc.peak_3yr_fp_per_game[fmt] = _peak_3yr_avg(arc_points, fmt)
        arc.career_avg_fp_per_game[fmt] = _career_avg_fp_per_game(arc_points, fmt)
    return arc


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _arc_to_dict(arc: CareerArc) -> Dict:
    return {
        "player_id": arc.player_id,
        "name": arc.name,
        "position": arc.position,
        "last_season": arc.last_season,
        "rookie_season": arc.rookie_season,
        "retired": arc.retired,
        "is_long_arc": arc.is_long_arc,
        "career_total_fp": {k: round(v, 1) for k, v in arc.career_total_fp.items()},
        "peak_season_fp_per_game": {k: round(v, 2) for k, v in arc.peak_season_fp_per_game.items()},
        "peak_3yr_fp_per_game": {k: round(v, 2) for k, v in arc.peak_3yr_fp_per_game.items()},
        "career_avg_fp_per_game": {k: round(v, 2) for k, v in arc.career_avg_fp_per_game.items()},
        "career_arc": [
            {
                "season": s.season,
                "age": s.age,
                "games": s.games,
                "era": s.era,
                "fp_per_game": {k: round(v, 2) for k, v in s.fp_per_game.items()},
                "fp_total": {k: round(v, 1) for k, v in s.fp_total.items()},
            }
            for s in arc.career_arc
        ],
    }


def persist_corpus(
    arcs: Iterable[CareerArc],
    out_path: Path = OUT_ROOT / "fantasy_arc_corpus.json",
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "v2.0",
        "formats": list(SUPPORTED_FORMATS),
        "arcs": [_arc_to_dict(a) for a in arcs],
    }
    out_path.write_text(json.dumps(payload, indent=2, default=float), encoding="utf-8")


# ---------------------------------------------------------------------------
# Convenience: era-pace pre-adjustment diagnostic
# ---------------------------------------------------------------------------

def era_pace_diagnostic(
    pace: EraPaceTable,
    position: str,
    raw_stats: Mapping[str, float],
    era: int,
) -> Dict[str, Dict[str, float]]:
    """Return a {stat: {raw, era_adjusted, multiplier}} table for a single
    season — used by the docs/CHANGELOG to show what era-pace did to (e.g.)
    Peyton Manning 2013.
    """
    out: Dict[str, Dict[str, float]] = {}
    for stat in ("passing_yards", "passing_tds", "interceptions",
                 "rushing_yards", "rushing_tds",
                 "receptions", "receiving_yards", "receiving_tds"):
        raw = float(raw_stats.get(stat, 0.0))
        if raw == 0.0:
            continue
        mult = pace.get(position, stat, era)
        out[stat] = {
            "raw": raw,
            "multiplier": mult,
            "era_adjusted": raw * mult,
        }
    return out


# ---------------------------------------------------------------------------
# v3.8 — retired-early flag (Phil 2026-05-29)
# ---------------------------------------------------------------------------
#
# Some historical comps left the NFL while still at peak (Andrew Luck at 29,
# Calvin Johnson at 30) due to injury/health rather than talent decline.
# Their realised career_arc is truncated, so when they appear as comps for
# young targets, the post-snapshot projection (project_remaining) only sums
# the realised seasons and time-discounts to zero quickly. This drags down
# the projection of young players whose talent profile is comparable.
#
# Fix: load a curated sidecar YAML of (player_id → extrapolate_to_age),
# tag the matching CareerArc objects with ``retired_early=True``, and let
# the projection code (project_remaining / project_year_2_plus) extend the
# arc with SYNTHETIC seasons at the final-3yr fp/G rate up to the cap.
# The synthetic seasons inherit the same time-discount as realised seasons
# so they're not free — but they meaningfully change the answer for the
# 1-2 comps in any given player's top-K that fit this profile.


RETIRED_EARLY_PATH_DEFAULT = Path(__file__).resolve().parents[3] / "data" / "retired_early_comps.yaml"


def load_retired_early_overrides(path: Optional[Path] = None) -> Dict[str, Dict]:
    """Load the retired-early sidecar YAML.

    Returns a dict keyed by player_id with at minimum:
        {
            "name": str,
            "position": str,
            "reason": str,                  # "injury" | "voluntary"
            "extrapolate_to_age": int,
        }

    Returns empty dict if the file is missing or PyYAML is unavailable —
    the engine continues to work in that case (no retired-early bonus).
    """
    path = path or RETIRED_EARLY_PATH_DEFAULT
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    out: Dict[str, Dict] = {}
    for entry in data.get("retired_early", []) or []:
        pid = entry.get("player_id")
        if not pid:
            continue
        out[str(pid)] = {
            "name": entry.get("name"),
            "position": entry.get("position"),
            "reason": entry.get("reason", "injury"),
            "extrapolate_to_age": int(entry.get("extrapolate_to_age", 0)) or None,
            "notes": entry.get("notes"),
        }
    return out


def apply_retired_early_flag(
    arc: CareerArc,
    overrides: Mapping[str, Dict],
) -> None:
    """Tag ``arc`` in place if its player_id appears in ``overrides``."""
    ovr = overrides.get(arc.player_id)
    if not ovr:
        return
    arc.retired_early = True
    arc.retired_early_extrapolate_to_age = ovr.get("extrapolate_to_age")
