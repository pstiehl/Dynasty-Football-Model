"""managerscore.html — rate dynasty managers on drafting, trading and waivers.

The concept
-----------
Pull every draft pick, trade and waiver add out of a Sleeper league, price
each asset with KeepTradeCut consensus value, and turn "did this manager
acquire value or leak it?" into one number per manager with an auditable
trail underneath.

What this module owns, and what it does not
-------------------------------------------
This module builds the *value artifact* and the *page*. It does not score
anything. Scoring happens once, in JavaScript, in
``dynasty.managerscore_js.MANAGERSCORE_CORE_JS``, because league data is
read live from Sleeper in the browser — the same arrangement My Team and
the Roster Reel use, and for the same reason: this site is a static GitHub
Pages build with no server to proxy an API through. Keeping the maths in
one place means there is no Python reimplementation to drift; the test
suite runs that JS in ``node`` instead of re-deriving it.

The honest part: there is no KTC price history
----------------------------------------------
The owner's brief asks for KTC values *as of the date of each transaction*.
That is the right way to measure this, and it is not currently possible.
Established by reading the repo rather than assuming:

* ``scripts/refresh_ktc_consensus.py`` calls ``save_snapshot(dated=True)``,
  which writes ``data/consensus/ktc_YYYY-MM-DD.json``.
* ``.gitignore`` excludes exactly that pattern
  (``data/consensus/ktc_[0-9]*-[0-9]*-[0-9]*.json``).
* ``.github/workflows/daily-refresh.yml`` runs with
  ``permissions: contents: read`` and has no commit step.

So the dated snapshot is written on every CI run and deleted with the
runner every time. ``data/consensus/`` holds ``ktc_latest.json`` and
``dp_playerids.csv`` and nothing else. **No dated KTC values exist, on disk
or anywhere else we can reach, and none can be reconstructed** — KTC
publishes a live board, not an archive, and Pro Football Reference (the
other historical source in this repo) is 403 to CI by deliberate access
control and is not to be scraped.

Two consequences, both deliberate:

1. Transactions are priced at *current* value and the page says so in a
   banner, not a code comment. That measures how a transaction **turned
   out**, which is a real and interesting thing, but it is emphatically not
   a measure of whether the manager was right at the time. The page says
   that too.
2. ``dynasty.ktc_history`` starts accumulating a ~6 KB dated value file per
   day so this stops being true going forward. The valuation path is
   already point-in-time-capable and labels each asset ``as-of <date>`` or
   ``current`` individually, so the page upgrades itself day by day as
   history accrues, with no further code change. It cannot be backfilled.

Value basis
-----------
KTC superflex consensus value. Assets outside KTC's published top 500 are
priced at the *board floor* (the lowest published value that day, 491 in
the snapshot this was written against) rather than zero: the 500th-ranked
dynasty asset is cheap, not free, and pricing it at zero would hand a fake
windfall to whoever acquired it.

Draft picks traded as picks (not yet made) are priced from KTC's own
rookie-pick rows. KTC publishes those as "2027 Early/Mid/Late 1st"; a
Sleeper trade knows only season and round, because which slot a future pick
lands on depends on standings that have not happened. "Mid" is therefore
the honest expectation, and the page labels it.
"""
from __future__ import annotations

import csv
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import ktc_history

VALUES_ARTIFACT = "managerscore_values.json"
SERIES_ARTIFACT = "managerscore_series.json"
HISTORY_SUBDIR = "ktc_history"
VALUES_SCHEMA = "managerscore.values.v1"

CONSENSUS_DIR = Path("data/consensus")
KTC_LATEST = "ktc_latest.json"
CROSSWALK = "dp_playerids.csv"


# ---------------------------------------------------------------------------
# Crosswalk
# ---------------------------------------------------------------------------

def load_ktc_to_sleeper(
    crosswalk_path: Path,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Read the dynastyprocess id crosswalk.

    Returns ``(ktc_id -> sleeper_id, mfl_id -> sleeper_id)``. The mfl map is
    the fallback: KTC's own payload carries ``mflid`` for most rows, so a
    player whose ``ktc_id`` is missing from the crosswalk can often still be
    bridged. Missing or malformed file yields empty maps — the caller
    degrades rather than raising.
    """
    by_ktc: Dict[str, str] = {}
    by_mfl: Dict[str, str] = {}
    path = Path(crosswalk_path)
    if not path.exists():
        return by_ktc, by_mfl
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames or "sleeper_id" not in reader.fieldnames:
                return by_ktc, by_mfl
            for row in reader:
                sleeper = (row.get("sleeper_id") or "").strip()
                if not sleeper:
                    continue
                sleeper = sleeper.split(".")[0]
                ktc = (row.get("ktc_id") or "").strip().split(".")[0]
                mfl = (row.get("mfl_id") or "").strip().split(".")[0]
                if ktc:
                    by_ktc.setdefault(ktc, sleeper)
                if mfl:
                    by_mfl.setdefault(mfl, sleeper)
    except (OSError, csv.Error, UnicodeDecodeError):
        return {}, {}
    return by_ktc, by_mfl


# ---------------------------------------------------------------------------
# Value artifact
# ---------------------------------------------------------------------------

def build_values_artifact(
    *,
    consensus_dir: Path = CONSENSUS_DIR,
    history_dir: Optional[Path] = None,
    generated_at: Optional[datetime] = None,
) -> Dict:
    """Build the sleeper_id -> KTC value artifact the page consumes.

    Never raises on missing inputs. Returns a payload whose ``available``
    flag is ``False`` and whose ``notes`` explain which input was missing,
    so the page can say something true instead of rendering an empty table.
    """
    consensus_dir = Path(consensus_dir)
    history_dir = Path(history_dir) if history_dir is not None \
        else consensus_dir / "history"
    generated_at = generated_at or datetime.utcnow()

    notes: List[str] = []
    snapshot: Optional[Dict] = None
    latest_path = consensus_dir / KTC_LATEST
    if latest_path.exists():
        try:
            snapshot = json.loads(latest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            notes.append(f"{KTC_LATEST} could not be parsed: {exc}")
    else:
        notes.append(f"{KTC_LATEST} not found in {consensus_dir}")

    by_ktc, by_mfl = load_ktc_to_sleeper(consensus_dir / CROSSWALK)
    if not by_ktc and not by_mfl:
        notes.append(
            f"{CROSSWALK} missing or unusable — KTC rows cannot be joined to "
            "Sleeper player ids"
        )

    by_sleeper: Dict[str, list] = {}
    picks: Dict[str, int] = {}
    floor: Optional[int] = None
    n_players = 0
    captured_at = None

    if snapshot:
        compact = ktc_history.compact_values(snapshot)
        picks = compact.get("picks") or {}
        floor = compact.get("floor")
        captured_at = compact.get("captured_at")

        for row in snapshot.get("players") or []:
            if not isinstance(row, dict):
                continue
            position = (row.get("position") or "").strip().upper()
            if position == ktc_history.PICK_POSITION:
                continue
            n_players += 1
            value = (row.get("superflex") or {}).get("value")
            if value is None:
                continue
            ktc_id = row.get("ktc_id")
            mfl_id = row.get("mfl_id")
            sleeper = None
            if ktc_id is not None:
                sleeper = by_ktc.get(str(ktc_id))
            if not sleeper and mfl_id:
                sleeper = by_mfl.get(str(mfl_id))
            if not sleeper:
                continue
            # [value, name, position, ktc_id] — ktc_id is what the dated
            # history files are keyed by, so the page needs it to look a
            # past value up.
            by_sleeper[str(sleeper)] = [
                int(value),
                row.get("name") or "",
                position,
                int(ktc_id) if ktc_id is not None else None,
            ]

    dates = ktc_history.available_history_dates(history_dir)
    if not dates:
        notes.append(
            "no dated KTC value history retained yet — every transaction is "
            "priced at current value; history begins accumulating on the next "
            "daily run with this code deployed and cannot be backfilled"
        )

    available = bool(by_sleeper)
    if snapshot and not by_sleeper:
        notes.append(
            "KTC snapshot parsed but no row could be joined to a Sleeper id"
        )

    return {
        "schema": VALUES_SCHEMA,
        "generated_at": generated_at.isoformat(),
        "available": available,
        "basis": "point-in-time where history exists, otherwise current",
        "ktc": {
            "captured_at": captured_at,
            "n_players": n_players,
            "n_mapped": len(by_sleeper),
            "floor": floor,
            "format": "superflex",
        },
        "crosswalk": {
            "available": bool(by_ktc or by_mfl),
            "n_ktc_ids": len(by_ktc),
            "n_mapped": len(by_sleeper),
        },
        "history": {
            "available": bool(dates),
            "count": len(dates),
            "dates": dates,
            "earliest": dates[0] if dates else None,
            "latest": dates[-1] if dates else None,
            "dir": HISTORY_SUBDIR,
            "file_template": ktc_history.HISTORY_PREFIX + "{date}.json",
        },
        "fields": ["sf_value", "name", "position", "ktc_id"],
        "by_sleeper": by_sleeper,
        "picks": picks,
        "notes": notes,
    }


def write_values_artifact(
    out_root: Path,
    *,
    consensus_dir: Path = CONSENSUS_DIR,
    history_dir: Optional[Path] = None,
    generated_at: Optional[datetime] = None,
) -> Dict:
    """Write the value artifact and publish any retained history alongside it.

    The dated history files are copied into the site so the browser can
    fetch one day at a time rather than downloading a growing archive on
    every page load.
    """
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    payload = build_values_artifact(
        consensus_dir=consensus_dir,
        history_dir=history_dir,
        generated_at=generated_at,
    )
    (out_root / VALUES_ARTIFACT).write_text(
        json.dumps(payload, separators=(",", ":")), encoding="utf-8"
    )

    src_history = Path(history_dir) if history_dir is not None \
        else Path(consensus_dir) / "history"

    # The page needs the whole series per asset (value on the transaction
    # date AND the peak over every later date), so ship one combined file
    # rather than making the browser fetch every dated board.
    series = ktc_history.build_series(src_history)
    (out_root / SERIES_ARTIFACT).write_text(
        json.dumps(series, separators=(",", ":")), encoding="utf-8"
    )

    if payload["history"]["available"] and src_history.is_dir():
        dest = out_root / HISTORY_SUBDIR
        dest.mkdir(parents=True, exist_ok=True)
        for day in payload["history"]["dates"]:
            src = ktc_history.history_point_path(day, src_history)
            if src.exists():
                shutil.copyfile(src, dest / src.name)
    return payload


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

_MANAGERSCORE_CSS = """
.ms-sub { font-size: 12px; color: var(--muted); margin-top: 6px; }
.ms-n { font-size: 11px; color: var(--muted); margin-left: 6px;
  font-variant-numeric: tabular-nums; }
.ms-pos { color: #047857; font-weight: 600; }
.ms-neg { color: #b91c1c; font-weight: 600; }
.ms-basis { font-size: 11px; color: var(--muted); }
.ms-flags { font-size: 11px; color: #92400e; max-width: 260px; }
tr.ms-row:hover { background: var(--hover); cursor: pointer; }
.ms-input { display: flex; gap: 10px; flex-wrap: wrap; align-items: center;
  margin: 10px 0; }
.ms-input input { font: inherit; padding: 8px 12px; border: 1px solid var(--border);
  border-radius: 6px; min-width: 240px; }
.btn { font: inherit; padding: 8px 16px; border: 0; border-radius: 6px;
  background: var(--accent); color: white; font-weight: 600; cursor: pointer; }
.btn:hover { background: var(--accent-dark); }
.ms-league-btn { display: block; margin: 6px 0; text-align: left; width: 100%; }
.ms-trade { border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 14px; margin: 8px 0; background: var(--card); }
.ms-trade-unscored { opacity: 0.65; border-style: dashed; }
.ms-trade-head { font-size: 12px; color: var(--muted); margin-bottom: 6px; }
.ms-trade-body { font-size: 13px; line-height: 1.7; }
.ms-got { display: inline-block; min-width: 42px; font-size: 11px;
  font-weight: 700; color: #047857; text-transform: uppercase; }
.ms-gave { display: inline-block; min-width: 42px; font-size: 11px;
  font-weight: 700; color: #b91c1c; text-transform: uppercase; }
.ms-formula { background: #f8fafc; border: 1px solid var(--border);
  border-radius: 8px; padding: 14px 18px; font-size: 13px; }
.ms-formula code { background: #eef2ff; padding: 1px 5px; border-radius: 4px; }
h4 { font-size: 14px; margin: 18px 0 8px 0; }
"""


def _methodology_html() -> str:
    """Plain-language description of the metric, rendered on the page.

    Deliberately on the page rather than in the docs: a score nobody can
    interrogate is a score nobody should trust, and the limitations matter
    as much as the formula.
    """
    return """
<h2>What <span class="accent">Manager Score</span> measures</h2>

<div class="ms-formula">
<p style="margin-top:0"><strong>One number per manager, league mean 100, one
standard deviation 15.</strong> 115 means "one standard deviation better than
the rest of this league". It is a ranking <em>within</em> your league, not an
absolute rating &mdash; the average manager in every league scores about 100.</p>

<p><code>Manager Score = 100 + 15 &times; (0.50&middot;z<sub>draft</sub> +
0.35&middot;z<sub>trade</sub> + 0.15&middot;z<sub>waiver</sub>)</code></p>

<p>Weights are renormalised over whichever components your league actually
has, so a league that never trades is not scored on a component nobody
played.</p>

<h4>The unit: value capture</h4>
<p>Everything below is built from one measurement. For any asset that
changes hands on a date, we take its KeepTradeCut value <strong>on that
date</strong> and the highest value it reached <strong>afterwards</strong>.
The difference is what was <em>captured</em>:</p>
<p><code>captured = peak value after the transaction &minus; value on the
day of the transaction</code></p>
<p>Acquiring an asset captures that gap; giving one up forfeits it. So
acquiring a player just before he breaks out scores well, and trading away a
player who then breaks out scores badly &mdash; which is the thing everyone
actually argues about in a dynasty league.</p>

<h4>Draft skill &mdash; did the pick beat its slot?</h4>
<p>Surplus = what the pick captured, minus what a pick at that slot
typically captured <em>in that same draft</em>. The par curve is fitted from
the draft's own picks (median per six-slot bin, forced never to rise as
slots get later), so the average pick in a room scores zero by construction.
That is what makes startup drafts, rookie drafts, 10-team and 14-team
leagues comparable without any hand-tuned constant. A draft with fewer than
12 evaluable picks is skipped rather than scored badly.</p>

<h4>Trade skill &mdash; did value come in or leak out?</h4>
<p>Net = captured by what you received, minus captured by what you gave,
counting players and draft picks on both sides. Within any trade these sum
to exactly zero, so it is a genuine transfer measure. A trade containing an
asset we cannot price is <strong>reported but not scored</strong> &mdash;
pricing one side and not the other would invent a steal that never
happened.</p>

<h4>Waiver skill &mdash; was there value on the wire?</h4>
<p>Captured by each waiver or free-agent add. A replacement-level body that
never rose captures zero; plucking a player who then becomes a starter
captures a lot. FAAB spent is shown in the audit but not scored: KTC points
and FAAB dollars have no exchange rate, and inventing one would be the least
defensible number on the page.</p>

<h4>Why volume cannot buy a good score</h4>
<p>Each component is a <strong>per-transaction mean</strong>, not a total,
then shrunk toward zero by <code>n / (n + k)</code> (k = 6 picks, 3 trades,
5 adds). Shrinkage only ever moves a manager <em>toward</em> average &mdash;
it never flips a sign and never overshoots &mdash; so one lucky pick cannot
top the table, and making 40 mediocre trades cannot either. A manager with
no activity in a component is scored as league-average for it, flagged, and
not penalised for abstaining.</p>
</div>

<h2>Where the <span class="accent">values</span> come from</h2>
<div class="ms-formula">
<p style="margin-top:0">KeepTradeCut publishes a <em>live</em> superflex
consensus board, not an archive, so historical values are not available from
KTC directly. The dated boards behind this page were recovered from public
web-archive captures of that page and are stored in this repository, one
small file per date. <strong>Nothing here is interpolated, modelled or
invented</strong> &mdash; every number was published by KTC on the date it
is filed under.</p>
<p>The archive is <strong>real but sparse</strong>: a few dozen dated boards
rather than a daily series, with gaps of weeks to months. Consequences,
stated rather than hidden:</p>
<ul>
<li>A transaction is priced at the nearest board <strong>on or before</strong>
its date &mdash; never after, because a price that did not exist yet is not a
point-in-time price. The date actually used is shown on every row.</li>
<li>A "peak" is the highest value we <strong>observed</strong>. The true peak
may fall in a gap, so captures are best read as a floor on what was really
available.</li>
<li>Transactions with no recorded board after them are <strong>not scored at
all</strong> and are counted separately. You cannot grade foresight on a
trade made last week.</li>
</ul>
<p>Going forward the daily job files a new dated board on every run, so the
series densifies from here even though the past cannot be filled in.</p>
</div>

<h2>What this <span class="accent">cannot</span> tell you</h2>
<div class="ms-formula">
<ul style="margin:0">
<li><strong>Hindsight is baked in, by design.</strong> This measures what
the assets went on to do. A defensible process that ran into an injury
scores badly here, and a reckless punt that hit scores well. It is a
measure of results, not of reasoning.</li>
<li><strong>KeepTradeCut is a crowd, not an oracle.</strong> Where the
consensus was wrong, this page is wrong the same way.</li>
<li><strong>Only players on the board are priced.</strong> KTC publishes a
top 500; assets that never appear cannot be scored and are counted as
unpriceable rather than guessed at.</li>
<li><strong>Future picks are priced at the "Mid" tier.</strong> A traded
2027 1st could land anywhere; KTC prices Early/Mid/Late separately and we
take Mid, because the final slot depends on standings that have not
happened.</li>
<li><strong>Roster management is not measured.</strong> Lineup decisions,
injury stashes, taxi squads and contending-vs-rebuilding timing are all
invisible here.</li>
<li><strong>Sleeper only.</strong> The live reads target Sleeper's public
API. MFL leagues are pre-baked elsewhere in this project and are not
covered by this page.</li>
</ul>
</div>
"""


def build_manager_score(latest_ts: datetime, league_label: str) -> str:
    """Render managerscore.html. Imported lazily by report.generate_site."""
    from .report import _page, _site_header  # local import: avoids a cycle
    from .managerscore_js import MANAGERSCORE_CORE_JS, MANAGERSCORE_UI_JS

    body = """<div class="container">

<h2>Manager <span class="accent">Score</span></h2>
<p class="lede">Who actually drafts and trades well in your league? This
prices every draft pick, trade and waiver add in a Sleeper league against
KeepTradeCut consensus value, and ranks the managers on what they acquired
versus what it cost them. Leagues are read live from Sleeper's public API in
your browser — nothing is sent to this site.</p>

<div id="ms-artifact-note" style="display:none"></div>
<div id="ms-basis" style="display:none"></div>

<div class="ms-input">
  <input id="ms-username" type="text" placeholder="Sleeper username" autocomplete="off">
  <button class="btn" id="ms-load-user">Find my leagues</button>
</div>
<div class="ms-input">
  <input id="ms-leagueid" type="text" placeholder="…or a Sleeper league ID" autocomplete="off">
  <button class="btn" id="ms-load-league">Score this league</button>
</div>
<div class="ms-input">
  <label><input type="checkbox" id="ms-history" checked> include previous
  seasons (follows the league's history chain)</label>
</div>

<div id="ms-status" class="callout" style="display:none"></div>
<div id="ms-league-list"></div>

<div id="ms-results" style="display:none">
  <h3 id="ms-league-label"></h3>
  <div id="ms-drafts" style="display:none"></div>
  <div id="ms-summary"></div>
  <div id="ms-table"></div>
  <div id="ms-audit" style="display:none"></div>
</div>

__METHODOLOGY__

</div>
<style>__MANAGERSCORE_CSS__</style>
<script>__MANAGERSCORE_CORE_JS__</script>
<script>__MANAGERSCORE_UI_JS__</script>
"""
    body = (
        body.replace("__METHODOLOGY__", _methodology_html())
        .replace("__MANAGERSCORE_CSS__", _MANAGERSCORE_CSS)
        .replace("__MANAGERSCORE_CORE_JS__", MANAGERSCORE_CORE_JS)
        .replace("__MANAGERSCORE_UI_JS__", MANAGERSCORE_UI_JS)
    )

    return _page(
        "Kings of Dynasty — Manager Score",
        _site_header("managerscore", latest_ts, league_label),
        body,
    )
