"""Compare a current rookie to historical NFL players at the same point.

Phil, 2026-09-22, verbatim:

    "the model is still comparing them to college players. It should be
    comparing them to nfl players using their nfl stats to this point.
    the model should be taking all of their starts so far from the 2026
    season and comparing them to similar historical nfl players who put
    up similar stats through their first 2 games."

This module is that comparison. A rookie who has played N NFL games is
matched against historical players' **first N career games** -- same
position, same N, same definition of a game. The comparison is between
two lines of identical shape, which is the whole point: it is not a
projection, it is "who else started like this, and what happened to
them".

N is derived from the rookie's game logs
----------------------------------------

Nothing here is hardcoded to two games. :func:`comps_for_player` reads
``n_games`` off the career-to-date line and asks the corpus for a cohort
of that length. In week 3 the same code compares first-3-game lines, and
in week 10, first-10. ``MAX_N`` in the corpus builder is the only
ceiling, and past it the season-level engine is the better tool anyway.

What this is not
----------------

It is not a projection and it does not produce one. Two games is a
sample you can count on one hand, and the honest output of a
two-game sample is a list of players who looked like this, not a
career forecast. :data:`SAMPLE_CAVEAT` is the sentence the site renders
next to every one of these tables; it lives here so the caveat cannot
drift away from the thing it is describing.

Method
------

Per-game volume plus position-appropriate efficiency, z-scored against
the cohort's own distribution at that N, then weighted-RMS distance.
Z-scoring against the cohort (rather than fixed scales) means a
feature's influence tracks how much it actually varies among players
with that many games -- at N=2 receiving yards per game varies far more
than yards per reception, and the distance reflects that.

Similarity is reported as ``1 / (1 + distance)`` in ``(0, 1]``, which is
monotone in distance and has no free parameters to tune.
"""
from __future__ import annotations

import logging
import math
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from ..sources import nflverse_weekly_stats as _weekly

log = logging.getLogger(__name__)

#: Rendered verbatim wherever these comps appear.
SAMPLE_CAVEAT = (
    "This is a very small sample. It describes how these players began, "
    "not how they will finish, and it is not a career projection."
)

#: Re-exported so a page can render one sentence from one place.
GAME_DEFINITION = _weekly.GAME_DEFINITION

#: Default number of comps surfaced per rookie.
DEFAULT_K = 10

#: A cohort smaller than this cannot support a similarity ranking --
#: z-scores against a handful of players are noise. Callers get an
#: explicit "cohort too small" state instead of a ranked list.
MIN_COHORT = 25


# ---------------------------------------------------------------------------
# Feature sets
#
# (key, label, weight, extractor). Volume first, then the efficiency
# ratio that distinguishes players at equal volume. Weights are relative
# within a position only.
# ---------------------------------------------------------------------------

def _pg(total: float, n: int) -> float:
    return (total / n) if n else 0.0


def _ratio(num: float, den: float) -> float:
    """Efficiency ratio with a zero-denominator guard.

    A receiver with zero catches has no yards-per-reception. Returning
    0.0 places him at the bottom of that axis, which is where a player
    with no production belongs -- as opposed to ``None``, which would
    have to be imputed, or a divide-by-zero, which would crash the
    build on the first player held without a catch.
    """
    return (num / den) if den else 0.0


_RECEIVING_FEATURES = (
    ("targets_pg", "Tgt/g", 1.0,
     lambda t, n: _pg(t["targets"], n)),
    ("rec_pg", "Rec/g", 1.0,
     lambda t, n: _pg(t["receptions"], n)),
    ("rec_yds_pg", "Rec yds/g", 1.4,
     lambda t, n: _pg(t["receiving_yards"], n)),
    ("rec_td_pg", "Rec TD/g", 0.8,
     lambda t, n: _pg(t["receiving_tds"], n)),
    ("yds_per_rec", "Yds/rec", 0.6,
     lambda t, n: _ratio(t["receiving_yards"], t["receptions"])),
)

_RUSHING_FEATURES = (
    ("carries_pg", "Car/g", 1.0,
     lambda t, n: _pg(t["carries"], n)),
    ("rush_yds_pg", "Rush yds/g", 1.4,
     lambda t, n: _pg(t["rushing_yards"], n)),
    ("rush_td_pg", "Rush TD/g", 0.7,
     lambda t, n: _pg(t["rushing_tds"], n)),
    ("yds_per_carry", "Yds/car", 0.6,
     lambda t, n: _ratio(t["rushing_yards"], t["carries"])),
    # A modern back who does not catch is a different player from one
    # who does, at identical rushing volume.
    ("rec_pg", "Rec/g", 0.8,
     lambda t, n: _pg(t["receptions"], n)),
    ("rec_yds_pg", "Rec yds/g", 0.7,
     lambda t, n: _pg(t["receiving_yards"], n)),
)

_PASSING_FEATURES = (
    ("attempts_pg", "Att/g", 1.0,
     lambda t, n: _pg(t["attempts"], n)),
    ("pass_yds_pg", "Pass yds/g", 1.4,
     lambda t, n: _pg(t["passing_yards"], n)),
    ("pass_td_pg", "Pass TD/g", 0.9,
     lambda t, n: _pg(t["passing_tds"], n)),
    ("int_pg", "INT/g", 0.6,
     lambda t, n: _pg(t["passing_interceptions"], n)),
    ("cmp_pct", "Cmp%", 0.7,
     lambda t, n: 100.0 * _ratio(t["completions"], t["attempts"])),
    # Rushing production is a large part of a modern QB's value and the
    # thing that most separates two passers with the same passing line.
    ("rush_yds_pg", "Rush yds/g", 0.6,
     lambda t, n: _pg(t["rushing_yards"], n)),
)

FEATURES_BY_POSITION: Dict[str, Tuple] = {
    "WR": _RECEIVING_FEATURES,
    "TE": _RECEIVING_FEATURES,
    "RB": _RUSHING_FEATURES,
    "QB": _PASSING_FEATURES,
}


def features_for(position: str) -> Tuple:
    return FEATURES_BY_POSITION.get((position or "").upper(), ())


# ---------------------------------------------------------------------------
# Stat-line shaping
# ---------------------------------------------------------------------------

def _totals_of(line: Mapping) -> Dict[str, float]:
    """Accept either a career-to-date line or a cohort row.

    ``career_to_date`` nests its counting stats under ``totals``; a
    cohort row carries them flat. Normalising here keeps one feature
    extractor working against both, so a rookie and his comps can never
    be measured with different code.
    """
    src = line.get("totals") if isinstance(line.get("totals"), Mapping) else line
    return {c: float(src.get(c, 0.0) or 0.0) for c in _weekly.STAT_COLUMNS}


def feature_vector(line: Mapping, position: str) -> Dict[str, float]:
    """The comparison vector for one first-N line."""
    n = int(line.get("n_games") or 0)
    totals = _totals_of(line)
    return {key: float(fn(totals, n))
            for key, _label, _w, fn in features_for(position)}


def display_line(line: Mapping, position: str) -> Dict[str, object]:
    """Everything a page needs to render one player's first-N line.

    Carries the raw counting totals *and* the derived per-game features,
    so the reader sees both "7 rec, 154 yds, 2 TD in 2 games" and the
    rates the similarity was actually computed on.
    """
    n = int(line.get("n_games") or 0)
    totals = _totals_of(line)
    return {
        "n_games": n,
        "totals": {k: (round(v, 1) if v % 1 else int(v))
                   for k, v in totals.items()},
        "features": {k: round(v, 2)
                     for k, v in feature_vector(line, position).items()},
        "feature_labels": [(k, label)
                           for k, label, _w, _fn in features_for(position)],
    }


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------

def _stdevs(vectors: Sequence[Mapping[str, float]],
            keys: Sequence[str]) -> Dict[str, float]:
    """Population standard deviation per feature across the cohort.

    A feature with no variation in the cohort gets ``0.0`` and is
    skipped by :func:`_distance` -- dividing by it would be a
    zero-division, and a constant feature carries no information for
    ranking anyway.
    """
    out: Dict[str, float] = {}
    n = len(vectors)
    if not n:
        return {k: 0.0 for k in keys}
    for k in keys:
        vals = [float(v.get(k, 0.0)) for v in vectors]
        mean = sum(vals) / n
        var = sum((x - mean) ** 2 for x in vals) / n
        out[k] = math.sqrt(var)
    return out


def _distance(a: Mapping[str, float], b: Mapping[str, float],
              stdevs: Mapping[str, float],
              weights: Mapping[str, float]) -> float:
    """Weighted RMS z-distance between two feature vectors."""
    num = 0.0
    den = 0.0
    for k, sd in stdevs.items():
        if sd <= 0:
            continue
        w = float(weights.get(k, 1.0))
        z = (float(a.get(k, 0.0)) - float(b.get(k, 0.0))) / sd
        num += w * (z ** 2)
        den += w
    return math.sqrt(num / den) if den else float("inf")


def similarity_from_distance(distance: float) -> float:
    """Map a distance in ``[0, inf)`` to a similarity in ``(0, 1]``."""
    if distance == float("inf"):
        return 0.0
    return 1.0 / (1.0 + distance)


# ---------------------------------------------------------------------------
# Career outcomes
# ---------------------------------------------------------------------------

#: Career fantasy-point bands (Superflex PPR, from the season corpus).
#: Chosen to separate "this start led somewhere" from "it did not", not
#: to be a fine-grained tiering.
ELITE_FP = 1500.0
STARTER_FP = 600.0
DEPTH_FP = 150.0


def outcome_label(career_fp: Optional[float], *, still_active: bool) -> str:
    """What became of this comp's career.

    ``still_active`` wins over every band. A player two years into his
    career has not busted; he is unfinished, and labelling him against
    a lifetime threshold would read as a verdict the data cannot
    support. This matters here more than usual: the cohort is skewed
    toward recent debuts precisely because they are the ones with
    complete weekly coverage.
    """
    if still_active:
        return "active"
    if career_fp is None:
        return "unknown"
    if career_fp >= ELITE_FP:
        return "elite"
    if career_fp >= STARTER_FP:
        return "starter"
    if career_fp >= DEPTH_FP:
        return "depth"
    return "bust"


def _career_outcome(player_id: str, position: str,
                    current_season: Optional[int]) -> Dict:
    """Career totals for a comp, from the season corpus.

    Deliberately reuses ``nflverse_career_stats`` rather than deriving
    career numbers from the weekly corpus: the season corpus is what
    every other career number on the site comes from, and two
    independent definitions of "career fantasy points" on one page
    would be a defect.
    """
    try:
        from ..sources import nflverse_career_stats as _career
        payload = _career.build_career_stats(player_id, position)
    except Exception as exc:  # noqa: BLE001 - never break the build
        log.warning("career outcome lookup failed for %s: %s", player_id, exc)
        return {"career_fp": None, "seasons_played": None,
                "last_season": None, "still_active": False,
                "outcome": "unknown"}

    rows = payload.get("rows") or []
    totals = payload.get("totals") or {}
    career_fp = totals.get("fp")
    seasons = len(rows)
    years = [int(r["year"]) for r in rows if str(r.get("year", "")).isdigit()]
    last_season = max(years) if years else None
    # "Active" means he appeared in the most recent completed season.
    # Anything older is a career we can read an ending into.
    still_active = bool(
        last_season is not None and current_season is not None
        and last_season >= current_season - 1
    )
    return {
        "career_fp": round(float(career_fp), 1) if career_fp is not None else None,
        "seasons_played": seasons,
        "last_season": last_season,
        "still_active": still_active,
        "outcome": outcome_label(
            float(career_fp) if career_fp is not None else None,
            still_active=still_active),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def comps_for_line(line: Mapping, position: str, *,
                   k: int = DEFAULT_K,
                   corpus_path: str = "",
                   current_season: Optional[int] = None,
                   with_outcomes: bool = True) -> Dict:
    """Rank historical first-N-game lines against ``line``.

    Returns a dict carrying the rookie's own line, the ranked comps, and
    the cohort metadata a page needs to describe what it is showing.
    ``comps`` is empty with an explicit ``state`` when there is nothing
    honest to show.
    """
    pos = (position or "").upper()
    n = int(line.get("n_games") or 0)

    if not n:
        return {"state": "no_nfl_games", "n_games": 0, "position": pos,
                "comps": [], "cohort_size": 0,
                "subject": None}

    feats = features_for(pos)
    if not feats:
        return {"state": "unsupported_position", "n_games": n,
                "position": pos, "comps": [], "cohort_size": 0,
                "subject": display_line(line, pos)}

    pool = _weekly.cohort(pos, n, corpus_path)
    subject = display_line(line, pos)
    if len(pool) < MIN_COHORT:
        return {"state": "cohort_too_small", "n_games": n, "position": pos,
                "comps": [], "cohort_size": len(pool),
                "min_cohort": MIN_COHORT, "subject": subject}

    keys = [f[0] for f in feats]
    weights = {f[0]: f[2] for f in feats}
    subject_vec = feature_vector(line, pos)
    pool_vecs = [feature_vector(p, pos) for p in pool]
    stdevs = _stdevs(pool_vecs, keys)

    scored: List[Tuple[float, Dict, Dict]] = []
    subject_id = str(line.get("player_id") or "")
    for row, vec in zip(pool, pool_vecs):
        if subject_id and row.get("player_id") == subject_id:
            continue
        scored.append((_distance(subject_vec, vec, stdevs, weights), row, vec))
    scored.sort(key=lambda t: t[0])

    comps: List[Dict] = []
    for distance, row, _vec in scored[:k]:
        comp = {
            "player_id": row.get("player_id"),
            "name": row.get("name"),
            "position": pos,
            "debut_season": row.get("debut_season"),
            "distance": round(distance, 4),
            "similarity": round(similarity_from_distance(distance), 4),
            "line": display_line(row, pos),
        }
        if with_outcomes:
            comp["career"] = _career_outcome(
                str(row.get("player_id") or ""), pos, current_season)
        comps.append(comp)

    return {
        "state": "ok",
        "n_games": n,
        "position": pos,
        "cohort_size": len(pool),
        "subject": subject,
        "comps": comps,
        "caveat": SAMPLE_CAVEAT,
        "game_definition": GAME_DEFINITION,
    }


def comps_for_player(player_id: str, position: str = "", *,
                     k: int = DEFAULT_K,
                     weekly_path: str = "",
                     corpus_path: str = "") -> Dict:
    """Comps for a current-season player, keyed on his nflverse id.

    Looks up his career-to-date line from the current-season weekly
    artifact, derives N from it, and ranks the cohort of that length.
    """
    line = _weekly.career_to_date(player_id, weekly_path)
    if line is None:
        return {"state": "no_nfl_games", "n_games": 0,
                "position": (position or "").upper(), "comps": [],
                "cohort_size": 0, "subject": None}
    pos = (position or line.get("position") or "").upper()
    return comps_for_line(
        line, pos, k=k, corpus_path=corpus_path,
        current_season=_weekly.current_season(weekly_path),
    )


def resolve_player_id(name: str, *, weekly_path: str = "",
                      aliases: Sequence[str] = ()) -> Optional[str]:
    """Find a current-season nflverse id for a display name.

    Tries the primary name then each alias, normalised. The rookie board
    is built from prospect records whose names come from the college
    roster and the draft card, neither of which is guaranteed to match
    the NFL roster string -- "KC Concepcion" is the roster name and
    "Kevin Concepcion" is the one the college corpus carries.
    """
    index = _weekly.current_name_index(weekly_path)
    for candidate in (name, *aliases):
        pid = index.get(_weekly.normalise_name(candidate))
        if pid:
            return pid
    return None
