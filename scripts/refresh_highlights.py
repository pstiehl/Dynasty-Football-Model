"""Build dynasty_site/highlights.json from curated YouTube channels.

Quota model — the whole reason this script is shaped the way it is
------------------------------------------------------------------
Verified against Google's published cost table on 2026-09-21. Since the
2026-06-01 granular-quota change there are THREE separate buckets, not one:

    search.list          own bucket: 100 CALLS/day, 1 unit each
    videos.insert        own bucket: 100 calls/day  (not used here)
    everything else      shared pool: 10,000 units/day
        playlistItems.list   1 unit   per page of up to 50 videos
        videos.list          1 unit   per page of up to 50 video ids
        channels.list        1 unit   per call (handle -> channel id)

Base layer: walking 12 channels x 200 recent uploads is ~48 units of
playlistItems plus ~48 units of videos.list. Call it ~100 units against the
10,000-unit pool - about 1%. It does not touch the search bucket at all.

Gap-fill layer (``--gap-fill``): one search.list call per uncovered player,
hard-capped by ``--search-budget`` and permanently cached per (player,
season, week) so no pair is ever searched twice. Because search has its own
bucket, an exhausted search budget CANNOT blank the base layer - they no
longer compete. What it cannot do is borrow: being frugal on playlistItems
buys exactly zero extra searches.

The old "search.list costs 100 units" model, which this file used to
describe, is obsolete on billing mechanics. Its conclusion - never put
search.list on the per-request read path - is more true than ever, since the
whole project now gets 100 searches a day.

Every channel's uploads live in a playlist whose id is the channel id with
the "UC" prefix swapped for "UU", so we skip a channels.list call whenever
the config already carries a channel id.

Usage
-----
    # real run (needs YOUTUBE_API_KEY)
    python scripts/refresh_highlights.py

    # offline: build from a canned video dump, no key, no network
    python scripts/refresh_highlights.py --fixture tests/fixtures/videos_sample.json

    # see what would be matched without writing
    python scripts/refresh_highlights.py --fixture ... --dry-run

    # pretend it is Monday morning and print the window that resolves
    python scripts/refresh_highlights.py --fixture ... --as-of 2026-09-21T12:41:00Z --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dynasty.highlights import (  # noqa: E402
    DEFAULT_GRACE_HOURS,
    DEFAULT_SEARCH_BUDGET_CALLS,
    DEFAULT_WINDOW_DAYS,
    MIN_CLIP_SECONDS,
    SEARCH_LIST_UNIT_COST,
    PlayerRef,
    SearchBudget,
    SearchCache,
    Video,
    accept_search_video,
    build_index,
    build_name_index,
    gap_fill_targets,
    load_players_from_db,
    most_recent_complete_slate,
    parse_ts,
    rank_search_videos,
    resolve_game_window,
    search_query_for,
    window_containing,
)

API_BASE = "https://www.googleapis.com/youtube/v3"
CHANNELS_CONFIG = REPO_ROOT / "data" / "highlights" / "channels.json"
DEFAULT_OUTPUT = REPO_ROOT / "dynasty_site" / "highlights.json"

#: Permanent (player, season, week) -> search outcome record. Lives under
#: data/ rather than dynasty_site/ because it is an input to future runs,
#: not a published site artifact, and it is committed by the workflow so
#: it survives the ephemeral runner. See SearchCache for why permanence is
#: sound.
DEFAULT_SEARCH_CACHE = REPO_ROOT / "data" / "highlights" / "search_cache.json"

#: (month, day) that week 1 kicks off. The NFL opens on the Thursday after
#: Labor Day, so this moves every year -- 2026 week 1 is Thursday Sep 10.
#:
#: This no longer drives clip selection. Since the rolling game window took
#: over (see ``dynasty.highlights.resolve_game_window``), the season start
#: only converts the resolved window into an advisory week *label*; being
#: wrong about it mislabels a reel and nothing else. Overridable per run
#: with ``--season-start YYYY-MM-DD``.
SEASON_START_MONTH_DAY = (9, 10)

# ISO-8601 duration -> seconds ("PT4M13S" -> 253)
_DUR_RE = re.compile(
    r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", re.IGNORECASE
)


class Quota:
    """Running tally so the build log shows exactly what each run cost."""

    def __init__(self) -> None:
        self.units = 0
        self.calls: Dict[str, int] = {}

    def charge(self, endpoint: str, units: int) -> None:
        self.units += units
        self.calls[endpoint] = self.calls.get(endpoint, 0) + 1

    def report(self) -> str:
        detail = ", ".join(f"{k}x{v}" for k, v in sorted(self.calls.items()))
        return f"{self.units} units ({detail or 'no calls'})"


def parse_duration(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    m = _DUR_RE.fullmatch(iso.strip())
    if not m:
        return None
    days, hours, mins, secs = (int(g) if g else 0 for g in m.groups())
    return days * 86400 + hours * 3600 + mins * 60 + secs


def uploads_playlist_id(channel_id: str) -> str:
    """UCxxxx -> UUxxxx. Saves a channels.list call per channel."""
    if channel_id.startswith("UC"):
        return "UU" + channel_id[2:]
    return channel_id


# --------------------------------------------------------------------------
# YouTube fetching
# --------------------------------------------------------------------------

def _get(client, path: str, params: dict, quota: Quota, cost: int) -> dict:
    resp = client.get(f"{API_BASE}/{path}", params=params, timeout=30)
    quota.charge(path, cost)
    if resp.status_code == 403:
        # Quota exhaustion and a bad key both land here; the body says which.
        raise RuntimeError(f"YouTube API 403 on {path}: {resp.text[:300]}")
    resp.raise_for_status()
    return resp.json()


def resolve_channel_id(client, handle: str, api_key: str, quota: Quota) -> Optional[str]:
    """@handle -> UC... channel id."""
    data = _get(
        client, "channels",
        {"part": "id", "forHandle": handle.lstrip("@"), "key": api_key},
        quota, 1,
    )
    items = data.get("items") or []
    return items[0]["id"] if items else None


def fetch_channel_uploads(
    client, playlist_id: str, api_key: str, quota: Quota,
    max_videos: int, published_after: datetime,
) -> List[dict]:
    """Walk an uploads playlist newest-first, stopping at the date cutoff.

    playlistItems returns uploads in reverse-chronological order, so we can
    bail as soon as we cross ``published_after`` instead of paging the whole
    channel history.
    """
    out: List[dict] = []
    page_token = None
    while len(out) < max_videos:
        params = {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": 50,
            "key": api_key,
        }
        if page_token:
            params["pageToken"] = page_token
        data = _get(client, "playlistItems", params, quota, 1)

        stop = False
        for item in data.get("items", []):
            cd = item.get("contentDetails", {})
            sn = item.get("snippet", {})
            published = cd.get("videoPublishedAt") or sn.get("publishedAt")
            if published:
                try:
                    ts = datetime.fromisoformat(published.replace("Z", "+00:00"))
                    if ts < published_after:
                        stop = True
                        break
                except ValueError:
                    pass
            out.append({
                "video_id": cd.get("videoId") or sn.get("resourceId", {}).get("videoId"),
                "title": sn.get("title", ""),
                "channel_id": sn.get("channelId", ""),
                "channel_title": sn.get("channelTitle", ""),
                "published_at": published or "",
            })
        if stop:
            break
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return [v for v in out if v.get("video_id")][:max_videos]


# --------------------------------------------------------------------------
# Gap-fill: targeted search.list for players the channels missed
# --------------------------------------------------------------------------

def search_videos(
    client, query: str, api_key: str, quota: Quota,
    *, max_results: int = 5,
    published_after: Optional[datetime] = None,
    published_before: Optional[datetime] = None,
) -> List[str]:
    """One ``search.list`` call -> video ids. **100 quota units.**

    The caller must already hold a :meth:`SearchBudget.take` authorisation
    before calling this. Nothing in here checks the budget, deliberately:
    one gate, in one place, is the only way that invariant stays true.

    Only ids are returned. ``search.list`` snippets carry a title and
    channel, but not duration or view count, and we need both to judge a
    clip - so every id goes through ``videos.list`` afterwards at 1 unit
    per 50, which also gives us a consistent shape with the base layer.

    ``videoDuration=medium`` is deliberately NOT set: it would exclude
    clips under 4 minutes, which is most single-player cut-ups.
    """
    params = {
        "part": "id",
        "q": query,
        "type": "video",
        "maxResults": max(1, min(50, int(max_results))),
        "order": "relevance",
        "key": api_key,
    }
    if published_after:
        params["publishedAfter"] = published_after.strftime("%Y-%m-%dT%H:%M:%SZ")
    if published_before:
        params["publishedBefore"] = published_before.strftime("%Y-%m-%dT%H:%M:%SZ")

    data = _get(client, "search", params, quota, SEARCH_LIST_UNIT_COST)
    out = []
    for item in data.get("items", []):
        vid = (item.get("id") or {}).get("videoId")
        if vid:
            out.append(vid)
    return out


def fetch_videos_by_id(
    client, video_ids: List[str], api_key: str, quota: Quota,
    trusted_ids: Optional[set] = None,
) -> List[Video]:
    """Full ``Video`` objects straight from ``videos.list``. 1 unit per 50.

    Unlike :func:`enrich_videos`, this does not need a pre-existing row
    from the channel walk - it pulls ``snippet`` too, so a bare video id
    (from a search, or from the permanent cache) becomes a complete Video.
    Adding ``snippet`` to the part list costs nothing: videos.list is
    charged per call, not per part.
    """
    trusted_ids = trusted_ids or set()
    out: List[Video] = []
    ids = [v for v in dict.fromkeys(video_ids) if v]

    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        try:
            data = _get(
                client, "videos",
                {"part": "snippet,contentDetails,status,statistics",
                 "id": ",".join(batch), "key": api_key},
                quota, 1,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  WARN videos.list batch failed: {exc}")
            continue
        for item in data.get("items", []):
            sn = item.get("snippet", {}) or {}
            out.append(Video(
                video_id=item["id"],
                title=sn.get("title", ""),
                channel_id=sn.get("channelId", ""),
                channel_title=sn.get("channelTitle", ""),
                published_at=sn.get("publishedAt", ""),
                duration_seconds=parse_duration(
                    item.get("contentDetails", {}).get("duration")
                ),
                embeddable=bool(item.get("status", {}).get("embeddable", True)),
                view_count=_as_int(item.get("statistics", {}).get("viewCount")),
                trusted_channel=sn.get("channelId", "") in trusted_ids,
            ))
    return out


def run_gap_fill(
    targets: List[PlayerRef],
    all_players: List[PlayerRef],
    *,
    cache: SearchCache,
    budget: SearchBudget,
    season: Optional[int],
    week: Optional[int],
    week_complete: bool,
    search_fn,
    fetch_fn,
    min_clip_seconds: int = MIN_CLIP_SECONDS,
    max_results: int = 5,
    max_keep_per_player: int = 3,
    as_of: Optional[datetime] = None,
) -> "tuple[List[Video], dict]":
    """The expensive half of the hybrid. Returns ``(videos, stats)``.

    ``search_fn`` and ``fetch_fn`` are injected rather than called
    directly so the whole orchestration - budget enforcement, cache
    dedupe, miss recording - is testable with stdlib fixtures and no API
    key. That is not decoration: the budget is the part of this feature
    that must provably never overrun, and a design that can only be
    exercised against the live API cannot prove it.

    Three phases, in this order for a reason:

    1. **Replay the cache.** Every previously-found video id for this
       (season, week) is collected for re-fetch. Free in search terms.
       Without this the index would lose every gap-fill result the moment
       it was rebuilt, and we would re-buy them.
    2. **Spend the budget.** One search per target, strictly gated by
       :meth:`SearchBudget.take`, stopping the moment it refuses.
    3. **Judge, then record.** Candidates are fetched, filtered by
       :func:`accept_search_video`, and only then written to the cache -
       including the empty result, which is what stops us re-asking.
    """
    stats = {
        "targets": len(targets),
        "searched": 0,
        "cache_replayed_players": 0,
        "cache_replayed_ids": 0,
        "candidates": 0,
        "accepted": 0,
        "rejected": 0,
        "new_hits": 0,
        "new_misses": 0,
        "budget_denied": 0,
        "skipped_incomplete_week": False,
    }

    if week is None or not week_complete:
        # Not an error. Before a week finishes, its film is still being
        # uploaded, so anything we cached would be a premature freeze.
        stats["skipped_incomplete_week"] = True
        return [], stats

    name_index = build_name_index(all_players)

    # -- phase 1: replay everything we already bought -----------------
    replay_ids: List[str] = []
    for p in all_players:
        ids = cache.video_ids(p.sleeper_id, season, week)
        if ids:
            stats["cache_replayed_players"] += 1
            replay_ids.extend(ids)
    stats["cache_replayed_ids"] = len(set(replay_ids))

    # -- phase 2: spend the budget ------------------------------------
    pending: Dict[str, List[PlayerRef]] = {}
    searched: List[tuple] = []
    for player in targets:
        if not budget.take():
            # Out of budget. Everything still in ``targets`` keeps its
            # place in the queue for the next run, because nothing was
            # written to the cache for them.
            break
        query = search_query_for(player, week, season)
        stats["searched"] += 1
        try:
            ids = search_fn(query, max_results=max_results)
        except Exception as exc:  # noqa: BLE001
            # Fail soft, and do NOT cache: a transport failure is not a
            # statement about whether film exists. The quota unit is gone
            # either way, which is why budget.take() is not refunded.
            print(f"  WARN search failed for {player.name}: {exc}")
            continue
        searched.append((player, query, ids))
        for vid in ids:
            pending.setdefault(vid, []).append(player)

    stats["budget_denied"] = budget.denied
    stats["candidates"] = len(pending)

    # -- phase 3: fetch, judge, record --------------------------------
    to_fetch = list(dict.fromkeys(list(pending) + replay_ids))
    fetched: List[Video] = fetch_fn(to_fetch) if to_fetch else []
    by_id = {v.video_id: v for v in fetched}

    accepted_by_player: Dict[str, List[Video]] = {}
    for player, query, ids in searched:
        keep: List[Video] = []
        for vid in ids:
            video = by_id.get(vid)
            if video is None:
                continue
            if accept_search_video(
                video, player, name_index,
                week=week, min_clip_seconds=min_clip_seconds,
            ):
                keep.append(video)
            else:
                stats["rejected"] += 1
        keep = rank_search_videos(keep)[:max_keep_per_player]
        accepted_by_player[player.sleeper_id] = keep
        stats["accepted"] += len(keep)
        # Record the outcome - an empty list included. This is the line
        # that makes the feature affordable.
        cache.record(
            player.sleeper_id, season, week,
            [v.video_id for v in keep],
            query=query, week_complete=week_complete, as_of=as_of,
        )
        if keep:
            stats["new_hits"] += 1
        else:
            stats["new_misses"] += 1

    # Replayed cache hits are already-judged film; they go straight back
    # in. Pass 2 of build_index re-applies the full matcher to them
    # anyway, so a video that has since become wrong still gets caught.
    out: Dict[str, Video] = {}
    for vids in accepted_by_player.values():
        for v in vids:
            out[v.video_id] = v
    for vid in replay_ids:
        v = by_id.get(vid)
        if v is not None:
            out.setdefault(vid, v)

    return list(out.values()), stats


def _as_int(value) -> Optional[int]:
    """YouTube returns counts as strings, and omits them when hidden."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def enrich_videos(
    client,
    rows: List[dict],
    api_key: str,
    quota: Quota,
    trusted_ids: Optional[set] = None,
) -> List[Video]:
    """Add duration, embeddability and view count in batches of 50.

    **Still 1 quota unit per batch.** ``videos.list`` is charged per call,
    not per ``part``, so adding ``statistics`` alongside ``contentDetails``
    and ``status`` costs nothing extra -- the alternative, a second pass
    for view counts, would have doubled this stage's quota.

    ``status.embeddable`` is now recorded rather than acted on. The pages
    link out to YouTube, so a video the uploader blocked from embedding is
    perfectly watchable and there is no longer a reason to drop it.
    """
    trusted_ids = trusted_ids or set()
    by_id = {r["video_id"]: r for r in rows}
    ids = list(by_id)
    out: List[Video] = []

    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        data = _get(
            client, "videos",
            {"part": "contentDetails,status,statistics",
             "id": ",".join(batch), "key": api_key},
            quota, 1,
        )
        for item in data.get("items", []):
            row = by_id.get(item["id"])
            if not row:
                continue
            out.append(Video(
                video_id=item["id"],
                title=row["title"],
                channel_id=row["channel_id"],
                channel_title=row["channel_title"],
                published_at=row["published_at"],
                duration_seconds=parse_duration(
                    item.get("contentDetails", {}).get("duration")
                ),
                embeddable=bool(item.get("status", {}).get("embeddable", True)),
                view_count=_as_int(
                    item.get("statistics", {}).get("viewCount")
                ),
                trusted_channel=row["channel_id"] in trusted_ids,
            ))
    return out


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

def load_channels() -> List[dict]:
    if not CHANNELS_CONFIG.exists():
        print(f"  WARN: no channel config at {CHANNELS_CONFIG}")
        return []
    data = json.loads(CHANNELS_CONFIG.read_text())
    return [c for c in data.get("channels", []) if c.get("enabled", True)]


def trusted_channel_ids() -> set:
    """Channel ids treated as curated provenance.

    Every entry in ``channels.json`` was resolved and had its recent
    uploads read before being enabled, so membership of that file *is* the
    trust signal -- there is no separate allow-list to keep in sync. A
    channel can opt out with ``"trusted": false`` if it is ever enabled
    for coverage without being vouched for.
    """
    return {
        c["channel_id"] for c in load_channels()
        if c.get("channel_id") and c.get("trusted", True)
    }


def load_fixture(path: Path) -> List[Video]:
    rows = json.loads(Path(path).read_text())
    if isinstance(rows, dict):
        rows = rows.get("videos", [])
    return [Video(**r) for r in rows]


def load_rostered_ids(path: Optional[Path]) -> set:
    """Sleeper ids that appear on some indexed roster.

    Accepts either a flat list of ids or a mapping whose values are id
    lists (which is the shape the league prefetch already writes), so the
    flag works against the artifacts that exist rather than requiring a
    new one.

    Failure is soft and silent-ish: rostered-ness is a *priority* signal,
    not a correctness one. Without it the gap-fill still runs, ordered by
    model rank alone.
    """
    if not path:
        return set()
    try:
        data = json.loads(Path(path).read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"  WARN couldn't read rosters from {path}: {exc}")
        return set()

    out: set = set()
    if isinstance(data, list):
        out = {str(v) for v in data if v}
    elif isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                out |= {str(v) for v in value if v}
    return out


def load_players() -> List[PlayerRef]:
    """Canonical players, annotated with model rank where the engine has one."""
    rank_by_gsis: Dict[str, int] = {}
    rankings_path = REPO_ROOT / "dynasty_site" / "engine_rankings.json"
    if rankings_path.exists():
        try:
            for row in json.loads(rankings_path.read_text()):
                pid, rank = row.get("player_id"), row.get("overall_rank")
                if pid and rank:
                    rank_by_gsis[pid] = int(rank)
        except Exception as exc:  # noqa: BLE001
            print(f"  WARN: couldn't read engine_rankings.json ({exc})")

    try:
        players = load_players_from_db(rank_by_gsis=rank_by_gsis)
        print(f"  Loaded {len(players):,} canonical players "
              f"({len(rank_by_gsis):,} carry a model rank)")
        return players
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL: couldn't load players from DB: {exc}")
        print("  Run `python -m dynasty.cli sync-players` first.")
        return []


def default_season_start(as_of: datetime) -> datetime:
    """Week-1 kickoff Thursday for the season ``as_of`` falls in.

    January and February belong to the *previous* calendar year's season,
    so a February run labels its windows against last autumn's kickoff
    rather than one seven months in the future.
    """
    month, day = SEASON_START_MONTH_DAY
    year = as_of.year - 1 if as_of.month < 3 else as_of.year
    return datetime(year, month, day, tzinfo=timezone.utc)


def parse_season_start(value: str) -> datetime:
    """``YYYY-MM-DD`` -> UTC midnight, for ``--season-start``."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:  # noqa: BLE001
        raise argparse.ArgumentTypeError(
            f"expected YYYY-MM-DD, got {value!r}"
        ) from exc


def parse_as_of(value: str) -> datetime:
    """ISO-8601 instant or ``YYYY-MM-DD``, for ``--as-of``.

    A bare date means 00:00 UTC, which is the useful default for "pretend
    the build ran on this day".
    """
    ts = parse_ts(value)
    if ts is None:
        raise argparse.ArgumentTypeError(
            f"expected an ISO-8601 date or datetime, got {value!r}"
        )
    return ts


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixture", type=Path,
                    help="Build from a local JSON video dump (no network/key).")
    ap.add_argument("--players", type=Path,
                    help="Build against a JSON player list instead of the DB "
                         "(offline testing; same fields as PlayerRef).")
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument("--days", type=int, default=21,
                    help="How far back to walk each channel (default 21).")
    ap.add_argument("--max-per-channel", type=int, default=200)
    ap.add_argument("--min-confidence", type=float, default=0.5)
    ap.add_argument("--max-clips", type=int, default=5)
    ap.add_argument("--min-clip-seconds", type=int, default=MIN_CLIP_SECONDS,
                    help="Hard floor on clip duration (default "
                         f"{MIN_CLIP_SECONDS}). This is a sanity bound, not "
                         "a Shorts filter -- the pages link out to YouTube, "
                         "where Shorts play fine, and short single-player "
                         "cut-ups are wanted. Duration above the floor is a "
                         "classification input, not a gate.")
    ap.add_argument("--season-start", type=parse_season_start, default=None,
                    metavar="YYYY-MM-DD",
                    help="Override week-1 kickoff (default: "
                         f"{SEASON_START_MONTH_DAY[0]:02d}-"
                         f"{SEASON_START_MONTH_DAY[1]:02d} of the current "
                         "season). Labels the window; does not select clips.")
    ap.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS,
                    help="Length of the game window in days, counted from "
                         f"its Thursday (default {DEFAULT_WINDOW_DAYS}, i.e. "
                         "Thursday through Monday).")
    ap.add_argument("--grace-hours", type=float, default=DEFAULT_GRACE_HOURS,
                    help="How long after a slate's final day begins before "
                         "it counts as the current window (default "
                         f"{DEFAULT_GRACE_HOURS}). This is what keeps "
                         "Monday-morning viewers on last week's finished film.")
    ap.add_argument("--as-of", type=parse_as_of, default=None,
                    metavar="ISO8601",
                    help="Resolve the window as if it were this instant "
                         "(e.g. 2026-09-21T12:41:00Z). Testing only.")
    ap.add_argument("--gap-fill", action="store_true",
                    help="Enable the targeted search.list layer for players "
                         "with no cut-up in the lead week. Costs 100 units "
                         "per player searched, capped by --search-budget, "
                         "and every result is cached permanently so no "
                         "(player, week) is ever searched twice.")
    ap.add_argument("--search-budget", type=int,
                    default=DEFAULT_SEARCH_BUDGET_CALLS,
                    help="Hard cap on search.list calls per run (default "
                         f"{DEFAULT_SEARCH_BUDGET_CALLS} = "
                         f"{DEFAULT_SEARCH_BUDGET_CALLS * SEARCH_LIST_UNIT_COST}"
                         " units). Never exceeded; the run stops searching "
                         "and says so.")
    ap.add_argument("--search-cache", type=Path, default=DEFAULT_SEARCH_CACHE,
                    help="Permanent (player, season, week) search record.")
    ap.add_argument("--search-max-results", type=int, default=5,
                    help="Results requested per search (does not affect "
                         "quota: search.list is 100 units regardless).")
    ap.add_argument("--rosters", type=Path, default=None,
                    help="JSON list of rostered sleeper_ids, or a dict whose "
                         "values are id lists. Rostered players are searched "
                         "before everyone else.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    print("=" * 60)
    print("Highlights refresh")
    print("=" * 60)

    as_of = args.as_of or datetime.now(timezone.utc)
    season_start = args.season_start or default_season_start(as_of)
    window = resolve_game_window(
        as_of,
        window_days=args.window_days,
        grace_hours=args.grace_hours,
        season_start=season_start,
    )
    print(f"\n  game window ............ {window.label} "
          f"({window.start.date()} -> {window.end.date()}"
          f"{f', week {window.week}' if window.week else ''})")
    print(f"  publish cutoff ......... {window.publish_cutoff.isoformat()} "
          f"(+{window.grace_hours:g}h grace)")
    print(f"  resolved as of ......... {as_of.isoformat()}")

    # Separate question from the window above: the window is about where
    # the film is, this is about which week has actually finished. They
    # disagree on a Monday, and the buckets follow this one.
    lead = most_recent_complete_slate(as_of, season_start=season_start)
    print(f"  lead (complete) week ... {lead.label}"
          f"{f' (week {lead.week})' if lead.week else ''} "
          f"— completed {lead.complete_at.isoformat()}")

    quota = Quota()
    videos: List[Video] = []

    if args.fixture:
        videos = load_fixture(args.fixture)
        print(f"\n[1/3] Fixture mode — {len(videos)} videos from {args.fixture}")
    else:
        api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
        if not api_key:
            print("\nFAIL: YOUTUBE_API_KEY not set (and no --fixture given).")
            return 1
        import httpx

        channels = load_channels()
        print(f"\n[1/3] Fetching uploads from {len(channels)} channels...")
        # Never walk back less far than the window itself, or a long
        # ``--window-days`` would silently index a truncated slate.
        cutoff = min(
            datetime.now(timezone.utc) - timedelta(days=args.days),
            window.start,
        )
        rows: List[dict] = []

        with httpx.Client() as client:
            for ch in channels:
                label = ch.get("name") or ch.get("handle") or ch.get("channel_id")
                channel_id = ch.get("channel_id")
                try:
                    if not channel_id and ch.get("handle"):
                        channel_id = resolve_channel_id(
                            client, ch["handle"], api_key, quota
                        )
                        if not channel_id:
                            print(f"  SKIP {label}: handle didn't resolve")
                            continue
                        print(f"  {label}: resolved to {channel_id} "
                              f"(add this to channels.json to save a call)")
                    got = fetch_channel_uploads(
                        client, uploads_playlist_id(channel_id), api_key, quota,
                        args.max_per_channel, cutoff,
                    )
                    rows.extend(got)
                    print(f"  {label}: {len(got)} recent uploads")
                except Exception as exc:  # noqa: BLE001
                    # One dead channel must never fail the whole refresh.
                    print(f"  WARN {label}: {exc}")

            print(f"\n[2/3] Enriching {len(rows)} videos "
                  f"(duration + embeddable + views)...")
            with httpx.Client() as client2:
                videos = enrich_videos(
                    client2, rows, api_key, quota, trusted_channel_ids()
                )

    print(f"\n[{'2' if args.fixture else '3'}/3] Matching to canonical players...")
    if args.players:
        players = [PlayerRef(**r) for r in json.loads(args.players.read_text())]
        print(f"  Loaded {len(players):,} players from {args.players}")
    else:
        players = load_players()
    if not players:
        return 1

    def build(win):
        return build_index(
            players, videos,
            min_confidence=args.min_confidence,
            max_clips_per_player=args.max_clips,
            expected_week=win.week,
            window=win,
            min_clip_seconds=args.min_clip_seconds,
            trusted_channel_ids=trusted_channel_ids(),
            as_of=as_of,
            season_start=season_start,
        )

    index = build(window)

    # ------------------------------------------------------------------
    # Gap-fill layer.
    #
    # Runs against the BASE INDEX, not the raw video list, because the
    # question it asks is "who did the cheap layer fail to cover" - and
    # that is only answerable after matching and classification have run.
    # Its output is merged back and the index is rebuilt, so gap-filled
    # clips go through exactly the same matcher, classifier, confidence
    # score and sort as everything else. No second code path, and no
    # lower bar for a clip that happened to arrive via search.
    # ------------------------------------------------------------------
    budget = SearchBudget(args.search_budget)
    cache = SearchCache.load(args.search_cache)
    gap_stats = None

    if args.gap_fill:
        if args.fixture:
            print("\n  gap-fill: skipped (fixture mode has no API key).")
        elif not lead.complete or lead.week is None:
            print(f"\n  gap-fill: skipped - {lead.label} is not a complete "
                  "week, so any cached result would freeze prematurely.")
        else:
            season = season_start.year
            rostered = load_rostered_ids(args.rosters)
            targets = gap_fill_targets(
                index, players,
                week=lead.week, week_complete=lead.complete, season=season,
                cache=cache, rostered_ids=rostered,
            )
            print(f"\n  gap-fill ............... week {lead.week} "
                  f"({lead.label})")
            print(f"  candidates ............. {len(targets):,} players with "
                  "no cut-up, never searched")
            print(f"  budget ................. {budget.max_calls} searches = "
                  f"{budget.units_budgeted:,} units")
            if rostered:
                print(f"  rostered prioritised ... {len(rostered):,} ids")

            api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
            import httpx

            with httpx.Client() as sclient:
                def _search(query, max_results=5):
                    return search_videos(
                        sclient, query, api_key, quota,
                        max_results=max_results,
                        published_after=lead.start - timedelta(days=1),
                    )

                def _fetch(ids):
                    return fetch_videos_by_id(
                        sclient, ids, api_key, quota, trusted_channel_ids()
                    )

                gap_videos, gap_stats = run_gap_fill(
                    targets, players,
                    cache=cache, budget=budget,
                    season=season, week=lead.week,
                    week_complete=lead.complete,
                    search_fn=_search, fetch_fn=_fetch,
                    min_clip_seconds=args.min_clip_seconds,
                    max_results=args.search_max_results,
                    as_of=as_of,
                )

            print(f"  searched ............... {gap_stats['searched']:,} "
                  f"({budget.units_spent:,} units)")
            print(f"  replayed from cache .... "
                  f"{gap_stats['cache_replayed_ids']:,} video ids for "
                  f"{gap_stats['cache_replayed_players']:,} players "
                  "(0 units of search)")
            print(f"  accepted / rejected .... {gap_stats['accepted']:,} / "
                  f"{gap_stats['rejected']:,}")
            print(f"  new hits / misses ...... {gap_stats['new_hits']:,} / "
                  f"{gap_stats['new_misses']:,} "
                  "(misses cached too, never re-searched)")
            if budget.denied:
                print(f"  BUDGET EXHAUSTED ....... {budget.denied:,} players "
                      "not searched; they stay at the front of the queue "
                      "for the next run.")

            if gap_videos:
                videos = list(videos) + gap_videos
                index = build(window)
                print(f"  merged ................. {len(gap_videos):,} "
                      "gap-fill videos, index rebuilt")

            if not args.dry_run:
                cache.save(args.search_cache, as_of=as_of)
                print(f"  cache .................. {len(cache.entries):,} "
                      f"permanent entries -> {args.search_cache}")

    # Safety net. The ingest is capped at ``--max-per-channel`` uploads,
    # not purely by date, and the configured channel posts mostly Shorts,
    # so a correctly-resolved completed slate can hold no film whatsoever.
    # That happened for real: every one of the 28 surviving videos in the
    # 2026-09-21 index was published Sep 20-21, so a Sep 10-14 window
    # matched 0 of 737 clip rows. Shipping that would blank the reel.
    #
    # Rather than widen the window and quietly blend slates, fall back to
    # the slate the newest film actually belongs to, and record that it
    # happened so the pages can say "nothing for X, showing Y".
    if index["stats"]["total_clips"] and not index["stats"]["clips_in_window"]:
        stamps = [
            ts for ts in (
                parse_ts(c.get("published_at"))
                for clips in index["clips"].values() for c in clips
            ) if ts is not None
        ]
        if stamps:
            newest = max(stamps)
            fallback = window_containing(
                newest,
                window_days=args.window_days,
                grace_hours=args.grace_hours,
                season_start=season_start,
            )
            if fallback.start != window.start:
                print(f"\n  WARN no film indexed for {window.label}; "
                      f"falling back to {fallback.label} "
                      f"(newest upload {newest.isoformat()})")
                fallback.adjusted_from = window.label
                window = fallback
                index = build(window)

    index["generated_at"] = datetime.now(timezone.utc).isoformat()
    index["resolved_as_of"] = as_of.isoformat()
    # Retained for readers that still key off a week number. It is the
    # window's label now, not an independently computed "today" week, so it
    # can no longer point at an in-progress slate.
    index["expected_week"] = window.week
    index["quota_units"] = quota.units
    # Quota telemetry, published so a reader can audit what a run cost
    # without digging through Actions logs. ``search_budget`` is the
    # enforcement record: calls_used can never exceed max_calls.
    index["quota"] = {
        "units_spent": quota.units,
        "daily_quota": budget.daily_quota,
        "units_remaining_of_daily": max(0, budget.daily_quota - quota.units),
        "calls": dict(sorted(quota.calls.items())),
        "search_budget": budget.to_json(),
    }
    index["search_cache"] = cache.to_json()
    if gap_stats is not None:
        index["gap_fill"] = gap_stats

    s = index["stats"]
    print(f"\n  videos seen ............ {s['videos_seen']:,}")
    print(f"  embed-blocked (KEPT) ... {s['videos_embed_blocked']:,}")
    print(f"  dropped (under {s['min_clip_seconds']}s) ... "
          f"{s['videos_too_short']:,}")
    print(f"  dropped (not a game) ... {s['videos_non_game']:,}")
    print(f"  no player matched ...... {s['videos_unmatched']:,}")
    print(f"  ambiguous name ......... {s['videos_ambiguous']:,}")
    print(f"  players with clips ..... {s['players_with_clips']:,}")
    print(f"  ... inside the window .. {s['players_with_window_clips']:,}")
    print(f"  total clips ............ {s['total_clips']:,}")
    print(f"  ... inside the window .. {s['clips_in_window']:,}")
    print(f"  ... player cut-ups ..... {s['clips_player_cutup']:,}")
    print(f"  ... game recaps ........ {s['clips_game_recap']:,}")
    print(f"  ... trusted channel .... {s['clips_trusted_channel']:,}")
    print("\n  week buckets (newest first):")
    for w in index["weeks"]:
        flag = "LEAD" if w["lead"] else ("" if w["complete"] else "in progress")
        print(f"    {w['label']:<14} "
              f"{('week ' + str(w['week'])) if w['week'] else 'week ?':<8} "
              f"{w['clip_count']:>6,} clips  {flag}")
    print(f"\n  quota spent ............ {quota.report()}")

    if args.dry_run:
        print("\n  --dry-run: nothing written.")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")
    kb = args.output.stat().st_size / 1024
    print(f"\n  Wrote {args.output} ({kb:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
