"""Tests for v3.10 PFR career-stats builder + audit math.

The career-stats tests run against checked-in fixtures captured from
Wayback (PFR player pages for Peyton Manning, Jerry Rice, Emmitt Smith).
Audit-math tests live in this file too because they're tiny and there's
no benefit to a second test file.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bs4 import BeautifulSoup  # noqa: E402

from dynasty.sources import pfr_career_stats as pcs  # noqa: E402

FIXTURE_DIR = ROOT / "tests" / "fixtures"


def _load_soup(name: str) -> BeautifulSoup:
    return BeautifulSoup(
        (FIXTURE_DIR / name).read_text(encoding="utf-8"), "lxml"
    )


def test_qb_career_shape_peyton():
    soup = _load_soup("pfr_player_MannPe00.html")
    body = pcs._build_qb_career(soup)
    rows = body["rows"]
    totals = body["totals"]
    # Peyton played 1998-2015, missed 2011 -> 17 NFL seasons rendered.
    assert 16 <= len(rows) <= 18
    # Every row carries the canonical QB keys we render in the table.
    expected = {"year", "team", "games", "pass_cmp", "pass_att",
                "cmp_pct", "pass_yds", "pass_td", "pass_int",
                "rush_att", "rush_yds", "rush_td", "fp"}
    assert expected.issubset(set(rows[0].keys()))
    # Career totals are non-trivial.
    assert totals["pass_yds"] > 50000
    assert totals["pass_td"] > 400
    # Career FP > sum of per-season FP within float tolerance.
    season_fp = round(sum(r["fp"] for r in rows), 1)
    assert abs(season_fp - totals["fp"]) <= 1.0


def test_skill_career_shape_emmitt():
    soup = _load_soup("pfr_player_SmitEm00.html")
    body = pcs._build_skill_career(soup)
    rows = body["rows"]
    totals = body["totals"]
    # Emmitt: 15 NFL seasons.
    assert 14 <= len(rows) <= 16
    expected = {"year", "team", "games", "rush_att", "rush_yds",
                "rush_td", "targets", "rec", "rec_yds", "rec_td", "fp"}
    assert expected.issubset(set(rows[0].keys()))
    # Career rushing yards > 18,000.
    assert totals["rush_yds"] > 18000
    assert totals["rush_td"] > 150


def test_career_stats_html_includes_career_row():
    soup = _load_soup("pfr_player_RiceJe00.html")
    body = pcs._build_skill_career(soup)
    payload = {
        "pfr_id": "RiceJe00", "position": "WR",
        "fp_format": "superflex_ppr",
        "rows": body["rows"], "totals": body["totals"],
    }
    html = pcs.career_stats_html(payload)
    assert "Career" in html and "Stats" in html
    assert "<strong>Career</strong>" in html
    # Year column header is present.
    assert ">Year<" in html
    # WR-shaped columns:
    assert ">Tgt<" in html and ">Rec<" in html


def test_career_stats_html_empty_when_no_rows():
    assert pcs.career_stats_html({"rows": [], "position": "QB",
                                  "totals": {}}) == ""


def test_season_fp_math():
    # 1 game producing 250 pass yds, 2 pass TD, 1 INT, 30 rush yds,
    # 1 rush TD, 5 rec, 50 rec yds, 0 rec TD, 1 fumble.
    fp = pcs._season_fp({
        "pass_yds": 250, "pass_td": 2, "pass_int": 1,
        "rush_yds": 30, "rush_td": 1,
        "rec": 5, "rec_yds": 50, "rec_td": 0,
        "fumbles": 1,
    })
    # 250/25=10 + 2*4=8 + (-2) + 30/10=3 + 6 + 5*1=5 + 50/10=5 + 0 + (-2)
    # = 10 + 8 - 2 + 3 + 6 + 5 + 5 - 2 = 33.0
    assert fp == pytest.approx(33.0)


# ---------------------------------------------------------------------------
# Audit set-overlap math
# ---------------------------------------------------------------------------

def test_audit_set_metrics_basic():
    """Imported lazily so the unit test doesn't require running scripts/."""
    sys.path.insert(0, str(ROOT / "scripts" / "audit"))
    import pfr_similarity_audit as audit

    ours = ["Cam Newton", "Donovan McNabb", "Mike Vick",
            "Peyton Manning", "Brett Favre"]
    theirs = ["Cam Newton", "Mike Vick", "Tim Tebow",
              "Steve Young", "Donovan McNabb"]
    m = audit.set_overlap_metrics(ours, theirs)
    assert m["overlap_at_10"] == 3  # Cam, Mike, Donovan
    assert m["jaccard"] == pytest.approx(3 / 7)
    assert "Tim Tebow" in m["pfr_misses"]
    assert "Steve Young" in m["pfr_misses"]
    assert "Peyton Manning" in m["ours_extras"]


def test_audit_name_normalisation_is_punctuation_safe():
    sys.path.insert(0, str(ROOT / "scripts" / "audit"))
    import pfr_similarity_audit as audit
    # PFR adds "*" for HoF — the parser strips it, but be sure the
    # audit comparison is case + whitespace + period insensitive too.
    ours = ["T.J. Watt", "A.J. Brown"]
    theirs = ["TJ Watt", "AJ Brown"]
    m = audit.set_overlap_metrics(ours, theirs)
    assert m["overlap_at_10"] == 2
    assert m["jaccard"] == pytest.approx(1.0)
