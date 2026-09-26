"""Tiny pytest-compatible runner used because pytest is not installed here.

Supports only what this repository's tests actually use: fixtures (including
the ones in tests/conftest.py), pytest.raises and plain assert functions.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import inspect
import sys
import tempfile
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []
PASSED = 0


class RaisesProxy:
    def __call__(self, exc_type):
        return _Raises(exc_type)


class _Raises:
    def __init__(self, exc_type) -> None:
        self.exc_type = exc_type
        self.value: Exception | None = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"{self.exc_type.__name__} was not raised")
        if not issubclass(exc_type, self.exc_type):
            return False
        self.value = exc
        return True


class FixtureRequest:
    def __init__(self, name, factory, cache):
        self._name = name
        self._factory = factory
        self._cache = cache

    def getfixturevalue(self, name):
        return self._factory(name)


def make_pytest():
    pytest = types.ModuleType("pytest")

    def fixture(func=None, **_kwargs):
        if func is None:
            return lambda f: _mark_fixture(f)
        return _mark_fixture(func)

    def _mark_fixture(func):
        func._is_fixture = True
        return func

    pytest.fixture = fixture
    pytest.raises = RaisesProxy()
    pytest.skip = lambda *a, **k: None
    return pytest


def load_conftest():
    sys.path.insert(0, str(ROOT / "tests"))
    import conftest  # noqa: F401

    return conftest


def collect_fixtures(conftest_module) -> dict[str, tuple[object, object]]:
    fixtures = {}
    for name, obj in vars(conftest_module).items():
        if callable(obj) and getattr(obj, "_is_fixture", False):
            gen = inspect.isgeneratorfunction(obj)
            fixtures[name] = (obj, gen)
    return fixtures


def run_module(path: Path, fixtures: dict[str, tuple[object, object]]) -> None:
    global PASSED
    module_name = f"tests.{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module

    def build(name, stack: list[str]):
        if name in stack:
            raise RuntimeError(f"fixture cycle: {stack + [name]}")
        if name in CACHE:
            return CACHE[name]
        if name == "request":
            value = FixtureRequest(name, lambda n: build(n, stack + [name]), {})
            CACHE[name] = value
            return value
        if name == "tmp_path":
            value = Path(tempfile.mkdtemp())
            CACHE[name] = value
            return value

        class CapSys:
            def __init__(self):
                import io

                self._out = io.StringIO()
                self._err = io.StringIO()
                self._old_out, self._old_err = sys.stdout, sys.stderr
                sys.stdout, sys.stderr = self._out, self._err

            def readouterr(self):
                out, err = self._out.getvalue(), self._err.getvalue()
                self._out.seek(0)
                self._out.truncate(0)
                self._err.seek(0)
                self._err.truncate(0)
                return types.SimpleNamespace(out=out, err=err)

            def close(self):
                sys.stdout, sys.stderr = self._old_out, self._old_err

        if name == "capsys":
            cap = CapSys()
            CACHE[name] = cap
            CLEANUPS.append(cap.close)
            return cap
        if name not in fixtures:
            raise RuntimeError(f"unknown fixture {name!r}")
        func, is_gen = fixtures[name]
        deps = set(inspect.signature(func).parameters)
        kwargs = {dep: build(dep, stack + [name]) for dep in deps}
        if is_gen:
            gen = func(**kwargs)
            value = next(gen)

            def cleanup(gen=gen):
                with contextlib.suppress(StopIteration):
                    next(gen)

        else:
            value = func(**kwargs)
            cleanup = lambda: None
        CACHE[name] = value
        CLEANUPS.append(cleanup)
        return value

    try:
        spec.loader.exec_module(module)
    except Exception:
        FAILURES.append(f"{path.name}: collection failed\n{traceback.format_exc()}")
        return

    for name, obj in sorted(vars(module).items()):
        if not name.startswith("test_") or not callable(obj):
            continue
        deps = list(inspect.signature(obj).parameters)
        CLEANUPS.clear()
        CACHE.clear()
        try:
            kwargs = {dep: build(dep, []) for dep in deps}
            obj(**kwargs)
            PASSED += 1
            print(".", end="", flush=True)
        except Exception:
            FAILURES.append(f"{path.name}::{name}\n{traceback.format_exc()}")
            print("F", end="", flush=True)
        finally:
            for cleanup in reversed(CLEANUPS):
                try:
                    cleanup()
                except Exception:
                    FAILURES.append(f"{path.name}::{name} teardown\n{traceback.format_exc()}")


CLEANUPS: list = []
CACHE: dict[str, object] = {}


def main() -> int:
    sys.modules["pytest"] = make_pytest()
    conftest_module = load_conftest()
    fixtures = collect_fixtures(conftest_module)
    tests_dir = ROOT / "tests"
    for path in sorted(tests_dir.glob("test_*.py")):
        run_module(path, fixtures)
    print()
    if FAILURES:
        print(f"\n{len(FAILURES)} failed, {PASSED} passed\n")
        for failure in FAILURES:
            print("=" * 70)
            print(failure)
        return 1
    print(f"\n{PASSED} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
