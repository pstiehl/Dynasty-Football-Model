"""Superflex Positional VORP — value-over-replacement scoring.

Phil's 2026-06-03 brief asks the Dynasty Rankings tab to surface a
positional-value ranking on top of the format-agnostic
``production_score``. Sorting QBs and skill players on the same raw
score is misleading in superflex: QBs score so many more fp/season
that they sweep the top of any pure-production ranking, but their
dynasty trade value depends on how scarce they are vs replacement
level at the position.

The VORP fix:

    superflex_vorp_score = production_score - replacement_level[position]

where ``replacement_level[position]`` is the production_score of the
worst STARTER at that position across 12 teams in a typical Superflex
roster:

    1 QB  + 2 RB + 3 WR + 1 TE + 1 SF (mostly QB) + 1.5 Flex (mostly RB/WR)

Resolving the 1 SF slot mostly going to QB and the 1.5 Flex split
1.0 RB / 0.5 WR (the position-tier averages settle there for a typical
SF league), the per-position starter counts across 12 teams come out
to:

    QB:  12 + 12 (SF)               = 24
    RB:  12*2 + 12*1.0 (Flex)       = 36
    WR:  12*3 + 12*0.5 (Flex)       = 42
    TE:  12*1                       = 12

The replacement level is the ``production_score`` of the
(N+1)-th-ranked player at that position (one past the last starter),
i.e. the best available bench player you'd be cutting to draft
the worst starter.

This is intentionally simple: a single function that takes the engine
rankings list and a settings dict, and returns the same list with a
``superflex_vorp_score`` field and a ``superflex_vorp_replacement``
diagnostic.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Mapping, Sequence


# Default 12-team Superflex starter counts (computed in module docstring).
SUPERFLEX_STARTERS: Dict[str, int] = {
    "QB": 24,
    "RB": 36,
    "WR": 42,
    "TE": 12,
}


def compute_replacement_levels(
    rankings: Sequence[Mapping],
    starters: Mapping[str, int] = SUPERFLEX_STARTERS,
    score_field: str = "production_score",
) -> Dict[str, float]:
    """Return ``{position: replacement_level}``.

    Replacement = ``production_score`` of the (N+1)-th-ranked player
    at each position (1-indexed: the player just past the last
    starter). If a position has fewer than N+1 ranked players, the
    replacement level is the score of the last-ranked player.
    Positions not present in ``starters`` get a replacement of 0.0.
    """
    by_pos: Dict[str, List[float]] = {}
    for row in rankings:
        pos = row.get("position")
        if pos not in starters:
            continue
        score = row.get(score_field)
        if score is None:
            continue
        by_pos.setdefault(pos, []).append(float(score))

    replacement: Dict[str, float] = {}
    for pos, n in starters.items():
        scores = sorted(by_pos.get(pos, []), reverse=True)
        if not scores:
            replacement[pos] = 0.0
            continue
        # The (N+1)-th-ranked player is at 0-indexed position n.
        # If fewer than n+1 players exist, use the last available.
        idx = min(n, len(scores) - 1)
        replacement[pos] = scores[idx]
    return replacement


def apply_superflex_vorp(
    rankings: List[Dict],
    starters: Mapping[str, int] = SUPERFLEX_STARTERS,
    score_field: str = "production_score",
) -> Dict[str, float]:
    """Mutate ``rankings`` in place to add ``superflex_vorp_score``
    and ``superflex_vorp_replacement`` to each row. Returns the
    replacement-level dict for diagnostics."""
    replacement = compute_replacement_levels(
        rankings, starters=starters, score_field=score_field,
    )
    for row in rankings:
        pos = row.get("position")
        score = row.get(score_field)
        if score is None or pos not in replacement:
            row["superflex_vorp_score"] = None
            row["superflex_vorp_replacement"] = None
            continue
        rep = replacement[pos]
        row["superflex_vorp_score"] = round(float(score) - rep, 1)
        row["superflex_vorp_replacement"] = round(rep, 1)
    return replacement
