"""Render prospects.html and assert on the strings a visitor sees.

The companion to ``tests/test_prospect_view.py``, which asserts the pure
logic. This one renders the real page through ``report._build_prospects``
and checks the output, because two of Phil's three complaints were about
text that only exists after interpolation:

* ``Arch Manning ? RNone`` came from an f-string, not from a function with
  a return value anyone could assert on.
* "the top three all show identical 3200" is a property of the rendered
  table, not of any single record.

Run as a subprocess, never imported: it stubs missing third-party modules
into ``sys.modules``. Same containment note as ``render_chrome.py``.

    python tests/support/render_prospects.py

Exit 0 when the page is correct, 1 with a report otherwise.
"""
from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests" / "support"))

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "prospects_fixture.json"

FAILURES: list[str] = []
CHECKS = 0


def ok(cond, label: str, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILURES.append(f"{label}{f'  [{detail[:300]}]' if detail else ''}")


def strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def table_text(html: str) -> str:
    """Only the rendered table, with markup removed.

    Scoped deliberately. The whole page also carries the shared highlights
    JS, which legitimately contains the word ``None`` in a sentence
    ("None of these players have indexed clips") and ``?`` in ternary
    operators. Asserting over the raw page would either fail on that prose
    or force a weaker assertion; the table is where a formatting leak
    would actually reach a reader.
    """
    m = re.search(r"<tbody>(.*?)</tbody>", html, re.S)
    return strip_tags(m.group(1)) if m else ""


def rows_of(html: str) -> list[str]:
    return re.findall(r'<tr class="player-row prospect-row.*?</tr>', html, re.S)


def cells(row: str) -> list[str]:
    return [strip_tags(c).strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]


def render(artifact: dict) -> str:
    from dynasty import report
    path = Path(tempfile.mktemp(suffix=".json"))
    path.write_text(json.dumps(artifact), encoding="utf-8")
    try:
        return report._build_prospects(
            datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
            "Superflex PPR", prospects_path=path,
        )
    finally:
        path.unlink(missing_ok=True)


def realistic_artifact(fixture: dict) -> dict:
    """The fixture, restamped to look like a post-v3.3 artifact.

    The committed fixture predates draft stamping: not one record has a
    ``drafted`` block, so it exercises only the legacy path. The live
    artifact has a drafted class plus a Tankathon future class, which is
    the shape both bugs needed. Built here rather than committed as a
    second fixture so it stays tied to the real one's field set.
    """
    art = copy.deepcopy(fixture)
    for i, p in enumerate(art["prospects"]):
        if p.get("draft_class") == 2025:
            p["draft_class"] = 2026
            p["drafted"] = {"year": 2026, "round": 1 + (i % 3),
                            "pick": 4 + i * 7, "team": "TEN",
                            "college": p.get("school")}
            p.setdefault("projection", {})["projection_source"] = "comp_weighted"
        else:
            # Exactly what _index_tankathon_by_class emits.
            p["draft_class"] = 2027
            p["drafted"] = {"year": 2027, "rnd": None, "round": None,
                            "pick": 2 + i, "team": None,
                            "source": "tankathon_big_board"}
            proj = p.setdefault("projection", {})
            proj["projection_source"] = "insufficient_evidence_undrafted"
            proj["projected_career_fp"] = None
            proj["projected_peak3_fp_pg"] = None
    art["draft_classes"] = [2026, 2027]
    return art


def saturated_artifact(fixture: dict) -> dict:
    """A drafted class whose QBs all land on the same tier constant."""
    art = copy.deepcopy(fixture)
    art["prospects"] = []
    for i in range(4):
        art["prospects"].append({
            "name": f"Tier QB {i}", "position": "QB", "draft_class": 2026,
            "school": "State", "age": 21.0,
            "cfb_player_id": f"90000{i}", "slug": f"tier-qb-{i}",
            "drafted": {"year": 2026, "round": 1, "pick": 1 + i,
                        "team": "LVR"},
            "projection": {
                "projected_career_fp": 3200.0,
                "projected_peak3_fp_pg": 13.5,
                "projection_source": "pick_tier_baseline_R1_top10",
                "n_comps_with_nfl": 0,
            },
            "comps": [],
        })
    art["draft_classes"] = [2026]
    return art


def main() -> int:
    import audit_site_links as harness
    harness._install_stubs()

    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    # ---------------------------------------------------------- leaks
    for name, art in (
        ("legacy artifact (no draft records)", fixture),
        ("realistic artifact (drafted + future class)",
         realistic_artifact(fixture)),
        ("saturated artifact", saturated_artifact(fixture)),
    ):
        html = render(art)
        text = table_text(html)
        ok(text.strip() != "", f"{name}: the table rendered rows", html[:200])
        for leak in ("RNone", "None", "R None", "#None"):
            ok(leak not in text,
               f"{name}: rendered table is free of {leak!r}", text)
        ok("?" not in text,
           f"{name}: rendered table has no literal '?' placeholder", text)
        # Nothing in a row may stringify a missing value.
        for row in rows_of(html):
            for cell in cells(row):
                ok("None" not in cell,
                   f"{name}: a cell stringified None", cell)

    # ------------------------------------------- class-scoped default view
    html = render(realistic_artifact(fixture))
    ok('id="board-heading"' in html,
       "the page states which board is being viewed")
    heading = re.search(r'id="board-heading"[^>]*>(.*?)</div>', html, re.S)
    heading_text = strip_tags(heading.group(1)) if heading else ""
    ok("2026" in heading_text,
       "and names the current rookie class by default", heading_text)
    ok('class-chip active" data-class="2026"' in html,
       "the 2026 chip is the active one, not 'All classes'",
       html[html.find('id="class-chips"'):][:400])
    ok("have not been drafted" in html,
       "future classes are called out as undrafted rather than ranked "
       "alongside drafted players")
    ok('data-class-kind="future"' in html,
       "and the chip carries which kind of class it is")

    # Rows are grouped, with the current rookie class leading and the
    # undrafted class last -- NOT simply newest-number-first, which would
    # put 2027 above 2026. This is the document order, so it is also what a
    # visitor sees if the filter JS never runs.
    order = [c[3] for c in (cells(r) for r in rows_of(html)) if len(c) > 3]
    ok(order and order[0] == "2026",
       "the document starts with the current rookie class", str(order[:6]))
    ok(order and order[-1] == "2027",
       "and the undrafted class is last, never interleaved",
       str(order[-6:]))
    seen: list[str] = []
    for value in order:
        if not seen or seen[-1] != value:
            seen.append(value)
    ok(len(seen) == len(set(seen)),
       "each class appears as one contiguous block", str(seen))

    # The reported symptom: a 2024 player at the top of the prospect board.
    penix = copy.deepcopy(realistic_artifact(fixture))
    penix["prospects"].append({
        "name": "Michael Penix", "position": "QB", "draft_class": 2024,
        "school": "Washington", "age": 24.0, "cfb_player_id": "111",
        "slug": "michael-penix", "comps": [],
        "drafted": {"year": 2024, "round": 1, "pick": 8, "team": "ATL"},
        "projection": {"projected_career_fp": 3200.0,
                       "projected_peak3_fp_pg": 13.5,
                       "projection_source": "pick_tier_baseline_R1_top10",
                       "n_comps_with_nfl": 0},
    })
    penix["draft_classes"] = [2024, 2026, 2027]
    phtml = render(penix)
    first_row = rows_of(phtml)[0]
    ok("Michael Penix" not in first_row,
       "a 2024 player no longer tops the board", strip_tags(first_row))
    ok('data-class="2026"' in first_row,
       "the first row belongs to the current rookie class",
       strip_tags(first_row))

    # ------------------------------------------------ saturation is labelled
    shtml = render(saturated_artifact(fixture))
    stext = table_text(shtml)
    ok("draft-slot constant" in stext,
       "a tier-constant projection is labelled as one, so 3200 cannot read "
       "as a measurement", stext)
    ok("basis-constant" in shtml,
       "and carries a distinguishing class for styling")
    ok(re.search(r"no comp in their pool", shtml) is not None,
       "the page explains how many rows rest on a constant", shtml[:400])
    # Draft order breaks the tie between identical constants.
    names = [cells(r)[1] for r in rows_of(shtml)]
    ok(names == sorted(names),
       "identical constants are ordered by draft position, not arbitrarily",
       str(names))

    # An evidence-backed row must NOT be labelled.
    ehtml = render(realistic_artifact(fixture))
    drafted_rows = [r for r in rows_of(ehtml) if 'data-basis="comps"' in r]
    ok(bool(drafted_rows), "comp-backed rows exist in the realistic artifact")
    for row in drafted_rows:
        ok("basis-constant" not in row,
           "a comp-backed projection is not flagged as a constant",
           strip_tags(row))

    # ------------------------------------------------- undrafted has no number
    uhtml = render(realistic_artifact(fixture))
    future_rows = [r for r in rows_of(uhtml) if 'data-class="2027"' in r]
    ok(bool(future_rows), "the future class rendered")
    for row in future_rows:
        c = cells(row)
        ok(c[6].strip().startswith("\u2014") or c[6].strip() == "\u2014",
           "an undrafted prospect with no NFL-career comps shows a dash, "
           "not an invented projection", str(c))
        ok("board #" in strip_tags(row),
           "and is badged with its big-board rank rather than a fake pick",
           strip_tags(row))

    if FAILURES:
        print(f"{len(FAILURES)} of {CHECKS} prospects-page checks FAILED:")
        for line in FAILURES:
            print(f"  FAIL {line}")
        return 1
    print(f"All {CHECKS} prospects-page checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
