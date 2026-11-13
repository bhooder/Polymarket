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
import os
import re
import unicodedata
from datetime import datetime, timedelta

import requests

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
    # added 2026-11-13 (paper only at first, see PAPER_ONLY)
    "elc": "KXEFLCHAMPIONSHIP", "el1": "KXEFLL1", "el2": "KXEFLL2", "efa": "KXFACUP", "efl": "KXEFLCUP",
    "arg": "KXARGPREMDIV", "spl": "KXSAUDIPL", "ere": "KXEREDIVISIE", "jap": "KXJLEAGUE",
    "ja2": "KXJ2LEAGUE", "bra": "KXBRASILEIRO", "bra2": "KXBRASILEIROB", "por": "KXLIGAPORTUGAL",
    "bel1": "KXBELGIANPL", "swe": "KXALLSVENSKAN", "bl2": "KXBUNDESLIGA2", "mex": "KXLIGAMX",
    "unl": "KXUEFANL", "pol": "KXEKSTRAKLASA", "den": "KXDENSUPERLIGA", "tur": "KXSUPERLIG",
    "kor": "KXKLEAGUE", "aus": "KXALEAGUE", "es2": "KXLALIGA2", "fr2": "KXLIGUE2", "itsb": "KXSERIEB",
    "nor": "KXELITESERIEN", "aut": "KXAUTBUNDES", "col1": "KXDIMAYOR", "scop": "KXSCOTTISHPREM",
    "nwsl": "KXNWSL", "uwcl": "KXUCLW", "acle": "KXAFCCL", "cdr": "KXCOPADELREY", "gre1": "KXSLGREECE",
    "chi1": "KXCHLLDP", "ecu1": "KXECULP", "per1": "KXPERLIGA1",
}
# Matched by date + both names (each site uses its own codes): series and name matcher.
KALSHI_NAMED = {"ufc": "KXUFCFIGHT", "cs2": "KXCS2GAME", "lol": "KXLOLGAME"}
# Sports bet on paper only (no real money) until ~30 bets settle; then decide. Added 2026-11-13.
PAPER_ONLY = (set(KALSHI_LEAGUES) - {"nba", "nfl", "nhl", "mlb", "wnba", "cfb", "cbb", "epl", "ucl", "uel",
                                     "lal", "sea", "bun", "fl1", "mls", "atp", "wta"}) | {"cs2", "lol"}
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

MORE = "-more-markets"  # soccer spreads etc. sit on a second event page, e.g. epl-liv-ars-2026-11-01-more-markets


def base_event(slug):
    """The game's own event slug, without the "-more-markets" suffix."""
    return slug[:-len(MORE)] if slug and slug.endswith(MORE) else slug


def describe(pick, m):
    """Break an international market into (event slug, kind, details) for matching."""
    slug = m.get("slug") or ""
    events = m.get("events") or []
    event = base_event(events[0].get("slug")) if events else None
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
    spread = re.fullmatch(re.escape(event) + r"-spread-(away|home)-(\w+)", slug)
    if kind == "spreads" and spread:
        side, tag = spread.groups()  # away|home, e.g. 5pt5
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
    """'Manchester U' matches 'Manchester United FC' but not 'Manchester City FC'.
    Also 'Broendby' ~ 'Brøndby IF' and 'Estrela Amadora' ~ 'CF Estrela da Amadora' (2026-11-13).
    Callers also require the other team, the date and the price to agree."""
    k, i = name_tokens(kalshi_name), name_tokens(intl_name)
    if bool(k) and len(k) <= len(i) and all(b.startswith(a) for a, b in zip(k, i)):
        return True
    return px_same(kalshi_name, intl_name)


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


def kalshi_named_ref(pick, d, intl_price, league):
    """A UFC fight or esports match on Kalshi, found among open events by date and both
    names (Kalshi's codes differ from Polymarket's)."""
    if d["kind"] != "winner" or len(d["outcomes"]) != 2:
        return None
    series = KALSHI_NAMED[league]
    same = same_fighter if league == "ufc" else same_esports_team
    try:
        day = datetime.strptime("-".join(d["event"].split("-")[-3:]), "%Y-%m-%d")
    except ValueError:
        return None
    dates = [(day + timedelta(days=k)).strftime("%y%b%d").upper() for k in (0, 1, -1)]  # time-zone slack
    for ticker in open_events(series):
        if not any(ticker.split("-")[1].startswith(day) for day in dates):
            continue
        e = (safe_get(f"/events/{ticker}", {"with_nested_markets": "true"}, base=KALSHI) or {}).get("event")
        markets = [m for m in (e or {}).get("markets") or [] if m.get("status") == "active"]
        names = [m.get("yes_sub_title") or "" for m in markets]
        if len(names) != 2 or not all(sum(same(k, n) for k in names) == 1 for n in d["outcomes"]):
            continue
        hits = [m for m in markets if same(m.get("yes_sub_title") or "", pick["Pick"])]
        m = hits[0] if len(hits) == 1 else None
        ask = float((m or {}).get("yes_ask_dollars") or 0)
        if not ask or abs(ask - intl_price) > SANITY:
            return None
        mult = float((safe_get(f"/series/{series}", base=KALSHI) or {}).get("series", {}).get("fee_multiplier") or 1)
        return {"venue": "kalshi", "ref": m["ticker"], "yes": True, "fee_rate": KALSHI_FEE * mult}
    return None


def kalshi_ref(pick, d, intl_price):
    league = d["event"].split("-")[0]
    if league in KALSHI_NAMED:
        return kalshi_named_ref(pick, d, intl_price, league)
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


# ---------------- ProphetX ----------------
# Needs PROPHETX_ACCESS_KEY + PROPHETX_SECRET_KEY (prices aren't public); without them it's skipped.
# Prices are American odds. Each listed price on a selection is one a taker can bet at now;
# "value" is how many dollars can be staked there. An order's "quantity" is the dollars staked.
# Settled through the international Polymarket result (same game, same line).

PX = "https://cash.api.prophetx.co/partner"
PX_FEE = 0.02  # fee as a share of WINNINGS (payout - cost), charged when a bet wins. Seen 2026-11-09 in
               # the wallet: $0.04 on $1.89 and on $2.24 won. Rates are set per account.
PX_LEAGUES = {"nba": ["nba"], "wnba": ["wnba"], "nfl": ["nfl"], "nhl": ["nhl"], "mlb": ["mlb"],
              "cfb": ["college football"], "cbb": ["college basketball"],
              "epl": ["premier league", "english premier league"], "lal": ["la liga"],
              "sea": ["serie a"], "bun": ["bundesliga"], "fl1": ["ligue 1"],
              "ucl": ["uefa champions league", "champions league"],
              "uel": ["uefa europa league", "europa league"], "mls": ["mls", "major league soccer"],
              "ufc": ["ufc"]}  # UFC added 2026-11-13: any tournament whose name contains "ufc"
_px = {}  # login token and per-run caches


def px_keys():
    return os.environ.get("PROPHETX_ACCESS_KEY"), os.environ.get("PROPHETX_SECRET_KEY")


def px_call(method, path, params=None, body=None):
    """A logged-in ProphetX request (logs in again once if the token has expired)."""
    for attempt in range(2):
        if "token" not in _px:
            key, secret = px_keys()
            r = requests.post(PX + "/auth/login", json={"access_key": key, "secret_key": secret}, timeout=30)
            r.raise_for_status()
            _px["token"] = r.json()["data"]["access_token"]
        r = requests.request(method, PX + path, params=params, json=body, timeout=30,
                             headers={"Authorization": f"Bearer {_px['token']}"})
        if r.status_code == 401 and attempt == 0:
            _px.pop("token")
            continue
        return r


def px_get(path, params=None):
    if not all(px_keys()) or _px.get("down"):
        return None
    try:
        r = px_call("GET", path, params)
    except Exception:
        _px["down"] = True  # login failed or site unreachable: skip ProphetX for this run
        return None
    return r.json() if r.status_code < 400 else None


def american_to_p(odds):
    """+150 -> 0.40, -150 -> 0.60."""
    odds = float(odds)
    return 100 / (100 + odds) if odds > 0 else -odds / (100 - odds)


def px_ladder_price(max_price):
    """The ladder's American odds whose price is the highest that's still <= max_price."""
    if "ladder" not in _px:
        _px["ladder"] = (px_get("/v4/mm/get_price_ladder") or {}).get("data") or []
    ok = [o for o in _px["ladder"] if american_to_p(o) <= max_price + 1e-9]
    return max(ok, key=american_to_p) if ok else None


def px_events(league):
    """ProphetX games for an international league prefix (cached for this run)."""
    names = PX_LEAGUES.get(league)
    if not names and league not in TENNIS:
        return []
    if "tournaments" not in _px:
        r = px_get("/mm/get_tournaments", {"has_active_events": "true"}) or {}
        _px["tournaments"] = (r.get("data") or {}).get("tournaments") or []
    ids = [t["id"] for t in _px["tournaments"]
           if (t.get("name") or "").lower() in (names or [])
           or (league == "ufc" and "ufc" in (t.get("name") or "").lower())
           or (league in TENNIS and (t.get("name") or "").lower().startswith(league + " "))]
    out = []
    for tid in ids:
        key = f"events-{tid}"
        if key not in _px:
            r = px_get("/mm/get_sport_events", {"tournament_id": tid}) or {}
            _px[key] = (r.get("data") or {}).get("sport_events") or []
        out += _px[key]
    return out


SPELL = str.maketrans({"ø": "oe", "Ø": "Oe", "æ": "ae", "Æ": "Ae", "ß": "ss", "ı": "i", "ł": "l", "Ł": "L"})


def plain_tokens(name):
    """Lower-case words without accents or filler: 'Club Atlético de Madrid' -> ['atletico', 'madrid'],
    'Brøndby' -> ['broendby'] (the way Kalshi spells it)."""
    name = unicodedata.normalize("NFKD", name.translate(SPELL)).encode("ascii", "ignore").decode()
    return name_tokens(name)


def px_same(a, b):
    """'Bulls' ~ 'Chicago Bulls', 'Atlético Madrid' ~ 'Club Atlético de Madrid'."""
    x, y = plain_tokens(a), plain_tokens(b)
    if not x or not y:
        return False
    short, long_ = sorted((x, y), key=len)
    return all(any(w == v or (len(w) >= 3 and v.startswith(w)) for v in long_) for w in short)


def same_fighter(a, b):
    """UFC names vary by site: 'Bia Mesquita' ~ 'Beatriz Mesquita', 'Alatengheili' ~ 'Alateng Heili'.
    Matches on the full name, the name without spaces, or the last name. Only used where both
    fighters (and the date) must match, so a shared last name alone can't pick the wrong fight."""
    x, y = plain_tokens(a), plain_tokens(b)
    return bool(x and y) and (px_same(a, b) or "".join(x) == "".join(y) or x[-1] == y[-1])


ESPORTS_FILLER = {"esports", "esport", "gaming", "team", "gg", "club", "academy"}


def same_esports_team(a, b):
    """'Bilibili Gaming' ~ 'Bilibili', 'Gen.G' ~ 'Gen.G Esports', 'BetBoom Team' ~ 'BetBoom'.
    Ignores filler words, then needs the same words (or the same letters without spaces)."""
    x = [w for w in plain_tokens(a) if w not in ESPORTS_FILLER]
    y = [w for w in plain_tokens(b) if w not in ESPORTS_FILLER]
    return bool(x and y) and (x == y or "".join(x) == "".join(y))


def px_team(competitors, name, same=None):
    """The one competitor matching `name`, else None."""
    same = same or px_same
    hits = [c for c in competitors if same(name, c.get("name") or "")
            or same(name, c.get("display_name") or "")]
    return hits[0] if len(hits) == 1 else None


def px_levels(market_list, strike_id):
    """[(price 0-1, shares)] a taker can buy now on this selection, from the markets reply."""
    levels = []
    for m in market_list:
        for mm in [m] + (m.get("market_strikes") or []):
            for group in mm.get("selections") or []:
                for s in group:
                    if s.get("strike_id") != strike_id or not s.get("price"):
                        continue
                    odds, qty = float(s["price"]), float(s.get("quantity") or 0)
                    stake = qty * 100 / odds if odds > 0 else qty * -odds / 100  # dollars a taker can stake
                    if s.get("value") is not None:
                        stake = min(stake, float(s["value"]))
                    p = american_to_p(odds)
                    if stake > 0:
                        levels.append((p, stake / p))
    return levels


def px_markets(event_id):
    r = px_get("/v4/mm/get_markets", {"event_id": event_id}) or {}
    return (r.get("data") or {}).get("markets") or []


def px_ref(pick, d, intl_market, intl_price):
    league = d["event"].split("-")[0]
    events = px_events(league)
    start = intl_market.get("gameStartTime")
    if not events or not start:
        return None
    start = datetime.fromisoformat(start.replace("Z", "+00:00"))
    title = ((intl_market.get("events") or [{}])[0].get("title") or "").split(": ")[-1]
    teams = d.get("outcomes") if d["kind"] in ("winner", "spread") else d.get("names")
    teams = teams or [t.strip() for t in re.split(r" vs\.? ", title)]
    if len(teams) != 2:
        return None
    games = []
    for e in events:
        try:
            when = datetime.fromisoformat(e["scheduled"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        comps = e.get("competitors") or []
        # UFC: Polymarket's time is the card's start; a main event can be hours later
        slack = 12 if league == "ufc" else 3
        same = same_fighter if league == "ufc" else None
        if abs((when - start).total_seconds()) <= slack * 3600 and all(px_team(comps, t, same) for t in teams):
            games.append(e)
    if len(games) != 1:
        return None  # not listed, or ambiguous: don't guess
    game = games[0]
    markets = [m for m in px_markets(game["event_id"]) if m.get("status") == "active"]
    sels = lambda m: [s for mm in [m] + (m.get("market_strikes") or [])
                      for group in mm.get("selections") or [] for s in group]
    full_game = lambda m: (m.get("category_name") or "Game Lines") == "Game Lines"
    sel = None
    if d["kind"] == "winner":
        team = px_team(game["competitors"], pick["Pick"], same_fighter if league == "ufc" else None)
        m = next((m for m in markets if m.get("type") == "moneyline"
                  and (m.get("name") or "").lower() == "moneyline"), None)
        if not m and league == "ufc":  # fight-winner market may have another name: use it if it's the only one
            ml = [m for m in markets if m.get("type") == "moneyline"]
            m = ml[0] if len(ml) == 1 else None
        sel = team and m and next((s for s in sels(m) if s.get("competitor_id") == team["id"]), None)
    elif d["kind"] == "1x2":
        if d["team"] == "draw":
            m = next((m for m in markets if (m.get("name") or "") == "Draw (90 Min)"), None)
        else:
            m = next((m for m in markets if (m.get("name") or "").endswith(" to Win (90 Min)")
                      and px_same(d["team_name"], m["name"][:-len(" to Win (90 Min)")])), None)
        want = "YES" if pick["Pick"] == "Yes" else "NO"
        sel = m and next((s for s in sels(m) if (s.get("name") or "").upper() == want), None)
    elif d["kind"] == "total":
        m = next((m for m in markets if m.get("type") == "total" and full_game(m)), None)
        sel = m and next((s for s in sels(m) if s.get("strike") == d["line"]
                          and (s.get("name") or "").lower().startswith(str(pick["Pick"]).lower())), None)
    else:  # spread: the favourite (listed first internationally) gives the points
        team = px_team(game["competitors"], pick["Pick"])
        line = -d["line"] if pick["Pick"] == d["outcomes"][0] else d["line"]
        m = next((m for m in markets if m.get("type") == "spread" and full_game(m)), None)
        sel = team and m and next((s for s in sels(m) if s.get("competitor_id") == team["id"]
                                   and s.get("strike") == line), None)
    if not sel:
        return None
    levels = px_levels(markets, sel["strike_id"])
    if not levels or abs(min(levels)[0] - intl_price) > SANITY:
        return None  # priced too differently: probably the wrong game or line
    return {"venue": "prophetx", "ref": sel["strike_id"], "event_id": game["event_id"], "fee_rate": PX_FEE,
            "_levels": levels}


def px_quote(ref, dollars):
    levels = ref.pop("_levels", None) or px_levels(px_markets(ref["event_id"]), ref["ref"])
    # the fee is only paid on a win: PX_FEE x (1 - p) per share, times the p chance of winning
    return fill(levels, dollars, lambda p: PX_FEE * p * (1 - p))


# ---------------- public helpers ----------------

def quotes(pick, intl_market, dollars):
    """Priced fills on every US venue that lists this exact bet, cheapest first."""
    d = describe(pick, intl_market)
    if not d:
        return []
    intl_price = float(pick.get("Price now") or 0)
    out = []
    for venue, quote_fn in (("polymarket_us", pm_us_quote), ("kalshi", kalshi_quote), ("prophetx", px_quote)):
        try:
            if venue == "polymarket_us":
                ref = pm_us_ref(pick, d)
            elif venue == "kalshi":
                ref = kalshi_ref(pick, d, intl_price)
            else:
                ref = px_ref(pick, d, intl_market, intl_price)
            got = ref and quote_fn(ref, dollars)
        except Exception as e:  # one venue's surprise reply must not stop the others
            print(f"  ({venue} lookup error: {type(e).__name__}: {e})")
            continue
        if got:
            out.append({**ref, **got})
    return sorted(out, key=lambda q: q["cost"] / q["shares"])  # lowest all-in cost per share


def top_up_quote(ref, dollars):
    fn = {"polymarket_us": pm_us_quote, "kalshi": kalshi_quote, "prophetx": px_quote}[ref["venue"]]
    return fn(dict(ref), dollars)


def result(ref):
    return (pm_us_result if ref["venue"] == "polymarket_us" else kalshi_result)(ref)
