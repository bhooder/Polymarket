"""One fixed set of fake picks, used to check that the existing strategies still decide exactly
as they did before Strategy ChatGPT was added (tests/test_existing_unchanged.py)."""
from conftest import make_pick

GAME1 = "nba-aaa-bbb-2026-12-01"


def build(w):
    w.add(make_pick("p01", bettors=5, pct=22, entry=0.50, game=GAME1), ("kalshi", 0.50))
    w.add(make_pick("p02", bettors=4, pct=16, entry=0.55), ("kalshi", 0.56))            # 1c above entry
    w.add(make_pick("p03", bettors=3, pct=12, entry=0.45), ("polymarket_us", 0.44))
    w.add(make_pick("p04", bettors=5, pct=8, entry=0.50), ("kalshi", 0.50))             # low conviction
    w.add(make_pick("p05", bettors=4, pct=30, against=4, entry=0.50), ("kalshi", 0.50))  # tie
    w.add(make_pick("p06", bettors=6, pct=12, against=2, entry=0.30), ("kalshi", 0.30))  # underdog
    w.add(make_pick("p07", bettors=4, pct=40, entry=0.80), ("kalshi", 0.80))            # over 75c
    w.add(make_pick("p08", bettors=3, pct=36, entry=0.60), ("kalshi", 0.62), ("prophetx", 0.60))
    w.add(make_pick("p09", bettors=5, pct=25, entry=0.50, game=GAME1, market_type="spreads"), ("kalshi", 0.50))
    w.add(make_pick("p10", bettors=5, pct=25, entry=0.50, game=GAME1, market_type="totals"), ("kalshi", 0.50))
    w.add(make_pick("p11", bettors=5, pct=15, against=1, entry=0.52, starts_in_hours=1), ("kalshi", 0.50))
    w.unlisted(make_pick("p12", bettors=5, pct=30, entry=0.50))                         # no venue
    w.add(make_pick("p13", bettors=5, pct=30, entry=0.27), ("kalshi", 0.27))            # under the floor
    w.add(make_pick("p14", bettors=5, pct=30, entry=0.50, starts_in_hours=100), ("kalshi", 0.50))  # too far off
    w.add(make_pick("p15", bettors=4, pct=18, against=1, entry=0.60), ("kalshi", 0.605), ("polymarket_us", 0.61))
    return w.picks


def decisions(state):
    return sorted((b["token_id"], b["stake"], b["venue"], round(b["cost"], 2), round(b["avg_price"], 4))
                  for b in state["bets"])
