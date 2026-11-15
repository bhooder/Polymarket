#!/usr/bin/env python3
"""Research report: Strategy ChatGPT next to Strategy A (added 2026-11-15).

Read-only: it reads the saved paper-bet files and prints a text report. It places nothing,
changes nothing and is not part of the hourly run or the dashboard.

    python3 research_chatgpt.py                      # ChatGPT vs A
    python3 research_chatgpt.py paper_trades_b.json  # any other strategy's file vs A

Review points fixed in advance: a first look at ~100 settled bets, the real decision at 200.
Positive short-term returns alone are never a reason to bet real money.
"""
import json
import math
import os
import sys
from collections import defaultdict

import forward_test as ft

HERE = os.path.dirname(os.path.abspath(__file__))
DONE = ("won", "lost", "push")


def load(name):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)["bets"]


def edge(bets):
    """(average of result - price paid, in cents; its standard error under 'the price is right')."""
    n = len(bets)
    if not n:
        return None, None
    mean = sum(b["payout"] / b["shares"] - b["avg_price"] for b in bets) / n
    se = math.sqrt(sum(b["avg_price"] * (1 - b["avg_price"]) for b in bets)) / n
    return 100 * mean, 100 * se


def drawdown(bets):
    """Largest fall in running profit from a peak, in dollars (bets in the order they settled)."""
    run = peak = worst = 0.0
    for b in sorted(bets, key=lambda b: b["settled_at"]):
        run += b["profit"]
        peak = max(peak, run)
        worst = max(worst, peak - run)
    return worst


def line(label, bets):
    if not bets:
        print(f"  {label:<26} no settled bets")
        return
    s = ft.summarize(bets)
    e, se = edge(bets)
    losses = sum(b["status"] == "lost" for b in bets)
    pushes = sum(b["status"] == "push" for b in bets)
    ret = s["profit"] / s["staked"] if s["staked"] else 0
    ret_se = s["sd"] / s["staked"] if s["staked"] else 0
    print(f"  {label:<26} {s['n']:>4} bets  {s['wins']}-{losses}-{pushes}  staked ${s['staked']:,.0f}  "
          f"profit {s['profit']:+,.2f}  return {ret:+.1%} (95% range {ret - 1.96 * ret_se:+.1%} to "
          f"{ret + 1.96 * ret_se:+.1%})  edge {e:+.1f}c +-{1.96 * se:.1f}  z {s['z_market']:+.2f}")


def report(name, bets):
    done = [b for b in bets if b["status"] in DONE]
    print(f"\n== {name} ==")
    print(f"  {len(bets)} bets: {len(bets) - len(done)} open, {len(done)} settled")
    if not done:
        return
    line("All settled", done)
    avg = lambda key: sum(b[key] for b in done) / len(done)
    c, t, n = ft.clv_summary(done)
    print(f"  price paid {100 * avg('avg_price'):.1f}c vs traders' entry {100 * avg('traders_entry'):.1f}c; "
          f"fees ${sum(b['fees'] for b in done):.2f}; "
          f"CLV {'n/a' if c is None else f'{c:+.2f}c'}{'' if t is None else f' (t {t:+.1f}, {n} bets)'}; "
          f"worst drawdown ${drawdown(done):.2f}")
    for title, key in (("By sport", ft.sport_of), ("By bet type", lambda b: ft.TYPE_NAMES.get(ft.bet_type(b), "Other")),
                       ("By venue", lambda b: b.get("venue", "?")), ("By week settled", lambda b: week(b))):
        groups = defaultdict(list)
        for b in done:
            groups[key(b)].append(b)
        print(f"  {title}:")
        for k in sorted(groups, key=lambda k: -len(groups[k])):
            line("  " + str(k), groups[k])
    print("  Calibration (did bets win as often as their price said?):")
    for lo, hi in ((0.28, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 0.76)):
        g = [b for b in done if lo <= b["avg_price"] < hi and b["status"] != "push"]
        if g:
            said = sum(b["avg_price"] for b in g) / len(g)
            won = sum(b["status"] == "won" for b in g) / len(g)
            print(f"    {100 * lo:.0f}-{100 * hi:.0f}c: {len(g):>3} bets, price said {said:.0%}, won {won:.0%}")


def week(b):
    from datetime import datetime
    y, w, _ = datetime.fromisoformat(b["settled_at"]).isocalendar()
    return f"{y}-W{w:02d}"


def matched(a_bets, x_bets, x_name):
    """The same pick bet by both, by only one, or by only the other: their results are NOT independent."""
    a = {b["token_id"]: b for b in a_bets if b["status"] in DONE}
    x = {b["token_id"]: b for b in x_bets if b["status"] in DONE}
    both = sorted(set(a) & set(x))
    print(f"\n== Matched comparison: Strategy A vs {x_name} (settled bets) ==")
    line("Both: A's bets", [a[t] for t in both])
    line(f"Both: {x_name}'s", [x[t] for t in both])
    line("Only A", [a[t] for t in a if t not in x])
    line(f"Only {x_name}", [x[t] for t in x if t not in a])
    if both:
        gap = [100 * (a[t]["avg_price"] - x[t]["avg_price"]) for t in both]
        print(f"  On shared picks {x_name} paid {sum(gap) / len(gap):+.2f}c less per share than A on average.")
    print(f"  The 'Only {x_name}' line is the real test: it is what this strategy adds that A does not already have.")


def main():
    other = sys.argv[1] if len(sys.argv) > 1 else ft.STRATEGIES["chatgpt"]["state"]
    name = next((c["name"] for c in ft.STRATEGIES.values() if c["state"] == other), other)
    a_bets, x_bets = load(ft.STRATEGIES["a"]["state"]), load(other)
    report("Strategy A", a_bets)
    report(name, x_bets)
    matched(a_bets, x_bets, name)
    n = sum(b["status"] in DONE for b in x_bets)
    print(f"\n{name}: {n} settled. First review at 100, decision point at 200. Rules are frozen until then.")


if __name__ == "__main__":
    main()
