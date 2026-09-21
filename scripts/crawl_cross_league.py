"""Build the cross-league Manager Score corpus: a bounded crawl of Sleeper.

Read ``docs/CROSS-LEAGUE-CORPUS.md`` before changing anything here. The short
version of the constraints, all verified rather than assumed:

* **Sleeper cannot be enumerated.** There is no endpoint that lists leagues.
  You can GET a league by id, or a user's leagues by user_id, and that is all.
  League ids are 19-digit snowflakes, so the id space cannot be swept. The
  only discovery path is the social graph:

      league -> /league/{id}/users -> /user/{user_id}/leagues/nfl/{season}
             -> their leagues -> their users -> ...

* **The API is free for non-commercial use only.** Sleeper's published terms:
  "free to use for non-commercial purposes... For commercial use of the
  Sleeper API, please reach out to us directly to discuss licensing." If this
  project is ever monetised, this crawl is the first thing that needs a
  licence conversation. That is recorded in docs/ so it cannot be forgotten.

* **Rate guidance is under 1000 calls/minute** or you risk an IP block. This
  crawler runs nowhere near that, deliberately.

Therefore this is a **bounded** crawl and never an attempt at coverage. Every
limit is an explicit, configurable cap, and every run reports the budget it
actually consumed. Exhaustive coverage of Sleeper is not a goal, is not
attempted, and the page never claims it.

Incremental by design
---------------------
A bounded crawl that starts from scratch every day is a treadmill: the first
build spent its entire 600-call budget re-reading the same five leagues and
had nothing left to find a sixth. Two mechanisms fix that, and neither is a
bigger budget:

* **Durable crawl state** (``dynasty.crawl_state``) remembers the frontier,
  the leagues and users already examined, and which leagues have been scored
  and when. Each run resumes the walk instead of repeating it, and spends its
  scoring budget on leagues it has never seen.
* **A permanent response cache** (``scripts/js/sleeper_disk_cache.js``) keeps
  completed seasons forever. Re-scoring an indexed league then costs only its
  live season, so "keep the corpus fresh" stops competing with "make the
  corpus bigger".

Both are committed artifacts, because the site is static on GitHub Pages and
a committed file is the only durable storage in the system.

Usage
-----
    # cheap: discovery only, no scoring, prints what it would index
    python scripts/crawl_cross_league.py --discover-only

    # full run with conservative caps
    python scripts/crawl_cross_league.py --max-leagues 12 --max-calls 400

    # what the daily job runs (caps come from the registry/defaults)
    python scripts/crawl_cross_league.py --write-corpus

    # fold every dynasty league found so far into the seed registry, so a
    # lost crawl state can never cost coverage
    python scripts/crawl_cross_league.py --write-corpus --update-seeds
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

SLEEPER = "https://api.sleeper.app/v1"
SEEDS_PATH = REPO_ROOT / "data" / "cross_league" / "seeds.json"
CORPUS_PATH = REPO_ROOT / "data" / "cross_league" / "corpus.json"
STATE_PATH = REPO_ROOT / "data" / "cross_league" / "crawl_state.json"
CACHE_DIR = REPO_ROOT / "data" / "cross_league" / "cache"
CONSENSUS_DIR = REPO_ROOT / "data" / "consensus"
HARNESS = REPO_ROOT / "scripts" / "js" / "score_league_harness.js"

# Honest, self-identifying User-Agent. This project does not spoof browsers
# to get around access controls (see the PFR notes in the daily workflow).
USER_AGENT = (
    "Dynasty-Football-Model/cross-league-corpus "
    "(+https://github.com/pstiehl/Dynasty-Football-Model)"
)

# --------------------------------------------------------------------------
# The dynasty filter
# --------------------------------------------------------------------------
# Sleeper's ``settings.type`` is the league format discriminator. Sleeper's
# public docs describe the settings object without enumerating this field, so
# the value was established empirically against real league objects rather
# than guessed -- and the crawl re-checks it on every run (see
# ``filter_evidence`` below), because a filter nobody re-validates is a filter
# that silently rots.
#
# Observed, on real leagues reachable from the seed:
#
#   type=2  "Dallas Kings"      taxi_slots=3  max_keepers=1   <- dynasty
#   type=2  "DLD"               taxi_slots=8  max_keepers=1   <- dynasty
#   type=2  "Game of Inches"    taxi_slots=2  max_keepers=1   <- dynasty
#   type=1  "Playing For Keeps" taxi_slots=0  max_keepers=5   <- keeper
#   type=1  "Deke Dynasty"      taxi_slots=0  max_keepers=18  <- keeper
#
# Two things that matter fall out of that table:
#
#  * type=2 co-occurs with taxi squads and max_keepers=1 (a dynasty league
#    keeps everyone, so "keepers" is not the mechanism); type=1 co-occurs with
#    a finite keeper count and no taxi squad. That is the signature of
#    dynasty vs keeper, and it is why 2 is the filter value.
#  * "Deke Dynasty" is **type=1**. A league with "dynasty" in its name can be
#    a keeper league, so name matching would be wrong. Deep-keeper leagues
#    (18 keepers!) play a lot like dynasty, but Sleeper does not classify them
#    as dynasty and neither do we: the corpus indexes what the platform calls
#    a dynasty league, which is a definition we do not have to defend.
DYNASTY_LEAGUE_TYPE = 2


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Budget:
    """Explicit, configurable caps. Nothing here is unbounded.

    Three independent ceilings, because they bound three different risks and
    collapsing them would lose one:

    * ``max_calls``      -- total politeness toward a free API.
    * ``max_discovery_calls`` -- a sub-cap so a wide social graph cannot eat
      the budget before a single league is scored. Discovery is cheap per
      call but unbounded in breadth: every league expands to ~12 users and
      every user to all their leagues, so an uncapped discovery pass on a
      large frontier will happily spend everything.
    * ``max_seconds``    -- wall clock, so a daily job stays a daily job.

    Cache hits are counted but are NOT calls: they never touch the network,
    so they cannot exceed a rate limit or a budget. Reporting them separately
    is what makes the cost reduction auditable rather than asserted.
    """

    def __init__(self, *, max_calls: int, max_leagues: int, max_hops: int,
                 min_delay_s: float, max_discovery_calls: Optional[int] = None,
                 max_seconds: float = 0.0):
        self.max_calls = max_calls
        self.max_leagues = max_leagues
        self.max_hops = max_hops
        self.min_delay_s = min_delay_s
        self.max_discovery_calls = (
            max_calls if max_discovery_calls is None else max_discovery_calls
        )
        self.max_seconds = max_seconds
        self.started_at = time.monotonic()
        self.discovery_calls = 0
        self.scoring_calls = 0
        self.cache_hits = 0
        self.refused = 0
        self.errors = 0

    @property
    def calls(self) -> int:
        return self.discovery_calls + self.scoring_calls

    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - self.calls)

    @property
    def discovery_remaining(self) -> int:
        """Whichever of the two discovery ceilings binds first."""
        return max(0, min(
            self.max_calls - self.calls,
            self.max_discovery_calls - self.discovery_calls,
        ))

    @property
    def seconds_remaining(self) -> float:
        if not self.max_seconds:
            return float("inf")
        return self.max_seconds - (time.monotonic() - self.started_at)

    def expired(self) -> bool:
        return self.seconds_remaining <= 0

    def to_dict(self) -> Dict:
        return {
            "max_calls": self.max_calls,
            "max_discovery_calls": self.max_discovery_calls,
            "max_leagues": self.max_leagues,
            "max_hops": self.max_hops,
            "max_seconds": self.max_seconds,
            "min_delay_seconds": self.min_delay_s,
            "calls_used_total": self.calls,
            "calls_used_discovery": self.discovery_calls,
            "calls_used_scoring": self.scoring_calls,
            "cache_hits_served_free": self.cache_hits,
            "calls_refused_over_budget": self.refused,
            "network_errors": self.errors,
            "elapsed_seconds": round(time.monotonic() - self.started_at, 1),
        }


class Sleeper:
    """Minimal budgeted, rate-limited, cached Sleeper reader."""

    def __init__(self, budget: Budget, *, verbose: bool = True):
        self.b = budget
        self.verbose = verbose
        self._cache: Dict[str, object] = {}
        self._last = 0.0

    def get(self, url: str, default=None):
        """GET and parse JSON. Returns ``default`` on 404/error/over-budget.

        Caching is not an optimisation here so much as a correctness measure:
        the social graph is full of cycles (every member of a league leads
        back to that league), so an uncached walk would re-fetch the same
        users endlessly and burn the budget on nothing.

        Note this in-process cache is deliberately NOT the durable one. What
        discovery reads -- a league's current settings, a user's current
        league list -- is mutable by definition: people join and leave
        leagues, and a league can change type. Persisting those would make
        the crawl blind to exactly the changes it exists to notice. Only
        completed seasons are cached across runs, and that happens in the
        scoring harness where the season is known.
        """
        if url in self._cache:
            return self._cache[url]
        if self.b.discovery_remaining <= 0 or self.b.expired():
            self.b.refused += 1
            return default

        gap = self.b.min_delay_s - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)

        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        self.b.discovery_calls += 1
        self._last = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 404 is a normal answer (no such league, no transactions that
            # week) and must not abort a crawl.
            if exc.code != 404:
                self.b.errors += 1
                if self.verbose:
                    print(f"    ! HTTP {exc.code} {url}", file=sys.stderr)
            self._cache[url] = default
            return default
        except Exception as exc:  # noqa: BLE001
            self.b.errors += 1
            if self.verbose:
                print(f"    ! {type(exc).__name__} {url}", file=sys.stderr)
            self._cache[url] = default
            return default

        self._cache[url] = data
        return data

    def league(self, league_id: str) -> Optional[Dict]:
        return self.get(f"{SLEEPER}/league/{league_id}", None)

    def league_users(self, league_id: str) -> List[Dict]:
        return self.get(f"{SLEEPER}/league/{league_id}/users", []) or []

    def user_leagues(self, user_id: str, season: int) -> List[Dict]:
        return self.get(
            f"{SLEEPER}/user/{user_id}/leagues/nfl/{season}", []
        ) or []


def is_dynasty(league: Dict) -> bool:
    settings = (league or {}).get("settings") or {}
    return settings.get("type") == DYNASTY_LEAGUE_TYPE


def discover(
    sl: Sleeper,
    season: int,
    budget: Budget,
    *,
    state: Dict,
    max_discover: int = 250,
    verbose: bool = True,
) -> Dict:
    """Resumable breadth-first walk of the Sleeper social graph.

    Returns newly discovered dynasty leagues, the frontier left unexpanded,
    and the evidence that re-validates the dynasty filter on this run's real
    data.

    **Resumption.** The frontier and the examined-league/user sets are loaded
    from durable crawl state and written back, so a run continues the walk
    rather than repeating it. This is the difference between a crawl that
    compounds and one that treadmills: the first build re-walked the same
    seed to the same five leagues every single day.

    The walk is bounded by ``max_hops`` *per run* measured from wherever the
    frontier currently sits, not from the seeds. A league parked on the
    frontier at hop 2 is expanded on the next run as a hop-0 entry, so reach
    grows by ``max_hops`` per day instead of being permanently capped at
    ``max_hops`` from the seed. That is deliberate, and it is the mechanism
    by which a two-hop-per-run crawl eventually reaches far-away leagues
    without ever issuing a large burst.

    Discovery deliberately enumerates a **single season**. That is what makes
    de-duplication of dynasty chains sound: a chain holds exactly one league
    per season, so two distinct same-season league ids cannot belong to the
    same lineage. Prior seasons are then picked up by the scorer, which walks
    ``previous_league_id`` itself — so history is scored without ever being
    discovered as separate leagues.

    ``max_discover`` caps how many NEW dynasty leagues one run adds to the
    queue. It is not the scoring cap: discovery is cheap (~2 calls per league
    examined) and scoring is not (~160 calls per league), so the two are
    separated on purpose. Finding sixty leagues today and scoring them over
    the following days is exactly the intended shape.
    """
    found: Dict[str, Dict] = {}
    seen_leagues: Set[str] = {str(x) for x in (state.get("seen_leagues") or [])}
    seen_users: Set[str] = {str(x) for x in (state.get("seen_users") or [])}
    known: Dict[str, Dict] = dict(state.get("known_dynasty") or {})
    type_counter: Counter = Counter()
    marker_rows: List[Dict] = []

    # Hops are counted from this run's starting frontier, not from the seeds.
    frontier: deque = deque(
        (str(f.get("league_id")), 0)
        for f in (state.get("frontier") or [])
        if f.get("league_id")
    )

    def _stop() -> bool:
        return (budget.discovery_remaining <= 0 or budget.expired()
                or len(found) >= max_discover)

    while frontier and not _stop():
        league_id, hop = frontier.popleft()

        # An unexamined league needs its own object read before anything
        # else: that is where the dynasty filter and the seed check happen.
        if league_id not in seen_leagues:
            lg = sl.league(league_id)
            seen_leagues.add(league_id)
            if not lg or not lg.get("league_id"):
                if verbose:
                    print(f"  league {league_id}: not found on Sleeper",
                          file=sys.stderr)
                continue
            _record(lg, type_counter, marker_rows)
            if is_dynasty(lg) and lg.get("sport") in (None, "nfl"):
                if league_id not in known and league_id not in found:
                    found[league_id] = {
                        "league_id": league_id,
                        "name": lg.get("name"),
                        "season": lg.get("season"),
                        "n_teams": lg.get("total_rosters"),
                        "discovered_via": "seed",
                        "hop": 0,
                    }
                    if verbose:
                        print(f"    + [seed] {lg.get('name')} "
                              f"({lg.get('total_rosters')} teams)")

        if hop >= budget.max_hops:
            # Out of depth for THIS run. Put it back so the next run starts
            # here — this is what carries reach across days.
            frontier.appendleft((league_id, hop))
            break

        users = sl.league_users(league_id)
        for u in users:
            uid = str(u.get("user_id") or "")
            if not uid or uid in seen_users:
                continue
            seen_users.add(uid)
            if _stop():
                break

            for lg in sl.user_leagues(uid, season):
                lid = str(lg.get("league_id") or "")
                if not lid or lid in seen_leagues:
                    continue
                seen_leagues.add(lid)
                _record(lg, type_counter, marker_rows)
                if not is_dynasty(lg):
                    continue
                if lg.get("sport") not in (None, "nfl"):
                    continue
                if lid not in known and lid not in found:
                    if len(found) >= max_discover:
                        break
                    found[lid] = {
                        "league_id": lid,
                        "name": lg.get("name"),
                        "season": lg.get("season"),
                        "n_teams": lg.get("total_rosters"),
                        # Pseudonymous Sleeper handle only, and only as crawl
                        # provenance so a league's presence is explainable.
                        "discovered_via": u.get("display_name") or uid,
                        "hop": hop + 1,
                    }
                    if verbose:
                        print(f"    + [hop {hop+1}] {lg.get('name')} "
                              f"({lg.get('total_rosters')} teams)")
                frontier.append((lid, hop + 1))

    return {
        "leagues": list(found.values()),
        "frontier": list(frontier),
        "seen_leagues": seen_leagues,
        "seen_users": seen_users,
        "n_leagues_examined": len(seen_leagues),
        "n_users_examined": len(seen_users),
        "n_new_this_run": len(found),
        "filter_evidence": _filter_evidence(type_counter, marker_rows),
    }


def _record(lg: Dict, counter: Counter, rows: List[Dict]) -> None:
    s = (lg or {}).get("settings") or {}
    counter[s.get("type")] += 1
    rows.append({
        "type": s.get("type"),
        "taxi_slots": s.get("taxi_slots"),
        "max_keepers": s.get("max_keepers"),
        "name": lg.get("name"),
    })


def _filter_evidence(counter: Counter, rows: List[Dict]) -> Dict:
    """Re-derive the dynasty-filter justification from this run's own data.

    The point is falsifiability: if ``settings.type == 2`` ever stops meaning
    dynasty, this block is where it shows up, in the committed artifact, on
    the run that saw it.
    """
    by_type: Dict[str, Dict] = {}
    for t in sorted({r["type"] for r in rows}, key=lambda x: (x is None, x)):
        group = [r for r in rows if r["type"] == t]
        taxi = [r["taxi_slots"] for r in group if isinstance(r["taxi_slots"], int)]
        keep = [r["max_keepers"] for r in group if isinstance(r["max_keepers"], int)]
        by_type[str(t)] = {
            "n_leagues": len(group),
            "with_taxi_squad": sum(1 for x in taxi if x > 0),
            "max_keepers_values": sorted(set(keep))[:8],
            "example_names": [r["name"] for r in group[:3]],
        }
    return {
        "field": "settings.type",
        "dynasty_value": DYNASTY_LEAGUE_TYPE,
        "method": (
            "empirical: type=2 co-occurs with taxi squads and max_keepers=1; "
            "type=1 co-occurs with a finite keeper count and no taxi squad. "
            "Confirmed against real league objects, not Sleeper's docs, which "
            "do not enumerate this field."
        ),
        "name_matching_rejected": (
            "a league named 'Deke Dynasty' is type=1 (keeper, 18 keepers), so "
            "matching on the league name would misclassify it"
        ),
        "observed_this_run": by_type,
    }


# --------------------------------------------------------------------------
# Scoring (delegated to the real page code via node)
# --------------------------------------------------------------------------

def score_leagues(
    leagues: List[Dict],
    budget: Budget,
    *,
    values_dir: Path,
    cache_dir: Optional[Path] = None,
    max_cache_bytes: Optional[int] = None,
    verbose: bool = True,
) -> Dict:
    """Score each discovered league with the shipped Manager Score JS.

    Nothing about the metric is computed here. ``scripts/js/score_league_harness.js``
    runs MANAGERSCORE_CORE_JS + MANAGERSCORE_UI_JS under node, so the corpus
    aggregates exactly the number the Manager Score page shows.

    ``cache_dir`` is the permanent store for completed seasons. Passing it is
    what makes a second run over an indexed league cost only its live season
    instead of its entire dynasty chain.
    """
    import shutil

    node = shutil.which("node")
    if not node:
        return {"ok": False, "error": "node not found — cannot score without "
                                      "the shipped scoring JS", "leagues": []}

    from dynasty.managerscore_js import MANAGERSCORE_CORE_JS, MANAGERSCORE_UI_JS

    tmp = Path(tempfile.mkdtemp(prefix="crossleague-"))
    page_js = tmp / "ms_page.js"
    page_js.write_text(MANAGERSCORE_CORE_JS + MANAGERSCORE_UI_JS, encoding="utf-8")

    request = {
        "leagues": leagues,
        "valuesPath": str(values_dir / "managerscore_values.json"),
        "historyDir": str(values_dir / "ktc_history"),
        "maxCalls": budget.remaining,
        "minDelayMs": int(budget.min_delay_s * 1000),
        "concurrency": 3,
        "includeHistory": True,
        "cacheDir": str(cache_dir) if cache_dir else None,
        "maxCacheBytes": max_cache_bytes,
        # Leave a little wall clock for merging and writing artifacts; a run
        # that scored leagues but never committed them has helped nobody.
        "maxSeconds": (max(0.0, budget.seconds_remaining - 20)
                       if budget.max_seconds else 0),
    }
    req_path = tmp / "request.json"
    req_path.write_text(json.dumps(request), encoding="utf-8")

    if verbose:
        print(f"  scoring {len(leagues)} league(s) via node harness "
              f"(budget left: {budget.remaining} calls, "
              f"{budget.seconds_remaining:.0f}s)")

    proc = subprocess.run(
        [node, str(HARNESS), str(page_js), str(req_path)],
        capture_output=True, text=True, timeout=7200,
    )
    if proc.stderr.strip() and verbose:
        for line in proc.stderr.strip().splitlines()[-10:]:
            print(f"    [node] {line}", file=sys.stderr)
    if proc.returncode != 0 and not proc.stdout.strip():
        return {"ok": False, "error": f"harness exited {proc.returncode}",
                "leagues": []}
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"harness output unparseable: {exc}",
                "leagues": []}

    hb = payload.get("budget") or {}
    budget.scoring_calls += int(hb.get("calls") or 0)
    budget.refused += int(hb.get("refused") or 0)
    budget.errors += int(hb.get("errors") or 0)
    budget.cache_hits += int(hb.get("cache_hits") or 0)
    return payload


def update_seed_registry(path: Path, state: Dict, season: int) -> int:
    """Fold every confirmed dynasty league into the hand-editable registry.

    The registry is the **durable floor on coverage**. Crawl state can be
    lost or reset (a corrupt file, a season rollover, a deliberate rebuild)
    and that is survivable precisely because every league ever confirmed
    dynasty is also written here, where nothing but an explicit edit removes
    it. A cold start then re-reaches all of them for one call each instead of
    re-walking the graph to find them again.

    Only leagues Sleeper itself confirmed as ``settings.type == 2`` are added
    — the same filter the crawl uses — so this file cannot accumulate
    leagues that would be discarded on read.

    Comment keys (``_comment`` and friends) and hand-written notes on
    existing entries are preserved: an operator's reason for adding a seed is
    not ours to overwrite.
    """
    path = Path(path)
    registry = json.loads(path.read_text(encoding="utf-8"))
    existing = {str(s.get("league_id")): s for s in registry.get("seeds") or []
                if s.get("league_id")}

    added = 0
    for lid, lg in sorted((state.get("known_dynasty") or {}).items()):
        if lid in existing:
            continue
        existing[lid] = {
            "league_id": lid,
            "name": lg.get("name"),
            "added_by": "crawl",
            "added_at": lg.get("first_seen_at") or _now().isoformat(
                timespec="seconds"),
            "note": (
                f"discovered at hop {lg.get('hop')} via "
                f"{lg.get('discovered_via')}; Sleeper settings.type == 2"
            ),
        }
        added += 1

    if added:
        registry["seeds"] = [existing[k] for k in sorted(existing)]
        registry["season"] = registry.get("season") or season
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(registry, indent=2, sort_keys=False) + "\n",
                       encoding="utf-8")
        tmp.replace(path)
    return added


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=Path, default=SEEDS_PATH)
    ap.add_argument("--season", type=int, default=None,
                    help="season to enumerate (default: from the registry)")
    ap.add_argument("--max-hops", type=int, default=2,
                    help="social-graph depth PER RUN from the current "
                         "frontier (default 2). Reach compounds across runs.")
    ap.add_argument("--max-leagues", type=int, default=12,
                    help="hard cap on leagues SCORED in this run")
    ap.add_argument("--max-discover", type=int, default=250,
                    help="hard cap on NEW dynasty leagues queued this run")
    ap.add_argument("--max-calls", type=int, default=400,
                    help="hard cap on total Sleeper API calls for the run")
    ap.add_argument("--max-discovery-calls", type=int, default=None,
                    help="sub-cap on discovery calls so a wide frontier "
                         "cannot consume the whole budget (default: 25%% "
                         "of --max-calls)")
    ap.add_argument("--max-seconds", type=float, default=0.0,
                    help="wall-clock cap for the run; 0 disables")
    ap.add_argument("--new-league-share", type=float, default=0.6,
                    help="share of the scoring budget reserved for leagues "
                         "never scored before (default 0.6)")
    ap.add_argument("--min-delay", type=float, default=0.12,
                    help="minimum seconds between call starts")
    ap.add_argument("--discover-only", action="store_true",
                    help="crawl and report, score nothing (cheap)")
    ap.add_argument("--write-corpus", action="store_true",
                    help="write data/cross_league/corpus.json")
    ap.add_argument("--corpus", type=Path, default=CORPUS_PATH)
    ap.add_argument("--state", type=Path, default=STATE_PATH,
                    help="durable crawl state (frontier + scored ledger)")
    ap.add_argument("--no-state", action="store_true",
                    help="ignore and do not write crawl state (cold run)")
    ap.add_argument("--cache-dir", type=Path, default=CACHE_DIR,
                    help="permanent cache for completed seasons")
    ap.add_argument("--no-cache", action="store_true",
                    help="disable the permanent cache (forces full re-reads)")
    ap.add_argument("--max-cache-mb", type=float, default=512.0,
                    help="stop ADDING to the permanent cache past this size; "
                         "existing entries are still served")
    ap.add_argument("--update-seeds", action="store_true",
                    help="fold every discovered dynasty league into the seed "
                         "registry so coverage cannot regress")
    ap.add_argument("--site-out", type=Path, default=None,
                    help="also publish the artifact into a site build dir")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    verbose = not args.quiet
    from dynasty import crossleague
    from dynasty import crawl_state as cs
    from dynasty import managerscore
    from dynasty import manager_detail

    registry = json.loads(Path(args.seeds).read_text(encoding="utf-8"))
    season = args.season or int(registry.get("season") or _now().year)
    seeds = [str(s["league_id"]) for s in registry.get("seeds") or []
             if s.get("league_id")]
    if not seeds:
        print("No seeds in registry — nothing to crawl.", file=sys.stderr)
        return 1

    max_disc_calls = (args.max_discovery_calls
                      if args.max_discovery_calls is not None
                      else max(30, int(args.max_calls * 0.25)))

    budget = Budget(max_calls=args.max_calls, max_leagues=args.max_leagues,
                    max_hops=args.max_hops, min_delay_s=args.min_delay,
                    max_discovery_calls=max_disc_calls,
                    max_seconds=args.max_seconds)
    sl = Sleeper(budget, verbose=verbose)

    # Durable crawl state: resume the walk instead of repeating it.
    state = (cs.empty_state(season) if args.no_state
             else cs.load_state(args.state, season))
    before = cs.state_summary(state)
    state = cs.merge_seed_frontier(state, seeds)

    if verbose:
        print(f"Cross-league crawl — season {season}, {len(seeds)} seed(s), "
              f"caps: {args.max_hops} hops/run / {args.max_leagues} scored / "
              f"{args.max_calls} calls ({max_disc_calls} discovery)")
        print(f"  resuming: {before['n_dynasty_leagues_discovered']} league(s) "
              f"known, {before['n_scored_ok']} scored ok, "
              f"{before['n_never_scored']} never scored, "
              f"frontier {len(state.get('frontier') or [])}")

    disc = discover(sl, season, budget, state=state,
                    max_discover=args.max_discover, verbose=verbose)
    state = cs.record_discovery(
        state,
        frontier=disc["frontier"],
        seen_leagues=disc["seen_leagues"],
        seen_users=disc["seen_users"],
        dynasty=disc["leagues"],
    )
    if verbose:
        print(f"  discovery: +{disc['n_new_this_run']} new dynasty league(s), "
              f"{disc['n_leagues_examined']} examined all-time, "
              f"{disc['n_users_examined']} users all-time, "
              f"{budget.discovery_calls} calls, "
              f"frontier now {len(disc['frontier'])}")

    if args.discover_only:
        print(json.dumps({
            "season": season,
            "discovery": {k: v for k, v in disc.items()
                          if k not in ("leagues", "seen_leagues",
                                       "seen_users", "frontier")},
            "leagues": disc["leagues"],
            "state": cs.state_summary(state),
            "budget": budget.to_dict(),
        }, indent=2))
        if not args.no_state:
            cs.save_state(args.state, state)
        return 0

    # The valuation artifact the page would fetch over HTTP. Built from the
    # committed KTC snapshot + crosswalk, so scoring is offline apart from
    # Sleeper itself.
    values_dir = Path(tempfile.mkdtemp(prefix="crossleague-values-"))
    values = managerscore.write_values_artifact(
        values_dir, consensus_dir=CONSENSUS_DIR)
    if not values.get("available") and verbose:
        print("  ! value artifact unavailable: "
              + "; ".join(values.get("notes") or []), file=sys.stderr)

    # Which leagues does this run pay for? New ones first, against a
    # reserved share of the budget; then the stalest indexed ones, which the
    # permanent cache has made cheap. See dynasty.crawl_state.plan_scoring.
    plan = cs.plan_scoring(
        state,
        max_leagues=args.max_leagues,
        new_league_share=args.new_league_share,
        scoring_budget=budget.remaining,
    )
    queue = plan["new"] + plan["refresh"]
    if verbose:
        print(f"  plan: {len(plan['new'])} new + {len(plan['refresh'])} "
              f"refresh (of {plan['n_never_scored_total']} never scored, "
              f"{plan['n_previously_scored_total']} indexed); "
              f"{plan['reserved_for_new']} calls reserved for new")

    cache_dir = None if args.no_cache else Path(args.cache_dir)
    scored = score_leagues(
        queue, budget,
        values_dir=values_dir,
        cache_dir=cache_dir,
        max_cache_bytes=int(args.max_cache_mb * 1024 * 1024),
        verbose=verbose,
    )
    if not scored.get("ok"):
        print(f"Scoring failed: {scored.get('error')}", file=sys.stderr)
        if not args.no_state:
            cs.save_state(args.state, state)
        return 1

    usable = [L for L in scored.get("leagues") or [] if L.get("ok")]
    unusable = [L for L in scored.get("leagues") or [] if not L.get("ok")]
    state = cs.record_scoring(state, scored.get("leagues") or [])
    if verbose:
        print(f"  scored: {len(usable)} usable, {len(unusable)} discarded")
        for L in unusable:
            print(f"    - {L.get('name') or L.get('league_id')}: {L.get('reason')}")
        cstats = scored.get("cache") or {}
        if cstats.get("enabled"):
            print(f"  cache: {cstats.get('hits', 0)} hit(s) served free, "
                  f"{cstats.get('stored', 0)} newly stored, "
                  f"{cstats.get('bundles_written', 0)} bundle(s) written, "
                  f"{(cstats.get('bytes_on_disk') or 0) / 1e6:.1f} MB on disk")

    previous = crossleague.load_corpus(args.corpus)
    merged = crossleague.merge_corpus(previous, usable)

    corpus = crossleague.build_corpus(
        merged,
        budget=budget.to_dict(),
        discovery={
            "season": season,
            "n_seeds": len(seeds),
            "n_leagues_examined": disc["n_leagues_examined"],
            "n_users_examined": disc["n_users_examined"],
            "n_new_dynasty_found_this_run": disc["n_new_this_run"],
            "n_discarded_unscorable": len(unusable),
            "discarded": [{"league_id": L.get("league_id"),
                           "name": L.get("name"),
                           "reason": L.get("reason")} for L in unusable],
            "method": (
                "bounded breadth-first walk of the Sleeper social graph from "
                "the seed registry; Sleeper has no endpoint that enumerates "
                "leagues, so this is the only discovery path that exists"
            ),
            "sampling_caveat": (
                "a social-graph crawl reaches leagues socially near its "
                "seeds. This is not a random sample of Sleeper and cannot "
                "be treated as one."
            ),
        },
        filter_meta=disc["filter_evidence"],
        previous=previous,
    )
    # Publish crawl progress alongside coverage. "5 indexed, 0 queued" and
    # "5 indexed, 61 queued" describe very different systems, and only one
    # of them is still growing.
    corpus["crawl"] = cs.state_summary(state)
    corpus["cache"] = scored.get("cache") or {"enabled": False}
    # How a visitor gets a league in here. The page reads `repo` to build the
    # issue link, so a fork points at its own issue tracker rather than
    # sending its users to this one.
    corpus["submission"] = {
        "repo": os.environ.get("GITHUB_REPOSITORY")
        or "pstiehl/Dynasty-Football-Model",
        "template": "league-submission.yml",
        "label": "league-submission",
        "how": (
            "the site is static, so a browser cannot write to the corpus; a "
            "pre-filled GitHub issue is the only inbox it can offer. A daily "
            "job validates the submitted id against Sleeper and appends it to "
            "the seed registry."
        ),
    }
    # Retain the per-league results so the next run can carry leagues that
    # fall outside its budget. This is what makes the corpus grow rather than
    # churn -- and it only works if the artifact is actually committed.
    #
    # Compacted first: the full msScoreLeague audit is ~2.3 MB for five
    # leagues, and this file is rewritten on every daily run. Aggregation only
    # needs each manager's per-component n and z, so that is all we keep. The
    # audit trail stays reproducible on the per-league page, which rebuilds it
    # live from Sleeper.
    #
    # The full audit is still in hand HERE and nowhere after this loop, so
    # this is where the per-manager drill-down evidence is captured. It goes
    # to a gitignored, CI-cached store (data/cross_league/detail/), never
    # into the committed corpus -- see dynasty.manager_detail for why that
    # is not a re-inlining of the audit PR #72 deliberately removed.
    #
    # Only leagues scored THIS run carry an audit; entries retained from an
    # earlier run are already compacted, and league_detail_from_result()
    # returns None for those. Their previously written detail file is left
    # in place rather than overwritten with an empty one.
    detail_dir = manager_detail.detail_dir(args.corpus.parent)
    n_detail_written = 0
    retained = []
    for L in merged:
        if not L.get("result"):
            continue
        detail = manager_detail.league_detail_from_result(L)
        if detail is not None and manager_detail.write_league_detail(
                detail_dir, detail):
            n_detail_written += 1
        entry = dict(L)
        entry["retained_from"] = L.get("retained_from") or corpus["generated_at"]
        retained.append(crossleague.compact_result(entry))
    corpus["retained"] = retained

    # Lineage dedup retires superseded league ids, so without a prune the
    # detail store would be append-only and grow without bound.
    n_detail_pruned = manager_detail.prune_league_details(
        detail_dir, [L.get("league_id") for L in merged if L.get("league_id")]
    )
    if n_detail_written or n_detail_pruned:
        print(f"  drill-down detail: {n_detail_written} league(s) written, "
              f"{n_detail_pruned} pruned -> {detail_dir}")
    corpus["values"] = scored.get("values") or {}

    cov = corpus["coverage"]
    print()
    print(f"  {crossleague.coverage_sentence(corpus)}")
    print(f"  league-seasons scored: {cov['n_league_seasons_scored']}  |  "
          f"runs: {corpus['n_runs']}  |  persisted: {corpus['persisted']}")
    print(f"  budget: {json.dumps(budget.to_dict())}")

    if corpus["draft_board"]:
        print()
        print("  Top drafters across the indexed corpus:")
        for row in corpus["draft_board"][:10]:
            print(f"    {row['rank']:>2}. {row['display_name']:<18} "
                  f"draft z={row['draft_z']:+.3f}  "
                  f"{row['n_picks']:>3} picks  {row['n_leagues']} league(s)")

    if args.write_corpus:
        args.corpus.parent.mkdir(parents=True, exist_ok=True)
        # Compact, not pretty-printed. This file is rewritten and committed
        # on every daily run, so every byte is paid again in git history
        # forever. At 1,223 managers `indent=1` cost 856 KB -- 29% of the
        # artifact -- to make a 3 MB machine-generated file marginally more
        # readable, which nobody reads anyway. Use `python -m json.tool` if
        # you need to inspect it.
        args.corpus.write_text(
            json.dumps(corpus, separators=(",", ":"), sort_keys=False),
            encoding="utf-8")
        print(f"\n  wrote {args.corpus}")

    if not args.no_state:
        cs.save_state(args.state, state)
        after = cs.state_summary(state)
        print(f"  crawl state: {after['n_dynasty_leagues_discovered']} known "
              f"(+{after['n_dynasty_leagues_discovered'] - before['n_dynasty_leagues_discovered']}), "
              f"{after['n_scored_ok']} scored ok, "
              f"{after['n_never_scored']} queued, "
              f"frontier {after['frontier_depth']} → {args.state}")

    if args.update_seeds:
        added = update_seed_registry(args.seeds, state, season)
        print(f"  seed registry: +{added} league(s) → {args.seeds}")

    if args.site_out:
        # Publish the per-manager shards BEFORE the corpus artifact: the
        # corpus carries the resulting stats block, so the page can state how
        # much evidence exists without probing 1,400 URLs to find out.
        details = manager_detail.read_league_details(detail_dir)
        stats = manager_detail.publish_manager_details(
            args.site_out, corpus, details)
        corpus["manager_detail"] = stats
        kib = stats["bytes"] / 1024.0
        print(f"  published {stats['n_files']} manager detail file(s), "
              f"{kib:.0f} KiB, from {stats['n_leagues_with_detail']} league "
              f"audit(s) ({stats['n_managers_complete']} complete, "
              f"{stats['n_managers_partial']} partial, "
              f"{stats['n_managers_without_evidence']} without evidence)")
        path = crossleague.write_corpus_artifact(args.site_out, corpus)
        print(f"  published {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
