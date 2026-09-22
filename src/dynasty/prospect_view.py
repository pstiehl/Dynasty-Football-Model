"""How a prospect record is presented: draft status, projection basis, class.

Split out of ``report._build_prospects`` because all three of the live
prospects-page defects Phil reported on 2026-09-22 were presentation
decisions made inline in an f-string, where they could not be tested:

1. ``Arch Manning ? RNone`` -- ``f"R{d_round}"`` with ``d_round = None``
   renders the literal text ``RNone``, and ``team or "?"`` renders ``?``.
   Both came from Tankathon big-board entries, which carry no round and no
   team because the player has not been drafted.

2. ``Michael Penix ATL R1`` at rank 1, class **2024**. A player two NFL
   seasons deep topping the *prospect* board means the page was ranking
   across every class at once with no statement of which class you were
   looking at.

3. The top three rows all reading ``3200`` projected career fp and ``13.5``
   peak3 fp/g. Not a coincidence and not a ceiling: those are the literal
   constants ``PICK_TIER_BASELINES_SF_PPR[("QB", "R1_top10")]`` and
   ``PICK_TIER_PEAK3_FPG["R1_top10"]`` in ``scripts/build_prospects_v3.py``.
   Any QB whose comp pool has no NFL careers gets the tier constant, so the
   number is a property of the tier, not of the player -- and ordering rows
   by a constant is arbitrary.

The category error behind (1) and (3)
-------------------------------------
``_index_tankathon_by_class`` normalises a big-board entry as
``{"rnd": None, "team": None, "pick": <big-board rank>}`` and the comment
calls the rank a "pick proxy". It is then fed to ``_pick_tier``, so Arch
Manning at big-board rank 4 is scored as the 4th overall NFL pick and
inherits the ``R1_top10`` baseline -- the same tier as Fernando Mendoza,
who was actually taken first overall.

A big-board rank is not draft capital. It is one site's opinion about a
draft that has not happened, and draft capital is the strongest single
input to the baseline. Treating them as the same thing asserts a landing
spot we do not have. :func:`pick_tier` therefore refuses to tier an
un-drafted prospect, and :func:`projection_basis` reports when a number is
a tier constant rather than a player-specific estimate so the page can say
so instead of printing ``3200`` four times and letting the reader assume it
was measured.

Everything here is pure and stdlib-only, so it is testable without the
engine, the corpus or numpy -- which is the point.
"""
from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Sequence, Tuple

#: Tiers used by the NFL-draft-pick baseline in build_prospects_v3.
#: Reproduced (not imported) because that module is a script that pulls in
#: the similarity engine; this needs to stay importable from a bare
#: checkout. ``test_prospect_view`` asserts the two agree.
PICK_TIER_BOUNDS: Sequence[Tuple[int, str]] = (
    (10, "R1_top10"),
    (32, "R1"),
    (64, "R2"),
    (100, "R3"),
    (150, "R4"),
    (200, "R5_6"),
)
LAST_TIER = "R7"

#: Marker ``_index_tankathon_by_class`` stamps on big-board entries.
BIG_BOARD_SOURCE = "tankathon_big_board"


def is_drafted(prospect: Mapping) -> bool:
    """True when this prospect has a real NFL draft record.

    A ``drafted`` block alone is not enough: big-board entries get one too,
    with ``round`` and ``team`` empty. The round is the discriminator --
    every real pick has one, and no un-drafted player can.
    """
    drafted = prospect.get("drafted") or {}
    if not drafted:
        return False
    if str(drafted.get("source") or "") == BIG_BOARD_SOURCE:
        return False
    return drafted.get("round") is not None


def pick_tier(pick: Optional[int], *, drafted: bool = True) -> Optional[str]:
    """The draft-capital tier for a real pick, or ``None``.

    ``drafted=False`` returns ``None`` rather than a tier. That is the fix
    for the saturation: an un-drafted prospect has no draft capital, so
    there is no tier to look a baseline up in, and inventing one from a
    big-board rank is what made four different players share one number.
    """
    if not drafted or pick is None:
        return None
    try:
        p = int(pick)
    except (TypeError, ValueError):
        return None
    if p < 1:
        return None
    for bound, tier in PICK_TIER_BOUNDS:
        if p <= bound:
            return tier
    return LAST_TIER


# ---------------------------------------------------------------------------
# Draft status, rendered without leaking None
# ---------------------------------------------------------------------------

def draft_status(prospect: Mapping) -> Dict[str, Optional[str]]:
    """Structured draft status for one prospect.

    Returns ``kind`` plus only the fields that kind actually has:

    ``drafted``    a real NFL pick: ``team``, ``round``, ``pick``, ``year``
    ``big_board``  on a big board for an undrafted class: ``board_rank``
    ``none``       nothing known

    No caller can render ``RNone`` off this, because a missing round means
    the record is not ``drafted`` in the first place.
    """
    drafted = prospect.get("drafted") or {}

    if is_drafted(prospect):
        return {
            "kind": "drafted",
            "team": str(drafted.get("team")) if drafted.get("team") else None,
            "round": str(drafted.get("round")),
            "pick": (str(drafted.get("pick"))
                     if drafted.get("pick") is not None else None),
            "year": (str(drafted.get("year"))
                     if drafted.get("year") is not None else None),
            "board_rank": None,
        }

    rank = drafted.get("pick")
    if rank is not None:
        return {
            "kind": "big_board",
            "team": None, "round": None, "pick": None,
            "year": (str(drafted.get("year"))
                     if drafted.get("year") is not None else None),
            "board_rank": str(rank),
        }

    return {"kind": "none", "team": None, "round": None, "pick": None,
            "year": None, "board_rank": None}


def draft_label(prospect: Mapping) -> str:
    """Short plain-text draft label. ``""`` when nothing is known.

    ``"TEN R1 #4"`` for a real pick; ``"big board #4"`` for a big-board
    entry, which says what the number is instead of dressing an opinion up
    as a selection.
    """
    st = draft_status(prospect)
    if st["kind"] == "drafted":
        bits = []
        if st["team"]:
            bits.append(st["team"])
        bits.append(f"R{st['round']}")
        if st["pick"]:
            bits.append(f"#{st['pick']}")
        return " ".join(bits)
    if st["kind"] == "big_board":
        return f"big board #{st['board_rank']}"
    return ""


# ---------------------------------------------------------------------------
# Projection basis -- is this number about the player, or about the tier?
# ---------------------------------------------------------------------------

#: ``projection_source`` prefixes emitted by build_prospects_v3, mapped to
#: how much of the number is player-specific evidence.
_BASIS_LABELS = {
    "comp_weighted": (
        "comps",
        "projected from this player's own college comps and the NFL careers "
        "those comps went on to have",
    ),
    "blend": (
        "blend",
        "part comp evidence, part draft-capital baseline -- the comp pool "
        "had few NFL careers, so the projection is pulled toward what this "
        "draft slot historically returns",
    ),
    "floor": (
        "baseline floor",
        "the comp projection fell below what this draft slot historically "
        "returns and was raised to that floor, so the number is set by "
        "draft position rather than by this player's comps",
    ),
    "pick_tier_baseline": (
        "draft-slot constant",
        "no comp in this player's pool ever played in the NFL, so this is "
        "the historical average for their draft slot -- it is identical for "
        "every player in the same position and pick tier and says nothing "
        "about this player specifically",
    ),
    "comp_weighted_no_draft_capital": (
        "comps only",
        "projected from this player's college comps alone. They have not "
        "been drafted, so there is no draft capital to anchor the estimate "
        "and it will move a lot once they are selected",
    ),
    "insufficient_evidence_undrafted": (
        "not enough evidence",
        "this player has not been drafted and no comp in their pool had a "
        "meaningful NFL career, so there is nothing to project from. Showing "
        "a number here would mean inventing one",
    ),
}

#: Sources that carry no point estimate at all.
NO_ESTIMATE_SOURCES = ("insufficient_evidence_undrafted",)


def projection_basis(prospect: Mapping) -> Dict:
    """What a prospect's projected numbers actually rest on.

    ``show_point_estimate`` is the load-bearing field. It is False when the
    number is a tier constant with no player-specific evidence behind it,
    which is the honest bound on the saturation: the page shows the tier
    average labelled as such instead of a figure that reads as a
    measurement of this player.
    """
    proj = prospect.get("projection") or {}
    source = str(proj.get("projection_source") or "")
    n_nfl = proj.get("n_comps_with_nfl")
    try:
        n_nfl = int(n_nfl) if n_nfl is not None else None
    except (TypeError, ValueError):
        n_nfl = None

    key = "comps"
    label, detail = _BASIS_LABELS["comp_weighted"]
    if source in NO_ESTIMATE_SOURCES:
        key = "none"
        label, detail = _BASIS_LABELS["insufficient_evidence_undrafted"]
    elif source == "comp_weighted_no_draft_capital":
        key = "comps_no_capital"
        label, detail = _BASIS_LABELS["comp_weighted_no_draft_capital"]
    elif source.startswith("pick_tier_baseline"):
        key = "baseline"
        label, detail = _BASIS_LABELS["pick_tier_baseline"]
    elif source.startswith("floor_"):
        key = "floor"
        label, detail = _BASIS_LABELS["floor"]
    elif source.startswith("blend"):
        key = "blend"
        label, detail = _BASIS_LABELS["blend"]
    elif source == "comp_weighted":
        key = "comps"
    elif not source:
        # An older artifact with no provenance. Infer conservatively from
        # the comp count rather than claiming comp support we cannot see.
        if n_nfl == 0:
            key = "baseline"
            label, detail = _BASIS_LABELS["pick_tier_baseline"]
        else:
            key = "unknown"
            label, detail = ("basis unrecorded",
                             "this artifact predates projection provenance")

    evidence_backed = key in ("comps", "blend", "comps_no_capital")
    # A tier constant IS shown -- draft capital is real signal and hiding it
    # would be worse -- but flagged, never presented as measured. A
    # no-evidence record has nothing to show at all.
    show_point_estimate = key != "none"
    return {
        "key": key,
        "label": label,
        "detail": detail,
        "source": source,
        "n_comps_with_nfl": n_nfl,
        "evidence_backed": evidence_backed,
        "show_point_estimate": show_point_estimate,
        "is_tier_constant": key == "baseline",
        "confidence": proj.get("projection_confidence"),
    }


# ---------------------------------------------------------------------------
# Classes -- which board am I looking at?
# ---------------------------------------------------------------------------

def class_of(prospect: Mapping) -> Optional[int]:
    value = prospect.get("draft_class")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def classify_classes(prospects: Sequence[Mapping]) -> Dict:
    """Split the board into drafted classes and not-yet-drafted classes.

    ``current`` is the newest class that has real draft records -- the
    rookie class, and the only defensible default view. Before this, the
    page defaulted to every class at once ranked together, which is how a
    2024 quarterback with two NFL seasons came to sit at rank 1 of a
    *prospect* board.

    ``future`` classes are kept and reachable, but separately: they have no
    draft capital, so their projections are not comparable with a drafted
    class's and must not be interleaved with them.
    """
    drafted_classes = set()
    future_classes = set()
    for p in prospects:
        year = class_of(p)
        if year is None:
            continue
        (drafted_classes if is_drafted(p) else future_classes).add(year)

    # A class with any real pick counts as drafted; drop it from future.
    future_classes -= drafted_classes
    return {
        "drafted": sorted(drafted_classes),
        "future": sorted(future_classes),
        "current": max(drafted_classes) if drafted_classes else None,
        "all": sorted(drafted_classes | future_classes),
    }


def class_display_order(class_info: Mapping) -> List[int]:
    """The order classes should appear in the document.

    Current rookie class first, then the remaining drafted classes newest
    first, then the not-yet-drafted classes.

    Not simply "all classes, newest first": that puts the upcoming class
    (2027) above the rookie class (2026), because 2027 is the larger
    number. The page filters to the rookie class by default, so the
    difference is invisible while the JS runs -- and decisive the moment it
    does not. A static-site table whose first visible row is an undrafted
    player with no projection is the same failure Phil reported, just
    waiting for a script error.

    Undrafted classes last for the same reason they are not interleaved:
    their projections carry no draft capital and are not comparable with a
    drafted class's.
    """
    drafted = list(class_info.get("drafted") or [])
    future = list(class_info.get("future") or [])
    current = class_info.get("current")

    ordered: List[int] = []
    if current is not None:
        ordered.append(current)
    ordered.extend(sorted((y for y in drafted if y != current), reverse=True))
    ordered.extend(sorted(future, reverse=True))
    return ordered


def in_class(prospects: Sequence[Mapping], year: Optional[int]) -> List[Mapping]:
    if year is None:
        return list(prospects)
    return [p for p in prospects if class_of(p) == year]


def sort_for_display(prospects: Sequence[Mapping]) -> List[Mapping]:
    """Order a single class's board.

    Evidence-backed projections come before tier constants at equal value.
    Without that, a block of players sharing one tier constant sorts
    arbitrarily and outranks players whose lower number was actually
    derived from their own comps -- which is the "ordering at the top is
    meaningless" complaint.

    Within the constants, real draft order breaks the tie, because when the
    projection carries no information about the player, draft position is
    the only real signal left rather than an accident of dict order.
    """
    def key(p: Mapping):
        proj = p.get("projection") or {}
        basis = projection_basis(p)
        career = proj.get("projected_career_fp")
        try:
            career = float(career) if career is not None else -1.0
        except (TypeError, ValueError):
            career = -1.0
        drafted = prospect_pick(p)
        return (
            -career,
            0 if basis["evidence_backed"] else 1,
            drafted if drafted is not None else 10 ** 6,
            str(p.get("name") or "").lower(),
        )

    return sorted(prospects, key=key)


def prospect_pick(prospect: Mapping) -> Optional[int]:
    drafted = prospect.get("drafted") or {}
    try:
        return int(drafted.get("pick")) if drafted.get("pick") is not None else None
    except (TypeError, ValueError):
        return None


def saturation_report(prospects: Sequence[Mapping]) -> Dict:
    """How much of a board is tier constants rather than projections.

    Published on the page so the reader can see the size of the problem
    instead of inferring it from repeated numbers.
    """
    total = 0
    baseline = 0
    groups: Dict[Tuple[str, float], int] = {}
    for p in prospects:
        total += 1
        basis = projection_basis(p)
        if basis["key"] != "baseline":
            continue
        baseline += 1
        proj = p.get("projection") or {}
        try:
            value = float(proj.get("projected_career_fp") or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        groups[(str(p.get("position") or ""), value)] = (
            groups.get((str(p.get("position") or ""), value), 0) + 1
        )
    largest = max(groups.values()) if groups else 0
    return {
        "n": total,
        "n_baseline": baseline,
        "n_evidence_backed": total - baseline,
        "largest_identical_group": largest,
    }
