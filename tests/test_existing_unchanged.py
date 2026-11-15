"""Test 18: adding Strategy ChatGPT must not change what any existing strategy decides.

EXPECTED was recorded on 2026-11-15 by running tests/scenario.py against the main-branch
forward_test.py from BEFORE Strategy ChatGPT existed (commit da450a6). Each entry is
(token, stake, venue, cost, price paid). If a strategy's rules are changed on purpose later,
re-record its entry; otherwise a failure here means an existing strategy changed by accident.
"""
import pytest

import forward_test as ft
import scenario
from conftest import new_state, run_strategy

EXPECTED = {
    "a": [("tok-p01-0", 0.02, "kalshi", 20.0, 0.5), ("tok-p02-0", 0.01, "kalshi", 10.0, 0.56),
          ("tok-p06-0", 0.01, "kalshi", 8.02, 0.3), ("tok-p08-0", 0.02, "prophetx", 20.0, 0.6),
          ("tok-p10-0", 0.02, "kalshi", 20.0, 0.5), ("tok-p11-0", 0.01, "kalshi", 10.0, 0.5),
          ("tok-p15-0", 0.01, "kalshi", 10.0, 0.605)],
    "b": [("tok-p01-0", 0.02, "kalshi", 20.0, 0.5), ("tok-p02-0", 0.01, "kalshi", 10.0, 0.56),
          ("tok-p04-0", 0.02, "kalshi", 20.0, 0.5), ("tok-p06-0", 0.02, "kalshi", 16.04, 0.3),
          ("tok-p07-0", 0.01, "kalshi", 10.0, 0.8), ("tok-p10-0", 0.02, "kalshi", 20.0, 0.5),
          ("tok-p11-0", 0.02, "kalshi", 20.0, 0.5), ("tok-p15-0", 0.01, "kalshi", 10.0, 0.605)],
    "c": [("tok-p01-0", 0.02, "kalshi", 20.0, 0.5), ("tok-p02-0", 0.01, "kalshi", 10.0, 0.56),
          ("tok-p04-0", 0.02, "kalshi", 20.0, 0.5), ("tok-p06-0", 0.01, "kalshi", 8.02, 0.3),
          ("tok-p07-0", 0.01, "kalshi", 10.0, 0.8), ("tok-p10-0", 0.02, "kalshi", 20.0, 0.5),
          ("tok-p11-0", 0.01, "kalshi", 10.0, 0.5)],
    "d": [("tok-p11-0", 0.01, "kalshi", 10.0, 0.5)],
    "e": [("tok-p01-0", 0.02, "kalshi", 20.0, 0.5), ("tok-p02-0", 0.01, "kalshi", 10.0, 0.56),
          ("tok-p03-0", 0.01, "polymarket_us", 10.0, 0.44), ("tok-p04-0", 0.02, "kalshi", 20.0, 0.5),
          ("tok-p06-0", 0.01, "kalshi", 8.02, 0.3), ("tok-p07-0", 0.01, "kalshi", 10.0, 0.8),
          ("tok-p08-0", 0.01, "prophetx", 10.0, 0.6), ("tok-p10-0", 0.02, "kalshi", 20.0, 0.5),
          ("tok-p11-0", 0.01, "kalshi", 10.0, 0.5), ("tok-p15-0", 0.01, "kalshi", 10.0, 0.605)],
    "claude": [("tok-p01-0", 0.02, "kalshi", 20.0, 0.5), ("tok-p02-0", 0.02, "kalshi", 20.0, 0.56),
               ("tok-p03-0", 0.02, "polymarket_us", 20.0, 0.44), ("tok-p04-0", 0.02, "kalshi", 20.0, 0.5),
               ("tok-p06-0", 0.02, "kalshi", 16.04, 0.3), ("tok-p07-0", 0.02, "kalshi", 20.0, 0.8),
               ("tok-p08-0", 0.02, "prophetx", 20.0, 0.6), ("tok-p10-0", 0.02, "kalshi", 20.0, 0.5),
               ("tok-p11-0", 0.02, "kalshi", 20.0, 0.5), ("tok-p15-0", 0.02, "kalshi", 20.0, 0.605)],
}


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_existing_strategy_decides_as_before(world, key):
    picks = scenario.build(world)
    state = run_strategy(key, new_state(), picks)
    assert scenario.decisions(state) == EXPECTED[key]


def test_existing_strategy_settings_untouched():
    """The older strategies have no price allowance of their own: they use the shared 1c rule."""
    assert ft.MAX_ABOVE == 0.01 and ft.MAX_BELOW == 0.04 and ft.A_MAX_PRICE == 0.75
    assert set(ft.STRATEGIES) == set(EXPECTED) | {"chatgpt"}
    for key in EXPECTED:
        assert "max_above" not in ft.STRATEGIES[key]
    assert ft.in_band(0.56, 0.55) and not ft.in_band(0.571, 0.55)  # 1c above entry still allowed by default
