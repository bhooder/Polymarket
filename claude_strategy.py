"""
Strategy "Claude": copy only traders with a proven record of beating the closing price.

1. Report card. Every run, each sports game position ($100+) held by the scanned traders is
   noted with the trader's average entry price (updated until the game starts). Once the game
   starts, it is graded against the price just before the start ("closing price", from
   Polymarket's price history): CLV = closing price - entry. Positive = they bought cheaper
   than where the market ended up, the classic sign of a sharp bettor. CLV doesn't wait for
   results, so it shows skill much faster than wins and losses.
2. A trader QUALIFIES once they have MIN_GRADED+ graded positions, an average CLV of at least
   MIN_CLV, and a t-score (average / standard error) of at least MIN_T. The bar is set high
   because ~150 traders are graded at once, so a few would look good by luck alone.
3. Picks: outcomes held by qualified traders, where at least one of them first bought within
   the last FRESH_HOURS. 1% stake with one qualified trader, 2% with two or more (a 1% bet tops
   up once if a second one joins). Every other forward-test rule applies (price within 2c
   above / 4c below their entry, games within 3 days, not started, 50% cap, underdog sizing,
   cheapest US venue, never against as many qualified traders).
Rules fixed 2026-11-10, before any data, so they can't be tuned to fit the results.
"""
import json
import math
import os
import time
from datetime import datetime, timedelta, timezone

import polymarket_smart_money as scanner

CLOB = "https://clob.polymarket.com"
MIN_GRADED = 25      # graded positions needed before a trader can qualify
MIN_CLV = 0.01       # average CLV of at least 1c...
MIN_T = 2.5          # ...and at least 2.5 standard errors above zero
FRESH_HOURS = 6      # only copy positions first bought within this many hours
TRACK_DAYS = 3       # note positions in games starting within this many days (the bet window)
KEEP_UNSEEN_DAYS = 7  # forget non-game markets not seen for this long (keeps the file small)
RECENT_KEEP = 300    # latest graded positions kept for the report


def now():
    return datetime.now(timezone.utc)


def new_card():
    return {"started_at": now().isoformat(), "markets": {}, "pending": {}, "traders": {}, "recent": []}


def game_starts(card, condition_ids):
    """condition_id -> game start (ISO) or None for non-game markets, cached in the card."""
    markets = card["markets"]
    todo = sorted(c for c in condition_ids if c not in markets)
    for i in range(0, len(todo), 50):
        found = {m["conditionId"]: m for m in scanner.get(
            "/markets", {"condition_ids": todo[i:i + 50], "limit": 50}, base=scanner.GAMMA)}
        for c in todo[i:i + 50]:
            start = (found.get(c) or {}).get("gameStartTime")
            if start:
                start = datetime.fromisoformat(start.replace("Z", "+00:00").replace(" ", "T"))
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                start = start.isoformat()
            markets[c] = {"start": start}
    stamp = now().date().isoformat()  # a day is precise enough, and keeps the file from changing every run
    for c in condition_ids:
        markets[c]["seen"] = stamp
    return {c: markets[c]["start"] for c in condition_ids}


def record(card, positions):
    """positions = {wallet: (name, [position, ...])}. Note game positions not yet started."""
    starts = game_starts(card, {p["condition_id"] for _, ps in positions.values() for p in ps})
    t = now()
    for wallet, (name, ps) in positions.items():
        for p in ps:
            start = starts.get(p["condition_id"])
            entry = p.get("avg_price") or 0
            if (not start or not t < datetime.fromisoformat(start) <= t + timedelta(days=TRACK_DAYS)
                    or not 0.02 < entry < 0.98):
                continue
            key = f'{wallet}|{p["token_id"]}'
            old = card["pending"].get(key) or {"first_seen": t.isoformat()}
            card["pending"][key] = {**old, "name": name, "start": start, "entry": round(entry, 4),
                                    "last_price": p.get("current_price"), "pick": p.get("outcome"), "market": p.get("title", "")}


def closing_price(token, start, cache):
    """Last traded price before the game started (Polymarket price history), or None."""
    if token not in cache:
        ts = int(datetime.fromisoformat(start).timestamp())
        try:
            hist = scanner.get("/prices-history", {"market": token, "startTs": ts - 6 * 3600,
                                                  "endTs": ts, "fidelity": 1}, base=CLOB)
            points = [h for h in hist.get("history") or [] if h["t"] <= ts]
            cache[token] = points[-1]["p"] if points else None
        except Exception:
            cache[token] = None
    return cache[token]


def grade(card):
    """Grade every pending position whose game has started. Returns how many were graded."""
    t, cache, graded = now(), {}, 0
    for key, p in list(card["pending"].items()):
        if datetime.fromisoformat(p["start"]) > t:
            continue
        wallet, token = key.split("|")
        close = closing_price(token, p["start"], cache)
        if close is None:
            close = p.get("last_price")  # our own last look before the start
        del card["pending"][key]
        if close is None:
            continue
        clv = close - p["entry"]
        tr = card["traders"].setdefault(wallet, {"name": p["name"], "n": 0, "sum": 0.0, "sumsq": 0.0})
        tr["name"] = p["name"]
        tr["n"] += 1
        tr["sum"] += clv
        tr["sumsq"] += clv * clv
        tr["last_graded"] = t.isoformat()
        card["recent"].append({"name": p["name"], "pick": p["pick"], "market": p["market"],
                               "entry": p["entry"], "close": close, "clv": round(clv, 4), "start": p["start"]})
        graded += 1
    card["recent"] = card["recent"][-RECENT_KEEP:]
    # forget non-game markets nobody has held for a while
    cutoff = (t - timedelta(days=KEEP_UNSEEN_DAYS)).date().isoformat()
    for c in [c for c, m in card["markets"].items() if m.get("seen", "") < cutoff]:
        del card["markets"][c]
    return graded


def score(tr):
    """(average CLV, t-score) for one trader's graded positions."""
    n = tr["n"]
    mean = tr["sum"] / n if n else 0.0
    if n < 2:
        return mean, 0.0
    var = max(tr["sumsq"] / n - mean * mean, 0.0) * n / (n - 1)
    se = math.sqrt(var / n)
    return mean, (mean / se if se > 0 else 0.0)


def qualified(card):
    """Wallets that pass the report card."""
    out = set()
    for wallet, tr in card["traders"].items():
        mean, t = score(tr)
        if tr["n"] >= MIN_GRADED and mean >= MIN_CLV and t >= MIN_T:
            out.add(wallet)
    return out


def extra_positions(card, scanned, min_value):
    """Qualified traders who fell off this run's leaderboard are still followed."""
    out = {}
    for wallet in qualified(card) - set(scanned):
        try:
            ps = scanner.open_positions(wallet, min_value)
            hedged = scanner.hedged_markets(wallet, {p["condition_id"] for p in ps})
            out[wallet] = (card["traders"][wallet]["name"], [p for p in ps if p["condition_id"] not in hedged])
        except Exception as e:
            print(f"  skip {card['traders'][wallet]['name']}: {e}")
    return out


def picks(card, positions):
    """Picks in the scanner's format, from qualified traders' fresh positions."""
    sharp = qualified(card)
    fresh_after = time.time() - FRESH_HOURS * 3600
    sides = {}
    for wallet, (_, ps) in positions.items():
        if wallet not in sharp:
            continue
        for p in ps:
            sides.setdefault(p["condition_id"], {}).setdefault(p.get("outcome"), []).append(p)
    rows = []
    for cond, outcomes in sides.items():
        if not (card["markets"].get(cond) or {}).get("start"):
            continue  # games only
        for outcome, held in outcomes.items():
            if not any((p.get("first_entry_at") or 0) >= fresh_after for p in held):
                continue
            size = sum(p.get("current_size") or 0 for p in held)
            if size <= 0:
                continue
            p0 = held[0]
            slug = p0.get("event_slug") or ""
            rows.append({
                "Pick": outcome, "Market": p0.get("title", ""), "Bettors": len(held),
                "Against": sum(len(h) for o, h in outcomes.items() if o != outcome),
                "Avg bankroll %": 0.0,
                "Avg entry price": round(sum(p["avg_price"] * (p.get("current_size") or 0) for p in held) / size, 2),
                "Price now": p0.get("current_price") or 0,
                "Link": f"https://polymarket.com/event/{slug}" if slug else "",
                "condition_id": cond, "token_id": p0["token_id"], "outcome_index": p0.get("outcome_index"),
            })
    rows.sort(key=lambda r: r["Bettors"], reverse=True)
    return rows


def save(card, path):
    """Compact JSON (this file is committed every run, so keep it small)."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(card, f, separators=(",", ":"))
    os.replace(tmp, path)


def card_html(card, esc):
    """Report-card section for the Claude tab."""
    sharp = qualified(card)
    graded = sum(tr["n"] for tr in card["traders"].values())
    tiles = [(f'{len(card["pending"])}', "Positions waiting for game start"),
             (f"{graded}", "Positions graded"),
             (f'{len(card["traders"])}', "Traders graded"),
             (f"{len(sharp)}", "Traders qualified")]
    tiles_html = "".join(f'<div class="tile"><b>{v}</b><span>{k}</span></div>' for v, k in tiles)
    rows = []
    ranked = sorted(card["traders"].items(), key=lambda kv: score(kv[1])[1], reverse=True)
    for wallet, tr in [kv for kv in ranked if kv[1]["n"] >= 5][:25]:
        mean, t = score(tr)
        status = ("Qualified" if wallet in sharp else
                  f'Needs {MIN_GRADED - tr["n"]} more' if tr["n"] < MIN_GRADED else "Not yet")
        rows.append(f'<tr><td class="pick">{esc(tr["name"])}</td><td class="num">{tr["n"]}</td>'
                    f'<td class="num {"up" if mean > 0 else "down" if mean < 0 else ""}">{mean * 100:+.1f}&cent;</td>'
                    f'<td class="num">{t:+.1f}</td><td>{status}</td></tr>')
    body = "".join(rows) or '<tr><td colspan="5">No trader has 5 graded positions yet.</td></tr>'
    return f"""<h2>Trader report card</h2>
<p class="sub">Each top trader's game bets are graded against the price just before the game started.
Beating that "closing price" on average is the best quick sign of a sharp bettor. A trader qualifies
at {MIN_GRADED}+ graded positions, an average of +{MIN_CLV * 100:.0f}&cent; or better, and a t-score of
{MIN_T}+ (how sure we can be it isn't luck). Collecting since {card["started_at"][:10]}.</p>
<div class="tiles">{tiles_html}</div>
<h2>Best graded traders (5+ positions)</h2>
<div class="wrap"><table><thead><tr><th>Trader</th><th class="num">Graded</th>
<th class="num">Avg vs close</th><th class="num">t-score</th><th>Status</th></tr></thead>
<tbody>{body}</tbody></table></div>"""
