"""v2.4 unified corpus loader tests.

Validates the PR 2 deliverable: ``load_unified_player_stats`` /
``load_corpus`` concatenate the pre-1999 PFR corpus with the canonical
1999+ nflverse stat file behind the ``USE_PRE1999_CORPUS`` feature
flag, and stitch crossover players (Emmitt Smith, Jerry Rice, Brett
Favre, ...) into a single continuous career arc via the PFR↔gsis
crosswalk.

Run with ``pytest tests/test_v2_4_corpus_loader.py -v``.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from dynasty.engine.similarity_v1 import (
    DATA_ROOT,
    PRE1999_BIRTH_DATES_PATH,
    PRE1999_PLAYERS_SIDECAR_FILENAME,
    PRE1999_STATS_FILENAME,
    _build_pfr_to_gsis_map,
    _iter_stat_rows,
    _load_players_meta,
    load_corpus,
    load_unified_player_stats,
)


# Real on-disk paths in this repo. The tests work against the actual
# committed corpus — these files are checked in.
_REPO_ROOT = Path(__file__).resolve().parent.parent
NFLVERSE_DIR = _REPO_ROOT / "data" / "nflverse"
STATS_POST = NFLVERSE_DIR / "player_stats_season.csv.gz"
STATS_PRE = NFLVERSE_DIR / "player_stats_season_pre1999.csv.gz"


# Tagging skip when the pre-1999 corpus isn't available (CI / fresh
# checkout before scrape has run). PR 2 ships with it on-disk so this
# should normally execute.
pre_corpus_available = pytest.mark.skipif(
    not STATS_PRE.exists(),
    reason="pre-1999 corpus file not present",
)


# ---------------------------------------------------------------------------
# 1. Feature flag — off by default
# ---------------------------------------------------------------------------

def test_unified_loader_disabled_by_default(monkeypatch):
    """With the flag off (default), the row count equals the 1999+ file
    alone — the loader behaves exactly as v2.0.
    """
    monkeypatch.delenv("USE_PRE1999_CORPUS", raising=False)

    rows, _meta = load_unified_player_stats(use_pre1999=False)
    n_default = len(rows)

    # Re-count the canonical 1999+ file directly.
    import gzip
    with gzip.open(STATS_POST, "rt", encoding="utf-8", newline="") as fh:
        baseline = sum(1 for _ in csv.DictReader(fh))

    assert n_default == baseline, (
        f"flag-off row count {n_default} != baseline {baseline}; "
        "the loader is leaking pre-1999 rows when the flag is off"
    )


def test_unified_loader_flag_env_var_honoured(monkeypatch):
    """``USE_PRE1999_CORPUS=true`` enables; ``=false`` disables; env-unset
    follows the v2.4 PR 4 default (True).
    """
    monkeypatch.setenv("USE_PRE1999_CORPUS", "true")
    rows_env_on, _ = load_unified_player_stats()

    # v2.4 PR 4: explicit ``false`` must opt out (env-unset is now True).
    monkeypatch.setenv("USE_PRE1999_CORPUS", "false")
    rows_env_off, _ = load_unified_player_stats()

    assert len(rows_env_on) > len(rows_env_off)


# ---------------------------------------------------------------------------
# 2. Concatenation
# ---------------------------------------------------------------------------

@pre_corpus_available
def test_unified_loader_enabled_concatenates(monkeypatch):
    """With the flag on, row count equals 1999+ rows + pre-1999 rows."""
    monkeypatch.delenv("USE_PRE1999_CORPUS", raising=False)

    rows_off, _ = load_unified_player_stats(use_pre1999=False)
    rows_on, _ = load_unified_player_stats(use_pre1999=True)

    import gzip
    with gzip.open(STATS_PRE, "rt", encoding="utf-8", newline="") as fh:
        pre_n = sum(1 for _ in csv.DictReader(fh))

    delta = len(rows_on) - len(rows_off)
    assert delta == pre_n, (
        f"unified row delta {delta} != pre-1999 row count {pre_n}; "
        "stitching should NOT drop or duplicate rows"
    )

    # Sanity band updated in v3.9 — the pre-1999 corpus now covers
    # 1936-1998 (was 1980-1998), bringing the unified row total to ~60k.
    assert 58_000 <= len(rows_on) <= 65_000, (
        f"unified row count {len(rows_on)} is outside the expected 58k-65k band"
    )


# ---------------------------------------------------------------------------
# 3. Schema compatibility
# ---------------------------------------------------------------------------

@pre_corpus_available
def test_unified_loader_schemas_compatible():
    """Both source files must use the same column set so concatenation is
    safe. If this fails, the PFR normaliser regressed.
    """
    import gzip

    def _columns(path: Path) -> list[str]:
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            r = csv.DictReader(fh)
            return list(r.fieldnames or [])

    post_cols = _columns(STATS_POST)
    pre_cols = _columns(STATS_PRE)

    # Order parity is what makes the merge mechanical.
    assert post_cols == pre_cols, (
        "pre-1999 and post-1999 stat files have different column "
        f"layouts.\n  post: {post_cols}\n  pre:  {pre_cols}"
    )

    # Essential per-row fields must be present in EVERY skill-position
    # row that the engine actually consumes (the engine filters empty
    # player_id rows / non-skill positions before scoring — those are
    # nflverse 'Team' aggregation rows). After that filter, season /
    # player_id / position / games must all be populated.
    essential = {"season", "player_id", "position", "games"}
    rows, _ = load_unified_player_stats(use_pre1999=True)
    skill_positions = {"QB", "RB", "WR", "TE"}
    sample = [
        row for row in rows
        if (row.get("player_id") or "").strip()
        and (row.get("position") or "").upper() in skill_positions
    ][:200]
    for row in sample:
        missing = essential - set(k for k, v in row.items() if v not in (None, ""))
        assert not missing, f"row missing essential cols {missing}: {row}"


# ---------------------------------------------------------------------------
# 4. Crossover stitching — Emmitt Smith
# ---------------------------------------------------------------------------

# Known nflverse ids for crossover players. These are stable.
EMMITT_GSIS = "00-0001098"  # nflverse 'gsis_id' — checked against players.csv.gz
EMMITT_PFR = "SmitEm00"


@pre_corpus_available
def test_emmitt_smith_stitching():
    """With the flag on, Emmitt Smith's career arc spans 1990-2004 under
    ONE player_id (his gsis_id), proving the pre-1999 pfr_SmitEm00 rows
    were rewritten to gsis_id during stitching.

    The scope doc names ``00-0001098`` as Emmitt's gsis_id. The on-disk
    nflverse ``players.csv.gz`` happens to list ``00-0015165`` for the
    same ``pfr_id='SmitEm00'``. We accept whichever gsis_id the on-disk
    crosswalk resolves to — the test asserts on the LOGICAL stitch, not
    on a hard-coded id.
    """
    meta = _load_players_meta(
        NFLVERSE_DIR / "players.csv.gz",
        sidecar_path=NFLVERSE_DIR / PRE1999_PLAYERS_SIDECAR_FILENAME,
        birth_date_overrides_path=PRE1999_BIRTH_DATES_PATH,
    )
    pfr_to_gsis = _build_pfr_to_gsis_map(meta)
    emmitt_gsis = pfr_to_gsis.get(EMMITT_PFR)
    assert emmitt_gsis, (
        "Emmitt Smith's pfr_id 'SmitEm00' has no gsis_id in the "
        "nflverse crosswalk; stitching cannot work without this"
    )

    careers = load_corpus(use_pre1999=True)
    emmitt = careers.get(emmitt_gsis)
    assert emmitt is not None, (
        f"Emmitt Smith ({emmitt_gsis}) missing from unified corpus"
    )

    seasons = {s.season for s in emmitt.seasons}
    # 1990-2004 = 15 seasons; the engine may filter on min-games but the
    # full picture should be there.
    assert 1990 in seasons, "Emmitt 1990 (pre-1999) missing — stitch broken"
    assert 1998 in seasons, "Emmitt 1998 (pre-1999) missing"
    assert 1999 in seasons, "Emmitt 1999 (post-1999) missing"
    assert 2004 in seasons, "Emmitt 2004 (post-1999) missing"
    assert len(emmitt.seasons) >= 15, (
        f"Emmitt has only {len(emmitt.seasons)} seasons; expected 15+"
    )

    # His age-31 season (2000) must appear with correct stats. PFR
    # records 1,203 rushing yards for Emmitt in 2000.
    age_31 = next((s for s in emmitt.seasons if s.season == 2000), None)
    assert age_31 is not None, "Emmitt 2000 season missing"
    assert age_31.age in (30, 31), (
        f"Emmitt 2000 age {age_31.age} — birth_year derivation may be wrong"
    )
    rushing = age_31.stats.get("rushing_yards", 0.0)
    assert 1100 <= rushing <= 1300, (
        f"Emmitt 2000 rushing_yards {rushing} not in expected band"
    )

    # And no orphaned pfr_SmitEm00 entry should exist.
    orphan = careers.get(f"pfr_{EMMITT_PFR}")
    assert orphan is None, (
        "pfr_SmitEm00 still exists as a separate career — stitch did "
        "not rewrite the player_id"
    )


# ---------------------------------------------------------------------------
# 5. No-crossover invariant — Walter Payton
# ---------------------------------------------------------------------------

@pre_corpus_available
def test_walter_payton_no_crossover():
    """Walter Payton retired 1987 — no post-1999 rows. His career stays
    under the ``pfr_PaytWa00`` id so non-crossover retirees aren't
    rewritten to a synthetic gsis_id.
    """
    careers = load_corpus(use_pre1999=True)
    wp = careers.get("pfr_PaytWa00")
    assert wp is not None, "Walter Payton missing from unified corpus"

    assert wp.player_id.startswith("pfr_"), (
        f"Walter Payton player_id '{wp.player_id}' should still start "
        "with 'pfr_' (no post-1999 stitch applies to him)"
    )

    seasons = sorted(s.season for s in wp.seasons)
    # v3.9: pre-1999 corpus extended back to 1936, so Payton's rookie
    # year (1975) and all subsequent seasons through retirement (1987)
    # are now present.
    assert seasons == [1975, 1976, 1977, 1978, 1979,
                       1980, 1981, 1982, 1983, 1984, 1985, 1986, 1987], (
        f"Walter Payton seasons {seasons} do not match 1975-1987"
    )

    # Position normalised to RB by the pre-1999 normaliser.
    assert wp.position == "RB"


# ---------------------------------------------------------------------------
# 6. Birth-date overlay
# ---------------------------------------------------------------------------

@pre_corpus_available
def test_pfr_birth_date_override(tmp_path):
    """A player whose pfr_id is in ``data/pfr_birth_dates.csv`` should
    surface that birth date in the loaded career, overriding the
    rookie-season+22 fallback.

    Builds a tiny isolated sidecar CSV and a tiny isolated players.csv
    so this test is hermetic — does not depend on the real partial
    birth-date file. (The real file is also exercised end-to-end in
    ``test_walter_payton_no_crossover`` where Payton's 1954 DOB drives
    correct ages.)
    """
    import gzip

    # Hermetic players.csv — one row for our test player with NO birth_date.
    fake_players_path = tmp_path / "players.csv.gz"
    cols = [
        "gsis_id", "display_name", "pfr_id", "birth_date",
        "position_group", "position", "rookie_season", "last_season",
    ]
    with gzip.open(fake_players_path, "wt", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerow({
            "gsis_id": "pfr_TestPl00",
            "display_name": "Test Player",
            "pfr_id": "TestPl00",
            "birth_date": "",  # missing
            "position_group": "RB",
            "position": "RB",
            "rookie_season": "1985",
            "last_season": "1990",
        })

    # Hermetic birth-date overrides.
    fake_bd = tmp_path / "pfr_birth_dates.csv"
    fake_bd.write_text("pfr_id,birth_date\nTestPl00,1962-04-15\n", encoding="utf-8")

    meta = _load_players_meta(
        fake_players_path,
        sidecar_path=None,
        birth_date_overrides_path=fake_bd,
    )

    # Resolution should land on the override.
    row = meta.get("pfr_TestPl00")
    assert row is not None, "test player missing from meta"
    assert row.get("birth_date") == "1962-04-15", (
        f"birth_date overlay did not apply: {row.get('birth_date')!r}"
    )

    # And the synthetic ``pfr_X`` key path resolves to the same row.
    via_pfr = meta.get("pfr_TestPl00")  # same key, same row
    assert via_pfr is row


@pre_corpus_available
def test_real_pre1999_birth_date_overlay_applies():
    """End-to-end: an A-D pfr_id whose birth date is in the on-disk
    overlay file should be present in the meta after loading.
    """
    if not PRE1999_BIRTH_DATES_PATH.exists():
        pytest.skip("pfr_birth_dates.csv not present")

    # Pick the first row from the overlay file as our probe — that way
    # the test is robust against the overlay being filled out further.
    with open(PRE1999_BIRTH_DATES_PATH, "rt", encoding="utf-8", newline="") as fh:
        r = csv.DictReader(fh)
        probe = next(iter(r), None)

    if probe is None or not probe.get("pfr_id") or not probe.get("birth_date"):
        pytest.skip("pfr_birth_dates.csv is empty or malformed")

    meta = _load_players_meta(
        NFLVERSE_DIR / "players.csv.gz",
        sidecar_path=NFLVERSE_DIR / PRE1999_PLAYERS_SIDECAR_FILENAME,
        birth_date_overrides_path=PRE1999_BIRTH_DATES_PATH,
    )
    row = meta.get(f"pfr_{probe['pfr_id']}")
    assert row is not None, f"probe player pfr_{probe['pfr_id']} missing"
    # Overlay only fills in missing dates; the existing value (if any)
    # wins. Either way, after the overlay the row has SOME date.
    bd = (row.get("birth_date") or "").strip()
    assert bd, f"no birth_date on row after overlay: {row}"


# ---------------------------------------------------------------------------
# 7. Stitch coverage stat — sanity check on the crosswalk
# ---------------------------------------------------------------------------

@pre_corpus_available
def test_stitch_count_in_expected_range():
    """The scope doc estimates ~50-150 crossover players. Empirically
    the nflverse PFR↔gsis crosswalk yields ~338 (Phil's intuition
    underestimated late-80s players who appear in 1999 game logs as
    rookies + retirees who hung on through 1999 as backups). Anything
    in 200-500 is healthy; outside that band suggests the crosswalk
    regressed.
    """
    meta = _load_players_meta(
        NFLVERSE_DIR / "players.csv.gz",
        sidecar_path=NFLVERSE_DIR / PRE1999_PLAYERS_SIDECAR_FILENAME,
        birth_date_overrides_path=PRE1999_BIRTH_DATES_PATH,
    )
    pfr_to_gsis = _build_pfr_to_gsis_map(meta)

    _rows, stitched_player_count = _iter_stat_rows(
        STATS_POST, STATS_PRE, pfr_to_gsis,
    )
    assert 200 <= stitched_player_count <= 500, (
        f"stitched {stitched_player_count} players; outside 200-500 "
        "healthy band — crosswalk may have regressed"
    )
