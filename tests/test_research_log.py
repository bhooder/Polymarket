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
    assert [r["change"] for r in rows] == ["baseline", "shares_up", "shares_down"]   # baseline: held when tracking began
    step(f, make_obs({W1: [pos("t1", shares=40, cost=21, price=0.60), pos("t2")]}), 40)
    assert read_all(f, "positions")[-1]["change"] == "first_seen"                    # appeared while we were watching
    assert rows[1]["shares_delta"] == "50.0" and rows[1]["cost_delta"] == "30.0"
    assert rows[1]["prev_observed_at"] == "2026-11-20T15:10:00Z"   # the last look before the change
    assert rows[1]["observed_at"] == "2026-11-20T15:20:00Z" and rows[0]["prev_observed_at"] == ""
    market = read_all(f, "markets")
    assert len(market) == 2 and market[0]["m"] == rows[0]["m"] and market[0]["token_id"] == "t1"   # code -> market
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
    assert [r["change"] for r in read_all(h, "positions")] == ["baseline"]        # no "not_observed" after the start


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
        ("baseline", "4", "0.5"), ("price", "4", "0.52"), ("counts", "5", "0.52")]
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
    for banned in ("import requests", "import polymarket_smart_money", "scanner.get", "import venues", "import live",
                   "urllib", "http"):
        assert banned not in code                                   # it has no way to fetch anything or place orders


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


# ---------- final check 1: pushes that fail, retries, interrupted runs ----------

def copy_tree(src, dst):
    import shutil
    if os.path.exists(dst):
        shutil.rmtree(dst)
    if os.path.exists(src):
        shutil.copytree(src, dst)


def raw(folder, kind):
    return read(folder, kind)


def test_run_whose_results_were_never_saved_leaves_no_trace_and_no_duplicates(tmp_path):
    """A failed push or a killed run: GitHub's next run starts from the last SAVED folder (state and logs
    are saved in one commit). Replaying from that saved copy must give exactly one record of each change."""
    f, saved = str(tmp_path / "repo"), str(tmp_path / "saved")
    step(f, make_obs({W1: [pos("t1", 100, 50)]}), 0)
    copy_tree(os.path.join(f, "research"), saved)                       # the last commit that reached GitHub
    step(f, make_obs({W1: [pos("t1", 150, 80)]}), 10)                   # this run's push failed: lost
    copy_tree(saved, os.path.join(f, "research"))                       # next run checks out the saved folder
    step(f, make_obs({W1: [pos("t1", 150, 80)]}), 20)
    rows = raw(f, "positions")
    assert [(r["change"], r["observed_at"], r["prev_observed_at"]) for r in rows] == [
        ("baseline", "2026-11-20T15:00:00Z", ""), ("shares_up", "2026-11-20T15:20:00Z", "2026-11-20T15:00:00Z")]
    assert len({r["oid"] for r in rows}) == 2 and len(read(f, "runs")) == 2


def test_rows_written_but_state_not_saved_repeat_with_the_same_id(tmp_path, monkeypatch):
    f = str(tmp_path)
    step(f, make_obs({W1: [pos("t1", 100, 50)]}), 0)
    real_save = rl.save_state
    monkeypatch.setattr(rl, "save_state", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    step(f, make_obs({W1: [pos("t1", 150, 80)]}), 10)                   # rows written, state lost
    assert "state not saved" in read(f, "runs")[-1]["notes"]
    monkeypatch.setattr(rl, "save_state", real_save)
    step(f, make_obs({W1: [pos("t1", 150, 80)]}), 20)                   # the same change is seen again
    rows = raw(f, "positions")
    assert [r["change"] for r in rows] == ["baseline", "shares_up", "shares_up"]
    assert rows[1]["oid"] == rows[2]["oid"] != rows[0]["oid"]           # the repeat is recognisable...
    clean = rl.read_log(f, "positions")
    assert [(r["change"], r["observed_at"]) for r in clean] == [
        ("baseline", "2026-11-20T15:00:00Z"), ("shares_up", "2026-11-20T15:10:00Z")]   # ...and dropped, earliest kept
    step(f, make_obs({W1: [pos("t1", 100, 50)]}), 30)
    step(f, make_obs({W1: [pos("t1", 150, 80)]}), 40)                   # a REAL second rise to the same numbers
    assert [r["change"] for r in rl.read_log(f, "positions")] == ["baseline", "shares_up", "shares_down", "shares_up"]


def test_failed_write_of_one_log_is_retried_without_loss_or_repeat(tmp_path, monkeypatch):
    f = str(tmp_path)
    p = make_pick("x", bettors=4, pct=15.0, entry=0.50)
    def obs(shares):
        o = make_obs({W1: [pos("t1", shares, shares / 2)]})
        o["card"]["markets"][p["condition_id"]] = {"start": (NOW + timedelta(hours=30)).isoformat()}
        o["picks"], o["top_wallets"], o["sides"] = [p], set(), {}
        return o
    real_append = rl.append
    def flaky(here, kind, when, cols, rows):
        if kind == "positions":
            raise OSError("disk write error")
        return real_append(here, kind, when, cols, rows)
    monkeypatch.setattr(rl, "append", flaky)
    step(f, obs(100), 0)
    assert raw(f, "positions") == [] and len(raw(f, "snapshots")) == 1  # one log failed, the others were written
    assert "positions: OSError" in read(f, "runs")[-1]["notes"]
    monkeypatch.setattr(rl, "append", real_append)
    step(f, obs(100), 10)
    rows = raw(f, "positions")
    assert [r["change"] for r in rows] == ["baseline"]                  # written now, once, still correctly labelled
    assert len(raw(f, "snapshots")) == 1
    # the market row had been written before the failure, so it was written again: same oid, dropped on reading
    assert len(raw(f, "markets")) == 2 and len(rl.read_log(f, "markets")) == 1


def test_torn_row_from_an_interrupted_write_is_isolated(tmp_path):
    f = str(tmp_path)
    step(f, make_obs({W1: [pos("t1", 100, 50)]}), 0)
    path = os.path.join(f, "research", "positions", "2026-11-20", "15.csv")
    with open(path, "a") as fh:
        fh.write("abc123,2026-11-20T15:05:00Z,,0xaaa,,deadbeef,shares_up,12")   # cut off mid-row, no line end
    step(f, make_obs({W1: [pos("t1", 150, 80)]}), 10)
    assert [r["change"] for r in rl.read_log(f, "positions")] == ["baseline", "shares_up"]   # torn row skipped
    with open(path) as fh:
        assert fh.read().count("\n") == 4                                 # header, row, torn row, new row


def test_runaway_output_is_refused(tmp_path, monkeypatch):
    f = str(tmp_path)
    monkeypatch.setattr(rl, "MAX_ROWS", 3)
    step(f, make_obs({W1: [pos(f"t{i}") for i in range(5)]}), 0)
    assert raw(f, "positions") == [] and "over the 3 limit" in read(f, "runs")[-1]["notes"]
    monkeypatch.setattr(rl, "MAX_ROWS", 50_000)
    step(f, make_obs({W1: [pos(f"t{i}") for i in range(5)]}), 10)
    monkeypatch.setattr(rl, "MAX_FILE", 10)
    said = rl.run(f, make_obs({W1: [pos(f"t{i}", 999) for i in range(5)]}), now=NOW + timedelta(minutes=20))
    assert len(raw(f, "positions")) == 5 and "nothing written" in said   # an oversized file is left alone


# ---------- final check 2: research failures cannot touch trading ----------

@pytest.mark.parametrize("state_text", ["not json at all", "[1, 2, 3]", "{}", '{"epoch": "x", "positions": [1], '
                                        '"decisions": {"a|b": "junk"}, "snapshots": {"k": 5}, "traders": {"w": "scanned"}}',
                                        '{"epoch": "x", "positions": {"w|m": {"s": "NaN"}, "bad": 7}}'])
def test_malformed_state_never_stops_logging(tmp_path, state_text):
    f = str(tmp_path)
    os.makedirs(os.path.join(f, "research"))
    with open(os.path.join(f, "research", "state.json"), "w") as fh:
        fh.write(state_text)
    step(f, make_obs({W1: [pos("t1")]}), 0)
    step(f, make_obs({W1: [pos("t1", 150, 80)]}), 10)
    assert [r["change"] for r in rl.read_log(f, "positions")] == ["baseline", "shares_up"]
    notes = [r["notes"] for r in read(f, "runs")]
    assert "Error:" not in notes[1] and ":" not in notes[1]                # second run is clean


@pytest.mark.parametrize("broken", ["append", "save_state", "load_state", "makedirs", "open"])
def test_disk_errors_in_research_leave_trading_untouched(world, main_env, monkeypatch, tmp_path, broken):
    scenario.build(world)
    monkeypatch.setenv("RESEARCH_LOG", "off")
    main_env.run()                                                       # reference run without research
    reference = snapshot_of_decisions(main_env)
    folder = tmp_path / "with_broken_research"
    folder.mkdir()
    monkeypatch.setattr(ft, "HERE", str(folder))
    main_env.folder = str(folder)
    monkeypatch.setenv("RESEARCH_LOG", "on")
    boom = lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left on device"))
    if broken in ("append", "save_state", "load_state"):
        monkeypatch.setattr(rl, broken, boom)
    elif broken == "makedirs":
        real = os.makedirs
        monkeypatch.setattr(rl.os, "makedirs", lambda path, **k: boom() if "research" in str(path) else real(path, **k))
    else:
        import builtins
        real_open = builtins.open
        monkeypatch.setattr(builtins, "open", lambda path, *a, **k: boom() if os.sep + "research" in str(path)
                            else real_open(path, *a, **k))
    main_env.seen.clear()
    main_env.run()                                                       # must not raise
    assert snapshot_of_decisions(main_env) == reference                  # same bets, same real-money intents
    for name in ("paper_trades.json", "paper_trades_chatgpt.json", "live_trades.json", "live_log.json",
                 "paper_report.html", "paper_trades.csv"):
        assert os.path.getsize(os.path.join(main_env.folder, name)) > 0  # state saved and report written
    assert main_env.seen["live_runs"] == 1


# ---------- bot-speed label and nothing sensitive ----------

def test_bot_speed_traders_are_kept_and_labelled(tmp_path):
    f = str(tmp_path)
    obs = make_obs({W1: [pos("t1")], W2: [pos("t2")], "0xccc": [pos("t3")]})
    obs["pace"], obs["max_pace"] = {W1: 320.0, W2: 4.0}, 50
    step(f, obs, 0)
    assert {r["w"]: r["bot_speed"] for r in raw(f, "positions")} == {W1: "1", W2: "0", "0xccc": ""}   # '' = unknown
    board = {r["wallet"]: (r["bot_speed"], r["trades_per_hour"]) for r in raw(f, "leaderboard")}
    assert board == {W1: ("1", "320.0"), W2: ("0", "4.0"), "0xccc": ("", "")}


def test_nothing_sensitive_reaches_the_research_folder(world, main_env, monkeypatch):
    secrets = {name: f"SECRET-{name}-9f8e7d" for name in (
        "KALSHI_KEY_ID", "KALSHI_PRIVATE_KEY", "PM_US_KEY_ID", "PM_US_SECRET_KEY", "PROPHETX_ACCESS_KEY",
        "PROPHETX_SECRET_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    scenario.build(world)
    main_env.run()
    obs = make_obs({W1: [pos("t1")]}, status={W2: "error"})
    obs["scan"]["traders"][W1]["reason"] = "HTTPError: 403 for url: https://example.test/x?user=0xabc&apikey=SECRET-K"
    obs["scan"]["board"][0]["user_id"] = W1
    rl.run(main_env.folder, obs)
    text, names = "", []
    for root, _, files in os.walk(os.path.join(main_env.folder, "research")):
        for name in files:
            names.append(name)
            with open(os.path.join(root, name)) as fh:
                text += fh.read()
    assert names and not any(v in text for v in secrets.values())
    assert "apikey" not in text and "SECRET" not in text                 # web address details are cut off
    with open(os.path.join(main_env.folder, "live_trades.json")) as fh:
        assert '"cash"' in fh.read()                                     # the real account file has balances...
    for word in ("cash", "balance", "private", "secret", "password", "bearer", "authorization", "chat_id", "@"):
        assert word not in text.lower()                                  # ...the research folder has none of it
    assert not [n for n in names if n.endswith(".tmp")]
    allowed = set(rl.GRADED_COLS + rl.POSITION_COLS + rl.MARKET_COLS + rl.DECISION_COLS + rl.SNAPSHOT_COLS
                  + rl.PATH_COLS + rl.BOARD_COLS + rl.RULES_COLS + rl.RUN_COLS)
    assert not [c for c in allowed if any(w in c for w in ("key", "secret", "auth", "email", "cash", "balance"))]


def test_scanner_keeps_only_a_few_trade_fields(monkeypatch):
    sc = ft.scanner
    for part in sc.RESEARCH.values():
        part.clear()
    monkeypatch.setattr(sc, "get", lambda path, params, **k: {"data": [
        {"timestamp": 1700000000 + i, "side": "BUY", "asset": "tokA", "size": 5, "title": "A vs B",
         "transaction_hash": "0xdead", "user_name": "someone", "bio": "x" * 500} for i in range(3)]})
    assert sc.crypto_share("0x1") == 0.0
    assert sc.RESEARCH["trades"]["0x1"][0] == {"timestamp": 1700000000, "side": "BUY", "asset": "tokA", "size": 5}
    assert "bio" in sc.RESEARCH["trade_fields"] and "asset" in sc.RESEARCH["trade_fields"]
    sc._keep_trades("0x2", [None, 5])                                    # nonsense in: no error out


# ---------- fixes of 2026-11-15: entry time as UTC text, no trade evidence without a previous look ----------

OLD_HEAD = ",".join(rl.POSITION_COLS)
# three rows exactly as the first production run wrote them (shortened ids), plus one already-correct row
OLD_ROWS = [
    "bc7e1cd7a1b2,2026-11-15T19:18:43Z,,0xf0318c3213,0,7e984e393951,baseline,217729.36,,77966.73,,0.358,77293.92,0.355,"
    "5687101.85,1.36,1794765284,,,confirmed,1,2026-11-15T18:53:23Z,2026-11-15T18:53:23Z,176932.98",
    "aaaaaaaaaaaa,2026-11-15T19:18:43Z,,0xf0318c3213,1,111111111111,baseline,500.0,,250.0,,0.5,255.0,0.51,"
    "5687101.85,0.0,1794000000,,,none_in_list,0,,,",
    "bbbbbbbbbbbb,2026-11-15T19:40:00Z,2026-11-15T19:18:43Z,0xf0318c3213,0,7e984e393951,shares_up,227729.36,10000.0,"
    "81500.0,3533.27,0.358,80000.0,0.355,5687101.85,1.4,1794765284,,,confirmed,2,2026-11-15T19:20:00Z,"
    "2026-11-15T19:30:00Z,10000.0",
    "cccccccccccc,2026-11-15T19:50:00Z,2026-11-15T19:40:00Z,0xf0318c3213,0,222222222222,first_seen,10.0,,5.0,,0.5,5.0,0.5,"
    "5687101.85,0.0,2026-11-15T19:45:00Z,,,none_in_list,0,,,",
]


def old_folder(tmp_path, extra=""):
    d = tmp_path / "research" / "positions" / "2026-11-15"
    d.mkdir(parents=True)
    (d / "19.csv").write_text(OLD_HEAD + "\r\n" + "\r\n".join(OLD_ROWS) + "\r\n" + extra, newline="")
    return str(tmp_path)


def test_entry_time_is_utc_text():
    assert rl.entry_time(1794765284) == rl.entry_time("1794765284") == "2026-11-15T17:54:44Z"
    assert rl.entry_time(1794765284000) == "2026-11-15T17:54:44Z"               # milliseconds
    assert rl.entry_time("2026-11-19T10:00:00Z") == "2026-11-19T10:00:00Z"
    assert rl.entry_time("2026-11-19 05:00:00-05:00") == "2026-11-19T10:00:00Z"  # any zone -> UTC
    assert rl.entry_time(None) == rl.entry_time("") == rl.entry_time("soon") == ""   # unknown stays blank


def test_new_rows_use_utc_entry_time_and_claim_no_evidence_without_a_previous_look(tmp_path):
    f = str(tmp_path)
    at = lambda minutes: int((NOW + timedelta(minutes=minutes)).timestamp())
    old_trades = {W1: [{"token_id": "t1", "timestamp": at(-90), "side": "BUY", "size": 100}]}   # before we ever looked
    p1 = {**pos("t1", 100, 50), "first_entry_at": at(-3000)}
    step(f, make_obs({W1: [p1]}, trades=old_trades), 0)
    base = rl.read_log(f, "positions")[0]
    assert base["change"] == "baseline" and base["prev_observed_at"] == ""
    assert (base["trade_evidence"], base["trades_in_gap"], base["trade_first_at"], base["trade_shares"]) == (
        "no_previous_look", "", "", "")                                        # an old trade is NOT a new purchase
    assert base["position_first_entry_at"] == "2026-11-18T13:00:00Z"            # UTC text, not 17946...
    # a trader read for the first time later on: also no previous look, so also no evidence claimed
    step(f, make_obs({W1: [p1], W2: [pos("t2")]}, trades={W2: [{"token_id": "t2", "timestamp": at(5), "side": "BUY", "size": 9}]}), 10)
    newcomer = rl.read_log(f, "positions")[-1]
    assert (newcomer["w"], newcomer["change"], newcomer["trade_evidence"]) == (W2, "first_seen", "no_previous_look")
    # with a previous look, evidence works as before
    trades = {W1: old_trades[W1] + [{"token_id": "t1", "timestamp": at(15), "side": "BUY", "size": 50}]}
    step(f, make_obs({W1: [{**p1, "current_size": 150, "entry_cost_usdc": 80}], W2: [pos("t2")]}, trades=trades), 20)
    up = rl.read_log(f, "positions")[-1]
    assert (up["change"], up["trade_evidence"], up["trades_in_gap"], up["trade_shares"]) == ("shares_up", "confirmed", "1", "50.0")


def test_saved_rows_are_corrected_once_with_an_audit_trail(tmp_path):
    f = old_folder(tmp_path, extra="dddddddddddd,2026-11-15T19:55:00Z,,0xf03")   # plus a row cut short
    before = read(f, "positions")
    step(f, make_obs({}), 0)
    rows = {r["oid"]: r for r in rl.read_log(f, "positions")}
    assert list(rows) == [r["oid"] for r in before[:4]]                         # same rows, same order, same ids
    a, b, c, d = (rows[k] for k in ("bc7e1cd7a1b2", "aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"))
    assert a["position_first_entry_at"] == "2026-11-15T17:54:44Z" and b["position_first_entry_at"] == "2026-11-06T21:20:00Z"
    assert [a[k] for k in rl.EVIDENCE_COLS] == ["no_previous_look", "", "", "", ""] == [b[k] for k in rl.EVIDENCE_COLS]
    # a row that HAD a previous look keeps its real evidence; only its time format changes
    assert [c[k] for k in rl.EVIDENCE_COLS] == ["confirmed", "2", "2026-11-15T19:20:00Z", "2026-11-15T19:30:00Z", "10000.0"]
    assert c["position_first_entry_at"] == "2026-11-15T17:54:44Z"
    assert d == before[3]                                                       # already correct: untouched
    for old, new in zip(before[:3], (a, b, c)):                                 # nothing else changed
        same = [k for k in old if k not in rl.EVIDENCE_COLS + ["position_first_entry_at"]]
        assert all(old[k] == new[k] for k in same)
    with open(os.path.join(f, "research", "positions", "2026-11-15", "19.csv")) as fh:
        assert "dddddddddddd,2026-11-15T19:55:00Z,,0xf03" in fh.read()          # the torn row is left as it was
    trail = rl.read_log(f, "corrections")
    assert [(t["row_oid"], t["old_value"]) for t in trail if t["row_oid"]] == [   # rows that named real trades
        ("bc7e1cd7a1b2", "confirmed|1|2026-11-15T18:53:23Z|2026-11-15T18:53:23Z|176932.98")]
    summary = sorted(t["old_value"] for t in trail if not t["row_oid"])              # identical changes, counted
    assert summary == ["Unix seconds in 3 rows", "none_in_list in 1 rows with no previous look"]
    assert all(t["file"] == os.path.join("positions", "2026-11-15", "19.csv") and t["corrected_at"] == "2026-11-20T15:00:00Z"
               and t["new_value"] and t["reason"] for t in trail)
    assert "corrected 3 saved rows in 1 files" in read(f, "runs")[-1]["notes"]
    # second run: nothing more to correct, nothing added to the trail
    size = os.path.getsize(os.path.join(f, "research", "corrections.csv"))
    step(f, make_obs({}), 10)
    assert os.path.getsize(os.path.join(f, "research", "corrections.csv")) == size
    assert "corrected" not in read(f, "runs")[-1]["notes"]
    # even if the state is lost and the correction runs again, it finds nothing left to change
    os.remove(os.path.join(f, "research", "state.json"))
    step(f, make_obs({}), 20)
    assert os.path.getsize(os.path.join(f, "research", "corrections.csv")) == size


def test_failed_correction_is_retried_and_never_blocks_logging(tmp_path, monkeypatch):
    f = old_folder(tmp_path)
    name, real = rl.CORRECTIONS[0]
    monkeypatch.setattr(rl, "CORRECTIONS", [(name, lambda *a: (_ for _ in ()).throw(OSError("disk error")))])
    step(f, make_obs({W1: [pos("t1")]}), 0)
    assert "correction not applied: OSError" in read(f, "runs")[-1]["notes"]
    assert any(r["w"] == W1 for r in rl.read_log(f, "positions"))               # this run's rows were still written
    assert read(f, "positions")[0]["trade_evidence"] == "confirmed"             # old rows untouched so far
    monkeypatch.setattr(rl, "CORRECTIONS", [(name, real)])
    step(f, make_obs({W1: [pos("t1")]}), 10)                                    # retried next run
    assert read(f, "positions")[0]["trade_evidence"] == "no_previous_look"
    assert len(rl.read_log(f, "corrections")) == 3                              # one row-level, two summaries


def test_correction_only_ever_touches_the_positions_log(world, main_env):
    scenario.build(world)
    main_env.run()                                                              # creates every trading file + research logs
    import shutil
    from pathlib import Path
    shutil.copytree(os.path.join(old_folder(Path(main_env.folder) / "x"), "research", "positions", "2026-11-15"),
                    os.path.join(main_env.folder, "research", "positions", "2026-11-15"), dirs_exist_ok=True)
    os.remove(os.path.join(main_env.folder, "research", "state.json"))          # so the correction runs again
    def digest():
        out = {}
        for root, _, files in os.walk(main_env.folder):
            for name in files:
                path = os.path.join(root, name)
                with open(path, "rb") as fh:
                    out[os.path.relpath(path, main_env.folder)] = hashlib.sha1(fh.read()).hexdigest()
        return out
    before = digest()
    name, fix = rl.CORRECTIONS[0]
    assert fix(main_env.folder, NOW, name) == (3, 1)
    changed = sorted(k for k, v in digest().items() if before.get(k) != v)
    assert changed == [os.path.join("research", "corrections.csv"),
                       os.path.join("research", "positions", "2026-11-15", "19.csv")]
