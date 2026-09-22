"""Client JS that offers a scored league to the cross-league corpus backend.

This is the browser half of the design described in ``docs/CORPUS-BACKEND.md``.
The short version of why it is shaped like this:

Scoring one dynasty league costs ~97 Sleeper calls, because transactions are
paged per week across every season in the league's history chain. A Cloudflare
Worker that crawled server-side would spend all ~97 as *external* subrequests
in one invocation and blow the Workers Free ceiling of 50. The browser already
does that crawl today — Sleeper is CORS-friendly, and the Manager Score page
has always read leagues live — so the crawl stays exactly where it already is,
and the worker only ever *verifies*.

So this module does one thing: after ``msRun`` has scored a league, it packages
the result and POSTs it. The worker then independently re-reads three Sleeper
endpoints to decide whether to believe it.

Two invariants this file must not break
---------------------------------------
1. **It never invents numbers.** Every figure posted comes from
   ``msScoreLeague``'s return value. Nothing is recomputed here, because a
   second implementation would drift from the first.

2. **It never posts a set of managers whose z-scores would no longer be a
   z-distribution.** The worker rejects a payload whose per-component z
   scores do not have mean 0 and sd 1 — that check is most of what makes
   fabrication expensive, so it has to hold for honest submissions too.
   ``msScoreLeague`` computes z over the managers with *evidence* in a
   component, so dropping a roster that has no evidence anywhere is safe and
   dropping one that has any is not. ``csEligibility`` enforces exactly that
   distinction rather than approximating it.

Token-substitution convention matches ``managerscore_js.py`` and ``reel.py``:
plain string, ``__TOKEN__`` replacement at build time.
"""
from __future__ import annotations

CORPUS_SUBMIT_JS = r"""
/* ============================================================
 * Cross-league corpus — client submit.
 *
 * Loaded after MANAGERSCORE_UI_JS. Defines csOnScored(), which
 * managerscore_js.py calls once a league has been scored. If this
 * script is absent, or no corpus URL was configured at build time,
 * the Manager Score page behaves exactly as it always has.
 * ============================================================ */

var CS_URL = '__CORPUS_URL__';          /* '' when unconfigured */
var CS_SUBMIT_ISSUE_URL = '__SUBMIT_ISSUE_URL__';  /* the honest fallback */
var CS_SCHEMA = 'dfm.corpus.submission.v1';
var CS_MAX_ORPHAN_ROSTERS = 2;          /* must match the worker */

var CSX = { last: null, inFlight: false };

function csEl(id) { return document.getElementById(id); }

function csEsc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function csEnabled() {
  return typeof CS_URL === 'string' && /^https?:\/\//.test(CS_URL);
}

/* A Sleeper user id is a numeric snowflake. msBuildInput() falls back to
 * 'roster:<leagueId>:<rosterId>' for a roster nobody owns; those are not
 * people and the worker refuses them. */
function csIsSleeperId(s) {
  return typeof s === 'string' && /^[0-9]{6,24}$/.test(s);
}

/* ------------------------------------------------------------ eligibility */

/* Decide whether this scored league can be offered at all, and which manager
 * rows may be dropped without invalidating the z-distribution.
 *
 * Returns { ok, reason, managers } where `managers` is the exact set to post.
 */
function csEligibility(result, chain) {
  if (!result || !result.managers || !result.managers.length) {
    return { ok: false, reason: 'nothing was scored' };
  }
  if (!chain || !chain.length) {
    return { ok: false, reason: 'no league object' };
  }
  var league = chain[0];
  if (!league || !csIsSleeperId(String(league.league_id || ''))) {
    return { ok: false, reason: 'no usable league id' };
  }

  /* The board is a DYNASTY board. settings.type == 2 is the empirical
   * dynasty marker (see docs/CROSS-LEAGUE-CORPUS.md section 3) — checking it
   * here means a redraft league never costs the worker a round trip. */
  var type = league.settings && league.settings.type;
  if (type !== 2) {
    return { ok: false, reason: 'not a dynasty league (settings.type ' +
             (type == null ? 'absent' : type) + ')' };
  }

  var comps = ['draft', 'trade', 'waiver'];
  var keep = [], dropped = 0, blocked = null;
  result.managers.forEach(function (m) {
    if (csIsSleeperId(String(m.id || ''))) { keep.push(m); return; }
    /* An unowned roster. Safe to drop ONLY if it contributed no evidence to
     * any component — otherwise it is inside the z pools and removing it
     * would change the distribution the worker checks. */
    var hasEvidence = comps.some(function (c) {
      return m[c] && m[c].n > 0;
    });
    if (hasEvidence) { blocked = m.id; return; }
    dropped += 1;
  });

  if (blocked) {
    return { ok: false, reason:
      'a roster with no Sleeper owner has scored transactions, so this ' +
      'league cannot be indexed without misstating the numbers' };
  }
  if (dropped > CS_MAX_ORPHAN_ROSTERS) {
    return { ok: false, reason: dropped + ' rosters have no Sleeper owner' };
  }
  if (keep.length < 2) {
    return { ok: false, reason: 'fewer than two identifiable managers' };
  }
  return { ok: true, managers: keep, league: league, dropped: dropped };
}

/* ------------------------------------------------------------- payload */

/* Package a msScoreLeague() result for the worker.
 *
 * Pure: same inputs, same bytes. Everything numeric is copied straight from
 * `result` — this function must never compute a score. */
function csBuildSubmission(result, chain) {
  var elig = csEligibility(result, chain);
  if (!elig.ok) return { ok: false, reason: elig.reason, payload: null };

  var league = elig.league;
  var lineage = chain.map(function (l) { return String(l.league_id); })
                     .filter(csIsSleeperId);

  var comps = ['draft', 'trade', 'waiver'];
  var managers = elig.managers.map(function (m) {
    var out = {
      manager_id: String(m.id),
      display_name: String(m.name == null ? m.id : m.name),
      index: m.index,
      rank: m.rank == null ? null : m.rank,
      composite: m.composite,
      components: {},
      flags: (m.flags || []).slice(0, 8)
    };
    comps.forEach(function (c) {
      var src = m[c] || {};
      out.components[c] = {
        n: src.n || 0,
        z: src.z || 0,
        mean: src.mean || 0,
        shrunk: src.shrunk || 0
      };
    });
    return out;
  });

  return {
    ok: true,
    reason: null,
    payload: {
      schema: CS_SCHEMA,
      league: {
        league_id: String(league.league_id),
        name: String(league.name || league.league_id),
        season: String(league.season || ''),
        n_teams: Number(league.total_rosters || managers.length + elig.dropped),
        n_seasons_scored: lineage.length,
        lineage_ids: lineage,
        lineage_root: lineage.length ? lineage[lineage.length - 1]
                                     : String(league.league_id)
      },
      managers: managers
    }
  };
}

/* ------------------------------------------------------------------- UI */

/* The honest state machine. Names match what the worker actually returns, so
 * the page cannot claim a state the backend does not have:
 *
 *   off         no corpus backend configured in this build
 *   ineligible  we will not offer this league, and why
 *   verifying   posted; the worker is checking it against Sleeper
 *   provisional verified by the worker, NOT yet on the public board
 *   confirmed   the nightly job re-scored it server-side and agreed
 *   rejected    refused, and why
 *   error       we could not reach the backend
 */
function csRender(state, detail) {
  var box = csEl('ms-corpus-state');
  if (!box) return;
  box.style.display = 'block';

  var cls = 'callout', html = '';
  if (state === 'ineligible') {
    html = '<strong>Not added to the cross-league board.</strong> ' +
      csEsc(detail || '');
  } else if (state === 'verifying') {
    html = '<strong>Verifying…</strong> Sent this league\u2019s scores to the ' +
      'index. The server is re-reading the league from Sleeper to check them.';
  } else if (state === 'provisional') {
    cls = 'callout';
    html = '<strong>Indexed \u2014 provisional.</strong> The server confirmed ' +
      'this is a real dynasty league and that these managers are in it. The ' +
      'scores themselves are still the ones your browser computed, so the ' +
      'league is <em>not</em> on the public cross-league board yet. The ' +
      'nightly job re-scores it from Sleeper and, if it agrees, promotes it.' +
      (detail && detail.league_id
        ? ' <a href="#" id="ms-corpus-recheck" data-lid="' +
          csEsc(detail.league_id) + '">Check again</a>.'
        : '');
  } else if (state === 'confirmed') {
    html = '<strong>Indexed \u2014 confirmed.</strong> This league was ' +
      're-scored server-side and is on the public ' +
      '<a href="crossleague.html">cross-league board</a>.';
  } else if (state === 'rejected') {
    cls = 'callout callout-warn';
    html = '<strong>Not indexed.</strong> ' + csEsc(detail || 'The server ' +
      'declined this submission.');
  } else if (state === 'error') {
    cls = 'callout callout-warn';
    html = '<strong>Could not reach the index.</strong> ' + csEsc(detail || '') +
      ' Your score above is unaffected \u2014 it was computed in your browser.';
  } else if (state === 'off') {
    /* No backend is deployed. Say so, rather than rendering nothing.
     *
     * Rendering nothing was the bug: scoring a league here produces a full
     * table and a "Score this league" button that visibly did something,
     * so a visitor reasonably concludes their league was submitted. It was
     * not, and nothing on the page said otherwise. An owner reported
     * exactly that -- submitted a league, clicked the button, saw the
     * score, and nothing was recorded anywhere.
     *
     * This is not an error state and must not be styled as one: nothing
     * failed, the feature simply is not offered here. */
    html = '<strong>This score was not recorded.</strong> It was computed ' +
      'in your browser and sent nowhere \u2014 this site has no submission ' +
      'backend configured, so scoring a league here cannot add it to the ' +
      '<a href="crossleague.html">cross-league board</a>.' +
      (CS_SUBMIT_ISSUE_URL
        ? ' To get this league indexed, <a href="' +
          csEsc(CS_SUBMIT_ISSUE_URL) + '" target="_blank" rel="noopener">' +
          'submit its id as a GitHub issue</a>; a daily job validates it ' +
          'against Sleeper and adds it to the crawl.'
        : '');
  } else {
    box.style.display = 'none';
    return;
  }
  box.className = cls;
  box.innerHTML = html;

  var again = csEl('ms-corpus-recheck');
  if (again) {
    again.addEventListener('click', function (ev) {
      ev.preventDefault();
      csCheckStatus(again.getAttribute('data-lid'));
    });
  }
}

/* --------------------------------------------------------------- network */

function csPostJSON(url, body) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(function (res) {
    return res.text().then(function (t) {
      var parsed = null;
      try { parsed = JSON.parse(t); } catch (e) { parsed = null; }
      return { status: res.status, body: parsed, raw: t };
    });
  });
}

function csCheckStatus(leagueId) {
  if (!csEnabled() || !csIsSleeperId(String(leagueId))) return Promise.resolve(null);
  return fetch(CS_URL + '/corpus/league/' + encodeURIComponent(leagueId))
    .then(function (res) { return res.json().catch(function () { return null; }); })
    .then(function (body) {
      if (!body) return null;
      if (body.state === 'confirmed') csRender('confirmed', body);
      else if (body.state === 'provisional') csRender('provisional', body);
      else if (body.state === 'rejected') {
        csRender('rejected', body.reconcile_notes ||
          'The nightly re-score did not agree with the submitted numbers.');
      } else if (body.state === 'unverified') {
        csRender('rejected', 'Stored but unverified, so it stays off the ' +
          'public board.');
      }
      return body;
    })
    .catch(function () { return null; });
}

/* The hook managerscore_js.py calls after a successful score. */
function csOnScored(leagueId, result, chain) {
  if (!csEnabled()) { csRender('off'); return Promise.resolve(null); }

  var optIn = csEl('ms-corpus-optin');
  if (optIn && !optIn.checked) {
    csRender('ineligible', 'You left \u201cadd this league to the ' +
      'cross-league index\u201d unticked.');
    return Promise.resolve(null);
  }

  var built = csBuildSubmission(result, chain || []);
  if (!built.ok) { csRender('ineligible', built.reason); return Promise.resolve(null); }
  if (CSX.inFlight) return Promise.resolve(null);

  CSX.inFlight = true;
  CSX.last = built.payload;
  csRender('verifying');

  return csPostJSON(CS_URL + '/corpus/submit', built.payload)
    .then(function (res) {
      CSX.inFlight = false;
      var body = res.body || {};
      if (res.status === 202 && body.state === 'provisional') {
        csRender('provisional', body);
      } else if (res.status === 200 && body.duplicate) {
        if (body.state === 'confirmed') csRender('confirmed', body);
        else csRender('provisional', body);
      } else if (res.status === 501) {
        csRender('ineligible', 'The cross-league index is not switched on yet.');
      } else if (res.status === 429 || res.status === 503) {
        csRender('error', body.message ||
          'The index is rate limited right now. Nothing is lost \u2014 try later.');
      } else {
        csRender('rejected', (body.errors && body.errors.length)
          ? body.errors.slice(0, 3).join('; ')
          : (body.message || ('HTTP ' + res.status)));
      }
      return body;
    })
    .catch(function (e) {
      CSX.inFlight = false;
      csRender('error', String(e && e.message || e));
      return null;
    });
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports.csBuildSubmission = csBuildSubmission;
  module.exports.csEligibility = csEligibility;
  module.exports.csOnScored = csOnScored;
  module.exports.csRender = csRender;
  module.exports.CSX = CSX;
}
"""


def corpus_submit_js(corpus_url: str = "", submit_issue_url: str = "") -> str:
    """Return the submit script with the build-time URLs spliced in.

    ``corpus_url`` empty (the default, and what happens when the owner has
    not deployed the D1 half) yields a script whose ``csEnabled()`` is false.
    The page then **says so** after scoring a league, which is the change
    from the original behaviour: it used to render nothing at all.

    Saying nothing was not neutral. The section still scored the league and
    still drew a full table, so the absence of any statement read as
    success -- and the one thing a visitor wants to know after clicking
    "Score this league" is whether the league is now on the board. It was
    not. ``submit_issue_url`` is what makes the honest message actionable:
    the issue-template path is the only inbox a static site actually has,
    and it works today, so the message points at it instead of leaving the
    visitor with a dead end.
    """
    def _clean(value: str) -> str:
        v = (value or "").strip().rstrip("/")
        if v and not v.startswith(("http://", "https://")):
            return ""
        # Build-time constants from the owner's own environment, not user
        # input; still quote-stripped so a malformed value cannot break out
        # of the JS string literal.
        return v.replace("\\", "").replace("'", "").replace('"', "")

    return (CORPUS_SUBMIT_JS
            .replace("__CORPUS_URL__", _clean(corpus_url))
            .replace("__SUBMIT_ISSUE_URL__", _clean(submit_issue_url)))
