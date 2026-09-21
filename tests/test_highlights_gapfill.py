"""The gap-fill search layer: budget, cache, dedupe and relevance.

What this file is actually defending
------------------------------------
Gap-fill is the only part of the highlights pipeline that can exhaust a
scarce, hard-capped resource. A project gets **100 ``search.list`` calls
per day** (own bucket, 1 unit each, since the 2026-06-01 granular-quota
change), and that ceiling cannot be bought around by being frugal
elsewhere.

So these tests are not about output quality. They pin safety properties
that must hold regardless of how the caller behaves:

1. **The budget cannot be exceeded.** Not by a caller that loops too many
   times, not when every search raises, not when a bad flag asks for more
   than the API allows. Asserted at both levels: the counter itself, and
   the full ``run_gap_fill`` orchestration driven by a fake search
   function that counts how many times it was *really* invoked.
2. **A (player, week) is never searched twice** -- and a *miss* counts as
   searched. Re-asking questions that returned nothing is the failure
   mode that would quietly consume the whole allowance forever, because
   misses leave no clips behind to notice.
3. **Cache permanence is only claimed for a completed week.** While a
   week is in progress its film is still being uploaded, so freezing a
   miss then would silence that player for that week permanently.

Everything runs on stdlib fixtures. There is no API key here and there
must never need to be one: a budget that can only be verified against the
live API is a budget nobody will verify.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from dynasty.highlights import (  # noqa: E402
    DEFAULT_DAILY_QUOTA_UNITS,
    DEFAULT_SEARCH_BUDGET_CALLS,
    KIND_PLAYER,
    SEARCH_LIST_DAILY_CALL_LIMIT,
    SEARCH_LIST_UNIT_COST,
    PlayerRef,
    SearchBudget,
    SearchCache,
    Video,
    accept_search_video,
    build_index,
    build_name_index,
    classify,
    gap_fill_targets,
    match_players,
    rank_search_videos,
    search_query_for,
)

AS_OF = datetime(2026, 9, 21, 17, 0, tzinfo=timezone.utc)
SEASON = 2026


def player(sid, name, rank=None, team=None, pos="WR"):
    return PlayerRef(sleeper_id=sid, name=name, position=pos,
                     team=team, rank=rank)


def video(vid, title, *, seconds=240, views=1000,
          published="2026-09-14T12:00:00Z", channel="ch1",
          channel_title="Some Channel"):
    return Video(
        video_id=vid, title=title, channel_id=channel,
        channel_title=channel_title, published_at=published,
        duration_seconds=seconds, view_count=views,
    )


def _run_gap_fill(**kw):
    from refresh_highlights import run_gap_fill
    return run_gap_fill(**kw)


# ==========================================================================
# 1. The budget cannot be exceeded
# ==========================================================================

def test_budget_grants_exactly_max_calls_and_then_refuses_forever():
    budget = SearchBudget(5)
    grants = [budget.take() for _ in range(50)]

    assert grants.count(True) == 5, "granted more than the cap"
    assert grants[:5] == [True] * 5
    assert all(g is False for g in grants[5:]), "cap leaked after exhaustion"
    assert budget.used == 5
    assert budget.denied == 45
    assert budget.remaining == 0


def test_budget_is_clamped_to_the_api_daily_ceiling():
    """A flag asking for more than the API allows must not be honoured.

    500 searches against a 100/day bucket is 400 guaranteed failures.
    Clamping fails safe and is reported rather than silently obeyed.
    """
    budget = SearchBudget(500)

    assert budget.requested_calls == 500
    assert budget.max_calls == SEARCH_LIST_DAILY_CALL_LIMIT == 100
    assert budget.clamped is True
    assert sum(budget.take() for _ in range(500)) == 100


def test_default_budget_survives_two_runs_in_one_day():
    """The workflow runs twice on in-season Tuesdays.

    This is why the default is 40 and not 100: two runs must fit inside
    one day's allowance with headroom, or the second run starts failing
    partway through.
    """
    assert DEFAULT_SEARCH_BUDGET_CALLS == 40
    assert DEFAULT_SEARCH_BUDGET_CALLS * 2 <= SEARCH_LIST_DAILY_CALL_LIMIT
    assert SEARCH_LIST_DAILY_CALL_LIMIT - (DEFAULT_SEARCH_BUDGET_CALLS * 2) >= 20


def test_search_billing_constants_match_the_verified_quota_model():
    """Pins the post-2026-06-01 model so nobody 'corrects' it back.

    The widely-quoted "search.list costs 100 units" figure is obsolete:
    search has its own bucket at 1 unit per call. Restoring 100 here
    would make every budget calculation in the module wrong by 100x.
    """
    assert SEARCH_LIST_UNIT_COST == 1
    assert SEARCH_LIST_DAILY_CALL_LIMIT == 100
    assert DEFAULT_DAILY_QUOTA_UNITS == 10_000


def test_zero_budget_authorises_nothing():
    budget = SearchBudget(0)
    assert budget.take() is False
    assert budget.units_spent == 0


def test_negative_budget_is_clamped_not_inverted():
    """A misconfigured -5 must mean "no searches", never "unlimited"."""
    budget = SearchBudget(-5)
    assert budget.max_calls == 0
    assert budget.take() is False


# ==========================================================================
# 2. The budget holds through the real orchestration
# ==========================================================================

def test_run_gap_fill_never_searches_more_than_the_budget():
    """The load-bearing test.

    200 eligible targets, a budget of 7. The fake search function counts
    every invocation, so this measures calls actually issued rather than
    the tally the budget kept about itself.
    """
    targets = [player(f"p{i}", f"Player{i} Smith", rank=i) for i in range(200)]
    budget = SearchBudget(7)
    cache = SearchCache()
    calls = []

    def fake_search(query, max_results=5):
        calls.append(query)
        return [f"vid{len(calls)}"]

    def fake_fetch(ids):
        return [video(v, "Player0 Smith Week 1 Every Touch") for v in ids]

    _videos, stats = _run_gap_fill(
        targets=targets, all_players=targets,
        cache=cache, budget=budget, season=SEASON, week=1,
        week_complete=True, search_fn=fake_search, fetch_fn=fake_fetch,
        as_of=AS_OF,
    )

    assert len(calls) == 7, f"issued {len(calls)} searches against a budget of 7"
    assert stats["searched"] == 7
    assert budget.used == 7
    assert budget.used <= budget.max_calls


def test_run_gap_fill_cannot_exceed_the_daily_ceiling_even_if_asked():
    """Budget clamping holds end to end, not just in the constructor."""
    targets = [player(f"p{i}", f"Player{i} Smith", rank=i) for i in range(300)]
    budget = SearchBudget(250)
    calls = []

    _run_gap_fill(
        targets=targets, all_players=targets,
        cache=SearchCache(), budget=budget, season=SEASON, week=1,
        week_complete=True,
        search_fn=lambda q, max_results=5: calls.append(q) or [],
        fetch_fn=lambda ids: [], as_of=AS_OF,
    )

    assert len(calls) == SEARCH_LIST_DAILY_CALL_LIMIT == 100


def test_budget_is_not_refunded_when_every_search_raises():
    """A failing search still spent its call at YouTube's end.

    If exceptions refunded budget, a run against a broken key would loop
    through every target issuing real (charged) requests while believing
    it had spent nothing -- the exact shape of an unbounded overrun.
    """
    targets = [player(f"p{i}", f"Player{i} Jones", rank=i) for i in range(100)]
    budget = SearchBudget(4)
    cache = SearchCache()
    calls = []

    def exploding_search(query, max_results=5):
        calls.append(query)
        raise RuntimeError("403 quotaExceeded")

    _videos, stats = _run_gap_fill(
        targets=targets, all_players=targets,
        cache=cache, budget=budget, season=SEASON, week=1,
        week_complete=True, search_fn=exploding_search,
        fetch_fn=lambda ids: [], as_of=AS_OF,
    )

    assert len(calls) == 4, "a raising search must not refund its budget"
    assert budget.used == 4
    # Nothing cached: a transport failure says nothing about whether film
    # exists, so these players must be retried on a later run.
    assert len(cache.entries) == 0
    assert stats["searched"] == 4


def test_gap_fill_refuses_to_spend_on_an_incomplete_week():
    targets = [player("p1", "CeeDee Lamb", rank=3)]
    budget = SearchBudget(40)
    cache = SearchCache()
    calls = []

    _videos, stats = _run_gap_fill(
        targets=targets, all_players=targets,
        cache=cache, budget=budget, season=SEASON, week=2,
        week_complete=False,
        search_fn=lambda q, max_results=5: calls.append(q) or [],
        fetch_fn=lambda ids: [], as_of=AS_OF,
    )

    assert calls == []
    assert budget.used == 0
    assert stats["skipped_incomplete_week"] is True
    assert len(cache.entries) == 0


# ==========================================================================
# 3. The cache prevents repeat spend
# ==========================================================================

def test_a_hit_is_never_researched():
    cache = SearchCache()
    cache.record("p1", SEASON, 1, ["abc123"], as_of=AS_OF)

    assert cache.seen("p1", SEASON, 1) is True
    assert cache.video_ids("p1", SEASON, 1) == ["abc123"]


def test_a_MISS_is_never_researched():
    """The single most important cache property.

    An empty result is a question we already paid a search call to ask.
    If ``seen`` returned False for it, every run would re-ask every
    unanswerable query -- and since misses leave no clips behind, the
    allowance would drain into nothing, permanently, with no visible
    output change to reveal it.
    """
    cache = SearchCache()
    cache.record("p2", SEASON, 1, [], as_of=AS_OF)

    assert cache.seen("p2", SEASON, 1) is True, "a miss must count as searched"
    assert cache.video_ids("p2", SEASON, 1) == []


def test_cached_players_are_excluded_from_targets():
    players = [player("p1", "CeeDee Lamb", rank=3),
               player("p2", "Jane Doe", rank=4)]
    cache = SearchCache()
    cache.record("p1", SEASON, 1, ["abc"], as_of=AS_OF)   # hit
    cache.record("p2", SEASON, 1, [], as_of=AS_OF)        # miss

    targets = gap_fill_targets(
        {"clips": {}}, players, week=1, week_complete=True,
        season=SEASON, cache=cache,
    )
    assert targets == [], "cached hits AND misses must both be excluded"


def test_second_run_spends_nothing_on_the_same_week():
    """End-to-end dedupe: run twice, pay once."""
    from refresh_highlights import run_gap_fill

    players = [player(f"p{i}", f"Player{i} Brown", rank=i) for i in range(5)]
    cache = SearchCache()
    total_calls = []

    def fake_search(query, max_results=5):
        total_calls.append(query)
        return []                      # every one a miss

    b1 = SearchBudget(40)
    t1 = gap_fill_targets({"clips": {}}, players, week=1, week_complete=True,
                          season=SEASON, cache=cache)
    run_gap_fill(targets=t1, all_players=players, cache=cache, budget=b1,
                 season=SEASON, week=1, week_complete=True,
                 search_fn=fake_search, fetch_fn=lambda ids: [], as_of=AS_OF)
    first = len(total_calls)

    b2 = SearchBudget(40)
    t2 = gap_fill_targets({"clips": {}}, players, week=1, week_complete=True,
                          season=SEASON, cache=cache)
    run_gap_fill(targets=t2, all_players=players, cache=cache, budget=b2,
                 season=SEASON, week=1, week_complete=True,
                 search_fn=fake_search, fetch_fn=lambda ids: [], as_of=AS_OF)

    assert first == 5
    assert len(total_calls) == 5, "run 2 re-searched an already-answered week"
    assert t2 == []
    assert b2.used == 0


def test_cache_refuses_to_record_an_incomplete_week():
    cache = SearchCache()
    stored = cache.record("p1", SEASON, 2, [], week_complete=False, as_of=AS_OF)

    assert stored is False
    assert cache.seen("p1", SEASON, 2) is False, (
        "an in-progress week must stay searchable once it completes"
    )


def test_season_is_part_of_the_key():
    """Week 1 recurs every year; the film does not."""
    cache = SearchCache()
    cache.record("p1", 2026, 1, ["old"], as_of=AS_OF)

    assert cache.seen("p1", 2026, 1) is True
    assert cache.seen("p1", 2027, 1) is False


def test_cache_survives_a_save_load_round_trip(tmp_path):
    path = tmp_path / "search_cache.json"
    cache = SearchCache()
    cache.record("p1", SEASON, 1, ["abc", "def"], query="q", as_of=AS_OF)
    cache.record("p2", SEASON, 1, [], query="q2", as_of=AS_OF)
    cache.save(path, as_of=AS_OF)

    reloaded = SearchCache.load(path)
    assert reloaded.seen("p1", SEASON, 1) is True
    assert reloaded.video_ids("p1", SEASON, 1) == ["abc", "def"]
    assert reloaded.seen("p2", SEASON, 1) is True, "miss lost across restart"
    assert reloaded.video_ids("p2", SEASON, 1) == []

    payload = json.loads(path.read_text())
    assert payload["entry_count"] == 2
    assert "_comment" in payload


def test_a_corrupt_cache_degrades_to_empty_rather_than_crashing(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json at all")
    assert SearchCache.load(path).entries == {}
    assert SearchCache.load(tmp_path / "nope.json").entries == {}


def test_cached_ids_are_replayed_without_spending():
    """A rebuilt index must not re-buy film it already paid for."""
    players = [player("p1", "CeeDee Lamb", rank=3)]
    cache = SearchCache()
    cache.record("p1", SEASON, 1, ["cached1"], as_of=AS_OF)
    budget = SearchBudget(40)
    calls = []

    def fake_fetch(ids):
        return [video(v, "CeeDee Lamb: Every Touch of 2026 Week 1")
                for v in ids]

    videos, stats = _run_gap_fill(
        targets=[], all_players=players, cache=cache, budget=budget,
        season=SEASON, week=1, week_complete=True,
        search_fn=lambda q, max_results=5: calls.append(q) or [],
        fetch_fn=fake_fetch, as_of=AS_OF,
    )

    assert calls == []
    assert budget.used == 0
    assert stats["cache_replayed_ids"] == 1
    assert [v.video_id for v in videos] == ["cached1"]


# ==========================================================================
# 4. Targeting: who gets the scarce allowance
# ==========================================================================

def test_players_with_a_cutup_that_week_are_not_targets():
    players = [player("p1", "CeeDee Lamb", rank=3),
               player("p2", "Jane Doe", rank=9)]
    index = {"clips": {"p1": [{"kind": KIND_PLAYER, "bucket_week": 1}]}}

    targets = gap_fill_targets(index, players, week=1, week_complete=True,
                               season=SEASON)
    assert [p.sleeper_id for p in targets] == ["p2"]


def test_a_game_recap_does_not_count_as_coverage():
    """A recap is exactly the thin coverage this feature exists to fix."""
    players = [player("p1", "CeeDee Lamb", rank=3)]
    index = {"clips": {"p1": [{"kind": "team_game", "bucket_week": 1}]}}

    targets = gap_fill_targets(index, players, week=1, week_complete=True,
                               season=SEASON)
    assert [p.sleeper_id for p in targets] == ["p1"]


def test_a_cutup_from_a_different_week_does_not_count():
    players = [player("p1", "CeeDee Lamb", rank=3)]
    index = {"clips": {"p1": [{"kind": KIND_PLAYER, "bucket_week": 2}]}}

    targets = gap_fill_targets(index, players, week=1, week_complete=True,
                               season=SEASON)
    assert [p.sleeper_id for p in targets] == ["p1"]


def test_rostered_players_outrank_better_ranked_unrostered_ones():
    players = [
        player("star", "Star Player", rank=1),
        player("mine", "Rostered Guy", rank=80),
    ]
    targets = gap_fill_targets(
        {"clips": {}}, players, week=1, week_complete=True, season=SEASON,
        rostered_ids={"mine"},
    )
    assert [p.sleeper_id for p in targets] == ["mine", "star"]


def test_rank_orders_the_unrostered_and_unranked_go_last():
    players = [
        player("c", "Cee Player"),                 # unranked
        player("a", "Aay Player", rank=50),
        player("b", "Bee Player", rank=10),
    ]
    targets = gap_fill_targets({"clips": {}}, players, week=1,
                               week_complete=True, season=SEASON)
    assert [p.sleeper_id for p in targets] == ["b", "a", "c"]


def test_target_order_is_stable_across_runs():
    """Unstable order would spread a small budget thinly over everyone."""
    players = [player(f"p{i}", f"Name{i} X", rank=(i * 7) % 23)
               for i in range(30)]
    first = gap_fill_targets({"clips": {}}, players, week=1,
                             week_complete=True, season=SEASON)
    second = gap_fill_targets({"clips": {}}, list(reversed(players)),
                              week=1, week_complete=True, season=SEASON)
    assert [p.sleeper_id for p in first] == [p.sleeper_id for p in second]


def test_no_targets_for_an_incomplete_week():
    players = [player("p1", "CeeDee Lamb", rank=3)]
    assert gap_fill_targets({"clips": {}}, players, week=2,
                            week_complete=False, season=SEASON) == []


# ==========================================================================
# 5. Relevance: the matcher is reused, not loosened
# ==========================================================================

def test_query_is_the_owners_stated_phrasing():
    q = search_query_for(player("p1", "CeeDee Lamb"), 1, 2026)
    assert q == "CeeDee Lamb week 1 2026 highlights"


def test_search_result_must_name_the_player():
    lamb = player("p1", "CeeDee Lamb", team="DAL")
    idx = build_name_index([lamb])

    good = video("v1", "CeeDee Lamb: Every Touch of 2026 Week 1 | Full Film")
    other = video("v2", "Jake Ferguson: Every Touch of 2026 Week 1")

    assert accept_search_video(good, lamb, idx, week=1) is True
    assert accept_search_video(other, lamb, idx, week=1) is False


def test_surname_alone_is_not_a_match():
    """Guards the n-gram matcher against being quietly loosened."""
    lamb = player("p1", "CeeDee Lamb", team="DAL")
    idx = build_name_index([lamb])
    v = video("v1", "Lamb chops: week 1 fantasy recap")
    assert accept_search_video(v, lamb, idx, week=1) is False


def test_search_results_obey_the_existing_shorts_floor():
    lamb = player("p1", "CeeDee Lamb", team="DAL")
    idx = build_name_index([lamb])
    tiny = video("v1", "CeeDee Lamb Week 1 Every Touch", seconds=9)
    ok = video("v2", "CeeDee Lamb Week 1 Every Touch", seconds=42)

    assert accept_search_video(tiny, lamb, idx, week=1) is False
    assert accept_search_video(ok, lamb, idx, week=1) is True, (
        "a 42s cut-up is wanted since #65 relaxed the floor to 15s"
    )


def test_search_results_still_reject_memes_and_talk():
    lamb = player("p1", "CeeDee Lamb", team="DAL")
    idx = build_name_index([lamb])
    for title in (
        "CeeDee Lamb reacts to the win",
        "CeeDee Lamb trade rumors and fantasy advice",
        "CeeDee Lamb press conference after week 1",
    ):
        assert accept_search_video(video("v", title), lamb, idx,
                                   week=1) is False, title


def test_search_result_with_the_wrong_week_in_the_title_is_rejected():
    lamb = player("p1", "CeeDee Lamb", team="DAL")
    idx = build_name_index([lamb])
    wrong = video("v1", "CeeDee Lamb: Every Touch of 2026 Week 3")
    assert accept_search_video(wrong, lamb, idx, week=1) is False


def test_search_result_with_no_week_in_the_title_is_allowed():
    lamb = player("p1", "CeeDee Lamb", team="DAL")
    idx = build_name_index([lamb])
    v = video("v1", "CeeDee Lamb Every Target vs the Giants")
    assert accept_search_video(v, lamb, idx, week=1) is True


def test_ranking_prefers_cutup_phrasing_over_raw_popularity():
    """'Well reviewed' must not outrank 'definitely the right clip'."""
    popular = video("v1", "CeeDee Lamb Week 1 Highlights", views=500_000)
    precise = video("v2", "CeeDee Lamb: Every Touch of Week 1", views=900)

    ordered = [v.video_id for v in rank_search_videos([popular, precise])]
    assert ordered == ["v2", "v1"]


def test_view_count_breaks_ties_between_equally_precise_clips():
    a = video("a", "CeeDee Lamb: Every Touch of Week 1", views=100)
    b = video("b", "CeeDee Lamb: Every Touch of Week 1", views=90_000)
    assert [v.video_id for v in rank_search_videos([a, b])] == ["b", "a"]


# ==========================================================================
# 6. The #65 behaviour this feature must not regress
# ==========================================================================

def test_stacked_every_dropback_is_a_cutup_not_a_recap():
    """Real title from the verified STACKED film feed, 2026-09-21.

    Before "every dropback" joined the strong markers, a long QB cut-up
    naming two teams could be refiled as a game recap.
    """
    lamar = player("p1", "Lamar Jackson", team="BAL", pos="QB")
    idx = build_name_index([lamar])
    v = video(
        "v1",
        "Lamar Jackson: Every Dropback of 2026 Week 2 | Full Film Compilation",
        seconds=600,
    )
    assert classify(v, match_players(v, idx)) == KIND_PLAYER


def test_stacked_every_touch_title_matches_and_classifies():
    henry = player("p1", "Derrick Henry", team="BAL", pos="RB")
    idx = build_name_index([henry])
    v = video(
        "v1",
        "Derrick Henry: Every Touch of 2026 Week 2 | Full Film Compilation",
    )
    matched = match_players(v, idx)
    assert [p.sleeper_id for p in matched] == ["p1"]
    assert classify(v, matched) == KIND_PLAYER


def test_adp_talk_that_names_a_player_is_not_a_cutup():
    """Real title shape from the DISABLED STACKED Fantasy sister channel.

    It names a tracked player and contains no recap/meme phrasing, so
    without the ADP markers it classified as that player's cut-up and
    would have appeared in his reel.
    """
    young = player("p1", "Bryce Young", team="CAR", pos="QB")
    idx = build_name_index([young])
    v = video(
        "v1",
        "Bryce Young is going around pick 172, QB27, and the three year "
        "table only moves one way",
        seconds=300,
    )
    assert classify(v, match_players(v, idx)) != KIND_PLAYER


def test_gap_filled_clips_go_through_the_normal_index_build():
    """No privileged path: a search result is judged like any upload."""
    lamb = player("p1", "CeeDee Lamb", team="DAL", pos="WR", rank=3)
    good = video("v1", "CeeDee Lamb: Every Touch of 2026 Week 1 | Full Film",
                 published="2026-09-14T12:00:00Z")
    junk = video("v2", "CeeDee Lamb reacts to the win",
                 published="2026-09-14T12:00:00Z")

    index = build_index([lamb], [good, junk], as_of=AS_OF)
    ids = [c["video_id"] for c in index["clips"].get("p1", [])]

    assert "v1" in ids
    assert "v2" not in ids, "meme rejection from #65 regressed"
