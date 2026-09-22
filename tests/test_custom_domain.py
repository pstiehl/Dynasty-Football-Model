"""The CNAME file is the whole custom domain, so test it like it matters.

A typo here does not break the build or fail a test in any obvious way --
it publishes the site on a hostname nobody owns, and the only symptom is a
dead link that looked fine in review.
"""
from __future__ import annotations

from pathlib import Path

from dynasty import custom_domain


def test_default_domain_is_the_registered_one():
    assert custom_domain.domain({}) == "nextleveldynastyfootball.com"


def test_env_overrides_the_default():
    assert custom_domain.domain({"DFM_SITE_DOMAIN": "staging.example.com"}) == "staging.example.com"


def test_explicitly_empty_env_means_no_custom_domain(tmp_path: Path):
    """The escape hatch back to github.io must actually write nothing."""
    assert custom_domain.domain({"DFM_SITE_DOMAIN": ""}) == ""
    assert custom_domain.write_cname(tmp_path, {"DFM_SITE_DOMAIN": ""}) is None
    assert not (tmp_path / "CNAME").exists()


def test_write_cname_emits_bare_host_and_one_newline(tmp_path: Path):
    path = custom_domain.write_cname(tmp_path, {})
    assert path == tmp_path / "CNAME"
    assert path.read_text(encoding="utf-8") == "nextleveldynastyfootball.com\n"


def test_scheme_and_slashes_are_stripped(tmp_path: Path):
    """GitHub rejects a CNAME containing a URL; normalise instead of trusting."""
    for raw in (
        "https://nextleveldynastyfootball.com",
        "http://nextleveldynastyfootball.com/",
        "  NextLevelDynastyFootball.com  ",
    ):
        path = custom_domain.write_cname(tmp_path, {"DFM_SITE_DOMAIN": raw})
        assert path is not None
        assert path.read_text(encoding="utf-8") == "nextleveldynastyfootball.com\n"


def test_site_build_includes_cname(tmp_path: Path):
    """Guard the wiring, not just the helper.

    The helper being correct is useless if generate_site stops calling it,
    which is exactly the kind of thing a later refactor drops silently.
    """
    import inspect

    from dynasty import report

    src = inspect.getsource(report.generate_site)
    assert "write_cname" in src, "generate_site must emit the CNAME file"
