#!/usr/bin/env python3
"""
Real-money betting for Strategy A on Kalshi and Polymarket US.

Mirrors Strategy A's paper bets: whenever the paper test places (or tops up) a bet in a run,
the same bet goes to the same venue for real, sized as the same share of the REAL bankroll
(cash + open positions on both accounts) and limited by the same price rule.

The LIVE_TRADING setting picks the mode:
  off       (default) do nothing
  practice  everything except sending orders: reads balances and positions, previews
            Polymarket US orders, and sends an alert describing each bet it would place
  on        places real "fill now or cancel" orders

Real bets that fill are recorded in live_trades.json (public, like the paper results) and
shown on the report's "Real bets" tab. Each bet also sends a phone alert (Telegram).
Keys come from GitHub Secrets: KALSHI_KEY_ID, KALSHI_PRIVATE_KEY, PM_US_KEY_ID,
PM_US_SECRET_KEY.
"""
import base64
import json
import math
import os
import re
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

import venues

PM_US_API = "https://api.polymarket.us"
MAX_EXPOSURE = 0.50  # same cap as the paper test: at most half the bankroll in open bets


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def mode():
    m = (os.environ.get("LIVE_TRADING") or "off").strip().lower()
    return m if m in ("practice", "on") else "off"


def alert(title, text):
    """Send a phone alert to the Telegram chat (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID).
    A failed alert never stops betting."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    try:
        if token and chat:  # a chat id can be a private chat or a group the bot was added to
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": f"{title}\n{text}"[:4000]}, timeout=15)
    except Exception:  # any alert problem is ignored: alerts must never stop betting
        pass


def amount(x):
    """Polymarket US money fields are {"value": "1.23", ...} or plain numbers."""
    if isinstance(x, dict):
        x = x.get("value")
    return float(x or 0)


class VenueError(Exception):
    pass


LAST_REPLY = {}  # the latest raw reply from a venue, kept for the technical log


def checked(r):
    """The JSON reply, or an error that includes the venue's own explanation (e.g. a missing permission)."""
    LAST_REPLY.clear()
    LAST_REPLY.update(path=urlparse(r.url).path, status=r.status_code, body=r.text[:1500])
    if r.status_code >= 400:
        raise VenueError(f"HTTP {r.status_code} on {urlparse(r.url).path}: {r.text[:300]}")
    return r.json() if r.content else {}


# ---------------- Kalshi ----------------

def kalshi_request(method, path, params=None, body=None):
    key = serialization.load_pem_private_key(os.environ["KALSHI_PRIVATE_KEY"].encode(), password=None)
    ts = str(int(time.time() * 1000))
    message = (ts + method + urlparse(venues.KALSHI + path).path).encode()  # no query string
    if isinstance(key, rsa.RSAPrivateKey):
        sig = key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
    else:
        sig = key.sign(message)  # Ed25519
    headers = {"KALSHI-ACCESS-KEY": os.environ["KALSHI_KEY_ID"], "KALSHI-ACCESS-TIMESTAMP": ts,
               "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}
    r = requests.request(method, venues.KALSHI + path, params=params, json=body, headers=headers, timeout=30)
    return checked(r)


def kalshi_account():
    """(cash, {ticker: (contracts, cost)}); contracts are negative for NO."""
    cash = kalshi_request("GET", "/portfolio/balance")["balance"] / 100  # cents
    held, cursor = {}, None
    for _ in range(20):
        params = {"settlement_status": "unsettled", "limit": 1000, **({"cursor": cursor} if cursor else {})}
        r = kalshi_request("GET", "/portfolio/positions", params)
        for p in r.get("market_positions") or []:
            n = float(p.get("position_fp") or 0)
            if n:
                held[p["ticker"]] = (n, float(p.get("market_exposure_dollars") or 0))
        cursor = r.get("cursor")
        if not cursor:
            break
    return cash, held


def kalshi_order(ref, count, max_price, order_id):
    """Buy `count` YES (or NO) at most max_price each, filling now or cancelling the rest.
    Kalshi orders are on the YES side: buying NO at q = selling YES at 1 - q."""
    if ref["yes"]:
        side, price = "bid", math.floor(max_price * 100 + 1e-9) / 100
    else:
        side, price = "ask", math.ceil((1 - max_price) * 100 - 1e-9) / 100
    r = kalshi_request("POST", "/portfolio/events/orders", body={
        "ticker": ref["ref"], "side": side, "count": f"{count:.2f}", "price": f"{price:.4f}",
        "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": order_id})  # Kalshi rejects a repeat of the same id, so no double bets
    filled = float(r.get("fill_count") or 0)
    yes_px = float(r.get("average_fill_price") or 0)
    px = yes_px if ref["yes"] else 1 - yes_px
    return filled, px, filled * float(r.get("average_fee_paid") or 0)


# ---------------- Polymarket US ----------------

def pm_us_request(method, path, params=None, body=None):
    secret = base64.b64decode(os.environ["PM_US_SECRET_KEY"])[:32]
    key = ed25519.Ed25519PrivateKey.from_private_bytes(secret)
    ts = str(int(time.time() * 1000))
    sig = key.sign((ts + method + path).encode())
    headers = {"X-PM-Access-Key": os.environ["PM_US_KEY_ID"], "X-PM-Timestamp": ts,
               "X-PM-Signature": base64.b64encode(sig).decode()}
    r = requests.request(method, PM_US_API + path, params=params, json=body, headers=headers, timeout=30)
    return checked(r)


def pm_us_account():
    """(spendable cash, {market slug: (net contracts, cost)})."""
    balances = pm_us_request("GET", "/v1/account/balances").get("balances") or []
    cash = sum(amount(b.get("buyingPower")) for b in balances if (b.get("currency") or "USD") == "USD")
    held, cursor = {}, None
    for _ in range(20):
        r = pm_us_request("GET", "/v1/portfolio/positions", {"limit": 100, **({"cursor": cursor} if cursor else {})})
        for slug, p in (r.get("positions") or {}).items():
            n = float(p.get("netPositionDecimal") or 0)
            if n and not p.get("expired"):
                held[slug] = (n, amount(p.get("cost")))
        cursor = r.get("nextCursor")
        if r.get("eof") or not cursor:
            break
    return cash, held


def pm_us_order_body(ref, count, max_price):
    # Prices always refer to the YES (long) side: buying NO at q is written as YES at 1 - q.
    price = (math.floor(max_price * 100 + 1e-9) / 100 if ref["long"]
             else math.ceil((1 - max_price) * 100 - 1e-9) / 100)
    return {"marketSlug": ref["ref"], "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{price:.2f}", "currency": "USD"}, "quantity": count,
            "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
            "intent": "ORDER_INTENT_BUY_LONG" if ref["long"] else "ORDER_INTENT_BUY_SHORT",
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC"}


def pm_us_order(ref, count, max_price, preview=False):
    """Returns (contracts filled, average price paid, fees). With preview, nothing is placed."""
    body = pm_us_order_body(ref, count, max_price)
    if preview:
        reply = pm_us_request("POST", "/v1/order/preview", body={"request": body})
        order = reply.get("order") or {}
    else:
        body.update(synchronousExecution=True, maxBlockTime="5")
        r = pm_us_request("POST", "/v1/orders", body=body)
        order = ((r.get("executions") or [{}])[-1]).get("order") or {}
    filled = float(order.get("cumQuantity") or 0)
    px = amount(order.get("avgPx"))
    if px and not ref["long"]:
        px = 1 - px
    fees = amount(order.get("commissionNotionalTotalCollected"))
    if preview:  # practice mode: the raw reply shows how Polymarket US read the order
        return filled, px, fees, json.dumps(reply)[:600]
    return filled, px, fees


# ---------------- mirroring Strategy A ----------------

VENUE_LABEL = {"kalshi": "Kalshi", "polymarket_us": "Polymarket US"}
KEYS = {"kalshi": ("KALSHI_KEY_ID", "KALSHI_PRIVATE_KEY"), "polymarket_us": ("PM_US_KEY_ID", "PM_US_SECRET_KEY")}
ACCOUNT = {"kalshi": kalshi_account, "polymarket_us": pm_us_account}


def record(state, it, ref, filled, px, fees, count):
    """Add a filled real order to the live state (a top-up merges into its bet)."""
    bet, cost = it["bet"], round(filled * px + fees, 2)
    if it["top_up"]:
        rec = next((b for b in state["bets"] if b["token_id"] == bet["token_id"] and b["status"] == "open"), None)
        if rec:
            rec["shares"] += filled
            rec["cost"] = round(rec["cost"] + cost, 2)
            rec["fees"] = round(rec["fees"] + fees, 4)
            rec["avg_price"] = round((rec["cost"] - rec["fees"]) / rec["shares"], 4)
            rec.update(stake=0.02, topped_up_at=now_iso(),
                       top_up_size_multiplier=bet.get("top_up_size_multiplier"))
            return
    state["bets"].append({
        "placed_at": now_iso(), "status": "open",
        "stake": bet["stake"], "size_multiplier": bet.get("size_multiplier"),
        **{k: bet.get(k) for k in ("pick", "market", "link", "market_type", "condition_id", "token_id",
                                   "outcome_index", "ends", "bettors", "avg_bankroll_pct")},
        "traders_entry": it["entry"], "avg_price": round(px, 4), "shares": filled, "cost": cost,
        "fees": round(fees, 4), "contracts_ordered": count, "venue": ref["venue"],
        "venue_ref": {k: ref[k] for k in ("venue", "ref", "long", "yes", "fee_rate") if k in ref},
        "paper_price": ref.get("avg_price"),  # what the paper test paid for the same bet, same moment
        "payout": None, "profit": None, "settled_at": None, "topped_up_at": None,
    })


# ---------------- messages ----------------

LOG = []  # technical details for live_log.json (forward_test.py saves it); Telegram gets short messages


def log(event, **data):
    LOG.append({"at": now_iso(), "event": event, **data})


def plain(pick, market):
    """'Arsenal to win (vs. Slavia Praha)', 'Under 48.5 (Panthers vs. Packers)', 'Mavericks +5.5'."""
    pick, market = str(pick), str(market)
    if market.startswith("Spread: ") and "(" in market:  # "Spread: Thunder (-5.5)"
        team, line = market[8:].rsplit(" (", 1)
        line = line.rstrip(")")
        if pick != team:  # the other team gets the opposite line: -5.5 <-> +5.5
            line = ("+" + line[1:]) if line.startswith("-") else ("-" + line.lstrip("+"))
        return f"{pick} {line}"
    if ": O/U " in market:  # "Panthers vs. Packers: O/U 48.5"
        game, line = market.rsplit(": O/U ", 1)
        return f"{pick} {line} ({game})"
    won = re.match(r"Will (.+) win on \d{4}-\d{2}-\d{2}\?$", market)
    if won:  # soccer win markets: "Will Arsenal FC win on 2026-11-04?"
        team = won.group(1)
        return f"{team} to win" if pick == "Yes" else f"{team} not to win (draw or loss)"
    game = market.split(": ", 1)[-1]  # drop a "Tournament: " prefix
    teams = [t.strip() for t in re.split(r" vs\.? ", game)]
    if len(teams) == 2 and pick in teams:
        return f"{pick} to win (vs. {teams[1 - teams.index(pick)]})"
    return f"{pick}: {market}"


def cents(p):
    return f"{p * 100:.0f}¢"


def pct(share):
    """Stake as a % of the bankroll: 0.01 -> 1%, 0.0061 -> 0.61%."""
    return f"{share * 100:.2f}".rstrip("0").rstrip(".") + "%"


def american(p):
    """Price (0-1) as American odds: 50c -> +100, 60c -> -150, 40c -> +150."""
    if not 0 < p < 1:
        return ""
    return f"-{p / (1 - p) * 100:.0f}" if p > 0.5 else f"+{(1 - p) / p * 100:.0f}"


def run(intents, max_above, state):
    """intents: Strategy A paper bets placed this run, as dicts with bet, fill (venue quote),
    share (of the paper bankroll), top_up and entry (traders' average entry price).
    max_above: the price rule (pay at most this much over the traders' average entry).
    state: live_trades.json contents; filled orders are added to it (only when mode is "on").
    Every run in practice/on checks both accounts; once a day it sends their status."""
    m = mode()
    if m == "off":
        return
    accounts, status, problems = {}, [], []
    for venue, names in KEYS.items():
        if not all(os.environ.get(n) for n in names):
            status.append(f"{VENUE_LABEL[venue]} no keys")
            continue
        try:
            accounts[venue] = ACCOUNT[venue]()
            status.append(f"{VENUE_LABEL[venue]} ${accounts[venue][0]:,.2f} ✓")
        except Exception as e:  # bad key, outage: skip this venue
            status.append(f"{VENUE_LABEL[venue]} ✗")
            problems.append(venue)
            log("account check failed", venue=venue, error=f"{type(e).__name__}: {e}", reply=dict(LAST_REPLY))
    today = datetime.now(timezone.utc).date().isoformat()
    manual = os.environ.get("RUN_EVENT") == "workflow_dispatch"  # a run started with "Run workflow"
    open_count = sum(b["status"] == "open" for b in state["bets"])
    if state.get("daily_check") != today or manual:  # once a day (and on manual runs): keys work? balances
        state["daily_check"] = today
        alert(f"📊 Daily check{' (practice mode)' if m == 'practice' else ''}",
              " · ".join(status) + f" · {open_count} open bets")
    elif intents and problems:
        alert("⚠️ Problem", f"Couldn't reach {', '.join(VENUE_LABEL[v] for v in problems)}. Details in the log.")
    if not accounts:
        return
    bankroll = sum(cash + sum(cost for _, cost in held.values()) for cash, held in accounts.values())
    state["cash"] = round(sum(cash for cash, _ in accounts.values()), 2)  # shown on the Real bets tab
    state["balances_checked_at"] = now_iso()
    exposure = sum(sum(cost for _, cost in held.values()) for _, held in accounts.values())

    for it in intents:
        bet, ref = it["bet"], it["fill"]
        venue = ref["venue"]
        what = plain(bet["pick"], bet["market"])
        where = VENUE_LABEL.get(venue, venue)
        info = {"bet": what, "pick": bet["pick"], "market": bet["market"], "venue": venue, "ref": ref.get("ref"),
                "top_up": it["top_up"], "mode": m, "paper_price": bet["avg_price"], "share": it["share"]}

        def skip(reason, tell=True):
            log("skipped", reason=reason, **info)
            if tell:
                alert(f"⏭️ Skipped{' top-up' if it['top_up'] else ''}", f"{what}\n{reason}")

        if venue not in accounts:
            skip(f"{where} account isn't working.")
            continue
        cash, held = accounts[venue]
        holding = held.get(ref["ref"])
        if it["top_up"] and not holding:
            skip("The first bet never filled for real, so there's nothing to top up.")
            continue
        if not it["top_up"] and holding:
            skip("Already holding this bet.", tell=False)  # e.g. a repeat run; never double up
            continue
        dollars = round(it["share"] * bankroll, 2)
        if exposure + dollars > MAX_EXPOSURE * bankroll or dollars > cash:
            skip(f"Would need ${dollars:.2f}: over the 50% cap or more than the ${cash:.2f} cash on {where}.")
            continue
        max_price = it["entry"] + max_above  # the traders' average entry when this bet was made
        rate = ref.get("fee_rate") or 0
        per_share = max_price + rate * max_price * (1 - max_price) + 0.005  # worst case, fee rounding
        count = math.floor(dollars / per_share)  # whole contracts, sized for the worst allowed price
        if count < 1:
            skip(f"${dollars:.2f} isn't enough for one contract at {cents(max_price)}.")
            continue
        side = "YES" if ref.get("yes" if venue == "kalshi" else "long") else "NO"
        info.update(side=side, contracts=count, max_price=round(max_price, 4), dollars=dollars,
                    bankroll=round(bankroll, 2))
        kind = "Top-up" if it["top_up"] else "Bet"
        try:
            if m == "practice":
                if venue == "polymarket_us":
                    pm_us_order(ref, count, max_price, preview=True)
                    info.update(sent=pm_us_order_body(ref, count, max_price), reply=dict(LAST_REPLY))
                log("practice", **info)
                alert(f"🧪 Practice: would {kind.lower()}", f"{what}\nUp to ${count * per_share:.2f} on {where} · "
                                                           f"{count} contracts at up to {cents(max_price)}")
                continue
            order_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'{bet["token_id"]}-{bet["placed_at"]}-{it["top_up"]}'))
            if venue == "kalshi":
                filled, px, fees = kalshi_order(ref, count, max_price, order_id)
            else:
                filled, px, fees = pm_us_order(ref, count, max_price)
            info.update(order_id=order_id, reply=dict(LAST_REPLY))
        except Exception as e:
            log("order failed", error=f"{type(e).__name__}: {e}", reply=dict(LAST_REPLY), **info)
            alert("⚠️ Problem", f"{what}\n{where} order failed. Details in the log.")
            continue
        state.setdefault("orders", []).append({"at": now_iso(), "pick": bet["pick"],
                                               "venue": venue, "ordered": count, "filled": filled})
        log("order", filled=filled, price=px, fees=fees, **info)
        if filled:
            record(state, it, ref, filled, px, fees, count)
            spent = filled * px + fees
            exposure += spent
            accounts[venue] = (cash - spent, held)
            alert(f"🟢 {kind} placed ({pct(it['share'])} @ {american(px)})", f"{what}\n${spent:.2f} on {where} · {filled:g} contracts at {cents(px)}")
        else:
            alert("⚪ Not filled", f"{what}\nThe price moved above {cents(max_price)}. Nothing bought.")
