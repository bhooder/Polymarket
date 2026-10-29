"""
Where a US trader could actually place a pick: Polymarket US and Kalshi.

Picks come from international Polymarket (polymarket.com) data. This module finds
the same bet on each US venue, prices it from that venue's real order book with
its fees, and settles it later. Read-only public data; no orders are placed.

Matching (only exact equivalents, otherwise the venue is skipped):
- Polymarket US reuses international slugs with a prefix:
    game winner   nba-bkn-bos-2026-10-27             -> aec-nba-bkn-bos-2026-10-27
    soccer 1X2    isp-che-pun-2026-03-22-che         -> atc-isp-che-pun-2026-03-22-che
    total         <event>-total-222pt5               -> tsc-<event>-222pt5
    spread        <event>-spread-away-5pt5 (away -5.5) -> asc-<event>-neg-5pt5
                  <event>-spread-home-5pt5 (home -5.5) -> asc-<event>-pos-5pt5
  ("long" on a US spread = the first/away team covers.)
- Kalshi tickers: SERIES-YYMONDD + AWAY + HOME codes, e.g. KXNBAGAME-26OCT27PORLAL-POR.
  Tennis codes are the first 3 letters of each surname.
"""
import json
import math
from datetime import datetime, timedelta

import polymarket_smart_money as scanner

PM_US = "https://gateway.polymarket.us/v1"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_FEE = 0.07  # taker fee = roundup(0.07 x multiplier x C x P x (1-P)) to the cent

# international slug prefix -> Kalshi series prefix (game, spread and total series add a suffix)
KALSHI_LEAGUES = {
    "nba": "KXNBA", "nfl": "KXNFL", "nhl": "KXNHL", "mlb": "KXMLB", "wnba": "KXWNBA",
    "cfb": "KXNCAAF", "cbb": "KXNCAAMB", "epl": "KXEPL", "ucl": "KXUCL", "uel": "KXUEL",
    "lal": "KXLALIGA", "sea": "KXSERIEA", "bun": "KXBUNDESLIGA", "fl1": "KXLIGUE1",
    "mls": "KXMLS", "atp": "KXATP", "wta": "KXWTA",
}
TENNIS = {"atp", "wta"}
SANITY = 0.15  # a "match" priced this far from Polymarket is probably the wrong team


def safe_get(path, params=None, base=PM_US):
    try:
        return scanner.get(path, params or {}, base=base)
    except Exception:
        return None  # not listed / not found


def fill(levels, dollars, fee_per_share):
    """Buy up to `dollars` walking `levels` [(price, qty)] cheapest first.
    fee_per_share(p) is the fee for one share at price p."""
    shares = spent = fees = 0.0
    for p, qty in sorted(levels):
        if p <= 0 or p >= 1:
            continue
        unit = p + fee_per_share(p)
        take = min(qty, (dollars - spent) / unit)
        shares += take
        spent += take * unit
        fees += take * fee_per_share(p)
        if dollars - spent < 0.01:
            break
    if shares <= 0 or dollars - spent > 0.01:
        return None  # book too thin
    return {"shares": shares, "cost": spent, "fees": fees, "avg_price": (spent - fees) / shares}


# ---------- figure out what kind of bet the international pick is ----------

def describe(pick, m):
    """Break an international market into (event slug, kind, details) for matching."""
    slug = m.get("slug") or ""
    events = m.get("events") or []
    event = events[0].get("slug") if events else None
    outcomes = scanner_json(m.get("outcomes"))
    kind = m.get("sportsMarketType")
    if not event:
        return None
    if slug == event and len(outcomes) == 2 and outcomes[0] not in ("Yes", "No"):
        return {"event": event, "kind": "winner", "outcomes": outcomes}
    if kind == "moneyline" and slug.startswith(event + "-") and outcomes == ["Yes", "No"]:
        title = events[0].get("title") or ""
        return {"event": event, "kind": "1x2", "team": slug[len(event) + 1:],  # team code or "draw"
                "names": title.split(" vs. "), "team_name": m.get("groupItemTitle") or ""}
    if kind == "totals" and slug.startswith(event + "-total-"):
        return {"event": event, "kind": "total", "line": float(m["line"]), "tag": slug.rsplit("-", 1)[1]}
    if kind == "spreads" and "-spread-" in slug:
        side, tag = slug[len(event) + 1:].split("-")[1:3]  # away|home, e.g. 5pt5
        return {"event": event, "kind": "spread", "fav": side, "tag": tag,
                "line": abs(float(m["line"])), "outcomes": outcomes}
    return None  # props, futures, etc.: international only


def scanner_json(v):
    return json.loads(v) if isinstance(v, str) else (v or [])


# ---------------- Polymarket US ----------------

def pm_us_ref(pick, d):
    e = d["event"]
    if d["kind"] == "winner":
        slug = f"aec-{e}"
    elif d["kind"] == "1x2":
        slug = f"atc-{e}-{d['team']}"
    elif d["kind"] == "total":
        slug = f"tsc-{e}-{d['tag']}"
    else:
        slug = f"asc-{e}-{'neg' if d['fav'] == 'away' else 'pos'}-{d['tag']}"
    r = safe_get(f"/market/slug/{slug}")
    m = (r or {}).get("market")
    if not m or m.get("closed") or not m.get("active"):
        return None
    if d["kind"] == "spread":
        # long = away team covers. Away team is outcomes[0] on "away" markets, outcomes[1] on "home".
        away = d["outcomes"][0] if d["fav"] == "away" else d["outcomes"][1]
        long = pick["Pick"] == away
    elif d["kind"] == "1x2":
        long = pick["Pick"] == "Yes"
    else:
        sides = {s.get("description"): s.get("long") for s in m.get("marketSides") or []}
        if pick["Pick"] not in sides:
            return None
        long = sides[pick["Pick"]]
    return {"venue": "polymarket_us", "ref": slug, "long": long,
            "fee_rate": float(m.get("feeCoefficient") or 0)}


def pm_us_quote(ref, dollars):
    r = safe_get(f"/markets/{ref['ref']}/book")
    book = (r or {}).get("marketData") or {}
    if ref["long"]:
        levels = [(float(o["px"]["value"]), float(o["qty"])) for o in book.get("offers") or []]
    else:  # buying the other side = selling to the long bids
        levels = [(1 - float(b["px"]["value"]), float(b["qty"])) for b in book.get("bids") or []]
    rate = ref["fee_rate"]
    return fill(levels, dollars, lambda p: rate * p * (1 - p))


def pm_us_result(ref):
    """Payout per share once resolved, else None."""
    m = (safe_get(f"/market/slug/{ref['ref']}") or {}).get("market") or {}
    if m.get("status") != "MARKET_STATUS_RESOLVED":
        return None
    long_px = next((float(s["price"]) for s in m.get("marketSides") or [] if s.get("long")), None)
    if long_px is None:
        return None
    return long_px if ref["long"] else 1 - long_px


# ---------------- Kalshi ----------------

def kalshi_codes(league, event):
    """Team codes and dates to try, from an international event slug."""
    parts = event.split("-")
    date = datetime.strptime("-".join(parts[-3:]), "%Y-%m-%d")
    teams = parts[1:-3]
    if league in TENNIS:
        if "doubles" in teams or len(teams) != 2:
            return None
        teams = [t[:3] for t in teams]
    if len(teams) != 2:
        return None
    dates = [date, date + timedelta(days=1), date - timedelta(days=1)]  # time-zone slack
    return [t.upper() for t in teams], [d.strftime("%y%b%d").upper() for d in dates]


FILLER = {"fc", "afc", "cf", "sc", "ac", "the", "club", "de", "cd", "ssc", "as", "&", "and"}


def name_tokens(name):
    return [t for t in "".join(c if c.isalnum() or c == " " else " " for c in name.lower()).split()
            if t not in FILLER]


def same_team(kalshi_name, intl_name):
    """'Manchester U' matches 'Manchester United FC' but not 'Manchester City FC'."""
    k, i = name_tokens(kalshi_name), name_tokens(intl_name)
    return bool(k) and len(k) <= len(i) and all(b.startswith(a) for a, b in zip(k, i))


def kalshi_soccer_event(series, dates, names):
    """Find a soccer game on Kalshi by date and team names (its team codes differ)."""
    if len(names) != 2:
        return None
    for ticker in open_events(series):
        if not any(ticker.split("-")[1].startswith(day) for day in dates):
            continue
        e = (safe_get(f"/events/{ticker}", {"with_nested_markets": "true"}, base=KALSHI) or {}).get("event")
        teams = [m.get("yes_sub_title") or "" for m in (e or {}).get("markets") or []
                 if not m["ticker"].endswith("-TIE")]
        if len(teams) == 2 and all(any(same_team(k, n) for k in teams) for n in names):
            return e
    return None


_open_events = {}


def open_events(series):
    """Tickers of a Kalshi series' open events (cached for this run)."""
    if series not in _open_events:
        tickers, cursor = [], None
        for _ in range(10):  # at most 2,000 events
            params = {"series_ticker": series, "status": "open", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            r = safe_get("/events", params, base=KALSHI) or {}
            tickers += [e["event_ticker"] for e in r.get("events") or []]
            cursor = r.get("cursor")
            if not cursor:
                break
        _open_events[series] = tickers
    return _open_events[series]


def kalshi_ref(pick, d, intl_price):
    league = d["event"].split("-")[0]
    base = KALSHI_LEAGUES.get(league)
    codes = base and kalshi_codes(league, d["event"])
    if not codes:
        return None
    (away, home), dates = codes
    kind = {"winner": "MATCH" if league in TENNIS else "GAME", "1x2": "GAME",
            "total": "TOTAL", "spread": "SPREAD"}[d["kind"]]
    event = None
    for series in [base + kind] + ([base + "CHALLENGER" + kind] if league in TENNIS else []):
        for day in dates:
            r = safe_get(f"/events/{series}-{day}{away}{home}", {"with_nested_markets": "true"}, base=KALSHI)
            if r and r.get("event"):
                event = r["event"]
                break
        if not event and d["kind"] == "1x2":
            event = kalshi_soccer_event(series, dates, d["names"])
        if not event and league in TENNIS:
            # tennis tickers carry the tournament's date, not the match day: search open events
            ticker = next((t for t in open_events(series) if t.endswith(away + home)), None)
            r = ticker and safe_get(f"/events/{ticker}", {"with_nested_markets": "true"}, base=KALSHI)
            event = r and r.get("event")
        if event:
            break
    if not event:
        return None
    markets = [m for m in event.get("markets") or [] if m.get("status") == "active"]
    suffix = lambda m: m["ticker"].rsplit("-", 1)[1]
    if d["kind"] == "winner":
        code = [away, home][d["outcomes"].index(pick["Pick"])]
        m, yes = next((m for m in markets if suffix(m) == code), None), True
    elif d["kind"] == "1x2":
        if d["team"] == "draw":
            m = next((m for m in markets if suffix(m) == "TIE"), None)
        else:  # by code if Kalshi uses the same one, else by team name
            m = next((m for m in markets if suffix(m) == d["team"].upper()), None) or next(
                (m for m in markets if suffix(m) != "TIE"
                 and same_team(m.get("yes_sub_title") or "", d["team_name"])), None)
        yes = pick["Pick"] == "Yes"
    elif d["kind"] == "total":
        m = next((m for m in markets if m.get("floor_strike") == d["line"]), None)
        yes = pick["Pick"] == "Over"
    else:
        fav = away if d["fav"] == "away" else home
        fav_name = d["outcomes"][0]  # the favoured team is listed first on international spreads
        m = next((m for m in markets if suffix(m) == f"{fav}{math.ceil(d['line'])}"
                  and m.get("floor_strike") == d["line"]), None)
        yes = pick["Pick"] == fav_name
    if not m:
        return None
    ask = float(m.get("yes_ask_dollars" if yes else "no_ask_dollars") or 0)
    if not ask or abs(ask - intl_price) > SANITY:
        return None  # priced too differently: likely matched the wrong team, so don't trust it
    mult = float((safe_get(f"/series/{series}", base=KALSHI) or {}).get("series", {}).get("fee_multiplier") or 1)
    return {"venue": "kalshi", "ref": m["ticker"], "yes": yes, "fee_rate": KALSHI_FEE * mult}


def kalshi_quote(ref, dollars):
    r = safe_get(f"/markets/{ref['ref']}/orderbook", base=KALSHI)
    book = (r or {}).get("orderbook_fp") or {}
    # Kalshi lists bids only: buying YES at p means matching a NO bid at 1-p, and vice versa.
    other = book.get("no_dollars" if ref["yes"] else "yes_dollars") or []
    levels = [(1 - float(p), float(q)) for p, q in other]
    rate = ref["fee_rate"]
    got = fill(levels, dollars - 0.01, lambda p: rate * p * (1 - p))  # leave a cent for rounding
    if got:
        rounded = math.ceil(got["fees"] * 100 - 1e-9) / 100  # Kalshi rounds the fee up to the cent
        got["cost"] += rounded - got["fees"]
        got["fees"] = rounded
    return got


def kalshi_result(ref):
    m = (safe_get(f"/markets/{ref['ref']}", base=KALSHI) or {}).get("market") or {}
    if m.get("result") not in ("yes", "no"):
        return None
    return 1.0 if (m["result"] == "yes") == ref["yes"] else 0.0


# ---------------- public helpers ----------------

def quotes(pick, intl_market, dollars):
    """Priced fills on every US venue that lists this exact bet, cheapest first."""
    d = describe(pick, intl_market)
    if not d:
        return []
    intl_price = float(pick.get("Price now") or 0)
    out = []
    for ref_fn, quote_fn in ((pm_us_ref, pm_us_quote), (kalshi_ref, kalshi_quote)):
        ref = ref_fn(pick, d) if ref_fn is pm_us_ref else ref_fn(pick, d, intl_price)
        got = ref and quote_fn(ref, dollars)
        if got:
            out.append({**ref, **got})
    return sorted(out, key=lambda q: q["cost"] / q["shares"])  # lowest all-in cost per share


def top_up_quote(ref, dollars):
    return (pm_us_quote if ref["venue"] == "polymarket_us" else kalshi_quote)(ref, dollars)


def result(ref):
    return (pm_us_result if ref["venue"] == "polymarket_us" else kalshi_result)(ref)
