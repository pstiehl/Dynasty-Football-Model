# Dynasty Model worker

One Cloudflare Worker doing two jobs.

**1. Read proxy (original).** Proxies `api.myfantasyleague.com` so the
GitHub Pages site can query MFL leagues directly from the browser. MFL's
CORS policy only allows their own subdomains, so without this in between,
the browser blocks every cross-origin request from `pstiehl.github.io`.
It also proxies `api.sleeper.app` purely so we can add edge caching to the
slow `transactions/<week>` endpoint that the manager rankings feature walks
once per page load.

**2. Cross-league corpus backend (new, and OFF until you switch it on).**
Accepts a Manager Score result that the *browser* computed, verifies it
independently against Sleeper, and stores it in D1 so managers can be
compared across leagues. Full design, trust model, abuse controls and
licensing risk: **[`docs/CORPUS-BACKEND.md`](../../docs/CORPUS-BACKEND.md)**.

> The corpus half is inert without a D1 binding. `/health` reports
> `corpus: "disabled"`, every `/corpus/*` endpoint answers 501 with an
> explanation, and the proxy behaves exactly as it always has. Adding this
> feature cannot break your existing MFL proxy.

## What it does

```
GET https://dynasty-model-proxy.<subdomain>.workers.dev/health
  -> { status: "ok", time: "...", corpus: "enabled" | "disabled" }

GET .../mfl/2026/export?TYPE=league&L=12345&JSON=1
  -> proxies api.myfantasyleague.com/2026/export?TYPE=league&L=12345&JSON=1
  -> CORS-allowed for pstiehl.github.io
  -> cached at the edge for 5 minutes

GET .../sleeper/v1/league/12345/transactions/1
  -> proxies api.sleeper.app/v1/league/12345/transactions/1
  -> cached at the edge for 5 minutes
```

Corpus endpoints (need D1):

```
POST .../corpus/submit              browser submits a scored league
  -> 202 { state: "provisional" }   verified, but NOT yet public
  -> 422 / 429 / 413 / 403          see docs/CORPUS-BACKEND.md section 3.1

GET  .../corpus/leaderboard         confirmed leagues only
GET  .../corpus/leaderboard?include=provisional
GET  .../corpus/league/<league_id>  state of one league
GET  .../corpus/stats               counts and caps

GET  .../corpus/pending             Bearer RECONCILE_TOKEN
POST .../corpus/reconcile           Bearer RECONCILE_TOKEN
```

Only GET is allowed on the proxy paths. CORS origin allowlist is in
`worker.js` (`ALLOWED_ORIGINS`); the corpus POST enforces it strictly,
rather than falling back to the first allowed origin as the read proxy does.

## Deploy

You need:

1. A Cloudflare account (free tier is enough — 100k requests/day).
2. A Cloudflare API token scoped to **Workers Scripts: Edit** for your
   account. Create one at
   <https://dash.cloudflare.com/profile/api-tokens>.
3. Node 18+ and `npx`.

Then:

```bash
cd scripts/cf-worker
export CLOUDFLARE_API_TOKEN=<paste-your-token-here>
npx wrangler@latest deploy
```

After the first deploy, Wrangler prints the worker URL — something like
`https://dynasty-model-proxy.<your-subdomain>.workers.dev`. Plumb that
into the site build:

### Option A — set in GitHub Actions (recommended)

1. In the repo, go to **Settings → Secrets and variables → Actions →
   Variables tab** (not Secrets — the worker URL isn't sensitive).
2. Click **New repository variable**.
   - Name: `PROXY_URL`
   - Value: `https://dynasty-model-proxy.<your-subdomain>.workers.dev`
     (no trailing slash)
3. Edit `.github/workflows/daily-refresh.yml`. Add an `env:` block to
   the `Run the model end-to-end` step:
   ```yaml
   - name: Run the model end-to-end
     env:
       PROXY_URL: ${{ vars.PROXY_URL }}
     run: |
       python -m dynasty.launcher_headless
   ```
4. Commit + push. Next workflow run bakes the URL into the site.

### Option B — local builds

If you're building the site locally rather than via CI:

```bash
export PROXY_URL=https://dynasty-model-proxy.<your-subdomain>.workers.dev
python -m dynasty.launcher_headless
```

The site build reads `PROXY_URL` and bakes it into `league.html` as a
`data-proxy-url` attribute on the form. When set, the MFL form on the
page activates; when unset, the page tells the user MFL leagues require
the worker (or the `leagues.json` fallback).

## Switching on the corpus backend

One time, with your own Cloudflare login. **No API token is committed, and
nothing here is automated.**

```bash
cd scripts/cf-worker

# 1. create the database
npx wrangler d1 create dynasty-corpus

# 2. paste the printed database_id into wrangler.toml and uncomment the
#    [[d1_databases]] block

# 3. create the tables
npx wrangler d1 execute dynasty-corpus --remote \
  --file=migrations/0001_corpus_init.sql

# 4. two secrets (generate with: openssl rand -hex 32)
npx wrangler secret put IP_HASH_SALT
npx wrangler secret put RECONCILE_TOKEN

# 5. deploy and confirm
npx wrangler deploy
curl https://dynasty-model-proxy.<subdomain>.workers.dev/health
```

Then in GitHub → **Settings → Secrets and variables → Actions**:

* **Variables** tab → `CORPUS_URL` = the worker URL, no trailing slash.
* **Secrets** tab → `CORPUS_RECONCILE_TOKEN` = the same value you gave
  `wrangler secret put RECONCILE_TOKEN`.

The daily workflow reads both and reconciles submitted leagues nightly.
Without them the reconcile step exits 0 immediately.

Step-by-step detail and what each piece is for: `docs/CORPUS-BACKEND.md` §9.

## Local testing

```bash
cd scripts/cf-worker
npx wrangler@latest dev    # spins up the worker on http://localhost:8787
```

The worker's request/response contract is also covered without any
Cloudflare account at all — `tests/test_corpus_backend.py` drives it in node
against the real migration SQL through a D1-shaped shim over `node:sqlite`:

```bash
python3 -m unittest tests.test_corpus_backend -v
```

Then in another terminal:

```bash
curl -H "Origin: http://localhost:8000" http://localhost:8787/health
curl -H "Origin: http://localhost:8000" \
  "http://localhost:8787/mfl/2026/export?TYPE=league&L=12345&JSON=1" | head
```

## Cost

Free tier: 100,000 requests/day. A typical user pulling one MFL league
makes ~20 requests (league, rosters, draftResults, transactions). So one
user-pull-per-second sustained would consume the daily budget. For
Phil's use case (a handful of leagues, occasional clicks), free tier is
way over-provisioned.

The corpus backend also fits the free tier, and that is not an accident.
Scoring a league costs ~97 Sleeper calls, which is why the **browser**
crawls and the worker only verifies: a submission costs the worker **3
external subrequests**, against a free-tier ceiling of 50 per invocation.
A worker that crawled server-side would need ~97 and would fail on free.
D1 free tier is 5 GB per account / 500 MB per database, and a 12-team
league is ~13 rows.

See `docs/CORPUS-BACKEND.md` §1 for the numbers, including a correction
about which subrequest budget the 50 actually applies to.

## Operational notes

- The proxy paths have no authentication — they're a public proxy. The MFL
  endpoints we forward are public anyway (no cookie required for read-only
  data on public leagues). The corpus write paths are different: `submit`
  is origin-gated, rate limited, size capped and deduped, and
  `pending`/`reconcile` require a bearer token.
- Worker caches by upstream URL, so two users querying the same league
  ID within the 5-minute TTL share one upstream hit.
- Logs visible in the Cloudflare dashboard if you turn on the Logpush
  product (paid). For free, use `wrangler tail` in your terminal to
  stream logs from a running worker.
