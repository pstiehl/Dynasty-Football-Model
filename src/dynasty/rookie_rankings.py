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

Everything here is pure and stdlib-only: it reads a dict and returns
dicts, so it is testable without the engine, the corpus or numpy.
"""
from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Sequence

from . import prospect_view as _pv

#: Marks a row as coming from the college-production layer rather than from
#: NFL production. Present on every row this module emits, so no consumer
#: can mistake one for a veteran row.
ROOKIE_ENGINE = "prospect_college_arc_v3"

#: Schema for the published artifact.
ROOKIE_ARTIFACT_SCHEMA = "dynasty.rookie_rankings.v1"


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
    return {
        "class_year": year,
        "n_in_class": len(in_class),
        "n_drafted": len(drafted),
        "n_shown": len(rows),
        "n_evidence_backed": evidence,
        "n_draft_slot_constant": len(rows) - evidence,
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
        "class_year": cov["class_year"],
        "coverage": cov,
        "why_separate": (
            "These projections come from the college-production layer, not "
            "from NFL production: the class has no completed NFL season "
            "yet. The units match a veteran's production_score but the "
            "error bars do not, so the rows are published as their own "
            "cohort rather than merged into the veteran ranking."
        ),
        "rookies": list(rows),
    }
