#!/usr/bin/env python3
"""
Forward test (paper trading) for the smart-money scanner.

Each run:
1. Settles open paper bets whose markets have been decided.
2. Runs the scanner (polymarket_smart_money.py defaults: top 100 sports traders by
   monthly PnL, hedges and crypto bots removed).
3. Keeps picks that meet a staking tier, haven't started, and are decided within 3 days.
4. "Buys" them with pretend money on the cheaper US venue, Polymarket US or Kalshi
   (see venues.py), from real order books with fees, if the price is no more than 2c
   above or 4c below the traders' average entry.
5. Does steps 1, 3 and 4 separately for each strategy (own $1,000 pretend bankroll).
6. Writes paper_trades.json / paper_trades_b.json (state), matching CSVs and paper_report.html.

No real orders are placed. Runs every 15 minutes on GitHub Actions.

Usage:
    python3 forward_test.py
"""
import csv
import html
import json
import math
import os
import shutil
import time
from datetime import datetime, timedelta, timezone

import polymarket_smart_money as scanner
import venues

HERE = os.path.dirname(os.path.abspath(__file__))
CLOB = "https://clob.polymarket.com"
# On a Mac, copies of the reports also go here (iCloud Drive), for viewing on a phone
ICLOUD = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/Polymarket")

START_BANKROLL = 1000.0
MAX_DAYS_TO_END = 3      # only markets that end within this many days
MAX_EXPOSURE = 0.50      # at most this share of the bankroll in open bets at once
MAX_ABOVE = 0.02         # pay at most 2c more than the traders' average entry...
MAX_BELOW = 0.04         # ...and at most 4c less

# Each strategy has its own pretend bankroll and state file; all other rules are shared.
# tiers: (stake as share of current bankroll, [(min bettors, min avg bankroll %), ...]),
# checked top to bottom; the first tier with any matching rule wins.
STRATEGIES = {
    "a": {"name": "Strategy A", "state": "paper_trades.json", "csv": "paper_trades.csv",
          "rules": "Stake by bettors and avg bankroll %. 2%: 3+ bettors & 35%+, 4+ & 25%+, or 5+ & 20%+. "
                   "1%: 3+ & 20%+, 4+ & 15%+, or 5+ & 10%+.",
          "tiers": [(0.02, [(3, 35), (4, 25), (5, 20)]),
                    (0.01, [(3, 20), (4, 15), (5, 10)])]},
    "b": {"name": "Strategy B", "state": "paper_trades_b.json", "csv": "paper_trades_b.csv",
          "rules": "Stake by number of bettors only. 2%: 5+ bettors. 1%: 4 bettors. "
                   "Avg bankroll % is ignored.",
          "tiers": [(0.02, [(5, 0)]),
                    (0.01, [(4, 0)])]},
}


def now():
    return datetime.now(timezone.utc)


def stake_for(bettors, avg_pct, tiers):
    for stake, rules in tiers:
        if any(bettors >= n and avg_pct >= p for n, p in rules):
            return stake
    return 0.0


def load_state(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"start_bankroll": START_BANKROLL, "cash": START_BANKROLL,
            "started_at": now().isoformat(), "bets": []}


def save_state(state, path):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)  # never leave a half-written file


def open_bets(state):
    return [b for b in state["bets"] if b["status"] == "open"]


def bankroll(state):
    """Current bankroll = cash + money in open bets (at cost)."""
    return state["cash"] + sum(b["cost"] for b in open_bets(state))


def settle(state):
    """Pay out bets whose market has resolved, on whichever venue they were placed."""
    pending = open_bets(state)
    # bets from before the US-venue switch were "placed" on international Polymarket
    intl = [b for b in pending if b.get("venue", "polymarket_intl") == "polymarket_intl"]
    ids = sorted({b["condition_id"] for b in intl})
    results = {}
    for i in range(0, len(ids), 20):
        for r in scanner.get("/resolutions", {"condition": ",".join(ids[i:i + 20])}).get("data") or []:
            if r.get("status") == "resolved" and r.get("payouts"):
                results[r["condition_id"]] = r["payouts"]
    for b in pending:
        if b in intl:
            payouts = results.get(b["condition_id"])
            per_share = payouts and payouts[b["outcome_index"]] / 1_000_000  # micro-USDC per share
        else:
            per_share = venues.result(b["venue_ref"])
        if per_share is None:
            continue
        payout = b["shares"] * per_share
        b["payout"] = round(payout, 2)
        b["profit"] = round(payout - b["cost"], 2)
        b["status"] = "won" if payout > b["cost"] else "lost" if payout < b["cost"] else "push"
        b["settled_at"] = now().isoformat()
        state["cash"] += payout
        print(f"  settled {b['status'].upper()}: {b['pick']} / {b['market'][:50]}  "
              f"cost ${b['cost']:.2f} -> paid ${payout:.2f}")


def decided_by(m):
    """When the market should be decided. For games, endDate is a placeholder about a week
    out, so use the game's start time instead (games end within hours of starting)."""
    when = m.get("gameStartTime") or m.get("endDate")
    return datetime.fromisoformat(when.replace("Z", "+00:00")) if when else None


def market_info(condition_ids):
    """condition_id -> Gamma market (endDate, closed, feeSchedule, ...)."""
    info, ids = {}, sorted(condition_ids)
    for i in range(0, len(ids), 50):
        for m in scanner.get("/markets", {"condition_ids": ids[i:i + 50], "limit": 50},
                             base=scanner.GAMMA):
            info[m["conditionId"]] = m
    return info


def simulate_buy(token_id, dollars, fee):
    """Walk the real order book: how many shares would `dollars` buy, fees included?"""
    book = scanner.get("/book", {"token_id": token_id}, base=CLOB)
    rate, exp = (fee or {}).get("rate") or 0, (fee or {}).get("exponent") or 1
    shares = spent = fees = 0.0
    for level in reversed(book.get("asks") or []):  # asks come highest first
        p, size = float(level["price"]), float(level["size"])
        fee_per_share = rate * (p * (1 - p)) ** exp  # Polymarket taker fee, paid in USDC
        take = min(size, (dollars - spent) / (p + fee_per_share))
        shares += take
        spent += take * (p + fee_per_share)
        fees += take * fee_per_share
        if dollars - spent < 0.01:
            break
    min_size = float(book.get("min_order_size") or 0)
    if shares <= 0 or shares < min_size or dollars - spent > 0.01:
        return None  # book too thin to fill the whole bet
    return {"shares": shares, "cost": spent, "fees": fees, "avg_price": (spent - fees) / shares}


def in_band(price, entry):
    """Price we'd pay may be at most 2c above, or 4c below, the traders' average entry."""
    return -MAX_BELOW - 1e-9 <= price - entry <= MAX_ABOVE + 1e-9


def place(state, pick, info, stake, existing=None):
    """Paper-buy `stake` of the bankroll on this pick (or top up an existing bet) on
    whichever US venue (Polymarket US or Kalshi) is cheapest, fees included.
    Returns the bet, or None if it was skipped."""
    label = f"{pick['Pick']} / {pick['Market'][:50]}"
    roll = bankroll(state)
    dollars = round(stake * roll, 2) if existing is None else round(0.01 * roll, 2)
    exposure = sum(b["cost"] for b in open_bets(state))
    if exposure + dollars > MAX_EXPOSURE * roll or dollars > state["cash"]:
        print(f"  skip (50% cap reached): {label}")
        return None
    if existing is not None:  # top-ups stay on the venue we already used
        got = venues.top_up_quote(existing["venue_ref"], dollars)
        choices = [{**existing["venue_ref"], **got}] if got else []
    else:
        choices = venues.quotes(pick, info, dollars)
    if not choices:
        print(f"  skip (not on Polymarket US or Kalshi, or book too thin): {label}")
        return None
    entry = pick["Avg entry price"]
    ok = [q for q in choices if in_band(q["avg_price"], entry)]
    if not ok:
        prices = ", ".join(f"{q['venue']} {q['avg_price']:.3f}" for q in choices)
        print(f"  skip (US prices {prices} outside {entry - MAX_BELOW:.2f}-{entry + MAX_ABOVE:.2f}): {label}")
        return None
    fill = ok[0]  # cheapest all-in cost per share that passes the price rule
    intl = simulate_buy(pick["token_id"], dollars, info.get("feeSchedule"))  # for comparison only
    state["cash"] -= fill["cost"]
    if existing is not None:
        existing["shares"] += fill["shares"]
        existing["cost"] = round(existing["cost"] + fill["cost"], 2)
        existing["fees"] = round(existing["fees"] + fill["fees"], 4)
        existing["avg_price"] = round((existing["cost"] - existing["fees"]) / existing["shares"], 4)
        existing["stake"] = 0.02
        existing["topped_up_at"] = now().isoformat()
        existing["top_up_bettors"] = pick["Bettors"]  # "bettors" stays the count when first placed
        existing["top_up_avg_bankroll_pct"] = pick["Avg bankroll %"]
        print(f"  TOP UP to 2% on {fill['venue']}: +${fill['cost']:.2f}  {label}")
        return existing
    state["bets"].append({
        "placed_at": now().isoformat(), "status": "open", "stake": stake,
        "pick": pick["Pick"], "market": pick["Market"], "link": pick["Link"],
        "market_type": info.get("sportsMarketType"), "condition_id": pick["condition_id"], "token_id": pick["token_id"],
        "outcome_index": pick["outcome_index"], "ends": decided_by(info).isoformat(),
        "bettors": pick["Bettors"], "avg_bankroll_pct": pick["Avg bankroll %"],
        "traders_entry": pick["Avg entry price"], "avg_price": round(fill["avg_price"], 4),
        "shares": fill["shares"], "cost": round(fill["cost"], 2), "fees": round(fill["fees"], 4),
        "venue": fill["venue"],
        "venue_ref": {k: fill[k] for k in ("venue", "ref", "long", "yes", "fee_rate") if k in fill},
        # what the same bet would have cost per share on each venue (fees included)
        "quotes": {q["venue"]: round(q["cost"] / q["shares"], 4) for q in choices},
        "intl_cost_per_share": round(intl["cost"] / intl["shares"], 4) if intl else None,
        "payout": None, "profit": None, "settled_at": None, "topped_up_at": None,
    })
    others = ", ".join(f"{q['venue']} {q['cost'] / q['shares']:.3f}" for q in choices[1:])
    print(f"  BET {stake:.0%} on {fill['venue']}: ${fill['cost']:.2f} on {pick['Pick']} @ "
          f"{fill['avg_price']:.3f} (fee ${fill['fees']:.2f}){' vs ' + others if others else ''}  {label}")
    return state["bets"][-1]


# Two bets in the same group on one game would partly cancel out, so each game gets at most
# one moneyline-or-spread bet and one total (over/under) bet.
SLOT_GROUPS = {"moneyline": "side", "spreads": "side", "totals": "total"}


def game_slot(link, market_type):
    """(game's Polymarket event link, group) for moneyline/spread/total bets, else None."""
    group = SLOT_GROUPS.get(market_type)
    return (link, group) if link and group else None


def bet_type(b):
    # Bets placed before 2026-10-29 didn't record market_type; their spread and total markets
    # say "Spread:" or "O/U"
    if b.get("market_type"):
        return b["market_type"]
    return "spreads" if b["market"].startswith("Spread:") else "totals" if "O/U" in b["market"] else "moneyline"


def trade(state, picks, tiers):
    cutoff = now() + timedelta(days=MAX_DAYS_TO_END)
    info = market_info({p["condition_id"] for p in picks})
    by_token = {b["token_id"]: b for b in open_bets(state)}
    by_market = {b["condition_id"] for b in open_bets(state)}
    by_slot = {game_slot(b["link"], bet_type(b)) for b in open_bets(state)} - {None}
    for pick in picks:
        stake = stake_for(pick["Bettors"], pick["Avg bankroll %"], tiers)
        if not stake:
            continue
        label = f"{pick['Pick']} / {pick['Market'][:50]} ({pick['Bettors']} bettors, {pick['Avg bankroll %']}%)"
        m = info.get(pick["condition_id"])
        if not m or m.get("closed") or not m.get("acceptingOrders") or not decided_by(m):
            print(f"  skip (market closed): {label}")
            continue
        if decided_by(m) <= now():
            print(f"  skip (game already started): {label}")
            continue
        if decided_by(m) > cutoff:
            print(f"  skip (decided {decided_by(m):%Y-%m-%d}, more than {MAX_DAYS_TO_END} days away): {label}")
            continue
        existing = by_token.get(pick["token_id"])
        if existing:
            if stake == 0.02 and existing["stake"] == 0.01 and existing.get("venue_ref"):
                place(state, pick, m, stake, existing)
            continue
        if pick["condition_id"] in by_market:
            continue  # we already bet the other side; never hold both
        slot = game_slot(pick["Link"], m.get("sportsMarketType"))
        if slot in by_slot:
            kind = "moneyline/spread" if slot[1] == "side" else "total"
            print(f"  skip (already have a {kind} bet on this game): {label}")
            continue
        bet = place(state, pick, m, stake)
        if bet:
            by_token[pick["token_id"]] = bet
            by_market.add(pick["condition_id"])
            if slot:
                by_slot.add(slot)


# ---------------- Statistics tab ----------------
# Null hypothesis: the market price is the true chance of winning. Under it, a bet of `shares`
# bought at pre-fee price q has expected profit shares*q - cost (i.e. minus the fees) and
# variance shares^2 * q * (1 - q). Z = (actual - expected) / SD over all settled bets.
MIN_BETS = 30     # below this, don't read anything into the numbers
REVIEW_AT = 200   # decide whether it works at this many settled bets, not before

TYPE_NAMES = {"moneyline": "Moneyline", "spreads": "Spread", "totals": "Total"}
SPORTS = {"nba": "Basketball", "wnba": "Basketball", "cbb": "Basketball", "ncaab": "Basketball",
          "nfl": "Football", "cfb": "Football", "nhl": "Hockey", "mlb": "Baseball",
          "atp": "Tennis", "wta": "Tennis", "ufc": "MMA", "cs2": "Esports", "lol": "Esports",
          "dota2": "Esports", "val": "Esports", "epl": "Soccer", "lal": "Soccer", "sea": "Soccer",
          "bun": "Soccer", "fl1": "Soccer", "ucl": "Soccer", "uel": "Soccer", "mls": "Soccer"}


def sport_of(b):
    prefix = (b.get("link") or "").rstrip("/").rsplit("/", 1)[-1].split("-")[0]
    return SPORTS.get(prefix, prefix.upper() or "Other")  # unknown leagues show their code


def summarize(bets):
    n = len(bets)
    wins = sum(b["status"] == "won" for b in bets)
    exp_wins = sum(b["avg_price"] for b in bets)
    staked = sum(b["cost"] for b in bets)
    profit = sum(b["profit"] for b in bets)
    exp_profit = sum(b["shares"] * b["avg_price"] - b["cost"] for b in bets)
    sd = math.sqrt(sum(b["shares"] ** 2 * b["avg_price"] * (1 - b["avg_price"]) for b in bets))
    return {"n": n, "wins": wins, "exp_wins": exp_wins, "staked": staked, "profit": profit,
            "exp_profit": exp_profit, "sd": sd,
            "z_market": (profit - exp_profit) / sd if sd else None,  # beat the market's odds?
            "z_money": profit / sd if sd else None}                  # made money after fees?


def verdict(n, z, goal):
    """(css class, plain-English verdict) for one test."""
    if n < MIN_BETS or z is None:
        return "", f"Too few settled bets to judge ({n} so far, need at least {MIN_BETS})."
    when = ("This is the review point: time to decide." if n >= REVIEW_AT
            else f"Not proof yet. Decide at {REVIEW_AT} settled bets.")
    if z >= 2:
        return "good", f"Above 2 SD: the picks might really {goal}. {when}"
    if z <= -2:
        return "bad", f"Below 2 SD: doing worse than bad luck alone would explain. {when}"
    return "", f"Within normal luck: no real evidence either way yet. {when}"


def stats_html(strats):
    """Statistics tab: the strategies side by side. strats = [(config, settled bets), ...]"""
    fmt_z = lambda z: f"{z:+.2f}" if z is not None else "&ndash;"
    money = lambda v: f'<span class="{"up" if v > 0 else "down" if v < 0 else ""}">{v:+,.2f}</span>'
    pct = lambda a, n: f"{a / n:.0%}" if n else "&ndash;"
    sums = [(cfg, summarize(bets), bets) for cfg, bets in strats]

    metrics = [
        ("Settled bets", lambda t: f'{t["n"]} / {REVIEW_AT}'),
        ("Won-lost", lambda t: f'{t["wins"]}-{t["n"] - t["wins"]}'),
        ("Win rate (market expected)",
         lambda t: f'{pct(t["wins"], t["n"])} <span class="muted">({pct(t["exp_wins"], t["n"])})</span>'),
        ("Staked", lambda t: f'${t["staked"]:,.2f}'),
        ("Actual profit", lambda t: money(t["profit"])),
        ("Expected if market is right", lambda t: money(t["exp_profit"])),
        ("Return per $ bet", lambda t: f'{t["profit"] / t["staked"]:+.1%}' if t["staked"] else "&ndash;"),
        ("1 SD (normal luck range)", lambda t: f'&plusmn;${t["sd"]:,.2f}'),
        ("Z: beat the market's odds", lambda t: fmt_z(t["z_market"])),
        ("Z: make money after fees", lambda t: fmt_z(t["z_money"])),
    ]
    head = "".join(f'<th class="num">{cfg["name"]}</th>' for cfg, _, _ in sums)
    body = "".join(f'<tr><td class="pick">{label}</td>'
                   + "".join(f'<td class="num">{f(t)}</td>' for _, t, _ in sums) + "</tr>"
                   for label, f in metrics)
    out = [f'<h2>Head to head</h2><div class="wrap"><table><thead><tr><th></th>{head}</tr></thead>'
           f'<tbody>{body}</tbody></table></div>']

    out.append('<div class="verdicts">')
    for cfg, t, _ in sums:
        for title, z, goal in (("beat the market's odds?", t["z_market"], "beat the market's odds"),
                               ("make money after fees?", t["z_money"], "make money after fees")):
            cls, text = verdict(t["n"], z, goal)
            out.append(f'<div class="verdict {cls}"><h3>{cfg["name"]}: does it {title}</h3>'
                       f'<div class="z">Z = {fmt_z(z)}</div><p>{text}</p></div>')
    out.append("</div>")

    def table(title, bets, key):
        groups = {}
        for b in bets:
            groups.setdefault(key(b), []).append(b)
        rows = []
        for name, gb in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            g = summarize(gb)
            ret = f'{g["profit"] / g["staked"]:+.1%}' if g["staked"] else "&ndash;"
            z_cls = "num" + (" muted" if g["n"] < MIN_BETS else "")
            rows.append(
                f'<tr><td class="pick">{html.escape(str(name))}</td><td class="num">{g["n"]}</td>'
                f'<td class="num">{g["wins"]}-{g["n"] - g["wins"]}</td>'
                f'<td class="num">{pct(g["wins"], g["n"])} <span class="muted">({pct(g["exp_wins"], g["n"])})</span></td>'
                f'<td class="num">{money(g["profit"])}</td><td class="num">{ret}</td>'
                f'<td class="{z_cls}">{fmt_z(g["z_market"])}</td></tr>')
        body = "".join(rows) or '<tr><td colspan="7">None yet.</td></tr>'
        return (f'<h3>{title}</h3><div class="wrap"><table><thead><tr><th>{title[3:]}</th>'
                f'<th class="num">Bets</th><th class="num">Won-lost</th><th class="num">Win % (exp.)</th>'
                f'<th class="num">Profit</th><th class="num">Return</th><th class="num">Z</th>'
                f'</tr></thead><tbody>{body}</tbody></table></div>')

    out.append('<h2>Breakdowns</h2><div class="compare">')
    for cfg, _, bets in sums:
        out.append(f'<div><h2 class="col">{cfg["name"]}</h2>'
                   + table("By bet type", bets, lambda b: TYPE_NAMES.get(bet_type(b), "Other"))
                   + table("By stake", bets, lambda b: f'{b["stake"]:.0%}')
                   + table("By sport", bets, sport_of) + "</div>")
    out.append("</div>")
    out.append(f"""<h2>What these numbers mean</h2><div class="explain">
<p><b>Expected if market is right:</b> each price is the market's guess at the chance of winning
(0.60 = 60%). If those guesses are right, this is what the bets should make on average: a small loss,
the fees.</p>
<p><b>SD (standard deviation):</b> how far luck alone usually moves the result. About 2 in 3 times
the actual profit lands within 1 SD of expected; about 95% of the time within 2 SD.</p>
<p><b>Z:</b> how many SDs the actual result is from the target. Above +2 happens by luck only about
2&ndash;3% of the time, so it hints the picks have a real edge. "Beat the market" compares with the
expected loss from fees; "make money" compares with breaking even, which is a harder bar.</p>
<p><b>Be patient:</b> with a real 10% edge it takes about 400 bets on average to reach 2 SD. Checking
often and stopping the first time Z touches 2 makes a fluke much more likely, so the decision point
is fixed at {REVIEW_AT} settled bets. Group Z-scores in grey have under {MIN_BETS} bets and mean little.</p>
<p><b>Comparing strategies:</b> both bet on the same picks feed, so they often share bets and their
results move together. A gap between them only means something once both have many settled bets.</p>
</div>""")
    return "\n".join(out)

REPORT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paper Trading Results</title>
<style>
  :root { --bg:#f7f7f5; --card:#fff; --text:#1d1d1f; --muted:#6e6e73; --line:#e5e5ea;
          --accent:#2f6fed; --up:#1a8a4a; --down:#c4372b; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#141416; --card:#1e1e21; --text:#f2f2f4; --muted:#9a9aa0; --line:#2e2e33;
            --accent:#6b9bff; --up:#4cc27e; --down:#ff6b5e; } }
  body { margin:0; padding:24px 16px; background:var(--bg); color:var(--text);
         font:15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  main { max-width:1280px; margin:0 auto; }
  h1 { font-size:24px; margin:0 0 4px; } h2 { font-size:17px; margin:28px 0 10px; }
  .sub { color:var(--muted); margin:0 0 20px; }
  .tiles { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:12px; }
  .tile { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
  .tile b { display:block; font-size:22px; font-variant-numeric:tabular-nums; }
  .tile > span { color:var(--muted); font-size:13px; }
  .wrap { background:var(--card); border:1px solid var(--line); border-radius:12px; overflow-x:auto; }
  table { width:100%; border-collapse:collapse; }
  th, td { padding:9px 12px; text-align:left; border-bottom:1px solid var(--line); white-space:nowrap; }
  td.market { white-space:normal; min-width:240px; }
  th { font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }
  tr:last-child td { border-bottom:0; }
  .num { text-align:right; font-variant-numeric:tabular-nums; }
  td.pick { font-weight:700; }
  .up { color:var(--up); } .down { color:var(--down); }
  a { color:var(--accent); text-decoration:none; }
  .note { color:var(--muted); font-size:13px; margin-top:16px; }
  .tabs { display:flex; gap:4px; border-bottom:1px solid var(--line); margin:0 0 18px; }
  .tabs button { border:0; background:none; color:var(--muted); font:inherit; font-weight:600;
                 padding:8px 14px; border-bottom:2px solid transparent; margin-bottom:-1px; cursor:pointer; }
  .tabs button.on { color:var(--text); border-bottom-color:var(--accent); }
  .verdicts { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:12px; }
  .verdict { background:var(--card); border:1px solid var(--line); border-left:4px solid var(--muted);
             border-radius:12px; padding:14px 16px; }
  .verdict.good { border-left-color:var(--up); } .verdict.bad { border-left-color:var(--down); }
  .verdict h3 { font-size:14px; margin:0 0 6px; color:var(--muted); font-weight:600; }
  .verdict p { margin:0; }
  .verdict .z { font-size:22px; font-weight:700; font-variant-numeric:tabular-nums; margin-bottom:4px; }
  .explain { color:var(--muted); font-size:14px; }
  .explain p { margin:8px 0; }
  .muted { color:var(--muted); }
  .compare { display:grid; grid-template-columns:repeat(auto-fit, minmax(420px, 1fr)); gap:20px; }
  .compare h2.col { margin-top:4px; }
  .compare h3 { font-size:15px; margin:18px 0 8px; }
  .compare th, .compare td { padding:8px 7px; font-size:14px; }
  .compare th { font-size:11px; }
  @media (max-width: 640px) { .compare, .verdicts { grid-template-columns:1fr; } }
__ODDS_CSS__
</style></head><body><main>
<h1>Paper Trading Results</h1>
<p class="sub">__SUB__</p>
<div class="tabs" role="tablist"><button data-tab="a" class="on">Strategy A</button>
<button data-tab="b">Strategy B</button><button data-tab="stats">Statistics</button></div>
__ODDS_TOGGLE__
<section id="tab-a">__BETS_A__</section>
<section id="tab-b" hidden>__BETS_B__</section>
<section id="tab-stats" hidden>__STATS__</section>
<p class="note">Pretend money only. Each bet goes to whichever US venue (Polymarket US or Kalshi)
had the lowest price including fees, from the real order books.
Bankroll = cash + open bets at cost. Research only, not financial advice.</p>
</main>
<script>
function showTab(name) {
  document.querySelectorAll(".tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === name));
  document.querySelectorAll("section[id^=tab-]").forEach(s => s.hidden = s.id !== "tab-" + name);
  try { localStorage.setItem("report-tab", name); } catch (e) {}
}
document.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));
try {
  const t = localStorage.getItem("report-tab");
  if (t && document.querySelector(`.tabs button[data-tab="${t}"]`)) showTab(t);
} catch (e) {}
</script></body></html>
"""


VENUE_NAMES = {"kalshi": "Kalshi", "polymarket_us": "Polymarket US",
               "polymarket_intl": "Polymarket (intl, before US switch)"}


def short_time(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%b %d %I:%M %p")


def bets_html(cfg, state):
    """One strategy's tab: rules, tiles, open bets, settled bets."""
    bets = state["bets"]
    esc = html.escape
    done = [b for b in bets if b["status"] != "open"]
    wins = sum(b["status"] == "won" for b in done)
    profit = sum(b["profit"] for b in done)
    staked = sum(b["cost"] for b in done)
    roll = bankroll(state)
    change = roll - state["start_bankroll"]
    tiles = [
        (f"${roll:,.2f}", "Bankroll"),
        (f'<span class="{"up" if change >= 0 else "down"}">{change:+,.2f}</span>', "Since start"),
        (f"{wins}-{len(done) - wins}", "Won-lost"),
        (f"{profit / staked:+.1%}" if staked else "&ndash;", "Return per $ bet"),
        (f"{len(open_bets(state))}", "Open bets"),
        (f"${state['cash']:,.2f}", "Cash"),
    ]
    tiles_html = "".join(f'<div class="tile"><b>{v}</b><span>{k}</span></div>' for v, k in tiles)

    def bettors_text(b):
        """Bettors when placed, and at the top-up if there was one (older top-ups didn't record it)."""
        if not b.get("topped_up_at"):
            return str(b["bettors"])
        return f'{b["bettors"]} &rarr; {b.get("top_up_bettors") or "topped up"}'

    def venue_name(b):
        return VENUE_NAMES.get(b.get("venue", "polymarket_intl"))

    def link(b):
        return f'<a href="{esc(b["link"])}" target="_blank">View &rarr;</a>' if b["link"] else ""

    open_rows = "".join(
        f'<tr><td>{short_time(b["placed_at"])}</td><td class="pick">{esc(str(b["pick"]))}</td>'
        f'<td class="market">{esc(b["market"])}</td>'
        f'<td class="num">{b["stake"]:.0%}</td><td class="num">{bettors_text(b)}</td>'
        f'<td class="num">{b["avg_bankroll_pct"]:.1f}%</td><td class="num">{scanner.px(b["avg_price"], 3)}</td>'
        f'<td class="num">${b["cost"]:.2f}</td><td>{venue_name(b)}</td>'
        f'<td>{short_time(b["ends"]) if b["ends"] else ""}</td><td>{link(b)}</td></tr>'
        for b in reversed(open_bets(state))) or '<tr><td colspan="11">None yet.</td></tr>'
    done_rows = "".join(
        f'<tr><td>{short_time(b["placed_at"])}</td><td class="pick">{esc(str(b["pick"]))}</td>'
        f'<td class="market">{esc(b["market"])}</td>'
        f'<td class="{"up" if b["status"] == "won" else "down" if b["status"] == "lost" else ""}">'
        f'{b["status"].title()}</td><td class="num">{b["stake"]:.0%}</td>'
        f'<td class="num">{scanner.px(b["avg_price"], 3)}</td><td class="num">${b["cost"]:.2f}</td>'
        f'<td class="num">${b["payout"]:.2f}</td>'
        f'<td class="num {"up" if b["profit"] > 0 else "down" if b["profit"] < 0 else ""}">'
        f'{b["profit"]:+.2f}</td><td>{venue_name(b)}</td></tr>'
        for b in reversed(done)) or '<tr><td colspan="10">None yet.</td></tr>'
    return f"""<p class="sub"><b>{cfg["name"]}.</b> {esc(cfg["rules"])}
Started {short_time(state["started_at"])} with ${state["start_bankroll"]:,.0f}.</p>
<div class="tiles">{tiles_html}</div>
<h2>Open bets</h2>
<div class="wrap"><table><thead><tr><th>Placed</th><th>Pick</th><th>Market</th>
<th class="num">Stake</th><th class="num">Bettors</th><th class="num">Avg bankroll %</th>
<th class="num">Price</th><th class="num">Cost</th><th>Venue</th><th>Ends</th><th></th></tr></thead><tbody>{open_rows}</tbody></table></div>
<h2>Settled bets</h2>
<div class="wrap"><table><thead><tr><th>Placed</th><th>Pick</th><th>Market</th><th>Result</th>
<th class="num">Stake</th><th class="num">Price</th><th class="num">Cost</th>
<th class="num">Paid</th><th class="num">Profit</th><th>Venue</th></tr></thead><tbody>{done_rows}</tbody></table></div>"""


def write_csv(path, bets):
    with open(path, "w", newline="") as f:
        if bets:
            fields = list(dict.fromkeys(k for b in bets for k in b))  # older bets lack newer columns
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(bets)


def write_reports(states):
    """states = {strategy key: state}. Writes each strategy's CSV and the shared report page."""
    for key, state in states.items():
        write_csv(os.path.join(HERE, STRATEGIES[key]["csv"]), state["bets"])
    settled = [(STRATEGIES[k], [b for b in st["bets"] if b["status"] != "open"]) for k, st in states.items()]
    sub = f"Pretend-money tests of two staking strategies. Last updated {time.strftime('%b %d %I:%M %p')}."
    page = (REPORT.replace("__ODDS_CSS__", scanner.ODDS_CSS + scanner.MOBILE_CSS)
            .replace("__ODDS_TOGGLE__", scanner.ODDS_TOGGLE)
            .replace("</body>", scanner.ODDS_JS + scanner.MOBILE_JS + "</body>")
            .replace("__SUB__", html.escape(sub))
            .replace("__BETS_A__", bets_html(STRATEGIES["a"], states["a"]))
            .replace("__BETS_B__", bets_html(STRATEGIES["b"], states["b"]))
            .replace("__STATS__", stats_html(settled)))
    with open(os.path.join(HERE, "paper_report.html"), "w") as f:
        f.write(page)
    copy_to_icloud("paper_report.html")


def copy_to_icloud(name):
    """Copy an output file to iCloud Drive, when running on a Mac with iCloud Drive (not on GitHub)."""
    if not os.path.isdir(os.path.dirname(ICLOUD)):
        return
    try:
        os.makedirs(ICLOUD, exist_ok=True)
        shutil.copy(os.path.join(HERE, name), os.path.join(ICLOUD, name))
    except OSError as e:
        print(f"  Could not copy {name} to iCloud Drive: {e}")


scanner_args = scanner.build_parser().parse_args([])  # scanner defaults...
scanner_args.max_move = 1  # ...except the price rule, applied below to the real US-venue price


def write_picks(picks):
    """Refresh picks.html (all picks, no price filter) here and in iCloud Drive."""
    scanner.write_html(os.path.join(HERE, "picks.html"), picks, scanner_args)
    copy_to_icloud("picks.html")


def main():
    print(f"\n=== Forward test run {time.strftime('%Y-%m-%d %H:%M')} ===")
    states = {}
    for key, cfg in STRATEGIES.items():
        path = os.path.join(HERE, cfg["state"])
        states[key] = load_state(path)
        settle(states[key])
        save_state(states[key], path)
    picks, _ = scanner.find_picks(scanner_args)
    write_picks(picks)
    for key, cfg in STRATEGIES.items():
        print(f"-- {cfg['name']} --")
        trade(states[key], picks, cfg["tiers"])
        save_state(states[key], os.path.join(HERE, cfg["state"]))
    write_reports(states)
    for key, cfg in STRATEGIES.items():
        st = states[key]
        print(f"{cfg['name']}: bankroll ${bankroll(st):,.2f} (cash ${st['cash']:,.2f}, "
              f"{len(open_bets(st))} open bets)")
    print("Wrote paper_report.html and picks.html")


if __name__ == "__main__":
    main()
