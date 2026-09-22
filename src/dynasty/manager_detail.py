"""Per-manager drill-down artifacts — what actually drove a manager's score.

The gap this closes
-------------------
``crossleague_corpus.json`` tells you *that* a manager made 19 scored picks
worth z = 2.34. It cannot tell you *which* picks, because the per-transaction
audit trail is deliberately not in it: the corpus is rewritten and committed
on **every daily run**, and an inline audit came to 2.3 MB for five leagues
(~56 MB at the corpus's current 122). A multi-megabyte artifact rewritten
daily bloats the repository permanently, so ``crossleague.compact_result``
strips the audit down to the per-component ``n`` and ``z`` that
re-aggregation actually reads. That compaction is correct and stays.

This module puts the evidence back **without** putting it back in git.

Where the bytes go, and why that is not a regression
----------------------------------------------------
Two stores, neither of them committed:

``data/cross_league/detail/<league_id>.json.gz``
    The durable per-league audit, gzipped. Gitignored, and carried between
    runs by the same ``actions/cache`` entry that already carries
    ``data/cross_league/cache``. This mirrors a decision the crawl already
    made for raw Sleeper responses: regenerable third-party-derived bulk
    belongs in the build cache, not in the repository.

``dynasty_site/managers/<shard>/<manager>.json``
    The published shards the browser fetches. ``dynasty_site/`` is
    gitignored and rebuilt by CI on every run — it is the deploy artifact,
    not a source tree — so **publishing here adds zero bytes to the repo.**

So the thing PR #72 fixed (a daily-rewritten committed audit) is not
reintroduced. What changed is only *where* the audit lives.

Why per-league durable, per-manager published
---------------------------------------------
Scoring produces an audit **per league**; a manager's evidence is assembled
from every league they appear in. The crawl scores a bounded number of
leagues per run (``--max-leagues``), so a manager's three leagues may well be
scored on three different days. Keying the durable store by league is
therefore the only shape that accumulates correctly: each run refreshes the
leagues it actually scored and leaves the rest alone, exactly as the corpus
itself does. The per-manager view is then assembled at publish time, when
every retained league detail is in hand.

It also means a league detail is invalidated by exactly one event — that
league being re-scored — rather than by any manager in it changing.

Why sharded, and why FNV rather than a prefix of the id
--------------------------------------------------------
Manager ids are mostly 17-19 digit Sleeper snowflakes, which are
time-ordered, so their leading digits are strongly correlated. Measured on
the live 1,407-manager corpus:

===================  =======  =========  ================
shard key            buckets  max/bucket  comment
===================  =======  =========  ================
first 2 chars of id       85         81  snowflake clustering
last 2 chars of id        41         73  worse: 66 ids end ``:<n>``
FNV-1a low byte          254         13  what this module uses
===================  =======  =========  ================

256 buckets keyed on the low byte of FNV-1a/32 gives ~5.5 files per
directory today and degrades gracefully as the corpus grows (the crawl has
hundreds of leagues queued, so 1,407 managers is a floor, not a ceiling).
FNV-1a is used rather than a real digest because the **browser** must compute
the same shard to build a fetch URL: it is a five-line synchronous integer
loop in JS, whereas SHA-1 via ``crypto.subtle`` is async and would turn a
path computation into a promise chain for no benefit. This is a bucketing
function, not a security boundary.

Why ids are encoded rather than used raw as filenames
------------------------------------------------------
66 of the corpus's 1,407 manager ids are not Sleeper user ids at all. When a
roster has no associated user, scoring synthesises ``roster:<league>:<slot>``
so the seat is still counted. Those contain colons, which are legal on Linux
but not on Windows checkouts and require escaping in URLs. Every id is
therefore ``~XX``-hex-encoded down to ``[A-Za-z0-9._-]`` (``~`` itself is
encoded, which is what makes the mapping injective and collision-free by
construction rather than by luck). :func:`publish_manager_details` still
asserts injectivity and refuses to overwrite, so a future id shape that broke
the property would fail the build instead of silently serving one manager's
evidence under another's name.

Privacy
-------
Carries the same fields the board already publishes: the pseudonymous
Sleeper ``display_name`` and the opaque user id. No real-identity resolution,
no avatars, no emails. A drill-down shows a manager their own transactions
priced against their leaguemates — it adds detail about *decisions already
summarised on the board*, not about people.
"""
from __future__ import annotations

import gzip
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .crossleague import COMPONENTS, MS_SHRINK_K, MS_WEIGHTS, shrink

MANAGER_DETAIL_SCHEMA = "crossleague.manager_detail.v1"
LEAGUE_DETAIL_SCHEMA = "crossleague.league_detail.v1"

#: Durable per-league audit, under ``data/cross_league/``. Gitignored.
DETAIL_DIRNAME = "detail"

#: Published shards, under the site root. Gitignored (built by CI).
SITE_DIRNAME = "managers"

#: Number of shard buckets. Low byte of FNV-1a/32.
SHARD_BUCKETS = 256

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def fnv1a32(text: str) -> int:
    """FNV-1a, 32-bit. Mirrored byte-for-byte by ``xlShard`` in the page JS.

    Kept deliberately trivial: both implementations must agree on every id
    or the browser fetches a 404 for a file that exists. ``tests/js`` asserts
    the two agree on the real corpus's ids.
    """
    h = 0x811C9DC5
    for byte in text.encode("utf-8"):
        h ^= byte
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def shard_of(manager_id: str) -> str:
    """Two lowercase hex chars — the directory a manager's file lives in."""
    return "%02x" % (fnv1a32(str(manager_id)) & (SHARD_BUCKETS - 1))


def safe_name(manager_id: str) -> str:
    """Filesystem- and URL-safe encoding of a manager id.

    ``~`` is encoded first, which is what makes this injective: no encoded
    output can be confused with a literal input.
    """
    out = str(manager_id).replace("~", "~7E")
    return _SAFE_CHARS.sub(lambda m: "~%02X" % ord(m.group(0)), out)


def manager_rel_path(manager_id: str) -> str:
    """``managers/<shard>/<safe-id>.json`` — relative to the site root."""
    return f"{SITE_DIRNAME}/{shard_of(manager_id)}/{safe_name(manager_id)}.json"


# ---------------------------------------------------------------------------
# Small numeric helpers (stdlib only; mirrors the scorer's population stats)
# ---------------------------------------------------------------------------

def _mean(xs: Sequence[float]) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def _pstdev(xs: Sequence[float]) -> float:
    """Population sd — the same one ``msZScores`` uses, not the sample sd."""
    xs = list(xs)
    if not xs:
        return 0.0
    mu = _mean(xs)
    return math.sqrt(sum((x - mu) ** 2 for x in xs) / len(xs))


def _r(value, digits: int = 6):
    """Round, passing through anything that is not a finite number."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return round(f, digits)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Per-league detail: extracted from a FULL scoring result, before compaction
# ---------------------------------------------------------------------------

def _pool_stats(managers: Sequence[Dict], component: str) -> Dict:
    """The z-score pool for one component, recovered from the full result.

    ``msScoreLeague`` computes each manager's ``z`` from the distribution of
    *shrunk* per-transaction means across the managers who have that
    component at all, then discards the distribution. Recovering ``mean`` and
    ``sd`` here is what lets a drill-down show the last step of the
    arithmetic — ``z = (shrunk - mean) / sd`` — instead of asking the reader
    to take the z on faith.

    Recomputed from the same rows the scorer used rather than exported from
    the JS, so this needs no change to the shipped scorer (and cannot be
    silently skipped by an older harness).
    """
    pool = [
        float((row.get(component) or {}).get("shrunk") or 0.0)
        for row in managers
        if int((row.get(component) or {}).get("n") or 0) > 0
    ]
    # "One participant cannot define a distribution" — the scorer leaves z at
    # 0 below two, and so must any explanation of it.
    if len(pool) < 2:
        return {"n_managers": len(pool), "mean": 0.0, "sd": 0.0,
                "degenerate": True}
    return {
        "n_managers": len(pool),
        "mean": _r(_mean(pool)),
        "sd": _r(_pstdev(pool)),
        "degenerate": _pstdev(pool) <= 0,
    }


def _pick_event(pick: Dict) -> Dict:
    """One scored draft pick, in the form the drill-down explains.

    Field names are short because there are ~26,500 of these across the
    corpus and every one is shipped to a browser; the meaning is documented
    here and rendered in full by the page.
    """
    return {
        "season": pick.get("season"),
        "draft": pick.get("draftLabel"),
        "slot": pick.get("slot"),
        "round": pick.get("round"),
        "player": pick.get("name"),
        "pos": pick.get("pos"),
        "sleeper_id": pick.get("playerId") or None,
        "date": pick.get("date"),
        # Valuation and its basis. PR #63's distinction: "point-in-time" is a
        # price read from the dated KTC history at the transaction date;
        # "current" means only today's board was available for that asset, so
        # the pick is priced at a value it did not have on the day.
        "basis": pick.get("basis") or "point-in-time",
        "value_at": _r(pick.get("vAt"), 2),
        "value_at_date": pick.get("vAtDate"),
        "peak": _r(pick.get("peak"), 2),
        "peak_date": pick.get("peakDate"),
        # The three numbers that add up to the score.
        "capture": _r(pick.get("capture"), 4),
        "expected": _r(pick.get("expected"), 4),
        "surplus": _r(pick.get("surplus"), 4),
    }


def _trade_event(trade: Dict, manager_id: str) -> Optional[Dict]:
    """This manager's side of one trade, or ``None`` if they are not in it."""
    side = None
    for s in trade.get("sides") or []:
        if str(s.get("managerId")) == str(manager_id):
            side = s
            break
    if side is None:
        return None

    def assets(key: str) -> List[Dict]:
        out = []
        for a in (side.get("assets") or {}).get(key) or []:
            out.append({
                "label": a.get("label"),
                "kind": a.get("kind"),
                "pos": a.get("pos"),
                "sleeper_id": a.get("playerId") or None,
                "basis": a.get("basis") or "current",
                "value_at": _r(a.get("vAt"), 2),
                "capture": _r(a.get("capture"), 4),
                "evaluable": bool(a.get("evaluable")),
            })
        return out

    return {
        "id": trade.get("id"),
        "date": trade.get("date"),
        "basis": trade.get("basis") or "current",
        "received": assets("received"),
        "given": assets("given"),
        "capture_received": _r(side.get("received"), 4),
        "capture_given": _r(side.get("given"), 4),
        "surplus": _r(side.get("net"), 4),
        "scored": bool(trade.get("scored")),
        "unbalanced": bool(trade.get("unbalanced")),
        "partial": bool(trade.get("partial")),
        "has_faab": bool(trade.get("faab")),
    }


def _waiver_event(waiver: Dict) -> Dict:
    return {
        "date": waiver.get("date"),
        "player": waiver.get("name"),
        "pos": waiver.get("pos"),
        "sleeper_id": waiver.get("playerId") or None,
        "basis": waiver.get("basis") or "point-in-time",
        "value_at": _r(waiver.get("vAt"), 2),
        "value_at_date": waiver.get("vAtDate"),
        "peak": _r(waiver.get("peak"), 2),
        "peak_date": waiver.get("peakDate"),
        "faab": waiver.get("faab"),
        "surplus": _r(waiver.get("surplus"), 4),
    }


def league_detail_from_result(entry: Dict) -> Optional[Dict]:
    """Build the durable per-league audit from a FULL scoring result.

    Must be called on the harness output *before*
    ``crossleague.compact_result`` — compaction is what removes the audit and
    the ``shrunk`` values this needs.
    """
    result = (entry or {}).get("result") or {}
    managers = result.get("managers") or []
    audit = result.get("audit")
    if not managers:
        return None
    # A COMPACTED entry (one retained from an earlier run) has no ``audit``
    # key at all and no ``shrunk`` on its component rows. Building a detail
    # record from it would emit a document that says "evidence: complete"
    # over an empty pick list -- presenting a manager's real draft record as
    # no activity. That is precisely the silent-null class of bug that once
    # made every manager score exactly 100.0, so it fails closed here: no
    # audit means no detail record, and the published shard then says the
    # evidence is missing rather than absent.
    if audit is None:
        return None

    pools = {c: _pool_stats(managers, c) for c in COMPONENTS}

    # Bucket the audit by manager once, rather than scanning it per manager.
    picks_by: Dict[str, List[Dict]] = {}
    for p in audit.get("picks") or []:
        picks_by.setdefault(str(p.get("managerId")), []).append(p)
    waivers_by: Dict[str, List[Dict]] = {}
    for w in audit.get("waivers") or []:
        waivers_by.setdefault(str(w.get("managerId")), []).append(w)
    trades_by: Dict[str, List[Dict]] = {}
    for t in audit.get("trades") or []:
        for s in t.get("sides") or []:
            trades_by.setdefault(str(s.get("managerId")), []).append(t)

    out_managers = []
    for row in managers:
        mid = str(row.get("id"))
        comps = {}
        for c in COMPONENTS:
            comp = row.get(c) or {}
            comps[c] = {
                "n": int(comp.get("n") or 0),
                "total": _r(comp.get("total"), 4),
                "mean": _r(comp.get("mean")),
                "shrunk": _r(comp.get("shrunk")),
                "k": MS_SHRINK_K[c],
                "z": _r(comp.get("z")),
                "pool": pools[c],
            }
        picks = sorted(
            (_pick_event(p) for p in picks_by.get(mid, [])),
            key=lambda e: (str(e.get("season") or ""), e.get("slot") or 0),
        )
        # A trade that could not be priced (an unvalued asset, or legs that
        # do not balance) is reported by the scorer but NOT counted: only
        # `scored` trades increment the component's n. So the two are kept
        # apart here. `trades` is the list the arithmetic is built from and
        # must have exactly n entries; `trades_unscored` is shown to the
        # reader so a trade they remember making does not simply vanish
        # from their own ledger, and is explicitly excluded from the total.
        trades: List[Dict] = []
        trades_unscored: List[Dict] = []
        for t in trades_by.get(mid, []):
            ev = _trade_event(t, mid)
            if ev is None:
                continue
            (trades if ev["scored"] else trades_unscored).append(ev)
        waivers = [_waiver_event(w) for w in waivers_by.get(mid, [])]
        out_managers.append({
            "id": mid,
            "name": row.get("name"),
            "index": _r(row.get("index"), 4),
            "rank": row.get("rank"),
            "flags": row.get("flags") or [],
            "components": comps,
            "picks": picks,
            "trades": trades,
            "trades_unscored": trades_unscored,
            "waivers": waivers,
        })

    return {
        "schema": LEAGUE_DETAIL_SCHEMA,
        "league_id": entry.get("league_id"),
        "name": entry.get("name"),
        "season": entry.get("season"),
        "n_teams": entry.get("n_teams"),
        "lineage_ids": entry.get("lineage_ids"),
        "generated_at": _now_iso(),
        "drafts": result.get("drafts") or [],
        "weights": result.get("weights") or {},
        "managers": out_managers,
    }


# ---------------------------------------------------------------------------
# Durable store
# ---------------------------------------------------------------------------

def detail_dir(data_root: Path) -> Path:
    return Path(data_root) / DETAIL_DIRNAME


def write_league_detail(dir_path: Path, detail: Dict) -> Optional[Path]:
    """Persist one league's audit, gzipped. Never raises."""
    league_id = (detail or {}).get("league_id")
    if not league_id:
        return None
    try:
        d = Path(dir_path)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{safe_name(str(league_id))}.json.gz"
        raw = json.dumps(detail, separators=(",", ":")).encode("utf-8")
        # mtime=0 so an unchanged league produces byte-identical output and
        # a cache layer can tell that nothing moved. Both handles are closed
        # explicitly -- GzipFile does not own a fileobj passed to it.
        with open(path, "wb") as raw_fh:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw_fh,
                               mtime=0) as fh:
                fh.write(raw)
        return path
    except OSError:
        return None


def read_league_details(dir_path: Path) -> Dict[str, Dict]:
    """Load every retained league audit, keyed by league id.

    Never raises and never lets one corrupt file cost the rest: a detail
    store is an optimisation over the corpus, so a bad entry must degrade to
    "no evidence for that league" rather than failing the build.
    """
    out: Dict[str, Dict] = {}
    d = Path(dir_path)
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.json.gz")):
        try:
            with gzip.open(path, "rb") as fh:
                data = json.loads(fh.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("schema") != LEAGUE_DETAIL_SCHEMA:
            continue
        lid = data.get("league_id")
        if lid:
            out[str(lid)] = data
    return out


def prune_league_details(dir_path: Path, keep_league_ids: Iterable[str]) -> int:
    """Drop audits for leagues no longer in the corpus. Returns files removed.

    Without this the detail store is append-only and grows without bound as
    the crawl's dedup drops superseded lineage members.
    """
    keep = {safe_name(str(i)) + ".json.gz" for i in keep_league_ids}
    removed = 0
    d = Path(dir_path)
    if not d.is_dir():
        return 0
    for path in list(d.glob("*.json.gz")):
        if path.name not in keep:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------------------
# Assembling the per-manager view
# ---------------------------------------------------------------------------

def build_manager_detail(row: Dict, details: Dict[str, Dict],
                         corpus: Optional[Dict] = None) -> Dict:
    """Assemble one manager's drill-down from the corpus row + league audits.

    The corpus row stays authoritative for the headline numbers. This adds
    the evidence under them and, per league, says plainly whether the
    evidence is actually present — a league whose audit was not retained is
    reported as missing, never rendered as an absence of activity.
    """
    mid = str(row.get("manager_id"))
    leagues_out = []
    n_with_evidence = 0

    for lg in row.get("leagues") or []:
        lid = str(lg.get("league_id"))
        detail = details.get(lid)
        block = {
            "league_id": lid,
            "name": lg.get("name"),
            "season": lg.get("season"),
            "n_teams": lg.get("n_teams"),
            "index": lg.get("index"),
            "rank": lg.get("rank"),
            "flags": lg.get("flags") or [],
            # What the corpus already knew, kept so the page can compare the
            # evidence against the published number rather than replacing it.
            "components": lg.get("components") or {},
        }
        mrow = None
        if detail:
            for m in detail.get("managers") or []:
                if str(m.get("id")) == mid:
                    mrow = m
                    break
        if mrow is None:
            block["evidence"] = "missing"
            block["evidence_why"] = (
                "this league's pick-level audit is not in the build cache — "
                "it was scored on an earlier run whose detail was not "
                "retained. Its score above still counts."
            )
            leagues_out.append(block)
            continue

        n_with_evidence += 1
        block["evidence"] = "complete"
        block["detail"] = {
            "components": mrow.get("components") or {},
            "picks": mrow.get("picks") or [],
            "trades": mrow.get("trades") or [],
            "trades_unscored": mrow.get("trades_unscored") or [],
            "waivers": mrow.get("waivers") or [],
            "drafts": detail.get("drafts") or [],
        }
        leagues_out.append(block)

    coverage = {
        "n_leagues": len(leagues_out),
        "n_leagues_with_evidence": n_with_evidence,
        "complete": n_with_evidence == len(leagues_out) and bool(leagues_out),
    }

    return {
        "schema": MANAGER_DETAIL_SCHEMA,
        "manager_id": mid,
        "display_name": row.get("display_name"),
        "generated_at": _now_iso(),
        "corpus_generated_at": (corpus or {}).get("generated_at"),
        "cross_index": row.get("cross_index"),
        "composite": row.get("composite"),
        "rank": row.get("rank"),
        "percentile": row.get("percentile"),
        "n_leagues": row.get("n_leagues"),
        "flags": row.get("flags") or [],
        "components": row.get("components") or {},
        "weights": dict(MS_WEIGHTS),
        "shrink_k": dict(MS_SHRINK_K),
        "coverage": coverage,
        "leagues": leagues_out,
    }


def publish_manager_details(out_root: Path, corpus: Dict,
                            details: Dict[str, Dict]) -> Dict:
    """Write every manager's shard under ``<out_root>/managers/``.

    Returns a stats block for the build log and the corpus artifact, so the
    page can say how much evidence exists without probing 1,400 URLs.
    """
    out_root = Path(out_root)
    base = out_root / SITE_DIRNAME
    base.mkdir(parents=True, exist_ok=True)

    seen: Dict[str, str] = {}
    n_files = 0
    n_bytes = 0
    n_complete = 0
    n_partial = 0
    n_empty = 0
    with_evidence: List[str] = []

    for row in corpus.get("leaderboard") or []:
        mid = str(row.get("manager_id") or "")
        if not mid:
            continue
        rel = manager_rel_path(mid)
        # Injectivity is a build-time guarantee, not an assumption: two ids
        # mapping to one path would serve one manager's evidence under the
        # other's name, which is exactly the class of silent wrongness this
        # feature exists to remove.
        if rel in seen and seen[rel] != mid:
            raise ValueError(
                f"manager id collision: {mid!r} and {seen[rel]!r} both map to "
                f"{rel!r}. safe_name() must be injective."
            )
        seen[rel] = mid

        doc = build_manager_detail(row, details, corpus)
        cov = doc["coverage"]
        if cov["complete"]:
            n_complete += 1
        elif cov["n_leagues_with_evidence"]:
            n_partial += 1
        else:
            n_empty += 1
        if cov["n_leagues_with_evidence"]:
            with_evidence.append(mid)

        path = out_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(doc, separators=(",", ":"))
        path.write_text(payload, encoding="utf-8")
        n_files += 1
        n_bytes += len(payload.encode("utf-8"))

    return {
        "schema": MANAGER_DETAIL_SCHEMA,
        "dir": SITE_DIRNAME,
        "shard_buckets": SHARD_BUCKETS,
        # ``available`` is what the page branches on. Without it, a build
        # that wrote zero shards is indistinguishable from one that wrote
        # thousands until the reader counts, and "no managers" silently
        # reads as "no activity" rather than "no audit data in this build".
        "available": bool(n_files),
        "reason": (None if n_files else "audit_data_unavailable"),
        "n_files": n_files,
        "bytes": n_bytes,
        "n_leagues_with_detail": len(details),
        "n_managers_complete": n_complete,
        "n_managers_partial": n_partial,
        "n_managers_without_evidence": n_empty,
    }


# ---------------------------------------------------------------------------
# The evidence gate — only rank a manager whose score can be explained
# ---------------------------------------------------------------------------
#
# Phil, 2026-09-22: "I only want to display managers where you can see the
# evidence of their draft, waiver wire, trade data ... for example you
# cannot see the data for this manager: FantasyticBeast. Users like that
# should not be included if we cannot see what is driving the manager score
# in the drill down."
#
# The board and the drill-down have always had different coverage, for a
# structural reason rather than a bug. A manager's SCORE survives forever:
# ``crossleague`` re-aggregates retained per-league results, so a league
# scored months ago still contributes. A manager's EVIDENCE lives in the
# build cache (``data/cross_league/detail/``), which is evicted on a cache
# miss and only refilled for leagues the current run actually re-scored. So
# a manager whose leagues have not been re-scored recently keeps a real,
# correct score with nothing behind it.
#
# Rendering them anyway produces the worst available outcome: a ranked row
# that invites a click and then explains nothing. A visitor cannot tell that
# apart from "this manager did nothing", which is a claim about a real
# person that we have no basis for.
#
# Two decisions here are deliberate and worth defending:
#
# 1. ``rank`` is NOT recomputed. The withheld managers were genuinely
#    scored and genuinely beat the people below them; renumbering would
#    promote someone to "rank 1 of the indexed sample" when they were rank
#    4. So the displayed ranks keep gaps, and the page says why. A gap is a
#    true statement about a missing row; a renumber is a false statement
#    about standing.
#
# 2. The withheld count is published, not swallowed. "120 of 1,407" is the
#    difference between a board that is small and a board that is hiding
#    something.


def managers_fully_explained(corpus: Dict, details: Dict[str, Dict]) -> set:
    """Manager ids whose EVERY indexed league has a retained audit.

    This is the predicate the published board is gated on, and it is
    deliberately stricter than :func:`managers_with_evidence`.

    ``crossleague_js`` renders two warning banners on a drill-down:

    * "No transaction-level evidence is retained for any of this manager's
      leagues yet" -- when ``coverage.n_leagues_with_evidence`` is 0;
    * "Pick-level evidence is available for N of M indexed league(s)" plus
      a per-league "this league's pick-level audit is not in the build
      cache" -- when ``coverage.complete`` is False.

    Phil, 2026-09-22: "remove all examples where you cannot see the
    transactions that are driving the manager score." A manager with one
    audited league out of four satisfies "at least one" and still renders
    the second banner, so an at-least-one gate leaves exactly the rows he
    asked to have removed. Requiring every league to be audited is what
    makes both banners unreachable.

    Mirrors ``coverage.complete`` in :func:`build_manager_detail`;
    ``test_gate_predicate_matches_coverage_complete`` asserts they agree
    row-for-row on the committed corpus, so the two cannot drift.
    """
    by_league: Dict[str, set] = {}
    for lid, detail in (details or {}).items():
        by_league[str(lid)] = {
            str(m.get("id")) for m in (detail.get("managers") or [])
        }

    out: set = set()
    for row in (corpus or {}).get("leaderboard") or []:
        mid = str(row.get("manager_id") or "")
        if not mid:
            continue
        leagues = row.get("leagues") or []
        if not leagues:
            # No indexed league is not "explained"; coverage.complete is
            # False for an empty league list too (``and bool(leagues_out)``).
            continue
        if all(mid in by_league.get(str(lg.get("league_id")), ())
               for lg in leagues):
            out.add(mid)
    return out


def managers_with_evidence(corpus: Dict, details: Dict[str, Dict]) -> set:
    """Manager ids that have at least one league audit in ``details``.

    Mirrors the per-league lookup in :func:`build_manager_detail` -- a
    manager has evidence in a league when that league's retained audit
    contains a row with their id. Kept as a separate, cheap pass (an index
    of league -> ids, rather than assembling every document) so the gate can
    be computed without paying for the full publish, and so it is directly
    testable. ``test_evidence_gate_agrees_with_build_manager_detail`` asserts
    the two do not drift.
    """
    by_league: Dict[str, set] = {}
    for lid, detail in (details or {}).items():
        by_league[str(lid)] = {
            str(m.get("id")) for m in (detail.get("managers") or [])
        }

    out: set = set()
    for row in (corpus or {}).get("leaderboard") or []:
        mid = str(row.get("manager_id") or "")
        if not mid:
            continue
        for lg in row.get("leagues") or []:
            if mid in by_league.get(str(lg.get("league_id")), ()):
                out.add(mid)
                break
    return out


def apply_evidence_gate(corpus: Dict, details: Dict[str, Dict]) -> Dict:
    """Restrict the published boards to managers whose score is explainable.

    Mutates ``corpus`` in place -- it is called on the in-memory copy that
    ``write_corpus_artifact`` publishes into the site, NOT on the committed
    ``data/cross_league/corpus.json``. The crawler's resumption state must
    keep every scored manager, or a manager would be permanently dropped the
    first time their detail aged out of the cache. Only the view is gated.

    Returns the gate block, which is also attached as ``corpus['evidence_gate']``
    for the page to render.

    NO FAIL-OPEN. An earlier version skipped the gate entirely when
    ``details`` was empty, reasoning that "no evidence for anybody" is a
    different fact from "no evidence for this manager". It is -- but the
    published behaviour was identical either way: every scored manager
    shipped in the artifact with nothing behind them. On the live site that
    meant 2,432 managers in ``crossleague_corpus.json`` and drill-downs
    reading "No transaction-level evidence is retained", which is the exact
    output the gate exists to prevent, arrived at by a different route.

    So an empty detail store now withholds every row and says so. Phil,
    2026-09-22: losing a board is acceptable; showing scores nobody can
    check is not. The distinction that motivated the fail-open is kept
    where it belongs -- in ``why`` and ``reason``, so the page can explain
    a build-wide outage differently from a per-manager gap without
    publishing unexplainable rows to do it.
    """
    if corpus is None:
        return {"applied": False, "why": "no corpus"}

    leaderboard = corpus.get("leaderboard") or []
    draft_board = corpus.get("draft_board") or []
    n_scored = len(leaderboard)

    if not details:
        # Fail CLOSED: withhold everything, publish the reason.
        corpus["leaderboard"] = []
        corpus["draft_board"] = []
        corpus["manager_detail"] = {
            "available": False,
            "reason": "audit_data_unavailable",
            "n_files": 0,
        }
        gate = {
            "applied": True,
            "reason": "audit_data_unavailable",
            "n_scored": n_scored,
            "n_shown": 0,
            "n_withheld": n_scored,
            "n_draft_scored": len(draft_board),
            "n_draft_shown": 0,
            "n_draft_withheld": len(draft_board),
            "why": (
                "audit data unavailable in this build: no per-league "
                "transaction audits were retained, so no manager's score "
                "can be explained. The board is withheld rather than "
                "published unexplained. Scores are unaffected and return "
                "when a crawl repopulates data/cross_league/detail/."
            ),
        }
        corpus["evidence_gate"] = gate
        return gate

    keep = managers_fully_explained(corpus, details)

    kept_lb = [r for r in leaderboard
               if str(r.get("manager_id") or "") in keep]
    kept_db = [r for r in draft_board
               if str(r.get("manager_id") or "") in keep]

    # Names of a few withheld managers, for the build log only. Useful when
    # someone asks "why is X gone" and nobody wants to re-run the crawl to
    # find out. Not published: a list of excluded handles on a public page
    # is a "managers we could not vouch for" board, which is not a thing
    # this project ships (see the no-worst-managers stance in
    # crossleague_page).
    withheld_sample = [
        str(r.get("display_name") or r.get("manager_id"))
        for r in leaderboard
        if str(r.get("manager_id") or "") not in keep
    ][:10]

    corpus["leaderboard"] = kept_lb
    corpus["draft_board"] = kept_db

    gate = {
        "applied": True,
        "reason": ("all_withheld" if not kept_lb else "gated"),
        "n_scored": n_scored,
        "n_shown": len(kept_lb),
        "n_withheld": n_scored - len(kept_lb),
        "n_draft_scored": len(draft_board),
        "n_draft_shown": len(kept_db),
        "n_draft_withheld": len(draft_board) - len(kept_db),
        "why": (
            "a manager is listed only when EVERY league counted in their "
            "score has its pick-level audit retained, so each row opens to "
            "the transactions that produced the number and no drill-down "
            "reports missing evidence. Ranks are the ranks among all scored "
            "managers and therefore skip the withheld ones."
        ),
    }
    corpus["evidence_gate"] = gate
    gate_log = dict(gate)
    gate_log["withheld_sample"] = withheld_sample
    return gate_log


# ---------------------------------------------------------------------------
# Reconciliation — the claim that the drill-down explains the published score
# ---------------------------------------------------------------------------

def reconcile_manager(doc: Dict, *, tol: float = 5e-4) -> List[str]:
    """Check a manager detail against the score it claims to explain.

    This is the test that matters. A drill-down that renders beautifully and
    tells a different story than the leaderboard is worse than no drill-down
    at all, because it looks like an explanation. So every step of the chain
    is re-derived here from the events themselves:

        Σ surplus over events  ->  total
        total / n              ->  mean
        mean · n/(n+k)         ->  shrunk
        (shrunk - poolμ)/poolσ ->  z            (within-league)
        Σ n·z / Σ n            ->  zbar         (evidence-weighted)
        zbar · N/(N+k)         ->  Z            (cross-league shrink)
        Σ w·Z (renormalised)   ->  composite
        100 + 15 · composite   ->  cross_index

    Returns a list of human-readable discrepancies; empty means the numbers
    on the drill-down add up to the number on the board.

    Leagues whose audit is missing are skipped for the event-level steps and
    reported, not silently treated as zero — conflating "no evidence" with
    "no activity" is the failure mode that once made every manager 100.0.
    """
    problems: List[str] = []
    mid = doc.get("manager_id")

    def near(a, b, label, tolerance=tol):
        if a is None or b is None:
            problems.append(f"{mid}: {label} missing ({a!r} vs {b!r})")
            return False
        if abs(float(a) - float(b)) > tolerance:
            problems.append(
                f"{mid}: {label} {float(a):.6f} != {float(b):.6f} "
                f"(delta {abs(float(a) - float(b)):.2e})"
            )
            return False
        return True

    # ---- per-league: events -> z -------------------------------------
    per_league: Dict[str, Dict[str, Tuple[int, float]]] = {}
    for lg in doc.get("leagues") or []:
        lid = str(lg.get("league_id"))
        pub = lg.get("components") or {}
        if lg.get("evidence") != "complete":
            # Still usable for the cross-league roll-up: the corpus row
            # carries n and z even when the audit does not survive.
            per_league[lid] = {
                c: (int((pub.get(c) or {}).get("n") or 0),
                    float((pub.get(c) or {}).get("z") or 0.0))
                for c in COMPONENTS
            }
            continue

        det = (lg.get("detail") or {}).get("components") or {}
        events = {
            "draft": (lg.get("detail") or {}).get("picks") or [],
            "trade": (lg.get("detail") or {}).get("trades") or [],
            "waiver": (lg.get("detail") or {}).get("waivers") or [],
        }
        per_league[lid] = {}
        for c in COMPONENTS:
            d = det.get(c) or {}
            n = int(d.get("n") or 0)
            per_league[lid][c] = (n, float(d.get("z") or 0.0))
            if not n:
                continue

            evs = events[c]
            if len(evs) != n:
                problems.append(
                    f"{mid}/{lid}/{c}: {len(evs)} events listed but n={n}"
                )
                continue

            # events -> total
            total = sum(float(e.get("surplus") or 0.0) for e in evs)
            near(total, d.get("total"), f"{lid}/{c} Σsurplus vs total",
                 max(tol, abs(total) * 1e-6 + 1e-3))
            # total -> mean
            near(float(d.get("total") or 0.0) / n, d.get("mean"),
                 f"{lid}/{c} total/n vs mean")
            # mean -> shrunk
            near(shrink(float(d.get("mean") or 0.0), n, MS_SHRINK_K[c]),
                 d.get("shrunk"), f"{lid}/{c} shrink(mean,n,k) vs shrunk")
            # shrunk -> z
            pool = d.get("pool") or {}
            sd = float(pool.get("sd") or 0.0)
            if pool.get("n_managers", 0) >= 2 and sd > 0:
                z = (float(d.get("shrunk") or 0.0) - float(pool.get("mean") or 0.0)) / sd
                near(z, d.get("z"), f"{lid}/{c} (shrunk-μ)/σ vs z", 1e-3)
            elif float(d.get("z") or 0.0) != 0.0:
                problems.append(
                    f"{mid}/{lid}/{c}: degenerate pool but z="
                    f"{d.get('z')} (scorer leaves z at 0)"
                )

            # The published corpus row for this league must agree with the
            # audit; if they disagree the drill-down is explaining a
            # different league-season than the board counted.
            pubc = pub.get(c) or {}
            if pubc:
                if int(pubc.get("n") or 0) != n:
                    problems.append(
                        f"{mid}/{lid}/{c}: corpus n={pubc.get('n')} but "
                        f"audit n={n}"
                    )
                near(pubc.get("z"), d.get("z"),
                     f"{lid}/{c} corpus z vs audit z", 1e-3)

    # ---- cross-league: z -> zbar -> Z ---------------------------------
    comps = doc.get("components") or {}
    for c in COMPONENTS:
        pubc = comps.get(c) or {}
        N = sum(v[c][0] for v in per_league.values())
        if int(pubc.get("n") or 0) != N:
            problems.append(
                f"{mid}: {c} corpus N={pubc.get('n')} but leagues sum to {N}"
            )
        if not N:
            continue
        zbar = sum(v[c][0] * v[c][1] for v in per_league.values()) / N
        near(zbar, pubc.get("zbar"), f"{c} evidence-weighted zbar", 1e-3)
        Z = shrink(zbar, N, MS_SHRINK_K[c])
        near(Z, pubc.get("z"), f"{c} shrink(zbar,N,k) vs Z", 1e-3)

    # ---- composite and index ------------------------------------------
    #
    # The effective weights are a property of the CORPUS, not of the
    # manager: ``crossleague.aggregate`` renormalises over the components at
    # least two managers anywhere in the corpus have, then applies that same
    # weight vector to everyone. So a manager who has never traded is scored
    # as corpus-average on trade (Z = 0 carried at full weight) and flagged,
    # rather than having the trade weight redistributed onto their draft.
    #
    # Reconciling against a per-manager renormalisation instead was wrong,
    # and this check is what caught it: it inflated the composite of the one
    # fixture manager with no trades from 0.545 to 0.838. The published
    # per-component ``weight`` is the authority, so it is what is used here.
    composite = sum(
        float((comps.get(c) or {}).get("weight") or 0.0)
        * float((comps.get(c) or {}).get("z") or 0.0)
        for c in COMPONENTS
    )
    near(composite, doc.get("composite"), "sum(weight x Z) vs composite", 1e-3)
    near(100.0 + 15.0 * float(doc.get("composite") or 0.0),
         doc.get("cross_index"), "100 + 15 x composite vs cross_index", 0.05)

    return problems
