"""Strategy ChatGPT must never get in the way of the existing strategies or Strategy A's real bets,
must keep its bankroll and bets from run to run, and must not add work nobody asked for.
Everything is fake: no network, no keys, no orders. main() here writes the REAL report page too."""
import os
import re

import pytest

import forward_test as ft
import venues
from conftest import make_pick

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OTHERS = ["a", "b", "c", "d", "e", "claude"]


def two_picks(world):
    world.add(make_pick("both", bettors=5, pct=25, entry=0.50), ("kalshi", 0.50))     # A, B, C, E and ChatGPT
    world.add(make_pick("gptonly", bettors=3, pct=12, entry=0.50), ("kalshi", 0.50))   # ChatGPT (and E)


def tokens(state):
    return sorted(b["token_id"] for b in state["bets"])


def test_chatgpt_runs_only_after_real_bets_are_placed(world, main_env):
    two_picks(world)
    main_env.run()
    files = main_env.seen["files_at_live_run"]            # what existed on disk when live.run was called
    assert "paper_trades.json" in files and "paper_trades_e.json" in files
    assert "paper_trades_chatgpt.json" not in files       # ChatGPT had not even been loaded yet
    assert tokens(main_env.state("chatgpt")) == ["tok-both-0", "tok-gptonly-0"]   # and afterwards it did trade


@pytest.mark.parametrize("where", ["trade", "settle", "quotes", "corrupt_file"])
def test_chatgpt_error_cannot_stop_the_others_or_real_bets(world, main_env, monkeypatch, where):
    two_picks(world)
    real_trade, real_settle, real_quotes = ft.trade, ft.settle, venues.quotes
    is_gpt = lambda tiers: tiers == ft.STRATEGIES["chatgpt"]["tiers"]
    if where == "trade":
        monkeypatch.setattr(ft, "trade", lambda state, picks, tiers, **k: (_ for _ in ()).throw(
            RuntimeError("boom in ChatGPT")) if is_gpt(tiers) else real_trade(state, picks, tiers, **k))
    elif where == "settle":
        def settle(state, real=False):
            if main_env.seen.get("live_runs") and not real:   # the only paper settle after live.run is ChatGPT's
                raise RuntimeError("boom in ChatGPT settle")
            return real_settle(state, real)
        monkeypatch.setattr(ft, "settle", settle)
    elif where == "quotes":
        def quotes(pick, info, dollars):
            if main_env.seen.get("live_runs"):
                raise RuntimeError("venue lookups failing for ChatGPT")
            return real_quotes(pick, info, dollars)
        monkeypatch.setattr(venues, "quotes", quotes)
    else:
        with open(main_env.path("chatgpt"), "w") as f:
            f.write("{ this is not valid json")
    main_env.run()                                            # must not raise
    for key in OTHERS:
        assert os.path.exists(main_env.path(key))             # every existing strategy finished and saved
    assert tokens(main_env.state("a")) == ["tok-both-0"]
    assert tokens(main_env.state("b")) == ["tok-both-0"]
    assert main_env.seen["live_runs"] == 1                    # real-money step ran, with A's bet
    assert [i["bet"]["token_id"] for i in main_env.seen["intents"]] == ["tok-both-0"]
    assert os.path.exists(os.path.join(main_env.folder, "paper_report.html"))   # report still written
    assert "Strategy ChatGPT error" in main_env.output
    if where == "corrupt_file":                               # an unreadable file is never overwritten
        with open(main_env.path("chatgpt")) as f:
            assert f.read() == "{ this is not valid json"
        assert not os.path.exists(os.path.join(main_env.folder, "paper_trades_chatgpt.csv"))
    else:
        assert tokens(main_env.state("chatgpt")) == []        # it placed nothing, bankroll intact
        assert main_env.state("chatgpt")["cash"] == 1000.0


def test_bankroll_and_history_persist_across_runs(world, main_env):
    two_picks(world)
    assert not os.path.exists(main_env.path("chatgpt"))       # first run ever: no file yet
    main_env.run()
    first = main_env.state("chatgpt")
    assert first["start_bankroll"] == 1000.0 and first["cash"] == pytest.approx(980.0)
    assert tokens(first) == ["tok-both-0", "tok-gptonly-0"]
    with open(os.path.join(main_env.folder, "paper_trades_chatgpt.csv")) as f:
        assert len(f.read().strip().splitlines()) == 3        # header + 2 bets

    world.add(make_pick("later", bettors=4, pct=11, entry=0.60), ("kalshi", 0.60))
    main_env.run()                                            # second run: loads the file, no repeats
    second = main_env.state("chatgpt")
    assert second["started_at"] == first["started_at"]        # same bankroll, not a fresh $1,000
    assert tokens(second) == ["tok-both-0", "tok-gptonly-0", "tok-later-0"]
    assert second["cash"] == pytest.approx(970.0)
    assert ft.bankroll(second) == pytest.approx(1000.0)
    old = {b["token_id"]: b for b in first["bets"]}
    for b in second["bets"]:
        if b["token_id"] in old:
            assert b == old[b["token_id"]]                    # earlier bets are untouched
    assert tokens(main_env.state("a")) == ["tok-both-0"]      # A did not repeat its bet either


def test_report_has_a_chatgpt_tab(world, main_env):
    two_picks(world)
    main_env.run()
    with open(os.path.join(main_env.folder, "paper_report.html")) as f:
        page = f.read()
    assert re.findall(r'data-tab="(\w+)"', page) == ["cal", "real", "a", "b", "c", "d", "e", "claude", "chatgpt",
                                                     "stats"]
    assert 'id="tab-chatgpt"' in page and "Strategy ChatGPT" in page


def test_no_extra_scanner_work_and_only_needed_venue_lookups(world, main_env, monkeypatch):
    two_picks(world)
    world.add(make_pick("lowconv", bettors=6, pct=5, entry=0.50), ("kalshi", 0.50))   # B bets it; ChatGPT must not look
    scans, lookups = [], []
    real_find, real_quotes = ft.scanner.find_picks, venues.quotes
    monkeypatch.setattr(ft.scanner, "find_picks", lambda args: scans.append(1) or real_find(args))
    monkeypatch.setattr(venues, "quotes", lambda pick, info, dollars: lookups.append(
        (bool(main_env.seen.get("live_runs")), pick["token_id"])) or real_quotes(pick, info, dollars))
    main_env.run()
    assert len(scans) == 1                                    # one scan a run, shared by every strategy
    gpt = sorted(tok for after_live, tok in lookups if after_live)
    assert gpt == ["tok-both-0", "tok-gptonly-0"]             # one lookup per pick it could bet, none for the rest
    lookups.clear()
    main_env.run()
    assert [tok for after_live, tok in lookups if after_live] == []   # already held: no lookups at all


def test_workflow_saves_chatgpt_files_and_runs_no_research():
    with open(os.path.join(HERE, ".github", "workflows", "hourly.yml")) as f:
        yml = f.read()
    assert "git add paper_trades_chatgpt.json paper_trades_chatgpt.csv || true" in yml
    assert yml.count("run: python") == 1 and "run: python -u forward_test.py" in yml   # the only script it runs
    assert "research_chatgpt" not in yml and "pytest" not in yml
    assert 'cron: "3,13,23,33,43,53 * * * *"' in yml                         # schedule unchanged
    for name in ("forward_test.py", "live.py", "venues.py", "pick_log.py", "claude_strategy.py",
                 "polymarket_smart_money.py"):
        with open(os.path.join(HERE, name)) as f:
            assert "research_chatgpt" not in f.read()                         # nothing imports or runs it
