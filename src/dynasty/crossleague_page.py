"""crossleague.html — the cross-league Manager Score board.

Renders ``crossleague_corpus.json``. The page itself scores nothing and
crawls nothing; see ``dynasty.crossleague_js`` for why that split is forced
rather than chosen.

The copy on this page is load-bearing. Two claims are easy to make here and
both would be false:

* that this ranks managers against *Sleeper*, rather than against the small
  set of leagues we happened to walk to, and
* that a high score means someone is a good manager in general, rather than
  that their transactions beat their own leaguemates' by KeepTradeCut's
  reckoning.

The coverage banner and the "what this cannot tell you" section exist to
stop both, and the coverage sentence is generated from the artifact's own
counts so it cannot drift away from the data.
"""
from __future__ import annotations

from datetime import datetime

_CROSSLEAGUE_CSS = """
.xl-sub { font-size: 12px; color: var(--muted); margin-top: 6px; }
.xl-n { font-size: 12px; color: var(--muted);
  font-variant-numeric: tabular-nums; }
.xl-name { font-weight: 600; }
.xl-rank { font-variant-numeric: tabular-nums; color: var(--muted);
  width: 38px; }
.xl-pos { color: #047857; font-weight: 600; }
.xl-neg { color: #b91c1c; font-weight: 600; }
.xl-flags { font-size: 11px; color: #92400e; max-width: 320px; }
.xl-coverage-head { font-size: 15px; font-weight: 700; }
.xl-table { width: 100%; border-collapse: collapse; margin: 10px 0; }
.xl-table th, .xl-table td { text-align: left; padding: 7px 10px;
  border-bottom: 1px solid var(--border); font-size: 13px; }
.xl-table th { font-size: 11px; text-transform: uppercase;
  letter-spacing: .04em; color: var(--muted); font-weight: 700; }
tr.xl-row:hover { background: var(--hover); cursor: pointer; }
.xl-breakdown { padding: 10px 4px 14px 4px; }
.xl-table.xl-inner th, .xl-table.xl-inner td { font-size: 12px;
  padding: 5px 8px; }
.xl-note { background: #f8fafc; border: 1px solid var(--border);
  border-radius: 8px; padding: 14px 18px; font-size: 13px; }
.xl-note code { background: #eef2ff; padding: 1px 5px; border-radius: 4px; }
.xl-board { margin: 6px 0 26px 0; }
h4 { font-size: 14px; margin: 18px 0 8px 0; }
"""


def _methodology_html() -> str:
    """Formula, limits, privacy stance and licensing — on the page.

    Deliberately on the page and not only in docs/: this board ranks named
    (if pseudonymous) people who did not ask to be ranked. The least we owe
    them is that anyone who finds themselves on it can read what was
    measured, what it cannot mean, and what we did and did not collect.
    """
    return """
<h2>How the <span class="accent">cross-league</span> score works</h2>

<div class="xl-note">
<p style="margin-top:0">The <a href="managerscore.html">Manager Score</a> page
ranks managers <em>inside</em> one league: mean 100, one standard deviation
15. That is useful, but it cannot compare leagues — the best manager in every
league scores about 115 by construction. This page aggregates that same
metric across every league we have indexed.</p>

<p><code>score = 100 + 15 × (w<sub>draft</sub>·Z<sub>draft</sub> +
w<sub>trade</sub>·Z<sub>trade</sub> + w<sub>waiver</sub>·Z<sub>waiver</sub>)</code></p>

<p>For each component, <code>Z</code> is built in two steps:</p>
<ol style="margin:0 0 10px 0">
<li><strong>Evidence-weighted average.</strong> Your within-league z-score in
each league, weighted by how many transactions it rests on. A league where you
made 30 picks counts ten times one where you made three — averaging leagues
equally would let a single thin league swing your whole record.</li>
<li><strong>Shrunk toward neutral</strong> by <code>N/(N+k)</code> on your
<em>total</em> transaction count, with the same constants the per-league
scorer uses (k = 6 picks, 3 trades, 5 adds).</li>
</ol>

<p>Shrinkage is applied at both levels on purpose. The per-league stage asks
"how much evidence inside this league?"; this stage asks "how much evidence
across the whole corpus?". They are different questions, and someone with one
draft in one league should be pulled toward the middle by both. Neither stage
can flip a sign or overshoot, so volume still cannot manufacture a good
score.</p>

<p>The score is <strong>not</strong> re-centred on the corpus. If it were,
everyone's number would move whenever an unrelated league was indexed, which
makes a score impossible to quote. The score is stable; the <em>rank</em> is
what carries the "against N managers" meaning.</p>

<h4>Why the draft board has an entry requirement</h4>
<p>A manager needs a minimum number of scored picks before appearing on the
draft-specific board. Shrinkage alone would still leave a one-pick manager
rankable, and "best drafter" computed off one pick is not an answer to the
question.</p>
</div>

<h2>What is <span class="accent">indexed</span>, and why</h2>
<div class="xl-note">
<p style="margin-top:0"><strong>Sleeper cannot be enumerated.</strong> There is
no API endpoint that lists leagues, and league ids are 19-digit numbers, so
there is no id space to sweep. The only way to find a league is to already
know its id, or to find it through a person: a league tells you its members,
and a member tells you their other leagues.</p>

<p>So the corpus is built by walking outward from a small set of known
leagues, a bounded number of hops, under a hard cap on both leagues and API
calls. Every run reports the budget it used. <strong>This is a sample, and a
small one. It is never a census of Sleeper, and this page never claims
otherwise.</strong></p>

<p><strong>What we read.</strong> Only Sleeper's public, read-only,
unauthenticated API — the same data any visitor to a league page can see:
league settings, members' handles, draft picks, trades and waiver claims.
No login, no private league data, nothing that required a credential.</p>

<p><strong>What we store about a person.</strong> Their Sleeper
<code>display_name</code> — the pseudonymous handle they chose, like
<code>pjstiehl</code> — and their opaque Sleeper user id. That is all. We do
not collect, infer, look up or store real names, emails, avatars or any other
identifying detail, and no attempt is made to connect a handle to a real
person.</p>

<p><strong>Only dynasty leagues.</strong> Filtered on Sleeper's own league
type, not on the league's name. A league called "Deke Dynasty" that is
configured as a keeper league is not indexed, because the platform does not
call it dynasty and we are not going to invent a definition.</p>

<p><strong>There is no "worst managers" board</strong>, and there will not be
one. The boards are ranked best-first and the tail is simply the tail. These
are real people playing a game with their friends.</p>

<p>Sleeper's API is free for non-commercial use; commercial use requires a
licence from Sleeper. This project is non-commercial, and that constraint is
recorded in <code>docs/CROSS-LEAGUE-CORPUS.md</code> so it survives any
future change of plan.</p>
</div>

<h2>What this <span class="accent">cannot</span> tell you</h2>
<div class="xl-note">
<ul style="margin:0">
<li><strong>It is a ranking against the indexed sample, not the world.</strong>
Rank 1 here means first among the managers we walked to. A better manager
almost certainly exists in a league we have never seen.</li>
<li><strong>Sample sizes are uneven.</strong> A manager in five indexed
leagues is measured on far more evidence than one in a single league.
Shrinkage accounts for this; it does not erase it.</li>
<li><strong>Who you play with is not controlled for.</strong> Every z-score is
relative to that league's own managers, so beating a casual league counts the
same as beating a sharp one. This measures beating your peers, whoever they
happened to be.</li>
<li><strong>KeepTradeCut is a crowd, not an oracle.</strong> Where the
consensus board is wrong, every score here is wrong the same way.</li>
<li><strong>It inherits every limit of the per-league metric</strong> —
outcome rather than process, top-500 pricing, "Mid" for future picks, and no
measurement of lineup or roster management. Those are set out on the
<a href="managerscore.html">Manager Score</a> page.</li>
<li><strong>Leagues can go stale.</strong> A league indexed on an earlier run
but outside today's crawl budget keeps its last score rather than
disappearing.</li>
</ul>
</div>
"""


def build_cross_league(latest_ts: datetime, league_label: str) -> str:
    """Render crossleague.html. Imported lazily by report.generate_site."""
    from .report import _page, _site_header      # local: avoids import cycle
    from .branding import page_title
    from .crossleague_js import CROSSLEAGUE_JS

    body = """<div class="container">

<h2>Best Dynasty <span class="accent">Managers</span></h2>
<p class="lede">Manager Score, aggregated across every dynasty league we have
been able to index — so a good drafter in one league can be compared with a
good drafter in another. Scores come from the same draft / trade / waiver
maths as the per-league <a href="managerscore.html">Manager Score</a> page.</p>

<div id="xl-coverage" class="callout" style="display:none"></div>

<div id="xl-results" style="display:none">

  <h3>Best <span class="accent">drafters</span></h3>
  <p class="xl-sub">Ranked on the draft component alone: did their picks beat
  what that slot usually returns, across every league we have indexed?</p>
  <div id="xl-draft-board" class="xl-board"></div>

  <h3>Overall <span class="accent">manager</span> ranking</h3>
  <div id="xl-leaderboard" class="xl-board"></div>

  <h3>Leagues in the <span class="accent">corpus</span></h3>
  <div id="xl-leagues" class="xl-board"></div>

</div>

__METHODOLOGY__

</div>
<style>__CROSSLEAGUE_CSS__</style>
<script>__CROSSLEAGUE_JS__</script>
"""
    body = (
        body.replace("__METHODOLOGY__", _methodology_html())
        .replace("__CROSSLEAGUE_CSS__", _CROSSLEAGUE_CSS)
        .replace("__CROSSLEAGUE_JS__", CROSSLEAGUE_JS)
    )
    return _page(
        page_title("Best Dynasty Managers"),
        _site_header("crossleague", latest_ts, league_label),
        body,
    )
