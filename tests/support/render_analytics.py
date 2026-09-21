"""Render the site chrome in both analytics states and assert on it.

Run as a **subprocess**, never imported — like
``tests/support/render_chrome.py``, this installs stand-in modules into
``sys.modules`` so ``report.py`` can be imported on a checkout with no
dependency stack, and a test that imported it would hand those stubs to
every test that ran afterwards.

    python tests/support/render_analytics.py                 # assert only
    python tests/support/render_analytics.py --emit <dir>    # + write HTML

``--emit`` writes the rendered pages for ``tests/js/analytics_head_tests.mjs``
to execute. The point of that second half is that nothing here can tell you
whether the beacon *breaks the page* — only running the page's scripts can,
and that is what PR #71 established as the bar for touching ``_page``.

Exit 0 when the analytics wiring is correct, 1 with a report otherwise.
"""
from __future__ import annotations

import argparse
import re
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

# A fake token of the exact shape Cloudflare issues (32 lowercase hex). It
# is a *test fixture*, not a secret, and the whole point of the repo-scan
# check at the bottom is that a real one never joins it here.
FAKE_TOKEN = "0123456789abcdef0123456789abcdef"

FAILURES: list[str] = []
CHECKS = 0


def ok(cond, label: str, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILURES.append(f"{label}{f'  [{detail}]' if detail else ''}")


def _stub(name: str, **attrs) -> None:
    """Install a stand-in module, but only if the real one is absent."""
    try:
        __import__(name)
        return
    except Exception:  # noqa: BLE001
        pass
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod


def _install_stubs() -> None:
    class _Base:
        def __init__(self, *a, **k):
            pass

        def __init_subclass__(cls, **k):
            pass

    _stub("httpx", Client=object, AsyncClient=object, HTTPError=Exception)
    _stub("numpy")
    _stub("pandas")
    _stub("pydantic", BaseModel=_Base, Field=lambda *a, **k: None)
    _stub("pydantic_settings", BaseSettings=_Base,
          SettingsConfigDict=lambda **k: dict(**k))

    import dynasty  # noqa: F401  (real package, real __path__)

    _stub("dynasty.engine.similarity_v1", EngineResult=object, OUT_ROOT=".",
          run_engine=lambda **k: None)
    _stub("dynasty.engine.format_overlay",
          PRESETS={"sf_ppr": {"label": "Superflex PPR"}},
          OverlayResult=object, all_format_overlays=lambda e: {})
    _stub("dynasty.engine.superflex_vorp", SUPERFLEX_STARTERS={},
          apply_superflex_vorp=lambda r: None)
    _stub("dynasty.consensus", ConsensusComparison=object,
          compare_to_consensus=lambda **k: None,
          load_crosswalk=lambda *a, **k: {})
    _stub("dynasty.sources", __path__=[])
    _stub("dynasty.sources.keeptradecut", load_latest=lambda *a, **k: None)
    _stub("dynasty.sources.nflverse_career_stats",
          build_career_stats=lambda *a, **k: None,
          career_stats_html=lambda c: "")


# ---------------------------------------------------------------------------
# A body that carries the PR #71 hazard on purpose
# ---------------------------------------------------------------------------

# The live outage was not "a script failed to load", it was a page script
# that touched DFMHL *while being evaluated*. Rendering a page with a
# <p>hello</p> body would prove nothing about that, so the body used here
# reproduces the exact shape: a blob that calls DFMHL.chip at evaluation
# time, and real shipped page JS alongside it.
HAZARD_JS = """
/* PR #71 in miniature: DFMHL is touched during evaluation, not in a
   DOMContentLoaded handler. If the shared renderer is not already
   defined above this point, this throws and the page dies here. */
var DFM_ORDER_PROBE = (function () {
  var chip = DFMHL.chip({ name: 'Ja\\u2019Marr Chase', gsis: '00-0036900' });
  return typeof chip === 'string' ? 'ok' : 'wrong-type';
})();
"""


def page_body() -> str:
    """A body containing real shipped page scripts plus the hazard probe."""
    from dynasty.corpus_submit_js import corpus_submit_js
    from dynasty.managerscore_js import MANAGERSCORE_CORE_JS

    return (
        '<main id="analytics-probe">\n'
        '<div id="ms-corpus-state"></div>\n'
        f"<script>{HAZARD_JS}</script>\n"
        f"<script>{MANAGERSCORE_CORE_JS}</script>\n"
        f"<script>{corpus_submit_js('https://corpus.example.workers.dev')}</script>\n"
        "</main>"
    )


def render(monkey_env: dict) -> "tuple[str, str]":
    """``(root page, players/ page)`` rendered under ``monkey_env``.

    The environment is set and restored around the call because ``_page``
    reads it at build time, which is the behaviour under test.
    """
    import os

    from dynasty import report
    from dynasty.branding import page_title

    saved = {k: os.environ.get(k) for k in monkey_env}
    try:
        for k, v in monkey_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        ts = datetime(2026, 9, 21, 17, 0, tzinfo=timezone.utc)
        header = report._site_header("rankings", ts, "Superflex PPR")
        body = page_body()
        root = report._page(page_title("Dynasty Rankings"), header, body)
        sub = report._page(page_title("Josh Allen"), header, body,
                           css_href="../assets/style.css")
        return root, sub
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


BEACON_SRC = "static.cloudflareinsights.com/beacon.min.js"


def head_of(html: str) -> str:
    return html[: html.index("</head>")]


def assert_configured(pages) -> None:
    for html, name in pages:
        hits = html.count(BEACON_SRC)
        ok(hits == 1, f"{name}: the beacon appears exactly once", f"count={hits}")
        ok(f'data-cf-beacon=\'{{"token": "{FAKE_TOKEN}"}}\'' in html,
           f"{name}: the token is interpolated into the beacon payload verbatim")
        # Position: in <head>, and after the DFM_BASE bootstrap.
        head = head_of(html)
        ok(BEACON_SRC in head, f"{name}: the beacon is inside <head>")
        ok(head.index("window.DFM_BASE") < head.index(BEACON_SRC),
           f"{name}: the beacon comes after the DFM_BASE bootstrap")
        # It must be external + deferred: that is what makes it unable to
        # interleave with the inline blobs PR #71 is about.
        tag = re.search(r"<script[^>]*cloudflareinsights[^>]*></script>", html)
        ok(tag is not None, f"{name}: the beacon is a well-formed empty script tag")
        if tag:
            ok(" defer " in tag.group(0) or tag.group(0).startswith("<script defer"),
               f"{name}: the beacon script is deferred", tag.group(0)[:90])
            ok("src=" in tag.group(0),
               f"{name}: the beacon script is external, so it carries no inline code")
        # And it is nowhere in the body, where it could sit between blobs.
        ok(BEACON_SRC not in html[html.index("<body>"):],
           f"{name}: no beacon markup anywhere in the body")
        # The ordering PR #71 fixed must still hold with the beacon present.
        ok(html.index("var DFMHL") < html.index("DFM_ORDER_PROBE"),
           f"{name}: the shared renderer is still emitted before the page scripts")


def assert_unconfigured(pages) -> None:
    for html, name in pages:
        ok("cloudflareinsights" not in html,
           f"{name}: no Cloudflare beacon of any kind")
        ok("data-cf-beacon" not in html,
           f"{name}: no beacon attribute left behind")
        ok(FAKE_TOKEN not in html, f"{name}: no token")
        ok("plausible.io" not in html and "usefathom.com" not in html,
           f"{name}: no other provider leaked in either")
        # The failure this guards is subtle: a template that interpolates an
        # empty provider string can still leave <script></script> or
        # <script defer src=""></script> on every page.
        ok("<script></script>" not in html,
           f"{name}: no empty script tag")
        ok('src=""' not in html and "src=''" not in html,
           f"{name}: no script with an empty src")
        ok(re.search(r"<script[^>]*>\s*</script>", head_of(html)) is None,
           f"{name}: nothing blank left in <head>")
        ok(html.index("var DFMHL") < html.index("DFM_ORDER_PROBE"),
           f"{name}: the shared renderer is still emitted before the page scripts")


def assert_identical_but_for_the_beacon(configured: str, unconfigured: str,
                                        name: str) -> None:
    """With the beacon line removed, the two builds must be byte-identical.

    This is the "the site builds and works identically with no token"
    requirement stated as an assertion rather than a hope: it catches a
    change that also, say, reorders a meta tag only when analytics is on.
    """
    stripped = re.sub(r"<script[^>]*cloudflareinsights[^>]*></script>\n?", "",
                      configured)
    ok(stripped == unconfigured,
       f"{name}: removing the beacon yields exactly the unconfigured page",
       f"{len(stripped)} vs {len(unconfigured)} bytes")


def assert_provider_seam() -> None:
    """Switching provider is configuration, not a rewrite."""
    from dynasty import analytics

    cf = analytics.snippet({analytics.ENV_TOKEN: FAKE_TOKEN})
    ok("cloudflareinsights" in cf, "default provider is Cloudflare", cf[:60])

    pl = analytics.snippet({analytics.ENV_TOKEN: "example.com",
                            analytics.ENV_PROVIDER: "plausible"})
    ok("plausible.io" in pl and 'data-domain="example.com"' in pl,
       "DFM_ANALYTICS_PROVIDER=plausible swaps the beacon with no code change",
       pl[:80])

    fa = analytics.snippet({analytics.ENV_TOKEN: "ABCDEFG1",
                            analytics.ENV_PROVIDER: "fathom"})
    ok("usefathom.com" in fa, "fathom is reachable the same way", fa[:80])

    off = analytics.snippet({analytics.ENV_TOKEN: FAKE_TOKEN,
                             analytics.ENV_PROVIDER: "none"})
    ok(off == "", "provider=none measures nothing even with a token set")

    bogus = analytics.snippet({analytics.ENV_TOKEN: FAKE_TOKEN,
                               analytics.ENV_PROVIDER: "nope"})
    ok(bogus == "", "an unknown provider emits nothing rather than guessing")

    # Every provider must be external+deferred, which is the property the
    # <head> position depends on.
    for pname in ("cloudflare", "plausible", "fathom"):
        out = analytics.snippet({analytics.ENV_TOKEN: "example.com",
                                 analytics.ENV_PROVIDER: pname})
        ok("defer" in out and "src=" in out,
           f"provider {pname} emits a deferred external script", out[:80])

    # Token hygiene: a token that could break out of the attribute is
    # refused outright rather than escaped into something that cannot work.
    for bad in ('a"b', "a'b", "a<b", "a b", "ab", "x" * 200, "&amp;token"):
        out = analytics.snippet({analytics.ENV_TOKEN: bad})
        ok(out == "", f"a malformed token ({bad[:12]!r}) emits nothing")

    # Whitespace around a pasted secret must not disable analytics.
    padded = analytics.snippet({analytics.ENV_TOKEN: f"  {FAKE_TOKEN}\n"})
    ok(f'"token": "{FAKE_TOKEN}"' in padded,
       "a secret pasted with stray whitespace still works", padded[:90])

    ok(analytics.snippet({}) == "", "no environment at all emits nothing")
    ok(analytics.snippet({analytics.ENV_TOKEN: ""}) == "",
       "an empty token emits nothing")


def assert_no_token_committed() -> None:
    """No Cloudflare-shaped secret is in any tracked or staged file.

    ``git ls-files`` deliberately covers the index, not just HEAD, so a
    **newly added, not-yet-committed** file is scanned too. That is not a
    detail: the first version of this check silently skipped the brand-new
    docs/ANALYTICS.md because it was still untracked, which is precisely
    when a pasted token is most likely to be sitting in a new file.

    The built site under dynasty_site/ is gitignored and correctly ignored
    here -- a local build with a real token puts one there on purpose.
    """
    import subprocess

    proc = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                          cwd=str(REPO_ROOT))
    if proc.returncode != 0:
        ok(False, "git ls-files ran", proc.stderr.strip()[:120])
        return
    files = [f for f in proc.stdout.splitlines() if f]
    ok(len(files) > 100, f"repo scan sees the tracked tree ({len(files)} files)")

    # The scan must actually reach this feature's own files, or it proves
    # nothing about the change most likely to carry a token.
    for expected in ("docs/ANALYTICS.md", "src/dynasty/analytics.py"):
        ok(expected in files,
           f"the scan covers {expected} (stage new files before trusting this)")

    # A real beacon token is 32 hex characters. The fixture above is
    # deliberately of that shape, so it is excluded by value: this proves
    # the check would FIRE on a real token rather than being vacuous.
    hexish = re.compile(r"\b[0-9a-f]{32}\b")
    offenders = []
    for rel in files:
        path = REPO_ROOT / rel
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in hexish.finditer(text):
            if m.group(0) != FAKE_TOKEN:
                offenders.append(f"{rel}: unexpected 32-hex string")
                break
    ok(not offenders, "no tracked or staged file carries a beacon-shaped token",
       "; ".join(offenders[:4]))

    # And the fixture really is the shape we claim, or the check above is
    # testing nothing.
    ok(hexish.fullmatch(FAKE_TOKEN) is not None,
       "the test fixture has real-token shape, so the scan is not vacuous")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit", type=Path, default=None,
                    help="write rendered pages here for the node DOM tests")
    args = ap.parse_args()

    _install_stubs()

    import os
    # Start from a clean slate so a developer's own shell cannot decide the
    # outcome of the unconfigured case.
    for k in ("DFM_ANALYTICS_TOKEN", "DFM_ANALYTICS_PROVIDER"):
        os.environ.pop(k, None)

    off_root, off_sub = render({"DFM_ANALYTICS_TOKEN": None,
                                "DFM_ANALYTICS_PROVIDER": None})
    on_root, on_sub = render({"DFM_ANALYTICS_TOKEN": FAKE_TOKEN,
                              "DFM_ANALYTICS_PROVIDER": None})

    assert_unconfigured([(off_root, "unconfigured root"),
                         (off_sub, "unconfigured players/")])
    assert_configured([(on_root, "configured root"),
                       (on_sub, "configured players/")])
    assert_identical_but_for_the_beacon(on_root, off_root, "root")
    assert_identical_but_for_the_beacon(on_sub, off_sub, "players/")
    assert_provider_seam()
    assert_no_token_committed()

    if args.emit:
        args.emit.mkdir(parents=True, exist_ok=True)
        (args.emit / "configured.html").write_text(on_root, encoding="utf-8")
        (args.emit / "unconfigured.html").write_text(off_root, encoding="utf-8")
        (args.emit / "configured_sub.html").write_text(on_sub, encoding="utf-8")
        print(f"wrote 3 pages to {args.emit}")

    if FAILURES:
        print(f"{len(FAILURES)} of {CHECKS} analytics checks FAILED:")
        for line in FAILURES:
            print(f"  FAIL {line}")
        return 1
    print(f"All {CHECKS} analytics render checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
