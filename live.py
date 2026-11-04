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

Privacy: the repo and its GitHub Actions logs are public, so nothing about real bets is
printed or saved. Details go only to the owner's private ntfy channel (NTFY_TOPIC).
Keys come from GitHub Secrets: KALSHI_KEY_ID, KALSHI_PRIVATE_KEY, PM_US_KEY_ID,
PM_US_SECRET_KEY.
"""
import base64
import math
import os
import time
import uuid
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

import venues

PM_US_API = "https://api.polymarket.us"
MAX_EXPOSURE = 0.50  # same cap as the paper test: at most half the bankroll in open bets


def mode():
    m = (os.environ.get("LIVE_TRADING") or "off").strip().lower()
    return m if m in ("practice", "on") else "off"


def alert(title, text):
    """Push a private notification to the owner's phone (ntfy). Never print bet details."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=text.encode(), headers={"Title": title}, timeout=15)
    except requests.RequestException:
        pass


def amount(x):
    """Polymarket US money fields are {"value": "1.23", ...} or plain numbers."""
    if isinstance(x, dict):
        x = x.get("value")
    return float(x or 0)


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
    r.raise_for_status()
    return r.json() if r.content else {}


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
    r.raise_for_status()
    return r.json() if r.content else {}


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
        order = pm_us_request("POST", "/v1/order/preview", body={"request": body}).get("order") or {}
    else:
        body.update(synchronousExecution=True, maxBlockTime="5")
        r = pm_us_request("POST", "/v1/orders", body=body)
        order = ((r.get("executions") or [{}])[-1]).get("order") or {}
    filled = float(order.get("cumQuantity") or 0)
    px = amount(order.get("avgPx"))
    if px and not ref["long"]:
        px = 1 - px
    return filled, px, amount(order.get("commissionNotionalTotalCollected"))


# ---------------- mirroring Strategy A ----------------

VENUE_LABEL = {"kalshi": "Kalshi", "polymarket_us": "Polymarket US"}
KEYS = {"kalshi": ("KALSHI_KEY_ID", "KALSHI_PRIVATE_KEY"), "polymarket_us": ("PM_US_KEY_ID", "PM_US_SECRET_KEY")}
ACCOUNT = {"kalshi": kalshi_account, "polymarket_us": pm_us_account}


def run(intents, max_above):
    """intents: Strategy A paper bets placed this run, as dicts with bet, fill (venue quote),
    share (of the paper bankroll), top_up and entry (traders' average entry price).
    max_above: the price rule (pay at most this much over the traders' average entry)."""
    m = mode()
    if m == "off" or not intents:
        return
    accounts = {}
    for venue, names in KEYS.items():
        if all(os.environ.get(n) for n in names):
            try:
                accounts[venue] = ACCOUNT[venue]()
            except Exception as e:  # bad key, outage: skip this venue, say so privately
                alert("Live betting: account check failed", f"{VENUE_LABEL[venue]}: {type(e).__name__}: {e}")
    if not accounts:
        alert("Live betting: no accounts", "No working Kalshi or Polymarket US keys, so no live bets.")
        return
    bankroll = sum(cash + sum(cost for _, cost in held.values()) for cash, held in accounts.values())
    exposure = sum(sum(cost for _, cost in held.values()) for _, held in accounts.values())
    tag = "PRACTICE (not placed)" if m == "practice" else "LIVE"

    for it in intents:
        bet, ref = it["bet"], it["fill"]
        venue, label = ref["venue"], f'{bet["pick"]} / {bet["market"]}'
        if venue not in accounts:
            alert(f"{tag}: skipped", f"{label}\nNo working {VENUE_LABEL.get(venue, venue)} account.")
            continue
        cash, held = accounts[venue]
        holding = held.get(ref["ref"])
        if it["top_up"] and not holding:
            alert(f"{tag}: top-up skipped", f"{label}\nNo live position to top up.")
            continue
        if not it["top_up"] and holding:
            continue  # already bet this one live (e.g. a repeat run); never double up
        dollars = round(it["share"] * bankroll, 2)
        if exposure + dollars > MAX_EXPOSURE * bankroll or dollars > cash:
            alert(f"{tag}: skipped", f"{label}\nWould need ${dollars:.2f}: over the 50% cap or more "
                                     f"than the {VENUE_LABEL[venue]} cash (${cash:.2f}).")
            continue
        max_price = it["entry"] + max_above  # the traders' average entry when this bet was made
        rate = ref.get("fee_rate") or 0
        per_share = max_price + rate * max_price * (1 - max_price) + 0.005  # worst case, fee rounding
        count = math.floor(dollars / per_share)  # whole contracts, sized for the worst allowed price
        if count < 1:
            alert(f"{tag}: skipped", f"{label}\n${dollars:.2f} is less than one contract at ${max_price:.2f}.")
            continue
        side = "YES" if ref.get("yes" if venue == "kalshi" else "long") else "NO"
        plan = (f"{label}\n{'Top-up' if it['top_up'] else 'Bet'}: buy {count} {side} on {VENUE_LABEL[venue]} "
                f"at up to ${max_price:.2f} (${dollars:.2f} = {it['share']:.2%} of ${bankroll:,.2f}).\n"
                f"Paper paid {bet['avg_price']:.3f}.")
        try:
            if m == "practice":
                if venue == "polymarket_us":
                    filled, px, fees = pm_us_order(ref, count, max_price, preview=True)
                    plan += f"\nPreview: would fill {filled:g} at {px:.3f}, fees ${fees:.2f}."
                alert(f"{tag}: bet", plan)
                continue
            order_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'{bet["token_id"]}-{bet["placed_at"]}-{it["top_up"]}'))
            if venue == "kalshi":
                filled, px, fees = kalshi_order(ref, count, max_price, order_id)
            else:
                filled, px, fees = pm_us_order(ref, count, max_price)
        except Exception as e:
            alert(f"{tag}: order failed", f"{plan}\n{type(e).__name__}: {e}")
            continue
        if filled:
            spent = filled * px + fees
            exposure += spent
            accounts[venue] = (cash - spent, held)
            alert(f"{tag}: bet placed", f"{plan}\nFilled {filled:g} of {count} at {px:.3f}, fees ${fees:.2f}.")
        else:
            alert(f"{tag}: not filled", f"{plan}\nNothing filled at ${max_price:.2f} or better (price moved).")
