"""Ingest league-id submissions filed as GitHub issues.

The gap this closes
-------------------
The site is a static GitHub Pages build. There is no server, no database and
no write path: a visitor who types their league id into the page has nowhere
to put it. So "populate the board with every league entered into the tool"
cannot be satisfied by the page alone, and pretending otherwise would just be
a form that silently discards input.

What *does* exist is a writable store this project already uses every day:
the repository itself, via the daily workflow's ``contents: write``. GitHub
issues are the one inbox a static site can point an anonymous visitor at
without operating any infrastructure. So:

    crossleague.html renders a link
        -> visitor opens a pre-filled issue form (label applied by the
           template, so it works for people with no repo access)
        -> this script reads open issues carrying that label
        -> validates the id against Sleeper
        -> appends it to data/cross_league/seeds.json
        -> comments and closes the issue
        -> the next daily crawl picks it up from the registry

The submission is public and attributable, and a human can audit or revert
every accepted id with ``git log data/cross_league/seeds.json``.

Threat model, and why this is safe
----------------------------------
Issue bodies are attacker-controlled text from strangers. Every one of these
is a deliberate property of this script, not an accident of implementation:

1. **Nothing from an issue is ever executed.** No shell, no ``subprocess``,
   no ``eval``, no template expansion. The only consumer of issue text is a
   regular expression.
2. **Only digits survive parsing.** ``re.fullmatch(r"[0-9]{6,24}")`` on a
   single whitespace-stripped field. A body of shell metacharacters, YAML,
   markdown or workflow syntax yields no candidate and the issue is rejected.
   Nothing but a bare integer string can ever reach the registry.
3. **The id must be real before it is trusted.** Sleeper must return an
   existing league whose ``sport`` is NFL and whose ``settings.type`` is 2
   (dynasty). An id that is merely well-formed is rejected.
4. **Nothing else from the issue is stored.** The league's name comes from
   *Sleeper's* response, never from the issue title or body, so a submitter
   cannot inject display text into the registry or the site. The only fields
   recorded are the validated id, the issue number, and the author's GitHub
   login (as provenance, so an abusive submitter is traceable).
5. **Bounded work.** At most ``--max-issues`` issues per run and exactly one
   league id per issue, so a flood of issues cannot turn into an unbounded
   crawl or an unbounded number of API calls.
6. **Least privilege.** Needs only ``contents: write`` and ``issues: write``.
   It reads no secrets beyond ``GITHUB_TOKEN`` and never checks out or runs
   code from a fork. The workflow is triggered by ``issues`` events, which
   run from the default branch — not ``pull_request_target``, which is the
   trigger that actually leaks write credentials to untrusted code.
7. **Accepting an id is not the same as scoring it.** Everything written here
   goes through the same crawl, the same dynasty filter and the same partial-
   read discard as a league found by the crawler. A bad id that somehow got
   through would produce no score, not a wrong one.

The residual risk is a real dynasty league submitted by someone who is not in
it. No API can distinguish that, so it is handled socially rather than
technically: the issue form says to submit only your own league, the corpus
records which issue introduced each league, and removing one is a one-line
revert. That limitation is stated rather than engineered around.

Usage
-----
    # what the workflow runs
    GITHUB_TOKEN=... python scripts/ingest_league_submissions.py \
        --repo pstiehl/Dynasty-Football-Model

    # see what it would do, touching nothing
    python scripts/ingest_league_submissions.py --repo owner/name --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
SEEDS_PATH = REPO_ROOT / "data" / "cross_league" / "seeds.json"

SLEEPER = "https://api.sleeper.app/v1"
GITHUB_API = "https://api.github.com"

SUBMISSION_LABEL = "league-submission"
ACCEPTED_LABEL = "league-accepted"
REJECTED_LABEL = "league-rejected"

DYNASTY_LEAGUE_TYPE = 2      # see docs/CROSS-LEAGUE-CORPUS.md section 3

USER_AGENT = (
    "Dynasty-Football-Model/league-submission-ingest "
    "(+https://github.com/pstiehl/Dynasty-Football-Model)"
)

# A Sleeper league id is an 18-19 digit snowflake. The window is slightly
# wider than that so a future id length change is a Sleeper 404 (clear,
# debuggable) rather than a silent regex rejection, while still being far too
# narrow to admit anything that is not a bare integer.
LEAGUE_ID_RE = re.compile(r"^[0-9]{6,24}$")

# The issue-form field. GitHub renders `id: league_id` as a "### Sleeper
# league id" heading followed by the value.
FIELD_HEADING_RE = re.compile(
    r"^###\s*Sleeper league id\s*$", re.IGNORECASE
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Parsing: the ONLY place issue text is touched
# --------------------------------------------------------------------------

def extract_league_id(body: Optional[str]) -> Tuple[Optional[str], str]:
    """Pull a single league id out of an issue body.

    Returns ``(league_id_or_None, reason)``.

    This function is the entire trust boundary. It takes arbitrary text from
    an untrusted stranger and returns either a string of digits or nothing.
    It must not grow a second return path, and nothing else in this file may
    read the raw body.

    Two shapes are accepted, both requiring the value to be digits only:

    * the issue form's ``### Sleeper league id`` section (the normal path),
    * failing that, a lone digit-string on its own line, so a submission
      typed by hand without the template still works.

    A body offering more than one distinct candidate is REJECTED rather than
    guessed at: picking one would make the outcome depend on text ordering a
    submitter did not know was significant.
    """
    if not body:
        return None, "issue body is empty"

    lines = [ln.strip() for ln in str(body).splitlines()]

    # Preferred: the value under the form's heading.
    for i, line in enumerate(lines):
        if not FIELD_HEADING_RE.match(line):
            continue
        for value in lines[i + 1:]:
            if not value:
                continue
            if value.startswith("#"):
                break                       # next section: field left blank
            if LEAGUE_ID_RE.match(value):
                return value, "from the submission form field"
            return None, (
                "the 'Sleeper league id' field must contain digits only "
                "(the long number from your league's Sleeper URL)"
            )
        return None, "the 'Sleeper league id' field was left blank"

    # Fallback: a bare id on its own line, anywhere.
    candidates = {ln for ln in lines if LEAGUE_ID_RE.match(ln)}
    if len(candidates) == 1:
        return candidates.pop(), "from a bare id line"
    if len(candidates) > 1:
        return None, (
            "more than one league id found — please open one issue per "
            "league so each can be checked and traced separately"
        )
    return None, (
        "no Sleeper league id found — paste the long number from your "
        "league's URL, e.g. 1316222126914539520"
    )


# --------------------------------------------------------------------------
# Validation: the id must be a real dynasty league
# --------------------------------------------------------------------------

def _get_json(url: str, *, token: Optional[str] = None, timeout: int = 30):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def validate_league(league_id: str, *, fetch=None) -> Tuple[bool, str, Dict]:
    """Ask Sleeper whether this id is an NFL dynasty league.

    ``fetch`` is injectable so the decision table can be tested against
    fixtures without touching the network.

    The dynasty check is the same ``settings.type == 2`` the crawler uses and
    is applied here as well as there. Checking twice is deliberate: rejecting
    at submission time gives the submitter an immediate, specific answer
    ("that is a redraft league") instead of their id sitting in the registry
    being silently skipped by every future crawl.
    """
    fetch = fetch or (lambda u: _get_json(u))
    try:
        lg = fetch(f"{SLEEPER}/league/{league_id}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False, "Sleeper has no league with that id", {}
        return False, f"could not reach Sleeper (HTTP {exc.code})", {}
    except Exception as exc:  # noqa: BLE001
        return False, f"could not reach Sleeper ({type(exc).__name__})", {}

    if not isinstance(lg, dict) or not lg.get("league_id"):
        return False, "Sleeper has no league with that id", {}

    sport = lg.get("sport")
    if sport not in (None, "nfl"):
        return False, f"that is a {sport} league; this board indexes NFL only", {}

    settings = lg.get("settings") or {}
    if settings.get("type") != DYNASTY_LEAGUE_TYPE:
        kind = {0: "redraft", 1: "keeper"}.get(settings.get("type"), "non-dynasty")
        return False, (
            f"Sleeper classifies that league as {kind}, not dynasty "
            f"(settings.type = {settings.get('type')}). This board indexes "
            f"only leagues Sleeper itself calls dynasty."
        ), {}

    return True, "validated against Sleeper as an NFL dynasty league", {
        # Name comes from SLEEPER, never from the issue. See threat model #4.
        "name": lg.get("name"),
        "season": lg.get("season"),
        "n_teams": lg.get("total_rosters"),
    }


# --------------------------------------------------------------------------
# The registry write
# --------------------------------------------------------------------------

def append_seed(seeds_path: Path, league_id: str, meta: Dict,
                issue_number: int, author: Optional[str]) -> bool:
    """Add a validated id to the registry. Returns False if already present."""
    registry = json.loads(Path(seeds_path).read_text(encoding="utf-8"))
    seeds = registry.get("seeds") or []
    if any(str(s.get("league_id")) == str(league_id) for s in seeds):
        return False

    seeds.append({
        "league_id": str(league_id),
        "name": meta.get("name"),
        "added_by": "issue-submission",
        "added_at": _now_iso(),
        "issue": int(issue_number),
        # Provenance only, so an abusive submitter is traceable. A GitHub
        # login is already public on the issue itself.
        "submitted_by": author,
        "note": (
            f"submitted via issue #{issue_number}; validated against Sleeper "
            f"as an NFL dynasty league (settings.type == 2)"
        ),
    })
    registry["seeds"] = seeds
    p = Path(seeds_path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(registry, indent=2, sort_keys=False) + "\n",
                   encoding="utf-8")
    tmp.replace(p)
    return True


# --------------------------------------------------------------------------
# GitHub plumbing
# --------------------------------------------------------------------------

class GitHub:
    def __init__(self, repo: str, token: str, *, dry_run: bool = False):
        self.repo = repo
        self.token = token
        self.dry_run = dry_run

    def _request(self, method: str, path: str, payload: Optional[Dict] = None):
        url = f"{GITHUB_API}/repos/{self.repo}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body.strip() else {}

    def ensure_labels(self) -> List[str]:
        """Create the routing labels if they do not exist yet.

        This is not housekeeping, it is a correctness requirement. A GitHub
        issue FORM applies ``labels:`` only for labels that already exist in
        the repository — a missing one is silently skipped, not created. So
        on a repo where ``league-submission`` has never been defined, every
        submission would arrive unlabelled, the workflow's ``if:`` gate would
        never match, the scheduled sweep would find nothing, and the feature
        would fail completely without a single error anywhere.

        Creating them here makes the feature self-contained in its own PR
        rather than depending on someone remembering to add three labels by
        hand. It is idempotent: an existing label returns 422 and is ignored.

        Ordering works out on its own. The scheduled sweep runs at 10:00 UTC
        and the site deploy that publishes the submission link runs at 11:00,
        so the labels exist before the page advertising them is live.
        """
        wanted = [
            (SUBMISSION_LABEL, "1d76db",
             "A Sleeper league id submitted for the cross-league corpus"),
            (ACCEPTED_LABEL, "0e8a16",
             "Submission validated against Sleeper and added to the seed registry"),
            (REJECTED_LABEL, "b60205",
             "Submission could not be validated as an NFL dynasty league"),
        ]
        created = []
        for name, color, description in wanted:
            if self.dry_run:
                continue
            try:
                self._request("POST", "/labels", {
                    "name": name, "color": color, "description": description,
                })
                created.append(name)
            except urllib.error.HTTPError as exc:
                # 422 = already exists. Anything else is a real problem, but
                # must not stop us processing submissions that are already in.
                if exc.code != 422:
                    print(f"  ! could not create label {name}: HTTP {exc.code}",
                          file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                print(f"  ! could not create label {name}: "
                      f"{type(exc).__name__}", file=sys.stderr)
        return created

    def open_submissions(self, limit: int) -> List[Dict]:
        path = (f"/issues?state=open&labels={SUBMISSION_LABEL}"
                f"&per_page={min(100, limit)}&sort=created&direction=asc")
        items = self._request("GET", path)
        # The issues endpoint also returns pull requests; a PR is not a
        # submission and must never be commented on or closed by this job.
        return [i for i in items if not i.get("pull_request")][:limit]

    def comment(self, number: int, body: str) -> None:
        if self.dry_run:
            print(f"    [dry-run] would comment on #{number}")
            return
        self._request("POST", f"/issues/{number}/comments", {"body": body})

    def close(self, number: int, label: str) -> None:
        if self.dry_run:
            print(f"    [dry-run] would label #{number} '{label}' and close")
            return
        try:
            self._request("POST", f"/issues/{number}/labels", {"labels": [label]})
        except Exception:  # noqa: BLE001
            pass          # a missing label must not block closing the issue
        self._request("PATCH", f"/issues/{number}",
                      {"state": "closed", "state_reason": "completed"})


ACCEPT_TEMPLATE = """Thanks — **{name}** is in the queue. ✅

`{league_id}` validated against Sleeper as an NFL dynasty league \
({n_teams} teams, {season} season) and has been added to \
[`data/cross_league/seeds.json`]\
(https://github.com/{repo}/blob/main/data/cross_league/seeds.json).

The crawl scores a bounded number of leagues per day, so it may take a few \
daily runs before this one appears on the \
[Best Dynasty Managers](https://pstiehl.github.io/Dynasty-Football-Model/crossleague.html) \
board. Nothing further is needed from you.

To have it removed, comment here and it will be reverted.
"""

REJECT_TEMPLATE = """Thanks for the submission, but this one could not be \
added. ❌

**Reason:** {reason}

{hint}

Feel free to open a new issue with a corrected id.
"""

REJECT_HINT = """Your league id is the long number in your Sleeper URL:

    https://sleeper.com/leagues/1316222126914539520/team
                               ^^^^^^^^^^^^^^^^^^^

Only **dynasty** leagues are indexed — the board aggregates a dynasty-specific \
metric, and Sleeper's own league type is what decides, not the league's name.
"""


def process(gh: GitHub, issues: List[Dict], seeds_path: Path, *,
            validator=None, verbose: bool = True) -> Dict:
    """Decide and act on each submission. Returns a run summary."""
    validator = validator or validate_league
    summary = {"examined": 0, "accepted": 0, "duplicate": 0, "rejected": 0,
               "accepted_ids": [], "errors": 0}

    for issue in issues:
        number = issue.get("number")
        author = ((issue.get("user") or {}).get("login"))
        summary["examined"] += 1
        if verbose:
            print(f"  #{number} by {author}")

        league_id, why = extract_league_id(issue.get("body"))
        if not league_id:
            if verbose:
                print(f"    reject: {why}")
            gh.comment(number, REJECT_TEMPLATE.format(reason=why, hint=REJECT_HINT))
            gh.close(number, REJECTED_LABEL)
            summary["rejected"] += 1
            continue

        ok, reason, meta = validator(league_id)
        if not ok:
            if verbose:
                print(f"    reject {league_id}: {reason}")
            gh.comment(number, REJECT_TEMPLATE.format(reason=reason,
                                                      hint=REJECT_HINT))
            gh.close(number, REJECTED_LABEL)
            summary["rejected"] += 1
            continue

        added = (True if gh.dry_run
                 else append_seed(seeds_path, league_id, meta, number, author))
        if not added:
            if verbose:
                print(f"    already indexed: {league_id}")
            gh.comment(number, (
                f"`{league_id}` is already in the seed registry, so it is "
                f"already being crawled. Closing — nothing further needed. 👍"
            ))
            gh.close(number, ACCEPTED_LABEL)
            summary["duplicate"] += 1
            continue

        if verbose:
            print(f"    accept {league_id}: {meta.get('name')}")
        gh.comment(number, ACCEPT_TEMPLATE.format(
            name=meta.get("name") or "your league",
            league_id=league_id,
            n_teams=meta.get("n_teams"),
            season=meta.get("season"),
            repo=gh.repo,
        ))
        gh.close(number, ACCEPTED_LABEL)
        summary["accepted"] += 1
        summary["accepted_ids"].append(league_id)
        time.sleep(0.2)      # polite toward both APIs

    return summary


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"),
                    help="owner/name (default: $GITHUB_REPOSITORY)")
    ap.add_argument("--seeds", type=Path, default=SEEDS_PATH)
    ap.add_argument("--max-issues", type=int, default=25,
                    help="hard cap on submissions handled per run")
    ap.add_argument("--dry-run", action="store_true",
                    help="report decisions, write and post nothing")
    args = ap.parse_args(argv)

    if not args.repo:
        print("--repo or $GITHUB_REPOSITORY required", file=sys.stderr)
        return 2
    token = os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        print("GITHUB_TOKEN not set — nothing to do.", file=sys.stderr)
        return 0 if args.dry_run else 1

    gh = GitHub(args.repo, token, dry_run=args.dry_run)

    # Must happen before anything else: an issue form cannot apply a label
    # that does not exist, so without this the inbox is silently unreachable.
    created = gh.ensure_labels()
    if created:
        print(f"Created routing label(s): {', '.join(created)}")

    try:
        issues = gh.open_submissions(args.max_issues)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not list submissions: {exc}", file=sys.stderr)
        return 1

    print(f"League submissions: {len(issues)} open issue(s) labelled "
          f"'{SUBMISSION_LABEL}'"
          + (" [dry run]" if args.dry_run else ""))
    if not issues:
        return 0

    summary = process(gh, issues, args.seeds)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
