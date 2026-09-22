"""Cross-league corpus backend — worker, D1 schema, client submit, reconcile.

Runnable with the stdlib alone::

    python3 -m unittest discover -s tests -p 'test_corpus_backend*.py' -v
    python3 tests/test_corpus_backend.py          # same thing

No pytest, no pip, no network. Same constraints as ``test_managerscore.py``
and for the same reason: a fresh clone has none of them.

What is verified here, and what genuinely cannot be
---------------------------------------------------
There is no Cloudflare account, no ``wrangler``, and no D1 instance in this
environment, so **the worker is never deployed and D1 is never exercised as
D1**. What is checked instead:

* ``worker.js`` parses under ``node --check``.
* The migration SQL is applied to a real in-memory SQLite database via
  ``python3 -m sqlite3``, so the DDL is parsed and executed rather than eyeballed.
* The worker's request/response contract is driven end to end in ``node``
  against that same schema through a D1-shaped shim over ``node:sqlite``
  (``tests/js/corpus_backend_tests.mjs``). Statements are therefore real
  SQL run against real SQLite; only Cloudflare's transport is simulated.
* The worker's cross-league aggregation is pinned against
  ``dynasty.crossleague.aggregate`` on identical input, so the two
  implementations of the same metric cannot drift apart silently.
* Scoring constants are pinned across all three places they appear.

What this does NOT prove: that Cloudflare's D1 driver behaves exactly like
SQLite under load, that subrequest accounting is what the docs say, or that
the deployed worker survives real traffic. Those need a deploy.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dynasty import crossleague  # noqa: E402
from dynasty.corpus_submit_js import CORPUS_SUBMIT_JS, corpus_submit_js  # noqa: E402
from dynasty.managerscore_js import MANAGERSCORE_CORE_JS  # noqa: E402

NODE = shutil.which("node")

WORKER_DIR = REPO_ROOT / "scripts" / "cf-worker"
WORKER_JS = WORKER_DIR / "worker.js"
WRANGLER = WORKER_DIR / "wrangler.toml"
MIGRATION = WORKER_DIR / "migrations" / "0001_corpus_init.sql"
JS_TESTS = REPO_ROOT / "tests" / "js" / "corpus_backend_tests.mjs"
ORDER_TESTS = REPO_ROOT / "tests" / "js" / "myteam_script_order_tests.mjs"
RENDER_MYTEAM = REPO_ROOT / "tests" / "support" / "render_myteam.py"


# ---------------------------------------------------------------------------
# The worker source itself
# ---------------------------------------------------------------------------

class WorkerSourceTests(unittest.TestCase):
    def test_worker_exists(self):
        self.assertTrue(WORKER_JS.is_file())
        self.assertTrue(MIGRATION.is_file())

    @unittest.skipUnless(NODE, "node not installed — cannot syntax-check the worker")
    def test_worker_parses(self):
        proc = subprocess.run([NODE, "--check", str(WORKER_JS)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_no_credential_is_committed(self):
        """A Cloudflare token in the repo would be the worst outcome here."""
        for path in (WORKER_JS, WRANGLER, MIGRATION):
            text = path.read_text(encoding="utf-8")
            # Cloudflare API tokens are 40 chars of [A-Za-z0-9_-].
            for match in re.findall(r"[A-Za-z0-9_-]{40,}", text):
                self.assertNotRegex(
                    match, r"^[A-Za-z0-9_-]{40}$",
                    f"{path.name} contains something token-shaped: {match[:8]}…",
                )
            lowered = text.lower()
            self.assertNotIn("cloudflare_api_token=", lowered)
            self.assertNotIn("bearer ey", lowered)

    def test_secrets_are_documented_as_wrangler_secrets(self):
        toml = WRANGLER.read_text(encoding="utf-8")
        self.assertIn("wrangler secret put IP_HASH_SALT", toml)
        self.assertIn("wrangler secret put RECONCILE_TOKEN", toml)
        # The D1 binding must ship commented out: an uncommented binding with
        # a placeholder id breaks `wrangler deploy` for the existing proxy.
        self.assertIn("# [[d1_databases]]", toml)

    def test_payload_is_never_executed(self):
        """Nothing from a submission may reach eval/Function/innerHTML."""
        src = WORKER_JS.read_text(encoding="utf-8")
        self.assertNotIn("eval(", src)
        self.assertNotIn("new Function", src)
        self.assertNotIn("innerHTML", src)

    def test_every_sql_statement_is_parameterised(self):
        """No payload value may be concatenated into SQL.

        The only interpolations allowed in a SQL string are generated
        placeholder lists (``?, ?, ?``), never a value.
        """
        src = WORKER_JS.read_text(encoding="utf-8")
        # Template literals inside db.prepare(...) are the risk surface.
        for match in re.findall(r"db\.prepare\(\s*`([^`]*)`", src):
            for expr in re.findall(r"\$\{([^}]*)\}", match):
                self.assertIn(
                    "holders", expr.replace("placeholders", "holders"),
                    f"interpolated non-placeholder into SQL: ${{{expr}}}",
                )


# ---------------------------------------------------------------------------
# D1 schema, executed against real SQLite
# ---------------------------------------------------------------------------

class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.executescript(MIGRATION.read_text(encoding="utf-8"))
        self.con.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.con.close()

    def _tables(self):
        return {r[0] for r in self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

    def test_expected_tables_exist(self):
        self.assertLessEqual(
            {"leagues", "managers", "league_managers", "submissions",
             "rate_counters", "corpus_meta"},
            self._tables(),
        )

    def test_migration_is_idempotent(self):
        """Re-running it must not fail — the owner will paste it twice."""
        self.con.executescript(MIGRATION.read_text(encoding="utf-8"))

    def test_status_is_constrained(self):
        self.con.execute(
            "INSERT INTO leagues (league_id, name, season, n_teams,"
            " first_submitted_at, last_submitted_at, status)"
            " VALUES ('1', 'L', '2026', 12, 'now', 'now', 'provisional')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO leagues (league_id, name, season, n_teams,"
                " first_submitted_at, last_submitted_at, status)"
                " VALUES ('2', 'L', '2026', 12, 'now', 'now', 'public')")

    def test_default_status_is_not_public(self):
        """A row inserted without an explicit status must not be visible."""
        self.con.execute(
            "INSERT INTO leagues (league_id, name, season, n_teams,"
            " first_submitted_at, last_submitted_at)"
            " VALUES ('3', 'L', '2026', 12, 'now', 'now')")
        row = self.con.execute(
            "SELECT status, verified FROM leagues WHERE league_id='3'").fetchone()
        self.assertEqual(row[0], "unverified")
        self.assertEqual(row[1], 0)

    def test_league_managers_cascade(self):
        self.con.execute(
            "INSERT INTO leagues (league_id, name, season, n_teams,"
            " first_submitted_at, last_submitted_at)"
            " VALUES ('9', 'L', '2026', 12, 'now', 'now')")
        self.con.execute(
            "INSERT INTO managers (manager_id, display_name, first_seen_at,"
            " last_seen_at) VALUES ('77', 'someone', 'now', 'now')")
        self.con.execute(
            "INSERT INTO league_managers (league_id, manager_id, league_index,"
            " updated_at) VALUES ('9', '77', 100.0, 'now')")
        self.con.execute("DELETE FROM leagues WHERE league_id='9'")
        n = self.con.execute("SELECT COUNT(*) FROM league_managers").fetchone()[0]
        self.assertEqual(n, 0)

    def test_submissions_records_rejections_too(self):
        """The audit table is only useful if it holds the blocked attempts."""
        for outcome in ("accepted", "rejected_schema", "rejected_rate",
                        "rejected_verify", "duplicate"):
            self.con.execute(
                "INSERT INTO submissions (submitted_at, ip_hash, outcome)"
                " VALUES ('now', 'abc', ?)", (outcome,))
        n = self.con.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
        self.assertEqual(n, 5)

    def test_no_raw_ip_column_exists(self):
        cols = {r[1] for r in self.con.execute("PRAGMA table_info(submissions)")}
        self.assertIn("ip_hash", cols)
        self.assertNotIn("ip", cols)
        self.assertNotIn("ip_address", cols)

    def test_only_display_name_is_stored_for_a_person(self):
        """Privacy: nothing beyond a pseudonymous handle."""
        cols = {r[1] for r in self.con.execute("PRAGMA table_info(managers)")}
        for forbidden in ("email", "real_name", "username", "avatar", "phone"):
            self.assertNotIn(forbidden, cols)


# ---------------------------------------------------------------------------
# Constants must not drift across the three places they live
# ---------------------------------------------------------------------------

class ConstantPinningTests(unittest.TestCase):
    """The board is only meaningful if it aggregates the SAME metric.

    ``crossleague.assert_scoring_constants_match`` already pins the Python
    aggregator against the page scorer. The worker is now a THIRD copy, so it
    gets pinned too.
    """

    def setUp(self):
        self.src = WORKER_JS.read_text(encoding="utf-8")

    def _num(self, pattern):
        m = re.search(pattern, self.src)
        self.assertIsNotNone(m, f"could not read {pattern} out of worker.js")
        return float(m.group(1))

    def test_page_scorer_and_python_still_agree(self):
        crossleague.assert_scoring_constants_match(MANAGERSCORE_CORE_JS)

    def test_worker_weights_match(self):
        self.assertAlmostEqual(
            self._num(r"MS_WEIGHTS\s*=\s*\{\s*draft:\s*([0-9.]+)"),
            crossleague.MS_WEIGHTS["draft"])
        self.assertAlmostEqual(
            self._num(r"MS_WEIGHTS\s*=[^}]*trade:\s*([0-9.]+)"),
            crossleague.MS_WEIGHTS["trade"])
        self.assertAlmostEqual(
            self._num(r"MS_WEIGHTS\s*=[^}]*waiver:\s*([0-9.]+)"),
            crossleague.MS_WEIGHTS["waiver"])

    def test_worker_shrink_constants_match(self):
        self.assertAlmostEqual(
            self._num(r"MS_SHRINK_K\s*=\s*\{\s*draft:\s*([0-9.]+)"),
            crossleague.MS_SHRINK_K["draft"])
        self.assertAlmostEqual(
            self._num(r"MS_SHRINK_K\s*=[^}]*trade:\s*([0-9.]+)"),
            crossleague.MS_SHRINK_K["trade"])
        self.assertAlmostEqual(
            self._num(r"MS_SHRINK_K\s*=[^}]*waiver:\s*([0-9.]+)"),
            crossleague.MS_SHRINK_K["waiver"])

    def test_worker_index_scale_matches(self):
        self.assertAlmostEqual(self._num(r"MS_INDEX_CENTER\s*=\s*([0-9.]+)"),
                               crossleague.MS_INDEX_CENTER)
        self.assertAlmostEqual(self._num(r"MS_INDEX_SCALE\s*=\s*([0-9.]+)"),
                               crossleague.MS_INDEX_SCALE)


# ---------------------------------------------------------------------------
# Client submit script
# ---------------------------------------------------------------------------

class ClientSubmitTests(unittest.TestCase):
    def test_unconfigured_build_is_inert(self):
        js = corpus_submit_js("")
        self.assertIn("var CS_URL = '';", js)
        self.assertNotIn("__CORPUS_URL__", js)

    def test_configured_build_splices_the_url(self):
        js = corpus_submit_js("https://w.example.workers.dev/")
        self.assertIn("var CS_URL = 'https://w.example.workers.dev';", js)

    def test_non_http_url_is_refused(self):
        for hostile in ("javascript:alert(1)", "'; alert(1); var x='",
                        "data:text/html,<script>"):
            js = corpus_submit_js(hostile)
            self.assertIn("var CS_URL = '';", js,
                          f"{hostile!r} was not neutralised")

    def test_quotes_cannot_break_out_of_the_literal(self):
        """The assignment must still parse as one closed string literal.

        Counting quotes on the whole line would also count the trailing
        comment, so match the literal itself and assert it contains no
        quote or backslash that could terminate it early.
        """
        for hostile in ("https://w.example'+alert(1)+'",
                        "https://w.example\\';alert(1);//",
                        'https://w.example";alert(1);//'):
            js = corpus_submit_js(hostile)
            line = [l for l in js.splitlines()
                    if l.startswith("var CS_URL")][0]
            m = re.match(r"var CS_URL = '([^']*)';", line)
            self.assertIsNotNone(m, f"literal not closed for {hostile!r}: {line}")
            self.assertNotIn("\\", m.group(1))

    def test_template_has_no_leftover_tokens(self):
        self.assertIn("__CORPUS_URL__", CORPUS_SUBMIT_JS)
        self.assertNotIn("__CORPUS_URL__", corpus_submit_js("https://x.example"))
        self.assertNotIn("__SUBMIT_ISSUE_URL__",
                         corpus_submit_js("", "https://x.example/issues/new"))

    def test_unconfigured_build_states_the_score_was_not_recorded(self):
        """Silence about a missing backend reads as success.

        The unconfigured page still scores the league and still draws a
        full table, so rendering nothing after "Score this league" leaves a
        visitor believing it was submitted. The owner hit exactly that.
        """
        js = corpus_submit_js("")
        self.assertIn("state === 'off'", js)
        self.assertIn("This score was not recorded.", js)

    def test_unconfigured_build_names_the_fallback_that_works(self):
        js = corpus_submit_js("", "https://github.com/o/r/issues/new"
                                  "?template=league-submission.yml")
        self.assertIn("var CS_SUBMIT_ISSUE_URL = "
                      "'https://github.com/o/r/issues/new"
                      "?template=league-submission.yml';", js)

    def test_issue_url_is_sanitised_like_the_corpus_url(self):
        for hostile in ("javascript:alert(1)", "'; alert(1); var x='",
                        "data:text/html,<script>"):
            js = corpus_submit_js("", hostile)
            self.assertIn("var CS_SUBMIT_ISSUE_URL = '';", js,
                          f"{hostile!r} was not neutralised")

    def test_default_issue_url_points_at_the_repo_template(self):
        """The wiring, not just the splice: the emitted script must carry it."""
        import os

        from dynasty.managerscore import manager_score_corpus_js

        old = os.environ.get("DFM_CORPUS_URL")
        os.environ.pop("DFM_CORPUS_URL", None)
        try:
            js = manager_score_corpus_js()
        finally:
            if old is not None:
                os.environ["DFM_CORPUS_URL"] = old
        self.assertIn("template=league-submission.yml", js)
        self.assertIn("/issues/new?", js)

    @unittest.skipUnless(NODE, "node not installed")
    def test_client_script_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "submit.js"
            p.write_text(corpus_submit_js("https://w.example"), encoding="utf-8")
            proc = subprocess.run([NODE, "--check", str(p)],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)


class ManagerScoreSectionWiringTests(unittest.TestCase):
    """The corpus UI after PR #69 moved Manager Score into Input Sleeper Team.

    Manager Score is no longer a page. ``build_manager_score`` is gone;
    ``manager_score_section()`` returns markup that ``myteam.py`` embeds, and
    the submit script is handed to the host page by
    ``manager_score_corpus_js()``. The corpus opt-in therefore has to live
    inside that section, which is what these assert.

    Neither function imports ``dynasty.report``, so unlike the old page test
    this needs no stub at all.
    """

    def _section(self, corpus_url):
        import os

        from dynasty.managerscore import (
            manager_score_corpus_js,
            manager_score_section,
        )

        old = os.environ.get("DFM_CORPUS_URL")
        if corpus_url:
            os.environ["DFM_CORPUS_URL"] = corpus_url
        else:
            os.environ.pop("DFM_CORPUS_URL", None)
        try:
            return manager_score_section(), manager_score_corpus_js()
        finally:
            if old is None:
                os.environ.pop("DFM_CORPUS_URL", None)
            else:
                os.environ["DFM_CORPUS_URL"] = old

    def test_unconfigured_section_offers_nothing(self):
        section, js = self._section("")
        self.assertNotIn('type="checkbox" id="ms-corpus-optin"', section)
        self.assertNotIn("cross-league index", section)
        # The script is still emitted -- inert, not absent.
        self.assertIn("var CS_URL = '';", js)

    def test_configured_section_offers_the_index(self):
        section, js = self._section("https://w.example.workers.dev")
        self.assertIn('type="checkbox" id="ms-corpus-optin"', section)
        self.assertIn("var CS_URL = 'https://w.example.workers.dev';", js)

    def test_status_box_is_in_the_section_either_way(self):
        """csRender writes into ms-corpus-state; it must always exist."""
        for url in ("", "https://w.example"):
            section, _ = self._section(url)
            self.assertIn("ms-corpus-state", section)

    def test_configured_section_states_what_is_stored(self):
        section, _ = self._section("https://w.example.workers.dev")
        self.assertIn("display name", section)
        self.assertIn("nightly job", section)

    def test_no_unreplaced_tokens(self):
        for url in ("", "https://w.example"):
            section, js = self._section(url)
            self.assertNotIn("__CORPUS_OPTIN__", section)
            self.assertNotIn("__METHODOLOGY__", section)
            self.assertNotIn("__CORPUS_URL__", js)

    def test_section_carries_no_script_tag(self):
        """The host page owns script placement; the section must not.

        This is the contract that lets myteam.py control ORDER, which is what
        PR #71 showed actually matters.
        """
        section, _ = self._section("https://w.example")
        self.assertNotIn("<script", section)

    def test_hook_is_optional_in_the_scorer(self):
        """Manager Score must work with the submit script absent."""
        from dynasty.managerscore_js import MANAGERSCORE_UI_JS
        self.assertIn("typeof csOnScored === 'function'", MANAGERSCORE_UI_JS)

    def test_myteam_emits_the_corpus_script_after_the_ui_script(self):
        """Source-level guard on the emission order myteam.py declares."""
        src = (REPO_ROOT / "src" / "dynasty" / "myteam.py").read_text(
            encoding="utf-8")
        self.assertIn("__MANAGER_SCORE_CORPUS_JS__", src)
        self.assertLess(src.index("<script>__MANAGER_SCORE_UI_JS__</script>"),
                        src.index("<script>__MANAGER_SCORE_CORPUS_JS__</script>"))

    def test_standalone_page_is_only_a_signpost(self):
        """managerscore.html must not carry corpus code any more."""
        src = (REPO_ROOT / "src" / "dynasty" / "managerscore.py").read_text(
            encoding="utf-8")
        pointer = src[src.index("def build_manager_score_pointer"):]
        self.assertNotIn("CORPUS", pointer)
        self.assertNotIn("<script", pointer)


@unittest.skipUnless(NODE, "node not installed — script order is unverified")
class MyTeamScriptOrderTests(unittest.TestCase):
    """Execute the real page's scripts in the real order.

    PR #71: ``report._page`` appends the shared highlight renderer AFTER the
    body, so a page script touching ``DFMHL`` during evaluation threw and
    killed the page. ``node --check`` cannot see that -- the file parses --
    and neither can reading the Python, because the order that matters is the
    order the built HTML ends up in.

    Manager Score moved onto that page, and the corpus script moved with it,
    so this renders myteam.html for real and runs every blob in document
    order over a DOM shim.

    It cannot confirm the page LOOKS right -- there is no browser here.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        render = subprocess.run(
            [sys.executable, str(RENDER_MYTEAM), cls.tmp.name],
            capture_output=True, text=True, timeout=600,
        )
        cls.render = render
        cls.proc = None
        if render.returncode == 0:
            cls.proc = subprocess.run(
                [NODE, str(ORDER_TESTS),
                 str(Path(cls.tmp.name) / "myteam-configured.html"),
                 str(Path(cls.tmp.name) / "myteam-unconfigured.html")],
                capture_output=True, text=True, timeout=600,
            )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_page_renders(self):
        self.assertEqual(self.render.returncode, 0,
                         "render_myteam.py failed:\n" + self.render.stderr[-4000:])

    def test_script_order_suite_passes(self):
        self.assertIsNotNone(self.proc, "page did not render; suite not run")
        if self.proc.returncode != 0:
            self.fail("node script-order suite failed:\n"
                      + (self.proc.stderr[-4000:] or self.proc.stdout[-4000:]))
        self.assertIn("checks passed", self.proc.stdout)


# ---------------------------------------------------------------------------
# Reconcile job
# ---------------------------------------------------------------------------

def _load_reconcile():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "reconcile_corpus", REPO_ROOT / "scripts" / "reconcile_corpus.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.rc = _load_reconcile()

    def _auth(self, **overrides):
        base = {"860000000000000001": 110.0, "860000000000000002": 90.0}
        base.update(overrides)
        return [{"manager_id": k, "index": v} for k, v in base.items()]

    def test_agreement_confirms(self):
        out = self.rc.compare(
            {"860000000000000001": 110.0, "860000000000000002": 90.0},
            self._auth())
        self.assertEqual(out["verdict"], "confirmed")

    def test_small_drift_still_confirms(self):
        """Values move day to day; the tolerance exists for that."""
        out = self.rc.compare(
            {"860000000000000001": 110.4, "860000000000000002": 89.7},
            self._auth())
        self.assertEqual(out["verdict"], "confirmed")

    def test_inflated_submission_is_rejected(self):
        out = self.rc.compare(
            {"860000000000000001": 145.0, "860000000000000002": 90.0},
            self._auth())
        self.assertEqual(out["verdict"], "rejected")
        self.assertIn("disagrees", out["note"])

    def test_extra_manager_is_rejected(self):
        out = self.rc.compare(
            {"860000000000000001": 110.0, "860000000000000002": 90.0,
             "860000000000000999": 130.0},
            self._auth())
        self.assertEqual(out["verdict"], "rejected")
        self.assertIn("manager set differs", out["note"])

    def test_missing_scores_adopt_the_rescore(self):
        out = self.rc.compare({}, self._auth())
        self.assertEqual(out["verdict"], "confirmed")

    def test_synthetic_roster_without_evidence_is_dropped(self):
        managers = self.rc.to_submission_managers({"managers": [
            {"id": "860000000000000001", "name": "a", "index": 100,
             "rank": 1, "composite": 0,
             "draft": {"n": 3, "z": 0.1, "mean": 1, "shrunk": 1},
             "trade": {"n": 0, "z": 0, "mean": 0, "shrunk": 0},
             "waiver": {"n": 0, "z": 0, "mean": 0, "shrunk": 0}},
            {"id": "roster:123:4", "name": "Team 4", "index": 100,
             "rank": 2, "composite": 0,
             "draft": {"n": 0, "z": 0, "mean": 0, "shrunk": 0},
             "trade": {"n": 0, "z": 0, "mean": 0, "shrunk": 0},
             "waiver": {"n": 0, "z": 0, "mean": 0, "shrunk": 0}},
        ]})
        self.assertEqual([m["manager_id"] for m in managers],
                         ["860000000000000001"])

    def test_synthetic_roster_with_evidence_refuses_the_league(self):
        """Dropping it would break the z-distribution the worker checks."""
        with self.assertRaises(ValueError):
            self.rc.to_submission_managers({"managers": [
                {"id": "roster:123:4", "name": "Team 4", "index": 100,
                 "rank": 1, "composite": 0,
                 "draft": {"n": 5, "z": 1.0, "mean": 1, "shrunk": 1},
                 "trade": {"n": 0, "z": 0, "mean": 0, "shrunk": 0},
                 "waiver": {"n": 0, "z": 0, "mean": 0, "shrunk": 0}},
            ]})

    def test_unconfigured_run_is_a_no_op_not_a_failure(self):
        import os
        for k in ("CORPUS_URL", "CORPUS_RECONCILE_TOKEN"):
            os.environ.pop(k, None)
        self.assertEqual(self.rc.main([]), 0)


# ---------------------------------------------------------------------------
# The node suite: worker contract + aggregation cross-check
# ---------------------------------------------------------------------------

@unittest.skipUnless(NODE, "node not installed — the worker contract is unverified")
class WorkerContractTests(unittest.TestCase):
    """Drive the real worker in node against real SQLite through a D1 shim."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        core = Path(cls.tmp.name) / "ms_core.js"
        core.write_text(MANAGERSCORE_CORE_JS, encoding="utf-8")
        cls.proc = subprocess.run(
            [NODE, str(JS_TESTS), str(core), str(WORKER_JS), str(MIGRATION)],
            capture_output=True, text=True, timeout=600,
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_worker_contract_suite_passes(self):
        if self.proc.returncode != 0:
            self.fail("node worker suite failed:\n" + self.proc.stderr[-4000:])
        self.assertIn("checks passed", self.proc.stderr)

    def _fixture(self):
        for line in self.proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            data = json.loads(line)
            if data.get("kind") == "aggregate_fixture":
                return data
        self.fail("node suite emitted no aggregate fixture")

    def test_worker_aggregation_matches_python_aggregation(self):
        """The corpus board must be the same number wherever it is computed.

        The worker aggregates in JS (it serves the board straight from D1);
        ``dynasty.crossleague`` aggregates in Python (it builds the committed
        artifact). Two implementations of one metric is exactly the drift
        risk this repo already worries about, so they are pinned on identical
        input.
        """
        fixture = self._fixture()
        rows = fixture["rows"]

        # Rebuild crossleague.aggregate()'s input shape from the same rows.
        managers = [{
            "id": r["manager_id"],
            "name": r["display_name"],
            "index": r["league_index"],
            "rank": r["league_rank"],
            "flags": [],
            "draft": {"n": r["draft_n"], "z": r["draft_z"],
                      "mean": r["draft_mean"], "shrunk": r["draft_shrunk"]},
            "trade": {"n": r["trade_n"], "z": r["trade_z"],
                      "mean": r["trade_mean"], "shrunk": r["trade_shrunk"]},
            "waiver": {"n": r["waiver_n"], "z": r["waiver_z"],
                       "mean": r["waiver_mean"], "shrunk": r["waiver_shrunk"]},
        } for r in rows]

        py = crossleague.aggregate([{
            "league_id": rows[0]["league_id"],
            "name": rows[0]["league_name"],
            "season": rows[0]["season"],
            "n_teams": rows[0]["n_teams"],
            "result": {"managers": managers},
        }])

        js_by_id = {m["manager_id"]: m for m in fixture["aggregate"]["managers"]}
        py_by_id = {m["manager_id"]: m for m in py["managers"]}
        self.assertEqual(set(js_by_id), set(py_by_id))

        for mid, pym in py_by_id.items():
            jsm = js_by_id[mid]
            self.assertAlmostEqual(jsm["composite"], pym["composite"], places=5,
                                   msg=f"composite drift for {mid}")
            self.assertAlmostEqual(jsm["cross_index"], pym["cross_index"], places=1,
                                   msg=f"cross_index drift for {mid}")
            self.assertEqual(jsm["rank"], pym["rank"], f"rank drift for {mid}")
            self.assertEqual(jsm["n_leagues"], pym["n_leagues"])
            for comp in ("draft", "trade", "waiver"):
                self.assertAlmostEqual(
                    jsm["components"][comp]["z"],
                    pym["components"][comp]["z"], places=5,
                    msg=f"{comp} z drift for {mid}")
                self.assertAlmostEqual(
                    jsm["components"][comp]["weight"],
                    pym["components"][comp]["weight"], places=6)

    def test_draft_board_matches_python(self):
        fixture = self._fixture()
        js_board = fixture["aggregate"]["draft_board"]
        self.assertTrue(js_board, "draft board fixture is empty")
        rows = fixture["rows"]
        managers = [{
            "id": r["manager_id"], "name": r["display_name"],
            "index": r["league_index"], "rank": r["league_rank"], "flags": [],
            "draft": {"n": r["draft_n"], "z": r["draft_z"],
                      "mean": r["draft_mean"], "shrunk": r["draft_shrunk"]},
            "trade": {"n": r["trade_n"], "z": r["trade_z"],
                      "mean": r["trade_mean"], "shrunk": r["trade_shrunk"]},
            "waiver": {"n": r["waiver_n"], "z": r["waiver_z"],
                       "mean": r["waiver_mean"], "shrunk": r["waiver_shrunk"]},
        } for r in rows]
        py = crossleague.aggregate([{
            "league_id": rows[0]["league_id"], "name": rows[0]["league_name"],
            "season": rows[0]["season"], "n_teams": rows[0]["n_teams"],
            "result": {"managers": managers},
        }])
        self.assertEqual([m["manager_id"] for m in js_board],
                         [m["manager_id"] for m in py["draft_board"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
