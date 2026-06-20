"""v3.14 — Box Score Dynasty rebrand.

Phil's 2026-06-03 brief on the rebrand:

  * Brand: "Box Score Dynasty" (everywhere — page titles, header logo,
    footer, social meta).
  * Meta description: "Box Score Dynasty — NFL dynasty fantasy football
    rankings focused on production, not hype. Every player ranked by
    what they actually did on the field."
  * Hero tagline: "Production over noise." (primary, on the homepage).
    Secondary subtitle: "Dynasty rankings built on box scores, not
    narratives."
  * <title> pattern: "Box Score Dynasty — <page name>".
  * Social meta (og:title, og:description, twitter:card) all consistent
    with the meta description above.

This test suite pins all of the above so an accidental revert to
"Kings of Dynasty" or a drift in the canonical meta description fails
loud at CI.
"""

from datetime import datetime

import pytest

from dynasty import report
from dynasty.report import (
    BRAND_NAME,
    BRAND_META_DESCRIPTION,
    BRAND_HERO_TAGLINE,
    BRAND_HERO_SUBTITLE,
    _brand_title,
    _page,
    _site_header,
    _footer,
    _meta_tags,
)


LATEST_TS = datetime(2026, 6, 3, 12, 0, 0)


# ---------------------------------------------------------------------------
# Brand constants — wire-pinned exactly as Phil specified
# ---------------------------------------------------------------------------


def test_brand_name_is_box_score_dynasty():
    assert BRAND_NAME == "Box Score Dynasty"


def test_brand_meta_description_matches_phil_brief_verbatim():
    """The meta description is the source of truth for og:description +
    twitter:description as well, so it must match Phil's brief exactly."""
    assert BRAND_META_DESCRIPTION == (
        "Box Score Dynasty — NFL dynasty fantasy football rankings focused "
        "on production, not hype. Every player ranked by what they actually "
        "did on the field."
    )


def test_brand_hero_tagline_is_production_over_noise():
    assert BRAND_HERO_TAGLINE == "Production over noise."


def test_brand_hero_subtitle_is_dynasty_rankings_built_on_box_scores():
    """Phil offered two subtitle options; the one chosen for v3.14 is
    'Dynasty rankings built on box scores, not narratives.' — it reads
    cleaner with the current layout than the alternative."""
    assert BRAND_HERO_SUBTITLE == (
        "Dynasty rankings built on box scores, not narratives."
    )


def test_brand_title_helper_follows_dash_pattern():
    """<title> tags follow the 'Box Score Dynasty — <page name>' pattern."""
    assert _brand_title("Methodology") == "Box Score Dynasty — Methodology"
    assert _brand_title("Dynasty Rankings") == "Box Score Dynasty — Dynasty Rankings"
    assert _brand_title("Prospects") == "Box Score Dynasty — Prospects"


# ---------------------------------------------------------------------------
# Page wrapper — title, meta, og, twitter
# ---------------------------------------------------------------------------


def _wrap(page_name: str) -> str:
    return _page(_brand_title(page_name), "<header></header>", "<body></body>")


def test_page_title_contains_brand_and_page_name():
    html = _wrap("Methodology")
    assert "<title>Box Score Dynasty — Methodology</title>" in html


def test_page_carries_meta_description_matching_canonical_brief():
    html = _wrap("Methodology")
    assert (
        f'<meta name="description" content="{BRAND_META_DESCRIPTION}">' in html
    )


def test_page_carries_og_title_with_brand_prefix():
    html = _wrap("Sources")
    assert (
        '<meta property="og:title" content="Box Score Dynasty — Sources">'
        in html
    )


def test_page_carries_og_description_matching_meta_description():
    html = _wrap("Sources")
    assert (
        f'<meta property="og:description" content="{BRAND_META_DESCRIPTION}">'
        in html
    )


def test_page_carries_og_site_name():
    html = _wrap("Prospects")
    assert (
        '<meta property="og:site_name" content="Box Score Dynasty">' in html
    )


def test_page_carries_og_type_website():
    html = _wrap("Prospects")
    assert '<meta property="og:type" content="website">' in html


def test_page_carries_og_url_pointing_at_phils_gh_pages():
    html = _wrap("Prospects")
    assert (
        '<meta property="og:url" '
        'content="https://pstiehl.github.io/Dynasty-Football-Model/">'
        in html
    )


def test_page_carries_twitter_card_summary():
    html = _wrap("Methodology")
    assert '<meta name="twitter:card" content="summary">' in html


def test_page_carries_twitter_title_matching_og_title():
    html = _wrap("Sources")
    assert (
        '<meta name="twitter:title" content="Box Score Dynasty — Sources">'
        in html
    )


def test_page_carries_twitter_description_matching_meta_description():
    html = _wrap("Sources")
    assert (
        f'<meta name="twitter:description" content="{BRAND_META_DESCRIPTION}">'
        in html
    )


# ---------------------------------------------------------------------------
# Site header + footer rebrand
# ---------------------------------------------------------------------------


def test_site_header_logo_is_box_score_dynasty():
    """The h1 in the site header reads 'Box Score Dynasty' with 'Dynasty'
    keeping the existing accent-coloured span."""
    html = _site_header("rankings", LATEST_TS, "sf_ppr")
    assert "Box Score" in html
    assert '<span class="accent">Dynasty</span>' in html
    assert "Kings of Dynasty" not in html


def test_site_header_meta_strip_carries_hero_tagline():
    """The site header's secondary meta line now leads with the hero
    tagline ('Production over noise.') instead of the generic
    'Fantasy Football' descriptor — the tagline is the brand."""
    html = _site_header("rankings", LATEST_TS, "sf_ppr")
    assert "Production over noise." in html


def test_site_footer_is_box_score_dynasty():
    footer = _footer()
    assert "Box Score Dynasty" in footer
    assert "Kings of Dynasty" not in footer


# ---------------------------------------------------------------------------
# Hero block on rankings (landing) page
# ---------------------------------------------------------------------------


def _stub_engine_for_rankings():
    """Minimal EngineResult-shaped stub for the rankings builder.

    ``_build_rankings`` only reads ``engine.rankings`` (list of dicts)
    and ``engine.long_arc_corpus`` (just used for a count) plus a few
    other counters in the KPI strip. Provide just enough for a render.
    """
    class _Stub:
        rankings = [
            {
                "player_id": "00-0034796",
                "name": "Test Player",
                "position": "RB",
                "age": 25,
                "projected_years_remaining": 7.0,
                "tier": 1,
                "overall_rank": 1,
                "production_score": 1234.0,
                "comp_tier": "elite",
                "engine": "similarity_v1",
            },
        ]
        long_arc_corpus = list(range(2000))

    return _Stub()


def test_rankings_page_renders_hero_tagline_visibly():
    """The hero tagline and subtitle render inside a ``.brand-hero``
    block on the landing page (rankings.html / index.html)."""
    html = report._build_rankings(
        _stub_engine_for_rankings(),
        LATEST_TS,
        "sf_ppr",
        team_lookup={"00-0034796": "BAL"},
    )
    assert 'class="brand-hero"' in html
    assert "Production over noise." in html
    assert "Dynasty rankings built on box scores, not narratives." in html


def test_rankings_page_hero_uses_brand_constants_not_inlined():
    """Sanity: the hero block reads from the BRAND_* constants. If
    someone changes BRAND_HERO_TAGLINE the rankings page picks it up
    automatically."""
    html = report._build_rankings(
        _stub_engine_for_rankings(),
        LATEST_TS,
        "sf_ppr",
        team_lookup={"00-0034796": "BAL"},
    )
    assert BRAND_HERO_TAGLINE in html
    assert BRAND_HERO_SUBTITLE in html


# ---------------------------------------------------------------------------
# CSS — hero block styling exists
# ---------------------------------------------------------------------------


def test_shared_css_has_brand_hero_styling():
    css = report._shared_css()
    assert ".brand-hero" in css
    assert ".brand-hero-tagline" in css
    assert ".brand-hero-subtitle" in css


# ---------------------------------------------------------------------------
# No lingering 'Kings of Dynasty' references in the rendered site
# ---------------------------------------------------------------------------


def test_no_kings_of_dynasty_in_site_header_or_footer():
    html = _site_header("rankings", LATEST_TS, "sf_ppr") + _footer()
    assert "Kings of Dynasty" not in html
    assert "Kings of" not in html


def test_no_kings_of_dynasty_in_rendered_page_skeleton():
    html = _wrap("Methodology")
    assert "Kings of Dynasty" not in html


def test_meta_tags_helper_renders_all_required_blocks():
    block = _meta_tags("Methodology")
    # Required blocks (one assert per to keep failures granular):
    assert 'name="description"' in block
    assert 'property="og:type"' in block
    assert 'property="og:site_name"' in block
    assert 'property="og:title"' in block
    assert 'property="og:description"' in block
    assert 'property="og:url"' in block
    assert 'name="twitter:card"' in block
    assert 'name="twitter:title"' in block
    assert 'name="twitter:description"' in block
