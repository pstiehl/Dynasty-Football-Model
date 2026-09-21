"""Stdlib-only tests for the nflverse-backed career stats panel.

Deliberately uses ``unittest`` and nothing else: this suite must run in
environments with no pip access, no pytest, no pandas and no bs4 (the
old PFR module can't even be imported without bs4). pytest still
collects ``unittest.TestCase`` classes, so CI keeps running it too.

    python3 -m unittest tests.test_nflverse_career_stats -v
    python3 tests/test_nflverse_career_stats.py

The fixture is a synthetic ``player_stats_season.csv.gz`` written to a
temp dir with the real 52-column nflverse schema, covering a QB, RB, WR
and TE across multiple seasons.
"""
from __future__ import annotations

import ast
import csv
import gzip
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    # Normal path (CI, where httpx/bs4 are installed).
    from dynasty.sources import nflverse_career_stats as ncs  # noqa: E402
except ImportError:  # pragma: no cover - offline/stdlib-only environments
    # ``dynasty.sources.__init__`` imports ``base``, which needs httpx.
    # This module is pure stdlib, so load it straight off disk instead;
    # that keeps the suite runnable with no third-party packages at all.
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "nflverse_career_stats",
        SRC / "dynasty" / "sources" / "nflverse_career_stats.py",
    )
    ncs = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(ncs)

# The real 52-column schema written by scripts/refresh_nflverse_corpus.py.
COLUMNS = [
    "season", "season_type", "player_id", "player_name",
    "player_display_name", "position", "position_group", "headshot_url",
    "games", "recent_team", "completions", "attempts", "passing_yards",
    "passing_tds", "interceptions", "sacks", "sack_yards", "sack_fumbles",
    "sack_fumbles_lost", "passing_air_yards", "passing_yards_after_catch",
    "passing_first_downs", "passing_epa", "passing_2pt_conversions",
    "pacr", "dakota", "carries", "rushing_yards", "rushing_tds",
    "rushing_fumbles", "rushing_fumbles_lost", "rushing_first_downs",
    "rushing_epa", "rushing_2pt_conversions", "receptions", "targets",
    "receiving_yards", "receiving_tds", "receiving_fumbles",
    "receiving_fumbles_lost", "receiving_air_yards",
    "receiving_yards_after_catch", "receiving_first_downs",
    "receiving_epa", "receiving_2pt_conversions", "racr", "target_share",
    "air_yards_share", "wopr", "special_teams_tds", "fantasy_points",
    "fantasy_points_ppr",
]


def row(**kw) -> dict:
    """One corpus row: every column blank unless overridden."""
    base = {c: "" for c in COLUMNS}
    base["season_type"] = "REG"
    base.update({k: str(v) for k, v in kw.items()})
    return base


# ---------------------------------------------------------------------------
# Synthetic corpus
# ---------------------------------------------------------------------------

QB = "00-TESTQB1"
RB = "00-TESTRB1"
WR = "00-TESTWR1"
TE = "00-TESTTE1"
OLDTIMER = "pfr_TestOl00"

FIXTURE_ROWS = [
    # --- sentinel + non-REG rows that must be filtered out -------------
    row(season=1999, player_id="0", games=247, recent_team="SF"),
    row(season=2023, season_type="POST", player_id=QB, position="QB",
        recent_team="KC", games=3, completions=60, attempts=90,
        passing_yards=800, passing_tds=8, interceptions=1),

    # --- QB: three seasons --------------------------------------------
    row(season=2021, player_id=QB, player_display_name="Test Quarterback",
        position="QB", position_group="QB", recent_team="KC", games=17,
        completions=350, attempts=550, passing_yards=4200, passing_tds=30,
        interceptions=12, carries=50, rushing_yards=250, rushing_tds=2,
        sack_fumbles_lost=1),
    row(season=2022, player_id=QB, player_display_name="Test Quarterback",
        position="QB", position_group="QB", recent_team="KC", games=17,
        completions=400, attempts=600, passing_yards=5000, passing_tds=40,
        interceptions=10, carries=60, rushing_yards=300, rushing_tds=3,
        sack_fumbles_lost=2, rushing_fumbles_lost=1),
    row(season=2023, player_id=QB, player_display_name="Test Quarterback",
        position="QB", position_group="QB", recent_team="KC", games=16,
        completions=401, attempts=597, passing_yards=4183, passing_tds=27,
        interceptions=14, carries=75, rushing_yards=389, rushing_tds=0,
        sack_fumbles_lost=3),

    # --- RB: two seasons, one with NA junk in unused-but-parsed cells --
    row(season=2022, player_id=RB, player_display_name="Test Runningback",
        position="RB", position_group="RB", recent_team="NYG", games=16,
        carries=295, rushing_yards=1312, rushing_tds=10, targets=76,
        receptions=57, receiving_yards=338, receiving_tds=2,
        rushing_fumbles_lost=1),
    row(season=2023, player_id=RB, player_display_name="Test Runningback",
        position="RB", position_group="RB", recent_team="NYG", games=14,
        carries=300, rushing_yards=1400, rushing_tds=14, targets=60,
        receptions=50, receiving_yards=400, receiving_tds=2,
        rushing_fumbles_lost=2),

    # --- WR: three seasons, incl. negative rushing yards ---------------
    row(season=2021, player_id=WR, player_display_name="Test Receiver",
        position="WR", position_group="WR", recent_team="MIN", games=17,
        targets=167, receptions=108, receiving_yards=1616,
        receiving_tds=10, carries=6, rushing_yards=14),
    row(season=2022, player_id=WR, player_display_name="Test Receiver",
        position="WR", position_group="WR", recent_team="MIN", games=10,
        targets=100, receptions=68, receiving_yards=1074, receiving_tds=5,
        carries=1, rushing_yards=-12, receiving_fumbles_lost=1),
    row(season=2023, player_id=WR, player_display_name="Test Receiver",
        position="WR", position_group="WR", recent_team="MIN", games=17,
        targets=150, receptions=100, receiving_yards=1500,
        receiving_tds=12, carries=5, rushing_yards=40, rushing_tds=1,
        receiving_fumbles_lost=1),

    # --- TE: two seasons, one with blank/NA cells (incomplete row) -----
    row(season=2022, player_id=TE, player_display_name="Test TightEnd",
        position="TE", position_group="TE", recent_team="KC", games=17,
        targets=110, receptions=78, receiving_yards=1000,
        receiving_tds=4),
    row(season=2023, player_id=TE, player_display_name="Test TightEnd",
        position="TE", position_group="TE", recent_team="KC", games="NA",
        targets=100, receptions=80, receiving_yards=900, receiving_tds=8,
        carries="", rushing_yards="NA"),

    # --- pre-1999 style synthetic pfr_ id ------------------------------
    row(season=1988, player_id=OLDTIMER, player_display_name="Test Oldtimer",
        position="RB", position_group="RB", recent_team="2TM", games=15,
        carries=200, rushing_yards=900, rushing_tds=8, targets=30,
        receptions=25, receiving_yards=200, receiving_tds=1),
]


def write_fixture(path: Path) -> None:
    with gzip.open(path, "wt", newline="", encoding="utf-8") as gz:
        w = csv.DictWriter(gz, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(FIXTURE_ROWS)


# ---------------------------------------------------------------------------
# Extract the PFR _season_fp from source WITHOUT importing the module
# (importing it needs bs4, which is not installable in this environment).
# ---------------------------------------------------------------------------

SCORING_CONSTANTS = (
    "PASS_YDS_PER_PT", "RUSH_YDS_PER_PT", "REC_YDS_PER_PT",
    "PT_PER_PASS_TD", "PT_PER_RUSH_TD", "PT_PER_REC_TD",
    "PT_PER_REC", "PT_PER_INT", "PT_PER_FUMBLE",
)


def load_pfr_season_fp():
    """Exec only the scoring constants + ``_season_fp`` from the PFR module.

    Lifts the exact nodes out of the live source file, so the parity
    assertions below break if anyone edits either implementation.
    """
    src_path = SRC / "dynasty" / "sources" / "pfr_career_stats.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    wanted = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in SCORING_CONSTANTS
            for t in node.targets
        ):
            wanted.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "_season_fp":
            wanted.append(node)
    missing = set(SCORING_CONSTANTS) - {
        t.id for n in wanted if isinstance(n, ast.Assign)
        for t in n.targets if isinstance(t, ast.Name)
    }
    if missing:
        raise AssertionError(f"PFR module missing constants: {sorted(missing)}")
    module = ast.Module(body=wanted, type_ignores=[])
    ns: dict = {}
    exec(compile(module, str(src_path), "exec"), ns)  # noqa: S102
    return ns["_season_fp"], ns


class TestFantasyPointParity(unittest.TestCase):
    """The ported scoring must be byte-identical to the PFR original."""

    def test_constants_match_pfr(self):
        _fp, ns = load_pfr_season_fp()
        for name in SCORING_CONSTANTS:
            self.assertEqual(
                getattr(ncs, name), ns[name],
                f"{name} drifted from pfr_career_stats",
            )

    def test_season_fp_matches_pfr_for_identical_inputs(self):
        pfr_fp, _ns = load_pfr_season_fp()
        cases = [
            {},
            {"pass_yds": 250, "pass_td": 2, "pass_int": 1, "rush_yds": 30,
             "rush_td": 1, "rec": 5, "rec_yds": 50, "rec_td": 0,
             "fumbles": 1},
            {"pass_yds": 5000, "pass_td": 40, "pass_int": 10,
             "rush_yds": 300, "rush_td": 3, "fumbles": 3},
            {"rush_yds": 1400, "rush_td": 14, "rec": 50, "rec_yds": 400,
             "rec_td": 2, "fumbles": 2},
            {"rec": 100, "rec_yds": 1500, "rec_td": 12, "rush_yds": 40,
             "rush_td": 1, "fumbles": 1},
            {"rush_yds": -12, "rec": 68, "rec_yds": 1074, "rec_td": 5,
             "fumbles": 1},
            {"pass_yds": 1, "pass_td": 0, "pass_int": 0},
        ]
        for c in cases:
            self.assertEqual(ncs._season_fp(c), pfr_fp(c), f"mismatch on {c}")

    def test_known_hand_computed_values(self):
        # 250/25 + 2*4 - 2 + 30/10 + 6 + 5 + 50/10 - 2 = 33.0
        self.assertEqual(
            ncs._season_fp({
                "pass_yds": 250, "pass_td": 2, "pass_int": 1,
                "rush_yds": 30, "rush_td": 1, "rec": 5, "rec_yds": 50,
                "rec_td": 0, "fumbles": 1,
            }),
            33.0,
        )


class CorpusTestCase(unittest.TestCase):
    """Base class that points the builder at the synthetic fixture."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.fixture = Path(cls._tmp.name) / "player_stats_season.csv.gz"
        write_fixture(cls.fixture)
        cls.paths = (str(cls.fixture),)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        ncs.clear_cache()

    def build(self, player_id, position):
        return ncs.build_career_stats(player_id, position, paths=self.paths)


class TestIndexing(CorpusTestCase):

    def test_sentinel_and_postseason_rows_are_filtered(self):
        index = ncs.load_season_index(self.paths)
        self.assertNotIn("0", index)
        qb_seasons = [r["season"] for r in index[QB]]
        self.assertEqual(qb_seasons, ["2021", "2022", "2023"])

    def test_seasons_sorted_oldest_first(self):
        career = self.build(WR, "WR")
        self.assertEqual([r["year"] for r in career["rows"]],
                         ["2021", "2022", "2023"])

    def test_pre1999_pfr_style_id_resolves_through_same_index(self):
        career = self.build(OLDTIMER, "RB")
        self.assertEqual(len(career["rows"]), 1)
        self.assertEqual(career["rows"][0]["year"], "1988")
        self.assertEqual(career["rows"][0]["team"], "2TM")


class TestQuarterback(CorpusTestCase):

    def test_rows_totals_and_fp(self):
        career = self.build(QB, "QB")
        rows = career["rows"]
        totals = career["totals"]
        self.assertEqual(len(rows), 3)
        self.assertEqual(career["fp_format"], "superflex_ppr")

        r22 = rows[1]
        self.assertEqual(r22["year"], "2022")
        self.assertEqual(r22["pass_cmp"], 400)
        self.assertEqual(r22["pass_att"], 600)
        self.assertEqual(r22["cmp_pct"], 66.7)
        self.assertEqual(r22["pass_yds"], 5000)
        self.assertEqual(r22["rush_yds"], 300)
        # sack_fumbles_lost 2 + rushing_fumbles_lost 1 = 3
        self.assertEqual(r22["fumbles"], 3)
        # 5000/25 + 40*4 - 10*2 + 300/10 + 3*6 - 3*2 = 382.0
        self.assertEqual(r22["fp"], 382.0)

        self.assertEqual(totals["games"], 50)
        self.assertEqual(totals["pass_yds"], 4200 + 5000 + 4183)
        self.assertEqual(totals["pass_td"], 97)
        self.assertEqual(totals["pass_int"], 36)
        self.assertEqual(totals["rush_yds"], 250 + 300 + 389)
        self.assertEqual(totals["cmp_pct"],
                         round(1151 / 1747 * 100, 1))
        self.assertEqual(totals["fp"], round(sum(r["fp"] for r in rows), 1))

    def test_html_has_qb_columns_seasons_and_totals(self):
        html = ncs.career_stats_html(self.build(QB, "QB"))
        for header in (">Year<", ">Cmp%<", ">Pass Yds<", ">INT<", ">FP<"):
            self.assertIn(header, html)
        # WR/RB-only headers must not appear on a QB panel.
        self.assertNotIn(">Tgt<", html)
        for season in ("2021", "2022", "2023"):
            self.assertIn(f">{season}<", html)
        self.assertIn("<strong>Career</strong>", html)
        self.assertIn(">5000<", html)          # 2022 passing yards
        self.assertIn(">382.0<", html)         # 2022 fantasy points
        self.assertIn("<strong>13383</strong>", html)  # career pass yds
        self.assertIn("nflverse", html)


class TestRunningBack(CorpusTestCase):

    def test_rows_totals_and_fp(self):
        career = self.build(RB, "RB")
        rows, totals = career["rows"], career["totals"]
        self.assertEqual(len(rows), 2)
        r23 = rows[1]
        # 1400/10 + 14*6 + 400/10 + 2*6 + 50*1 - 2*2 = 322.0
        self.assertEqual(r23["fp"], 322.0)
        self.assertEqual(totals["rush_yds"], 1312 + 1400)
        self.assertEqual(totals["rec"], 107)
        self.assertEqual(totals["targets"], 136)
        self.assertEqual(totals["fp"], round(sum(r["fp"] for r in rows), 1))

    def test_html_uses_rush_leaning_columns(self):
        html = ncs.career_stats_html(self.build(RB, "RB"))
        self.assertIn(">Rush Att<", html)
        self.assertIn(">Rec Yds<", html)
        self.assertIn(">Tgt<", html)
        self.assertNotIn(">Cmp%<", html)
        self.assertIn(">322.0<", html)


class TestReceiverAndTightEnd(CorpusTestCase):

    def test_wr_fp_and_negative_rushing_yards(self):
        career = self.build(WR, "WR")
        rows = career["rows"]
        # 2022: -12/10 + 68 + 1074/10 + 5*6 - 2 = -1.2+68+107.4+30-2 = 202.2
        self.assertEqual(rows[1]["rush_yds"], -12)
        self.assertEqual(rows[1]["fp"], 202.2)
        # 2023: 40/10 + 6 + 1500/10 + 12*6 + 100 - 2 = 330.0
        self.assertEqual(rows[2]["fp"], 330.0)

    def test_te_uses_receiving_columns_and_tolerates_blank_cells(self):
        career = self.build(TE, "TE")
        rows = career["rows"]
        self.assertEqual(len(rows), 2)
        # games="NA" and rushing_yards="NA" coerce to 0, no exception.
        self.assertEqual(rows[1]["games"], 0)
        self.assertEqual(rows[1]["rush_yds"], 0)
        # 900/10 + 8*6 + 80*1 = 218.0
        self.assertEqual(rows[1]["fp"], 218.0)
        html = ncs.career_stats_html(career)
        self.assertIn(">Tgt<", html)
        self.assertIn(">Rec TD<", html)
        self.assertNotIn(">Cmp%<", html)
        self.assertIn(">218.0<", html)


class TestGracefulDegradation(CorpusTestCase):

    def test_unknown_player_yields_empty_output_no_exception(self):
        career = self.build("00-9999999", "WR")
        self.assertEqual(career["rows"], [])
        self.assertEqual(career["totals"], {})
        self.assertEqual(ncs.career_stats_html(career), "")

    def test_blank_player_id_yields_empty_output(self):
        career = self.build("", "QB")
        self.assertEqual(career["rows"], [])
        self.assertEqual(ncs.career_stats_html(career), "")

    def test_missing_corpus_file_yields_empty_output(self):
        ncs.clear_cache()
        career = ncs.build_career_stats(
            QB, "QB", paths=(str(Path(self._tmp.name) / "nope.csv.gz"),)
        )
        self.assertEqual(career["rows"], [])
        self.assertEqual(ncs.career_stats_html(career), "")

    def test_unsupported_position_renders_nothing(self):
        career = self.build(QB, "K")
        self.assertEqual(career["rows"], [])
        self.assertEqual(ncs.career_stats_html(career), "")

    def test_position_inferred_from_corpus_when_engine_row_lacks_it(self):
        career = self.build(WR, "")
        self.assertEqual(career["position"], "WR")
        self.assertEqual(len(career["rows"]), 3)

    def test_html_handles_empty_and_malformed_payloads(self):
        self.assertEqual(ncs.career_stats_html({}), "")
        self.assertEqual(ncs.career_stats_html(
            {"rows": [], "position": "QB", "totals": {}}), "")


class TestPublicSurfaceParity(unittest.TestCase):
    """The report only needs these two names; keep them stable."""

    def test_exposes_pfr_compatible_surface(self):
        self.assertTrue(callable(ncs.build_career_stats))
        self.assertTrue(callable(ncs.career_stats_html))


if __name__ == "__main__":
    unittest.main(verbosity=2)
