"""app/algo/native.cpp gives the Python engine's answers exactly.

Skipped where no .so is built (a dev box without g++). On the server the same
checks gate the build: deploy/update.sh deletes a .so that fails native_check.
"""
import random

import pytest

from app.algo import engine, native_check


@pytest.fixture
def native():
    if engine._native_lib() is None:
        pytest.skip("app/algo/_native.so not built here")


def test_native_mc_matches_python_bit_for_bit(native):
    paths = engine._native_mc()
    rnd = random.Random(3)
    for n, runs, seed, cap in [(10, 50, 7, 1e5), (9537, 30, 7, 1e5), (1000, 20, 0, 50.0), (257, 10, 2**32 - 1, 1e4)]:
        nets = [rnd.uniform(-900, 1000) for _ in range(n)]
        assert paths(nets, runs, seed, cap) == engine._mc_paths(nets, runs, seed, cap), (n, seed)


def test_native_loop_matches_python_bit_for_bit(native):
    assert native_check.check() == []


def test_uncovered_strategies_stay_on_the_python_loop():
    s = native_check.specs(1)[0]
    for change in ({"scale_out": [{"type": "pct", "value": 1, "qty_pct": 50}]},
                   {"session": {**s["session"], "square_off": None}},
                   {"instrument": {**s["instrument"], "type": "future"}}):
        assert not engine._native_ok({**s, **change})
