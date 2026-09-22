# Can a visitor submit a league? — current status

Short answer, measured on 2026-09-22: **no backend submission is accepted,
and the page now says so.** The GitHub-issue path is the one that works, and
it is what the page points at.

This document exists because the difference between "submissions are off"
and "submissions are on" is a single repository variable that nobody can
infer by reading the site, and because the previous behaviour when it was
off was to say nothing at all.

## 1. What was wrong

`scripts/cf-worker` (the corpus backend, see `CORPUS-BACKEND.md`) is written
but not deployed. The site build reads `vars.CORPUS_URL` into
`DFM_CORPUS_URL`; with it unset, `corpus_submit_js` emits a script whose
`csEnabled()` is false.

Measured:

```
$ gh api repos/pstiehl/Dynasty-Football-Model/actions/variables
{"variables":[],"total_count":0}
```

So `CORPUS_URL` is unset, and every submission path through the worker is
inert.

The defect was not that it is inert — that is the correct state for an
undeployed backend. The defect was that **`csOnScored` hid the status box
entirely**, so nothing on the page said anything. The Manager Score section
still scored the league and still drew a full table, so a visitor who
clicked "Score this league" saw something obviously happen and reasonably
concluded their league had been added to the board.

That is what happened to the owner: submitted a league, clicked "Score this
league", saw `You already loaded league 1316222126914539520 on this page`
and a full score table, and nothing was recorded anywhere.

Silence about a missing backend is indistinguishable from success.

## 2. What the page does now

With no `CORPUS_URL`, scoring a league renders, in the `ms-corpus-state`
box:

> **This score was not recorded.** It was computed in your browser and sent
> nowhere — this site has no submission backend configured, so scoring a
> league here cannot add it to the cross-league board. To get this league
> indexed, submit its id as a GitHub issue; a daily job validates it
> against Sleeper and adds it to the crawl.

It is deliberately **not** styled as an error (`callout`, not
`callout-warn`). Nothing failed; the feature is simply not offered here.

Pinned by `tests/js/myteam_script_order_tests.mjs` (the unconfigured block)
and `tests/test_corpus_backend.py::ClientSubmitTests`. The old assertion
that the box "stays hidden" was removed in the same change — it encoded the
defect.

## 3. The fallback, and whether it actually works

Yes, it works. The chain is:

```
crossleague.html link  ->  .github/ISSUE_TEMPLATE/league-submission.yml
  ->  label: league-submission  (declared by the template, so it applies to
      submitters with no repo access)
  ->  .github/workflows/league-submissions.yml  (on: issues, + daily sweep)
  ->  scripts/ingest_league_submissions.py
  ->  data/cross_league/seeds.json  ->  next crawl
```

Verified on 2026-09-22:

* the `league-submission` label exists on the repository, as do
  `league-accepted` / `league-rejected`;
* the workflow exists and its most recent scheduled run concluded
  `success`;
* `extract_league_id` pulls the id out of a real issue-form body, and
  rejects `` `rm -rf /` ``, `${{ secrets.GITHUB_TOKEN }}`, `12` and
  `not-a-number` (only 6–24 digits survive);
* `validate_league("1316222126914539520")` returns
  `True, "validated against Sleeper as an NFL dynasty league"`.

**What is not proven:** no league-submission issue has ever been filed
(`state=all` returns zero), so the GitHub-write half — comment, close,
commit to `seeds.json` — has never executed against a real issue. The
parsing and validation half is exercised above; the write half is
`contents: write` + `issues: write` in a workflow that runs green but has
never had an issue to act on. The first real submission is also the first
end-to-end test of that half.

## 4. Turning real submissions on

This PR deploys nothing and invents no URL. That is the owner's decision,
and it costs a Cloudflare account.

The full procedure is `CORPUS-BACKEND.md` §9 and is not duplicated here.
The two-line summary:

1. Deploy the worker in `scripts/cf-worker` (create the D1 database, bind
   it, run `migrations/0001_corpus_init.sql`, set the `IP_HASH_SALT` and
   `RECONCILE_TOKEN` secrets, `npx wrangler deploy`).
2. In **Settings → Secrets and variables → Actions**, set:
   * **Variable** `CORPUS_URL` = the deployed worker URL, no trailing slash
     — this is the one that flips the UI;
   * **Secret** `CORPUS_RECONCILE_TOKEN` = the same value given to
     `wrangler secret put RECONCILE_TOKEN`.

With `CORPUS_URL` set, the opt-in checkbox appears, `csEnabled()` becomes
true, and the "not recorded" copy above is replaced by the real
verifying/provisional/confirmed lifecycle. A submitted league still stays
off the public board until `scripts/reconcile_corpus.py` re-scores it
server-side and agrees — see `CORPUS-BACKEND.md` §4.

Setting `CORPUS_URL` to anything that is not `http(s)://` is ignored and
treated as unset, so a typo degrades to the honest message rather than to a
broken fetch.

## 5. Related: no other inert controls

Swept on 2026-09-22 for controls whose backend might be unconfigured while
the UI still looked successful. `CS_URL` was the only one. Every other
interactive control on the site either reads Sleeper live in the visitor's
browser (`ms-load-user`, `ms-load-league`, `load-user`, `load-league`,
`load-custom`) or is pure client-side rendering over a static artifact
(the sort and format buttons, and `crossleague_js`'s fetch of
`crossleague_corpus.json`, which the build always writes). None of those
depend on a deployment that may be absent.
