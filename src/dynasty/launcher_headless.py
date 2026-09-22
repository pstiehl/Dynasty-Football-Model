"""Headless launcher — v2.3.4 daily-refresh pipeline.

Every step that pulls external data runs on each invocation so the
site always reflects fresh-as-of-today inputs. Phil 2026-05-22:

  "I want everything to pull from every source on a daily basis. I
   know the code is meant to run every day, but lets make sure that
   the scrapes from all of the sources runs every day as well."

Pipeline:
    [1/8]  init DB (used for Sleeper metadata + league imports)
    [2/8]  refresh nflverse caches (player_stats_season + players.csv.gz)
    [3/8]  sync Sleeper + MFL player metadata
    [4/8]  refresh KeepTradeCut consensus + dynastyprocess crosswalk
    [5/8]  run the v2.3 similarity engine
    [6/8]  build v3.0 prospect projection layer (PR 4)
    [7/8]  build the static site
    [8/8]  pre-fetch any leagues listed in leagues.json
"""
from __future__ import annotations
import sys
from pathlib import Path


def main():
    print("=" * 60)
    print("Dynasty Football Model — v1.0 headless refresh (CI)")
    print("=" * 60)

    # Step 1: init DB (used for Sleeper metadata + league imports only).
    print("\n[1/8] Initializing database...")
    try:
        from dynasty.db.session import init_db
        init_db()
        print("  OK")
    except Exception as e:
        print(f"  FAIL: {e}")
        sys.exit(1)

    # Step 2: refresh nflverse caches (player_stats_season + players).
    # This MUST run before the engine — the engine reads from these
    # files. Daily mode just re-pulls the current season's stats and
    # the players metadata; older seasons are static. Non-fatal: if
    # the network is down we keep the previous cache and the engine
    # still builds.
    print("\n[2/8] Refreshing nflverse caches...")
    try:
        from pathlib import Path as _P
        sys.path.insert(
            0,
            str(_P(__file__).resolve().parent.parent.parent / "scripts"),
        )
        import refresh_nflverse_corpus  # type: ignore
        nflverse_summary = refresh_nflverse_corpus.refresh(verbose=True)
        print(
            f"  OK · current_season={nflverse_summary['current_season']} "
            f"years_fetched={len(nflverse_summary['years_fetched'])} "
            f"rows_total={nflverse_summary['rows_total']:,}"
        )
    except Exception as e:
        print(f"  WARN: nflverse refresh failed: {e}")
        print("  (Engine will run against the previously cached corpus.)")

    # v3.15: the weekly artifacts behind the rookie NFL comparison.
    #
    # Separate from the season refresh above because it answers a
    # different question: the season file says "16 games, 1,100 yards",
    # and a rookie comparison needs "through two games he had 154". This
    # re-pulls the in-progress season every build, so N grows on its own
    # as weeks are played and nothing has to be edited in September.
    #
    # Warn-and-continue, exactly like the season refresh: a network
    # failure leaves the previous artifacts in place and the rookie board
    # renders last build's N rather than taking the site down.
    try:
        import refresh_nflverse_weekly  # type: ignore
        weekly_summary = refresh_nflverse_weekly.refresh(verbose=False)
        print(
            f"  weekly OK · season={weekly_summary['current_season']} "
            f"weeks={weekly_summary['current_weeks']} "
            f"first_n_players={weekly_summary['first_n_players']:,}"
        )
    except Exception as e:
        print(f"  WARN: nflverse weekly refresh failed: {e}")
        print("  (Rookie NFL comps will use the previously cached weekly data.)")

    # Step 3: Sleeper + MFL player metadata.
    print("\n[3/8] Syncing player metadata (Sleeper + MFL)...")
    try:
        from dynasty.sync import sync_sleeper_players
        n = sync_sleeper_players()
        print(f"  Sleeper: {n:,} players")
    except Exception as e:
        print(f"  Sleeper WARN: {e}")
    try:
        from dynasty.sync import sync_mfl_players
        mfl = sync_mfl_players()
        print(
            f"  MFL crosswalk: matched={mfl['matched']:,} "
            f"already_set={mfl['already_set']:,} "
            f"ambiguous={mfl['ambiguous']:,} "
            f"(of {mfl['total_mfl_players']:,})"
        )
    except Exception as e:
        print(f"  MFL crosswalk WARN: {e}")

    # Step 4: Refresh KTC consensus snapshot + dynastyprocess crosswalk.
    # Moved BEFORE the engine run so a freshly published consensus is
    # available to the site builder in the same invocation. Non-fatal:
    # if the network call fails we keep the previous day's cache and
    # the site falls back gracefully.
    print("\n[4/8] Refreshing KTC consensus + dynastyprocess crosswalk...")
    try:
        import refresh_ktc_consensus  # type: ignore
        n = refresh_ktc_consensus.refresh()
        print(f"  OK · {n} consensus players cached")
    except Exception as e:
        print(f"  WARN: KTC refresh failed: {e}")
        print("  (Site will use cached snapshot if available, otherwise overlay fallback.)")

    # Step 5: Run the similarity engine.
    print("\n[5/8] Running similarity engine...")
    try:
        from dynasty.engine.similarity_v1 import run_engine
        engine_result = run_engine(persist=True)
        print(
            f"  OK · active={len(engine_result.active_players):,} "
            f"retired_corpus={len(engine_result.retired_corpus):,} "
            f"ranked={len(engine_result.rankings):,}"
        )
        print(f"  Era-pace source: {engine_result.era_pace.source}")
        # Surface the top-5 for log breadcrumbs.
        for r in engine_result.rankings[:5]:
            print(
                f"    #{r['overall_rank']}. {r['name']:24s} "
                f"{r['position']:>2}  pts={r['production_score']:.0f}  "
                f"comp={r['top_comp']}"
            )
    except Exception as e:
        print(f"  FAIL: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # Step 5.5 (v3.3, Phil 2026-05-28): refresh external draft data
    # (PFR draft classes for 2022..2026 + Tankathon 2027 big board)
    # so the prospects pipeline below can mark drafted prospects and
    # surface the 2027 class. Non-fatal — a network blip shouldn't
    # kill the daily refresh.
    print("\n[5b/8] Refreshing PFR + Tankathon draft data (v3.3)...")
    try:
        scripts_dir = str(Path(__file__).resolve().parents[2] / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import refresh_pfr_draft_classes  # type: ignore
        rc = refresh_pfr_draft_classes.main([])
        if rc != 0:
            print("  WARN: PFR draft-class refresh exited non-zero (continuing)")
        else:
            print("  OK · PFR draft data refreshed (2022..2026)")
    except Exception as e:
        print(f"  WARN: PFR draft-class refresh failed: {e}")
    try:
        import refresh_tankathon_big_board  # type: ignore
        rc = refresh_tankathon_big_board.main([])
        if rc != 0:
            print("  WARN: Tankathon big-board refresh exited non-zero")
        else:
            print("  OK · Tankathon 2027 big board refreshed")
    except Exception as e:
        print(f"  WARN: Tankathon big-board refresh failed: {e}")

    # Step 6: Build v3.0 prospect projection layer (PR 4).
    print("\n[6/8] Building v3.0 prospect projection layer...")
    try:
        import os as _os
        scripts_dir = str(Path(__file__).resolve().parents[2] / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import build_prospects_v3  # type: ignore
        rc = build_prospects_v3.main([])
        if rc != 0:
            print("  WARN: prospect projection layer exited non-zero (continuing)")
        else:
            print("  OK · prospect artifacts written")
    except Exception as e:
        # Non-fatal: don't block the daily site refresh on a stale engine cache.
        print(f"  WARN: v3.0 prospect projection failed: {e}")
        print("  (Site will fall back to the placeholder prospects page.)")

    # Step 7: Build the static site.
    print("\n[7/8] Building site...")
    try:
        from dynasty.report import generate_site
        out = generate_site(
            output_dir="dynasty_site",
            league_format="sf_ppr",
            limit=300,
            engine=engine_result,
        )
        print(f"  OK -> {out}")
    except Exception as e:
        print(f"  FAIL: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # Step 8: Pre-fetch leagues listed in leagues.json.
    print("\n[8/8] Pre-fetching listed leagues...")
    try:
        import prefetch_leagues  # type: ignore
        summary = prefetch_leagues.prefetch_all()
        ok_count = len(summary.get("leagues", []))
        err_count = len(summary.get("errors", []))
        print(f"  Pre-fetched {ok_count} leagues, {err_count} errors")
        for L in summary.get("leagues", []):
            print(f"    {L['slug']:>40}  teams={L['n_teams']:>2}  managers={L['n_managers']:>2}  ({L['name']})")
        for err in summary.get("errors", []):
            print(f"    [error] {err['entry']}: {err['error']}")
    except Exception as e:
        print(f"  WARN: pre-fetch step failed: {e}")
        print("  (Site still builds without pre-fetched leagues.)")

    print("\nDone.")


if __name__ == "__main__":
    main()
