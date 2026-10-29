#!/usr/bin/env python3
"""
Polymarket "smart money" scanner.

1. Pulls the top N traders from Polymarket's public leaderboard.
2. Pulls each trader's open positions.
3. Aggregates by market outcome: how many top traders hold it, how much
   money is behind it, their average entry vs. the current price.
4. Writes consensus.csv and traders.csv and prints the top picks.

Uses only the public, no-auth Data API v2 (https://data-api.polymarket.com/v2).
Read-only: it never places orders. Output is a research signal, not advice.

Usage:
    pip install requests
    python polymarket_smart_money.py --top 50 --period month --category overall
"""
import argparse
import csv
import html
import re
import time
from collections import defaultdict

import requests

BASE = "https://data-api.polymarket.com/v2"
GAMMA = "https://gamma-api.polymarket.com"  # market metadata (event tags like "sports")
session = requests.Session()
session.headers["User-Agent"] = "smart-money-research/0.1"

# Short-term crypto price markets ("Bitcoin Up or Down - ...", "Will ETH reach $5k?"),
# the favourite of speed/arbitrage bots.
CRYPTO_PRICE = re.compile(
    r"up or down|\b(bitcoin|btc|ethereum|eth|solana|sol|xrp|dogecoin|doge|bnb)\b"
    r".*\b(above|below|reach|dip|price|between|hit)\b", re.I)


def get(path, params, retries=5, base=BASE):
    """GET with backoff on 429/503 (honours Retry-After)."""
    for attempt in range(retries):
        r = session.get(f"{base}{path}", params=params, timeout=30)
        if r.status_code in (429, 503):
            time.sleep(float(r.headers.get("Retry-After", 2 * (attempt + 1))))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Gave up on {path} after {retries} tries")


def leaderboard(top, period, category, sort_by):
    rows, cursor = [], None
    while len(rows) < top:
        params = {"limit": min(top, 100)} if cursor is None else {"cursor": cursor}
        if cursor is None:
            params.update(time_period=period, category=category, sort_by=sort_by)
        page = get("/leaderboard", params)
        rows += page.get("data") or []
        cursor = (page.get("pagination") or {}).get("next_cursor")
        if not cursor:
            break
    return rows[:top]


def open_positions(wallet, min_value):
    out, cursor = [], None
    while True:
        params = {"user": wallet, "status": "OPEN", "limit": 500}
        if cursor:
            params["cursor"] = cursor  # cursor pages must repeat the user anchor
        page = get("/positions", params)
        for p in page.get("data") or []:
            if (p.get("current_value") or 0) < min_value:
                return out  # sorted by value, largest first: the rest are smaller
            out.append(p)
        cursor = (page.get("pagination") or {}).get("next_cursor")
        if not cursor:
            return out


def hedged_markets(wallet, condition_ids):
    """Markets where the wallet holds more than one side, counting positions of any size."""
    sides = defaultdict(set)
    ids = sorted(condition_ids)
    for i in range(0, len(ids), 20):  # the API takes at most 20 condition ids per call
        params = {"user": wallet, "condition": ",".join(ids[i:i + 20]), "limit": 500}
        for p in get("/positions", params).get("data") or []:
            if (p.get("entry_cost_usdc") or 0) >= 1:  # ignore leftover dust
                sides[p["condition_id"]].add(p.get("outcome_index"))
    return {c for c, s in sides.items() if len(s) > 1}


def event_tags(event_ids):
    """Map event id -> set of tag slugs (e.g. {"sports", "nba"})."""
    tags, ids = {}, sorted(event_ids)
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        for e in get("/events", {"id": chunk, "limit": len(chunk)}, base=GAMMA):
            tags[str(e["id"])] = {t.get("slug") for t in e.get("tags") or []}
    return tags


def crypto_share(wallet):
    """Fraction of the wallet's last 500 trades that are on crypto price markets."""
    trades = get("/trades", {"user": wallet, "limit": 500}).get("data") or []
    if not trades:
        return 0.0
    return sum(bool(CRYPTO_PRICE.search(t.get("title") or "")) for t in trades) / len(trades)


# Price / American odds switch, shared by picks.html and paper_report.html.
# Price cells are <span class="px" data-p="0.61">0.61</span>.
ODDS_CSS = """
  .toggle { display:inline-flex; border:1px solid var(--line); border-radius:8px; overflow:hidden; margin:0 0 16px; }
  .toggle button { border:0; background:var(--card); color:var(--muted); padding:6px 12px;
                   font:inherit; font-size:13px; cursor:pointer; }
  .toggle button.on { background:var(--accent); color:#fff; }
"""
ODDS_TOGGLE = """<div class="toggle" role="group" aria-label="Price format">
<button data-fmt="price" class="on">Price</button><button data-fmt="odds">American odds</button></div>"""
ODDS_JS = """<script>
function american(p) {  // 0.61 -> -156, 0.25 -> +300
  if (!(p > 0 && p < 1)) return "\\u2013";
  return p >= 0.5 ? "\\u2212" + Math.round(100 * p / (1 - p)) : "+" + Math.round(100 * (1 - p) / p);
}
function showPrices(fmt) {
  document.querySelectorAll(".px").forEach(el => {
    if (el.dataset.raw === undefined) el.dataset.raw = el.textContent;
    el.textContent = fmt === "odds" ? american(+el.dataset.p) : el.dataset.raw;
  });
  document.querySelectorAll(".toggle button").forEach(b => b.classList.toggle("on", b.dataset.fmt === fmt));
  try { localStorage.setItem("price-format", fmt); } catch (e) {}
}
document.querySelectorAll(".toggle button").forEach(b => b.addEventListener("click", () => showPrices(b.dataset.fmt)));
let saved = "price";
try { saved = localStorage.getItem("price-format") || "price"; } catch (e) {}
showPrices(saved);
</script>"""


def px(p, digits=2):
    """A price cell the Price / American odds switch can flip."""
    return f'<span class="px" data-p="{p}">{p:.{digits}f}</span>'


HTML_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Smart Money Picks</title>
<style>
  :root { --bg:#f7f7f5; --card:#fff; --text:#1d1d1f; --muted:#6e6e73; --line:#e5e5ea;
          --accent:#2f6fed; --up:#1a8a4a; --down:#c4372b; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#141416; --card:#1e1e21; --text:#f2f2f4; --muted:#9a9aa0; --line:#2e2e33;
            --accent:#6b9bff; --up:#4cc27e; --down:#ff6b5e; } }
  body { margin:0; padding:24px 16px; background:var(--bg); color:var(--text);
         font:15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  main { max-width:1100px; margin:0 auto; }
  h1 { font-size:24px; margin:0 0 4px; }
  .sub { color:var(--muted); margin:0 0 20px; }
  .wrap { background:var(--card); border:1px solid var(--line); border-radius:12px; overflow-x:auto; }
  table { width:100%; border-collapse:collapse; }
  th, td { padding:10px 12px; text-align:left; border-bottom:1px solid var(--line); white-space:nowrap; }
  td.market { white-space:normal; min-width:260px; }
  th { font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted);
       cursor:pointer; user-select:none; position:sticky; top:0; background:var(--card); }
  th:hover { color:var(--text); }
  tr:last-child td { border-bottom:0; }
  .num { text-align:right; font-variant-numeric:tabular-nums; }
  .pick { font-weight:600; }
  .bettors { font-weight:700; font-size:17px; }
  .bar { display:inline-block; width:70px; height:6px; border-radius:3px; background:var(--line);
         vertical-align:middle; margin-left:8px; overflow:hidden; }
  .bar i { display:block; height:100%; background:var(--accent); }
  .up { color:var(--up); } .down { color:var(--down); }
  a { color:var(--accent); text-decoration:none; }
  .note { color:var(--muted); font-size:13px; margin-top:16px; }
__ODDS_CSS__
</style></head><body><main>
<h1>Smart Money Picks: __CATEGORY__</h1>
<p class="sub">__SUBTITLE__</p>
__ODDS_TOGGLE__
<div class="wrap"><table id="t"><thead><tr>
  <th>#</th><th>Pick</th><th>Market</th><th class="num">Bettors</th>
  <th class="num">Avg bankroll %</th><th class="num">Total $ in</th>
  <th class="num">Entry &rarr; Now</th><th></th>
</tr></thead><tbody>
__ROWS__
</tbody></table></div>
<p class="note">Bettors = top leaderboard traders holding this pick (hedged traders removed).
Avg bankroll % = share of each holder's open-bet value on this pick, averaged.
Click a column header to sort. Research only, not financial advice.</p>
</main>
<script>
document.querySelectorAll("#t th").forEach((th, col) => th.addEventListener("click", () => {
  const body = document.querySelector("#t tbody");
  const dir = th.dataset.dir === "desc" ? 1 : -1;
  th.dataset.dir = dir === 1 ? "asc" : "desc";
  [...body.rows].sort((a, b) => {
    const x = a.cells[col].dataset.v ?? a.cells[col].innerText;
    const y = b.cells[col].dataset.v ?? b.cells[col].innerText;
    return (isNaN(x) || isNaN(y) ? x.localeCompare(y) : x - y) * dir;
  }).forEach(r => body.appendChild(r));
}));
</script></body></html>
"""


def write_html(path, rows, args):
    """Eye-friendly, sortable table of the picks."""
    esc = html.escape
    top_pct = max([r["Avg bankroll %"] for r in rows] or [1]) or 1
    out = []
    for n, r in enumerate(rows, 1):
        move = r["Price now"] - r["Avg entry price"]
        cls = "up" if move > 0 else "down" if move < 0 else ""
        link = f'<a href="{esc(r["Link"])}" target="_blank">View &rarr;</a>' if r["Link"] else ""
        out.append(
            f'<tr><td class="num" data-v="{n}">{n}</td>'
            f'<td class="pick">{esc(str(r["Pick"]))}</td>'
            f'<td class="market">{esc(r["Market"])}</td>'
            f'<td class="num bettors" data-v="{r["Bettors"]}">{r["Bettors"]}</td>'
            f'<td class="num" data-v="{r["Avg bankroll %"]}">{r["Avg bankroll %"]:.1f}%'
            f'<span class="bar"><i style="width:{100 * r["Avg bankroll %"] / top_pct:.0f}%"></i></span></td>'
            f'<td class="num" data-v="{r["Total $ in"]}">${r["Total $ in"]:,}</td>'
            f'<td class="num {cls}" data-v="{move:.2f}">{px(r["Avg entry price"])} &rarr; {px(r["Price now"])}</td>'
            f'<td>{link}</td></tr>')
    if not out:
        out.append('<tr><td colspan="8">No picks met the filters this run.</td></tr>')
    subtitle = (f"Top {args.top} {args.category} traders by {args.sort} ({args.period}), "
                f"picks held by at least {args.min_traders}, "
                + (f"price within {args.max_move * 100:.0f}¢ of their entry. " if args.max_move < 1
                   else "no price-move filter. ")
                + f"Generated {time.strftime('%b %d, %Y %I:%M %p')}.")
    page = (HTML_PAGE.replace("__ODDS_CSS__", ODDS_CSS).replace("__ODDS_TOGGLE__", ODDS_TOGGLE)
            .replace("</body>", ODDS_JS + "</body>")
            .replace("__CATEGORY__", esc(args.category.title()))
            .replace("__SUBTITLE__", esc(subtitle)).replace("__ROWS__", "\n".join(out)))
    with open(path, "w") as f:
        f.write(page)


def build_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=100, help="how many leaderboard traders")
    ap.add_argument("--period", default="month", choices=["day", "week", "month", "all"])
    ap.add_argument("--category", default="sports",
                    help="sports, politics, crypto, esports, ... or overall. Also keeps only "
                         "positions in that category")
    ap.add_argument("--sort", default="PNL", choices=["PNL", "VOLUME"])
    ap.add_argument("--min-value", type=float, default=100, help="ignore positions worth < $X")
    ap.add_argument("--min-traders", type=int, default=3, help="min top traders on same side")
    ap.add_argument("--max-move", type=float, default=0.04,
                    help="skip picks whose price is now more than this far from the traders' "
                         "average entry (0.04 = 4 cents); 1 turns the filter off")
    ap.add_argument("--max-crypto", type=float, default=0.5,
                    help="skip traders whose recent trades are more than this share crypto "
                         "price bets (likely bots); 1 turns the filter off")
    return ap


def find_picks(args):
    """Scan the leaderboard and return (picks, traders), picks sorted best first."""
    board = leaderboard(args.top, args.period, args.category, args.sort)
    print(f"Leaderboard: {len(board)} traders ({args.period}, {args.category}, {args.sort})")

    markets = defaultdict(lambda: {"title": "", "event_id": "", "slug": "", "token_id": "",
                                   "outcome_index": None, "traders": set(),
                                   "bankroll_pcts": [], "cost": 0.0, "value": 0.0, "shares": 0.0})
    trader_rows = []

    for i, t in enumerate(board, 1):
        wallet = t.get("user_id") or t.get("proxy_wallet")
        name = t.get("user_name") or wallet
        try:
            crypto = crypto_share(wallet)
            if crypto > args.max_crypto:
                print(f"  [{i}/{len(board)}] skip {name}: {crypto:.0%} crypto price bets (likely bot)")
                continue
            positions = open_positions(wallet, args.min_value)
            hedged = hedged_markets(wallet, {p["condition_id"] for p in positions})
            # bankroll = value of all the trader's open bets (uninvested cash isn't public)
            bankroll = get("/value", {"user": wallet})["data"]["value"] if positions else 0
        except Exception as e:  # keep going if one wallet fails
            print(f"  skip {name}: {e}")
            continue
        positions = [p for p in positions if p["condition_id"] not in hedged]
        trader_rows.append({"rank": t.get("rank", i), "name": name, "wallet": wallet,
                            "pnl": t.get("pnl"), "volume": t.get("volume"),
                            "crypto_share": round(crypto, 2),
                            "open_positions": len(positions),
                            "hedged_markets_dropped": len(hedged),
                            "open_value": round(sum(p.get("current_value") or 0 for p in positions), 2)})
        for p in positions:
            m = markets[(p["condition_id"], p.get("outcome"))]
            m["title"] = p.get("title", "")
            m["event_id"] = str(p.get("event_id", ""))
            m["slug"] = p.get("event_slug") or ""
            m["token_id"] = p.get("token_id") or ""
            m["outcome_index"] = p.get("outcome_index")
            m["traders"].add(wallet)
            if bankroll > 0:
                m["bankroll_pcts"].append(100 * (p.get("current_value") or 0) / bankroll)
            m["cost"] += p.get("entry_cost_usdc") or 0
            m["value"] += p.get("current_value") or 0
            m["shares"] += p.get("current_size") or 0
        print(f"  [{i}/{len(board)}] {name}: {len(positions)} open positions"
              f" ({len(hedged)} hedged markets dropped)")

    tags = None
    if args.category != "overall":
        candidates = {m["event_id"] for m in markets.values() if len(m["traders"]) >= args.min_traders}
        tags = event_tags(candidates)

    rows = []
    for (cond, outcome), m in markets.items():
        if len(m["traders"]) < args.min_traders or m["shares"] <= 0:
            continue
        if tags is not None and args.category not in tags.get(m["event_id"], set()):
            continue
        avg_entry, price_now = m["cost"] / m["shares"], m["value"] / m["shares"]
        if abs(price_now - avg_entry) > args.max_move:  # price already ran (or fell) too far
            continue
        pcts = m["bankroll_pcts"]
        rows.append({
            "Pick": outcome,
            "Market": m["title"],
            "Bettors": len(m["traders"]),
            # how much of their bankroll the average holder has on this pick
            "Avg bankroll %": round(sum(pcts) / len(pcts), 1) if pcts else 0.0,
            "Total $ in": round(m["cost"]),
            "Avg entry price": round(avg_entry, 2),
            "Price now": round(price_now, 2),
            "Link": f"https://polymarket.com/event/{m['slug']}" if m["slug"] else "",
            "condition_id": cond,
            "token_id": m["token_id"],
            "outcome_index": m["outcome_index"],
        })
    rows.sort(key=lambda r: (r["Bettors"], r["Avg bankroll %"]), reverse=True)
    return rows, trader_rows


def main():
    args = build_parser().parse_args()
    rows, trader_rows = find_picks(args)

    for path, data in (("consensus.csv", rows), ("traders.csv", trader_rows)):
        with open(path, "w", newline="") as f:  # always overwrite so old results never linger
            if data:
                w = csv.DictWriter(f, fieldnames=list(data[0]))
                w.writeheader()
                w.writerows(data)
    write_html("picks.html", rows, args)

    print(f"\nTop {args.category} consensus picks "
          f"(>= {args.min_traders} top traders on the same outcome, none hedged):")
    for r in rows[:15]:
        print(f"{r['Bettors']:>3} bettors  {r['Avg bankroll %']:>5.1f}% of bankroll  "
              f"${r['Total $ in']:>10,}  entry {r['Avg entry price']:.2f} -> now {r['Price now']:.2f}  "
              f"{r['Pick']}: {r['Market'][:60]}")
    print("\nWrote consensus.csv, traders.csv and picks.html (open picks.html in a browser)")


if __name__ == "__main__":
    main()
