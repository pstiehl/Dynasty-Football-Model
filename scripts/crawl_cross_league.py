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

Usage
-----
    # cheap: discovery only, no scoring, prints what it would index
    python scripts/crawl_cross_league.py --discover-only

    # full run with conservative caps
    python scripts/crawl_cross_league.py --max-leagues 12 --max-calls 400

    # what the daily job runs (caps come from the registry/defaults)
    python scripts/crawl_cross_league.py --write-corpus
"""
from __future__ import annotations

import argparse
import json
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
    """Explicit, configurable caps. Nothing here is unbounded."""

    def __init__(self, *, max_calls: int, max_leagues: int, max_hops: int,
                 min_delay_s: float):
        self.max_calls = max_calls
        self.max_leagues = max_leagues
        self.max_hops = max_hops
        self.min_delay_s = min_delay_s
        self.discovery_calls = 0
        self.scoring_calls = 0
        self.refused = 0
        self.errors = 0

    @property
    def calls(self) -> int:
        return self.discovery_calls + self.scoring_calls

    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - self.calls)

    def to_dict(self) -> Dict:
        return {
            "max_calls": self.max_calls,
            "max_leagues": self.max_leagues,
            "max_hops": self.max_hops,
            "min_delay_seconds": self.min_delay_s,
            "calls_used_total": self.calls,
            "calls_used_discovery": self.discovery_calls,
            "calls_used_scoring": self.scoring_calls,
            "calls_refused_over_budget": self.refused,
            "network_errors": self.errors,
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
        """
        if url in self._cache:
            return self._cache[url]
        if self.b.remaining <= 0:
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
    seeds: List[str],
    season: int,
    budget: Budget,
    *,
    verbose: bool = True,
) -> Dict:
    """Breadth-first walk of the Sleeper social graph, depth- and count-capped.

    Returns discovered dynasty leagues plus the evidence that re-validates
    the dynasty filter on this run's real data.

    Discovery deliberately enumerates a **single season**. That is what makes
    de-duplication of dynasty chains sound: a chain holds exactly one league
    per season, so two distinct same-season league ids cannot belong to the
    same lineage. Prior seasons are then picked up by the scorer, which walks
    ``previous_league_id`` itself — so history is scored without ever being
    discovered as separate leagues.
    """
    found: Dict[str, Dict] = {}
    seen_leagues: Set[str] = set()
    seen_users: Set[str] = set()
    type_counter: Counter = Counter()
    marker_rows: List[Dict] = []

    frontier: deque = deque()

    for s in seeds:
        lg = sl.league(s)
        if not lg or not lg.get("league_id"):
            if verbose:
                print(f"  seed {s}: not found on Sleeper", file=sys.stderr)
            continue
        seen_leagues.add(str(lg["league_id"]))
        _record(lg, type_counter, marker_rows)
        if is_dynasty(lg):
            found[str(lg["league_id"])] = {
                "league_id": str(lg["league_id"]),
                "name": lg.get("name"),
                "season": lg.get("season"),
                "n_teams": lg.get("total_rosters"),
                "discovered_via": "seed",
                "hop": 0,
            }
        frontier.append((str(lg["league_id"]), 0))

    while frontier:
        league_id, hop = frontier.popleft()
        if hop >= budget.max_hops:
            continue
        if len(found) >= budget.max_leagues or budget.remaining <= 0:
            break

        users = sl.league_users(league_id)
        for u in users:
            uid = str(u.get("user_id") or "")
            if not uid or uid in seen_users:
                continue
            seen_users.add(uid)
            if len(found) >= budget.max_leagues or budget.remaining <= 0:
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
                if len(found) >= budget.max_leagues:
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
                frontier.append((lid, hop + 1))
                if verbose:
                    print(f"    + [hop {hop+1}] {lg.get('name')} "
                          f"({lg.get('total_rosters')} teams)")

    return {
        "leagues": list(found.values()),
        "n_leagues_examined": len(seen_leagues),
        "n_users_examined": len(seen_users),
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
    verbose: bool = True,
) -> Dict:
    """Score each discovered league with the shipped Manager Score JS.

    Nothing about the metric is computed here. ``scripts/js/score_league_harness.js``
    runs MANAGERSCORE_CORE_JS + MANAGERSCORE_UI_JS under node, so the corpus
    aggregates exactly the number the Manager Score page shows.
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
    }
    req_path = tmp / "request.json"
    req_path.write_text(json.dumps(request), encoding="utf-8")

    if verbose:
        print(f"  scoring {len(leagues)} league(s) via node harness "
              f"(budget left: {budget.remaining} calls)")

    proc = subprocess.run(
        [node, str(HARNESS), str(page_js), str(req_path)],
        capture_output=True, text=True, timeout=1800,
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
    return payload


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=Path, default=SEEDS_PATH)
    ap.add_argument("--season", type=int, default=None,
                    help="season to enumerate (default: from the registry)")
    ap.add_argument("--max-hops", type=int, default=2,
                    help="social-graph depth from the seeds (default 2)")
    ap.add_argument("--max-leagues", type=int, default=12,
                    help="hard cap on leagues added to the corpus")
    ap.add_argument("--max-calls", type=int, default=400,
                    help="hard cap on total Sleeper API calls for the run")
    ap.add_argument("--min-delay", type=float, default=0.12,
                    help="minimum seconds between call starts")
    ap.add_argument("--discover-only", action="store_true",
                    help="crawl and report, score nothing (cheap)")
    ap.add_argument("--write-corpus", action="store_true",
                    help="write data/cross_league/corpus.json")
    ap.add_argument("--corpus", type=Path, default=CORPUS_PATH)
    ap.add_argument("--site-out", type=Path, default=None,
                    help="also publish the artifact into a site build dir")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    verbose = not args.quiet
    from dynasty import crossleague
    from dynasty import managerscore

    registry = json.loads(Path(args.seeds).read_text(encoding="utf-8"))
    season = args.season or int(registry.get("season") or _now().year)
    seeds = [str(s["league_id"]) for s in registry.get("seeds") or []
             if s.get("league_id")]
    if not seeds:
        print("No seeds in registry — nothing to crawl.", file=sys.stderr)
        return 1

    budget = Budget(max_calls=args.max_calls, max_leagues=args.max_leagues,
                    max_hops=args.max_hops, min_delay_s=args.min_delay)
    sl = Sleeper(budget, verbose=verbose)

    if verbose:
        print(f"Cross-league crawl — season {season}, {len(seeds)} seed(s), "
              f"caps: {args.max_hops} hops / {args.max_leagues} leagues / "
              f"{args.max_calls} calls")

    disc = discover(sl, seeds, season, budget, verbose=verbose)
    if verbose:
        print(f"  discovery: {len(disc['leagues'])} dynasty league(s) from "
              f"{disc['n_leagues_examined']} examined, "
              f"{disc['n_users_examined']} users, "
              f"{budget.discovery_calls} calls")

    if args.discover_only:
        print(json.dumps({
            "season": season,
            "discovery": {k: v for k, v in disc.items() if k != "leagues"},
            "leagues": disc["leagues"],
            "budget": budget.to_dict(),
        }, indent=2))
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

    scored = score_leagues(disc["leagues"], budget,
                           values_dir=values_dir, verbose=verbose)
    if not scored.get("ok"):
        print(f"Scoring failed: {scored.get('error')}", file=sys.stderr)
        return 1

    usable = [L for L in scored.get("leagues") or [] if L.get("ok")]
    unusable = [L for L in scored.get("leagues") or [] if not L.get("ok")]
    if verbose:
        print(f"  scored: {len(usable)} usable, {len(unusable)} discarded")
        for L in unusable:
            print(f"    - {L.get('name') or L.get('league_id')}: {L.get('reason')}")

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
            "n_discarded_unscorable": len(unusable),
            "discarded": [{"league_id": L.get("league_id"),
                           "name": L.get("name"),
                           "reason": L.get("reason")} for L in unusable],
            "method": (
                "bounded breadth-first walk of the Sleeper social graph from "
                "the seed registry; Sleeper has no endpoint that enumerates "
                "leagues, so this is the only discovery path that exists"
            ),
        },
        filter_meta=disc["filter_evidence"],
        previous=previous,
    )
    # Retain the per-league results so the next run can carry leagues that
    # fall outside its budget. This is what makes the corpus grow rather than
    # churn -- and it only works if the artifact is actually committed.
    #
    # Compacted first: the full msScoreLeague audit is ~2.3 MB for five
    # leagues, and this file is rewritten on every daily run. Aggregation only
    # needs each manager's per-component n and z, so that is all we keep. The
    # audit trail stays reproducible on the per-league page, which rebuilds it
    # live from Sleeper.
    retained = []
    for L in merged:
        if not L.get("result"):
            continue
        entry = dict(L)
        entry["retained_from"] = L.get("retained_from") or corpus["generated_at"]
        retained.append(crossleague.compact_result(entry))
    corpus["retained"] = retained
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
        args.corpus.write_text(json.dumps(corpus, indent=1, sort_keys=False),
                               encoding="utf-8")
        print(f"\n  wrote {args.corpus}")

    if args.site_out:
        path = crossleague.write_corpus_artifact(args.site_out, corpus)
        print(f"  published {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
