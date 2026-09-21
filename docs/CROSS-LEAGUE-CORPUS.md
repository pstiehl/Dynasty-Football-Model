# Cross-league Manager Score: the corpus

How the "Best Dynasty Managers" board is built, what bounds it, and the two
constraints that must not be forgotten — **Sleeper's non-commercial licence**
and **the privacy of the people in it**.

---

## 1. The ask

> "anyone who inputs their data into the tool should be part of the analysis
> like this example: *'ranked against 340 managers across 28 dynasty leagues
> we've indexed'*"

and, specifically:

> "who are the best managers at drafting across every dynasty football league"

The per-league [Manager Score](../src/dynasty/managerscore.py) page cannot
answer either. It scores managers *within* one league, mean 100 and sd 15 by
construction — so the best manager in every league scores about 115, and two
such managers cannot be compared. Answering the question needs a **corpus**,
and a corpus needs a **crawl**.

---

## 2. Why this is a bounded crawl and never a census

Three hard constraints, all verified against the live API and Sleeper's own
published terms rather than assumed.

### 2.1 Sleeper cannot be enumerated

There is **no endpoint that lists leagues**. The API offers:

| You have | You can get |
|---|---|
| a league id | `GET /v1/league/{id}` |
| a league id | `GET /v1/league/{id}/users` |
| a user id | `GET /v1/user/{user_id}/leagues/nfl/{season}` |

League ids are 19-digit snowflakes (`1316222126914539520`). That id space
cannot be swept — it is sparse beyond any feasible probing, and probing it
would be abusive even if it were not.

So there is exactly **one** discovery path, the social graph:

```
league ──▶ /league/{id}/users ──▶ for each user
       ──▶ /user/{user_id}/leagues/nfl/{season} ──▶ their leagues
       ──▶ /league/{id}/users ──▶ ...
```

Every league in the corpus is therefore reachable only because somebody in an
already-known league also plays in it. **Coverage is a function of who we
happened to walk to, not of what exists.** The page says this in the banner,
not in a code comment.

### 2.2 The API is free for non-commercial use only

Sleeper's published terms, verbatim:

> The Sleeper API is a read-only HTTP API that is **free to use for
> non-commercial purposes** and allows access to a user's leagues, drafts, and
> rosters.
>
> **For commercial use of the Sleeper API, please reach out to us directly to
> discuss licensing.**

**This project is non-commercial, and this crawl is the single largest
consumer of that allowance in the repository.**

> ⚠️ **If Next Level Dynasty Football is ever monetised — ads, subscriptions, a paid
> tier, a sponsored placement, sale of the site or its data — this crawl
> needs a licensing conversation with Sleeper *before* the money starts.**
> It is the most licence-exposed thing here: it reads other people's leagues
> in volume and republishes a derived ranking of named handles. Do not assume
> the non-commercial allowance carries over. This paragraph exists so that
> whoever flips the commercial switch cannot claim nobody wrote it down.

### 2.3 Rate guidance

> Be mindful of the frequency of calls. A general rule is to stay **under 1000
> API calls per minute**, otherwise, you risk being IP-blocked.

The crawler runs nowhere near that ceiling, deliberately: a floor on the gap
between call starts (`--min-delay`, default 120 ms) and a concurrency cap of 3
in the scoring harness, giving roughly 4–8 calls/second. Being a good citizen
on a free read-only API costs nothing but wall time.

### 2.4 Therefore: explicit caps, always

| Cap | Flag | Default | What it bounds |
|---|---|---|---|
| Depth | `--max-hops` | 2 | hops out from the seeds |
| Leagues | `--max-leagues` | 12 | leagues added per run |
| Calls | `--max-calls` | 400 | total Sleeper calls per run |
| Pace | `--min-delay` | 0.12 s | minimum gap between call starts |

Every run reports the budget it consumed, and the consumption is committed
into the artifact (`budget` block). `tests/test_crossleague.py` asserts that
all four caps remain configurable, so a future edit cannot quietly remove a
bound.

---

## 3. The dynasty filter: `settings.type == 2`

The corpus indexes dynasty leagues only. The discriminator is Sleeper's own
`settings.type` on the league object.

Sleeper's public docs describe the settings object **without enumerating this
field**, so the value was established **empirically against real league
objects**, not guessed:

| `settings.type` | Example league | `taxi_slots` | `max_keepers` | Reading |
|---|---|---|---|---|
| **2** | Dallas Kings | 3 | 1 | dynasty |
| **2** | DLD | 8 | 1 | dynasty |
| **2** | Game of Inches | 2 | 1 | dynasty |
| 1 | Playing For Keeps | 0 | 5 | keeper |
| 1 | Deke Dynasty | 0 | 18 | keeper |
| 0 | Stummy, The Megalabowl, MCIS | 0 | 1 | redraft |

The first real bounded run saw all three values across 13 leagues — 6 of
`type=2`, 2 of `type=1`, 5 of `type=0` — which is what makes the reading
solid rather than a two-case guess:

* **`type=2` is dynasty.** 4 of the 6 carried taxi squads; all had
  `max_keepers=1`, because a dynasty league keeps everyone and "keepers" is
  not the mechanism.
* **`type=1` is keeper.** Finite keeper counts (5 and 18), no taxi squad.
* **`type=0` is redraft.** No taxi squad, `max_keepers=1` — i.e. the field is
  simply unused, which is why `max_keepers` alone cannot separate redraft from
  dynasty and the `type` field itself has to be the discriminator.

Two consequences:

1. **Filter on `type == 2`.** Taxi squads and keeper counts are corroborating
   markers, not the test — 2 of the 6 dynasty leagues had no taxi squad, and
   `max_keepers=1` appears in both redraft and dynasty.
2. **Name matching would be wrong.** "Deke Dynasty" is `type=1` — a keeper
   league with 18 keepers. It plays a lot like dynasty, but Sleeper does not
   classify it as dynasty and neither do we. Indexing "what the platform calls
   a dynasty league" is a definition we do not have to defend; "leagues that
   feel dynasty-ish to us" is not. The cost is real: deep-keeper leagues that
   play like dynasty are excluded.

**This filter re-validates itself on every run.** `_filter_evidence()` in the
crawler recomputes the type-vs-markers table from the run's own data and
commits it into the artifact under `filter.observed_this_run`. If `type == 2`
ever stops meaning dynasty, it shows up there, on the run that saw it, rather
than rotting silently.

---

## 4. De-duplicating dynasty chains

A dynasty league is a *chain* of one league id per season, linked by
`previous_league_id`. The seed alone is four ids for one league:

```
2026 1316222126914539520
2025 1182068302871396352
2024 1048697508282695680
2023  970003719653826560
```

Counting those as four leagues would inflate coverage fourfold and count every
manager in them four times. Two mechanisms prevent it:

1. **Primary — the single-season invariant.** Discovery enumerates exactly one
   season. A chain holds exactly one league per season, so two *distinct
   same-season* league ids cannot belong to the same lineage. Prior seasons
   are never discovered as leagues at all; the scorer walks
   `previous_league_id` itself, so a league's history is *scored* without
   being *counted*.
2. **Safety net — `dedupe_lineages()`.** For the case the invariant does not
   cover (a hand-added seed that is a prior-season id of a league also
   discovered via its current-season head), lineages are collapsed on
   overlapping chain ids, keeping the entry with the longest chain — the one
   resting on the most evidence.

One league in the corpus can therefore carry several *league-seasons*, which
is why the artifact reports `n_leagues` and `n_league_seasons_scored`
separately.

---

## 5. Scoring: the same metric, not a second opinion

The per-league score is **not reimplemented** for the corpus.

The Manager Score maths, and the mapping from Sleeper's JSON into it, live
only in JavaScript — the page reads Sleeper live in the browser because the
site is a static GitHub Pages build with no server. Porting either to Python
would create a second implementation that immediately starts drifting.

Instead, `scripts/js/score_league_harness.js` runs the **shipped page
scripts** under node:

* `MANAGERSCORE_CORE_JS` + `MANAGERSCORE_UI_JS` are evaluated in a `vm`
  context, which makes every top-level declaration reachable — including the
  ones the UI layer does not export, `msFetchLeagueData`
  (`previous_league_id` chaining) and `msFetchTransactions` (per-week paging).
* `msBuildInput` → `msScoreLeague` are then called exactly as the page calls
  them.
* Three injections: a no-op `document` (so `msInit` never fires), a
  passthrough `console`, and an instrumented `fetch`.

`tests/test_crossleague.py` asserts the harness calls those functions by name
and contains no second copy of the maths.

`dynasty.crossleague.assert_scoring_constants_match()` additionally pins the
weights, shrink constants and index scale against the live
`MANAGERSCORE_CORE_JS` source. Change a weight in the per-league scorer and
the cross-league build **fails** rather than silently producing two different
metrics wearing one name.

### 5.1 Partial reads are discarded, never scored

`msGetJSONSoft()` turns any failed fetch into an empty fallback. That is right
for a browser — a league with no drafts legitimately 404s — and dangerous
here: a league whose transaction pages were refused mid-read would score
against half its history and *look* precise.

So a refused or failed call marks that league, and the orchestrator **throws
the whole league away**. A league is scored on all of its data or none of it.

---

## 6. The aggregation formula

For manager `m`, component `c` ∈ {draft, trade, waiver}:

```
N_c(m)    = Σ_leagues n_c(m, l)                  total scored events
zbar_c(m) = Σ_l n_c(m,l) · z_c(m,l) / N_c(m)     evidence-weighted mean
Z_c(m)    = zbar_c(m) · N_c(m) / (N_c(m) + K_c)  shrunk toward neutral

composite(m)  = Σ_c w_c · Z_c(m)      w renormalised over live components
crossIndex(m) = 100 + 15 · composite(m)
```

with `w = {draft 0.50, trade 0.35, waiver 0.15}` and
`K = {draft 6, trade 3, waiver 5}` — the merged scorer's own constants.

**Why a z-score is the right thing to carry across leagues.** It is already
normalised per league: "this far from your own league's average" is comparable
between a 10-team and a 14-team league in a way that raw KTC-point surplus is
not.

**Why evidence weighting.** A league where you made 30 picks counts ten times
one where you made three. Averaging leagues equally is the obvious
implementation and it is wrong — it lets one thin league swing a career.

**Why shrinkage twice, deliberately.** The per-league stage asks "how much
evidence *inside this league*?". This stage asks "how much evidence *across
the corpus*?". Different questions; someone with one draft in one league
should be pulled toward the middle by both. Both stages use the same
`n/(n+k)` form, so the guarantee the existing tests pin — shrinkage only ever
moves a manager *toward* neutral, never flips a sign, never overshoots — holds
end to end.

**Why the score is not re-centred on the corpus.** Re-standardising would make
every manager's number move whenever an unrelated league was indexed, which
makes a score impossible to quote or audit. The score is stable; the **rank**
carries the "against N managers" meaning. That is why `rank` and `percentile`
are first-class fields rather than something the page derives.

**The draft board has an entry requirement** (`MIN_DRAFT_PICKS_FOR_BOARD`,
default 6 = the draft shrink constant). Shrinkage alone still leaves a
one-pick manager *rankable*, and "best drafter" computed off one pick is not
an answer to the question that was asked. Gated managers stay on the main
leaderboard; they are excluded from the drafting board only.

---

## 7. Persistence, and what happens without write access

The site is static on GitHub Pages. **A browser cannot write to the corpus**,
so the corpus must be a committed artifact refreshed by the daily job:

```
data/cross_league/corpus.json     committed; grows run over run
dynasty_site/crossleague_corpus.json   published copy the page fetches
```

The daily workflow needs `contents: write` to commit it. **That permission is
already granted** in `.github/workflows/daily-refresh.yml` (it landed with the
merged Manager Score work, for the KTC history step) — but see §7.2: as of
this writing the write path has never actually executed, so it is granted in
configuration and **unproven in practice**.

The design therefore does not assume it works.

### 7.1 Two states, both correct

| State | What happens | What the page says |
|---|---|---|
| Corpus committed | `merge_corpus()` unions this run's crawl with leagues retained from previous runs. Coverage grows; leagues outside today's budget keep their last score rather than vanishing. | "Indexed since *date* over *N* daily runs; the corpus grows as each run reaches further into the league graph." |
| Not committed | Each run crawls from the seeds and ranks what it found. The feature works; it does not accumulate. | "Indexed in this build only — the corpus is not being retained between runs yet, so it does not grow day to day." |

Degradation needs no separate code path: no committed corpus simply means no
prior provenance to carry forward. `persisted` is `false`, `n_runs` is 1, and
both the artifact `notes` and the page banner say so. A corrupt or
schema-mismatched corpus is treated as absent (`load_corpus()` returns `None`
rather than raising), because a bad file must degrade the feature, not break
the daily build.

The site build never crawls. It copies the committed corpus into
`dynasty_site/` if one exists, and builds the page either way.

### 7.2 What is actually unproven

* **The repo-level Actions setting is unreadable with this token.** GitHub caps
  the workflow token at the repository/organisation "Workflow permissions"
  setting regardless of the YAML. `GET /repos/.../actions/permissions/workflow`
  returns **403** for the credential available here, so whether the setting is
  *Read and write* or *Read-only* could not be established. If it is
  read-only, the YAML grant is inert.
* **The existing write path has never run.** The `Retain KTC value history`
  step has succeeded on every recent run, but its log says
  `No new KTC value history to commit.` every time, and
  `data/consensus/history/` does not exist on `main`. The step is
  `continue-on-error`, so a failing `git push` would still report success.
  **No commit-back from this workflow has been observed to succeed.**

This is the same pending question that affects PR #63's KTC snapshots, and it
is a repository-settings question, not a code question. The corpus feature is
built to be correct either way, so it does not block on the answer.

---

## 8. Privacy and conduct

This board ranks real people who did not ask to be ranked. The rules are not
optional.

### 8.1 What is read

Only Sleeper's **public, read-only, unauthenticated** API — the same data any
visitor to a league page can see. No login, no credential, no private league
data. Sleeper: *"We do not perform authentication as our API is read-only and
only contains league information."*

### 8.2 What is stored about a person

**The Sleeper `display_name` and the opaque Sleeper `user_id`. Nothing else.**

`display_name` is the pseudonymous handle the user chose — `pjstiehl`,
`SimpsDontCry`. No real names, emails, avatars, team metadata or any other
identifying field reaches the artifact, and **no attempt is made anywhere in
this pipeline to resolve a handle to a real person.** Doing so is out of scope
permanently, not merely unimplemented.

`tests/test_crossleague.py::TestPrivacy` asserts the exact key set of a
leaderboard row and fails if `avatar`, `email`, `real_name`, `phone` or
`username` ever appear in the artifact.

### 8.3 No shaming board

**There is no "worst managers" board and there will not be one.** Boards are
ranked best-first; the tail is simply the tail. The renderer tests assert that
no "worst manager" / "shame" / "biggest loser" framing appears on the page.

The draft board's entry gate also protects the bottom of the table: a manager
with four scored picks is not held up as a bad drafter on that evidence.

### 8.4 Said in plain language, on the page

The page carries a "What is indexed, and why" section covering: that Sleeper
cannot be enumerated, that the corpus is a bounded sample and never a census,
what is read, what is stored about a person, that filtering is on Sleeper's
league type rather than the league's name, that there is no shaming board, and
that the API is non-commercial-use-only. In plain language, not jargon, and on
the page rather than only in this file — someone who finds themselves on this
board should be able to read what was measured without opening the repo.

### 8.5 Honest User-Agent

The crawler identifies itself:

```
Dynasty-Football-Model/cross-league-corpus
(+https://github.com/pstiehl/Dynasty-Football-Model)
```

This project does not spoof browsers to evade access controls — the same
position taken with Pro Football Reference in the daily workflow, where a 403
was accepted rather than worked around. The tests assert no `Mozilla/5.0`,
`Chrome/` or `Safari/` string appears in the crawler.

---

## 9. Running it

```bash
# cheap: discovery only, scores nothing, prints what it would index
python scripts/crawl_cross_league.py --discover-only

# a bounded real run
python scripts/crawl_cross_league.py --max-leagues 6 --max-calls 400 \
    --min-delay 0.25 --write-corpus

# what the daily job runs, publishing into a site build too
python scripts/crawl_cross_league.py --write-corpus --site-out dynasty_site
```

Seeds live in `data/cross_league/seeds.json`.

### 9.1 Why a separate registry, not `leagues.json`

`leagues.json` at the repo root is a **publishing manifest**: every entry
produces `dynasty_site/leagues/<platform>-<id>.json` and appears in the
`league.html` selector, and it exists mainly because MFL has no CORS and must
be baked in at build time.

The two files differ in every dimension that matters:

| | `leagues.json` | `data/cross_league/seeds.json` |
|---|---|---|
| Purpose | publish a league page | entry points for a crawl |
| Platforms | MFL + Sleeper | Sleeper only |
| Lifecycle | hand-curated, small | machine-extensible, grows |
| Consumer | `league.html` selector | the corpus crawler |
| Side effect of an entry | a published page + selector row | a few more API calls |

Adding crawl seeds to `leagues.json` would publish a per-league page for every
seed and pollute the league picker with leagues nobody on this site asked to
see. **Kept separate on purpose.**

---

## 10. Verification status

**Verified:**

* Dynasty filter value `settings.type == 2`, against real league objects
  spanning both dynasty and keeper leagues (§3), including the
  name-matching counter-example.
* All Sleeper response shapes the pipeline depends on, against the live API:
  league, `/users`, `/user/{id}/leagues/nfl/{season}`, `/drafts`,
  `/draft/{id}/picks`, `/transactions/{week}`.
* Sleeper's licensing and rate-limit terms, read from `docs.sleeper.com`.
* A real bounded end-to-end run: discovery, scoring via the shipped JS,
  aggregation and artifact write. **5 dynasty leagues, 56 managers, 21
  league-seasons, 526 API calls** (16 discovery + 510 scoring), 0 refused,
  0 network errors, against a 600-call cap.
* That compacting the retained block is lossless for aggregation: the
  leaderboard and draft board are byte-identical before and after, at 13x
  smaller (1.57 MB → 118 KB).
* Aggregation maths, lineage de-duplication, persistence degradation, coverage
  phrasing and the privacy key-set, by stdlib tests.
* Renderer behaviour and HTML escaping, in node against a stub DOM.
* Constant parity between the per-league scorer and the aggregator, including
  a tampered-fixture test proving the guard can actually fail.

**Not verified:**

* **Rendering.** There is no browser in the build environment. The page's
  *logic* is asserted against a stub DOM; how it **looks** is unconfirmed by
  eye.
* **Whether the workflow can actually commit** (§7.2) — repo-level Actions
  permission is unreadable with the available token, and the existing
  commit-back path has never executed.
* **Corpus representativeness.** A social-graph walk from one seed reaches
  leagues socially near that seed. The sample is not random and no claim is
  made that it is.
