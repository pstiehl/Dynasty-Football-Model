"""Build dynasty_site/highlights.json from curated YouTube channels.

Quota model — the whole reason this script is shaped the way it is
------------------------------------------------------------------
YouTube Data API v3 gives you 10,000 units/day by default.

    search.list        100 units   <- never used here
    playlistItems.list   1 unit    per page of up to 50 videos
    videos.list          1 unit    per page of up to 50 video ids
    channels.list        1 unit    per call (handle -> channel id)

Walking 12 channels x 200 recent uploads is 48 units of playlistItems plus
~48 units of videos.list. Call it ~100 units for a full refresh, against a
10,000 daily budget. Searching per player instead would cost 100 units *per
player*, which is why that approach is a dead end.

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
    DEFAULT_WINDOW_DAYS,
    PlayerRef,
    Video,
    build_index,
    load_players_from_db,
    parse_ts,
    resolve_game_window,
    window_containing,
)

API_BASE = "https://www.googleapis.com/youtube/v3"
CHANNELS_CONFIG = REPO_ROOT / "data" / "highlights" / "channels.json"
DEFAULT_OUTPUT = REPO_ROOT / "dynasty_site" / "highlights.json"

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


def enrich_videos(client, rows: List[dict], api_key: str, quota: Quota) -> List[Video]:
    """Add duration + embeddable in batches of 50 (1 unit per batch).

    ``status.embeddable`` matters more than it looks: a non-embeddable video
    throws error 150 in the IFrame player, and in a playlist that stalls the
    whole reel. Cheaper to drop them here than to handle it in the browser.
    """
    by_id = {r["video_id"]: r for r in rows}
    ids = list(by_id)
    out: List[Video] = []

    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        data = _get(
            client, "videos",
            {"part": "contentDetails,status", "id": ",".join(batch), "key": api_key},
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


def load_fixture(path: Path) -> List[Video]:
    rows = json.loads(Path(path).read_text())
    if isinstance(rows, dict):
        rows = rows.get("videos", [])
    return [Video(**r) for r in rows]


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

            print(f"\n[2/3] Enriching {len(rows)} videos (duration + embeddable)...")
            with httpx.Client() as client2:
                videos = enrich_videos(client2, rows, api_key, quota)

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
        )

    index = build(window)

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

    s = index["stats"]
    print(f"\n  videos seen ............ {s['videos_seen']:,}")
    print(f"  dropped (non-embeddable) {s['videos_unembeddable']:,}")
    print(f"  dropped (not a game) ... {s['videos_non_game']:,}")
    print(f"  no player matched ...... {s['videos_unmatched']:,}")
    print(f"  ambiguous name ......... {s['videos_ambiguous']:,}")
    print(f"  players with clips ..... {s['players_with_clips']:,}")
    print(f"  ... inside the window .. {s['players_with_window_clips']:,}")
    print(f"  total clips ............ {s['total_clips']:,}")
    print(f"  ... inside the window .. {s['clips_in_window']:,}")
    print(f"  quota spent ............ {quota.report()}")

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
