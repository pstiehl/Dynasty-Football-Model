"""Cross-league Manager Score — aggregate one manager across every league we index.

The question this answers
-------------------------
The Manager Score page ranks managers *within* one league. Mean 100, sd 15,
by construction — so the best manager in a twelve-team league and the best
manager in a ten-team league both score about 115, and neither number says
which of them is actually better. The owner's ask is the cross-league view:

    "ranked against 340 managers across 28 dynasty leagues we've indexed"

That requires a corpus, and a corpus requires a crawl, because Sleeper has
no endpoint that enumerates leagues (see ``docs/CROSS-LEAGUE-CORPUS.md``).

What this module owns
---------------------
The aggregation maths and the corpus artifact schema, and nothing else.

* Per-league scoring is **not** reimplemented here. It is the already-merged
  scoring core in ``dynasty.managerscore_js.MANAGERSCORE_CORE_JS``, executed
  by ``scripts/js/score_league_harness.js`` in node against the real page
  assembly code. This module consumes its output.
* Discovery/crawling lives in ``scripts/crawl_cross_league.py``.

Why the aggregation is Python and build-time, when per-league scoring is JS
and browser-time: the browser scores one league the user typed in, live. It
cannot crawl a corpus — that is hundreds of API calls against a rate-limited
third-party API on every page load, from every visitor. So the corpus is
precomputed and committed, and the page is a pure renderer of this artifact.

The aggregation formula
-----------------------
Per-league scoring already gives each manager, per component, a
within-league z-score built from a sample-size-shrunk per-transaction mean.
A z-score is the right unit to carry across leagues precisely *because* it
is already normalised per league: it means "this far from your own league's
average", which is comparable between a 10-team and a 14-team league.

For manager ``m`` and component ``c`` (draft / trade / waiver):

    N_c(m)    = Σ_leagues n_c(m, l)                       total scored events
    zbar_c(m) = Σ_l n_c(m,l) · z_c(m,l) / N_c(m)          evidence-weighted
    Z_c(m)    = zbar_c(m) · N_c(m) / (N_c(m) + K_c)       shrunk toward neutral

    composite(m) = Σ_c w_c · Z_c(m)      (weights renormalised over the
                                          components the corpus actually has)
    crossIndex(m) = 100 + 15 · composite(m)

Two properties are deliberate:

1. **Evidence weighting.** A manager's z in a league where they made 30
   picks counts ten times a league where they made 3. Averaging the leagues
   unweighted would let a single thin league swing a career.
2. **Shrinkage is applied twice, on purpose.** The per-league shrink asks
   "how much evidence inside this league?"; this one asks "how much evidence
   across the corpus?". They are different questions and a manager with one
   draft in one league should be pulled toward neutral by both. Both stages
   use the same ``n/(n+k)`` form and the same k constants as the merged
   scorer, so the guarantee the existing tests pin — shrinkage only ever
   moves a manager *toward* neutral, never flips a sign, never overshoots —
   holds end to end.

``crossIndex`` is deliberately **not** re-standardised across the corpus.
Re-centring on the corpus would make every manager's number move whenever
an unrelated league is indexed, which makes a score impossible to quote or
audit. Instead the score is stable and the *rank* carries the "against N
managers" meaning. That is also why rank and percentile are first-class
fields in the artifact rather than something the page derives.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

CORPUS_SCHEMA = "crossleague.corpus.v1"
CORPUS_ARTIFACT = "crossleague_corpus.json"

COMPONENTS = ("draft", "trade", "waiver")

# Mirrors MS_WEIGHTS / MS_SHRINK_K / MS_INDEX_* in managerscore_js.py. Kept as
# a named mirror rather than a second opinion: ``assert_scoring_constants_match``
# is called by the test suite so a change to the merged scorer breaks the build
# here instead of silently producing two different metrics.
MS_WEIGHTS = {"draft": 0.50, "trade": 0.35, "waiver": 0.15}
MS_SHRINK_K = {"draft": 6, "trade": 3, "waiver": 5}
MS_INDEX_CENTER = 100.0
MS_INDEX_SCALE = 15.0

# A manager needs this many scored picks in total before appearing on the
# draft-specific board. The owner asked "who are the best managers at
# drafting" — a one-pick manager with a lucky hit is not an answer to that,
# and shrinkage alone still leaves them rankable. Set to the draft shrink
# constant: the point at which the scorer half-trusts the average.
MIN_DRAFT_PICKS_FOR_BOARD = MS_SHRINK_K["draft"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def shrink(mean: float, n: int, k: float) -> float:
    """``n/(n+k)`` shrinkage toward zero — the merged scorer's ``msShrink``.

    Reproduced (not imported) because it is three lines of arithmetic in a
    JS string; ``assert_scoring_constants_match`` pins it against the real
    JS so the two cannot drift.
    """
    if not n or n <= 0:
        return 0.0
    return mean * (n / (n + k))


def assert_scoring_constants_match(core_js: str) -> None:
    """Fail loudly if the merged scorer's constants no longer match ours.

    Called from the test suite. The cross-league metric is only meaningful
    if it is the *same* metric aggregated, so a weight or shrink constant
    changing in ``managerscore_js.py`` must break this build rather than
    quietly produce a second, different Manager Score.
    """
    import re

    def _num(pattern: str) -> Optional[float]:
        m = re.search(pattern, core_js)
        return float(m.group(1)) if m else None

    checks = {
        "draft weight": (_num(r"MS_WEIGHTS\s*=\s*\{\s*draft:\s*([0-9.]+)"),
                         MS_WEIGHTS["draft"]),
        "trade weight": (_num(r"MS_WEIGHTS\s*=[^}]*trade:\s*([0-9.]+)"),
                         MS_WEIGHTS["trade"]),
        "waiver weight": (_num(r"MS_WEIGHTS\s*=[^}]*waiver:\s*([0-9.]+)"),
                          MS_WEIGHTS["waiver"]),
        "draft k": (_num(r"MS_SHRINK_K\s*=\s*\{\s*draft:\s*([0-9.]+)"),
                    MS_SHRINK_K["draft"]),
        "trade k": (_num(r"MS_SHRINK_K\s*=[^}]*trade:\s*([0-9.]+)"),
                    MS_SHRINK_K["trade"]),
        "waiver k": (_num(r"MS_SHRINK_K\s*=[^}]*waiver:\s*([0-9.]+)"),
                     MS_SHRINK_K["waiver"]),
        "index center": (_num(r"MS_INDEX_CENTER\s*=\s*([0-9.]+)"),
                         MS_INDEX_CENTER),
        "index scale": (_num(r"MS_INDEX_SCALE\s*=\s*([0-9.]+)"),
                        MS_INDEX_SCALE),
    }
    for label, (found, expected) in checks.items():
        if found is None:
            raise AssertionError(
                f"could not read {label} out of MANAGERSCORE_CORE_JS — the "
                "cross-league aggregator mirrors those constants and must be "
                "re-checked by hand"
            )
        if abs(found - expected) > 1e-12:
            raise AssertionError(
                f"{label} drifted: merged scorer says {found}, "
                f"dynasty.crossleague says {expected}. The cross-league board "
                "aggregates the per-league metric, so these must agree."
            )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate(
    league_results: Sequence[Dict],
    *,
    weights: Optional[Dict[str, float]] = None,
    shrink_k: Optional[Dict[str, float]] = None,
    min_draft_picks: int = MIN_DRAFT_PICKS_FOR_BOARD,
) -> Dict:
    """Aggregate per-league scoring results into a cross-league leaderboard.

    ``league_results`` is a sequence of per-league payloads as emitted by
    ``scripts/js/score_league_harness.js``::

        {
          "league_id": "...", "name": "...", "season": "2026",
          "n_teams": 12, "lineage_ids": [...], "lineage_root": "...",
          "result": { <exact msScoreLeague() return value> }
        }

    Returns ``{"managers": [...], "draft_board": [...], "components": {...}}``.
    Never raises on a malformed league — it is skipped and counted, because
    one bad crawl result must not lose the whole corpus.
    """
    weights = dict(weights or MS_WEIGHTS)
    shrink_k = dict(shrink_k or MS_SHRINK_K)

    # manager_id -> accumulator
    acc: Dict[str, Dict] = {}
    skipped: List[Dict] = []

    for entry in league_results or []:
        result = (entry or {}).get("result") or {}
        managers = result.get("managers")
        if not isinstance(managers, list) or not managers:
            skipped.append({
                "league_id": (entry or {}).get("league_id"),
                "reason": "no scored managers in result",
            })
            continue

        league_id = str(entry.get("league_id") or "")
        league_name = entry.get("name") or league_id
        season = entry.get("season")
        n_teams = entry.get("n_teams")

        for row in managers:
            if not isinstance(row, dict):
                continue
            mid = row.get("id")
            if not mid:
                continue
            mid = str(mid)
            a = acc.get(mid)
            if a is None:
                a = acc[mid] = {
                    "manager_id": mid,
                    # Sleeper display_name only. See the privacy note in
                    # docs/CROSS-LEAGUE-CORPUS.md — no real-identity
                    # resolution is attempted anywhere in this pipeline.
                    "display_name": row.get("name") or mid,
                    "components": {
                        c: {"n": 0, "weighted_z": 0.0} for c in COMPONENTS
                    },
                    "leagues": [],
                }
            if row.get("name"):
                a["display_name"] = row["name"]

            per_league_components = {}
            for c in COMPONENTS:
                comp = row.get(c) or {}
                n = comp.get("n") or 0
                z = comp.get("z") or 0.0
                try:
                    n = int(n)
                    z = float(z)
                except (TypeError, ValueError):
                    continue
                if n > 0:
                    a["components"][c]["n"] += n
                    a["components"][c]["weighted_z"] += n * z
                # n and z only. The per-league breakdown on the page renders
                # exactly these two (see xlRenderBreakdown: it prints z and
                # n per component and nothing else), and aggregation above
                # reads exactly these two. `mean` and `shrunk` are the raw
                # per-transaction scale and the within-league shrink of a
                # single league -- interesting on the Manager Score page,
                # which rebuilds them live, and dead weight here. Carrying
                # them cost ~0.5 MB across 1,223 managers for data nothing
                # reads.
                per_league_components[c] = {"n": n, "z": round(z, 6)}

            a["leagues"].append({
                "league_id": league_id,
                "name": league_name,
                "season": season,
                "n_teams": n_teams,
                "index": round(float(row.get("index") or 0.0), 1),
                "rank": row.get("rank"),
                "components": per_league_components,
                "flags": row.get("flags") or [],
            })

    # Which components does the corpus actually have? Mirrors the merged
    # scorer's rule: a component needs at least two managers with evidence
    # before it can define a distribution worth weighting.
    live = [
        c for c in COMPONENTS
        if sum(1 for a in acc.values() if a["components"][c]["n"] > 0) >= 2
    ]
    wsum = sum(weights[c] for c in live)
    effective = {
        c: (weights[c] / wsum if (wsum > 0 and c in live) else 0.0)
        for c in COMPONENTS
    }

    rows: List[Dict] = []
    for a in acc.values():
        comps_out = {}
        composite = 0.0
        for c in COMPONENTS:
            n = a["components"][c]["n"]
            zbar = (a["components"][c]["weighted_z"] / n) if n else 0.0
            z_shrunk = shrink(zbar, n, shrink_k[c])
            # `available` is dropped: it is exactly `n > 0`, so it is a
            # boolean restating the integer beside it, and at 1,283 managers
            # x 3 components it costs real bytes in a file rewritten daily.
            #
            # `zbar` STAYS. It looks like a pure intermediate and it is not:
            # the draft board publishes `draft_zbar` (the pre-shrink,
            # evidence-weighted mean) alongside the shrunk `z`, so dropping
            # it breaks aggregation outright. Verified the hard way.
            comps_out[c] = {
                "n": n,
                "zbar": round(zbar, 6),
                "z": round(z_shrunk, 6),
                "weight": round(effective[c], 6),
            }
            composite += effective[c] * z_shrunk

        flags = [
            f"no {c} activity in any indexed league — scored as corpus average"
            for c in COMPONENTS
            if effective[c] > 0 and a["components"][c]["n"] == 0
        ]
        n_draft = comps_out["draft"]["n"]
        if 0 < n_draft < shrink_k["draft"]:
            flags.append(
                f"only {n_draft} scored picks across all indexed leagues — "
                "heavily shrunk"
            )

        leagues = sorted(
            a["leagues"],
            key=lambda L: (str(L.get("season") or ""), str(L.get("name") or "")),
            reverse=True,
        )
        rows.append({
            "manager_id": a["manager_id"],
            "display_name": a["display_name"],
            "composite": round(composite, 6),
            "cross_index": round(MS_INDEX_CENTER + MS_INDEX_SCALE * composite, 1),
            "n_leagues": len(leagues),
            "components": comps_out,
            "flags": flags,
            "leagues": leagues,
        })

    rows.sort(key=lambda r: (-r["cross_index"], r["display_name"].lower()))
    n = len(rows)
    for i, r in enumerate(rows):
        r["rank"] = i + 1
        # Percentile as "share of the indexed field at or below this score".
        # Rank 1 of 340 is the 100th percentile, not the 0th.
        r["percentile"] = round(100.0 * (n - i) / n, 1) if n else None

    # ---- draft-specific board -------------------------------------------
    # The owner asked this explicitly: "who are the best managers at drafting
    # across every dynasty football league". Ranked on the draft component
    # alone, gated on evidence so the board answers the question asked.
    eligible = [
        r for r in rows
        if r["components"]["draft"]["n"] >= min_draft_picks
    ]
    eligible.sort(
        key=lambda r: (-r["components"]["draft"]["z"], r["display_name"].lower())
    )
    dn = len(eligible)
    draft_board = []
    for i, r in enumerate(eligible):
        draft_board.append({
            "rank": i + 1,
            "manager_id": r["manager_id"],
            "display_name": r["display_name"],
            "draft_z": r["components"]["draft"]["z"],
            "draft_zbar": r["components"]["draft"]["zbar"],
            "n_picks": r["components"]["draft"]["n"],
            "n_leagues": r["n_leagues"],
            "percentile": round(100.0 * (dn - i) / dn, 1) if dn else None,
        })

    return {
        "managers": rows,
        "draft_board": draft_board,
        "components": {
            "live": live,
            "weights": {c: round(effective[c], 6) for c in COMPONENTS},
            "shrink_k": dict(shrink_k),
            "min_draft_picks_for_board": min_draft_picks,
        },
        "skipped_leagues": skipped,
    }


# ---------------------------------------------------------------------------
# Lineage de-duplication
# ---------------------------------------------------------------------------

def dedupe_lineages(league_results: Sequence[Dict]) -> tuple[List[Dict], List[Dict]]:
    """Collapse league-years belonging to the same dynasty chain.

    A dynasty league is a chain of one league id per season linked by
    ``previous_league_id``. The seed alone is four ids for one league:
    2026 → 2025 → 2024 → 2023. Counting those as four leagues would inflate
    coverage fourfold and count every manager four times.

    The primary defence is in the crawler: discovery enumerates a single
    season, and a chain holds exactly one league per season, so two
    distinct same-season ids cannot share a lineage. This is the safety net
    for the case the invariant does not cover — a hand-added seed that is a
    prior-season id of a league also discovered via its current-season head.

    Keeps the entry covering the most seasons (the longest chain), because
    that is the one whose score rests on the most evidence.

    Returns ``(kept, dropped)``.
    """
    ordered = sorted(
        league_results or [],
        key=lambda e: len((e or {}).get("lineage_ids") or []),
        reverse=True,
    )
    claimed: Dict[str, str] = {}   # league id -> owning entry's league_id
    kept: List[Dict] = []
    dropped: List[Dict] = []

    for entry in ordered:
        ids = {str(x) for x in ((entry or {}).get("lineage_ids") or []) if x}
        lid = str((entry or {}).get("league_id") or "")
        if lid:
            ids.add(lid)
        clash = next((claimed[i] for i in sorted(ids) if i in claimed), None)
        if clash is not None:
            dropped.append({
                "league_id": lid,
                "name": (entry or {}).get("name"),
                "reason": f"same dynasty chain as already-indexed league {clash}",
            })
            continue
        for i in ids:
            claimed[i] = lid
        kept.append(entry)
    return kept, dropped


# ---------------------------------------------------------------------------
# Corpus artifact
# ---------------------------------------------------------------------------

def build_corpus(
    league_results: Sequence[Dict],
    *,
    budget: Optional[Dict] = None,
    discovery: Optional[Dict] = None,
    filter_meta: Optional[Dict] = None,
    previous: Optional[Dict] = None,
    generated_at: Optional[datetime] = None,
) -> Dict:
    """Assemble the committed corpus artifact.

    ``previous`` is the corpus from the last run, if one was committed. Its
    only use here is provenance — ``first_indexed_at`` and ``n_runs`` are
    what let the page say "indexed since <date>, N runs" when persistence is
    working, and fall back to "indexed this run" when it is not. That is the
    whole graceful-degradation mechanism, and it needs no code path of its
    own: no committed corpus simply means no prior provenance to carry.
    """
    generated_at = generated_at or _now()
    kept, dropped = dedupe_lineages(league_results)
    agg = aggregate(kept)

    seasons = sorted({
        str(e.get("season")) for e in kept if e.get("season")
    })
    leagues_meta = [{
        "league_id": str(e.get("league_id") or ""),
        "name": e.get("name"),
        "season": e.get("season"),
        "n_teams": e.get("n_teams"),
        "n_seasons_scored": len(e.get("lineage_ids") or []) or 1,
        "lineage_root": e.get("lineage_root"),
        "n_managers": len((e.get("result") or {}).get("managers") or []),
        "discovered_via": e.get("discovered_via"),
        "hop": e.get("hop"),
        "scored_picks": ((e.get("result") or {}).get("meta") or {}).get("nPicksScored"),
        "scored_trades": ((e.get("result") or {}).get("meta") or {}).get("nTradesScored"),
        "scored_waivers": ((e.get("result") or {}).get("meta") or {}).get("nWaivers"),
    } for e in kept]

    prev = previous or {}
    prev_first = prev.get("first_indexed_at")
    prev_runs = prev.get("n_runs")
    try:
        prev_runs = int(prev_runs)
    except (TypeError, ValueError):
        prev_runs = 0

    notes: List[str] = []
    if not prev_first:
        notes.append(
            "no previously committed corpus was found, so this artifact "
            "describes only what this run indexed"
        )
    if dropped:
        notes.append(
            f"{len(dropped)} league(s) dropped as duplicate dynasty chains"
        )
    if agg["skipped_leagues"]:
        notes.append(
            f"{len(agg['skipped_leagues'])} league(s) discovered but not "
            "scorable (no priced transactions)"
        )

    return {
        "schema": CORPUS_SCHEMA,
        "generated_at": generated_at.isoformat(),
        "first_indexed_at": prev_first or generated_at.isoformat(),
        "n_runs": prev_runs + 1,
        "persisted": bool(prev_first),
        "coverage": {
            "n_leagues": len(kept),
            "n_managers": len(agg["managers"]),
            "n_league_seasons_scored": sum(
                m["n_seasons_scored"] for m in leagues_meta
            ),
            "seasons": seasons,
        },
        "filter": filter_meta or {},
        "budget": budget or {},
        "discovery": discovery or {},
        "components": agg["components"],
        "leaderboard": agg["managers"],
        "draft_board": agg["draft_board"],
        "leagues": leagues_meta,
        "dropped_leagues": dropped,
        "skipped_leagues": agg["skipped_leagues"],
        "notes": notes,
    }


def compact_result(entry: Dict) -> Dict:
    """Strip a per-league scoring result down to what re-aggregation needs.

    ``msScoreLeague`` returns a full audit trail — every priced pick, every
    trade leg, every waiver claim. The per-league page needs that; the corpus
    does not, and it is enormous: one 8-season league in the first real run
    carried 510 picks, 219 trades and 1,310 waiver claims, and the retained
    block came to **2.3 MB for five leagues**.

    That matters because the corpus is *committed on every daily run*. A
    multi-megabyte artifact rewritten daily bloats the repository permanently
    and for nothing: aggregation only ever reads each manager's per-component
    ``n`` and ``z``, plus the league's headline counts.

    So retained entries keep exactly that. The audit stays where it is
    reproducible — the per-league page rebuilds it live from Sleeper on
    demand.
    """
    result = (entry or {}).get("result") or {}
    managers = []
    for row in result.get("managers") or []:
        if not isinstance(row, dict):
            continue
        slim = {
            "id": row.get("id"),
            "name": row.get("name"),
            "index": row.get("index"),
            "rank": row.get("rank"),
            "flags": row.get("flags") or [],
        }
        for c in COMPONENTS:
            comp = row.get(c) or {}
            # Same reasoning as the per-league block in aggregate(): the only
            # fields re-aggregation consumes are n and z. Retaining the
            # others would preserve a number no consumer reads, in a file
            # rewritten on every daily run.
            slim[c] = {
                "n": comp.get("n") or 0,
                "z": round(float(comp.get("z") or 0.0), 6),
            }
        managers.append(slim)

    meta = result.get("meta") or {}
    return {
        "league_id": entry.get("league_id"),
        "name": entry.get("name"),
        "season": entry.get("season"),
        "n_teams": entry.get("n_teams"),
        "lineage_ids": entry.get("lineage_ids"),
        "lineage_root": entry.get("lineage_root"),
        "discovered_via": entry.get("discovered_via"),
        "hop": entry.get("hop"),
        "retained_from": entry.get("retained_from"),
        "result": {
            "managers": managers,
            "meta": {
                k: meta.get(k) for k in (
                    "nPicksScored", "nTradesScored", "nTrades",
                    "nTradesUnbalanced", "nTradesWithFaab", "nWaivers",
                    "unvaluedAssets", "liveComponents", "floor",
                ) if k in meta
            },
        },
    }


def merge_corpus(previous: Optional[Dict], league_results: Sequence[Dict]) -> List[Dict]:
    """Union this run's league results with those retained from a prior run.

    The corpus grows across runs only if the previous artifact was actually
    committed. Retained entries carry their own scoring result, so a league
    that drops out of this run's crawl budget does not vanish from the
    board — it just ages. ``retained_from`` marks it so the page can show
    staleness rather than implying a fresh read.

    This run's result always wins for a league present in both.
    """
    fresh = {str(e.get("league_id")): e for e in (league_results or [])
             if e and e.get("league_id")}
    out = list(fresh.values())
    prev = previous or {}
    if not prev.get("first_indexed_at"):
        return out

    for entry in prev.get("retained") or []:
        lid = str((entry or {}).get("league_id") or "")
        if lid and lid not in fresh:
            e = dict(entry)
            e["retained_from"] = entry.get("retained_from") or prev.get("generated_at")
            out.append(e)
    return out


def write_corpus_artifact(out_root: Path, corpus: Dict) -> Path:
    """Publish the corpus into the site build.

    The ``retained`` block is dropped here. It is the crawler's working
    state -- the compacted per-league results that the NEXT run re-aggregates
    so leagues outside today's budget keep their scores -- and the page never
    reads it. Shipping it made every visitor download ~450 KB (26% of the
    artifact) of data that exists only so a CI job can resume.

    It stays in the committed ``data/cross_league/corpus.json``, which is
    where resumption actually reads it from.
    """
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    path = out_root / CORPUS_ARTIFACT
    published = {k: v for k, v in corpus.items() if k != "retained"}
    published["retained_omitted"] = {
        "n_leagues": len(corpus.get("retained") or []),
        "why": ("crawler resumption state; kept in the repository artifact, "
                "not needed to render this page"),
    }
    path.write_text(json.dumps(published, separators=(",", ":")),
                    encoding="utf-8")
    return path


def load_corpus(path: Path) -> Optional[Dict]:
    """Read a committed corpus, or ``None`` if absent/unreadable.

    Never raises: a corrupt corpus must degrade to "indexed this run", not
    break the daily build.
    """
    try:
        p = Path(path)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != CORPUS_SCHEMA:
        return None
    return data


def coverage_sentence(corpus: Dict) -> str:
    """The exact sentence the owner asked for, built from real counts.

    "ranked against 340 managers across 28 dynasty leagues we've indexed"

    Deliberately a function of the artifact rather than a hand-written
    string: the numbers can only ever be what was actually indexed, and the
    wording never claims coverage of Sleeper as a whole.
    """
    cov = (corpus or {}).get("coverage") or {}
    n_m = cov.get("n_managers") or 0
    n_l = cov.get("n_leagues") or 0
    return (
        f"ranked against {n_m:,} manager{'' if n_m == 1 else 's'} across "
        f"{n_l:,} dynasty league{'' if n_l == 1 else 's'} we've indexed"
    )
