# Polymarket Smart-Money Scanner

Project context for any Claude assistant helping with this project (Claude Code
in VS Code, or Claude in the browser). If you can't see the code files, ask
the owner to paste or upload the one you need. The owner is a beginner coder:
explain simply, give exact commands, and keep replies short and plain.

This repo is public. Keep personal details out of every uploaded file.
Private notes and Mac-only setup live in `CLAUDE.local.md` (not uploaded).

Last updated: 2026-10-28.

## Goal
Use public Polymarket data to find what top leaderboard traders hold, infer
which outcomes have "smart money" consensus, and test whether following them
would make money. Research and paper trading only. The code never places
real orders and never touches money, wallets, or private keys.

## Files (Python 3.11, needs `requests`)
- `polymarket_smart_money.py`: the scanner. Finds consensus picks.
- `forward_test.py`: paper trading. Runs the scanner every hour and places
  pretend bets by the owner's rules.
- `venues.py`: finds the same bet on Polymarket US and Kalshi, prices it from
  real order books with fees, and settles it.
- `.github/workflows/hourly.yml`: GitHub Actions runs the forward test every
  hour at :17 and commits the results back to the repo.
- `index.html`: home page linking to the two reports (GitHub Pages).
- Scanner outputs: `consensus.csv`, `traders.csv`, `picks.html` (sortable
  picks table; open in a browser).
- Forward test outputs: `paper_trades.json` (state; don't hand-edit),
  `paper_trades.csv`, `paper_report.html` (results page), `forward_test.log`.
  Each forward-test run also rewrites `picks.html` (all picks, no 4c filter).

## How to run by hand (from the VS Code terminal)
    pip3 install requests
    python3 polymarket_smart_money.py --period month    # scanner, ~1.5 min
    python3 forward_test.py                              # one forward-test run

## How the scanner works
1. Gets the top 100 traders from the Polymarket leaderboard (default: sports
   category, last month, ranked by profit).
2. Skips likely bots: traders whose last 500 trades are >50% crypto price bets.
3. Gets each trader's open positions worth $100+.
4. Drops any market where a trader holds both sides (a hedge), counting
   positions of any size ($1+ cost).
5. Keeps only positions in events tagged with the chosen category.
6. Groups by market + outcome. A "pick" needs 3+ top traders on the same side.
7. For each pick: number of bettors, average "bankroll %" (each holder's
   position value / their total open-bet value, averaged), total $ in,
   traders' average entry price, current price.
8. Sorted by bettors, then average bankroll %.
9. Skips picks whose price is now more than 4c from the traders' average
   entry (either direction).

Options: `--top N` (default 100), `--period day|week|month|all`,
`--category sports|politics|crypto|esports|culture|weather|economics|tech|finance|mentions|overall`
(default sports), `--sort PNL|VOLUME`, `--min-value` (100), `--min-traders` (3),
`--max-move` (0.04; 1 = off), `--max-crypto` (0.5; 1 = off).

## Forward test (paper trading), started 2026-10-27
A true backtest was rejected: Polymarket keeps no old leaderboards, so
testing today's top traders on their past bets is hindsight bias. Instead the
forward test records picks as they appear and tracks real results.

The owner's rules:
- Start with $1,000 of pretend money in one shared bankroll. Stakes are a
  share of the CURRENT bankroll (cash + open bets at cost).
- Stake tiers (avg bankroll % = average across the pick's bettors):
  - 2%: 3+ bettors & 35%+, or 4+ & 25%+, or 5+ & 20%+
  - 1%: 3+ bettors & 20%+, or 4+ & 15%+, or 5+ & 10%+
- One bet per pick. A 1% bet tops up once to 2% if it later meets a 2% tier.
  Never bet both sides of a market.
- Only markets decided within 3 days. Skip games that have already started.
- Price rule: the price actually paid (before fees) must be at most 2c ABOVE
  or 4c BELOW the traders' average entry. (The scanner's 4c filter is off
  in the forward test.)
- Hold until the market is decided. At most 50% of the bankroll in open bets.
- Venues: only US-regulated venues, Polymarket US and
  Kalshi. Each bet goes to whichever has the lowest all-in cost per share
  (fees included) and passes the price rule. The international Polymarket
  price is recorded for comparison only.
- Runs every hour on GitHub Actions.

Status as of 2026-10-27: 2 open bets. Perricard (tennis, placed on
international Polymarket before the switch to US venues) and Knicks (Kalshi).
Check results after a few weeks. A handful of bets proves nothing.

## Data sources (all public, no API key)
- Polymarket Data API v2: https://data-api.polymarket.com/v2
  (spec: https://data-api.polymarket.com/v2/openapi.json). Uses
  `/leaderboard`, `/positions`, `/trades`, `/value`, `/resolutions`.
  Cursor pagination only. Repeat `user` on every `/positions` page.
  `/positions` is sorted by value, largest first. `volume` is in shares, and
  `_usdc` fields are USD. 429/503 errors are retried using Retry-After.
- Polymarket Gamma API (market details, tags, dates, fees):
  https://gamma-api.polymarket.com. For games, `endDate` is a placeholder
  about a week out, so use `gameStartTime`.
- Polymarket order book: https://clob.polymarket.com/book?token_id=...
  International fee = shares x rate x (p(1-p))^exponent; sports rate is
  mostly 0.03.
- Polymarket US (public gateway): https://gateway.polymarket.us/v1. Labels
  are the international slug plus a prefix: `aec-` winner, `atc-` soccer
  win/draw, `tsc-` total, `asc-...-neg|pos-<line>` spread. Fee =
  feeCoefficient (~0.07) x C x p x (1-p). It doesn't list every game (no
  tennis or Premier League seen).
- Kalshi: https://api.elections.kalshi.com/trade-api/v2. Tickers look like
  KXNBAGAME-26OCT27PORLAL-POR. Soccer team codes differ from Polymarket's, so
  soccer is matched by team name. Tennis tickers carry the tournament date.
  Fee = roundup(0.07 x C x P x (1-P)). A Kalshi match priced >15c away from
  Polymarket is discarded as a probable mismatch.

## Things learned / caveats
- US-venue fees are roughly double the international ones: about
  0.07 x (1 - price) of each bet (~2.7% at 61c, ~5.7% at 18c).
- Many top accounts are bots: crypto "up or down" bots, and sports market
  makers holding both sides. Ranking by VOLUME surfaces mostly bots, so PNL
  is better.
- Kalshi was ruled out as a DATA source (not on-chain, opt-in leaderboard),
  but it is used as a betting venue.
- Leaderboards reward luck. Top traders hedge, and prices move after they buy.
- Output is a research signal, not financial advice.

## Ideas / next steps
1. Let the forward test run for a few weeks, then review won-lost, return
   per dollar bet, and how much choosing the cheaper venue saved.
2. Multi-window consistency (traders who rank in day, week AND month) is not
   built yet. `--period` takes one window.
3. Only after a convincing forward test: consider small real-money trades
   with strict caps and manual approval. Check each platform's terms first.
4. Done 2026-10-28: project moved to GitHub, hourly runs on GitHub Actions.
