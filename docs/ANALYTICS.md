# Web analytics

The site is static on GitHub Pages. GitHub Pages gives you **no server logs
at all** — no request count, no referrer, no country, nothing. Until this
was wired up, the only evidence anybody had ever visited the site was
somebody telling you they had.

This document covers what got added, what you have to do to switch it on,
how to check it worked, and — the part that matters most before you make
decisions on it — **what it cannot tell you**.

---

## 1. Why Cloudflare Web Analytics

| | |
|---|---|
| **Cost** | Free, with no page-view cap on the free tier. |
| **Cookies** | None. It sets no cookie and stores no identifier on the visitor's device. |
| **Consent banner** | Not required. No cookies and no cross-site identifier means no GDPR/ePrivacy consent gate. Nothing to click through, nothing to maintain. |
| **Account** | You already have one, with a Worker deployed (`scripts/cf-worker/`). No new vendor, no new bill, no new login. |
| **Weight** | One `defer`red external script. It cannot block rendering. |

The realistic alternatives were Plausible (~$9/month) and Fathom
(~$15/month). Both are good and both are also cookieless, but neither does
anything Cloudflare's free tier does not do for this site's needs, and both
add a vendor relationship. Google Analytics was never a candidate: it needs
a consent banner, it is heavy, and pointing it at a hobby dynasty-football
site means handing Google the visitor log for no return.

**None of this is locked in.** See §7 — switching is a config change.

---

## 2. Switching it on

Two things: get a token from Cloudflare, put it in a GitHub secret. Five
minutes, once.

### 2.1 Get the beacon token from Cloudflare

The site is on `pstiehl.github.io`, which is **not proxied through
Cloudflare**, so this is the manual-snippet path:

1. Sign in to the Cloudflare dashboard: <https://dash.cloudflare.com>
2. Go to **Web Analytics** in the left sidebar.
   (Direct link: <https://dash.cloudflare.com/?to=/:account/web-analytics>)
3. Select **Add a site**.
4. Under **Set up hostname**, type `pstiehl.github.io`.
5. Click the message box that appears to confirm that hostname, then
   select **Done**.
6. You are now shown a **JS snippet** on the **Manage site** screen. It
   looks like this:

   ```html
   <script defer src='https://static.cloudflareinsights.com/beacon.min.js'
     data-cf-beacon='{"token": "PASTE-YOUR-OWN-32-CHAR-TOKEN"}'></script>
   ```

   (Yours will have a real value there: 32 hexadecimal characters,
   `0`–`9` and `a`–`f`.)

7. **Copy only the token** — the 32-character hex string inside the quotes
   after `"token":`. Not the whole snippet. The build already knows the
   snippet's shape; it just needs the token.

Do not paste the snippet into any file in this repo. The whole point of
the wiring below is that the token never lands in git.

> **A note on the hostname.** Cloudflare keys a site by *hostname*, and
> `pstiehl.github.io` is your whole GitHub Pages user domain, not just this
> project. In practice only pages that actually carry the beacon report
> anything, so today that is exactly this site. But if you later publish a
> second project to `pstiehl.github.io` and give it the same token, the two
> will be pooled into one set of numbers. If that happens, either use a
> second Cloudflare site/token, or filter by path in the dashboard.

### 2.2 Put the token in a GitHub repository secret

1. Go to the repository on GitHub:
   <https://github.com/pstiehl/Dynasty-Football-Model>
2. **Settings** → **Secrets and variables** → **Actions**.
3. Stay on the **Secrets** tab and select **New repository secret**.
4. **Name:** `CF_ANALYTICS_TOKEN`
   **Secret:** paste the token from step 2.1.7.
5. **Add secret**.

That is everything. The next scheduled build picks it up. To see it sooner,
go to **Actions** → **Daily refresh and publish** → **Run workflow**.

### 2.3 The names, in one place

| Name | Where | What it is |
|---|---|---|
| `CF_ANALYTICS_TOKEN` | GitHub **repository secret** | The Cloudflare beacon token from §2.1. **This is the one you must set.** |
| `ANALYTICS_PROVIDER` | GitHub **repository variable** (optional) | `cloudflare` (default), `plausible`, `fathom`, or `none`. Leave unset. |
| `DFM_ANALYTICS_TOKEN` | Build **environment variable** | What the workflow sets the secret to. You never set this by hand except for a local test. |
| `DFM_ANALYTICS_PROVIDER` | Build **environment variable** | Set from `ANALYTICS_PROVIDER`. |

The secret and the env var have different names on purpose: the secret name
says whose token it is, the env var name says which knob it turns.
`.github/workflows/daily-refresh.yml` maps one to the other, following the
same pattern already used for `YOUTUBE_API_KEY`.

---

## 3. Verifying it actually works

Do these in order. Each one rules out a different failure.

**A. The build emitted it.** After a deploy, open
<https://pstiehl.github.io/Dynasty-Football-Model/rankings.html>, view
source (`Ctrl-U` / `Cmd-Opt-U`), and search for `cloudflareinsights`. You
should find exactly one line, in `<head>`, with your token in it. If it is
missing, the secret is not set or is misnamed — check the Actions log for
the line `analytics: DFM_ANALYTICS_TOKEN is set but is not a valid token`.

**B. The beacon is actually firing.** Open the site with DevTools →
**Network** tab, reload, and filter for `beacon`. You want:
- `beacon.min.js` — status 200 (the script loaded), and
- a request to `cloudflareinsights.com/cdn-cgi/rum` — status 204 (the page
  view was *reported*).

The second one is the real test. The first only proves the file downloaded.

**C. Cloudflare received it.** Back in **Web Analytics**, select your site.
Data takes a few minutes to appear on a new site. If A and B pass and this
is still empty after ~15 minutes, the token is valid-looking but wrong —
re-copy it from **Manage site**.

**D. It is on every page, not just the front one.** Check a player page
too, e.g. any link from the rankings table into `players/`. Those are
rendered by the same function, so if one has it they all do, but it costs
ten seconds to confirm.

> Your own visits are counted. Cloudflare Web Analytics has no
> "exclude me" switch, because it has no way to know who you are — that is
> the flip side of it not tracking anybody. For the first few weeks, assume
> a chunk of the traffic is you. A browser extension that blocks
> `static.cloudflareinsights.com` is the simplest fix.

---

## 4. What it measures

- **Page views** and **visits**, per page path, over time.
- **Referrers** — where a visitor came from. This is the single most useful
  field you are currently missing: it is how you find out that a Reddit
  thread or a Sleeper Discord is sending you people.
- **Country**, derived from the connecting IP at request time.
- **Browser**, **OS**, **device type**.
- **Core Web Vitals** — real-world load performance.

## 5. What it does *not* measure

- **No cookies, no visitor ID, no cross-session identity.** You cannot ask
  "how many *people*", only "how many visits". A visitor who returns
  tomorrow is a new visit and is not linkable to today's.
- **No individual visitors.** There is no user list and never will be.
- **No path through the site.** No funnels, no session replay, no
  "they read rankings then went to Best Managers".
- **No events.** Nothing is instrumented — no "clicked a highlight", no
  "scored a league". Page views only.
- **Anything with JavaScript off, or an ad blocker on, is invisible.**
  This is a real undercount, not a rounding error: `cloudflareinsights.com`
  is on common blocklists, and this site's audience skews technical.
  Treat the numbers as a **floor and a trend line**, never a census.
- **Bots are filtered by Cloudflare's own heuristics**, which are neither
  perfect nor inspectable.

---

## 6. What analytics cannot tell you — including the corpus question

This section exists because of a specific idea worth putting to rest.

**The cross-league corpus is not a traffic signal.** The hypothesis was
that growth in corpus data could reveal organic visitors. It cannot, and
the reason is in how the corpus is populated:

1. **Almost all of it is a server-side crawl.** `scripts/crawl_cross_league.py`
   runs in GitHub Actions on a schedule and walks Sleeper's social graph —
   league → members → their leagues. Those leagues are discovered by *our
   own crawler*, on a timer, whether or not a single human loads the site.
   Corpus growth of that kind is a measure of the crawl budget, nothing
   else. See `docs/CROSS-LEAGUE-CORPUS.md`.

2. **The visitor-submitted part is opt-in, and small.** A visitor who
   scores their league on the Manager Score page may *tick a box* to submit
   it (`docs/CORPUS-BACKEND.md`). It is off by default. So a submission
   means "somebody visited **and** scored a league **and** chose to share
   it" — three conditions deep. You cannot invert that into a visitor
   count, and the conversion rate from "visit" to "submitted" is both
   unknown and certainly tiny.

3. **A league is not a person.** One manager submitting one league adds up
   to twelve managers to the board. None of the other eleven visited.

So: corpus size measures crawl reach plus a sliver of opt-in sharing.
Traffic measures traffic. They are different quantities, and no amount of
arithmetic converts one into the other. That is exactly the gap the beacon
fills.

**Other things no page-view analytics can tell you**, worth being clear
about before any monetisation decision rests on them:

- **Why** anyone came, or whether they found what they wanted.
- Whether a visit was a real reader or thirty seconds of a bounced tab.
- Whether the same person came back next week.
- Who they are, in any form. There is no email, no identity, no
  remarketing audience. If monetisation later needs any of that, it needs a
  *different* mechanism with its own consent story — not this beacon.
- Anything at all about the period before the token is set. There is no
  backfill and never can be — the data was never collected. Measurement
  starts the day you switch it on, which is the single best argument for
  doing it now rather than when it first feels needed.

---

## 7. Swapping the provider

Nothing outside `src/dynasty/analytics.py` knows the site uses Cloudflare.
`report._page` asks for "the analytics snippet" and emits whatever comes
back.

To move to Plausible:

1. Set repository variable `ANALYTICS_PROVIDER` to `plausible`.
2. Set repository secret `CF_ANALYTICS_TOKEN` to the registered domain
   (for Plausible the "token" is the domain; the env var is provider-neutral
   even though the secret keeps its Cloudflare-era name).

Same shape for `fathom` with a site id. `none` keeps the secret in place
while emitting nothing — useful for answering "is analytics causing this?"
without deleting anything.

Adding a fourth provider is one function and one dict entry in
`analytics.py`. The one constraint, enforced by the tests: a provider must
emit a **single `defer`red external script**, because that is what makes the
`<head>` position safe (§8).

---

## 8. Where the beacon is emitted, and why there

It is the **last element in `<head>`**, after the `window.DFM_BASE`
bootstrap, injected by `report._page` so that every page gets it —
including pages added later by someone who never reads this document.

Cloudflare's own instructions say to put the snippet before the closing
`</body>` tag. This build deliberately does not, and the reason is
[PR #71](https://github.com/pstiehl/Dynasty-Football-Model/pull/71): that
PR fixed a live outage in this exact function, where the shared highlight
renderer was emitted *after* the page body, so a page script that read
`DFMHL` while being evaluated threw
`TypeError: Cannot read properties of undefined` and rendered an empty
table. Script order in `_page` has already broken this site once.

`<head>` keeps the beacon completely outside the body's inline execution
sequence, so it cannot be inserted between two blobs that depend on each
other. And because it is `defer`red and *external*, the HTML spec requires
it to execute only after the document is parsed — it cannot run before,
between, or during any inline script on the page. The safety is structural,
not a property of where it happens to sit today.

This is verified by executing the pages, not by reading them
(`tests/test_analytics.py`), because the PR #71 bug was invisible to both
`node --check` and to reading the Python source.

## 9. Local builds and forks

Nothing changes. With `DFM_ANALYTICS_TOKEN` unset — which is the case for
every local build, every fork and every PR — `_page` emits **no beacon
markup at all**: no script tag, no empty `src`, no placeholder, not even an
HTML comment. The rationale for the beacon's position lives in a Python
comment in `report.py`, deliberately, so that an unconfigured build ships
nothing.

This was measured, not assumed: the same page rendered by the pre-feature
`report.py` and by this one with no token set are **byte-for-byte
identical** (88,223 bytes). With a token set, the two differ by exactly one
line — the beacon.

The committed tests assert the durable half of that: with the beacon line
removed, a configured page equals an unconfigured page exactly
(`tests/support/render_analytics.py`). They do not re-derive the
pre-feature comparison, because after this merge `main` contains the
feature and the comparison would no longer mean anything.

To test the wiring locally without touching the deployed site:

```bash
DFM_ANALYTICS_TOKEN=0123456789abcdef0123456789abcdef python -m dynasty.launcher_headless
grep -c cloudflareinsights dynasty_site/rankings.html   # 1
```

Use a fake token like the one above. `dynasty_site/` is gitignored, so a
local build cannot commit one — but do not paste your real token into a
shell that writes history either.
