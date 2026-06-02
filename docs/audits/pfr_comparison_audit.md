# PFR Similarity Sanity-Check Audit
Compares our v3.10 Fantasy-Point Arc Comparables (top-10) to PFR's `#all_sim_scores` **Career** row for the top-50 ranked players.

## Themes / Model Holes

Audited **7** of 50 ranked players (43 skipped — see per-player blocks for reasons).

- Average overlap@10 with PFR: **0.43 / 10**
- Average overlap@20 with PFR: 0.43 / 20
- Average Jaccard (top-10 vs top-10): 0.023

### Where PFR comps live that ours don't

- `mid-tier-veteran-PFR-likes` — **25** misses
- `uncategorised` — **18** misses
- `active-modern-PFR-prefers` — **14** misses
- `early-career-bust-PFR-keeps` — **8** misses
- `pre1999-comp-PFR-also-pulls` — **2** misses

### Where our extras live that PFR rejects

- `durable-legend-we-over-pull` — **33** extras
- `pre1999-corpus-comp` — **20** extras
- `uncategorised` — **9** extras
- `washed-out-bust-overweighted-by-arc` — **5** extras

### Actionable model holes

1. **Active-modern cohort under-represented.** PFR's Career row for veteran QBs surfaces *currently-playing peers* (Mahomes, Burrow, Herbert, Goff, Prescott) but our top-10 skews to retired all-time greats. The model's age-band tolerance + the era-pace-adjusted retired corpus are pulling us toward retirees. *Fix:* gate the comp pool so at least 3 of 10 comps are *active* same-position players within ±3 yrs of age; boost similarity weight on overlap with the queried player's NFL years.

2. **Mid-tier-veteran blind spot.** PFR repeatedly surfaces competent journeymen (Aaron Brooks, Trent Green, Danny White, Bert Jones, Matt Schaub) that don't appear in our top-10. We're filtering them out via wash-out-rate + long-career bias — which sounds like a feature but means a Sam Darnold or Jared Goff profile gets matched only to Brady / Brees / Manning, inflating the projection. *Fix:* add a `mid-tier-anchor` lane that requires the cohort to include 2–3 comps with career_total_fp within ±25% of the query player's current arc.

3. **Pre-1999 corpus is pulling hard.** Era-pace-adjusted comps from the 1980-1998 corpus (Marino, Tarkenton, Unitas, Elway) show up in our top-10 for almost every veteran QB profile; PFR's Career row stays inside the post-1999 per-page sim scope and rarely cites them. This is a model choice (we expanded the corpus deliberately in v3.9) but it's currently *driving* the projection rather than sanity-checking it. *Fix:* hard-cap pre-1999 comps at 3 of 10 in the display table, and weight their similarity by the existing 0.9× confidence haircut so they don't out-rank a post-1999 comp at equal raw sim.

4. **Durable-legend over-pull on mid-tier query players.** When the query player is *not* an elite producer (Mayfield, Darnold, Goff), our top-10 still cites Brady / Brees / Manning / Favre because their post-age-N fp/g curve happens to align. PFR's similarity Bill James method weights career-shape-and-magnitude together, so it stays away. *Fix:* anchor the similarity vector to *cumulative* career-fp percentile alongside fp/g curve; players in different career-fp deciles should rarely match.


---

## Per-player detail

### #1 Josh Allen (QB)
PFR: `AlleJo02` · gsis: `00-0034857`

- overlap@10 = **1/10**  ·  overlap@20 = 1/20  ·  Jaccard = 0.053

| # | Our top-10 | PFR top-10 (Career) |
|---|---|---|
| 1 | Cam Newton | Patrick Mahomes |
| 2 | Brett Favre | Jared Goff |
| 3 | Donovan McNabb | Baker Mayfield |
| 4 | Steve McNair | Daunte Culpepper |
| 5 | Peyton Manning | Scott Mitchell |
| 6 | Daunte Culpepper | Teddy Bridgewater |
| 7 | Dan Marino | Aaron Brooks |
| 8 | Matt Ryan | Colin Kaepernick |
| 9 | Johnny Unitas | Andrew Luck |
| 10 | Fran Tarkenton | Jake Delhomme |

**We missed (in PFR not us):** Patrick Mahomes _(active-modern-PFR-prefers)_, Jared Goff _(active-modern-PFR-prefers)_, Baker Mayfield _(active-modern-PFR-prefers)_, Scott Mitchell _(mid-tier-veteran-PFR-likes)_, Teddy Bridgewater _(mid-tier-veteran-PFR-likes)_, Aaron Brooks _(mid-tier-veteran-PFR-likes)_, Colin Kaepernick _(mid-tier-veteran-PFR-likes)_, Andrew Luck _(active-modern-PFR-prefers)_, Jake Delhomme _(mid-tier-veteran-PFR-likes)_

**Our extras (in us not PFR top-20):** Cam Newton _(durable-legend-we-over-pull)_, Brett Favre _(durable-legend-we-over-pull)_, Donovan McNabb _(durable-legend-we-over-pull)_, Steve McNair _(durable-legend-we-over-pull)_, Peyton Manning _(durable-legend-we-over-pull)_, Dan Marino _(pre1999-corpus-comp)_, Matt Ryan _(durable-legend-we-over-pull)_, Johnny Unitas _(pre1999-corpus-comp)_, Fran Tarkenton _(pre1999-corpus-comp)_

### #2 Jalen Hurts (QB)
PFR: `HurtJa00` · gsis: `00-0036389`

- overlap@10 = **1/10**  ·  overlap@20 = 1/20  ·  Jaccard = 0.053

| # | Our top-10 | PFR top-10 (Career) |
|---|---|---|
| 1 | Mike Vick | Andrew Luck |
| 2 | Dan Marino | Daunte Culpepper |
| 3 | Andrew Luck | Justin Herbert |
| 4 | Peyton Manning | Joe Burrow |
| 5 | Steve McNair | Kyler Murray |
| 6 | Brett Favre | Bert Jones |
| 7 | Cam Newton | Aaron Brooks |
| 8 | Donovan McNabb | Josh Allen |
| 9 | Johnny Unitas | Trent Green |
| 10 | Fran Tarkenton | Danny White |

**We missed (in PFR not us):** Daunte Culpepper _(early-career-bust-PFR-keeps)_, Justin Herbert _(active-modern-PFR-prefers)_, Joe Burrow _(active-modern-PFR-prefers)_, Kyler Murray _(active-modern-PFR-prefers)_, Bert Jones _(mid-tier-veteran-PFR-likes)_, Aaron Brooks _(mid-tier-veteran-PFR-likes)_, Josh Allen _(active-modern-PFR-prefers)_, Trent Green _(mid-tier-veteran-PFR-likes)_, Danny White _(mid-tier-veteran-PFR-likes)_

**Our extras (in us not PFR top-20):** Mike Vick _(washed-out-bust-overweighted-by-arc)_, Dan Marino _(pre1999-corpus-comp)_, Peyton Manning _(durable-legend-we-over-pull)_, Steve McNair _(durable-legend-we-over-pull)_, Brett Favre _(durable-legend-we-over-pull)_, Cam Newton _(durable-legend-we-over-pull)_, Donovan McNabb _(durable-legend-we-over-pull)_, Johnny Unitas _(pre1999-corpus-comp)_, Fran Tarkenton _(pre1999-corpus-comp)_

### #3 Trevor Lawrence (QB)
PFR: `LawrTr00` · gsis: `00-0036971`

_Skipped: no sim_scores table._

### #4 Justin Herbert (QB)
PFR: `HerbJu00` · gsis: `00-0036355`

_Skipped: no sim_scores table._

### #5 Jahmyr Gibbs (RB)
PFR: `GibbJa01` · gsis: `00-0039139`

_Skipped: no sim_scores table._

### #6 Bijan Robinson (RB)
PFR: `RobiBi01` · gsis: `00-0038542`

_Skipped: no sim_scores table._

### #7 Lamar Jackson (QB)
PFR: `JackLa00` · gsis: `00-0034796`

_Skipped: no sim_scores table._

### #8 Amon-Ra St. Brown (WR)
PFR: `StxxAm00` · gsis: `00-0036963`

_Skipped: no sim_scores table._

### #9 Patrick Mahomes (QB)
PFR: `MahoPa00` · gsis: `00-0033873`

- overlap@10 = **1/10**  ·  overlap@20 = 1/20  ·  Jaccard = 0.053

| # | Our top-10 | PFR top-10 (Career) |
|---|---|---|
| 1 | Cam Newton | Daunte Culpepper |
| 2 | Brett Favre | Andrew Luck |
| 3 | Peyton Manning | Dak Prescott |
| 4 | Steve McNair | Jared Goff |
| 5 | Dan Marino | Bert Jones |
| 6 | Donovan McNabb | Randall Cunningham |
| 7 | Drew Brees | Aaron Brooks |
| 8 | Tom Brady | Trent Green |
| 9 | Daunte Culpepper | Danny White |
| 10 | Johnny Unitas | Kurt Warner* |

**We missed (in PFR not us):** Andrew Luck _(active-modern-PFR-prefers)_, Dak Prescott _(active-modern-PFR-prefers)_, Jared Goff _(active-modern-PFR-prefers)_, Bert Jones _(mid-tier-veteran-PFR-likes)_, Randall Cunningham _(mid-tier-veteran-PFR-likes)_, Aaron Brooks _(mid-tier-veteran-PFR-likes)_, Trent Green _(mid-tier-veteran-PFR-likes)_, Danny White _(mid-tier-veteran-PFR-likes)_, Kurt Warner* _(mid-tier-veteran-PFR-likes)_

**Our extras (in us not PFR top-20):** Cam Newton _(durable-legend-we-over-pull)_, Brett Favre _(durable-legend-we-over-pull)_, Peyton Manning _(durable-legend-we-over-pull)_, Steve McNair _(durable-legend-we-over-pull)_, Dan Marino _(pre1999-corpus-comp)_, Donovan McNabb _(durable-legend-we-over-pull)_, Drew Brees _(durable-legend-we-over-pull)_, Tom Brady _(durable-legend-we-over-pull)_, Johnny Unitas _(pre1999-corpus-comp)_

### #10 Jaxson Dart (QB)
PFR: `DartJa00` · gsis: `00-0040691`

_Skipped: no sim_scores table._

### #11 Drake Maye (QB)
PFR: `MayeDr00` · gsis: `00-0039851`

_Skipped: no sim_scores table._

### #12 Brock Purdy (QB)
PFR: `PurdBr00` · gsis: `00-0037834`

_Skipped: no sim_scores table._

### #13 De'Von Achane (RB)
PFR: `AchaDe00` · gsis: `00-0039040`

_Skipped: no sim_scores table._

### #14 Kyler Murray (QB)
PFR: `MurrKy00` · gsis: `00-0035228`

_Skipped: no sim_scores table._

### #15 Puka Nacua (WR)
PFR: `NacuPu00` · gsis: `00-0039075`

_Skipped: no sim_scores table._

### #16 Bo Nix (QB)
PFR: `NixxBo00` · gsis: `00-0039732`

_Skipped: no sim_scores table._

### #17 Jonathan Taylor (RB)
PFR: `TaylJo02` · gsis: `00-0036223`

_Skipped: no sim_scores table._

### #18 Baker Mayfield (QB)
PFR: `MayfBa00` · gsis: `00-0034855`

- overlap@10 = **0/10**  ·  overlap@20 = 0/20  ·  Jaccard = 0.000

| # | Our top-10 | PFR top-10 (Career) |
|---|---|---|
| 1 | Kordell Stewart | Jay Fiedler |
| 2 | Matt Ryan | Teddy Bridgewater |
| 3 | Jake Plummer | Colin Kaepernick |
| 4 | Ben Roethlisberger | Eric Hipple |
| 5 | Otto Graham | David Carr |
| 6 | Joe Montana | Marcus Mariota |
| 7 | Eli Manning | Kyle Orton |
| 8 | Drew Brees | Mitchell Trubisky |
| 9 | John Elway | James Harris |
| 10 | Dave Krieg | Blake Bortles |

**We missed (in PFR not us):** Jay Fiedler _(uncategorised)_, Teddy Bridgewater _(mid-tier-veteran-PFR-likes)_, Colin Kaepernick _(mid-tier-veteran-PFR-likes)_, Eric Hipple _(uncategorised)_, David Carr _(uncategorised)_, Marcus Mariota _(early-career-bust-PFR-keeps)_, Kyle Orton _(uncategorised)_, Mitchell Trubisky _(uncategorised)_, James Harris _(uncategorised)_, Blake Bortles _(uncategorised)_

**Our extras (in us not PFR top-20):** Kordell Stewart _(uncategorised)_, Matt Ryan _(durable-legend-we-over-pull)_, Jake Plummer _(washed-out-bust-overweighted-by-arc)_, Ben Roethlisberger _(durable-legend-we-over-pull)_, Otto Graham _(uncategorised)_, Joe Montana _(pre1999-corpus-comp)_, Eli Manning _(durable-legend-we-over-pull)_, Drew Brees _(durable-legend-we-over-pull)_, John Elway _(pre1999-corpus-comp)_, Dave Krieg _(uncategorised)_

### #19 Nico Collins (WR)
PFR: `CollNi00` · gsis: `00-0036554`

_Skipped: no sim_scores table._

### #20 Tetairoa McMillan (WR)
PFR: `McMiTe00` · gsis: `00-0040124`

_Skipped: no sim_scores table._

### #21 Jayden Daniels (QB)
PFR: `DaniJa02` · gsis: `00-0039910`

_Skipped: no sim_scores table._

### #22 Ja'Marr Chase (WR)
PFR: `ChasJa00` · gsis: `00-0036900`

_Skipped: no sim_scores table._

### #23 Jared Goff (QB)
PFR: `GoffJa00` · gsis: `00-0033106`

- overlap@10 = **0/10**  ·  overlap@20 = 0/20  ·  Jaccard = 0.000

| # | Our top-10 | PFR top-10 (Career) |
|---|---|---|
| 1 | Ben Roethlisberger | Danny White |
| 2 | Drew Brees | Aaron Brooks |
| 3 | Matt Ryan | Bert Jones |
| 4 | Jay Cutler | Daryle Lamonica |
| 5 | Jake Plummer | Dak Prescott |
| 6 | Eli Manning | Andrew Luck |
| 7 | Fran Tarkenton | Matt Schaub |
| 8 | John Hadl | David Garrard |
| 9 | John Elway | Jameis Winston |
| 10 | Drew Bledsoe | Brian Sipe |

**We missed (in PFR not us):** Danny White _(mid-tier-veteran-PFR-likes)_, Aaron Brooks _(mid-tier-veteran-PFR-likes)_, Bert Jones _(mid-tier-veteran-PFR-likes)_, Daryle Lamonica _(pre1999-comp-PFR-also-pulls)_, Dak Prescott _(active-modern-PFR-prefers)_, Andrew Luck _(active-modern-PFR-prefers)_, Matt Schaub _(mid-tier-veteran-PFR-likes)_, David Garrard _(early-career-bust-PFR-keeps)_, Jameis Winston _(early-career-bust-PFR-keeps)_, Brian Sipe _(mid-tier-veteran-PFR-likes)_

**Our extras (in us not PFR top-20):** Ben Roethlisberger _(durable-legend-we-over-pull)_, Drew Brees _(durable-legend-we-over-pull)_, Matt Ryan _(durable-legend-we-over-pull)_, Jay Cutler _(washed-out-bust-overweighted-by-arc)_, Jake Plummer _(washed-out-bust-overweighted-by-arc)_, Eli Manning _(durable-legend-we-over-pull)_, Fran Tarkenton _(pre1999-corpus-comp)_, John Hadl _(pre1999-corpus-comp)_, John Elway _(pre1999-corpus-comp)_, Drew Bledsoe _(pre1999-corpus-comp)_

### #24 Rashee Rice (WR)
PFR: `RiceRa01` · gsis: `00-0039067`

_Skipped: no sim_scores table._

### #25 Emeka Egbuka (WR)
PFR: `EgbuEm01` · gsis: `00-0040129`

_Skipped: no sim_scores table._

### #26 Daniel Jones (QB)
PFR: `JoneDa05` · gsis: `00-0035710`

_Skipped: no sim_scores table._

### #27 CeeDee Lamb (WR)
PFR: `LambCe00` · gsis: `00-0036358`

_Skipped: no sim_scores table._

### #28 Jaxon Smith-Njigba (WR)
PFR: `SmitJa06` · gsis: `00-0038543`

_Skipped: no sim_scores table._

### #29 Chris Olave (WR)
PFR: `OlavCh00` · gsis: `00-0037239`

_Skipped: no sim_scores table._

### #30 Kyren Williams (RB)
PFR: `WillKy02` · gsis: `00-0037840`

_Skipped: no sim_scores table._

### #31 Justin Jefferson (WR)
PFR: `JeffJu00` · gsis: `00-0036322`

_Skipped: no sim_scores table._

### #32 Cam Ward (QB)
PFR: `WardCa00` · gsis: `00-0040676`

_Skipped: no sim_scores table._

### #33 A.J. Brown (WR)
PFR: `BrowAJ00` · gsis: `00-0035676`

_Skipped: no sim_scores table._

### #34 Tee Higgins (WR)
PFR: `HiggTe00` · gsis: `00-0036410`

_Skipped: no sim_scores table._

### #35 Dak Prescott (QB)
PFR: `PresDa01` · gsis: `00-0033077`

- overlap@10 = **0/10**  ·  overlap@20 = 0/20  ·  Jaccard = 0.000

| # | Our top-10 | PFR top-10 (Career) |
|---|---|---|
| 1 | Brett Favre | Daryle Lamonica |
| 2 | Steve McNair | Andrew Luck |
| 3 | Dan Marino | Aaron Brooks |
| 4 | Donovan McNabb | David Garrard |
| 5 | Peyton Manning | Michael Vick |
| 6 | Mike Vick | Jameis Winston |
| 7 | Drew Brees | Danny White |
| 8 | Johnny Unitas | Kirk Cousins |
| 9 | Ben Roethlisberger | Brian Sipe |
| 10 | Joe Montana | Carson Wentz |

**We missed (in PFR not us):** Daryle Lamonica _(pre1999-comp-PFR-also-pulls)_, Andrew Luck _(active-modern-PFR-prefers)_, Aaron Brooks _(mid-tier-veteran-PFR-likes)_, David Garrard _(early-career-bust-PFR-keeps)_, Michael Vick _(early-career-bust-PFR-keeps)_, Jameis Winston _(early-career-bust-PFR-keeps)_, Danny White _(mid-tier-veteran-PFR-likes)_, Kirk Cousins _(uncategorised)_, Brian Sipe _(mid-tier-veteran-PFR-likes)_, Carson Wentz _(uncategorised)_

**Our extras (in us not PFR top-20):** Brett Favre _(durable-legend-we-over-pull)_, Steve McNair _(durable-legend-we-over-pull)_, Dan Marino _(pre1999-corpus-comp)_, Donovan McNabb _(durable-legend-we-over-pull)_, Peyton Manning _(durable-legend-we-over-pull)_, Mike Vick _(washed-out-bust-overweighted-by-arc)_, Drew Brees _(durable-legend-we-over-pull)_, Johnny Unitas _(pre1999-corpus-comp)_, Ben Roethlisberger _(durable-legend-we-over-pull)_, Joe Montana _(pre1999-corpus-comp)_

### #36 Tua Tagovailoa (QB)
PFR: `TagoTu00` · gsis: `00-0036212`

_Skipped: no sim_scores table._

### #37 Caleb Williams (QB)
PFR: `WillCa03` · gsis: `00-0039918`

_Skipped: no sim_scores table._

### #38 Sam Darnold (QB)
PFR: `DarnSa00` · gsis: `00-0034869`

- overlap@10 = **0/10**  ·  overlap@20 = 0/20  ·  Jaccard = 0.000

| # | Our top-10 | PFR top-10 (Career) |
|---|---|---|
| 1 | Y.A. Tittle | Mike Pagel |
| 2 | Bob Hoernschemeyer | Kyle Boller |
| 3 | Terry Bradshaw | Mark Malone |
| 4 | Dan Fouts | Scott Hunter |
| 5 | Alex Smith | Charlie Batch |
| 6 | Bernie Kosar | Vince Young |
| 7 | Roman Gabriel | Joey Harrington |
| 8 | Drew Bledsoe | Mark Sanchez |
| 9 | Ben Roethlisberger | Rick Mirer |
| 10 | Frank Ryan | Gary Cuozzo |

**We missed (in PFR not us):** Mike Pagel _(uncategorised)_, Kyle Boller _(uncategorised)_, Mark Malone _(uncategorised)_, Scott Hunter _(uncategorised)_, Charlie Batch _(uncategorised)_, Vince Young _(early-career-bust-PFR-keeps)_, Joey Harrington _(uncategorised)_, Mark Sanchez _(uncategorised)_, Rick Mirer _(uncategorised)_, Gary Cuozzo _(uncategorised)_

**Our extras (in us not PFR top-20):** Y.A. Tittle _(uncategorised)_, Bob Hoernschemeyer _(uncategorised)_, Terry Bradshaw _(uncategorised)_, Dan Fouts _(pre1999-corpus-comp)_, Alex Smith _(uncategorised)_, Bernie Kosar _(pre1999-corpus-comp)_, Roman Gabriel _(uncategorised)_, Drew Bledsoe _(pre1999-corpus-comp)_, Ben Roethlisberger _(durable-legend-we-over-pull)_, Frank Ryan _(uncategorised)_

### #39 Garrett Wilson (WR)
PFR: `WilsGa00` · gsis: `00-0037740`

_Skipped: no sim_scores table._

### #40 Joe Burrow (QB)
PFR: `BurrJo01` · gsis: `00-0036442`

_Skipped: no sim_scores table._

### #41 Justin Fields (QB)
PFR: `FielJu00` · gsis: `00-0036945`

_Skipped: no sim_scores table._

### #42 Josh Jacobs (RB)
PFR: `JacoJo01` · gsis: `00-0035700`

_Skipped: no sim_scores table._

### #43 Luther Burden III (WR)
PFR: `BurdLu00` · gsis: `00-0040735`

_Skipped: no sim_scores table._

### #44 Harold Fannin Jr. (TE)
PFR: `FannHa00` · gsis: `00-0040663`

_Skipped: no sim_scores table._

### #45 Zay Flowers (WR)
PFR: `FlowZa00` · gsis: `00-0039064`

_Skipped: no sim_scores table._

### #46 Deebo Samuel Sr. (WR)
PFR: `SamuDe00` · gsis: `00-0035719`

_Skipped: no sim_scores table._

### #47 Drake London (WR)
PFR: `LondDr00` · gsis: `00-0037238`

_Skipped: no sim_scores table._

### #48 Brock Bowers (TE)
PFR: `BoweBr01` · gsis: `00-0039338`

_Skipped: no sim_scores table._

### #49 George Pickens (WR)
PFR: `PickGe00` · gsis: `00-0037247`

_Skipped: no sim_scores table._

### #50 Deshaun Watson (QB)
PFR: `WatsDe00` · gsis: `00-0033537`

_Skipped: no sim_scores table._

