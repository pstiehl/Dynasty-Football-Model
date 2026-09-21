"""Run this repo's pure-Python tests with nothing but the standard library.

``pytest`` is not always installable -- an offline checkout, a locked-down
CI image, a machine with no pip. The highlight tests have no network and no
DB dependency, so there is no real reason they should be unrunnable there.

This installs a tiny stand-in for the slice of the pytest API those modules
use (``@pytest.fixture``, ``@pytest.mark.parametrize``, ``pytest.raises``),
imports the requested test modules, and calls every ``test_*`` function.

    python scripts/run_tests_stdlib.py                       # default modules
    python scripts/run_tests_stdlib.py tests/test_names.py   # specific ones

This is a fallback, not a replacement: it does not implement conftest
plugins, parametrised fixtures, marks, or anything else pytest does. Modules
needing those will fail here and should be run under real pytest.
"""
from __future__ import annotations

import importlib.util
import inspect
import sys
import traceback
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

DEFAULT_MODULES = [
    "tests/test_highlights.py",
    "tests/test_highlights_window.py",
    "tests/test_roster_crosswalk.py",
]


# --------------------------------------------------------------------------
# The pytest stand-in
# --------------------------------------------------------------------------

def _install_pytest_stub() -> None:
    if "pytest" in sys.modules:
        return

    mod = types.ModuleType("pytest")

    def fixture(func=None, **_kw):
        def wrap(f):
            f.__is_fixture__ = True
            return f
        return wrap(func) if func is not None else wrap

    class _Mark:
        @staticmethod
        def parametrize(argnames, argvalues, **_kw):
            names = ([a.strip() for a in argnames.split(",")]
                     if isinstance(argnames, str) else list(argnames))

            def wrap(f):
                f.__parametrize__ = (names, list(argvalues))
                return f
            return wrap

        def __getattr__(self, _name):          # skip/xfail/etc: no-op marks
            def wrap(*_a, **_k):
                return lambda f: f
            return wrap

    class _Raises:
        def __init__(self, exc):
            self.exc = exc

        def __enter__(self):
            return self

        def __exit__(self, t, v, tb):
            if t is None:
                raise AssertionError(f"expected {self.exc.__name__}")
            return issubclass(t, self.exc)

    mod.fixture = fixture
    mod.mark = _Mark()
    mod.raises = _Raises
    mod.approx = lambda v, rel=1e-6, abs=1e-12: v   # noqa: A002
    sys.modules["pytest"] = mod


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


def _fixtures(module) -> dict:
    return {
        name: obj for name, obj in vars(module).items()
        if callable(obj) and getattr(obj, "__is_fixture__", False)
    }


def _calls(func, fixtures):
    """Yield ``(label, kwargs)`` for every invocation of one test."""
    names, values = getattr(func, "__parametrize__", (None, None))
    params = list(inspect.signature(func).parameters)

    def resolve(extra):
        kwargs = dict(extra)
        for p in params:
            if p in kwargs:
                continue
            if p not in fixtures:
                raise KeyError(f"no fixture named {p!r}")
            kwargs[p] = fixtures[p]()
        return kwargs

    if names is None:
        yield "", resolve({})
        return
    for case in values:
        case = case if isinstance(case, (tuple, list)) else (case,)
        extra = dict(zip(names, case))
        yield "[" + ", ".join(repr(c) for c in case) + "]", resolve(extra)


def run(paths) -> int:
    _install_pytest_stub()
    passed = failed = 0
    failures = []

    for rel in paths:
        path = (REPO_ROOT / rel) if not Path(rel).is_absolute() else Path(rel)
        print(f"\n{path.relative_to(REPO_ROOT)}")
        try:
            module = _load(path)
        except Exception:
            failed += 1
            failures.append((str(rel), "<import>", traceback.format_exc()))
            print("  FAIL <import>")
            continue

        fixtures = _fixtures(module)
        tests = [(n, o) for n, o in sorted(vars(module).items())
                 if n.startswith("test_") and callable(o)]

        for name, func in tests:
            try:
                invocations = list(_calls(func, fixtures))
            except Exception:
                failed += 1
                failures.append((str(rel), name, traceback.format_exc()))
                print(f"  FAIL {name} (setup)")
                continue
            for label, kwargs in invocations:
                try:
                    func(**kwargs)
                    passed += 1
                    print(f"  ok   {name}{label}")
                except Exception:
                    failed += 1
                    failures.append((str(rel), name + label,
                                     traceback.format_exc()))
                    print(f"  FAIL {name}{label}")

    for rel, name, tb in failures:
        print(f"\n{'=' * 68}\n{rel}::{name}\n{'=' * 68}\n{tb}")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:] or DEFAULT_MODULES))
