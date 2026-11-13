"""Pick log (added 2026-11-13): every game pick the scanner finds, bet on or not.

For research only: no strategy or real bet reads it. It lets new strategy ideas be tested on
every pick, not just the ones we happened to bet.

- pick_log_open.json: picks whose game hasn't been decided yet. For each: how it looked when
  first seen and when last seen before the start (bettors, against, bankroll %, money, prices).
- pick_log.csv: one row per finished pick (added once, never changed), with the closing price
  (last price before the start) and the result (1 = won, 0 = lost, 0.5 = refund/split).
Futures and other markets without a game start time are left out.
"""
import csv
import json
import os
from datetime import datetime, timedelta, timezone

import claude_strategy  # closing_price
import polymarket_smart_money as scanner

OPEN = "pick_log_open.json"
CSV = "pick_log.csv"
# The research log of picks counted over the top 200 traders (2026-11-13): pick_log_200.csv and
# pick_log_200_open.json, with extra columns for how many bettors / against are in the top 100.
GIVE_UP_DAYS = 14  # no result this long after the start: written with an empty result

# pick column -> short name used in the log
SNAP = {"Bettors": "bettors", "Against": "against", "Conviction bettors": "conv_bettors",
        "Conviction against": "conv_against", "Avg bankroll %": "bankroll_pct", "Total $ in": "usd_in",
        "Against $ in": "against_usd", "Avg entry price": "entry", "Price now": "price"}
WIDE_SNAP = {**SNAP, "Bettors top 100": "bettors_top100", "Against top 100": "against_top100"}
COLUMNS = (["sport", "start", "market", "pick", "market_type", "link", "first_seen", "last_seen", "runs_seen",
            "max_bettors"]
           + [f"first_{v}" for v in SNAP.values()] + [f"last_{v}" for v in SNAP.values()]
           + ["close_price", "result", "bet_by", "token_id", "condition_id"])


def columns(snap):
    return (COLUMNS[:10] + [f"first_{v}" for v in snap.values()] + [f"last_{v}" for v in snap.values()]
            + COLUMNS[-5:])


def files(name):
    """pick_log -> (pick_log_open.json, pick_log.csv); pick_log_200 -> its own pair."""
    return f"{name}_open.json", f"{name}.csv"


def now():
    return datetime.now(timezone.utc)


def load(here, name="pick_log"):
    path = os.path.join(here, files(name)[0])
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"open": {}, "not_games": []}


def save(log, here, name="pick_log"):
    path = os.path.join(here, files(name)[0])
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(log, f, separators=(",", ":"))
    os.replace(tmp, path)


def start_of(m):
    when = (m or {}).get("gameStartTime")
    if not when:
        return None
    return datetime.fromisoformat(when.replace("Z", "+00:00").replace(" ", "T"))


def record(log, picks, market_info, snap_fields=SNAP):
    """Note this run's picks. market_info(condition_ids) -> {condition_id: Gamma market}."""
    t = now()
    stamp = t.isoformat(timespec="seconds")
    not_games = set(log.get("not_games") or [])
    new = {p["condition_id"] for p in picks if p.get("token_id") and p["token_id"] not in log["open"]
           and p["condition_id"] not in not_games}
    info = market_info(new) if new else {}
    for p in picks:
        tok = p.get("token_id")
        if not tok:
            continue
        snap = {v: p.get(k) for k, v in snap_fields.items()}
        e = log["open"].get(tok)
        if e is None:
            if p["condition_id"] not in new:
                continue  # known not to be a game
            m = info.get(p["condition_id"])
            start = start_of(m)
            if not start:
                not_games.add(p["condition_id"])
                continue
            if start <= t:
                continue  # first seen after the start: nothing useful to record
            sport = (p.get("Link") or "").rstrip("/").rsplit("/", 1)[-1].split("-")[0]
            log["open"][tok] = {
                "sport": sport, "start": start.isoformat(), "market": p["Market"], "pick": p["Pick"],
                "market_type": (m or {}).get("sportsMarketType"), "link": p.get("Link"),
                "condition_id": p["condition_id"], "outcome_index": p.get("outcome_index"),
                "first_seen": stamp, "last_seen": stamp, "runs_seen": 1,
                "max_bettors": p.get("Bettors"), "first": snap, "last": snap}
        elif datetime.fromisoformat(e["start"]) > t:  # keep the last look BEFORE the start
            e.update(last_seen=stamp, last=snap, runs_seen=e["runs_seen"] + 1,
                     max_bettors=max(e["max_bettors"] or 0, p.get("Bettors") or 0))
    log["not_games"] = sorted(not_games)[-5000:]


def resolutions(condition_ids):
    """condition_id -> payout per share for each outcome (1.0 / 0.0 / 0.5), once resolved."""
    ids, out = sorted(condition_ids), {}
    for i in range(0, len(ids), 20):
        for r in scanner.get("/resolutions", {"condition": ",".join(ids[i:i + 20])}).get("data") or []:
            if r.get("status") == "resolved" and r.get("payouts"):
                out[r["condition_id"]] = [p / 1_000_000 for p in r["payouts"]]  # micro-USDC per share
    return out


def settle(log, here, states, live_state, name="pick_log", snap_fields=SNAP):
    """Add closing prices and results; move finished picks to the CSV. Returns how many."""
    t = now()
    cache = {}
    for tok, e in log["open"].items():
        if "close" not in e and datetime.fromisoformat(e["start"]) <= t:
            e["close"] = claude_strategy.closing_price(tok, e["start"], cache)
    due = {tok: e for tok, e in log["open"].items()
           if datetime.fromisoformat(e["start"]) <= t - timedelta(hours=2)}
    if not due:
        return 0
    paid = resolutions({e["condition_id"] for e in due.values()})
    bet_by = {}
    for key, st in list(states.items()) + [("real", live_state)]:
        for b in st.get("bets", []):
            bet_by.setdefault(b.get("token_id"), []).append(key.upper() if key != "real" else "real")
    rows = []
    for tok, e in due.items():
        try:
            result = round(paid[e["condition_id"]][e["outcome_index"]], 2)
        except (KeyError, IndexError, TypeError):
            result = None
        too_old = datetime.fromisoformat(e["start"]) < t - timedelta(days=GIVE_UP_DAYS)
        if result is None and not too_old:
            continue
        row = {k: e.get(k) for k in ("sport", "start", "market", "pick", "market_type", "link",
                                       "first_seen", "last_seen", "runs_seen", "max_bettors", "condition_id")}
        row.update({f"first_{k}": v for k, v in e["first"].items()})
        row.update({f"last_{k}": v for k, v in e["last"].items()})
        row.update(close_price=e.get("close"), result=result, token_id=tok,
                   bet_by=" ".join(sorted(set(bet_by.get(tok, [])))))
        rows.append(row)
        del log["open"][tok]
    if rows:
        path = os.path.join(here, files(name)[1])
        new_file = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=columns(snap_fields))
            if new_file:
                w.writeheader()
            w.writerows(rows)
    return len(rows)
