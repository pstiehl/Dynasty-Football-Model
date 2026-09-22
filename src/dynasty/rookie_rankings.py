"""The current rookie class, as ranking rows for the rankings pages.

Phil, 2026-09-22: 2026 rookies are missing from "Similar NFL Career Paths"
and "Dynasty Rankings". "Anyone drafted in 2026 is a rookie."

What was actually wrong
-----------------------
Verified against the live site rather than assumed:

* ``engine_rankings.json`` has 735 rows and **not one** 2026 draftee.
  Fernando Mendoza (1st overall), Jeremiyah Love, Carnell Tate, Denzel
  Boston, Caleb Douglas and KC Concepcion are all absent.
* The 2026 class **is** already in the prospects pipeline -- 23 of them on
  ``prospects.html`` -- and each already has a per-prospect page carrying
  25 historical college comps with the NFL careers those comps went on to
  have. Those pages resolve today.

So the data existed and the college->NFL layer already produced it; the
rankings pages simply never looked at it. The cause is explicit in
``similarity_v1``'s cohort dispatcher::

    #   0 seasons: 2026 draft class (drafted, not yet played). EXCLUDED
    #              from main rankings; deferred to v2.2's college chain.
    n_completed = _completed_nfl_seasons(ap)
    if n_completed == 0:
        continue

That was right when it was written and is wrong now: a season needs 4+
games to count, and on 2026-09-22 the 2026 class is three weeks into its
first year. They have no completed NFL season and will have none until
December, which is most of a dynasty offseason spent invisible.

Why these rows are a separate cohort and not merged into the veteran sort
------------------------------------------------------------------------
This is the load-bearing design decision, so it is written down.

A veteran's ``production_score`` is projected lifetime fantasy points
derived from their **own NFL production** comped against full historical
careers. A rookie's ``projected_career_fp`` comes from **college**
production comps plus draft capital. Both are "projected career fantasy
points" in Superflex PPR, so they share units -- but they do not share
error bars. The prospect engine's own back-test is Spearman rho 0.20
overall, 0.27 excluding TE; the veteran engine is far better established.

Interleaving them in one sorted column would assert a comparability the
model has not demonstrated, and it would do it silently: nothing in a
sorted list tells a reader that row 14 was estimated a different way from
row 13.

There is also a concrete harm. ``apply_superflex_vorp`` derives
replacement level from the ranking pool, so adding ~23 rookies with
projections up to 2682 would move every veteran's VORP and every veteran's
positional rank. A change to how rookies are displayed must not silently
restate every existing number on the site.

So the rookie class is a labelled cohort with its own ordering, its own
provenance column, and its own artifact. Nothing a veteran row shows
changes. Both boards link to the prospect page, which is where the
historical NFL comparisons actually live.

v3.15 -- the class has NFL snaps now
------------------------------------

Phil, 2026-09-22, after the above shipped: "the model is still comparing
them to college players. It should be comparing them to nfl players
using their nfl stats to this point. the model should be taking all of
their starts so far from the 2026 season and comparing them to similar
historical nfl players who put up similar stats through their first 2
games."

He is right, and the argument above has a shelf life that expired the
moment these players took a snap. "There is no NFL production to comp"
was true in April and is false in September. :func:`attach_nfl_comps`
adds, to every row that has one, the player's actual NFL line to date
and the historical players whose **first N career games** looked most
like it -- N read off his game logs, never hardcoded.

The college projection stays on the row, demoted: it is what we have
for a rookie who has not played, and it is the weaker estimator even
for one who has. It is no longer the headline.

What the ordering still rests on
--------------------------------

The rows are still ordered by the college/draft-capital projection, and
that is deliberate rather than leftover. Two games cannot rank 80
players: sorting this board on a two-game sample would put whoever drew
the best coverage matchup in week 1 at the top and call it a dynasty
ranking. The NFL line and its comps are shown on every row precisely so
a reader can see the small sample instead of having it silently folded
into a sort key.

The pure-function core here is stdlib-only and reads dicts. Only
:func:`attach_nfl_comps` touches the corpus, and it takes injectable
paths so tests never need the real files.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Mapping, Optional, Sequence

from . import prospect_view as _pv

log = logging.getLogger(__name__)

#: Marks a row as coming from the college-production layer rather than from
#: NFL production. Present on every row this module emits, so no consumer
#: can mistake one for a veteran row.
ROOKIE_ENGINE = "prospect_college_arc_v3"

#: Marks the NFL-production comparison layer added in v3.15. A row
#: carrying ``nfl_comp_engine`` has been compared against historical NFL
#: players; a row without one has not played and says so.
ROOKIE_NFL_ENGINE = "nfl_first_n_games_v1"

#: Schema for the published artifact. Bumped from v1: every row may now
#: carry ``nfl`` (the player's own NFL line to date) and ``nfl_comps``
#: (historical players through the same number of career games). A
#: consumer pinned to v1 will not silently read the new shape.
ROOKIE_ARTIFACT_SCHEMA = "dynasty.rookie_rankings.v2"


def rookie_class_year(artifact: Optional[Mapping]) -> Optional[int]:
    """The class these rows describe: the newest class with real picks.

    Derived from the artifact rather than hardcoded to 2026. A constant
    here would be wrong every April, and silently -- the page would keep
    saying "2026 rookie class" over 2027 players.
    """
    prospects = (artifact or {}).get("prospects") or []
    return _pv.classify_classes(prospects)["current"]


def rookie_rows(artifact: Optional[Mapping],
                limit: Optional[int] = None) -> List[Dict]:
    """Ranking-shaped rows for the current rookie class.

    Only drafted players: an undrafted prospect is not a rookie, and
    ``prospects.html`` is where they belong. Ordered by
    ``prospect_view.sort_for_display``, so an evidence-backed projection
    outranks a draft-slot constant of equal value.

    Every row carries ``projection_basis`` so the page can say what the
    number rests on, and ``href`` pointing at the prospect page, which is
    the page that actually shows historical NFL comparisons.
    """
    prospects = list((artifact or {}).get("prospects") or [])
    year = rookie_class_year(artifact)
    if year is None:
        return []

    in_class = [p for p in _pv.in_class(prospects, year) if _pv.is_drafted(p)]
    ordered = _pv.sort_for_display(in_class)

    rows: List[Dict] = []
    for i, p in enumerate(ordered, 1):
        proj = p.get("projection") or {}
        basis = _pv.projection_basis(p)
        drafted = p.get("drafted") or {}
        slug = _prospect_slug(p)

        career_fp = proj.get("projected_career_fp")
        peak3 = proj.get("projected_peak3_fp_pg")
        years = proj.get("projected_years_in_league")
        if not basis["show_point_estimate"]:
            career_fp = peak3 = years = None

        rows.append({
            "rookie_rank": i,
            "name": _display_name(p),
            "corpus_name": p.get("name") or "",
            "position": p.get("position") or "",
            "school": p.get("school"),
            "age": p.get("age"),
            "draft_class": year,
            "team": drafted.get("team"),
            "draft_round": drafted.get("round"),
            "draft_pick": drafted.get("pick"),
            "draft_label": _pv.draft_label(p),
            # Same units as a veteran's production_score, different
            # estimator -- see the module docstring.
            "projected_career_fp": career_fp,
            "projected_peak3_fp_pg": peak3,
            "projected_years_in_league": years,
            "n_comps_with_nfl": basis["n_comps_with_nfl"],
            "projection_basis": basis["key"],
            "projection_basis_label": basis["label"],
            "projection_basis_detail": basis["detail"],
            "evidence_backed": basis["evidence_backed"],
            "engine": ROOKIE_ENGINE,
            # The prospect page: 25 college comps, each with the NFL career
            # that comp went on to have. This is the "similar NFL career
            # paths" view for a player with no NFL career of his own.
            "href": f"players/{slug}-prospect.html",
            "slug": slug,
            "ktc_rank_sf": (p.get("ktc") or {}).get("ktc_rank_sf"),
            "ktc_delta_overall": p.get("ktc_delta_overall"),
        })

    return rows[:limit] if limit else rows


# ---------------------------------------------------------------------------
# v3.15 -- NFL production and NFL comps
# ---------------------------------------------------------------------------

def attach_nfl_comps(rows: Sequence[Dict], *,
                     k: int = 10,
                     weekly_path: str = "",
                     corpus_path: str = "") -> Dict:
    """Add each rookie's real NFL line to date and his NFL comps.

    Mutates ``rows`` in place and returns a coverage summary.

    For every row we try to resolve the player in the current season's
    weekly stat file, by draft-card name and then by corpus name. On a
    hit the row gains:

    ``nfl_games``      games he has actually played (the N everything else keys off)
    ``nfl_line``       his cumulative line and per-game rates
    ``nfl_comps``      historical players through their own first N games
    ``nfl_comp_state`` ``ok`` / ``cohort_too_small`` / ``unsupported_position``

    A row with no 2026 stat line gets ``nfl_games = 0`` and
    ``nfl_comp_state = "no_nfl_games"``. That is a real state -- a
    drafted rookie who has not played -- and it is recorded rather than
    quietly backfilled with the college comps, which is exactly the
    substitution Phil has now rejected twice.

    Never raises. A missing weekly corpus degrades every row to the
    "has not played" state and logs once; it must not take down a build
    that was fine before this feature existed.
    """
    summary = {
        "engine": ROOKIE_NFL_ENGINE,
        "n_rows": len(rows),
        "n_with_nfl_games": 0,
        "n_with_comps": 0,
        "n_no_nfl_games": 0,
        "n_games_seen": [],
        "season": None,
        "cohort_sizes": {},
        "available": False,
    }
    if not rows:
        return summary

    try:
        from .engine import rookie_nfl_debut_similarity as _sim
        from .sources import nflverse_weekly_stats as _weekly
    except Exception as exc:  # noqa: BLE001
        log.warning("rookie NFL comps unavailable (import): %s", exc)
        for r in rows:
            _mark_no_nfl(r)
        summary["n_no_nfl_games"] = len(rows)
        return summary

    try:
        season = _weekly.current_season(weekly_path)
        summary["season"] = season
        summary["available"] = bool(_weekly.current_weeks(weekly_path))
    except Exception as exc:  # noqa: BLE001
        log.warning("rookie NFL comps unavailable (weekly corpus): %s", exc)
        for r in rows:
            _mark_no_nfl(r)
        summary["n_no_nfl_games"] = len(rows)
        return summary

    n_seen = set()
    for row in rows:
        # Two shots at the join: the name on the draft card and the name
        # in the college corpus. They disagree often enough to matter --
        # "KC Concepcion" on the NFL roster, "Kevin Concepcion" in the
        # corpus -- and a missed join looks identical to "has not
        # played", which would be a silent lie on the page.
        pid = None
        try:
            pid = _sim.resolve_player_id(
                row.get("name") or "",
                weekly_path=weekly_path,
                aliases=(row.get("corpus_name") or "",),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("rookie NFL id resolution failed for %r: %s",
                        row.get("name"), exc)

        if not pid:
            _mark_no_nfl(row)
            summary["n_no_nfl_games"] += 1
            continue

        try:
            result = _sim.comps_for_player(
                pid, row.get("position") or "", k=k,
                weekly_path=weekly_path, corpus_path=corpus_path,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("rookie NFL comps failed for %r: %s",
                        row.get("name"), exc)
            _mark_no_nfl(row)
            summary["n_no_nfl_games"] += 1
            continue

        state = result.get("state")
        row["nfl_player_id"] = pid
        row["nfl_comp_state"] = state
        row["nfl_games"] = int(result.get("n_games") or 0)
        row["nfl_line"] = result.get("subject")
        row["nfl_comps"] = list(result.get("comps") or [])
        row["nfl_cohort_size"] = int(result.get("cohort_size") or 0)
        row["nfl_comp_engine"] = ROOKIE_NFL_ENGINE
        row["nfl_season"] = season

        if row["nfl_games"]:
            summary["n_with_nfl_games"] += 1
            n_seen.add(row["nfl_games"])
            if row["nfl_comps"]:
                summary["n_with_comps"] += 1
                # Keyed by position AND N. A cohort is "WRs through 2
                # games", not "players through 2 games": keying on N
                # alone made the last position written overwrite the
                # rest, so the summary reported 844 for a comparison
                # actually drawn from 1,200.
                summary["cohort_sizes"][
                    f"{row.get('position') or '?'}@{row['nfl_games']}"
                ] = row["nfl_cohort_size"]
        else:
            summary["n_no_nfl_games"] += 1

    summary["n_games_seen"] = sorted(n_seen)
    return summary


def _mark_no_nfl(row: Dict) -> None:
    """The honest empty state for a rookie with no 2026 stat line."""
    row["nfl_player_id"] = None
    row["nfl_comp_state"] = "no_nfl_games"
    row["nfl_games"] = 0
    row["nfl_line"] = None
    row["nfl_comps"] = []
    row["nfl_cohort_size"] = 0
    row["nfl_comp_engine"] = ROOKIE_NFL_ENGINE


def nfl_sample_label(rows: Sequence[Mapping]) -> Optional[str]:
    """"N = 2 games" -- the sample size, stated as a plain sentence.

    Derived from the rows, so it tracks the season instead of asserting
    a week. Returns ``None`` when nobody has played.
    """
    ns = sorted({int(r.get("nfl_games") or 0) for r in rows
                 if r.get("nfl_games")})
    if not ns:
        return None
    if len(ns) == 1:
        return f"N = {ns[0]} game{'s' if ns[0] != 1 else ''}"
    return f"N = {ns[0]}\u2013{ns[-1]} games, per player"


def _display_name(prospect: Mapping) -> str:
    """The name to show: the draft card's, falling back to the corpus's.

    The corpus is keyed on the college roster, which sometimes carries a
    player's formal name while every draft board, fantasy site and search
    box uses the short one. The v3.6 fuzzy join logs the collision it
    resolved -- ``PFR='KC Concepcion' -> corpus='Kevin Concepcion'`` -- and
    then the board rendered "Kevin Concepcion", which is correct and
    useless: a reader looking for the Browns' first-rounder does not find
    him. ``drafted.player_name`` comes from the committed PFR draft class
    (``data/pfr/draft_class_2026.json``), so this prefers the name on the
    pick and keeps the corpus name alongside it as ``corpus_name``.
    """
    drafted = prospect.get("drafted") or {}
    return str(drafted.get("player_name")
               or prospect.get("name") or "")


def _prospect_slug(prospect: Mapping) -> str:
    """Mirror of ``report._prospect_slug``.

    Duplicated rather than imported because ``report`` imports the engine
    at module scope and this module must stay importable from a bare
    checkout. ``test_rookie_rankings`` asserts the two agree on the
    committed fixture, so the duplication cannot drift unnoticed.
    """
    # EXACT mirror of report._prospect_slug. It previously returned the
    # record's own ``slug`` field when present, which report ignores except
    # as a fallback id -- so for the stub records that PFR picks with no
    # corpus match produce (slug "nate-boerkircher-pfr"), the page was
    # written as "nate-boerkircher-rcher1" and the rookie board linked
    # "nate-boerkircher-pfr". Four dead links on rankings.html, league.html
    # and index.html, caught by the link audit against a real build and not
    # by the agreement test, whose fixture contains no stub record.
    import re
    name = prospect.get("name") or "prospect"
    pid = str(prospect.get("cfb_player_id") or prospect.get("slug") or "")
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    tail = re.sub(r"[^a-z0-9]+", "", pid.lower())[-6:] or "x"
    return f"{s}-{tail}"


def coverage(artifact: Optional[Mapping], rows: Sequence[Mapping]) -> Dict:
    """Counts the page states, so the cohort explains its own size."""
    prospects = list((artifact or {}).get("prospects") or [])
    year = rookie_class_year(artifact)
    in_class = _pv.in_class(prospects, year) if year is not None else []
    drafted = [p for p in in_class if _pv.is_drafted(p)]
    evidence = sum(1 for r in rows if r.get("evidence_backed"))
    # v3.15. Counted separately from the college-projection states above:
    # "has an NFL line" and "has an evidence-backed college projection"
    # are different facts about a player and a reader must be able to see
    # how many rows rest on each.
    played = [r for r in rows if r.get("nfl_games")]
    with_comps = [r for r in played if r.get("nfl_comps")]
    return {
        "class_year": year,
        "n_in_class": len(in_class),
        "n_drafted": len(drafted),
        "n_shown": len(rows),
        "n_evidence_backed": evidence,
        "n_draft_slot_constant": len(rows) - evidence,
        "n_with_nfl_games": len(played),
        "n_with_nfl_comps": len(with_comps),
        "n_not_yet_played": len(rows) - len(played),
        "nfl_sample_label": nfl_sample_label(rows),
    }


def artifact_payload(artifact: Optional[Mapping],
                     rows: Sequence[Mapping]) -> Dict:
    """The published ``rookie_rankings.json``.

    A separate file rather than extra entries in ``engine_rankings.json``.
    That file is a bare JSON list consumed by the league-import flow, so
    appending rows estimated a different way would change what an existing
    consumer reads without telling it. A new artifact with its own schema
    cannot break anything that does not ask for it.
    """
    cov = coverage(artifact, rows)
    return {
        "schema": ROOKIE_ARTIFACT_SCHEMA,
        "engine": ROOKIE_ENGINE,
        "nfl_comp_engine": ROOKIE_NFL_ENGINE,
        "class_year": cov["class_year"],
        "coverage": cov,
        "primary_comparison": (
            "Each rookie who has played is compared to historical NFL "
            "players through the same number of career games "
            "(nfl_games / nfl_line / nfl_comps on each row). N is read "
            "from his 2026 game logs and grows every week."
        ),
        "sample_warning": (
            "The NFL comparison rests on "
            + (cov.get("nfl_sample_label") or "no games yet")
            + ". That is a very small sample: it describes how these "
            "players began, not how they will finish, and it is not a "
            "career projection."
        ),
        "why_separate": (
            "projected_career_fp on each row still comes from the "
            "college-production layer and draft capital, not from NFL "
            "production. It is retained as a secondary figure and as the "
            "board's sort key, because a two-game sample cannot rank a "
            "class. The units match a veteran's production_score but the "
            "error bars do not, so the rows stay their own cohort rather "
            "than merging into the veteran ranking."
        ),
        "rookies": list(rows),
    }
