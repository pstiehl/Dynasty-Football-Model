"""Durable crawl state: what makes the corpus compound instead of restart.

The problem this solves
-----------------------
The first cross-league build crawled from the seed registry, spent its whole
600-call budget, and stopped. The next day it did the same thing: same seed,
same two hops, same five leagues, same 600 calls. The corpus could not grow,
because every run threw away everything the previous run had learned about
where it had already been.

Two different things have to persist for a crawl to resume rather than
repeat, and they have different lifetimes:

1. **Where the walk had got to.** The breadth-first frontier of leagues that
   were discovered but never expanded, plus the set of leagues and users
   already examined. Without this, run N+1 re-walks run N's ground to reach
   the same edge of the graph.
2. **What has already been paid for.** Which dynasty leagues have been
   scored, and when. Without this, the scorer cannot tell a league it has
   never seen from one it scored yesterday, so it cannot prefer the former.

Both live in ``data/cross_league/crawl_state.json``, committed by the daily
job. The site is static on GitHub Pages, so a committed artifact is the only
durable storage in the system -- the same reason the corpus itself is
committed.

Losing this file is not a correctness problem. The seed registry still holds
every league ever confirmed dynasty, so a cold start re-reaches all of them;
what is lost is the accumulated *frontier*, i.e. reach into leagues found but
not yet expanded. That is a cost, not a corruption, and it is why the seed
registry is maintained as a separate, hand-editable, monotonically growing
file rather than being folded into this one.

Budget policy
-------------
``plan_scoring`` implements the priority that actually makes the corpus grow:

* **New leagues first**, up to a reserved share of the run's scoring budget.
  A run that spent everything refreshing what it already had would be a
  very expensive no-op.
* **Then the stalest already-indexed leagues**, with whatever is left. These
  are cheap now -- their completed seasons come from the permanent cache, so
  only the live season costs calls.
* A league that is never reached keeps its last score. The page already says
  so, and ``crossleague.merge_corpus`` already carries it forward.

Everything here is pure stdlib and does no I/O beyond one JSON file, so the
resumption logic is testable without a network or a Sleeper fixture.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

STATE_SCHEMA = "crossleague-crawl-state/2"

# The examined-user set is the one unbounded structure here: every league
# expansion adds up to a dozen user ids and they are never invalidated. At
# ~19 bytes an id plus JSON overhead, 200k ids is roughly 6 MB -- large
# enough to be rude to a git repo, small enough that we will not hit it for
# years. When it is hit, the OLDEST entries are dropped: re-examining a user
# we saw long ago costs one call and is self-correcting, whereas dropping
# recent ones would make the active frontier thrash.
MAX_SEEN_USERS = 200_000
MAX_SEEN_LEAGUES = 200_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def empty_state(season: Optional[int] = None) -> Dict:
    return {
        "schema": STATE_SCHEMA,
        "updated_at": None,
        "season": season,
        "n_runs": 0,
        "frontier": [],
        "seen_leagues": [],
        "seen_users": [],
        "known_dynasty": {},
        "scored": {},
    }


def load_state(path: Path, season: Optional[int] = None) -> Dict:
    """Read committed crawl state, or a clean one.

    Never raises. A corrupt or schema-mismatched state file degrades to a
    cold start, which is slower but correct -- exactly the tradeoff
    ``crossleague.load_corpus`` makes for the corpus artifact.

    A change of season also resets the walk: ``discover`` enumerates a single
    season so that dynasty chains de-duplicate soundly (one league id per
    season per chain), which means last season's frontier and examined-league
    set describe league ids that are no longer the ones being enumerated.
    Carrying them over would silently suppress this season's leagues. The
    scored/known-dynasty history is *not* reset, because that is what makes
    leagues survive into the new year.
    """
    try:
        p = Path(path)
        if not p.exists():
            return empty_state(season)
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_state(season)

    if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
        return empty_state(season)

    for key, default in (
        ("frontier", []), ("seen_leagues", []), ("seen_users", []),
        ("known_dynasty", {}), ("scored", {}),
    ):
        if not isinstance(data.get(key), type(default)):
            data[key] = default if isinstance(default, list) else {}

    if season is not None and data.get("season") not in (None, season):
        carried = {
            "known_dynasty": data.get("known_dynasty") or {},
            "scored": data.get("scored") or {},
            "n_runs": data.get("n_runs") or 0,
        }
        fresh = empty_state(season)
        fresh.update(carried)
        fresh["season_rolled_from"] = data.get("season")
        return fresh

    data["season"] = season if season is not None else data.get("season")
    return data


def save_state(path: Path, state: Dict) -> Path:
    """Persist state, trimming the unbounded sets. Temp + rename."""
    state = dict(state)
    state["schema"] = STATE_SCHEMA
    state["updated_at"] = _now_iso()
    state["seen_users"] = list(state.get("seen_users") or [])[-MAX_SEEN_USERS:]
    state["seen_leagues"] = list(state.get("seen_leagues") or [])[-MAX_SEEN_LEAGUES:]

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=False), encoding="utf-8")
    tmp.replace(p)
    return p


def record_discovery(
    state: Dict,
    *,
    frontier: Sequence,
    seen_leagues: Sequence[str],
    seen_users: Sequence[str],
    dynasty: Sequence[Dict],
) -> Dict:
    """Fold one discovery pass back into durable state.

    ``frontier`` entries are ``(league_id, hop)`` pairs still awaiting
    expansion. They are stored as objects rather than tuples so the file
    stays readable by a human deciding whether to hand-edit it.
    """
    state["frontier"] = [
        {"league_id": str(lid), "hop": int(hop)} for lid, hop in frontier
    ]
    state["seen_leagues"] = sorted({str(x) for x in seen_leagues})
    state["seen_users"] = sorted({str(x) for x in seen_users})

    known = dict(state.get("known_dynasty") or {})
    for lg in dynasty:
        lid = str(lg.get("league_id") or "")
        if not lid:
            continue
        prev = known.get(lid) or {}
        entry = dict(lg)
        # First-seen provenance wins: how a league entered the corpus is a
        # fact about history, and rewriting it on every run would make the
        # "discovered_via" column meaningless.
        entry["first_seen_at"] = prev.get("first_seen_at") or _now_iso()
        if prev.get("discovered_via"):
            entry["discovered_via"] = prev["discovered_via"]
            entry["hop"] = prev.get("hop", entry.get("hop"))
        known[lid] = entry
    state["known_dynasty"] = known
    return state


def record_scoring(state: Dict, results: Sequence[Dict]) -> Dict:
    """Record the outcome of a scoring pass, successes and failures alike.

    Failures are recorded deliberately. A league that cannot be scored --
    private, empty, an unreadable chain -- would otherwise stay permanently
    at the front of the "never scored" queue and be retried first on every
    single run, which is how a crawl gets stuck. Recording the attempt lets
    it age into the normal staleness ordering instead.
    """
    scored = dict(state.get("scored") or {})
    for r in results:
        lid = str(r.get("league_id") or "")
        if not lid:
            continue
        prev = scored.get(lid) or {}
        ok = bool(r.get("ok"))
        scored[lid] = {
            "last_attempt_at": _now_iso(),
            "last_ok_at": _now_iso() if ok else prev.get("last_ok_at"),
            "ok": ok,
            "reason": None if ok else (r.get("reason") or "")[:200],
            "calls": r.get("calls"),
            "n_attempts": int(prev.get("n_attempts") or 0) + 1,
            "n_seasons": r.get("n_seasons") or prev.get("n_seasons"),
        }
    state["scored"] = scored
    state["n_runs"] = int(state.get("n_runs") or 0) + 1
    return state


def merge_seed_frontier(state: Dict, seed_ids: Sequence[str]) -> Dict:
    """Ensure every registry seed is reachable, without re-walking the graph.

    A seed that has never been examined is pushed onto the frontier at hop 0.
    A seed already in ``seen_leagues`` is left alone: it was walked on an
    earlier run and re-expanding it would spend calls to rediscover leagues
    already in ``known_dynasty``.

    This is what makes the registry the durable floor on coverage. Operators
    (and the issue-submission ingest) add ids here; the crawl picks them up
    on its next run without anyone touching crawl state.
    """
    seen = set(state.get("seen_leagues") or [])
    have = {str(f.get("league_id")) for f in state.get("frontier") or []}
    frontier = list(state.get("frontier") or [])
    for sid in seed_ids:
        sid = str(sid)
        if sid in seen or sid in have:
            continue
        frontier.append({"league_id": sid, "hop": 0})
        have.add(sid)
    state["frontier"] = frontier
    return state


def plan_scoring(
    state: Dict,
    *,
    max_leagues: int,
    new_league_share: float = 0.6,
    scoring_budget: int = 0,
    est_calls_new: int = 160,
) -> Dict:
    """Choose which leagues this run scores, and in what order.

    Returns ``{"new": [...], "refresh": [...], "reserved_for_new": int}``.

    The ordering encodes the growth policy:

    * Never-scored leagues first, capped by the reserved share of the budget
      and by ``max_leagues``. These are the only leagues that can make the
      corpus bigger.
    * Then already-indexed leagues, stalest first. With the permanent cache
      these are cheap -- only their live season costs calls -- so refreshing
      them is close to free once their history is banked.

    Leagues whose last attempt FAILED sort after successful ones of the same
    age, so a permanently unreadable league cannot monopolise the queue.
    """
    known = state.get("known_dynasty") or {}
    scored = state.get("scored") or {}

    never = [lid for lid in known if lid not in scored]
    seen_before = [lid for lid in known if lid in scored]

    def staleness_key(lid: str):
        rec = scored.get(lid) or {}
        return (0 if rec.get("ok") else 1, str(rec.get("last_attempt_at") or ""))

    # Stable, deterministic ordering for the new queue: first discovered
    # first scored, so a run is reproducible and the queue cannot reshuffle
    # between runs in a way that starves the same leagues forever.
    never.sort(key=lambda lid: (
        str((known.get(lid) or {}).get("first_seen_at") or ""),
        int((known.get(lid) or {}).get("hop") or 0),
        lid,
    ))
    seen_before.sort(key=staleness_key)

    reserved = int(max(0, scoring_budget) * max(0.0, min(1.0, new_league_share)))
    n_new_affordable = max(0, reserved // max(1, est_calls_new))
    new_batch = never[: min(max_leagues, n_new_affordable)]
    refresh_batch = seen_before[: max(0, max_leagues - len(new_batch))]

    return {
        "new": [dict(known[lid]) for lid in new_batch],
        "refresh": [dict(known[lid]) for lid in refresh_batch],
        "reserved_for_new": reserved,
        "n_never_scored_total": len(never),
        "n_previously_scored_total": len(seen_before),
    }


def state_summary(state: Dict) -> Dict:
    """Compact, publishable view of crawl progress.

    Goes into the corpus artifact so the page can say how much is queued,
    not just how much is done. "5 leagues indexed, 0 queued" and "5 indexed,
    61 queued" describe very different systems and the difference should be
    visible.
    """
    known = state.get("known_dynasty") or {}
    scored = state.get("scored") or {}
    ok_ids = {k for k, v in scored.items() if v.get("ok")}
    return {
        "n_dynasty_leagues_discovered": len(known),
        "n_scored_ok": len(ok_ids),
        "n_never_scored": len([k for k in known if k not in scored]),
        "n_failed_last_attempt": len(
            [k for k, v in scored.items() if not v.get("ok")]
        ),
        "frontier_depth": len(state.get("frontier") or []),
        "n_leagues_examined_all_time": len(state.get("seen_leagues") or []),
        "n_users_examined_all_time": len(state.get("seen_users") or []),
        "crawl_runs": int(state.get("n_runs") or 0),
        "updated_at": state.get("updated_at"),
    }
