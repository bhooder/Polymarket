"""Shared helpers for the tests. Everything is fake: no network, no keys, no real orders.

Run from the repo folder with:   python3 -m pytest tests -q
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for name in ("LIVE_TRADING", "KALSHI_KEY_ID", "KALSHI_PRIVATE_KEY", "PM_US_KEY_ID", "PM_US_SECRET_KEY",
             "PROPHETX_ACCESS_KEY", "PROPHETX_SECRET_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
    os.environ.pop(name, None)  # tests must never see real keys or a live-trading switch

import forward_test as ft  # noqa: E402
import venues  # noqa: E402


def make_pick(name, bettors=4, pct=15.0, against=0, entry=0.50, now=None, game=None, market_type="moneyline",
              side=0, starts_in_hours=5.0):
    """A scanner pick. `game` groups markets of one game; `side` 0/1 = the two outcomes of one market."""
    game = game or f"nba-{name}-2026-12-01"
    cond = f"cond-{name}"
    return {"Pick": f"{name}-side{side}", "Market": f"{name} market", "Link": f"https://polymarket.com/event/{game}",
            "Bettors": bettors, "Against": against, "Avg bankroll %": pct, "Avg entry price": entry,
            "Price now": entry if now is None else now, "Total $ in": 10000, "Against $ in": 0,
            "Conviction bettors": bettors, "Conviction against": against,
            "token_id": f"tok-{name}-{side}", "condition_id": cond, "outcome_index": side,
            "_market_type": market_type, "_starts_in_hours": starts_in_hours}


def market_for(pick):
    start = datetime.now(timezone.utc) + timedelta(hours=pick["_starts_in_hours"])
    return {"conditionId": pick["condition_id"], "closed": False, "acceptingOrders": True,
            "gameStartTime": start.isoformat(), "sportsMarketType": pick["_market_type"]}


def fake_quote(venue, price, dollars, ref="ref"):
    """What venues.quotes returns for one venue: a full fill of `dollars` at `price` plus a 7% x p(1-p) fee."""
    fee = 0.07 * price * (1 - price)
    shares = dollars / (price + fee)
    return {"venue": venue, "ref": ref, "yes": True, "fee_rate": 0.07, "shares": shares, "cost": dollars,
            "fees": shares * fee, "avg_price": price}


class World:
    """Fake markets and venue prices. prices: token_id -> [(venue, price), ...]; missing = not listed /
    book too thin. Counts top-up quote requests so tests can prove none happened."""

    def __init__(self):
        self.picks, self.prices, self.top_up_calls = [], {}, 0

    def add(self, pick, *venue_prices):
        self.picks.append(pick)
        self.prices[pick["token_id"]] = list(venue_prices) if venue_prices else [("kalshi", pick["Avg entry price"])]
        return pick

    def unlisted(self, pick):
        self.picks.append(pick)
        self.prices[pick["token_id"]] = []
        return pick

    def market_info(self, condition_ids):
        return {p["condition_id"]: market_for(p) for p in self.picks if p["condition_id"] in condition_ids}

    def quotes(self, pick, info, dollars):
        out = [fake_quote(v, p, dollars, ref=f"{v}-{pick['token_id']}") for v, p in self.prices[pick["token_id"]]]
        return sorted(out, key=lambda q: q["cost"] / q["shares"])

    def top_up_quote(self, ref, dollars):
        self.top_up_calls += 1
        return fake_quote(ref["venue"], 0.5, dollars)


@pytest.fixture
def world(monkeypatch):
    w = World()
    monkeypatch.setattr(ft, "market_info", w.market_info)
    monkeypatch.setattr(ft, "simulate_buy", lambda *a, **k: None)  # international comparison price: not needed
    monkeypatch.setattr(venues, "quotes", w.quotes)
    monkeypatch.setattr(venues, "top_up_quote", w.top_up_quote)
    return w


def new_state():
    return {"start_bankroll": 1000.0, "cash": 1000.0, "started_at": "2026-11-15T00:00:00+00:00", "bets": []}


def run_strategy(key, state, picks, on_bet=None):
    """Trade one strategy exactly the way forward_test.main() does."""
    cfg = ft.STRATEGIES[key]
    ft.trade(state, picks, cfg["tiers"], on_bet=on_bet, net=cfg.get("net", False), window=cfg.get("window"),
             min_money_share=cfg.get("min_money_share", 0), count=cfg.get("count"),
             max_price=cfg.get("max_price", 1.0), max_above=cfg.get("max_above"))
    return state
