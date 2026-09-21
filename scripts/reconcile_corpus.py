"""Re-score client-submitted leagues authoritatively and reconcile the corpus.

What this is for
----------------
The cross-league corpus backend accepts Manager Score results computed in a
visitor's **browser**. That is the only affordable way to index a league —
scoring one dynasty league costs ~97 Sleeper calls, which no Cloudflare
Worker on the free tier can spend per request — but it means the numbers
arrive from a place we do not control and cannot trust.

The worker's own verification (league exists, is ``settings.type == 2``, the
claimed managers really are in it, the z-scores are internally consistent)
makes a *fabricated* submission expensive. It does not make it impossible:
a determined forger who reimplements the scoring model can still post
self-consistent lies about a real league they are a member of.

This job is the thing that actually closes that hole. It re-reads each
pending league from Sleeper and re-scores it with the same shipped page code
the browser ran (``scripts/js/score_league_harness.js``), then tells the
worker whether the submitted numbers hold up. Only this job can move a
league to ``confirmed``, and only ``confirmed`` leagues appear on the public
cross-league board.

So the trust chain is: browser computes -> worker verifies cheaply and marks
PROVISIONAL -> this job re-computes independently and marks CONFIRMED or
REJECTED. Until it runs, a submitted league is visible to the submitter and
to nobody else.

Usage
-----
::

    export CORPUS_URL=https://dynasty-model-proxy.<subdomain>.workers.dev
    export CORPUS_RECONCILE_TOKEN=<the wrangler secret RECONCILE_TOKEN>

    python scripts/reconcile_corpus.py --limit 25
    python scripts/reconcile_corpus.py --limit 25 --dry-run

Exits 0 when there was nothing to do or the run completed, so a transient
Sleeper problem never fails the daily workflow. Exits 2 only on a
configuration error the owner needs to see.

Deliberately stdlib-only (urllib, json, subprocess): this runs in CI next to
a build that already has enough dependencies.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS = REPO_ROOT / "scripts" / "js" / "score_league_harness.js"

# How far a re-scored index may sit from the submitted one before we call the
# submission wrong rather than stale.
#
# It cannot be zero. The browser and this job read Sleeper at different
# times, and the Manager Score page prices transactions against the KTC
# value artifact *as it stood at read time* — so a league legitimately
# re-scores a little differently the next day. One index point is about
# 0.067 standard deviations, comfortably below anything a reader would
# notice on the board and far below what a fabricated submission would need
# to move to be worth fabricating.
INDEX_TOLERANCE = 1.0

# A submission that omits or invents managers is wrong regardless of scores.
REQUIRE_SAME_MANAGER_SET = True


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _request(url: str, token: str, *, method: str = "GET",
             payload: Optional[Dict] = None, timeout: int = 60) -> Dict:
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def fetch_pending(base: str, token: str, limit: int) -> List[Dict]:
    out = _request(f"{base}/corpus/pending?limit={int(limit)}", token)
    return out.get("pending") or []


# ---------------------------------------------------------------------------
# Scoring (delegated to the real page code via node)
# ---------------------------------------------------------------------------

def rescore(leagues: List[Dict], *, values_dir: Path, max_calls: int,
            min_delay_ms: int, verbose: bool = True) -> Dict:
    """Re-score leagues with the shipped Manager Score JS.

    Nothing about the metric is computed here — that is the whole point. The
    harness runs MANAGERSCORE_CORE_JS + MANAGERSCORE_UI_JS under node, so the
    authoritative score is produced by the same code the browser ran, not by
    a Python lookalike that would drift away from it.
    """
    node = shutil.which("node")
    if not node:
        return {"ok": False, "error": "node not found — cannot re-score "
                                      "without the shipped scoring JS",
                "leagues": []}

    sys.path.insert(0, str(REPO_ROOT / "src"))
    from dynasty.managerscore_js import MANAGERSCORE_CORE_JS, MANAGERSCORE_UI_JS

    tmp = Path(tempfile.mkdtemp(prefix="corpus-reconcile-"))
    page_js = tmp / "ms_page.js"
    page_js.write_text(MANAGERSCORE_CORE_JS + MANAGERSCORE_UI_JS, encoding="utf-8")

    request = {
        "leagues": leagues,
        "valuesPath": str(values_dir / "managerscore_values.json"),
        "historyDir": str(values_dir / "ktc_history"),
        "maxCalls": max_calls,
        "minDelayMs": min_delay_ms,
        "concurrency": 3,
        "includeHistory": True,
    }
    req_path = tmp / "request.json"
    req_path.write_text(json.dumps(request), encoding="utf-8")

    if verbose:
        print(f"  re-scoring {len(leagues)} league(s) via node harness")

    proc = subprocess.run(
        [node, str(HARNESS), str(page_js), str(req_path)],
        capture_output=True, text=True, timeout=1800,
    )
    if proc.stderr.strip() and verbose:
        for line in proc.stderr.strip().splitlines()[-10:]:
            print(f"    [node] {line}", file=sys.stderr)
    if proc.returncode != 0 and not proc.stdout.strip():
        return {"ok": False, "error": f"harness exited {proc.returncode}",
                "leagues": []}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"harness output unparseable: {exc}",
                "leagues": []}


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def to_submission_managers(result: Dict) -> List[Dict]:
    """Convert an msScoreLeague() result into the worker's manager shape.

    Rosters with no Sleeper owner get a synthetic ``roster:<league>:<n>`` id
    from ``msBuildInput``. They are not people and the worker refuses them.
    Dropping one is only safe when it contributed no evidence to any
    component, because the worker checks that each component's z-scores
    still have mean 0 and sd 1 — and those pools contain exactly the
    managers with evidence. See ``csEligibility`` in
    ``dynasty.corpus_submit_js``, which applies the identical rule in the
    browser.
    """
    comps = ("draft", "trade", "waiver")
    out: List[Dict] = []
    for m in result.get("managers") or []:
        mid = str(m.get("id") or "")
        if not mid.isdigit():
            if any((m.get(c) or {}).get("n", 0) > 0 for c in comps):
                raise ValueError(
                    f"unowned roster {mid} carries scored transactions; "
                    "cannot submit this league without misstating it"
                )
            continue
        out.append({
            "manager_id": mid,
            "display_name": str(m.get("name") or mid),
            "index": m.get("index"),
            "rank": m.get("rank"),
            "composite": m.get("composite"),
            "components": {
                c: {
                    "n": (m.get(c) or {}).get("n", 0),
                    "z": (m.get(c) or {}).get("z", 0.0),
                    "mean": (m.get(c) or {}).get("mean", 0.0),
                    "shrunk": (m.get(c) or {}).get("shrunk", 0.0),
                } for c in comps
            },
            "flags": (m.get("flags") or [])[:8],
        })
    return out


def compare(submitted_index: Dict[str, float],
            authoritative: List[Dict],
            tolerance: float = INDEX_TOLERANCE) -> Dict:
    """Decide whether the submitted numbers survive an independent re-score."""
    auth_index = {m["manager_id"]: float(m["index"]) for m in authoritative}

    if not submitted_index:
        # Nothing stored to compare against (an 'unverified' row whose
        # managers were never written). Treat the re-score as the truth.
        return {"verdict": "confirmed", "max_delta": None,
                "note": "no stored scores to compare; adopted the re-score"}

    missing = sorted(set(submitted_index) - set(auth_index))
    extra = sorted(set(auth_index) - set(submitted_index))
    if REQUIRE_SAME_MANAGER_SET and (missing or extra):
        return {
            "verdict": "rejected",
            "max_delta": None,
            "note": (f"manager set differs: {len(missing)} submitted "
                     f"manager(s) absent on re-score, {len(extra)} new"),
        }

    max_delta = 0.0
    worst = None
    for mid, sub in submitted_index.items():
        auth = auth_index.get(mid)
        if auth is None:
            continue
        delta = abs(float(sub) - auth)
        if delta > max_delta:
            max_delta, worst = delta, mid

    if max_delta > tolerance:
        return {
            "verdict": "rejected",
            "max_delta": round(max_delta, 3),
            "note": (f"re-score disagrees by {max_delta:.2f} index points "
                     f"(manager {worst}); tolerance is {tolerance}"),
        }
    return {
        "verdict": "confirmed",
        "max_delta": round(max_delta, 3),
        "note": f"re-score agrees within {max_delta:.2f} index points",
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=25,
                    help="maximum pending leagues to reconcile in one run")
    ap.add_argument("--max-calls", type=int, default=600,
                    help="Sleeper call budget for the whole run")
    ap.add_argument("--min-delay-ms", type=int, default=250,
                    help="minimum delay between Sleeper calls")
    ap.add_argument("--values-dir", type=Path, default=Path("dynasty_site"),
                    help="directory holding managerscore_values.json")
    ap.add_argument("--tolerance", type=float, default=INDEX_TOLERANCE)
    ap.add_argument("--dry-run", action="store_true",
                    help="re-score and report, but post no verdict")
    args = ap.parse_args(argv)

    base = (os.environ.get("CORPUS_URL") or "").strip().rstrip("/")
    token = (os.environ.get("CORPUS_RECONCILE_TOKEN") or "").strip()
    if not base or not token:
        print("CORPUS_URL and CORPUS_RECONCILE_TOKEN must both be set — the "
              "corpus backend is not configured, so there is nothing to "
              "reconcile.", file=sys.stderr)
        # Not a failure: a repo without the backend deployed is the normal
        # state, and the daily workflow must not go red over it.
        return 0

    try:
        pending = fetch_pending(base, token, args.limit)
    except urllib.error.HTTPError as exc:
        print(f"could not read pending leagues: HTTP {exc.code}", file=sys.stderr)
        return 0 if exc.code >= 500 else 2
    except Exception as exc:  # noqa: BLE001
        print(f"could not read pending leagues: {exc}", file=sys.stderr)
        return 0

    if not pending:
        print("nothing pending — corpus is reconciled.")
        return 0

    print(f"{len(pending)} league(s) pending reconciliation")

    specs = [{
        "league_id": p["league_id"],
        "name": p.get("name"),
        "season": p.get("season"),
        "n_teams": p.get("n_teams"),
    } for p in pending]

    scored = rescore(specs, values_dir=args.values_dir,
                     max_calls=args.max_calls, min_delay_ms=args.min_delay_ms)
    if not scored.get("ok"):
        print(f"re-score failed: {scored.get('error')}", file=sys.stderr)
        return 0

    by_id = {str(r.get("league_id")): r for r in scored.get("leagues") or []}
    results: List[Dict] = []

    for p in pending:
        lid = str(p["league_id"])
        entry = by_id.get(lid)
        if not entry or not entry.get("ok"):
            # A league we could not read is left pending, not rejected. An
            # incomplete read must never look like a failed audit.
            print(f"  {lid}: skipped — {(entry or {}).get('reason', 'not scored')}")
            continue

        try:
            authoritative = to_submission_managers(entry.get("result") or {})
        except ValueError as exc:
            print(f"  {lid}: skipped — {exc}")
            continue

        if not authoritative:
            print(f"  {lid}: skipped — no identifiable managers")
            continue

        verdict = compare(p.get("submitted_index") or {}, authoritative,
                          args.tolerance)
        print(f"  {lid}: {verdict['verdict']} — {verdict['note']}")

        item = {
            "league_id": lid,
            "verdict": verdict["verdict"],
            "note": verdict["note"],
        }
        if verdict["verdict"] == "confirmed":
            # Replace the client's numbers with ours outright. Even when they
            # agree, the stored figures should be the ones we computed.
            item["league"] = {
                "league_id": lid,
                "name": entry.get("name") or p.get("name") or lid,
                "season": str(entry.get("season") or p.get("season") or ""),
                "n_teams": entry.get("n_teams") or p.get("n_teams"),
                "n_seasons_scored": entry.get("n_seasons") or 1,
                "lineage_ids": entry.get("lineage_ids") or [lid],
                "lineage_root": entry.get("lineage_root") or lid,
            }
            item["managers"] = authoritative
        results.append(item)

    if not results:
        print("no verdicts to post.")
        return 0

    if args.dry_run:
        print(f"[dry-run] would post {len(results)} verdict(s)")
        for r in results:
            print(f"  [dry-run] {r['league_id']} -> {r['verdict']}")
        return 0

    try:
        out = _request(
            f"{base}/corpus/reconcile", token, method="POST",
            payload={
                "schema": "dfm.corpus.reconcile.v1",
                "run_id": os.environ.get("GITHUB_RUN_ID") or "local",
                "results": results,
            },
        )
    except Exception as exc:  # noqa: BLE001
        print(f"could not post verdicts: {exc}", file=sys.stderr)
        return 0

    print(f"applied {out.get('n_applied', 0)} verdict(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
