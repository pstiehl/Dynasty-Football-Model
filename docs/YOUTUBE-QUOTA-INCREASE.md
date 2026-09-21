# Requesting a YouTube Data API quota increase

What the quota actually is, how an increase is really obtained (a compliance
audit, not a console button), what it would and would not buy this project,
and why the honest recommendation is to **not apply yet**.

**Current status: we have NOT applied for anything.** No audit submitted, no
form filled, no Google contact opened. This documents the path so a future
decision is informed — not so anyone treats the path as already started.

---

## 1. The quota model changed on 1 June 2026 — read this first

Most writing about YouTube quota on the open web, **and the module docstring in
[`src/dynasty/highlights.py`](../src/dynasty/highlights.py)**, describes a model
Google has since replaced. The old model was one pool of 10,000 units/day in
which `search.list` cost **100 units per call**. That is no longer how it
works, and the difference changes the design conversation.

From Google's revision history, dated **June 1, 2026**:

> The YouTube Data API is transitioning to a granular quota system covering
> smaller sets of methods, starting with `videos.insert` and `search.list`. […]
> API calls to the `videos.insert` and `search.list` methods will be charged to
> their own respective quota buckets. API calls to all other methods will be
> charged to the existing quota bucket.

— <https://developers.google.com/youtube/v3/revision_history>

The current default allocation, stated identically on two official pages:

> Projects that enable the YouTube Data API have a default quota allocation of
> **100 `search.list` calls, 100 `videos.insert` calls, and 10,000 units per day
> combined for all other endpoints.**

— <https://developers.google.com/youtube/v3/determine_quota_cost> and
<https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits>

So there are now **three separate buckets**, not one:

| Bucket | Default | Cost per call | What we use it for |
|---|---|---|---|
| `search.list` | **100 calls/day** | 1 | keyword discovery of clips |
| `videos.insert` | 100 calls/day | 1 | nothing — we never upload |
| everything else | **10,000 units/day** | 1 for our reads | `playlistItems.list`, `videos.list` |

The quota-cost table renders the `search.list` row as "100 quota per day. Each
call costs 1 quota", which is the same statement compressed. The practical
reading: **search is capped at 100 calls/day and can no longer be paid for out
of the 10,000-unit pool at all.** (`videos.batchGetStats`, added 3 June 2026,
also has its own bucket at 1 unit, 10,000/day default. We do not use it.)

**The old arithmetic is superseded but worth keeping visible**, since the
repository still contains it. Under the pre-June-2026 model, 10,000 ÷ 100 = 100
searches/day, and base-layer headroom put the practical ceiling near **60
searches per run** — exactly the number this codebase was designed around, and
a 10x extension to 100,000 units would have raised it to **900+**. Right when
written; no longer a description of the billing.

---

## 2. Quota is per Cloud project, not per API key

The most important structural fact for our design, and easy to get wrong
because a key *feels* like the unit of access.

Google's wording is "**Projects** that enable the YouTube Data API have a
default quota allocation of…", and usage is inspected per project on the
console's Quotas page. An API key is a credential belonging to a project; it
carries no allocation of its own. **Five API keys in one Cloud project share
one 100-call search budget and one 10,000-unit pool.** Minting keys buys
nothing.

The obvious follow-up — several Cloud projects, rotated — is **prohibited**.
API Services Terms of Service, §15:

> You and your API Client(s) will not, and will not attempt to, **exceed or
> circumvent** use or quota restrictions.

— <https://developers.google.com/youtube/terms/api-services-terms-of-service>

The ToS does not name multi-project rotation as an example, so this is a
reading of a general prohibition, not a quotation of a specific rule. Note also
that Google may reduce or eliminate a project's quota after **90 consecutive
days of inactivity** (Developer Policies) — a dormant spare is not a reserve.

---

## 3. How an increase is actually requested

Unlike most Google Cloud quotas, **there is no "request increase" button that
resolves itself.** The console path
(<https://console.cloud.google.com/iam-admin/quotas>, IAM & Admin → Quotas)
shows consumption and limits, and is the right place to confirm which bucket is
exhausted. It is not where an extension is granted.

Extensions route through a **compliance audit**:

> If you would like to request additional quota beyond the default allocation,
> you must first complete an audit to show that your project is in compliance
> with the YouTube API Services Terms of Service.

The form is the **YouTube API Services - Audit and Quota Extension Form** at
<https://support.google.com/youtube/contact/yt_api_form>. That URL was fetched
and returns a live form; it asks for full legal name, organisation legal name
(individuals write "self"), an organisation website that **must start with
`https://`**, legal address, business category, and primary plus technical
contacts, with a description of "your organization's work as it relates to
YouTube" at a **100-character minimum**.

Related forms, from the same official page:

| Situation | Form |
|---|---|
| First audit / quota extension | <https://support.google.com/youtube/contact/yt_api_form> |
| Further extension, audited within 12 months | same form |
| Appeal a failed audit | <https://support.google.com/youtube/contact/yt_api_appeals> |
| Periodic (YouTube-initiated) audit | same as the audit form |
| Change of control of the project | <https://support.google.com/youtube/contact/yt_api_change_of_control_form> |

Two conditions attach to any grant, per the Developer Policies: the extra quota
may be used **only for the approved use case**, and a change of use case
requires resubmitting an audit and being approved again.

---

## 4. What an applicant must have ready

From the Developer Policies, Branding Guidelines and Required Minimum
Functionality documents.

- **A working, publicly reachable implementation.** The form demands an
  `https://` organisation website; an audit of something reviewers cannot open
  is not plausible.
- **A written use case** — what the client does, who uses it, why the quota is
  needed. The 100-character floor is not the bar that matters.
- **A privacy policy** complying with Google's privacy policies, giving users
  control over their data and supporting deletion within 30 days.
- **Correct branding and attribution.** Logos must link back to YouTube content
  or a YouTube component, and **"YouTube", "YT" or "You-Tube" must never appear
  in the application's own name**.
  <https://developers.google.com/youtube/terms/branding-guidelines>
- **Required Minimum Functionality compliance**, chiefly around the embedded
  player: unmodified thumbnails and metadata, no blocking of standard player
  behaviour, correct `Referer` identification.
  <https://developers.google.com/youtube/terms/required-minimum-functionality>
- **Screenshots or a screencast.** Not published as a fixed requirement, but
  reviewers requesting a demo mid-review is commonly reported. Treat it as
  likely, not certain.

### 4.1 Two policy clauses this project would answer for

The Developer Policies tell developers not to "**merge or combine YouTube API
data with any other data**", and that where other sources appear alongside
YouTube API data, "**you must make the difference clear to the user**." Our
page places clips next to model output. The mitigation is presentational and
cheap — label the sources distinctly — but it is exactly what an audit looks
at, and should be fixed *before* applying rather than after a finding.

The same document prohibits assigning "custom scores to channels based on
independently calculated averages or ratios" and gamifying channel performance
by ranking channels against each other. We score **players**, not channels, so
the clause is not on its face about us. That distinction is ours, not Google's,
and has not been tested with a reviewer.

<https://developers.google.com/youtube/terms/developer-policies-guide>

---

## 5. Realistic expectations

**Google publishes no service-level commitment.** The official page says only
that "a member of YouTube's API Services team will contact you as soon as
possible". No turnaround time appears in any Google document read for this file.

Third-party accounts — vendor blogs and Stack Overflow questions, not Google —
describe waits of several weeks to a few months, reviews that stall without
explanation, and approvals granted below the amount requested. They are
consistent with each other but **not authoritative, and no specific number
should be quoted from this file as fact.** Commonly cited rejection themes are
bulk data harvesting, scraping, and competitive-analytics use cases.

Honest summary: an unknown wait measured in weeks at best, a real chance of
rejection, a grant possibly smaller than asked. Nothing here should assume an
extension arrives.

---

## 6. What an increase would and would not fix for us

**It would lift exactly one thing: the 100 `search.list` calls per day.**

Under the granular model the two cheap calls are not merely affordable, they
are nowhere near the ceiling. `playlistItems.list` and `videos.list` each cost
**1 unit per page of up to 50 results** against the shared 10,000-unit pool.
Ten thousand pages is on the order of **half a million video records per day**.
The curated-channel base layer that `highlights.py` already implements **cannot
realistically exhaust its bucket**, and an extension for it would solve a
problem we do not have. It follows that a *rejected* search extension leaves
the base layer untouched — the cheap path is not at risk either way.

Search is the whole constraint, and the constraint is now a **call count, not a
unit price**. Whether a run uses one search or the budget's worth, each search
is one of a hundred for the entire project for the entire day — shared across
every user, page load and scheduled refresh. Spending discipline matters more
under the new model than the old one, not less.

---

## 7. The alternatives, which are the actual recommendation

Applying is the expensive option: weeks of latency, an audit, an uncertain
outcome, ongoing obligations. All three mitigations below are available now,
need no permission, and together very likely remove the need to apply.

**Permanent per-(player, week) caching.** A player's week-N clips do not change
once week N is over. That makes the result a permanent fact, not a cache entry
with a sensible expiry: store it once, never search for it again. The corpus of
(player, week) pairs is bounded and mostly historical, so search spend falls
toward zero as coverage accumulates — a one-off backfill paced across days,
not a recurring bill.

**Curated-channel RSS, which is free.** Every channel publishes an Atom feed:

```
https://www.youtube.com/feeds/videos.xml?channel_id=UC...
```

Verified against the NFL channel (`UCDVYQ4Zhbm3S2dlz7P1GBDg`) while writing
this document: **HTTP 200, no API key, no OAuth, no quota consumed, 15
`<entry>` elements** carrying video id, title, publication date and thumbnail.
It returns only the 15 most recent uploads, so it is a freshness mechanism and
not a back-catalogue one — precisely the shape of the problem that otherwise
burns search on recent games.

**Prioritise the scarce search budget.** With 100 calls/day for the whole
project, searches should go to rostered players and high model ranks first, and
that ordering should be explicit in code rather than incidental to iteration
order. A hundred well-chosen searches per day is a meaningfully different
product from a hundred arbitrary ones.

---

## 8. Verification status

**Verified by fetching the source:** the June 1, 2026 granular-quota transition
and the June 3, 2026 `videos.batchGetStats` bucket, from the official revision
history; the current default allocation, identical on the quota-cost and
quota-and-compliance-audits pages; per-call costs of `playlistItems.list` and
`videos.list` (1 unit each); that the audit form is live at the cited URL and
asks for the §3 fields, plus the appeals, periodic-audit and change-of-control
URLs; ToS §15 anti-circumvention wording; the 12-month re-audit window,
approved-use-case restriction and 90-day inactivity clause, from the Developer
Policies; branding and Required Minimum Functionality obligations; and that
channel RSS returns 200 with 15 entries and no credential (§7).

**Not verified, and marked uncertain above:**

- **Review turnaround.** Google publishes none; the weeks-to-months range in §5
  is third-party reporting only.
- **Rejection rates and reasons.** No official statistics; §5 reflects reported
  themes, not measured outcomes.
- **Whether a screencast is formally required** (§4) — reported, not documented.
- **Whether player-level scoring sits outside the channel-scoring prohibition**
  (§4.1). Our reading, untested with a reviewer.
- **Multi-project rotation as a named ToS violation** (§2) — the general clause
  is quoted; the application is inferred.
- **What extension size Google would grant**, not published, apparently set
  case by case.
