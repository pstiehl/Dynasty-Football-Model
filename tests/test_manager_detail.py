"""Per-manager drill-down artifacts: do they explain the published score?

The test that matters here is :class:`TestReconciliation`. A drill-down that
renders beautifully and tells a *different* story than the leaderboard is
worse than no drill-down at all, because it looks like an explanation. So
the suite re-derives the whole chain from the individual events back up to
the number on the board:

    Σ surplus  ->  total  ->  mean  ->  shrunk  ->  z        (within league)
    Σ n·z / Σn ->  z̄      ->  Z                              (cross league)
    Σ weight·Z ->  composite  ->  100 + 15·composite

and requires every step to agree with what ``crossleague.aggregate``
published.

Crucially the fixture is scored by the **real** ``msScoreLeague``, run under
node (``tests/support/score_fixture_league.js``). Reconciling against a
Python re-implementation of the scorer would agree with itself by
construction and prove nothing. The league is synthetic only because the
corpus is built by a bounded crawl of a rate-limited third-party API and no
test may require network access.

``TestReconciliationDetectsDrift`` is the control: it perturbs one pick and
requires the reconciliation to fail. Without it, a reconciliation that
silently checked nothing would still pass.

Runs under pytest or ``python scripts/run_tests_stdlib.py``; node-dependent
cases skip cleanly when node is absent.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dynasty import crossleague  # noqa: E402
from dynasty import manager_detail as md  # noqa: E402

NODE = shutil.which("node")
FIXTURE_JS = REPO_ROOT / "tests" / "support" / "score_fixture_league.js"
ORDER_TESTS = REPO_ROOT / "tests" / "js" / "crossleague_script_order_tests.mjs"
RENDER_XL = REPO_ROOT / "tests" / "support" / "render_crossleague.py"

_CACHE: dict = {}


def scored_fixture(seed: int = 7) -> dict:
    """Score the fixture league with the shipped scorer. Cached per seed."""
    key = f"fx{seed}"
    if key in _CACHE:
        return json.loads(json.dumps(_CACHE[key]))
    from dynasty.managerscore_js import MANAGERSCORE_CORE_JS

    tmp = Path(tempfile.mkdtemp(prefix="mdfix-"))
    core = tmp / "core.js"
    core.write_text(MANAGERSCORE_CORE_JS, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(FIXTURE_JS), str(core), str(seed)],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        raise AssertionError(f"fixture scorer failed: {proc.stderr[-2000:]}")
    entry = json.loads(proc.stdout)
    _CACHE[key] = entry
    return json.loads(json.dumps(entry))


def corpus_and_details(entry: dict):
    """The corpus the board publishes, plus the audits behind it."""
    detail = md.league_detail_from_result(entry)
    corpus = crossleague.build_corpus([crossleague.compact_result(entry)])
    return corpus, {detail["league_id"]: detail}


# ---------------------------------------------------------------------------
# Paths, sharding and filename safety
# ---------------------------------------------------------------------------

class TestLayout(unittest.TestCase):

    def test_shard_is_two_hex_chars(self):
        for mid in ["1", "308629444837269504", "roster:123:4", ""]:
            s = md.shard_of(mid)
            self.assertEqual(len(s), 2, mid)
            int(s, 16)  # must parse as hex

    def test_synthetic_roster_ids_become_safe_filenames(self):
        """66 of the live corpus's 1,407 ids are ``roster:<league>:<slot>``."""
        name = md.safe_name("roster:900100200300400500:13")
        self.assertNotIn(":", name)
        self.assertRegex(name, r"^[A-Za-z0-9._~-]+$")

    def test_encoding_is_injective(self):
        """The property that stops one manager's evidence being served
        under another's name. ``~`` is encoded first, so no encoded output
        can collide with a literal input."""
        ids = [
            "roster:1:2", "roster_1_2", "roster~3A1~3A2", "roster:1:2 ",
            "123", "12:3", "1~23", "~", "~7E",
        ]
        names = [md.safe_name(i) for i in ids]
        self.assertEqual(len(set(names)), len(ids), dict(zip(ids, names)))

    def test_shard_distribution_is_not_degenerate(self):
        """Snowflake ids are time-ordered, so a prefix shard clusters badly.

        Measured on the live corpus: first-2-chars puts 81 managers in one
        bucket, FNV puts at most 13. This pins the property, not the number.
        """
        ids = [str(10 ** 18 + i * 7919) for i in range(1500)]
        from collections import Counter
        fnv = Counter(md.shard_of(i) for i in ids)
        prefix = Counter(i[:2] for i in ids)
        self.assertGreater(len(fnv), 200, "FNV should use most buckets")
        self.assertLess(max(fnv.values()), max(prefix.values()),
                        "FNV must spread better than an id prefix")

    def test_rel_path_shape(self):
        p = md.manager_rel_path("308629444837269504")
        self.assertTrue(p.startswith("managers/"))
        self.assertTrue(p.endswith(".json"))
        self.assertEqual(len(p.split("/")), 3)


# ---------------------------------------------------------------------------
# Fail-closed behaviour
# ---------------------------------------------------------------------------

class TestFailsClosed(unittest.TestCase):

    def test_compacted_entry_yields_no_detail(self):
        """A retained (already compacted) league has no audit.

        Building a detail record from it would publish "evidence: complete"
        over an empty pick list -- a manager's real draft record rendered as
        no activity. That is the silent-null class of bug that once made
        every manager score exactly 100.0, so it must fail closed.
        """
        if not NODE:
            self.skipTest("node not available")
        entry = scored_fixture()
        compacted = crossleague.compact_result(entry)
        self.assertIsNone(md.league_detail_from_result(compacted))

    def test_empty_entry_is_tolerated(self):
        self.assertIsNone(md.league_detail_from_result({}))
        self.assertIsNone(md.league_detail_from_result(
            {"result": {"managers": []}}))

    def test_missing_league_audit_is_reported_not_zeroed(self):
        """A league with no retained audit must say so, not render empty."""
        if not NODE:
            self.skipTest("node not available")
        entry = scored_fixture()
        corpus, _ = corpus_and_details(entry)
        row = corpus["leaderboard"][0]
        doc = md.build_manager_detail(row, {}, corpus)   # no details at all
        self.assertEqual(doc["coverage"]["n_leagues_with_evidence"], 0)
        self.assertFalse(doc["coverage"]["complete"])
        for lg in doc["leagues"]:
            self.assertEqual(lg["evidence"], "missing")
            self.assertIn("still counts", lg["evidence_why"])
            self.assertNotIn("detail", lg)
        # The headline score is untouched by the absence of evidence.
        self.assertEqual(doc["cross_index"], row["cross_index"])

    def test_corrupt_detail_file_is_skipped_not_fatal(self):
        tmp = Path(tempfile.mkdtemp(prefix="mdbad-"))
        (tmp / "bad.json.gz").write_bytes(b"not gzip at all")
        self.assertEqual(md.read_league_details(tmp), {})

    def test_collision_would_fail_the_build(self):
        """Two ids mapping to one path must raise, never silently overwrite."""
        tmp = Path(tempfile.mkdtemp(prefix="mdcol-"))
        corpus = {"leaderboard": [
            {"manager_id": "dup", "display_name": "A", "leagues": [],
             "components": {}},
            {"manager_id": "dup", "display_name": "B", "leagues": [],
             "components": {}},
        ]}
        # Same id twice is legal (an idempotent rewrite of one file), so a
        # real collision has to be forced: two DIFFERENT ids landing on one
        # path. The guard is on the full relative path -- shard included --
        # so patching safe_name alone is not enough, because the two ids
        # still hash to different shard directories.
        original = md.manager_rel_path
        try:
            md.manager_rel_path = lambda _i: "managers/aa/same.json"  # type: ignore[assignment]
            corpus["leaderboard"][1]["manager_id"] = "other"
            with self.assertRaises(ValueError):
                md.publish_manager_details(tmp, corpus, {})
        finally:
            md.manager_rel_path = original             # type: ignore[assignment]


# ---------------------------------------------------------------------------
# The reconciliation
# ---------------------------------------------------------------------------

class TestReconciliation(unittest.TestCase):
    """Do the numbers on the drill-down add up to the number on the board?"""

    def setUp(self):
        if not NODE:
            self.skipTest("node not available")
        self.entry = scored_fixture()
        self.corpus, self.details = corpus_and_details(self.entry)

    def test_fixture_is_substantial_enough_to_mean_something(self):
        meta = self.entry["result"]["meta"]
        self.assertGreaterEqual(meta["nPicksScored"], 40)
        self.assertGreaterEqual(meta["nTradesScored"], 5)
        self.assertGreaterEqual(meta["nWaivers"], 20)
        self.assertGreaterEqual(len(self.corpus["leaderboard"]), 10)

    def test_every_manager_reconciles_to_the_leaderboard(self):
        problems = []
        for row in self.corpus["leaderboard"]:
            doc = md.build_manager_detail(row, self.details, self.corpus)
            problems.extend(md.reconcile_manager(doc))
        self.assertEqual(problems, [], "\n".join(problems[:20]))

    def test_reconciles_across_several_seeds(self):
        """One fixture could reconcile by coincidence; five will not."""
        for seed in (1, 3, 11, 23):
            entry = scored_fixture(seed)
            corpus, details = corpus_and_details(entry)
            for row in corpus["leaderboard"]:
                doc = md.build_manager_detail(row, details, corpus)
                problems = md.reconcile_manager(doc)
                self.assertEqual(problems, [],
                                 f"seed {seed}: " + "\n".join(problems[:10]))

    def test_pick_surpluses_sum_to_the_draft_total(self):
        """The claim a reader checks by adding up the column themselves."""
        checked = 0
        for row in self.corpus["leaderboard"]:
            doc = md.build_manager_detail(row, self.details, self.corpus)
            for lg in doc["leagues"]:
                if lg.get("evidence") != "complete":
                    continue
                comp = lg["detail"]["components"]["draft"]
                if not comp["n"]:
                    continue
                total = sum(p["surplus"] for p in lg["detail"]["picks"])
                self.assertAlmostEqual(total, comp["total"], places=2)
                self.assertEqual(len(lg["detail"]["picks"]), comp["n"])
                checked += 1
        self.assertGreater(checked, 0, "no draft evidence was checked")

    def test_every_pick_carries_a_labelled_valuation_basis(self):
        """PR #63's point-in-time vs current distinction, per asset."""
        seen = set()
        for row in self.corpus["leaderboard"]:
            doc = md.build_manager_detail(row, self.details, self.corpus)
            for lg in doc["leagues"]:
                if lg.get("evidence") != "complete":
                    continue
                for p in lg["detail"]["picks"]:
                    self.assertIn(p["basis"], ("point-in-time", "current"))
                    self.assertIsNotNone(p["player"])
                    self.assertIsNotNone(p["surplus"])
                    seen.add(p["basis"])
        self.assertEqual(seen, {"point-in-time", "current"},
                         "fixture should exercise both pricing bases")

    def test_unscored_trades_are_shown_but_excluded_from_the_total(self):
        """A trade that could not be priced must not vanish, and must not
        be counted as zero either."""
        found_unscored = False
        for row in self.corpus["leaderboard"]:
            doc = md.build_manager_detail(row, self.details, self.corpus)
            for lg in doc["leagues"]:
                if lg.get("evidence") != "complete":
                    continue
                det = lg["detail"]
                comp = det["components"]["trade"]
                self.assertEqual(len(det["trades"]), comp["n"])
                if det.get("trades_unscored"):
                    found_unscored = True
                    for t in det["trades_unscored"]:
                        self.assertFalse(t["scored"])
        self.assertTrue(found_unscored,
                        "fixture should contain an unscorable trade")

    def test_pool_stats_reproduce_the_scorers_z(self):
        """z = (shrunk - poolμ)/poolσ, using the pool recovered from the
        full result rather than exported from the JS."""
        checked = 0
        detail = self.details[next(iter(self.details))]
        for m in detail["managers"]:
            for c in ("draft", "trade", "waiver"):
                comp = m["components"][c]
                pool = comp["pool"]
                if not comp["n"] or pool.get("n_managers", 0) < 2:
                    continue
                if not pool.get("sd"):
                    continue
                z = (comp["shrunk"] - pool["mean"]) / pool["sd"]
                self.assertAlmostEqual(z, comp["z"], places=3)
                checked += 1
        self.assertGreater(checked, 5)


class TestReconciliationDetectsDrift(unittest.TestCase):
    """The control. A reconciliation that cannot fail proves nothing."""

    def setUp(self):
        if not NODE:
            self.skipTest("node not available")
        self.entry = scored_fixture()
        self.corpus, self.details = corpus_and_details(self.entry)

    def _first_doc_with_picks(self, details):
        for row in self.corpus["leaderboard"]:
            doc = md.build_manager_detail(row, details, self.corpus)
            for lg in doc["leagues"]:
                if lg.get("evidence") == "complete" and lg["detail"]["picks"]:
                    return doc
        raise AssertionError("no manager with pick evidence in the fixture")

    def test_baseline_is_clean(self):
        doc = self._first_doc_with_picks(self.details)
        self.assertEqual(md.reconcile_manager(doc), [])

    def test_a_tampered_pick_surplus_is_caught(self):
        details = json.loads(json.dumps(self.details))
        lid = next(iter(details))
        for m in details[lid]["managers"]:
            if m["picks"]:
                m["picks"][0]["surplus"] = (m["picks"][0]["surplus"] or 0) + 500.0
                break
        doc = self._first_doc_with_picks(details)
        self.assertNotEqual(md.reconcile_manager(doc), [],
                            "a falsified pick surplus must be detected")

    def test_a_dropped_pick_is_caught(self):
        details = json.loads(json.dumps(self.details))
        lid = next(iter(details))
        for m in details[lid]["managers"]:
            if len(m["picks"]) > 1:
                m["picks"].pop()
                break
        doc = self._first_doc_with_picks(details)
        problems = md.reconcile_manager(doc)
        self.assertNotEqual(problems, [], "a missing pick must be detected")

    def test_a_tampered_z_is_caught(self):
        details = json.loads(json.dumps(self.details))
        lid = next(iter(details))
        for m in details[lid]["managers"]:
            if m["picks"]:
                m["components"]["draft"]["z"] = 9.99
                break
        doc = self._first_doc_with_picks(details)
        self.assertNotEqual(md.reconcile_manager(doc), [])


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

class TestPublishing(unittest.TestCase):

    def setUp(self):
        if not NODE:
            self.skipTest("node not available")
        self.entry = scored_fixture()
        self.corpus, self.details = corpus_and_details(self.entry)
        self.out = Path(tempfile.mkdtemp(prefix="mdsite-"))

    def test_one_file_per_manager_at_the_expected_path(self):
        stats = md.publish_manager_details(self.out, self.corpus, self.details)
        self.assertEqual(stats["n_files"], len(self.corpus["leaderboard"]))
        for row in self.corpus["leaderboard"]:
            p = self.out / md.manager_rel_path(row["manager_id"])
            self.assertTrue(p.is_file(), p)
            doc = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(doc["schema"], md.MANAGER_DETAIL_SCHEMA)
            self.assertEqual(str(doc["manager_id"]), str(row["manager_id"]))

    def test_published_files_stay_individually_small(self):
        """They are fetched one at a time, on click, so per-file size is
        the number that matters -- not the total."""
        md.publish_manager_details(self.out, self.corpus, self.details)
        sizes = [p.stat().st_size
                 for p in self.out.rglob("managers/**/*.json")]
        self.assertTrue(sizes)
        self.assertLess(max(sizes), 512 * 1024, "a shard got unreasonably big")

    def test_index_does_not_carry_the_audit(self):
        """The corpus artifact must stay small: that regression was fixed
        on purpose in PR #72 and must not come back through this feature."""
        stats = md.publish_manager_details(self.out, self.corpus, self.details)
        self.corpus["manager_detail"] = stats
        crossleague.write_corpus_artifact(self.out, self.corpus)
        published = json.loads(
            (self.out / crossleague.CORPUS_ARTIFACT).read_text(encoding="utf-8"))
        blob = json.dumps(published)
        self.assertNotIn("\"picks\"", blob)
        self.assertNotIn("\"waivers\"", blob)
        self.assertNotIn("value_at_date", blob)
        self.assertIn("manager_detail", published)

    def test_round_trip_through_the_durable_store(self):
        dd = Path(tempfile.mkdtemp(prefix="mddur-"))
        detail = md.league_detail_from_result(self.entry)
        self.assertIsNotNone(md.write_league_detail(dd, detail))
        back = md.read_league_details(dd)
        self.assertEqual(list(back), [detail["league_id"]])
        self.assertEqual(len(back[detail["league_id"]]["managers"]),
                         len(detail["managers"]))

    def test_prune_drops_leagues_no_longer_in_the_corpus(self):
        dd = Path(tempfile.mkdtemp(prefix="mdprune-"))
        detail = md.league_detail_from_result(self.entry)
        md.write_league_detail(dd, detail)
        md.write_league_detail(dd, dict(detail, league_id="999retired"))
        self.assertEqual(len(md.read_league_details(dd)), 2)
        removed = md.prune_league_details(dd, [detail["league_id"]])
        self.assertEqual(removed, 1)
        self.assertEqual(list(md.read_league_details(dd)),
                         [detail["league_id"]])

    def test_privacy_no_identity_fields_are_published(self):
        """Pseudonymous handle and opaque id only -- a decision already made
        for the board, which this feature must not quietly widen."""
        md.publish_manager_details(self.out, self.corpus, self.details)
        banned = ("email", "avatar", "real_name", "realName", "phone",
                  "user_name", "username")
        for p in self.out.rglob("managers/**/*.json"):
            blob = p.read_text(encoding="utf-8")
            for key in banned:
                self.assertNotIn(f'"{key}"', blob, f"{key} in {p.name}")


# ---------------------------------------------------------------------------
# The browser must agree with Python about where the file is
# ---------------------------------------------------------------------------

class TestShardAgreesWithTheBrowser(unittest.TestCase):

    def test_js_and_python_compute_the_same_shard_path(self):
        """The browser builds the fetch URL itself. A disagreement here is a
        404 on a file that exists, for whichever ids happen to diverge."""
        if not NODE:
            self.skipTest("node not available")
        from dynasty.crossleague_js import CROSSLEAGUE_JS

        ids = [
            "308629444837269504", "1", "12345678901234567890",
            "roster:900100200300400500:13", "roster:1:2",
            "a-b.c_d", "~tilde~", "Ünïcødé",
        ]
        tmp = Path(tempfile.mkdtemp(prefix="mdshard-"))
        js = tmp / "xl.js"
        js.write_text(CROSSLEAGUE_JS, encoding="utf-8")
        driver = tmp / "drive.mjs"
        driver.write_text(
            "import fs from 'node:fs';\n"
            "import vm from 'node:vm';\n"
            f"const src = fs.readFileSync({json.dumps(str(js))}, 'utf8');\n"
            "const ctx = vm.createContext({console, Math, JSON});\n"
            "vm.runInContext(src, ctx);\n"
            f"const ids = {json.dumps(ids)};\n"
            "const out = {};\n"
            "for (const i of ids) {\n"
            "  ctx.__i = i;\n"
            "  out[i] = vm.runInContext('xlDetailUrl(__i)', ctx);\n"
            "}\n"
            "process.stdout.write(JSON.stringify(out));\n",
            encoding="utf-8")
        proc = subprocess.run([NODE, str(driver)], capture_output=True,
                              text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        from_js = json.loads(proc.stdout)
        for mid in ids:
            self.assertEqual(from_js[mid], md.manager_rel_path(mid),
                             f"shard path disagreement for {mid!r}")


# ---------------------------------------------------------------------------
# The page: executed, not read
# ---------------------------------------------------------------------------

class TestPageExecutesInOrder(unittest.TestCase):
    """PR #71's bug only appears when the page is RUN. So run it."""

    def test_real_page_scripts_execute_and_render_chips(self):
        if not NODE:
            self.skipTest("node not available")
        entry = scored_fixture()
        corpus, details = corpus_and_details(entry)
        tmp = Path(tempfile.mkdtemp(prefix="mdpage-"))
        site = tmp / "site"
        stats = md.publish_manager_details(site, corpus, details)
        corpus["manager_detail"] = stats
        crossleague.write_corpus_artifact(site, corpus)

        html = tmp / "crossleague.html"
        render = subprocess.run(
            [sys.executable, str(RENDER_XL), str(html)],
            capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(render.returncode, 0, render.stderr[-2000:])

        proc = subprocess.run(
            [NODE, str(ORDER_TESTS), str(html),
             str(site / crossleague.CORPUS_ARTIFACT), str(site / "managers")],
            capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(proc.returncode, 0,
                         proc.stdout[-4000:] + proc.stderr[-4000:])


if __name__ == "__main__":
    unittest.main(verbosity=2)
