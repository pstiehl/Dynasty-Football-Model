# Box Score Dynasty — Fantasy Football

> **Production over noise.** Dynasty rankings built on box scores, not narratives.

*(Brand history: previously published as "Kings of Dynasty" in v2.x / v3.x;
rebranded to **Box Score Dynasty** at v3.14, 2026-06-03.)*

A dynasty fantasy football ranking model built on a fantasy-point-arc
similarity engine (v2.0/v2.1) plus a survival / confidence / late-
breakout penalty stack (v2.2). Active players are comped to historical
players whose fp/G curves match under modern scoring, their projected
remaining career fantasy points are computed, then three multiplicative
penalties discount bust-prone comp pools, small-sample players, and
late-breakout QBs into the rank tier their realistic dynasty value
warrants.

Output is a self-contained static site (**Box Score Dynasty**) you can open
in any browser — a `Similarity Scores` ranking page, a `Dynasty Rankings`
page with format presets (Superflex PPR and 2QB PPR), a Methodology page,
and per-player comparable pages.

See [docs/V2-METHODOLOGY.md](docs/V2-METHODOLOGY.md) for the full
pipeline,
[docs/SURVIVAL-PENALTY.md](docs/SURVIVAL-PENALTY.md),
[docs/CONFIDENCE-SHRINKAGE.md](docs/CONFIDENCE-SHRINKAGE.md), and
[docs/LATE-BREAKOUT-QBs.md](docs/LATE-BREAKOUT-QBs.md) for the v2.2
penalty mechanics.

---

## Quick start (non-technical users)

1. Install Python 3.12 — https://www.python.org/downloads/ (on Windows,
   check "Add Python to PATH" during install).
2. Double-click the script for your OS:
   - macOS → `RUN_ME_MAC.command`
   - Windows → `RUN_ME_WINDOWS.bat`
3. The script sets up everything on first run (~1 min), syncs sources,
   computes the model, and opens the rankings page in your browser.

To refresh later, just double-click the script again (~10s).

See `READ_ME_FIRST.txt` for security-warning workarounds (macOS Gatekeeper,
Windows SmartScreen) and troubleshooting.

---

## Quick start (developers)

```bash
git clone https://github.com/pstiehl/Dynasty-Football-Model
cd Dynasty-Football-Model
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e .

# One-time DB setup + canonical player map
python -m dynasty.cli init-db
python -m dynasty.cli sync-players

# Pull rankings from every source and compute the composite
python -m dynasty.cli sync-all
python -m dynasty.cli score --league-format sf_ppr
python -m dynasty.cli top --n 30 --league-format sf_ppr
```

Generate the static HTML report:

```bash
python -m dynasty.launcher_headless        # writes dynasty_rankings.html
```

---

## Methodology

The model is a **weighted composite** of independent ranking sources. Each
source's weight is multiplied by a track-record factor derived from backtests
against real NFL fantasy production.

### 1. Pull from many sources

Each source is an adapter (`src/dynasty/sources/*.py`) that returns
normalized `RankingRecord`s. Sources are categorized:

| Category     | Meaning                                              | Examples                  |
|--------------|------------------------------------------------------|---------------------------|
| `market`     | Reflects real fantasy-manager behavior / trades      | FantasyCalc               |
| `aggregator` | Blends many experts into a consensus                 | FantasyPros ECR, DynastyProcess |
| `expert`     | A single analyst's published list                    | Manual CSV imports        |
| `model`      | Algorithmic prospect / dynasty model                 | Brainy Ballers SPS, PFF   |

The Sleeper player API is used purely for **canonical ID resolution** — every
player is tied to a `sleeper_id`, which cross-references MFL, ESPN, Yahoo,
FantasyCalc, etc.

### 2. Normalize each source

For each source we either:

- Convert overall rank to a 0–100 score with linear decay over a depth of
  300 (rank 1 = 100, rank 300 = 0, rank > 300 = 0), **or**
- Rescale the source's native `market_value` (e.g. FantasyCalc trade values)
  to 0–100 against that source's top value.

### 3. Weight by track record, position, and years pro

Each source has a `default_weight`. At score time, the effective weight per
ranking row is:

```
effective_weight = default_weight
                 × track_record_multiplier(source, position)
                 × position_modifier(source, position)
                 × years_pro_modifier(source, years_pro)
```

**Track-record multiplier** — derived from backtests against realized NFL
production. Prefer the position-specific `source_track_record` row when one
exists; fall back to the overall (position-None) row otherwise.

| Spearman \|ρ\| vs. realized production | Multiplier |
|-----------------------------------------|-----------:|
| ≥ 0.35                                  | 1.6        |
| ≥ 0.25                                  | 1.3        |
| ≥ 0.15                                  | 1.0        |
| < 0.15                                  | 0.5        |
| no backtest yet                         | 1.0        |

**Position modifier** — source-specific override per (slug, position).
Example: RAS is 1.5× at WR/TE, 1.2× at RB, 0.3× at QB. CFBD breakouts are
1.5× at WR, 1.3× at TE, 0.4× at QB. See `src/dynasty/weights.py` for the
full table.

**Years-pro modifier** — rookie-signal sources (`nfl_draft_capital`, `ras`,
`cfbd_breakouts`) decay linearly from 1.0 in the rookie year to a 0.3 floor.
Market-source signals (`fantasycalc`, `ffc_adp`, `dynastyprocess`) get an
inverse curve: 0.6 at year 0 → 0.8 at year 1 → 1.0 at year 2+. Rationale:
market values are *trailing* indicators for rookies, but reliable for vets;
athleticism and draft capital are pre-NFL signals that lose relevance once
real production exists.

All three knobs live in `src/dynasty/weights.py` and the policy is editable
in one place. Sources with no backtest yet are treated as neutral (1.0).

### 4. Composite

For every player, composite score = weighted average of available per-source
scores. Players are then ranked overall and per-position.

### 5. Consensus divergence (buy / sell signals)

Separately, the model computes a "market consensus rank" using only the
`market` and `aggregator` sources — i.e. where the broader fantasy community
has the player.

```
rank_divergence = consensus_rank − model_rank
```

- **Positive divergence** → the model likes the player more than the market →
  **buy signal**
- **Negative divergence** → the model lower than the market →
  **sell signal**

This is the most useful column for actually playing dynasty: it tells you
where the model thinks the market is wrong.

### 6. Tiers

Composite ranks are bucketed into simple tiers (T1: top 6, T2: 7–12, T3:
13–24, etc.) for portfolio-level decisions.

---

## Sources currently wired up

| Source                | Type        | Status                                   |
|-----------------------|-------------|------------------------------------------|
| FantasyCalc           | market      | ✅ free public API, fetched daily         |
| Sleeper               | aggregator  | ✅ free public API — player ID map        |
| DynastyProcess        | aggregator  | ✅ free open CSV (FantasyPros consensus)  |
| Brainy Ballers        | model       | ✅ public Top-500 scrape (rate-limited)   |
| **NFL Draft capital** | model       | ✅ nflverse public CSV (rookies + recent classes — see `docs/RESEARCH-sources.md` §A1) |
| **FFC ADP**           | market      | ✅ FantasyFootballCalculator public REST API (PPR, 2QB, Dynasty, Rookie — §A3) |
| **RAS**               | model       | ✅ Kent Lee Platte's Relative Athletic Score — drop CSV in `data/ras/` (§A2)  |
| **CFBD breakouts**    | model       | ✅ College Breakout Age + Dominator — drop CSV in `data/cfbd/` (§C1)           |
| FantasyPros direct    | aggregator  | 🔒 stub — requires paid API key           |
| PFF                   | model       | 🔒 stub — requires paid API partnership   |
| Manual CSV            | expert      | ✅ via `dynasty.manual_import.import_csv` |

> **Note on KeepTradeCut:** KTC's ToS forbids scraping. FantasyCalc is the
> closest legal substitute — values come from real fantasy-manager trades
> and update multiple times per day.

Adding a new source: implement `BaseSource` in `src/dynasty/sources/new_source.py`,
register it in `sources/__init__.py`, and the rest of the pipeline (player
resolution, time-series storage, composite scoring, backtest weighting)
picks it up automatically.

---

## Backtesting

Every source's accuracy can be measured against realized NFL fantasy
production:

```bash
python -m dynasty.cli backtest fantasycalc --years 2020,2021,2022 --window 3
```

This writes a `source_track_record` row containing Spearman correlation,
R², MAE, and top-12 / top-24 hit-rate over an outcome window of `N` seasons.
The next time you run `score`, that source's weight is automatically
adjusted by the track-record multiplier table above.

---

## Data model

Five tables (SQLAlchemy 2.0 ORM, SQLite by default — set `DATABASE_URL`
to use Postgres):

- `players` — canonical entity, keyed by `sleeper_id`; cross-references
  `mfl_id`, `espn_id`, `yahoo_id`, etc.
- `sources` — one row per ranking source.
- `rankings` — **time-series**: every sync appends; never overwrites. This
  is what powers trend signals and backtests.
- `production` — actual NFL fantasy production by season / week.
- `evaluations` — granular per-metric scores (PFF grades, Reception
  Perception route data, model outputs).
- `composite_scores` — model output, append-only history with breakdown JSON.
- `source_track_record` — per-source backtest results that feed the weighting.

See `src/dynasty/db/models.py` for the full schema.

---

## Project layout

```
src/dynasty/
  cli.py                # Typer CLI entry point
  config.py             # env-based settings (pydantic-settings)
  sync.py               # source → DB sync layer (player resolution)
  scoring.py            # composite score + divergence math
  weights.py            # position-/years-pro-aware weight modifiers
  backtest.py           # per-source accuracy calculation
  manual_import.py      # import expert rankings from CSV
  scheduler.py          # APScheduler — daily syncs in `run-scheduler`
  report.py             # HTML report generator
  launcher.py           # GUI launcher (open browser)
  launcher_headless.py  # CI / GitHub Pages launcher
  starter_pack.py       # first-run player seed
  production_loader.py  # weekly NFL production loader
  sources/              # one adapter per ranking source
    base.py             # BaseSource ABC + RankingRecord dataclass
    fantasycalc.py
    dynastyprocess.py
    sleeper.py
    brainy_ballers.py
    nfl_draft_capital.py
    ffc_adp.py
    ras.py
    cfbd_breakouts.py
    fantasypros.py      # stub
    pff.py              # stub
  db/
    models.py           # SQLAlchemy schema
    session.py          # engine + session factory
tests/
  smoke_test.py
  test_nfl_draft_capital.py
  test_ffc_adp.py
  test_ras.py
  test_cfbd_breakouts.py
  test_weights.py
data/
  ras/                  # drop Kent Lee Platte's RAS CSV here (gitignored)
  cfbd/                 # drop college Breakout Age + Dominator CSV here (gitignored)
docs/
  RESEARCH-sources.md     # 440-line source-landscape writeup w/ citations
  CHANGELOG-model.md      # what each release shifts about score outputs
```

---

## Configuration

Copy `.env.example` to `.env` and edit as needed:

```ini
DATABASE_URL=sqlite:///dynasty.db
FANTASYPROS_API_KEY=        # optional, enables fantasypros source
PFF_API_KEY=                # optional, enables pff source
REQUEST_TIMEOUT_SECONDS=30
```

---

## Roadmap

Research foundation: see `docs/RESEARCH-sources.md` (440-line writeup of the
source landscape with statistical evidence and per-source weighting
recommendations) and `docs/CHANGELOG-model.md` (running log of what each
release changes about the score outputs and why).

Near-term:

- ~~**FantasyFootballCalculator ADP** — second free market signal, complements
  FantasyCalc.~~ (PR #3 — done)
- ~~**RAS (Relative Athletic Score)** — Kent Lee Platte's free CSV. Best free
  athleticism composite; especially useful as a tail-risk filter.~~ (PR #4 — done)
- ~~**Breakout Age + College Dominator** — computed from `cfbd-api-py` college
  stats. Replicates ~80% of PlayerProfiler's "secret sauce" for free.~~
  (PR #5 — CSV ingestion done; live CFBD API integration is a follow-up.)
- ~~**Position-specific + years-pro weighting** — the same source should not
  weight the same for a rookie WR and a Year-6 RB. Refactors
  `SourceTrackRecord` lookup to apply position-level multipliers.~~ (PR #6 — done)
- ~~**League roster import (MFL + Sleeper)** — KeepTradeCut-style "rate my
  team / league" view from the user's actual rosters.~~ (PR #7 — done. Try
  `python -m dynasty.cli league sleeper <league_id>` after `score`.)

Later:

- Additional model-grade prospect sources via manual CSV (Reception Perception,
  PFF Big Board, Matt Waldman RSP, Dane Brugler "Beast", Hayden Winks).
- Trade calculator (sum of values on each side, with positional scarcity
  adjustment).
- Mock-draft tool for rookie drafts.

---

## License

Personal project — no license declared. All third-party data is fetched
from publicly-published endpoints in accordance with each source's
Terms of Service.
