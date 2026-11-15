"""Research logging: it must change nothing about trading, and what it writes must be honest.
All fake data, network blocked."""
import csv
import hashlib
import os
from datetime import datetime, timedelta, timezone

import pytest

import forward_test as ft
import research_log as rl
import scenario
from conftest import make_pick, new_state, run_strategy

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
W1, W2 = "0xaaa", "0xbbb"


def read(folder, kind, day=None):
    """Rows of research/<kind>.csv, or of every hourly file of one UTC day (or of all days)."""
    base = os.path.join(folder, "research")
    if os.path.exists(os.path.join(base, kind + ".csv")):
        paths = [os.path.join(base, kind + ".csv")]
    else:
        paths = sorted(os.path.join(root, f) for root, _, files in os.walk(os.path.join(base, kind))
                       for f in files if day is None or os.path.basename(root) == day)
    out = []
    for path in paths:
        with open(path, newline="") as f:
            out += list(csv.DictReader(f))
    return out


def read_all(folder, kind):
    return read(folder, kind)


def pos(token, shares=100.0, cost=50.0, price=0.5, cond=None):
    return {"token_id": token, "condition_id": cond or f"c-{token}", "event_slug": f"nba-{token}-2026-11-21",
            "outcome": "Yes", "current_size": shares, "entry_cost_usdc": cost, "avg_price": cost / shares,
            "current_value": shares * price, "current_price": price, "first_entry_at": "2026-11-19T10:00:00Z"}


def make_obs(positions, status=None, trades=None, start_in_hours=24, hedged=None):
    """positions: {wallet: [position, ...]}. Every market is a game starting `start_in_hours` from NOW."""
    start = (NOW + timedelta(hours=start_in_hours)).isoformat()
    markets = {p["condition_id"]: {"start": start} for ps in positions.values() for p in ps}
    traders = {w: {"name": w, "rank": i, "status": (status or {}).get(w, "scanned"), "bankroll": 1000.0,
                   "hedged": set((hedged or {}).get(w, [])), "raw_positions": len(ps)}
               for i, (w, ps) in enumerate(positions.items(), 1)}
    return {"card": {"markets": markets}, "positions": {w: (w, ps) for w, ps in positions.items()
                                                        if traders[w]["status"] == "scanned"},
            "scan": {"board": [{"user_id": w, "user_name": w, "rank": i, "pnl": 10, "volume": 20}
                               for i, w in enumerate(positions, 1)], "traders": traders, "trades": trades or {}},
            "picks": [], "wide_picks": [], "decisions": [], "rules": {}, "graded": [], "history": {}}


def step(folder, obs, minutes):
    rl.run(folder, obs, now=NOW + timedelta(minutes=minutes))


# ---------- it must not change trading ----------

def snapshot_of_decisions(env):
    out = {}
    for key in ft.STRATEGIES:
        st = env.state(key)
        out[key] = scenario.decisions(st) + [("cash", round(st["cash"], 2))]
    out["real"] = [(i["bet"]["token_id"], i["top_up"], i["entry"]) for i in env.seen["intents"]]
    return out


@pytest.mark.parametrize("runs", [1, 2])
def test_08_identical_decisions_with_and_without_research_logging(world, main_env, monkeypatch, tmp_path, runs):
    scenario.build(world)
    results = {}
    for mode in ("off", "on"):
        folder = tmp_path / mode
        folder.mkdir()
        monkeypatch.setattr(ft, "HERE", str(folder))
        main_env.folder = str(folder)
        monkeypatch.setenv("RESEARCH_LOG", mode)
        for _ in range(runs):
            main_env.run()
        results[mode] = snapshot_of_decisions(main_env)
    assert results["on"] == results["off"]
    assert len(results["on"]["a"]) > 1                                 # and there really were bets to compare
    assert results["on"]["real"] or runs == 2                          # (a second run has no new real bets)
    assert not os.path.exists(tmp_path / "off" / "research")           # off: nothing written
    assert read_all(str(tmp_path / "on"), "decisions")                 # on: decisions were logged


def test_notes_do_not_change_what_trade_decides(world):
    """The notes are collected inside trade(); the recorded pre-change decisions must still hold."""
    import test_existing_unchanged as golden
    picks = scenario.build(world)
    for key, expected in golden.EXPECTED.items():
        ft._NOTE["strategy"] = key
        assert scenario.decisions(run_strategy(key, new_state(), picks)) == expected
    codes = {d["code"] for d in ft.DECISIONS}   # (no_tier is noted but not written: it follows from the counts)
    assert {"bet", "no_tier", "price_outside_band", "minority_or_tie", "game_slot_taken", "no_venue"} <= codes


def test_06_research_error_cannot_stop_or_change_anything(world, main_env, monkeypatch):
    scenario.build(world)
    monkeypatch.setattr(rl, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("research broke")))
    main_env.run()
    assert "Research log error: RuntimeError" in main_env.output
    assert main_env.seen["live_runs"] == 1 and main_env.state("a")["bets"] and main_env.state("chatgpt")["bets"]
    assert os.path.exists(os.path.join(main_env.folder, "paper_report.html"))


def test_06_research_runs_last_and_05_only_writes_inside_research(world, main_env, monkeypatch):
    scenario.build(world)
    real_run, seen = rl.run, {}

    def digest():
        out = {}
        for root, dirs, files in os.walk(main_env.folder):
            if os.path.basename(root) == "research" or os.sep + "research" + os.sep in root + os.sep:
                continue
            for name in files:
                with open(os.path.join(root, name), "rb") as f:
                    out[os.path.relpath(os.path.join(root, name), main_env.folder)] = hashlib.sha1(f.read()).hexdigest()
        return out

    def wrapped(here, obs, now=None):
        seen["before"] = digest()
        result = real_run(here, obs, now)
        seen["after"] = digest()
        return result

    monkeypatch.setattr(rl, "run", wrapped)
    main_env.run()
    assert seen["before"] == seen["after"]                             # no trading file touched or created
    for name in ("paper_trades.json", "paper_trades_chatgpt.json", "live_trades.json", "paper_report.html",
                 "paper_trades.csv", "live_log.json"):
        assert name in seen["before"]                                  # all saved and reported BEFORE research ran
    assert main_env.seen["live_runs"] == 1
    assert os.path.exists(os.path.join(main_env.folder, "research", "runs.csv"))


def test_07_research_makes_no_web_requests(world, main_env):
    """Every test runs with the network blocked (conftest.no_network): a request would raise and show
    up as a note in runs.csv."""
    scenario.build(world)
    main_env.run()
    runs = read(main_env.folder, "runs")
    assert len(runs) == 1 and "Error" not in runs[0]["notes"] and "network" not in runs[0]["notes"]
    assert float(runs[0]["run_seconds"]) >= 0 and int(runs[0]["research_bytes"]) > 0


# ---------- 3. decision log ----------

def test_decisions_logged_once_then_only_on_change(world, main_env):
    world.add(make_pick("ok", bettors=5, pct=25, entry=0.50), ("kalshi", 0.50))
    world.add(make_pick("dear", bettors=5, pct=25, entry=0.50), ("kalshi", 0.53))
    main_env.run()
    rows = read_all(main_env.folder, "decisions")
    a = {r["pick"]: r for r in rows if r["strategy"] == "a"}
    assert a["ok-side0"]["code"] == "bet" and a["ok-side0"]["venue"] == "kalshi"
    assert a["dear-side0"]["code"] == "price_outside_band" and a["dear-side0"]["quotes"].startswith("kalshi:0.53:")
    assert {r["strategy"] for r in rows} == set(ft.STRATEGIES) - {"claude"}   # every strategy that saw the picks
    assert all(len(r["rules"]) == 8 and r["observed_at"].endswith("Z") for r in rows)
    n = len(rows)
    main_env.run()                                                    # nothing changed except "bet" -> "already_held"
    rows = read_all(main_env.folder, "decisions")
    new = rows[n:]
    assert {r["code"] for r in new} == {"already_held"}
    main_env.run()
    assert len(read_all(main_env.folder, "decisions")) == len(rows)   # third run: no change, no rows
    world.prices["tok-dear-0"] = [("kalshi", 0.55)]                   # the venue price moves 2c: logged again
    main_env.run()
    latest = read_all(main_env.folder, "decisions")[len(rows):]
    assert latest and all(r["code"] == "price_outside_band" and "0.55" in r["quotes"] for r in latest)


def test_rule_versions_recorded_and_change_when_rules_change(tmp_path):
    obs = make_obs({})
    obs["rules"] = {"a": {"settings": {"tiers": [[0.01, [[3, 20]]]]}}, "b": {"settings": {"tiers": [1]}}}
    step(str(tmp_path), obs, 0)
    step(str(tmp_path), obs, 10)
    assert [r["strategy"] for r in read(str(tmp_path), "rules")] == ["a", "b"]     # once, not every run
    obs["rules"]["a"]["settings"]["tiers"] = [[0.02, [[3, 20]]]]
    step(str(tmp_path), obs, 20)
    rows = read(str(tmp_path), "rules")
    assert [r["strategy"] for r in rows] == ["a", "b", "a"] and rows[0]["rules"] != rows[2]["rules"]


def test_real_rules_snapshot_covers_every_strategy_and_real_bets():
    snap = ft.rules_snapshot()
    assert set(snap) == set(ft.STRATEGIES) | {"real"}
    assert snap["chatgpt"]["settings"]["max_above"] == 0.0 and snap["real"]["flat_share"] == 0.01


# ---------- 2. position changes ----------

def test_position_changes_are_shares_or_cost_not_price(tmp_path):
    f = str(tmp_path)
    step(f, make_obs({W1: [pos("t1", shares=100, cost=50, price=0.50)]}), 0)
    step(f, make_obs({W1: [pos("t1", shares=100, cost=50, price=0.60)]}), 10)     # only the price moved
    step(f, make_obs({W1: [pos("t1", shares=150, cost=80, price=0.60)]}), 20)     # bought more
    step(f, make_obs({W1: [pos("t1", shares=40, cost=21, price=0.60)]}), 30)      # reduced
    rows = read_all(f, "positions")
    assert [r["change"] for r in rows] == ["first_seen", "shares_up", "shares_down"]
    assert rows[1]["shares_delta"] == "50.0" and rows[1]["cost_delta"] == "30.0"
    assert rows[1]["prev_observed_at"] == "2026-11-20T15:10:00Z"   # the last look before the change
    assert rows[1]["observed_at"] == "2026-11-20T15:20:00Z" and rows[0]["prev_observed_at"] == ""
    market = read_all(f, "markets")
    assert len(market) == 1 and market[0]["m"] == rows[0]["m"] and market[0]["token_id"] == "t1"   # code -> market
    assert market[0]["game"] == "nba-t1-2026-11-21" and market[0]["condition_id"] == "c-t1"
    assert rows[0]["position_first_entry_at"] == "2026-11-19T10:00:00Z" and rows[0]["pct_of_open_value"] == "5.0"


def test_02_missing_is_never_called_a_sale(tmp_path):
    f = str(tmp_path)
    both = {W1: [pos("t1")], W2: [pos("t2")]}
    step(f, make_obs(both), 0)
    # W1 could not be scanned this run; W2 was scanned and t2 is no longer in the list
    step(f, make_obs({W1: [pos("t1")], W2: []}, status={W1: "error"}), 10)
    rows = read_all(f, "positions")[2:]
    assert [(r["w"], r["change"]) for r in rows] == [(W2, "not_observed")]   # nothing concluded about W1
    assert rows[0]["not_observed_reason"] == "not_in_positions_over_min_value" and rows[0]["last_value"] == "50.0"
    assert not any("sold" in v.lower() for r in read_all(f, "positions") for v in r.values())
    board = read_all(f, "leaderboard")
    assert [(r["wallet"], r["status"], r["reason"]) for r in board[2:]] == [(W1, "error", "status_change")]
    step(f, make_obs(both), 20)                                                   # both back
    later = read_all(f, "positions")[3:]
    assert [(r["w"], r["change"]) for r in later] == [(W2, "reappeared")]    # W1: unchanged, so no row
    assert later[0]["prev_observed_at"] == "2026-11-20T15:10:00Z"
    assert read_all(f, "leaderboard")[-1]["status"] == "scanned"


def test_hedged_drop_and_started_games_and_far_games(tmp_path):
    f = str(tmp_path)
    step(f, make_obs({W1: [pos("t1", cond="c1")]}), 0)
    step(f, make_obs({W1: []}, hedged={W1: ["c1"]}), 10)
    assert read_all(f, "positions")[-1]["not_observed_reason"] == "market_dropped_as_hedged"
    g = str(tmp_path / "far")
    step(g, make_obs({W1: [pos("t9")]}, start_in_hours=100), 0)                   # over 3 days away: not tracked
    assert read_all(g, "positions") == []
    h = str(tmp_path / "started")
    step(h, make_obs({W1: [pos("t5")]}, start_in_hours=1), 0)
    step(h, make_obs({W1: []}, start_in_hours=1), 90)                             # game has started by now
    assert [r["change"] for r in read_all(h, "positions")] == ["first_seen"]      # no "not_observed" after the start


def test_01_trade_times_only_with_transaction_evidence(tmp_path):
    f = str(tmp_path)
    at = lambda minutes: int((NOW + timedelta(minutes=minutes)).timestamp())
    trades = {W1: [{"token_id": "t1", "timestamp": at(14), "side": "BUY", "size": 30},
                   {"token_id": "t1", "timestamp": at(17), "side": "BUY", "size": 20},
                   {"token_id": "t1", "timestamp": at(-500), "side": "BUY", "size": 100},   # before the gap
                   {"token_id": "other", "timestamp": at(15), "side": "BUY", "size": 999}]}
    step(f, make_obs({W1: [pos("t1", 100, 50)], W2: [pos("t2", 100, 50)]}), 10)
    step(f, make_obs({W1: [pos("t1", 150, 80)], W2: [pos("t2", 150, 80)]},
                     trades={**trades, W2: [{"token_id": "zzz", "timestamp": at(15), "side": "BUY", "size": 1}]}), 20)
    w1, w2 = read_all(f, "positions")[2:]
    assert (w1["trade_evidence"], w1["trades_in_gap"], w1["trade_shares"]) == ("confirmed", "2", "50.0")
    assert w1["trade_first_at"] == "2026-11-20T15:14:00Z" and w1["trade_last_at"] == "2026-11-20T15:17:00Z"
    assert (w2["trade_evidence"], w2["trade_first_at"]) == ("none_in_list", "")   # change seen, no time claimed
    step(f, make_obs({W1: [pos("t1", 170, 90)]}, trades={W1: [{"title": "no market id here", "timestamp": at(25)}]}), 30)
    assert read_all(f, "positions")[-1]["trade_evidence"] == "unavailable"


# ---------- 1. graded log and 4. no future leakage ----------

def graded(wallet, game, clv, at, token="t", close=0.5):
    return {"wallet": wallet, "token_id": token, "name": wallet, "game": game, "start": at, "first_seen": at,
            "entry": close - clv, "close": close, "clv": clv, "close_source": "price_history", "graded_at": at,
            "market": "m", "pick": "p"}


def test_graded_log_keeps_wallet_and_market(tmp_path):
    obs = make_obs({})
    obs["graded"] = [graded(W1, "nba-aaa-bbb-2026-11-20", 0.02, "2026-11-20T14:00:00+00:00", token="tok1"),
                     {**graded(W1, "nfl-x-y-2026-11-20-more-markets", 0, "2026-11-20T14:00:00+00:00"),
                      "close": None, "clv": None, "close_source": "none"}]
    step(str(tmp_path), obs, 0)
    rows = read_all(str(tmp_path), "graded")
    assert (rows[0]["wallet"], rows[0]["token_id"], rows[0]["sport"], rows[0]["clv"]) == (W1, "tok1", "nba", "0.02")
    assert (rows[1]["clv"], rows[1]["close_source"], rows[1]["game"]) == ("", "none", "nfl-x-y-2026-11-20")


def test_04_trader_record_uses_only_what_was_known_then():
    rows = rl.graded_rows([
        graded(W1, "nba-g1", 0.04, "2026-11-10T01:00:00+00:00", "a"),
        graded(W1, "nba-g1", 0.02, "2026-11-10T01:00:00+00:00", "b"),     # same game: counts once (avg 0.03)
        graded(W1, "nfl-g2", 0.01, "2026-11-11T01:00:00+00:00"),
        graded(W1, "nba-g3", 0.50, "2026-11-15T01:00:00+00:00"),          # graded AFTER the decision below
        graded(W2, "nba-g1", 0.99, "2026-11-10T01:00:00+00:00")])
    then = rl.record_as_of(rows, W1, "2026-11-12T00:00:00+00:00")
    assert then["games"] == 2 and then["avg_clv"] == pytest.approx(0.02) and then["days"] == 2
    assert rl.record_as_of(rows, W1, "2026-11-12T00:00:00+00:00", sport="nba")["avg_clv"] == pytest.approx(0.03)
    assert rl.record_as_of(rows, W1, "2026-11-20T00:00:00+00:00")["games"] == 3    # later, the new game counts
    assert rl.record_as_of(rows, W1, "2026-11-10T01:00:00+00:00")["games"] == 0    # not yet known at that moment


# ---------- 5. snapshots, 4. price paths, 6. leaderboard ----------

def test_snapshots_written_only_when_something_changes(tmp_path):
    f = str(tmp_path)
    p = make_pick("x", bettors=4, pct=15.0, against=1, entry=0.50)
    obs = make_obs({})
    obs["card"] = {"markets": {p["condition_id"]: {"start": (NOW + timedelta(hours=30)).isoformat()}}}
    obs["picks"], obs["top_wallets"], obs["sides"] = [p], set(), {}
    step(f, obs, 0)
    step(f, obs, 10)                                         # identical: no row
    p["Price now"] = 0.505
    step(f, obs, 20)                                         # half a cent: no row
    p["Price now"] = 0.52
    step(f, obs, 30)                                         # two cents: row
    p["Bettors"] = 5
    step(f, obs, 40)                                         # a count changed: row
    rows = read_all(f, "snapshots")
    assert [(r["reason"], r["bettors"], r["price"]) for r in rows] == [
        ("first_seen", "4", "0.5"), ("price", "4", "0.52"), ("counts", "5", "0.52")]
    assert rows[0]["scope"] == "top100" and rows[0]["game"] == "nba-x-2026-12-01" and rows[0]["against"] == "1"
    assert rows[1]["m"] == rows[0]["m"] and rows[0]["token_id"] == "tok-x-0" and rows[1]["token_id"] == ""
    obs["card"]["markets"] = {}
    step(f, obs, 50)                                         # start time unknown: skipped and said so
    assert "without a known game start" in read(f, "runs")[-1]["notes"]


def test_price_path_samples_before_the_start_once_per_market(tmp_path):
    f = str(tmp_path)
    start = int((NOW - timedelta(minutes=5)).timestamp())
    points = [(start - 60 * m, round(0.40 + 0.001 * (360 - m), 3)) for m in range(360, -1, -1)]   # one a minute
    obs = make_obs({})
    obs["history"] = {"tokA": (start, points), "tokB": (start, [])}
    step(f, obs, 0)
    step(f, obs, 10)                                         # same markets again: not repeated
    rows = read_all(f, "price_paths")
    assert [r["token_id"] for r in rows] == ["tokA", "tokB"]
    assert (rows[0]["p_360m"], rows[0]["p_60m"], rows[0]["p_5m"], rows[0]["close"]) == ("0.4", "0.7", "0.755", "0.76")
    assert rows[0]["points"] == "361" and rows[1]["points"] == "0" and rows[1]["close"] == ""   # missing stays blank


def test_leaderboard_once_a_day_then_changes_only(tmp_path):
    f = str(tmp_path)
    step(f, make_obs({W1: [], W2: []}), 0)
    step(f, make_obs({W1: [], W2: []}), 10)
    assert [r["reason"] for r in read_all(f, "leaderboard")] == ["daily", "daily"]
    step(f, make_obs({W1: []}), 20)                          # W2 fell off the board
    assert [(r["wallet"], r["status"]) for r in read_all(f, "leaderboard")[2:]] == [(W2, "off_board")]
    step(f, make_obs({W1: []}), 60 * 24)                     # next UTC day: full list again
    assert [r["reason"] for r in read(f, "leaderboard", "2026-11-21")] == ["daily"]
    obs = make_obs({W1: []})
    obs["scan"]["board"] = []                                # leaderboard missing this run: conclude nothing
    step(f, obs, 60 * 24 + 10)
    assert len(read(f, "leaderboard", "2026-11-21")) == 1


def test_one_broken_log_does_not_lose_the_others_and_bad_state_restarts(tmp_path):
    f = str(tmp_path)
    obs = make_obs({W1: [pos("t1")]})
    obs["graded"] = [{"wallet": W1}]                         # malformed: the graded log will fail
    step(f, obs, 0)
    assert read_all(f, "positions") and "graded:" in read(f, "runs")[0]["notes"]
    with open(os.path.join(f, "research", "state.json"), "w") as fh:
        fh.write("not json")
    step(f, make_obs({W1: [pos("t1")]}), 10)
    assert "state reset" in read(f, "runs")[1]["notes"] and len(read(f, "runs")) == 2   # history kept, said so


def test_07_workflow_gains_one_tolerant_line_and_nothing_else():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, ".github", "workflows", "hourly.yml")) as f:
        yml = f.read()
    assert "git add research || true" in yml                       # can't block saving the trading results
    assert yml.count("run: python") == 1 and 'cron: "3,13,23,33,43,53 * * * *"' in yml and yml.count("cron:") == 1
    assert sorted(os.listdir(os.path.join(root, ".github", "workflows"))) == ["hourly.yml"]   # no new workflow
    with open(os.path.join(root, "research_log.py")) as f:
        code = f.read()
    assert "import requests" not in code and "scanner" not in code and "venues" not in code   # it cannot fetch


# ---------- the passive collection points themselves ----------

def fake_scan(monkeypatch, sc):
    board = [{"user_id": w, "user_name": w, "rank": i, "pnl": 1, "volume": 2} for i, w in enumerate(["0x1", "0x2", "0x3", "0x4"], 1)]
    def crypto(wallet):
        if wallet == "0x4":
            raise RuntimeError("data unavailable")
        return 0.9 if wallet == "0x3" else 0.0
    def positions(wallet, min_value):
        return [{"condition_id": "cA", "outcome": "Yes", "title": "A vs B", "event_id": "e1", "event_slug": "nba-a-b-2026-12-01",
                 "token_id": "tokA", "outcome_index": 0, "current_value": 500, "entry_cost_usdc": 400, "current_size": 1000},
                {"condition_id": "cH", "outcome": "Yes", "title": "H", "event_id": "e2", "event_slug": "nba-h-2026-12-01",
                 "token_id": "tokH", "outcome_index": 0, "current_value": 300, "entry_cost_usdc": 300, "current_size": 600}]
    monkeypatch.setattr(sc, "leaderboard", lambda *a: board)
    monkeypatch.setattr(sc, "crypto_share", crypto)
    monkeypatch.setattr(sc, "open_positions", positions)
    monkeypatch.setattr(sc, "hedged_markets", lambda wallet, ids: {"cH"})
    monkeypatch.setattr(sc, "get", lambda path, params, **k: {"data": {"value": 2000}})
    monkeypatch.setattr(sc, "event_tags", lambda ids: {e: {"sports"} for e in ids})
    args = sc.build_parser().parse_args(["--min-traders", "2"])
    args.max_move = 1
    return args


def test_scanner_notes_every_trader_including_skipped_and_failed(monkeypatch):
    sc = ft.scanner
    rows, _ = sc.find_picks(fake_scan(monkeypatch, sc))
    assert [(r["Pick"], r["Bettors"], r["Avg bankroll %"], r["Total $ in"]) for r in rows] == [("Yes", 2, 25.0, 800)]
    t = sc.RESEARCH["traders"]
    assert [t[w]["status"] for w in ("0x1", "0x2", "0x3", "0x4")] == ["scanned", "scanned", "skipped_bot", "error"]
    assert t["0x1"]["bankroll"] == 2000 and t["0x1"]["hedged"] == {"cH"} and t["0x1"]["raw_positions"] == 2
    assert "data unavailable" in t["0x4"]["reason"] and len(sc.RESEARCH["board"]) == 4
    sc.find_picks(fake_scan(monkeypatch, sc))
    assert len(sc.RESEARCH["board"]) == 4                          # cleared at the start of every scan


def test_report_card_grading_unchanged_and_each_position_noted(monkeypatch):
    cs = ft.claude_strategy
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    pend = lambda entry, last: {"first_seen": past, "name": "n", "game": "nba-a-b", "start": past, "entry": entry,
                                "last_price": last, "pick": "A", "market": "A vs B"}
    card = cs.new_card()
    card["pending"] = {"0x1|t1": pend(0.50, 0.52), "0x1|t2": pend(0.40, 0.41), "0x2|t3": pend(0.30, None)}
    monkeypatch.setattr(cs, "closing_price", lambda token, start, cache: {"t1": 0.56}.get(token))
    del cs.GRADED[:]
    assert cs.grade(card) == 2 and card["pending"] == {}
    tr = card["traders"]["0x1"]
    assert tr["n"] == 1 and tr["sum"] == pytest.approx((0.06 + 0.01) / 2)       # one game, average of both
    assert "0x2" not in card["traders"]
    assert [(g["wallet"], g["token_id"], g["close_source"], g["close"]) for g in cs.GRADED] == [
        ("0x1", "t1", "price_history", 0.56), ("0x1", "t2", "last_look", 0.41), ("0x2", "t3", "none", None)]
