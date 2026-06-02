"""v3.10 — PFR similarity sanity-check audit.

For the top-50 Dynasty Rankings players, compare our Fantasy-Point Arc
Comparables (top 10) against Pro Football Reference's "Similarity
Scores" (#all_sim_scores → "Career" row of 10 similars). Report set
overlap, Jaccard, misses (in PFR not us), extras (in us not PFR), plus
heuristic root-cause buckets so we can spot themes.

Outputs:
    docs/audits/pfr_comparison_audit.md
    docs/audits/pfr_audit_summary.csv

Run from the repo root:
    PYTHONPATH=src python3 scripts/audit/pfr_similarity_audit.py
        [--top 50] [--rebuild]

The script is idempotent: PFR HTML lives under data/pfr_cache (re-used
by the existing v2.4 scraper). Per-player extracted comp tables get
cached under data/cache/pfr_career_stats/<pfr_id>.json (the new v3.10
cache); audit-specific data is recomputed on each run.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import logging
import re
import sys
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bs4 import BeautifulSoup, Comment  # noqa: E402

from dynasty.sources import pro_football_reference_seasonal as pfr  # noqa: E402

log = logging.getLogger("pfr_audit")
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)

ENGINE_RANKINGS = REPO_ROOT / "dynasty_site" / "engine_rankings.json"
PLAYERS_HTML_DIR = REPO_ROOT / "dynasty_site" / "players"
NFLVERSE_PLAYERS = REPO_ROOT / "data" / "nflverse" / "players.csv.gz"
DOCS_DIR = REPO_ROOT / "docs" / "audits"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slug(name: str, pid: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{s}-{pid.replace('-', '')[-6:]}"


def load_gsis_to_pfr() -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not NFLVERSE_PLAYERS.exists():
        log.warning("nflverse players file missing: %s", NFLVERSE_PLAYERS)
        return out
    with gzip.open(NFLVERSE_PLAYERS, "rt", encoding="utf-8",
                   errors="replace") as fh:
        for row in csv.DictReader(fh):
            gsis = (row.get("gsis_id") or "").strip()
            pfr_id = (row.get("pfr_id") or "").strip()
            if gsis and pfr_id:
                out[gsis] = pfr_id
    return out


_NAME_NORMALIZE_RE = re.compile(r"[^a-z0-9]")
# v3.10 badges leak into rendered comp cells: era chip "⏳ 1998", washed
# out chip, PFR HoF marker "*". After ASCII transliteration the chip
# emoji is dropped but the year remains, so we strip a trailing 4-digit
# year too.
_BADGE_TAIL_RE = re.compile(
    r"(\u23f3.*$|\u2b50.*$|washed.*$|\s+\d{4}\b|\*+$|\++$)",
    re.IGNORECASE,
)


def normalize_name(s: str) -> str:
    """Lowercase, strip punctuation, normalize unicode + badge tails."""
    if not s:
        return ""
    # Strip badges in the unicode form first (so era chip catches).
    s = _BADGE_TAIL_RE.sub("", s)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    # Defensive: a year tail may have been hidden by an ASCII
    # transliteration that ate the chip. Strip leading/trailing space.
    s = re.sub(r"\b(19|20)\d{2}\b", "", s)
    return _NAME_NORMALIZE_RE.sub("", s.lower())


# ---------------------------------------------------------------------------
# Our model's comps (from the rendered player profile page)
# ---------------------------------------------------------------------------

def load_our_comps(player_name: str, player_id: str) -> List[str]:
    """Pull the top-10 comp NAMES from the player's static profile page."""
    slug = _slug(player_name, player_id)
    path = PLAYERS_HTML_DIR / f"{slug}.html"
    if not path.exists():
        return []
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "lxml")
    # The comp table is the first <table> with a tbody after the
    # "Fantasy-Point Arc Comparables" heading. To be robust, we just
    # find the first table whose tbody first-row has a td.name.
    for table in soup.find_all("table"):
        tb = table.find("tbody")
        if not tb:
            continue
        rows = tb.find_all("tr")
        if not rows:
            continue
        first_name_td = rows[0].find("td", class_="name")
        if not first_name_td:
            continue
        comps: List[str] = []
        for tr in rows[:10]:
            td = tr.find("td", class_="name")
            if td:
                # Strip era / washed-out badges from text — they live
                # in spans inside the same cell.
                # Capture only the leading text-node text.
                lead = td.find(string=True)
                comps.append(lead.strip() if lead else td.get_text(strip=True))
        return comps
    return []


# ---------------------------------------------------------------------------
# PFR's Career sim_scores row
# ---------------------------------------------------------------------------

def _extract_sim_scores_table(soup: BeautifulSoup):
    div = soup.find("div", id="all_sim_scores")
    if div is None:
        return None
    # First try direct.
    t = div.find("table", id="sim_scores")
    if t is not None and t.find("tbody"):
        return t
    # Fall back to commented HTML.
    for c in div.find_all(string=lambda s: isinstance(s, Comment)):
        inner = BeautifulSoup(c, "lxml")
        t = inner.find("table", id="sim_scores")
        if t is not None and t.find("tbody"):
            return t
    return None


def load_pfr_career_comps(pfr_id: str) -> Tuple[List[str], Dict]:
    """Return ``(career_row_comps_top10, meta)`` for ``pfr_id``.

    ``meta`` carries:
        ``fetch_error`` — present iff scrape failed
        ``thru_age_comps`` — most recent Through-Age row's similars
    """
    meta: Dict = {}
    try:
        html = pfr.fetch_player_bio_html(pfr_id)
    except Exception as exc:  # noqa: BLE001
        meta["fetch_error"] = str(exc)
        return [], meta
    soup = BeautifulSoup(html, "lxml")
    table = _extract_sim_scores_table(soup)
    if table is None:
        meta["fetch_error"] = "no sim_scores table"
        return [], meta

    career_row = None
    thru_rows: List[List[str]] = []
    for tr in table.tbody.find_all("tr"):
        thru = ""
        similars = ""
        for cell in tr.find_all(["th", "td"]):
            stat = cell.get("data-stat")
            txt = cell.get_text(strip=True)
            if stat == "thru_years":
                thru = txt
            elif stat == "similars":
                similars = txt
        if not similars:
            continue
        names = [n.strip() for n in similars.split(",") if n.strip()]
        if thru == "Career":
            career_row = names
        else:
            thru_rows.append((thru, names))

    if thru_rows:
        meta["last_thru_age_comps"] = thru_rows[-1][1]
        meta["last_thru_age_label"] = thru_rows[-1][0]

    if career_row is None:
        # Some players (mostly early-career) only have through-age rows.
        meta["fetch_error"] = "no Career row"
        if thru_rows:
            career_row = thru_rows[-1][1]
            meta["fell_back_to_thru_age"] = thru_rows[-1][0]
    return (career_row or [])[:10], meta


# ---------------------------------------------------------------------------
# Overlap math
# ---------------------------------------------------------------------------

def set_overlap_metrics(ours: List[str], theirs: List[str]) -> Dict:
    """Compute overlap@10 / @20, Jaccard, misses, extras."""
    ours_norm = [normalize_name(x) for x in ours]
    theirs_norm = [normalize_name(x) for x in theirs]
    ours_set = set(filter(None, ours_norm))
    theirs_set = set(filter(None, theirs_norm))

    overlap_10 = len(ours_set & set(theirs_norm[:10]))
    overlap_20 = len(ours_set & set(theirs_norm[:20]))
    union = ours_set | theirs_set
    jaccard = (len(ours_set & theirs_set) / len(union)) if union else 0.0

    # Build display-name lists for "misses" / "extras", using the
    # original strings.
    def _display(name_list, normalized_target_set):
        seen = set()
        out = []
        for orig, n in zip(name_list, [normalize_name(x) for x in name_list]):
            if n in normalized_target_set and n not in seen:
                seen.add(n)
                out.append(orig)
        return out

    pfr_misses = _display(theirs, ours_set ^ theirs_set & theirs_set
                          if False else theirs_set - ours_set)
    ours_extras = _display(ours, ours_set - theirs_set)

    return {
        "overlap_at_10": overlap_10,
        "overlap_at_20": overlap_20,
        "jaccard": jaccard,
        "pfr_misses": pfr_misses,
        "ours_extras": ours_extras,
    }


# ---------------------------------------------------------------------------
# Heuristic root-cause categorisation
# ---------------------------------------------------------------------------

# These are HEURISTIC labels — they answer the prompt "why is this comp
# in/out", but the deeper diagnosis is up to the human review of the
# audit markdown. Keep the buckets simple so the themes section can
# aggregate by category.

# v3.10 audit buckets — curated from the first run against top-50 ranked
# players. These are HEURISTIC; un-bucketed names just fall through as
# "uncategorised" which the themes section also counts.

DURABLE_LEGENDS = {
    "tom brady", "brett favre", "drew brees", "peyton manning",
    "frank gore", "adrian peterson", "larry fitzgerald", "jerry rice",
    "antonio gates", "tony gonzalez", "jason witten", "philip rivers",
    "matt ryan", "eli manning", "ben roethlisberger", "aaron rodgers",
    "steve mcnair", "donovan mcnabb", "cam newton",
}

PRE_1999_CORPUS = {
    "dan marino", "joe montana", "john elway", "fran tarkenton",
    "warren moon", "boomer esiason", "ken anderson", "ken stabler",
    "johnny unitas", "roger staubach", "bart starr", "dan fouts",
    "jim kelly", "steve young", "troy aikman", "jim mcmahon",
    "jeff george", "bernie kosar",
    "walter payton", "barry sanders", "marcus allen", "thurman thomas",
    "emmitt smith", "jim brown", "ottis anderson", "earl campbell",
    "eric dickerson", "tony dorsett", "franco harris",
    "steve largent", "lance alworth", "paul warfield", "bob hayes",
    "michael irvin", "cris carter", "tim brown", "jerry rice",
    "andre rison", "sterling sharpe", "henry ellard",
    "john hadl", "daryle lamonica", "drew bledsoe",
}

EARLY_CAREER_BUSTS = {
    "robert griffin iii", "vince young", "andre brown",
    "cadillac williams", "daunte culpepper", "michael vick",
    "mike vick", "marcus mariota", "blaine gabbert",
    "jake plummer", "jay cutler", "david garrard", "jameis winston",
}

ACTIVE_MODERN_PFR_FAVOURED = {
    "patrick mahomes", "lamar jackson", "justin herbert", "joe burrow",
    "jalen hurts", "jared goff", "baker mayfield", "dak prescott",
    "trevor lawrence", "andrew luck", "kyler murray", "josh allen",
    "sam darnold", "matthew stafford", "tua tagovailoa",
    "deshaun watson", "daniel jones", "trevor siemian",
    "saquon barkley", "christian mccaffrey", "ezekiel elliott",
    "derrick henry", "nick chubb", "alvin kamara", "aaron jones",
    "jonathan taylor", "josh jacobs", "bijan robinson",
    "tyreek hill", "davante adams", "mike evans", "justin jefferson",
    "jamarr chase", "ja'marr chase", "ceedee lamb", "amon-ra st brown",
    "amon ra st brown", "a.j. brown", "aj brown", "chris godwin",
    "deebo samuel", "keenan allen", "stefon diggs", "calvin ridley",
    "travis kelce", "george kittle", "mark andrews",
}

MID_TIER_VETERAN = {
    # The "average career" PFR sim_scores often surface — capable
    # journeymen rather than legends.
    "aaron brooks", "trent green", "danny white", "jake delhomme",
    "matt schaub", "brian sipe", "kurt warner", "colin kaepernick",
    "teddy bridgewater", "scott mitchell", "bert jones",
    "randall cunningham", "jeff garcia", "chad pennington",
    "steve grogan", "steve deberg", "vinny testaverde",
}


def _in_bucket(name: str, bucket: set) -> bool:
    target = normalize_name(name)
    return any(normalize_name(b) == target for b in bucket)


def categorise_miss(name: str) -> str:
    """Categorise a PFR comp that our top-10 missed."""
    if _in_bucket(name, ACTIVE_MODERN_PFR_FAVOURED):
        return "active-modern-PFR-prefers"
    if _in_bucket(name, MID_TIER_VETERAN):
        return "mid-tier-veteran-PFR-likes"
    if _in_bucket(name, DURABLE_LEGENDS):
        return "longevity-favoured-by-PFR"
    if _in_bucket(name, EARLY_CAREER_BUSTS):
        return "early-career-bust-PFR-keeps"
    if _in_bucket(name, PRE_1999_CORPUS):
        return "pre1999-comp-PFR-also-pulls"
    return "uncategorised"


def categorise_extra(name: str) -> str:
    """Categorise our extra comp PFR didn't surface."""
    if _in_bucket(name, PRE_1999_CORPUS):
        return "pre1999-corpus-comp"
    if _in_bucket(name, DURABLE_LEGENDS):
        return "durable-legend-we-over-pull"
    if _in_bucket(name, EARLY_CAREER_BUSTS):
        return "washed-out-bust-overweighted-by-arc"
    if _in_bucket(name, ACTIVE_MODERN_PFR_FAVOURED):
        return "active-modern-also-in-ours"
    return "uncategorised"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run_audit(top_n: int = 50) -> List[Dict]:
    if not ENGINE_RANKINGS.exists():
        raise SystemExit(f"engine_rankings.json missing at {ENGINE_RANKINGS}; "
                         "run generate_site first")
    rankings = json.loads(ENGINE_RANKINGS.read_text(encoding="utf-8"))
    rankings = [r for r in rankings if r.get("overall_rank") is not None]
    rankings.sort(key=lambda r: r.get("overall_rank") or 9999)
    rankings = rankings[:top_n]

    gsis_to_pfr = load_gsis_to_pfr()
    results: List[Dict] = []
    for row in rankings:
        pid = row["player_id"]
        name = row["name"]
        pos = row.get("position", "")
        pfr_id = (
            pid[4:] if pid.startswith("pfr_") else gsis_to_pfr.get(pid)
        )

        ours = load_our_comps(name, pid)
        if not ours:
            log.warning("no rendered comps for %s (%s); skipping",
                        name, pid)
            results.append({
                "rank": row.get("overall_rank"),
                "name": name, "position": pos, "player_id": pid,
                "pfr_id": pfr_id, "ours_top10": [], "pfr_top10": [],
                "metrics": None, "skip_reason": "missing rendered comps",
            })
            continue

        if not pfr_id:
            results.append({
                "rank": row.get("overall_rank"),
                "name": name, "position": pos, "player_id": pid,
                "pfr_id": None, "ours_top10": ours, "pfr_top10": [],
                "metrics": None, "skip_reason": "no PFR id mapping",
            })
            continue

        log.info("auditing #%s %s (%s)", row.get("overall_rank"), name, pfr_id)
        theirs, meta = load_pfr_career_comps(pfr_id)
        if not theirs:
            results.append({
                "rank": row.get("overall_rank"),
                "name": name, "position": pos, "player_id": pid,
                "pfr_id": pfr_id, "ours_top10": ours,
                "pfr_top10": [], "metrics": None,
                "skip_reason": meta.get("fetch_error", "no PFR comps"),
            })
            continue

        m = set_overlap_metrics(ours, theirs)
        m["miss_buckets"] = {n: categorise_miss(n) for n in m["pfr_misses"]}
        m["extra_buckets"] = {n: categorise_extra(n) for n in m["ours_extras"]}
        results.append({
            "rank": row.get("overall_rank"),
            "name": name, "position": pos, "player_id": pid,
            "pfr_id": pfr_id, "ours_top10": ours,
            "pfr_top10": theirs, "metrics": m,
            "meta": meta,
        })
    return results


# ---------------------------------------------------------------------------
# Output: markdown + csv
# ---------------------------------------------------------------------------

def _md_player_block(rec: Dict) -> str:
    head = f"### #{rec['rank']} {rec['name']} ({rec['position']})\n"
    head += f"PFR: `{rec.get('pfr_id') or '—'}` · gsis: `{rec['player_id']}`\n\n"
    if rec.get("skip_reason"):
        return head + f"_Skipped: {rec['skip_reason']}._\n\n"
    m = rec["metrics"]
    body = (
        f"- overlap@10 = **{m['overlap_at_10']}/10**  ·  "
        f"overlap@20 = {m['overlap_at_20']}/20  ·  "
        f"Jaccard = {m['jaccard']:.3f}\n\n"
    )
    body += "| # | Our top-10 | PFR top-10 (Career) |\n"
    body += "|---|---|---|\n"
    ours = rec["ours_top10"] + [""] * (10 - len(rec["ours_top10"]))
    theirs = rec["pfr_top10"] + [""] * (10 - len(rec["pfr_top10"]))
    for i in range(10):
        body += f"| {i+1} | {ours[i]} | {theirs[i]} |\n"
    if m["pfr_misses"]:
        body += "\n**We missed (in PFR not us):** "
        body += ", ".join(
            f"{n} _({m['miss_buckets'].get(n, '?')})_"
            for n in m["pfr_misses"]
        ) + "\n"
    if m["ours_extras"]:
        body += "\n**Our extras (in us not PFR top-20):** "
        body += ", ".join(
            f"{n} _({m['extra_buckets'].get(n, '?')})_"
            for n in m["ours_extras"]
        ) + "\n"
    return head + body + "\n"


def _summary_themes(results: List[Dict]) -> str:
    """Aggregate miss + extra buckets across the corpus."""
    miss_counts: Dict[str, int] = {}
    extra_counts: Dict[str, int] = {}
    overlaps_10 = []
    overlaps_20 = []
    jaccards = []
    skipped = 0
    for r in results:
        if r.get("skip_reason") or not r.get("metrics"):
            skipped += 1
            continue
        m = r["metrics"]
        overlaps_10.append(m["overlap_at_10"])
        overlaps_20.append(m["overlap_at_20"])
        jaccards.append(m["jaccard"])
        for bucket in m["miss_buckets"].values():
            miss_counts[bucket] = miss_counts.get(bucket, 0) + 1
        for bucket in m["extra_buckets"].values():
            extra_counts[bucket] = extra_counts.get(bucket, 0) + 1

    n_audited = len(overlaps_10)
    if n_audited == 0:
        return "_Audit produced no usable rows._\n"
    avg_10 = sum(overlaps_10) / n_audited
    avg_20 = sum(overlaps_20) / n_audited
    avg_jac = sum(jaccards) / n_audited

    out = "## Themes / Model Holes\n\n"
    out += (
        f"Audited **{n_audited}** of {len(results)} ranked players "
        f"({skipped} skipped — see per-player blocks for reasons).\n\n"
        f"- Average overlap@10 with PFR: **{avg_10:.2f} / 10**\n"
        f"- Average overlap@20 with PFR: {avg_20:.2f} / 20\n"
        f"- Average Jaccard (top-10 vs top-10): {avg_jac:.3f}\n\n"
    )

    # Miss buckets — what PFR has and we don't
    out += "### Where PFR comps live that ours don't\n\n"
    for bucket, n in sorted(miss_counts.items(), key=lambda x: -x[1]):
        out += f"- `{bucket}` — **{n}** misses\n"

    # Per-audited-player rate for threshold logic.
    def rate(label: str, counter: Dict[str, int]) -> float:
        return counter.get(label, 0) / n_audited if n_audited else 0.0

    out += "\n### Where our extras live that PFR rejects\n\n"
    for bucket, n in sorted(extra_counts.items(), key=lambda x: -x[1]):
        out += f"- `{bucket}` — **{n}** extras\n"

    # Actionable recommendations driven by miss + extra buckets. We
    # threshold by *rate per audited player* so the themes work
    # whether 7 or 70 players were auditable.
    out += "\n### Actionable model holes\n\n"
    actions: List[str] = []

    if rate("active-modern-PFR-prefers", miss_counts) >= 0.5:
        actions.append(
            "**Active-modern cohort under-represented.** PFR's Career "
            "row for veteran QBs surfaces *currently-playing peers* "
            "(Mahomes, Burrow, Herbert, Goff, Prescott) but our top-10 "
            "skews to retired all-time greats. The model's age-band "
            "tolerance + the era-pace-adjusted retired corpus are "
            "pulling us toward retirees. *Fix:* gate the comp pool so "
            "at least 3 of 10 comps are *active* same-position players "
            "within ±3 yrs of age; boost similarity weight on overlap "
            "with the queried player's NFL years."
        )
    if rate("mid-tier-veteran-PFR-likes", miss_counts) >= 0.4:
        actions.append(
            "**Mid-tier-veteran blind spot.** PFR repeatedly surfaces "
            "competent journeymen (Aaron Brooks, Trent Green, Danny "
            "White, Bert Jones, Matt Schaub) that don't appear in our "
            "top-10. We're filtering them out via wash-out-rate + "
            "long-career bias — which sounds like a feature but means "
            "a Sam Darnold or Jared Goff profile gets matched only to "
            "Brady / Brees / Manning, inflating the projection. *Fix:* "
            "add a `mid-tier-anchor` lane that requires the cohort to "
            "include 2–3 comps with career_total_fp within ±25% of "
            "the query player's current arc."
        )
    if rate("longevity-favoured-by-PFR", miss_counts) >= 0.3:
        actions.append(
            "**Durable-veteran longevity bias inverted.** When PFR *does* "
            "point at a long-career legend (Brady, Brees, Rivers, "
            "Roethlisberger), it's often somebody our model already "
            "surfaced — but the SET overlap is still low because we "
            "only get 1 of their 3-4 durable picks. *Fix:* sort the "
            "comp pool by `completed_seasons` quartile inside the "
            "already-similar cohort and pull 2 from the top quartile."
        )
    if rate("pre1999-corpus-comp", extra_counts) >= 0.5:
        actions.append(
            "**Pre-1999 corpus is pulling hard.** Era-pace-adjusted comps "
            "from the 1980-1998 corpus (Marino, Tarkenton, Unitas, "
            "Elway) show up in our top-10 for almost every veteran QB "
            "profile; PFR's Career row stays inside the post-1999 "
            "per-page sim scope and rarely cites them. This is a model "
            "choice (we expanded the corpus deliberately in v3.9) but "
            "it's currently *driving* the projection rather than "
            "sanity-checking it. *Fix:* hard-cap pre-1999 comps at 3 "
            "of 10 in the display table, and weight their similarity "
            "by the existing 0.9× confidence haircut so they don't "
            "out-rank a post-1999 comp at equal raw sim."
        )
    if rate("durable-legend-we-over-pull", extra_counts) >= 0.5:
        actions.append(
            "**Durable-legend over-pull on mid-tier query players.** When "
            "the query player is *not* an elite producer (Mayfield, "
            "Darnold, Goff), our top-10 still cites Brady / Brees / "
            "Manning / Favre because their post-age-N fp/g curve "
            "happens to align. PFR's similarity Bill James method "
            "weights career-shape-and-magnitude together, so it stays "
            "away. *Fix:* anchor the similarity vector to *cumulative* "
            "career-fp percentile alongside fp/g curve; players in "
            "different career-fp deciles should rarely match."
        )

    if not actions:
        out += (
            "_No bucket crossed the 0.3–0.5 per-player rate threshold; "
            "the patterns below are real but heterogeneous._\n"
        )
    else:
        for i, action in enumerate(actions, 1):
            out += f"{i}. {action}\n\n"

    return out


def write_markdown(results: List[Dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# PFR Similarity Sanity-Check Audit\n",
             "Compares our v3.10 Fantasy-Point Arc Comparables (top-10) "
             "to PFR's `#all_sim_scores` **Career** row for the top-50 "
             "ranked players.\n\n"]
    lines.append(_summary_themes(results))
    lines.append("\n---\n\n## Per-player detail\n\n")
    for r in results:
        lines.append(_md_player_block(r))
    path.write_text("".join(lines), encoding="utf-8")


def write_summary_csv(results: List[Dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([
            "rank", "name", "position", "player_id", "pfr_id",
            "overlap_at_10", "overlap_at_20", "jaccard",
            "n_misses", "n_extras", "skip_reason",
        ])
        for r in results:
            m = r.get("metrics") or {}
            w.writerow([
                r.get("rank"), r["name"], r.get("position"),
                r.get("player_id"), r.get("pfr_id") or "",
                m.get("overlap_at_10", ""), m.get("overlap_at_20", ""),
                f"{m['jaccard']:.4f}" if "jaccard" in m else "",
                len(m.get("pfr_misses", [])),
                len(m.get("ours_extras", [])),
                r.get("skip_reason", ""),
            ])


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--out-md", default=str(DOCS_DIR / "pfr_comparison_audit.md"))
    ap.add_argument("--out-csv", default=str(DOCS_DIR / "pfr_audit_summary.csv"))
    args = ap.parse_args(argv)

    results = run_audit(top_n=args.top)
    write_markdown(results, Path(args.out_md))
    write_summary_csv(results, Path(args.out_csv))
    log.info("wrote %s and %s", args.out_md, args.out_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
