#!/usr/bin/env python3
"""
Forward test (paper trading) for the smart-money scanner.

Each run:
1. Settles open paper bets whose markets have been decided.
2. Runs the scanner (polymarket_smart_money.py defaults: top 100 sports traders by
   monthly PnL, hedges and crypto bots removed).
3. Keeps picks that meet a staking tier, haven't started, and are decided within 3 days.
4. "Buys" them with pretend money on the cheapest US venue (Polymarket US, Kalshi or ProphetX)
   (see venues.py), from real order books with fees, if the price is no more than 1c
   above or 4c below the traders' average entry.
5. Does steps 1, 3 and 4 separately for each strategy (own $1,000 pretend bankroll).
6. Writes paper_trades.json / paper_trades_b.json (state), matching CSVs and paper_report.html.

Real orders (Strategy A only) are placed by live.py. Runs every 10 minutes on GitHub Actions.

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

import claude_strategy
import pick_log
import live
import polymarket_smart_money as scanner
import venues

HERE = os.path.dirname(os.path.abspath(__file__))
CLOB = "https://clob.polymarket.com"
# On a Mac, copies of the reports also go here (iCloud Drive), for viewing on a phone
ICLOUD = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/Polymarket")

START_BANKROLL = 1000.0
MAX_DAYS_TO_END = 3      # only markets that end within this many days
MAX_EXPOSURE = 0.50      # at most this share of the bankroll in open bets at once
MAX_ABOVE = 0.01         # pay at most 1c more than the traders' average entry (2c before 2026-11-10 and on 11-13)...
MAX_BELOW = 0.04         # ...and at most 4c less
MIN_PRICE = 100 / 350   # never bet longer than +250 (~28.6c). 35c from 2026-11-10, 30c then +250 from 2026-11-11
A_MAX_PRICE = 0.75       # Strategy A and real bets: never pay more than 75c (added 2026-11-11)
UNDERDOG_BELOW = 0.40    # bets on picks priced under this get smaller (see size_multiplier)

LIVE_LOG = "live_log.json"  # every live order sent, the venue's reply, skips and errors
LOG_KEEP = 2000             # entries kept

LIVE = {"name": "Real bets", "state": "live_trades.json", "csv": "live_trades.csv",
        "rules": "Real money on Kalshi, Polymarket US and ProphetX, copying each Strategy A paper bet with the "
                 "same price rule. Since Nov 10 every real bet is a flat 1.5% of the real balance (smaller on "
                 "35-40c picks), with no top-ups. Since Nov 11, never over 75c."}

# Each strategy has its own pretend bankroll and state file; all other rules are shared.
# tiers: (stake as share of current bankroll, [(min bettors, min avg bankroll %), ...]),
# checked top to bottom; the first tier with any matching rule wins.
STRATEGIES = {
    "a": {"name": "Strategy A", "state": "paper_trades.json", "csv": "paper_trades.csv",
          "rules": "Stake by bettors and avg bankroll %. 2%: 3+ bettors & 35%+, 4+ & 25%+, or 5+ & 20%+. "
                   "1%: 3+ & 20%+, 4+ & 15%+, or 5+ & 10%+. Since Nov 11, never pays over 75c.",
          "tiers": [(0.02, [(3, 35), (4, 25), (5, 20)]),
                    (0.01, [(3, 20), (4, 15), (5, 10)])],
          "max_price": A_MAX_PRICE},
    "b": {"name": "Strategy B", "state": "paper_trades_b.json", "csv": "paper_trades_b.csv",
          "rules": "Stake by number of bettors only. 2%: 5+ bettors. 1%: 4 bettors. "
                   "Avg bankroll % is ignored.",
          "tiers": [(0.02, [(5, 0)]),
                    (0.01, [(4, 0)])]},
    # net = bettors minus top traders on the other side of the same market
    "c": {"name": "Strategy C", "state": "paper_trades_c.json", "csv": "paper_trades_c.csv",
          "rules": "Stake by NET bettors (bettors minus top traders on the other side). "
                   "2%: net 5+. 1%: net 4. Avg bankroll % is ignored.",
          "tiers": [(0.02, [(5, 0)]),
                    (0.01, [(4, 0)])],
          "net": True},
    # late money: only 90 to 15 minutes before the start, when top traders have mostly placed their bets
    "d": {"name": "Strategy D", "state": "paper_trades_d.json", "csv": "paper_trades_d.csv",
          "rules": "Late bets only: placed 90 to 15 minutes before the game starts. Stake by NET bettors "
                   "(bettors minus top traders on the other side): 1% at net 3-4, 2% at net 5+, and the "
                   "average bankroll % must be 10%+. Skipped unless at least two-thirds of the top "
                   "traders' money in that market is on our side.",
          "tiers": [(0.02, [(5, 10)]),
                    (0.01, [(3, 10)])],
          "net": True, "window": (15, 90), "min_money_share": 2 / 3},
    # like C, but only traders with conviction count (10%+ of their bankroll on the pick)
    "e": {"name": "Strategy E", "state": "paper_trades_e.json", "csv": "paper_trades_e.csv",
          "rules": "Counts only traders with conviction: a position worth 10%+ of their bankroll. "
                   "Stake by NET conviction bettors (ours minus the other side's): 1% at net 3-4, 2% at "
                   "net 5+. Bets as soon as a pick qualifies. 'Bettors' = conviction bettors.",
          "tiers": [(0.02, [(5, 0)]),
                    (0.01, [(3, 0)])],
          "net": True, "count": ("Conviction bettors", "Conviction against")},
    # copies only traders whose bets beat the closing price (see claude_strategy.py)
    "claude": {"name": "Claude", "state": "paper_trades_claude.json", "csv": "paper_trades_claude.csv",
               "rules": "Copies only traders with a proven record of beating the closing price (see the "
                        "report card below). 1% with one qualified trader on a pick, 2% with two or more. "
                        "Only positions they first bought in the last 6 hours. 'Bettors' = qualified traders.",
               "tiers": [(0.02, [(2, 0)]),
                         (0.01, [(1, 0)])],
               "claude": True},
}
CARD = "trader_card.json"  # the Claude strategy's trader report card


def now():
    return datetime.now(timezone.utc)


def size_multiplier(price):
    """Shrink bets on underdogs so each carries about the same luck risk as a 40c bet.
    One bet's SD is stake * sqrt((1-p)/p), so scale the stake by sqrt(p/(1-p)), normalised to 1
    at 40c: 40c -> 1.00, 30c -> 0.80, 20c -> 0.61, 10c -> 0.41. Added 2026-10-30."""
    if price >= UNDERDOG_BELOW:
        return 1.0
    p = max(price, 0.01)
    return math.sqrt((p / (1 - p)) / (UNDERDOG_BELOW / (1 - UNDERDOG_BELOW)))


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


def settle(state, real=False):
    """Pay out bets whose market has resolved, on whichever venue they were placed.
    real=True for real bets: ProphetX takes its fee out of the winnings then (paper bets
    already paid the expected fee up front). Returns the bets settled in this call."""
    pending = open_bets(state)
    settled = []
    # bets from before the US-venue switch were "placed" on international Polymarket; ProphetX has
    # no public results, so its bets settle by the international result of the same game and line
    intl = [b for b in pending if b.get("venue", "polymarket_intl") in ("polymarket_intl", "prophetx")]
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
        if real and b.get("venue") == "prophetx" and payout > b["cost"] and not b.get("fees"):
            fee = round(venues.PX_FEE * (payout - b["cost"]), 2)
            payout -= fee
            b["fees"] = fee
        b["payout"] = round(payout, 2)
        b["profit"] = round(payout - b["cost"], 2)
        b["status"] = "won" if payout > b["cost"] else "lost" if payout < b["cost"] else "push"
        b["settled_at"] = now().isoformat()
        state["cash"] += payout
        print(f"  settled {b['status'].upper()}: {b['pick']} / {b['market'][:50]}  "
              f"cost ${b['cost']:.2f} -> paid ${payout:.2f}")
        settled.append(b)
    return settled


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


def in_band(price, entry, max_price=1.0):
    """Price we'd pay may be at most 1c above, or 4c below, the traders' average entry,
    never longer than +250 (~28.6c), and never over max_price (75c for Strategy A)."""
    return (MIN_PRICE - 1e-9 <= price <= max_price + 1e-9
            and -MAX_BELOW - 1e-9 <= price - entry <= MAX_ABOVE + 1e-9)


def place(state, pick, info, stake, existing=None, on_bet=None, max_price=1.0):
    """Paper-buy `stake` of the bankroll on this pick (or top up an existing bet) on
    whichever US venue (Polymarket US or Kalshi) is cheapest, fees included.
    on_bet, if given, is called with each bet placed (live.py mirrors Strategy A's).
    Returns the bet, or None if it was skipped."""
    label = f"{pick['Pick']} / {pick['Market'][:50]}"
    roll = bankroll(state)
    mult = size_multiplier(pick["Price now"])  # current Polymarket price, close to the US-venue one
    dollars = round((stake if existing is None else 0.01) * roll * mult, 2)
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
        print(f"  skip (not on Polymarket US, Kalshi or ProphetX, or book too thin): {label}")
        return None
    entry = pick["Avg entry price"]
    ok = [q for q in choices if in_band(q["avg_price"], entry, max_price)]
    if not ok:
        prices = ", ".join(f"{q['venue']} {q['avg_price']:.3f}" for q in choices)
        print(f"  skip (US prices {prices} outside {max(entry - MAX_BELOW, MIN_PRICE):.2f}-{min(entry + MAX_ABOVE, max_price):.2f}): {label}")
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
        existing["top_up_size_multiplier"] = round(mult, 3)
        print(f"  TOP UP to 2% on {fill['venue']}: +${fill['cost']:.2f}  {label}")
        if on_bet:
            on_bet({"bet": existing, "fill": fill, "share": dollars / roll, "top_up": True, "entry": entry})
        return existing
    state["bets"].append({
        "placed_at": now().isoformat(), "status": "open", "stake": stake,
        "pick": pick["Pick"], "market": pick["Market"], "link": pick["Link"],
        "market_type": info.get("sportsMarketType"), "size_multiplier": round(mult, 3), "condition_id": pick["condition_id"], "token_id": pick["token_id"],
        "outcome_index": pick["outcome_index"], "ends": decided_by(info).isoformat(),
        "bettors": pick["Bettors"], "avg_bankroll_pct": pick["Avg bankroll %"],
        "traders_entry": pick["Avg entry price"], "avg_price": round(fill["avg_price"], 4),
        "shares": fill["shares"], "cost": round(fill["cost"], 2), "fees": round(fill["fees"], 4),
        "venue": fill["venue"],
        "venue_ref": {k: fill[k] for k in ("venue", "ref", "long", "yes", "fee_rate", "event_id") if k in fill},
        # what the same bet would have cost per share on each venue (fees included)
        "quotes": {q["venue"]: round(q["cost"] / q["shares"], 4) for q in choices},
        "intl_cost_per_share": round(intl["cost"] / intl["shares"], 4) if intl else None,
        "payout": None, "profit": None, "settled_at": None, "topped_up_at": None,
    })
    others = ", ".join(f"{q['venue']} {q['cost'] / q['shares']:.3f}" for q in choices[1:])
    print(f"  BET {stake:.0%} on {fill['venue']}: ${fill['cost']:.2f} on {pick['Pick']} @ "
          f"{fill['avg_price']:.3f} (fee ${fill['fees']:.2f}){' vs ' + others if others else ''}  {label}")
    if on_bet:
        on_bet({"bet": state["bets"][-1], "fill": fill, "share": dollars / roll, "top_up": False, "entry": entry})
    return state["bets"][-1]


# Two bets in the same group on one game would partly cancel out, so each game gets at most
# one moneyline-or-spread bet and one total (over/under) bet.
SLOT_GROUPS = {"moneyline": "side", "spreads": "side", "totals": "total"}


def game_slot(link, market_type):
    """(game's Polymarket event link, group) for moneyline/spread/total bets, else None."""
    group = SLOT_GROUPS.get(market_type)
    link = venues.base_event(link)  # soccer spreads live on the game's "-more-markets" page
    return (link, group) if link and group else None


def bet_type(b):
    # Bets placed before 2026-10-29 didn't record market_type; their spread and total markets
    # say "Spread:" or "O/U"
    if b.get("market_type"):
        return b["market_type"]
    return "spreads" if b["market"].startswith("Spread:") else "totals" if "O/U" in b["market"] else "moneyline"


def trade(state, picks, tiers, on_bet=None, net=False, window=None, min_money_share=0, count=None,
          max_price=1.0):
    """window = (min, max) minutes before the start, to bet only then (Strategy D).
    min_money_share = least share of the top traders' money in the market that must be on our side.
    count = (bettors key, against key) to count other columns than Bettors / Against (Strategy E)."""
    cutoff = now() + timedelta(days=MAX_DAYS_TO_END)
    info = market_info({p["condition_id"] for p in picks})
    by_token = {b["token_id"]: b for b in open_bets(state)}
    by_market = {b["condition_id"] for b in open_bets(state)}
    by_slot = {game_slot(b["link"], bet_type(b)) for b in open_bets(state)} - {None}
    for pick in picks:
        if count:
            pick = {**pick, "Bettors": pick.get(count[0], 0), "Against": pick.get(count[1], 0)}
        bettors = pick["Bettors"] - (pick.get("Against", 0) if net else 0)
        stake = stake_for(bettors, pick["Avg bankroll %"], tiers)
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
        if window:
            mins = (decided_by(m) - now()).total_seconds() / 60
            if not window[0] <= mins <= window[1]:
                continue  # outside the betting window (no message: most picks are)
        if min_money_share:
            ours, theirs = pick.get("Total $ in", 0), pick.get("Against $ in", 0)
            if ours < min_money_share * (ours + theirs):
                print(f"  skip (only {ours / (ours + theirs):.0%} of top traders' money on this side): {label}")
                continue
        if pick.get("Against", 0) >= pick["Bettors"]:  # never bet the minority side, or a tie
            print(f"  skip (other side has {pick['Against']} top traders vs {pick['Bettors']}): {label}")
            continue
        existing = by_token.get(pick["token_id"])
        if existing:
            if stake == 0.02 and existing["stake"] == 0.01 and existing.get("venue_ref"):
                place(state, pick, m, stake, existing, on_bet, max_price)
            continue
        if pick["condition_id"] in by_market:
            continue  # we already bet the other side; never hold both
        slot = game_slot(pick["Link"], m.get("sportsMarketType"))
        if slot in by_slot:
            kind = "moneyline/spread" if slot[1] == "side" else "total"
            print(f"  skip (already have a {kind} bet on this game): {label}")
            continue
        bet = place(state, pick, m, stake, on_bet=on_bet, max_price=max_price)
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


def real_vs_paper(live_state, paper_state):
    """Real bets next to the Strategy A paper bets they copied: prices paid, fills, results."""
    real = live_state["bets"]
    orders = live_state.get("orders") or []
    if not real and not orders:
        return '<h2>Real vs paper</h2><p class="muted">No real bets yet.</p>'
    paper = {b["token_id"]: b for b in paper_state["bets"]}
    priced = [b for b in real if b.get("paper_price")]
    gap = sum(b["avg_price"] - b["paper_price"] for b in priced) / len(priced) if priced else None
    done = [b for b in real if b["status"] in ("won", "lost", "push") and b["token_id"] in paper
            and paper[b["token_id"]]["status"] != "open"]
    ret = lambda bets: sum(b["profit"] for b in bets) / sum(b["cost"] for b in bets) if bets else None
    pr = lambda v: f"{v:+.1%}" if v is not None else "&ndash;"
    full = sum(o["filled"] >= o["ordered"] for o in orders)
    some = sum(0 < o["filled"] < o["ordered"] for o in orders)
    rows = [
        ("Orders sent", f"{len(orders)}: {full} filled, {some} partly, {len(orders) - full - some} not filled"),
        ("Avg price paid vs paper (same bets)",
         f"{gap * 100:+.1f}&cent; per contract" if gap is not None else "&ndash;"),
        ("Return per $ on the same settled bets",
         f"real {pr(ret(done))} vs paper {pr(ret([paper[b['token_id']] for b in done]))} ({len(done)} bets)"),
    ]
    body = "".join(f'<tr><td class="pick">{k}</td><td class="num">{v}</td></tr>' for k, v in rows)
    return (f'<h2>Real vs paper</h2><div class="wrap"><table><thead><tr><th></th><th class="num">Value</th>'
            f'</tr></thead><tbody>{body}</tbody></table></div>')


def stats_html(strats, extra=""):
    """Statistics tab, phone first: a card per strategy, two comparison charts, breakdowns as bars,
    and the full numbers table + explanations in tap-to-open sections.
    strats = [(config, settled bets), ...]"""
    esc = html.escape
    fmt_z = lambda z: f"{z:+.2f}" if z is not None else "&ndash;"
    money = lambda v: f'<span class="{"up" if v > 0 else "down" if v < 0 else ""}">{"+" if v >= 0 else "&minus;"}${abs(v):,.2f}</span>'
    pct = lambda a, n: f"{a / n:.0%}" if n else "&ndash;"
    ret = lambda t: t["profit"] / t["staked"] if t["staked"] else None
    sums = [(cfg, summarize(bets), bets) for cfg, bets in strats]

    def badge(t):
        z = t["z_market"]
        if t["n"] < MIN_BETS or z is None:
            return '<span class="pill">Too early</span>'
        if z >= 2:
            return '<span class="pill good">&#9650; Possible edge</span>'
        if z <= -2:
            return '<span class="pill bad">&#9660; Worse than luck</span>'
        return '<span class="pill">Within normal luck</span>'

    def spark(bets):
        """Running profit, one point per settled bet, with a zero line."""
        bets = sorted(bets, key=lambda b: b.get("settled_at") or b["placed_at"])
        if len(bets) < 2:
            return '<div class="spark empty">Chart appears after 2 settled bets</div>'
        run, pts = 0.0, [0.0]
        for b in bets:
            run += b["profit"]
            pts.append(run)
        lo, hi = min(pts + [0]), max(pts + [0])
        span = (hi - lo) or 1
        W, H = 300, 56
        y = lambda v: 4 + (H - 8) * (hi - v) / span
        xy = " ".join(f"{i * W / (len(pts) - 1):.1f},{y(v):.1f}" for i, v in enumerate(pts))
        end = "up" if run >= 0 else "down"
        return (f'<svg class="spark" viewBox="0 0 {W} {H}" preserveAspectRatio="none" role="img" '
                f'aria-label="Running profit over {len(bets)} settled bets, now {run:+.2f} dollars">'
                f'<line x1="0" x2="{W}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="zero"/>'
                f'<polyline points="{xy}" class="line {end}" vector-effect="non-scaling-stroke"/></svg>')

    def card(cfg, t):
        if not t["n"]:
            return (f'<div class="scard"><div class="shead"><b>{esc(cfg["name"])}</b>{badge(t)}</div>'
                    f'<div class="ssub">No settled bets yet.</div></div>')
        r = ret(t)
        progress = min(t["n"] / REVIEW_AT, 1)
        return f"""<div class="scard">
<div class="shead"><b>{esc(cfg["name"])}</b>{badge(t)}</div>
<div class="sbig">{money(t["profit"])}</div>
<div class="ssub">{f"{r:+.1%} per $ bet" if r is not None else "No settled bets yet"} &middot; {t["wins"]}-{t["n"] - t["wins"]}
 &middot; won {pct(t["wins"], t["n"])} <span class="muted">(market said {pct(t["exp_wins"], t["n"])})</span></div>
{spark(t["bets"])}
<div class="prog" role="img" aria-label="{t["n"]} of {REVIEW_AT} settled bets"><i style="width:{progress:.0%}"></i></div>
<div class="ssub">{t["n"]} of {REVIEW_AT} settled bets before the verdict</div>
</div>"""

    def bars(rows, lim, fmt, marks=(), ticks=()):
        """Horizontal bars from a zero line in the middle. rows = [(label, value or None, faded, tooltip)]."""
        out = []
        for m in marks:  # reference lines, e.g. the +-2 SD lines
            out.append(f'<i class="mark" style="left:{50 + 50 * m / lim:.2f}%"></i>')
        marks_html = "".join(out)
        body = []
        for label, v, faded, tip in rows:
            if v is None:
                bar, val = "", "&ndash;"
            else:
                w = min(abs(v) / lim, 1) * 50
                side = f"left:50%;width:{w:.2f}%" if v >= 0 else f"right:50%;width:{w:.2f}%"
                bar = f'<i class="bar {"pos" if v >= 0 else "neg"}" style="{side}"></i>'
                val = fmt(v)
            body.append(f'<div class="brow{" faded" if faded else ""}" title="{esc(tip)}"><span class="blabel">{esc(label)}</span>'
                        f'<span class="track"><i class="axis0"></i>{marks_html}{bar}</span>'
                        f'<span class="bval">{val}</span></div>')
        if ticks:  # labels under the reference lines
            labels = "".join(f'<span style="left:{50 + 50 * v / lim:.2f}%">{esc(text)}</span>' for v, text in ticks)
            body.append(f'<div class="brow ticks"><span></span><span class="track">{labels}</span><span></span></div>')
        return '<div class="bars">' + "".join(body) + "</div>"

    for cfg, t, bets in sums:
        t["bets"] = bets
    out = ['<h2>At a glance</h2><div class="scards">'
           + "".join(card(cfg, t) for cfg, t, _ in sums) + "</div>"]

    tip = lambda cfg, t: (f'{cfg["name"]}: {t["n"]} settled bets, profit {t["profit"]:+.2f}, '
                          f'Z beat market {fmt_z(t["z_market"])}, Z make money {fmt_z(t["z_money"])}')
    out.append('<h2>Beating the market&rsquo;s odds?</h2><p class="hint">How many &ldquo;luck units&rdquo; (SD) each '
               'result is above what the prices predicted. Past the <b>+2</b> line hints at a real edge. Faded = under '
               f'{MIN_BETS} settled bets, too early to read.</p>')
    out.append(bars([(cfg["name"], t["z_market"], t["n"] < MIN_BETS, tip(cfg, t)) for cfg, t, _ in sums],
                    4, lambda v: f"{v:+.1f}", marks=(-2, 2), ticks=((-2, "−2"), (0, "0"), (2, "+2"))))
    rets = [ret(t) for _, t, _ in sums if ret(t) is not None]
    lim = max([abs(r) for r in rets] + [0.05])
    out.append('<h2>Return per $ bet</h2><p class="hint">Profit divided by money staked, settled bets only.</p>')
    out.append(bars([(cfg["name"], ret(t), t["n"] < MIN_BETS, tip(cfg, t)) for cfg, t, _ in sums],
                    lim, lambda v: f"{v:+.0%}"))
    out.append(extra)

    # breakdowns: one strategy at a time, picked from a menu
    def breakdown(title, bets, key):
        groups = {}
        for b in bets:
            groups.setdefault(key(b), []).append(b)
        rows = []
        for name, gb in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            g = summarize(gb)
            rows.append((f"{name} ({g['n']})", ret(g), g["n"] < MIN_BETS,
                         f'{name}: {g["n"]} bets, {g["wins"]}-{g["n"] - g["wins"]}, profit {g["profit"]:+.2f}, '
                         f'Z {fmt_z(g["z_market"])}'))
        if not rows:
            return f'<h3>{title}</h3><p class="muted">None yet.</p>'
        lim = max([abs(r[1]) for r in rows if r[1] is not None] + [0.05])
        return f"<h3>{title}</h3>" + bars(rows, lim, lambda v: f"{v:+.0%}")

    opts = "".join(f'<option value="{i}">{esc(cfg["name"])}</option>' for i, (cfg, _, _) in enumerate(sums))
    out.append('<h2>Breakdowns</h2><p class="hint">Return per $ bet by group. Number of bets in brackets; '
               f'faded = under {MIN_BETS} bets.</p>'
               f'<select class="pick-strat" aria-label="Strategy">{opts}</select>')
    for i, (cfg, _, bets) in enumerate(sums):
        out.append(f'<div class="bd" data-i="{i}"{"" if i == 0 else " hidden"}>'
                   + breakdown("By bet type", bets, lambda b: TYPE_NAMES.get(bet_type(b), "Other"))
                   + breakdown("By stake", bets, lambda b: live.pct(b["stake"]))
                   + breakdown("By sport", bets, sport_of) + "</div>")
    out.append("""<script>
document.querySelectorAll(".pick-strat").forEach(s => s.addEventListener("change", () => {
  document.querySelectorAll(".bd").forEach(d => d.hidden = d.dataset.i !== s.value);
}));
</script>""")

    # the full numbers, as a table (also the text version of the charts above)
    metrics = [
        ("Settled bets", lambda t: f'{t["n"]} / {REVIEW_AT}'),
        ("Won-lost", lambda t: f'{t["wins"]}-{t["n"] - t["wins"]}'),
        ("Win rate (market expected)",
         lambda t: f'{pct(t["wins"], t["n"])} <span class="muted">({pct(t["exp_wins"], t["n"])})</span>'),
        ("Staked", lambda t: f'${t["staked"]:,.2f}'),
        ("Actual profit", lambda t: money(t["profit"])),
        ("Expected if market is right", lambda t: money(t["exp_profit"])),
        ("Return per $ bet", lambda t: f'{ret(t):+.1%}' if ret(t) is not None else "&ndash;"),
        ("1 SD (normal luck range)", lambda t: f'&plusmn;${t["sd"]:,.2f}'),
        ("Z: beat the market's odds", lambda t: fmt_z(t["z_market"])),
        ("Z: make money after fees", lambda t: fmt_z(t["z_money"])),
    ]
    head = "".join(f'<th class="num">{esc(cfg["name"])}</th>' for cfg, _, _ in sums)
    body = "".join(f'<tr><td class="pick">{label}</td>'
                   + "".join(f'<td class="num">{f(t)}</td>' for _, t, _ in sums) + "</tr>"
                   for label, f in metrics)
    out.append(f'<details class="more"><summary>All the numbers (table)</summary>'
               f'<div class="wrap"><table class="sticky1"><thead><tr><th></th>{head}</tr></thead>'
               f'<tbody>{body}</tbody></table></div></details>')
    out.append(f"""<details class="more"><summary>What these numbers mean</summary><div class="explain">
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
is fixed at {REVIEW_AT} settled bets.</p>
<p><b>Comparing strategies:</b> they all bet from the same picks feed, so they often share bets and their
results move together. A gap between them only means something once they have many settled bets.</p>
</div></details>""")
    return "\n".join(out)

STATS_CSS = """
  .scards { display:grid; grid-template-columns:repeat(auto-fill, minmax(260px, 1fr)); gap:12px; }
  .scard { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; min-width:0; }
  .shead { display:flex; justify-content:space-between; align-items:center; gap:8px; }
  .shead b { font-size:16px; }
  .sbig { font-size:26px; font-weight:700; margin:6px 0 2px; }
  .ssub { color:var(--muted); font-size:13px; }
  .pill { font-size:12px; font-weight:600; padding:2px 8px; border-radius:999px; white-space:nowrap;
          background:var(--line); color:var(--muted); }
  .pill.good { background:color-mix(in srgb, var(--up) 15%, transparent); color:var(--up); }
  .pill.bad { background:color-mix(in srgb, var(--down) 15%, transparent); color:var(--down); }
  svg.spark { display:block; width:100%; height:56px; margin:10px 0 8px; overflow:visible; }
  svg.spark .zero { stroke:var(--line); stroke-width:1; }
  svg.spark .line { fill:none; stroke-width:2; stroke-linejoin:round; stroke-linecap:round; }
  svg.spark .line.up { stroke:var(--up); } svg.spark .line.down { stroke:var(--down); }
  .spark.empty { height:56px; margin:10px 0 8px; display:flex; align-items:center; justify-content:center;
                 color:var(--muted); font-size:12px; border:1px dashed var(--line); border-radius:8px; }
  .prog { height:6px; background:var(--line); border-radius:3px; overflow:hidden; margin-bottom:4px; }
  .prog i { display:block; height:100%; background:var(--accent); border-radius:3px; }
  .hint { color:var(--muted); font-size:13px; margin:-4px 0 10px; }
  .bars { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:8px 12px; }
  .brow { display:grid; grid-template-columns:minmax(84px, 34%) 1fr 52px; align-items:center; gap:8px;
          min-height:30px; font-size:14px; }
  .brow.faded { opacity:.45; }
  .blabel { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .bval { text-align:right; font-variant-numeric:tabular-nums; font-weight:600; }
  .track { position:relative; height:16px; }
  .track .axis0 { position:absolute; left:50%; top:-4px; bottom:-4px; width:1px; background:var(--muted); opacity:.5; }
  .track .mark { position:absolute; top:-4px; bottom:-4px; width:1px; background:var(--muted); opacity:.35; }
  .track .bar { position:absolute; top:2px; height:12px; border-radius:4px; }
  .track .bar.pos { background:var(--chart-pos); } .track .bar.neg { background:var(--chart-neg); }
  .brow.ticks { min-height:18px; font-size:11px; color:var(--muted); }
  .brow.ticks .track span { position:absolute; transform:translateX(-50%); top:0; }
  .pick-strat { font:inherit; font-size:15px; padding:8px 10px; border-radius:10px; border:1px solid var(--line);
                background:var(--card); color:var(--text); margin-bottom:4px; max-width:100%; }
  details.more { margin-top:18px; }
  details.more summary { cursor:pointer; font-weight:600; padding:10px 0; color:var(--accent); }
  table.sticky1 td:first-child, table.sticky1 th:first-child { position:sticky; left:0; background:var(--card); }
"""


def day_of(b):
    """The calendar day (Eastern on GitHub) a bet belongs to: the day its game started, or the
    day it settled if that came first. Open bets go on their game day."""
    times = [t for t in (b.get("ends"), b.get("settled_at")) if t]
    if not times:
        return None
    first = min(datetime.fromisoformat(t.replace("Z", "+00:00")) for t in times)
    return first.astimezone().strftime("%Y-%m-%d")


def calendar_html(strats):
    """Summary tab (the home page): a Performance card (profit, ROI, record and a running-profit
    chart for the last week / month / year / all), a two-week calendar strip, and a full month
    calendar that opens from it, with each day's bets on tap. A picker switches between Real bets
    and the paper strategies. strats = [(key, config, state), ...]. The bets go into the page as
    data; CAL_JS draws everything."""
    data = {}
    for key, cfg, state in strats:
        rows = []
        for b in state["bets"]:
            day = day_of(b)
            if day:
                rows.append({"d": day, "t": b.get("settled_at") or b["placed_at"], "pick": str(b["pick"]),
                             "market": b["market"], "type": TYPE_NAMES.get(bet_type(b), ""),
                             "status": b["status"], "price": round(b["avg_price"], 4),
                             "cost": round(b["cost"], 2), "profit": round(b.get("profit") or 0, 2)})
        data[key] = {"name": cfg["name"], "bets": rows}
    options = "".join(f'<option value="{k}">{html.escape(v["name"])}</option>' for k, v in data.items())
    blob = json.dumps(data).replace("</", "<\\/")  # must not close the script tag early
    return f"""<select id="cal-strat" class="pick-strat" aria-label="Which bets to show">{options}</select>
<div id="cal-home">
<div class="home-perf">
<h2 class="cal-h">Performance</h2>
<div class="perf">
<div class="perf-nums"><div><span>Profit</span><b id="perf-profit"></b></div>
<div><span>ROI</span><b id="perf-roi"></b></div><div><span>Record</span><b id="perf-record"></b></div></div>
<div id="perf-chart" class="pchart"></div>
<div id="perf-info" class="perf-info"></div>
<div class="perf-range" role="group" aria-label="Time range"><button data-range="7">Week</button>
<button data-range="30">Month</button><button data-range="365">Year</button><button data-range="0">All</button></div>
</div>
</div>
<div class="home-cal">
<h2 class="cal-h">Calendar <span id="strip-title" class="muted"></span></h2>
<div class="cal-grid" id="cal-strip"></div>
<button class="cal-link" id="cal-open">View full calendar &rarr;</button>
</div>
</div>
<div id="cal-full" hidden>
<div class="cal-head"><button id="cal-back" class="cal-round" aria-label="Back to summary">&lsaquo;</button>
<b id="cal-title"></b><span id="cal-total"></span><span class="cal-space"></span>
<button id="cal-prev" class="cal-round small" aria-label="Previous month">&lsaquo;</button>
<button id="cal-next" class="cal-round small" aria-label="Next month">&rsaquo;</button></div>
<div class="full-body"><div class="full-main">
<div class="cal-grid" id="cal-grid"></div>
<p class="hint" style="margin-top:14px">Each day shows the profit or loss on bets from that day's games. Tap a day
to see its bets. Open bets are listed but not counted until they settle.</p>
</div>
<div class="full-side" id="cal-day"></div></div>
</div>
<script type="application/json" id="cal-data">{blob}</script>"""


CAL_CSS = """
  .cal-h { font-size:22px; margin:22px 0 12px; }
  .perf { background:var(--card); border:1px solid var(--line); border-radius:18px; padding:18px 16px 14px; }
  .perf-nums { display:flex; gap:28px; flex-wrap:wrap; }
  .perf-nums > div > span { display:block; color:var(--muted); font-size:14px; }
  .perf-nums b { font-size:21px; font-variant-numeric:tabular-nums; }
  .pchart { position:relative; height:190px; margin:12px 0 6px; touch-action:pan-y; user-select:none;
            -webkit-user-select:none; -webkit-tap-highlight-color:transparent; cursor:crosshair; }
  .pchart svg { position:absolute; left:0; top:26px; width:100%; height:138px; overflow:visible; }
  .pchart .line { fill:none; stroke-width:3; stroke-linejoin:round; stroke-linecap:round; }
  .pchart .line.up { stroke:var(--up); } .pchart .line.down { stroke:var(--down); }
  .pchart .line.after { opacity:.3; }
  .pchart .zero { stroke:var(--line); stroke-width:1; stroke-dasharray:4 4; }
  .pchart .none { height:100%; display:flex; align-items:center; justify-content:center; color:var(--muted); font-size:14px; }
  .plabel { position:absolute; transform:translateX(-50%); font-size:14px; font-weight:600; color:var(--muted);
            font-variant-numeric:tabular-nums; white-space:nowrap; pointer-events:none; }
  .plabel.hi { top:0; } .plabel.lo { bottom:0; }
  .pdate { position:absolute; top:0; right:0; font-size:14px; font-weight:600; color:var(--muted); pointer-events:none; }
  .pdot { position:absolute; width:12px; height:12px; margin:-6px 0 0 -6px; border-radius:50%; background:currentColor;
          box-shadow:0 0 0 26px color-mix(in srgb, currentColor 22%, transparent); pointer-events:none; }
  .pdot.up { color:var(--up); } .pdot.down { color:var(--down); }
  .perf-info { min-height:21px; text-align:center; font-size:14px; color:var(--muted); margin:0 0 8px;
               white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .perf-info b { color:var(--text); font-weight:600; }
  .perf-range { display:flex; justify-content:space-around; }
  .perf-range button { border:0; background:none; color:var(--muted); font:inherit; font-size:16px;
                       padding:7px 16px; border-radius:999px; cursor:pointer; }
  .perf-range button.on { background:var(--line); color:var(--text); font-weight:600; }
  .cal-link { border:0; background:none; color:var(--accent); font:inherit; font-size:16px; font-weight:500;
              padding:14px 0 4px; cursor:pointer; }
  .cal-head { display:flex; align-items:center; gap:10px; margin:6px 0 16px; }
  .cal-head b { font-size:24px; white-space:nowrap; }
  #cal-total { font-size:15px; font-weight:600; color:var(--muted); white-space:nowrap; }
  .cal-space { flex:1; }
  .cal-round { border:1px solid var(--line); background:var(--card); color:var(--text); font:inherit; font-size:24px;
               line-height:1; width:44px; height:44px; border-radius:50%; cursor:pointer; flex:none; padding:0 0 3px; }
  .cal-round.small { width:38px; height:38px; font-size:20px; }
  .cal-round:disabled { opacity:.3; cursor:default; }
  .cal-grid { display:grid; grid-template-columns:repeat(7, minmax(0, 1fr)); gap:5px; }
  .cal-dow { text-align:center; font-size:15px; color:var(--muted); padding-bottom:6px; }
  .cal-cell { border:0; background:none; border-radius:13px; aspect-ratio:1 / 1.04; padding:0; font:inherit;
              color:var(--muted); display:flex; flex-direction:column; align-items:center; justify-content:center;
              gap:4px; min-width:0; }
  button.cal-cell { cursor:pointer; background:color-mix(in srgb, var(--muted) 16%, var(--card)); }
  .cal-cell.other { opacity:.45; }
  .cal-cell.win { background:color-mix(in srgb, var(--up) 22%, var(--card)); }
  .cal-cell.loss { background:color-mix(in srgb, var(--down) 22%, var(--card)); }
  .cal-cell.sel { box-shadow:0 0 0 2px var(--accent); }
  .cal-cell.today .cal-num { color:var(--accent); font-weight:700; }
  .cal-num { font-size:15px; line-height:1; }
  .cal-amt { font-size:13.5px; font-weight:600; line-height:1; font-variant-numeric:tabular-nums; white-space:nowrap;
             color:var(--text); }
  .cal-amt.up { color:var(--up); } .cal-amt.down { color:var(--down); }
  .cal-amt.open { font-weight:500; font-size:11.5px; color:var(--muted); }
  @media (max-width: 420px) { .cal-grid { gap:4px; } .cal-amt { font-size:12px; letter-spacing:-.02em; }
                              .cal-num { font-size:14px; } .cal-head b { font-size:21px; } }
  .cal-more, .cal-msum { display:none; }
  @media (min-width: 900px) {
    #cal-home:not([hidden]) { display:grid; grid-template-columns:minmax(0, 1.1fr) minmax(0, 1fr); gap:32px; align-items:start; }
    .full-body { display:grid; grid-template-columns:minmax(0, 1.7fr) minmax(300px, 1fr); gap:32px; align-items:start; }
    .full-side { position:sticky; top:16px; max-height:calc(100vh - 32px); overflow-y:auto; }
    .full-side .cal-count { margin-top:0; }
    .cal-h .muted { font-weight:500; font-size:17px; margin-left:6px; }
    .cal-cell { box-sizing:border-box; aspect-ratio:auto; height:84px; align-items:flex-start; justify-content:flex-start; padding:8px 10px;
                gap:5px; border-radius:12px; text-align:left; }
    #cal-grid .cal-cell { height:108px; padding:10px 12px; }
    #cal-grid .cal-amt.open, .cal-amt.open { font-size:13px; }
    #cal-strip .cal-n { display:none; }
    .cal-cell .cal-num { font-size:14px; }
    .cal-amt { font-size:16px; }
    #cal-grid .cal-amt { font-size:19px; }
    .cal-more { display:block; font-size:12px; color:var(--muted); white-space:nowrap; overflow:hidden;
                text-overflow:ellipsis; max-width:100%; }
  }
  @media (min-width: 900px) and (max-width: 1199px) { #cal-grid .cal-n { display:none; } #cal-grid .cal-amt { font-size:16px; } }
  @media (min-width: 900px) {
    .cal-msum { display:block; background:var(--card); border:1px solid var(--line); border-radius:16px; padding:16px 18px; }
    .cal-msum h3 { margin:0 0 8px; font-size:18px; }
    .msum-row { display:flex; justify-content:space-between; gap:12px; padding:8px 0; border-bottom:1px solid var(--line);
                font-size:15px; }
    .msum-row:last-of-type { border-bottom:0; }
    .msum-row span { color:var(--muted); }
    .msum-row b { font-variant-numeric:tabular-nums; text-align:right; }
  }
  .cal-count { font-size:20px; margin:24px 0 4px; }
  .cal-bet { display:flex; justify-content:space-between; align-items:center; gap:14px; padding:14px 0;
             border-bottom:1px solid var(--line); }
  .cal-bet b { display:block; font-size:16px; } .cal-bet small { color:var(--muted); font-size:14px; }
  .cal-bet .amt { text-align:right; white-space:nowrap; }
  .cal-bet .amt b { font-variant-numeric:tabular-nums; }
"""

CAL_JS = r"""<script>
(function () {
  const data = JSON.parse(document.getElementById("cal-data").textContent);
  const $ = id => document.getElementById(id);
  const sel = $("cal-strat"), wide = window.matchMedia("(min-width: 900px)");
  const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  const pad = n => String(n).padStart(2, "0");
  const ymd = d => d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  const toDate = s => new Date(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10));
  const money = v => (v > 0 ? "+" : v < 0 ? "−" : "") + "$" + Math.abs(v).toLocaleString("en-US",
      {minimumFractionDigits: 2, maximumFractionDigits: 2});
  function short(v) {  // a day's total, short enough for a calendar square: -$89.3, $118, $9
    const a = Math.abs(v), t = a >= 99.95 ? Math.round(a).toLocaleString("en-US") : String(+a.toFixed(1));
    return (v < 0 ? "−" : "") + "$" + t;
  }
  const cls = v => v > 0 ? "up" : v < 0 ? "down" : "";
  const today = ymd(new Date());
  let month = today.slice(0, 7), picked = null, range = 0;

  const bets = () => data[sel.value].bets;
  function sum(list) {
    const s = list.filter(b => b.status !== "open"), count = st => s.filter(b => b.status === st).length;
    return {profit: s.reduce((t, b) => t + b.profit, 0), cost: s.reduce((t, b) => t + b.cost, 0),
            won: count("won"), lost: count("lost"), push: count("push"), n: s.length, open: list.length - s.length};
  }
  function cell(key, other) {  // one calendar square
    const s = sum(bets().filter(b => b.d === key)), num = '<span class="cal-num">' + +key.slice(8) + "</span>";
    const mark = (key === today ? " today" : "") + (key === picked ? " sel" : "") + (other ? " other" : "");
    if (!s.n && !s.open) return '<div class="cal-cell' + mark + '">' + num + "</div>";
    const tint = !s.n ? "" : s.profit > 0.004 ? " win" : s.profit < -0.004 ? " loss" : "";
    return '<button class="cal-cell' + tint + mark + '" data-day="' + key + '">' + num +
      (s.n ? '<span class="cal-amt ' + cls(Math.round(s.profit * 100)) + '">' +
             (wide.matches ? money(s.profit) : short(s.profit)) + "</span>"
           : '<span class="cal-amt open">' + s.open + " open</span>") +
      (s.n ? '<span class="cal-more">' + s.won + "-" + s.lost + (s.push ? "-" + s.push : "") +
             '<span class="cal-n"> \u00b7 ' + (s.n + s.open) + (s.n + s.open === 1 ? " bet" : " bets") + "</span></span>" : "") +
      "</button>";
  }
  const DOW = ["S","M","T","W","T","F","S"].map(d => '<div class="cal-dow">' + d + "</div>").join("");

  let perfPts = [], perfSel = null, perfDrag = false;
  function drawPerf() {  // the numbers, chart and buttons for the chosen time range
    const from = range ? ymd(new Date(Date.now() - (range - 1) * 864e5)) : "";
    const list = bets().filter(b => b.status !== "open" && b.d >= from).sort((a, b) => a.t < b.t ? -1 : 1);
    const z = {profit: 0, cost: 0, won: 0, lost: 0, push: 0, bet: null};
    perfPts = [z];  // point 0 = the start of the period; point i = after the i-th bet settled
    list.forEach(b => {
      const p = perfPts[perfPts.length - 1];
      perfPts.push({profit: p.profit + b.profit, cost: p.cost + b.cost, won: p.won + (b.status === "won"),
                    lost: p.lost + (b.status === "lost"), push: p.push + (b.status === "push"), bet: b});
    });
    perfSel = null;
    document.querySelectorAll(".perf-range button").forEach(b => b.classList.toggle("on", +b.dataset.range === range));
    drawPerfChart();
  }
  function drawPerfChart() {  // redrawn on every touch, so the numbers follow your finger
    const pts = perfPts, n = pts.length, at = pts[perfSel === null ? n - 1 : perfSel];
    $("perf-profit").innerHTML = '<span class="' + cls(at.profit) + '">' + money(at.profit) + "</span>";
    $("perf-roi").innerHTML = at.cost ? '<span class="' + cls(at.profit) + '">' + (at.profit >= 0 ? "+" : "−") +
      Math.abs(100 * at.profit / at.cost).toFixed(2) + "%</span>" : "–";
    $("perf-record").textContent = at.won + "-" + at.lost + "-" + at.push;
    if (n < 2) {
      $("perf-chart").innerHTML = '<div class="none">No settled bets in this period yet.</div>';
      $("perf-info").innerHTML = ""; return;
    }
    const ys = pts.map(p => p.profit), lo = Math.min.apply(null, ys), hi = Math.max.apply(null, ys);
    const span = hi - lo || 1, W = 300, H = 120, SVG_TOP = 26, SVG_H = 138;
    const xp = i => i / (n - 1) * 100;  // percent across
    const yv = v => H - 4 - (v - lo) / span * (H - 8);
    const pts2 = (a, b) => ys.slice(a, b + 1).map((v, k) => (xp(a + k) * W / 100).toFixed(1) + "," + yv(v).toFixed(1)).join(" ");
    const end = perfSel === null ? n - 1 : perfSel, tone = (perfSel === null ? ys[n - 1] : at.profit) >= 0 ? "up" : "down";
    let svg = '<svg viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none" aria-hidden="true">' +
      '<line class="zero" x1="0" x2="' + W + '" y1="' + yv(0).toFixed(1) + '" y2="' + yv(0).toFixed(1) +
      '" vector-effect="non-scaling-stroke"/>' +
      '<polyline class="line ' + tone + '" vector-effect="non-scaling-stroke" points="' + pts2(0, end) + '"/>' +
      (end < n - 1 ? '<polyline class="line after ' + tone + '" vector-effect="non-scaling-stroke" points="' +
        pts2(end, n - 1) + '"/>' : "") + "</svg>";
    const clamp = v => Math.min(88, Math.max(12, v));
    const iHi = ys.indexOf(hi), iLo = ys.indexOf(lo);
    if (hi > lo && !(perfSel !== null && clamp(xp(iHi)) > 55))  // keep the high label clear of the date
      svg += '<span class="plabel hi" style="left:' + clamp(xp(iHi)) + '%">' + money(hi) + "</span>";
    if (hi > lo) svg += '<span class="plabel lo" style="left:' + clamp(xp(iLo)) + '%">' + money(lo) + "</span>";
    const b = at.bet, when = b ? new Date(b.t).toLocaleDateString("en-US", {month: "short", day: "numeric", year: "numeric"}) : "";
    if (perfSel !== null) {
      svg += '<span class="pdate">' + (b ? when : "Start") + "</span>" +
        '<span class="pdot ' + tone + '" style="left:' + xp(perfSel) + "%;top:" +
        (SVG_TOP + yv(at.profit) / H * SVG_H).toFixed(1) + 'px"></span>';
    }
    $("perf-chart").innerHTML = svg;
    $("perf-info").innerHTML = perfSel === null ? "Tap or drag the chart to see any point" :
      !b ? "Start of this period" :
      "Bet " + perfSel + " of " + (n - 1) + ": <b>" + esc(b.pick) + (b.type ? " · " + esc(b.type) : "") +
      '</b> <span class="' + cls(b.profit) + '">' + money(b.profit) + "</span>";
  }
  function perfPick(e) {
    const r = $("perf-chart").getBoundingClientRect(), n = perfPts.length;
    if (n < 2 || !r.width) return;
    const i = Math.round(Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)) * (n - 1));
    if (i !== perfSel) { perfSel = i; drawPerfChart(); }
  }
  $("perf-chart").addEventListener("pointerdown", e => { e.onPerfChart = true; perfDrag = true; perfPick(e); });
  $("perf-chart").addEventListener("pointermove", e => { if (perfDrag || e.pointerType === "mouse") perfPick(e); });
  ["pointerup", "pointercancel"].forEach(t => $("perf-chart").addEventListener(t, () => { perfDrag = false; }));
  $("perf-chart").addEventListener("pointerleave", e => {
    perfDrag = false;
    if (e.pointerType === "mouse" && perfSel !== null) { perfSel = null; drawPerfChart(); }
  });
  document.addEventListener("pointerdown", e => {  // tapping anywhere else goes back to the totals
    if (perfSel !== null && !e.onPerfChart) { perfSel = null; drawPerfChart(); }
  });
  function drawStrip() {  // phones: last week and this week; computer screens: this whole month
    let out = DOW;
    if (wide.matches) {
      const now = new Date(), y = now.getFullYear(), mo = now.getMonth();
      for (let i = 0; i < new Date(y, mo, 1).getDay(); i++) out += "<div></div>";
      for (let d = 1; d <= new Date(y, mo + 1, 0).getDate(); d++) out += cell(today.slice(0, 7) + "-" + pad(d), false);
      $("strip-title").textContent = now.toLocaleDateString("en-US", {month: "long"});
    } else {
      const start = new Date(); start.setDate(start.getDate() - start.getDay() - 7);
      for (let i = 0; i < 14; i++) { out += cell(ymd(start), false); start.setDate(start.getDate() + 1); }
      $("strip-title").textContent = "";
    }
    $("cal-strip").innerHTML = out;
  }
  wide.addEventListener("change", () => { if (!$("cal-home").hidden) drawStrip(); else drawFull(); });
  function drawFull() {
    const all = bets(), m = sum(all.filter(b => b.d.slice(0, 7) === month));
    const months = all.map(b => b.d.slice(0, 7)).concat(today.slice(0, 7)).sort();
    $("cal-title").textContent = MONTHS[+month.slice(5, 7) - 1] + " " + month.slice(0, 4);
    $("cal-total").innerHTML = m.n ? '<span class="' + cls(m.profit) + '">' + money(m.profit) + "</span>" : "";
    $("cal-prev").disabled = month <= months[0];
    $("cal-next").disabled = month >= months[months.length - 1];
    const y = +month.slice(0, 4), mo = +month.slice(5, 7) - 1;
    const first = new Date(y, mo, 1).getDay(), days = new Date(y, mo + 1, 0).getDate();
    let out = DOW;
    for (let i = 0; i < first; i++) out += "<div></div>";
    for (let d = 1; d <= days; d++) out += cell(month + "-" + pad(d), false);
    $("cal-grid").innerHTML = out;
    const list = picked ? all.filter(b => b.d === picked) : [];
    $("cal-day").innerHTML = !list.length ? monthSummary(all.filter(b => b.d.slice(0, 7) === month)) :
      '<h2 class="cal-count">' + list.length + (list.length === 1 ? " Bet" : " Bets") + ' <small class="muted" style="font-size:15px;' +
      'font-weight:500">' + toDate(picked).toLocaleDateString("en-US", {weekday: "short", month: "short", day: "numeric"}) + "</small></h2>" +
      list.map(b => '<div class="cal-bet"><div><b>' + esc(b.pick) + (b.type ? " · " + esc(b.type) : "") + "</b><small>" +
        esc(b.market) + " · " + (b.status === "open" ? "Open" : b.status === "push" ? "Push" : "Final") +
        " · $" + b.cost.toFixed(2) + " bet</small></div>" +
        '<div class="amt"><b class="' + (b.status === "open" ? "muted" : cls(b.profit)) + '">' +
        (b.status === "open" ? "Open" : money(b.profit)) + '</b><small><span class="px" data-p="' + b.price + '">' +
        b.price.toFixed(2) + "</span></small></div></div>").join("");
    let fmt = "price"; try { fmt = localStorage.getItem("price-format") || "price"; } catch (e) {}
    if (typeof showPrices === "function") showPrices(fmt);  // honour the Price / American odds switch
  }
  function monthSummary(list) {  // computer screens: the side panel before a day is tapped
    const m = sum(list), days = {};
    list.filter(b => b.status !== "open").forEach(b => { days[b.d] = (days[b.d] || 0) + b.profit; });
    const ds = Object.entries(days).sort((a, b) => a[1] - b[1]);
    const label = d => toDate(d).toLocaleDateString("en-US", {weekday: "short", month: "short", day: "numeric"});
    const row = (k, v) => '<div class="msum-row"><span>' + k + "</span><b>" + v + "</b></div>";
    if (!m.n && !m.open) return '<div class="cal-msum"><p class="muted">No bets this month.</p></div>';
    return '<div class="cal-msum"><h3>' + $("cal-title").textContent + "</h3>" +
      row("Profit", '<span class="' + cls(m.profit) + '">' + money(m.profit) + "</span>") +
      row("ROI", m.cost ? '<span class="' + cls(m.profit) + '">' + (m.profit >= 0 ? "+" : "−") +
          Math.abs(100 * m.profit / m.cost).toFixed(2) + "%</span>" : "–") +
      row("Record", m.won + "-" + m.lost + "-" + m.push) +
      row("Settled bets", m.n + (m.open ? " (+" + m.open + " open)" : "")) +
      row("Amount bet", "$" + m.cost.toFixed(2)) +
      row("Up days / down days", ds.filter(d => d[1] > 0.004).length + " / " + ds.filter(d => d[1] < -0.004).length) +
      (ds.length ? row("Best day", label(ds[ds.length - 1][0]) + ' <span class="' + cls(ds[ds.length - 1][1]) + '">' +
          money(ds[ds.length - 1][1]) + "</span>") +
        row("Worst day", label(ds[0][0]) + ' <span class="' + cls(ds[0][1]) + '">' + money(ds[0][1]) + "</span>") : "") +
      '<p class="muted" style="margin:12px 0 0;font-size:13px">Click a day to see its bets.</p></div>';
  }
  function show(full) {
    $("cal-home").hidden = full; $("cal-full").hidden = !full;
    if (full) drawFull(); else { drawPerf(); drawStrip(); }
    try { sessionStorage.setItem("cal-view", full ? "full" : "home"); } catch (e) {}
  }
  function shift(by) {
    const d = new Date(+month.slice(0, 4), +month.slice(5, 7) - 1 + by, 1);
    month = d.getFullYear() + "-" + pad(d.getMonth() + 1); picked = null; drawFull();
  }
  $("cal-strip").addEventListener("click", e => {
    const c = e.target.closest("[data-day]");
    if (c) { picked = c.dataset.day; month = picked.slice(0, 7); show(true); }
  });
  $("cal-grid").addEventListener("click", e => {
    const c = e.target.closest("[data-day]");
    if (!c) return;
    picked = picked === c.dataset.day ? null : c.dataset.day;
    drawFull();
  });
  $("cal-open").addEventListener("click", () => { month = today.slice(0, 7); picked = null; show(true); window.scrollTo(0, 0); });
  $("cal-back").addEventListener("click", () => show(false));
  $("cal-prev").addEventListener("click", () => shift(-1));
  $("cal-next").addEventListener("click", () => shift(1));
  document.querySelectorAll(".perf-range button").forEach(b => b.addEventListener("click", () => {
    range = +b.dataset.range;
    try { localStorage.setItem("cal-range", range); } catch (e) {}
    drawPerf();
  }));
  sel.addEventListener("change", () => {
    try { localStorage.setItem("cal-strat", sel.value); } catch (e) {}
    picked = null; show(!$("cal-full").hidden);
  });
  let full = false;
  try {
    const k = localStorage.getItem("cal-strat"); if (k && data[k]) sel.value = k;
    const r = localStorage.getItem("cal-range"); if (r !== null && [0, 7, 30, 365].includes(+r)) range = +r;
    full = sessionStorage.getItem("cal-view") === "full";  // a Refresh keeps the calendar open
  } catch (e) {}
  show(full);
})();
</script>"""


REPORT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Betting Results</title>
<style>
  :root { --bg:#f7f7f5; --card:#fff; --text:#1d1d1f; --muted:#6e6e73; --line:#e5e5ea;
          --accent:#2f6fed; --up:#1a8a4a; --down:#c4372b;
          --chart-pos:#2a78d6; --chart-neg:#e34948; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#141416; --card:#1e1e21; --text:#f2f2f4; --muted:#9a9aa0; --line:#2e2e33;
            --accent:#6b9bff; --up:#4cc27e; --down:#ff6b5e;
            --chart-pos:#3987e5; --chart-neg:#e66767; } }
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
  .tabs { display:flex; gap:4px; border-bottom:1px solid var(--line); margin:0 0 18px;
          overflow-x:auto; scrollbar-width:none; }
  .tabs button { border:0; background:none; color:var(--muted); font:inherit; font-weight:600;
                 padding:8px 14px; border-bottom:2px solid transparent; margin-bottom:-1px; cursor:pointer;
                 white-space:nowrap; flex:none; }
  @media (max-width: 640px) { .tabs button { padding:8px 12px; } .tabs .long { display:none; } }
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
__STATS_CSS__
__CAL_CSS__
</style></head><body><main>
<h1>Betting Results</h1>
<p class="sub">__SUB__</p>
<div class="tabs" role="tablist"><button data-tab="cal" class="on">Summary</button>
<button data-tab="real">Real<span class="long"> bets</span></button>
__STRATEGY_TABS__
<button data-tab="stats">Stat<span class="long">istic</span>s</button></div>
__ODDS_TOGGLE__
<section id="tab-cal">__CALENDAR__</section>
<section id="tab-real" hidden>__BETS_REAL__</section>
__STRATEGY_SECTIONS__
<section id="tab-stats" hidden>__STATS__</section>
<p class="note">Strategies A to E and Claude use pretend money; Real bets use real money. Each bet goes to
whichever US venue (Polymarket US, Kalshi or ProphetX) had the lowest price including fees, from the real order
books. Bankroll = cash + open bets at cost. Research only, not financial advice.</p>
</main>
<script>
function showTab(name) {
  document.querySelectorAll(".tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === name));
  document.querySelectorAll("section[id^=tab-]").forEach(s => s.hidden = s.id !== "tab-" + name);
  try { sessionStorage.setItem("report-tab", name); } catch (e) {}  // kept through a Refresh; a fresh open starts on Calendar
}
document.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));
try {
  const t = sessionStorage.getItem("report-tab");
  if (t && document.querySelector(`.tabs button[data-tab="${t}"]`)) showTab(t);
} catch (e) {}
</script></body></html>
"""


VENUE_NAMES = {"kalshi": "Kalshi", "polymarket_us": "Polymarket US", "prophetx": "ProphetX",
               "polymarket_intl": "Polymarket (intl, before US switch)"}


def short_time(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%b %d %I:%M %p")


def real_tiles(state):
    """Tiles for the Real bets tab (the real bankroll comes from the accounts, not a start amount)."""
    done = [b for b in state["bets"] if b["status"] != "open"]
    wins = sum(b["status"] == "won" for b in done)
    profit = sum(b["profit"] for b in done)
    staked = sum(b["cost"] for b in done)
    open_cost = sum(b["cost"] for b in open_bets(state))
    return [
        (f'${state.get("cash", 0) + open_cost:,.2f}', "Balance + open bets"),
        (f'<span class="{"up" if profit >= 0 else "down"}">{profit:+,.2f}</span>', "Profit (settled)"),
        (f"{wins}-{len(done) - wins}", "Won-lost"),
        (f"{profit / staked:+.1%}" if staked else "&ndash;", "Return per $ bet"),
        (f"{len(open_bets(state))}", "Open bets"),
        (f'${sum(b["fees"] for b in state["bets"]):,.2f}', "Fees paid"),
    ]


def bets_html(cfg, state, tiles=None):
    """One strategy's tab: rules, tiles, open bets, settled bets."""
    bets = state["bets"]
    esc = html.escape
    done = [b for b in bets if b["status"] != "open"]
    wins = sum(b["status"] == "won" for b in done)
    profit = sum(b["profit"] for b in done)
    staked = sum(b["cost"] for b in done)
    roll = bankroll(state)
    change = roll - state.get("start_bankroll", roll)
    tiles = tiles or [
        (f"${roll:,.2f}", "Bankroll"),
        (f'<span class="{"up" if change >= 0 else "down"}">{change:+,.2f}</span>', "Since start"),
        (f"{wins}-{len(done) - wins}", "Won-lost"),
        (f"{profit / staked:+.1%}" if staked else "&ndash;", "Return per $ bet"),
        (f"{len(open_bets(state))}", "Open bets"),
        (f"${state['cash']:,.2f}", "Cash"),
    ]
    tiles_html = "".join(f'<div class="tile"><b>{v}</b><span>{k}</span></div>' for v, k in tiles)

    def stake_text(b):
        """Stake tier, plus the underdog size multiplier when it shrank the bet."""
        mults = dict.fromkeys(m for m in (b.get("size_multiplier"), b.get("top_up_size_multiplier"))
                              if m and m < 1)  # once if the bet and its top-up shrank the same
        return live.pct(b["stake"]) + "".join(f' <span class="muted">&times;{m:.2f}</span>' for m in mults)

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
        f'<td class="num">{stake_text(b)}</td><td class="num">{bettors_text(b)}</td>'
        f'<td class="num">{b["avg_bankroll_pct"]:.1f}%</td><td class="num">{scanner.px(b["avg_price"], 3)}</td>'
        f'<td class="num">${b["cost"]:.2f}</td><td>{venue_name(b)}</td>'
        f'<td>{short_time(b["ends"]) if b["ends"] else ""}</td><td>{link(b)}</td></tr>'
        for b in reversed(open_bets(state))) or '<tr><td colspan="11">None yet.</td></tr>'
    done_rows = "".join(
        f'<tr><td>{short_time(b["placed_at"])}</td><td class="pick">{esc(str(b["pick"]))}</td>'
        f'<td class="market">{esc(b["market"])}</td>'
        f'<td class="{"up" if b["status"] == "won" else "down" if b["status"] == "lost" else ""}">'
        f'{b["status"].title()}</td><td class="num">{stake_text(b)}</td>'
        f'<td class="num">{scanner.px(b["avg_price"], 3)}</td><td class="num">${b["cost"]:.2f}</td>'
        f'<td class="num">${b["payout"]:.2f}</td>'
        f'<td class="num {"up" if b["profit"] > 0 else "down" if b["profit"] < 0 else ""}">'
        f'{b["profit"]:+.2f}</td><td>{venue_name(b)}</td></tr>'
        for b in reversed(done)) or '<tr><td colspan="10">None yet.</td></tr>'
    started = (f'Started {short_time(state["started_at"])} with ${state["start_bankroll"]:,.0f}.'
               if "start_bankroll" in state else "")
    return f"""<p class="sub"><b>{cfg["name"]}.</b> {esc(cfg["rules"])} {started}</p>
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


def tab_label(name):
    """'Strategy A' shows as just 'A' on phones."""
    return name.replace("Strategy ", '<span class="long">Strategy </span>')


def write_reports(states, live_state, card=None):
    """states = {strategy key: state}; live_state = real bets. Writes the CSVs and the report page."""
    for key, state in states.items():
        write_csv(os.path.join(HERE, STRATEGIES[key]["csv"]), state["bets"])
    write_csv(os.path.join(HERE, LIVE["csv"]), live_state["bets"])
    settled = [(cfg, [b for b in st["bets"] if b["status"] != "open"])
               for cfg, st in [(LIVE, live_state)] + [(STRATEGIES[k], st) for k, st in states.items()]]
    sub = f"Real-money bets and pretend-money tests of six strategies. Last updated {time.strftime('%b %d %I:%M %p')}."
    page = (REPORT.replace("__ODDS_CSS__", scanner.ODDS_CSS + scanner.MOBILE_CSS + scanner.REFRESH_CSS).replace("__STATS_CSS__", STATS_CSS).replace("__CAL_CSS__", CAL_CSS)
            .replace("__ODDS_TOGGLE__", scanner.ODDS_TOGGLE)
            .replace("</body>", scanner.ODDS_JS + scanner.MOBILE_JS + scanner.REFRESH_JS + CAL_JS + "</body>")
            .replace("__SUB__", html.escape(sub))
            .replace("__BETS_REAL__", bets_html(LIVE, live_state, real_tiles(live_state)))
            .replace("__CALENDAR__", calendar_html(
                [("real", LIVE, live_state)] + [(k, STRATEGIES[k], st) for k, st in states.items()]))
            .replace("__STRATEGY_TABS__", "".join(
                f'<button data-tab="{k}">{tab_label(cfg["name"])}</button>' for k, cfg in STRATEGIES.items()))
            .replace("__STRATEGY_SECTIONS__", "\n".join(
                f'<section id="tab-{k}" hidden>{bets_html(cfg, states[k])}'
                f'{claude_strategy.card_html(card, html.escape) if cfg.get("claude") and card else ""}</section>'
                for k, cfg in STRATEGIES.items()))
            .replace("__STATS__", stats_html(settled, real_vs_paper(live_state, states["a"]))))
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
scanner_args.research_top = 200  # also scan ranks 101-200: Claude's report card + pick_log_200 only


def write_picks(picks):
    """Refresh picks.html (all picks, no price filter) here and in iCloud Drive."""
    scanner.write_html(os.path.join(HERE, "picks.html"), picks, scanner_args)
    copy_to_icloud("picks.html")


def settled_alert(b, state):
    """(title, text) for a real bet that just settled, with the running real-money record."""
    done = [x for x in state["bets"] if x["status"] in ("won", "lost", "push")]
    wins = sum(x["status"] == "won" for x in done)
    losses = sum(x["status"] == "lost" for x in done)
    total = sum(x["profit"] for x in done)
    money = lambda v: f"{'+' if v >= 0 else '−'}${abs(v):,.2f}"
    title = {"won": f"✅ Won {money(b['profit'])}", "lost": f"❌ Lost {money(b['profit'])}",
             "push": "↩️ Push (money back)"}[b["status"]]
    live.log("settled", bet=live.plain(b["pick"], b["market"]), status=b["status"], cost=b["cost"],
             payout=b["payout"], profit=b["profit"], venue=b["venue"], ref=b["venue_ref"].get("ref"))
    return title, f"{live.plain(b['pick'], b['market'])}\nRecord: {wins}–{losses} · total {money(total)}"


def main():
    print(f"\n=== Forward test run {time.strftime('%Y-%m-%d %H:%M')} ===")
    states = {}
    for key, cfg in STRATEGIES.items():
        path = os.path.join(HERE, cfg["state"])
        states[key] = load_state(path)
        if cfg.get("claude") and states[key].get("version") != claude_strategy.VERSION:
            # the Claude rules changed: restart its pretend bankroll (its old bets came from the old rules)
            print(f"{cfg['name']}: restarting under rules version {claude_strategy.VERSION}")
            states[key] = {**load_state("/nonexistent"), "version": claude_strategy.VERSION}
        settle(states[key])
        save_state(states[key], path)
    log_path = os.path.join(HERE, LIVE_LOG)
    if os.path.exists(log_path):
        with open(log_path) as f:
            live.LOG[:] = json.load(f)
    live_path = os.path.join(HERE, LIVE["state"])
    live_state = load_state(live_path) if os.path.exists(live_path) else {"cash": 0.0, "bets": [], "orders": []}
    # real payouts land in the accounts by themselves; this records them and tells the group
    for b in settle(live_state, real=True):
        live.alert(*settled_alert(b, live_state))
    save_state(live_state, live_path)
    picks, _ = scanner.find_picks(scanner_args)
    write_picks(picks)
    card_path = os.path.join(HERE, CARD)
    card = load_state(card_path) if os.path.exists(card_path) else claude_strategy.new_card()
    if card.get("version") != claude_strategy.VERSION:
        card = claude_strategy.new_card()  # rules changed: grade everyone again from scratch
    claude_picks = []
    try:  # the report card must never stop the other strategies or real bets
        positions = {**scanner.POSITIONS, **claude_strategy.extra_positions(
            card, scanner.POSITIONS, scanner_args.min_value)}
        claude_strategy.record(card, positions, scanner.PACE)
        print(f"Report card: graded {claude_strategy.grade(card)} positions, "
              f"{len(card['pending'])} waiting, {len(claude_strategy.qualified(card))} traders qualified")
        claude_picks = claude_strategy.picks(card, positions)
        claude_strategy.save(card, card_path)
    except Exception as e:
        print(f"Report card error: {type(e).__name__}: {e}")
    live_bets = []  # Strategy A's new paper bets, mirrored with real money by live.py

    def copy_real(intent):
        """Real money copies A's bets, except sports still on paper only (venues.PAPER_ONLY)."""
        sport = venues.base_event(intent["bet"]["link"].rstrip("/").rsplit("/", 1)[-1]).split("-")[0]
        if sport in venues.PAPER_ONLY:
            print(f"  (paper only, no real bet: {sport.upper()} is new)")
            return
        live_bets.append(intent)
    for key, cfg in STRATEGIES.items():
        print(f"-- {cfg['name']} --")
        trade(states[key], claude_picks if cfg.get("claude") else picks, cfg["tiers"], on_bet=copy_real if key == "a" else None,
              net=cfg.get("net", False), window=cfg.get("window"),
              min_money_share=cfg.get("min_money_share", 0), count=cfg.get("count"),
              max_price=cfg.get("max_price", 1.0))
        save_state(states[key], os.path.join(HERE, cfg["state"]))
    try:
        live.run(live_bets, MAX_ABOVE, live_state, states["a"]["bets"], A_MAX_PRICE)
    except Exception as e:
        print(f"Live betting error ({type(e).__name__}); details sent to the phone alert channel")
        live.alert("Live betting crashed", f"{type(e).__name__}: {e}")
    save_state(live_state, live_path)
    save_state(live.LOG[-LOG_KEEP:], log_path)  # technical details for each live order, newest last
    try:  # research log of every pick (pick_log.py); must never stop the betting
        plog = pick_log.load(HERE)
        pick_log.record(plog, picks, market_info)
        done = pick_log.settle(plog, HERE, states, live_state)
        pick_log.save(plog, HERE)
        print(f"Pick log: {len(plog['open'])} open, {done} finished this run")
        wide = pick_log.load(HERE, "pick_log_200")
        pick_log.record(wide, scanner.WIDE_PICKS, market_info, pick_log.WIDE_SNAP)
        done = pick_log.settle(wide, HERE, states, live_state, "pick_log_200", pick_log.WIDE_SNAP)
        pick_log.save(wide, HERE, "pick_log_200")
        print(f"Pick log (top 200, research): {len(wide['open'])} open, {done} finished this run")
    except Exception as e:
        print(f"Pick log error: {type(e).__name__}: {e}")
    write_reports(states, live_state, card)
    for key, cfg in STRATEGIES.items():
        st = states[key]
        print(f"{cfg['name']}: bankroll ${bankroll(st):,.2f} (cash ${st['cash']:,.2f}, "
              f"{len(open_bets(st))} open bets)")
    print("Wrote paper_report.html and picks.html")


if __name__ == "__main__":
    main()
