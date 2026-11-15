"""Strategy ChatGPT's rules, one test per rule (numbers match the owner's list of 18).
All data is fake: no network, no keys, no orders."""
import pytest

import forward_test as ft
from conftest import make_pick, new_state, run_strategy


def bets(state):
    return [b["token_id"] for b in state["bets"]]


def go(world, state=None):
    return run_strategy("chatgpt", state or new_state(), world.picks)


def test_settings():
    cfg = ft.STRATEGIES["chatgpt"]
    assert cfg["tiers"] == [(0.01, [(3, 10)])]          # flat 1%: 3+ bettors and 10%+ conviction
    assert cfg["max_price"] == 0.75 and cfg["max_above"] == 0.0
    assert cfg["state"] == "paper_trades_chatgpt.json" and cfg["csv"] == "paper_trades_chatgpt.csv"
    assert not cfg.get("net") and not cfg.get("window") and not cfg.get("count") and not cfg.get("claude")


def test_qualifying_pick_is_bet(world):
    world.add(make_pick("ok", bettors=3, pct=10.0, against=2, entry=0.50), ("kalshi", 0.50))
    st = go(world)
    assert bets(st) == ["tok-ok-0"]
    b = st["bets"][0]
    assert b["stake"] == 0.01 and b["venue"] == "kalshi" and b["avg_price"] == 0.50
    assert b["bettors"] == 3 and b["avg_bankroll_pct"] == 10.0 and b["traders_entry"] == 0.50


def test_01_fewer_than_3_bettors_rejected(world):
    world.add(make_pick("x", bettors=2, pct=30))
    assert bets(go(world)) == []


def test_02_under_10_percent_conviction_rejected(world):
    world.add(make_pick("x", bettors=6, pct=9.9))
    assert bets(go(world)) == []


def test_no_upper_conviction_limit(world):
    world.add(make_pick("x", bettors=3, pct=80))
    assert bets(go(world)) == ["tok-x-0"]


def test_03_tie_rejected(world):
    world.add(make_pick("x", bettors=4, against=4, pct=20))
    assert bets(go(world)) == []


def test_04_minority_rejected(world):
    world.add(make_pick("x", bettors=4, against=6, pct=20))
    assert bets(go(world)) == []


def test_05_price_above_entry_rejected(world):
    world.add(make_pick("x", entry=0.50), ("kalshi", 0.505))   # half a cent over
    world.add(make_pick("y", entry=0.50), ("kalshi", 0.51))    # the 1c Strategy A allows
    assert bets(go(world)) == []
    assert bets(run_strategy("a", new_state(), world.picks)) == ["tok-x-0", "tok-y-0"]  # A still takes both


def test_price_more_than_4c_below_entry_rejected(world):
    world.add(make_pick("x", entry=0.50), ("kalshi", 0.455))
    world.add(make_pick("y", entry=0.50), ("kalshi", 0.46))
    assert bets(go(world)) == ["tok-y-0"]


def test_06_price_under_floor_rejected(world):
    world.add(make_pick("x", entry=0.29), ("kalshi", 0.28))    # under 28.6c (+250)
    world.add(make_pick("y", entry=0.29), ("kalshi", 0.29))
    assert bets(go(world)) == ["tok-y-0"]


def test_07_price_over_75c_rejected(world):
    world.add(make_pick("x", entry=0.77), ("kalshi", 0.76))
    world.add(make_pick("y", entry=0.77), ("kalshi", 0.75))
    assert bets(go(world)) == ["tok-y-0"]


def test_08_started_game_rejected(world):
    world.add(make_pick("x", starts_in_hours=-0.1))
    assert bets(go(world)) == []


def test_09_game_over_3_days_away_rejected(world):
    world.add(make_pick("x", starts_in_hours=73))
    world.add(make_pick("y", starts_in_hours=71))
    assert bets(go(world)) == ["tok-y-0"]


def test_10_held_position_not_bought_again(world):
    world.add(make_pick("x"))
    st = go(world)
    st = go(world, st)
    assert bets(st) == ["tok-x-0"] and round(st["bets"][0]["cost"], 2) == 10.0


def test_11_never_both_sides_of_a_market(world):
    world.add(make_pick("x", side=0, bettors=5, against=2))
    st = go(world)
    world.picks.clear()
    world.add(make_pick("x", side=1, bettors=7, against=5))   # the other outcome of the same market, later
    st = go(world, st)
    assert bets(st) == ["tok-x-0"]


def test_12_one_side_bet_and_one_total_per_game(world):
    game = "nba-aaa-bbb-2026-12-01"
    world.add(make_pick("ml", game=game, market_type="moneyline"))
    world.add(make_pick("sp", game=game, market_type="spreads"))
    world.add(make_pick("t1", game=game, market_type="totals"))
    world.add(make_pick("t2", game=game, market_type="totals"))
    assert bets(go(world)) == ["tok-ml-0", "tok-t1-0"]


def test_13_no_liquidity_skipped(world):
    world.unlisted(make_pick("x"))
    assert bets(go(world)) == []


def test_cheapest_venue_that_passes_wins(world):
    world.add(make_pick("x", entry=0.50), ("kalshi", 0.52), ("prophetx", 0.50), ("polymarket_us", 0.49))
    st = go(world)
    assert st["bets"][0]["venue"] == "polymarket_us" and st["bets"][0]["avg_price"] == 0.49
    assert set(st["bets"][0]["quotes"]) == {"kalshi", "prophetx", "polymarket_us"}


def test_14_one_percent_stake_and_underdog_multiplier(world):
    world.add(make_pick("fav", entry=0.50), ("kalshi", 0.50))
    world.add(make_pick("dog", entry=0.30, now=0.30), ("kalshi", 0.30))
    st = go(world)
    by = {b["token_id"]: b for b in st["bets"]}
    assert round(by["tok-fav-0"]["cost"], 2) == 10.00                  # 1% of $1,000
    mult = ((0.30 / 0.70) / (0.40 / 0.60)) ** 0.5                      # 0.80 at 30c
    assert by["tok-dog-0"]["size_multiplier"] == round(mult, 3) == 0.802
    assert round(by["tok-dog-0"]["cost"], 2) == round(1000 * 0.01 * mult, 2) == 8.02
    assert all(b["stake"] == 0.01 for b in st["bets"])


def test_15_fifty_percent_exposure_cap(world):
    st = new_state()
    st["cash"] = 505.0
    st["bets"].append({"status": "open", "cost": 495.0, "token_id": "old", "condition_id": "old-cond",
                       "link": "https://polymarket.com/event/nba-old-old-2026-11-30", "market": "old",
                       "market_type": "moneyline", "stake": 0.01})
    world.add(make_pick("x"))                # $10 more would make $505 of $1,000 open
    assert bets(go(world, st)) == ["old"]
    st["bets"][0]["cost"], st["cash"] = 480.0, 520.0
    assert bets(go(world, st)) == ["old", "tok-x-0"]


def test_16_no_top_ups(world):
    world.add(make_pick("x", bettors=3, pct=12))
    st = go(world)
    world.picks[0].update({"Bettors": 9, "Avg bankroll %": 60})      # now meets every other strategy's 2% tier
    st = go(world, st)
    b = st["bets"][0]
    assert len(st["bets"]) == 1 and b["stake"] == 0.01 and round(b["cost"], 2) == 10.0
    assert b["topped_up_at"] is None and world.top_up_calls == 0


def test_17_never_reaches_live_trading(world, main_env):
    """Run the whole of forward_test.main() with fakes and look at what is handed to live.run."""
    world.add(make_pick("both", bettors=5, pct=25, entry=0.50), ("kalshi", 0.50))    # A and ChatGPT both bet
    world.add(make_pick("gptonly", bettors=3, pct=12, entry=0.50), ("kalshi", 0.50))  # only ChatGPT bets
    main_env.run()
    gpt, a = main_env.state("chatgpt"), main_env.state("a")
    assert sorted(bets(gpt)) == ["tok-both-0", "tok-gptonly-0"]       # ChatGPT did bet on paper...
    assert bets(a) == ["tok-both-0"]
    seen = main_env.seen
    assert [i["bet"]["token_id"] for i in seen["intents"]] == ["tok-both-0"]   # ...but only A's bet goes to live
    assert seen["intents"][0]["bet"]["placed_at"] == a["bets"][0]["placed_at"]  # and it is A's own bet record
    assert [b["token_id"] for b in seen["paper_bets"]] == ["tok-both-0"]
    assert gpt["cash"] == pytest.approx(980.0) and a["cash"] == pytest.approx(980.0)  # separate $1,000 bankrolls
