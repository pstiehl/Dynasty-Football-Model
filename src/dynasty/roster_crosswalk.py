"""Repair the sleeper_id -> gsis_id crosswalk that My Team joins on.

The problem, measured on the live artifacts of 2026-09-21
---------------------------------------------------------
``roster_index.json`` maps a Sleeper player id to
``[name, position, team, gsis_id]``. ``engine_rankings.json`` is keyed by
gsis id. My Team therefore joins roster -> crosswalk -> rankings, and that
join is where the owner's missing ranks came from:

* 12,196 crosswalk entries, of which **8,303 (68%) carry an empty gsis id**,
  because ``report._load_sleeper_player_index`` writes ``""`` whenever the
  player table has no gsis for that row.
* Consequence: only **170 of 735** ranking rows were reachable from any
  Sleeper id. **565 were unreachable** -- including Jalen Hurts (#2),
  Trevor Lawrence (#3), Jahmyr Gibbs (#4), Justin Herbert (#5), Bijan
  Robinson (#6), Amon-Ra St. Brown (#8) and Puka Nacua (#14).

A dynasty roster is made almost entirely of players like those, which is
why the page looked like it was ranking nothing.

There are two independent defects behind that, not one
------------------------------------------------------
1. **Whitespace.** 866 crosswalk entries store the gsis id with a leading
   space -- ``" 00-0035228"``. A string join against ``"00-0035228"`` fails
   even though the ids are identical. That alone accounted for 70 ranking
   rows, among them Kyler Murray (#12), A.J. Brown (#35), Josh Jacobs
   (#42), Deebo Samuel Sr. (#46), DK Metcalf (#61) and Terry McLaurin
   (#114). Trimming lifts raw reachability from 170 to 240 before anything
   clever happens.
2. **Empty slots.** The remaining gap is rows with no gsis at all, which a
   name match can fill.

The fix
-------
The site build already holds both sides of the join, so it repairs it once
at build time instead of leaving every browser to cope. :func:`backfill`
first trims every stored id, then fills the still-empty ones by matching on
the normalized name via ``highlights.match_key`` -- the same folding the
highlight matcher and the client-side ``matchKey`` mirror already use, so
"Kenneth Walker III" and "Kenneth Walker" agree on one key.

Measured against those artifacts the two together take reachability from
**170/735 to 728/735**, with **zero** ambiguous matches. The handful left
over are engine rows whose name has no Sleeper counterpart in any form
("Bam Knight", "Irvin Charles" vs Sleeper's "Irv Charles", "Mike Strachan"
vs "Michael Strachan") and are reported rather than guessed at.

Rules
-----
* Trimming is unconditional; it cannot change which player an id refers to.
* Beyond that, only ever *fills* an empty slot. A non-empty id is
  authoritative and is never replaced by a name guess.
* A name that maps to more than one ranking row is left empty unless
  exactly one candidate shares the crosswalk's position. Putting another
  player's rank on a row is worse than showing no rank, and the page has an
  honest "unranked" state for that.
* A name matching more than one *Sleeper* entry is still filled: both rows
  legitimately describe players with that name, and the position check plus
  the client's own refusal-to-guess cover the rest.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from .highlights import match_key

#: Index positions inside a crosswalk value, mirroring the ``fields`` list
#: written alongside it in roster_index.json.
NAME, POSITION, TEAM, GSIS = 0, 1, 2, 3


def index_rankings_by_name(
    rankings: Iterable[dict],
) -> Dict[str, List[dict]]:
    """``match_key(name) -> [ranking row, ...]``.

    A list, not a single row: same-name collisions are real and the caller
    has to be able to see them rather than silently taking the first.
    """
    out: Dict[str, List[dict]] = {}
    for row in rankings or ():
        if not row:
            continue
        key = match_key(row.get("name") or "")
        if key:
            out.setdefault(key, []).append(row)
    return out


def resolve_gsis(
    name: str,
    position: Optional[str],
    by_name: Dict[str, List[dict]],
) -> "tuple[Optional[str], str]":
    """``(gsis_id, outcome)`` for one crosswalk row.

    ``outcome`` is one of ``matched`` / ``ambiguous`` / ``no_candidate``,
    so the build can report *why* a row stayed empty instead of just how
    many did.
    """
    key = match_key(name or "")
    if not key:
        return None, "no_candidate"

    rows = by_name.get(key) or []
    if not rows:
        return None, "no_candidate"

    if len(rows) == 1:
        return (rows[0].get("player_id") or None), "matched"

    same_pos = [
        r for r in rows
        if (r.get("position") or "") == (position or "")
    ]
    if len(same_pos) == 1:
        return (same_pos[0].get("player_id") or None), "matched"

    return None, "ambiguous"


def backfill(
    players: Dict[str, list],
    rankings: Iterable[dict],
) -> dict:
    """Fill empty gsis slots in ``players`` in place. Returns a stats dict.

    ``players`` is the ``sleeper_id -> [name, position, team, gsis_id]``
    mapping exactly as ``roster_index.json`` carries it.
    """
    by_name = index_rankings_by_name(rankings)

    stats = {
        "entries": len(players),
        "gsis_present": 0,
        "gsis_trimmed": 0,
        "gsis_backfilled": 0,
        "gsis_ambiguous": 0,
        "gsis_missing": 0,
    }

    for value in players.values():
        # Tolerate a short row rather than raising: a malformed crosswalk
        # entry must not take the whole site build down.
        if not isinstance(value, list) or len(value) <= GSIS:
            stats["gsis_missing"] += 1
            continue

        # Whitespace first. This is a straight normalization -- it cannot
        # point a row at a different player -- and it has to happen before
        # the emptiness test so a value of "   " is treated as empty.
        if isinstance(value[GSIS], str):
            trimmed = value[GSIS].strip()
            if trimmed != value[GSIS]:
                value[GSIS] = trimmed
                if trimmed:
                    stats["gsis_trimmed"] += 1

        if value[GSIS]:
            stats["gsis_present"] += 1
            continue

        gsis, outcome = resolve_gsis(value[NAME], value[POSITION], by_name)
        if gsis:
            value[GSIS] = gsis
            stats["gsis_backfilled"] += 1
        elif outcome == "ambiguous":
            stats["gsis_ambiguous"] += 1
        else:
            stats["gsis_missing"] += 1

    # How much of the model is actually reachable once repaired. This is the
    # number that matters to My Team, and the one that was 170/735.
    reachable = {
        v[GSIS] for v in players.values()
        if isinstance(v, list) and len(v) > GSIS and v[GSIS]
    }
    ranked_ids = {
        r.get("player_id") for r in (rankings or ()) if r and r.get("player_id")
    }
    stats["rankings_total"] = len(ranked_ids)
    stats["rankings_reachable"] = len(ranked_ids & reachable)
    stats["rankings_unreachable"] = len(ranked_ids - reachable)
    return stats


def unreachable_rankings(
    players: Dict[str, list],
    rankings: Iterable[dict],
) -> List[dict]:
    """Ranking rows no Sleeper id can reach, best-ranked first.

    For the build log: these are the players My Team still cannot rank, and
    naming them is the difference between a known gap and a mystery.
    """
    reachable = {
        v[GSIS] for v in players.values()
        if isinstance(v, list) and len(v) > GSIS and v[GSIS]
    }
    missing = [
        r for r in (rankings or ())
        if r and r.get("player_id") and r["player_id"] not in reachable
    ]
    missing.sort(key=lambda r: r.get("overall_rank") or 10 ** 6)
    return missing
