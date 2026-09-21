# Next Level Dynasty Football — v2.0 / v2.1 / v2.2 Methodology

> **TL;DR.** v1.x ranked players by per-stat z-score shape. v2.0 replaced
> the engine with a fantasy-point-arc methodology that ranks players by
> the fantasy points they actually produce under modern scoring, comping
> them to historical players whose fp/g curves match. v2.1 adds a 1-NFL-
> season ROOKIE engine and a cohort-aware dispatcher so 2025 draft class
> players appear in the main rankings with proper rookie-comp
> projections. v2.2 adds the **survival / confidence / late-breakout
> penalty stack** — multiplicative discounts that drop bust-pool, small-
> sample, and late-breakout QBs into the rank tier their realistic
> dynasty value warrants.

## Multiplier stack (v2.2)

```
  raw                              ← from v2.0 (vets) or v2.1 (rookies)
  × survival_multiplier            ← comp pool career-length quality
  × confidence (asymmetric)        ← small-sample Bayesian shrinkage
  × effective_late_breakout        ← QB only, conf-weighted
  clamp [0.20×raw, 1.00×raw]
  → production_score
```

Detailed formulas:

- `survival_multiplier = (1 - bust_rate)×0.20 + (1 - short_career_rate)×0.10 + 0.70`,
  clamped [0.65, 1.0]. Bust = comp's career ended by age 30 AND with
  < 8 NFL seasons. Short = comp had ≤ 5 NFL seasons. See
  [SURVIVAL-PENALTY.md](SURVIVAL-PENALTY.md).
- `confidence = min(career_nfl_starts / 32, 1.0)`; QBs with < 16 starts
  cap at 0.5. Above-baseline projections pull toward the position-tier
  median, below-baseline straight-multiply (asymmetric). See
  [CONFIDENCE-SHRINKAGE.md](CONFIDENCE-SHRINKAGE.md).
- `late_breakout_penalty` table: 22 → 1.00, 23 → 0.95, 24 → 0.88,
  25+ → 0.80; effective applied = `1 - (1 - lb) × confidence`. See
  [LATE-BREAKOUT-QBs.md](LATE-BREAKOUT-QBs.md).

Non-QB players take `late_breakout_penalty = 1.0` always. Non-QB rookies
routed through the v2.1 rookie engine skip v2.2 confidence shrinkage
(the rookie engine has its own games-played shrinkage).

Per-player diagnostics land in `data/diagnostics/v2.2_survival.json`,
`v2.2_confidence.json`, `v2.2_late_breakout.json`.

---

---

## Why v1.x failed

v1.0's similarity engine compared players by per-stat z-scores: passing
yards, passing TDs, INTs, rushing yards, rushing TDs (for QBs). Cosine
similarity matched players with similar STAT SHAPE — but z-scoring is
*scale-invariant within era*. A player producing 28 fp/G under sf_ppr
has the same z-score-shape as a player producing 17 fp/G if their stat
proportions match.

Josh Allen, the canonical example, produces 24-26 fp/G at peak under
sf_ppr — most of that from rushing TDs (6 pts each). v1.0's vector had
"rushing TDs" as one of five components, equally weighted with passing
yards (which scored 0.04 pts each — 150× lower per unit). The cosine
match for Allen pulled in pocket passers with similar passing volume,
ignoring the massive fantasy-point gap.

v1.1 added a career-length era lift (capped at 1.5× for dual-threats),
but it multiplied only `projected_remaining_years`. The BASE projection
was still the comp-pool average of stat-shape-similar players. v1.2
moved to per-category fp z-scoring, but z-scoring is *still*
scale-invariant.

**The diagnosis (Phil verbatim):**

> "I think we need a different methodology entirely. We should still
> compare to historical players at the position, but we should do a
> translation to fantasy point production before doing so."

## The v2.0 fix: fantasy-point arcs

v2.0 builds a per-player, per-format **fantasy-point arc**: a list of
season-by-season fp/g values, with each season's raw stats era-pace-
adjusted to era 4 (current) BEFORE scoring. Every value in the corpus
is in modern-fp-equivalent units. Players are then compared by their
fp/g curves directly, in raw fantasy-point space — no z-scoring.

### Pipeline

1. **Load corpus.** nflverse season-level stats (1999-present),
   restricted to skill positions (QB / RB / WR / TE), filtered to
   seasons with ≥ 4 games played.
2. **Build era-pace table.** Per-position, per-stat, per-era-from
   multipliers derived empirically from corpus median per-game rates,
   clamped to [0.6, 2.0]. Cell-level fallback to a documented table
   for thin buckets.
3. **Long-arc corpus.** Players are eligible as comps if any of:
   - retired (last_season ≤ 2022)
   - 8+ NFL seasons
   - age ≥ 33 with 6+ seasons.
   Long-arc-active veterans (Rodgers, Stafford, Wilson) contribute
   ONLY their completed seasons.
4. **Build fantasy-point arcs.** For each player and each scoring
   format (`sf_ppr`, `1qb_ppr`, `2qb_ppr`, `half_ppr`, `std`,
   `sf_te_premium`):
   - Era-pace-adjust each season's raw stats to era 4.
   - Apply the format's scoring coefficients → season fp total and
     fp/game.
   - Pre-compute career totals + peak-3yr + peak-single-season +
     career-avg per format.
5. **Build career-stage percentile table.** For each (position,
   career-season-index), collect the long-arc-corpus career-total
   fp through that stage, sorted ascending. Used for the v[9]
   vector dimension.

### The similarity vector (10-dim, in fantasy-point units)

For a target at age A, snapshotted at career-season-index S:

| v[i] | Component                                            | Weight  |
| ----:| ---------------------------------------------------- | -------:|
| 0    | fp/g at age A (recent-season weight 1.0)             |     2.0 |
| 1    | fp/g at age A-1 (weight 0.7)                         |     1.5 |
| 2    | fp/g at age A-2 (weight 0.5)                         |     1.0 |
| 3    | career-avg fp/g through age A                        |     3.0 |
| 4    | peak-3yr-avg fp/g through age A                      |     4.0 |
| 5    | peak-single-season fp/g through age A                |     3.0 |
| 6    | career-total fp through age A (/100 scale)           |     0.8 |
| 7    | trajectory slope (fp/g per career-season)            |     0.2 |
| 8    | durability × 10 (career games / possible games)      |     0.3 |
| 9    | career-stage percentile × 30                         |     0.5 |

All values era-pace-adjusted at corpus build → raw fantasy-point units.

**Distance metric: weighted Euclidean** (not cosine). We need magnitude
to matter: Allen (peak 25) is NOT similar to Daniel Jones (peak 16)
even if their proportions match. The peak / career-avg / current-fp
dimensions are weighted highest because they're the magnitude anchors;
slope and durability are weighted low because they're noisy on small
samples.

Similarity = `1 / (1 + d / 20.0)` so identical vectors → 1.0; a
different-tier comp scores ~0.2–0.4.

### Projection

For each comp returned by KNN:

- Sum the comp's realised post-snapshot fantasy points under the
  target format (already in modern-fp units → no additional era-pace
  multiplier needed).
- Time-discount 5%/year out.
- Similarity-weight, sum → `comp_weighted_fp`.
- Carry `comp_weighted_seasons` (similarity-weighted post-age
  seasons) as the expected remaining-career-length signal.

**Peak-anchored projection** (the second projection path):

- `projection_rate = max(recent_3yr × 1.10, peak_3yr × 0.90)`.
  - 1.10×recent: small upward bias on current form.
  - 0.90×peak: soft floor — a player who sustained an elite peak
    retains most of that ceiling.
  - `max()` of the two captures both stars-still-in-form (recent
    dominates) and stars-in-a-slump (peak floor catches them).
- `peak_anchored_fp = projection_rate × 17 × comp_weighted_seasons ×
  mid-life discount`.

### Final score

The dynasty production score per player is:

```
if target_peak_3yr >= ELITE_THRESHOLD[position]:
    production_score = max(comp_weighted_fp, peak_anchored_fp)
elif target_peak_3yr >= ELITE_THRESHOLD[position] - 5.0:
    # Linear blend in the soft band.
    production_score = blend(comp_weighted_fp, peak_anchored_fp)
else:
    production_score = comp_weighted_fp
```

ELITE thresholds (peak 3yr fp/g under sf_ppr):

- QB: 18.0
- RB: 15.0
- WR: 16.0
- TE: 12.0

Below the threshold the projection falls back to comp-weighted-only so
sub-elite players whose comp pool happens to include a few elite
long-career retired comps don't get inflated.

### Mobile / dual-threat lift

For QBs only, v1.1's per-style, per-era career-length lift is preserved:

- pocket: 1.00× fp, 1.00× years (no lift)
- mobile: 1.05× fp, 1.30× years (display)
- dual-threat: 1.10× fp, 1.50× years (display)

The fp lift is milder than v1.1's 1.5× because v2.0's fantasy-arc
methodology already surfaces long-career comps for any high-fp
dual-threat target via the projection-rate path — we no longer need
the brute-force lift to overcome a v1.x sample-bias bug. The display
lift on `projected_remaining_years` keeps the full v1.1 value so the
UI accurately reflects "modern medicine + rule changes continue to
extend mobile careers".

## Why this is dynasty-appropriate

Dynasty value is the projected lifetime fantasy points a player will
score for your roster. v1.x's stat-shape matching answered a different
question — "what shape of NFL career does this player project to
have?" — which correlated imperfectly with fantasy production. v2.0
measures the thing we actually care about: **fantasy points produced
under modern scoring**.

## Format overlay

Per-format projections (sf_ppr, 1qb_ppr, 2qb_ppr, sf_te_premium,
half_ppr, std) are produced by reading per-format fp totals directly
from the pre-computed arc corpus and recomputing positional VORP
baselines under the target roster rules. No re-scoring needed.

## v2.1 — 1-NFL-season rookie engine + cohort dispatcher

v2.0's cumulative-arc engine assumed every active player had 2+ seasons
of NFL data to vectorize. With the 2025 corpus refresh, this assumption
broke for the 2025 draft class — they had ONE completed NFL season,
which produced noisy comp matches when compared against 10-data-point
veteran arc vectors.

v2.1 introduces a three-tier cohort dispatcher in
`engine.similarity_v1.run_engine`:

```
for ap in active_players:
    n_completed = count(season.games >= 4 for season in ap)
    if n_completed == 0:                      # 2026 draft class
        skip  # deferred to v2.2 college chain
    elif n_completed == 1 and is_recent:      # 2025 draft class
        project via rookie_nfl_fp_arc
    else:                                     # 2+ seasons
        project via fantasy_arc_v2 (v2.0)
```

### 1-NFL-season rookie engine (`rookie_nfl_fp_arc.py`)

The rookie engine builds an 11-dim profile vector from the player's
rookie-year stats:

| Dim | Feature | Weight | Notes |
|---:|---|---:|---|
| 0 | rookie_fp_per_game | 8.0 | Dominant tier separator |
| 1 | rookie_games / 17 | 0.1 | Durability |
| 2 | passing_yards / G | 0.0005 | Per-stat differentiator (low weight; magnitude is large) |
| 3 | rushing_yards / G | 0.003 | Differentiates style |
| 4 | receiving_yards / G | 0.003 | Differentiates style |
| 5 | passing_TDs / G | 0.2 | |
| 6 | rushing_TDs / G | 0.3 | Fantasy-significant (6 pts each) |
| 7 | receiving_TDs / G | 0.3 | |
| 8 | completion_rate (QB only) | 0.1 | QB tier signal |
| 9 | age_at_rookie_year | **20.0** | **v2.3.5**: bumped from 0.2. Age is a primary skill-position predictor; pre-v2.3.5 it was a tie-breaker against trivial fp/G noise. Calibrated empirically against Phil's Johnny Wilson bug-report: weight 20.0 makes a 1-year age gap cost enough distance to overcome the BREAKOUT_BIAS multiplier (≤1.3×) that previously kept late-bloomer survivors at the top of bust-tier rookies' comp lists. |
| 10 | position_encoded | 0.0 | Informational; position-filter applies upstream |

**v2.3.5 corpus change**: `build_rookie_corpus` default flipped to
`require_post_rookie_season=False` and a new `bust_aware=True` flag
is on by default. Year-1-only busts are now included in the comp pool;
their realised year-2+ fp is zero (natural projection contribution),
and the v2.3.3 wash-out penalty fires correctly on bust-heavy pools.
The pre-v2.3.5 survivor-only corpus hid the bust population from the
penalty layer.

The corpus contains every historical NFL player's actual rookie
season (filtered to games ≥ 4, rookie_season ≥ 1999). For each entry,
the engine pre-computes `post_rookie_total_fp` — used for the
**breakout-bias re-ranking** that tilts top-K comp selection toward
proven year-2+ producers. The rookie projection result also carries
`bust_rate_in_comps` (fraction of top-K comps with no year-2+
season) as a confidence indicator surfaced in the report row.

Projection: `max(comp_weighted, peak_anchored) × confidence`
* `comp_weighted` — similarity-weighted sum of comps' realised year-2+
  fp (5%/yr discount).
* `peak_anchored` — `rookie_fp/G × 17 × expected_career_seasons ×
  position_discount`. Position horizons: QB 8, RB 8.5, WR 9.5, TE 9.
  Discounts: QB 0.72 (higher rookie-projection variance), RB/WR/TE 0.85.
* `confidence = max(0.35, min(games/10, 1.0))`. Limited-usage rookies
  (Travis Hunter, 7G) get 0.7 confidence; cup-of-coffee rookies (3G)
  floor at 0.35.

### Cohort definitions

* **0 completed NFL seasons**: 2026 draft class — drafted but not yet
  played any NFL games (Jeremiyah Love and others). EXCLUDED from main
  rankings; deferred to v2.2's college chain.
* **1 completed NFL season**: 2025 draft class — played one NFL season
  with games ≥ 4 (Jaxson Dart, Ashton Jeanty, Cam Ward, Tetairoa
  McMillan, Travis Hunter). Use `rookie_nfl_fp_arc`. Travis Hunter
  at 7 G still counts as "1 completed season" — his durability v[1]
  reflects the games-played ratio.
* **2+ completed NFL seasons**: 2024 class and earlier — use the v2.0
  cumulative-arc engine. Jayden Daniels (17 G 2024 + 7 G 2025) counts
  as 2 completed seasons.

### Why not chain to college?

v2.1 explicitly does NOT chain current 2025 rookies through their
college fp profiles to historical college players. That's v2.2's scope.
The 2025 class HAS NFL stats now — the 1-season-rookie engine projects
from NFL-data comps, which is more predictive than a college-chain
relay through historical college→NFL transitions.

The college chain remains the right tool for 2026 draft class (0 NFL
seasons). v2.2 will surface them on a separate `/prospects.html` page.

---

## Validation

See `tests/test_v2_fantasy_arc.py` (25 tests, all passing) and
`tests/test_v2_1_rookie_nfl.py` (19 tests, all passing) for the
pinned methodology invariants:

- **Top-of-board**: Allen / Hurts / Lamar / Daniels in elite QB cluster.
- **Comp-list quality**: top-10 comps for Allen / Mahomes / Lamar /
  Hurts each include ≥3 elite-fp historical QBs regardless of style.
- **Sub-tiers**: pocket QBs (Stroud, Tua, Love, Purdy) NOT top 5 but
  still rosterable (top 75).
- **Aging-veteran haircut**: Rodgers at 41 ranks ≥ #100.
- **Non-QB invariants preserved**: Nacua → retired all-time WRs,
  Bijan → retired RBs, Bowers → retired TEs.
- **Format overlay**: Allen SF rank ≥ 1QB rank by ≥ 7 spots; 2QB QB
  premium ≥ SF QB premium.

## Known limitations

- Corpus starts in 1999 (nflverse). Pre-1999 retired greats (Jim
  Brown, Steve Young peak, Barry Sanders, Jerry Rice prime) are not
  fully represented. Era 1 → 4 multipliers fall back to a documented
  table for thin pre-1999 cells.
- Birth dates missing for ~2% of retired players; we fall back to
  `rookie_season + 22` for age estimation.
- Sample-of-1 comp pools (e.g., Rodgers at 41 with only Brady as a
  same-age comp) fall back to comp-weighted only — the peak-anchored
  projection requires ≥ 3 comps. This intentionally damps the
  inflation that would otherwise occur for outlier-comp aging stars.

## v1.x → v2.0 migration notes

- `style_cohort.py` is deleted. Per the brief: "fantasy arc
  methodology allows Allen → Brady if their fp curves match."
- `similarity_v1.py` is now a thin wrapper around the fantasy-arc
  engine. Module name retained for back-compat with all existing
  callers (`format_overlay`, `report`, sources, tests). The
  per-stat z-score machinery is gone.
- `career_length_era.py` is kept and applied as a final fp / years
  multiplier for mobile / dual-threat QBs.
- `era_pace.py` is kept and applied EARLIER in the pipeline (to raw
  stats before scoring, not to scored fp).
- Tests:
  - `tests/test_engine_v1.py` — all v1.0 invariants still pass
    (corpus, comps, formats).
  - `tests/test_v1_1_calibration.py` — two obsolete tests skipped
    with pointers to v2.0 replacements (`test_mahomes_top_10`,
    `test_pocket_passers_unchanged`).
  - `tests/test_v1_2_fantasy_weighted_knn.py` — module-level skip;
    methodology entirely replaced.
  - `tests/test_v2_fantasy_arc.py` — v2.0 invariants, 25 tests, all passing
    against `current_season=2025` (refreshed corpus).
  - `tests/test_v2_1_rookie_nfl.py` — v2.1 cohort dispatcher + rookie
    engine, 19 tests, all passing.
