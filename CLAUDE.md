# Polymarket Smart-Money Scanner

Project context for any Claude assistant helping with this project (Claude Code
in VS Code, or Claude in the browser). If you can't see the code files, ask
the owner to paste or upload the one you need. The owner is a beginner coder:
explain simply, give exact commands, and keep replies short and plain.

This repo is public. Keep personal details out of every uploaded file.
Private notes and Mac-only setup live in `CLAUDE.local.md` (not uploaded).
**Also read `FINDINGS.md`**: what the data has shown so far and why each rule exists.

Last updated: 2026-11-13.

## Goal
Use public Polymarket data to find what top leaderboard traders hold, infer
which outcomes have "smart money" consensus, and test whether following them
would make money. Paper trading, plus (from 2026-11-03) optional real-money
betting that mirrors Strategy A on Kalshi, Polymarket US and ProphetX (`live.py`).

## Files (Python 3.11, needs `requests`)
- `polymarket_smart_money.py`: the scanner. Finds consensus picks.
- `forward_test.py`: paper trading. Runs the scanner every hour and places
  pretend bets by the owner's rules.
- `venues.py`: finds the same bet on Polymarket US, Kalshi and ProphetX, prices
  it from real order books with fees, and settles it.
- `live.py`: real-money betting. Copies each new Strategy A paper bet to the
  same venue. From 2026-11-10 every real bet is a FLAT 1.5% of the real bankroll
  (cash + open positions on all accounts), times the underdog multiplier, and
  real bets are never topped up (live.FLAT_SHARE; before that they copied A's
  1%/2% stakes and top-ups). Same price rule, "fill now or
  cancel" limit orders, 50% exposure cap, never doubles a position it already
  holds. Mode from the LIVE_TRADING repository variable: off (default),
  practice (no orders; Polymarket US orders previewed), on. Keys in GitHub
  Secrets: KALSHI_KEY_ID, KALSHI_PRIVATE_KEY, PM_US_KEY_ID, PM_US_SECRET_KEY,
  PROPHETX_ACCESS_KEY, PROPHETX_SECRET_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (alerts go to a Telegram group; ntfy was
  tried first and removed 2026-11-04 because its pushes didn't reach the iPhone). Filled real bets are saved to `live_trades.json` / `.csv` (public,
  owner's choice) and shown on the report's first tab, "Real bets"; the
  Statistics tab adds Real next to A and B plus a "Real vs paper" table (fill
  rate, price paid vs paper, return on the same bets). Each bet also sends a
  short, plain-English phone alert (Telegram); the technical details (order
  sent, venue reply, skips with reasons, errors) go to `live_log.json` (last
  2,000 entries). Needs the `cryptography` package.
- `.github/workflows/hourly.yml`: GitHub Actions runs the forward test, asked
  for every 10 minutes (:03, :13, ...; every 15 until 2026-11-04). GitHub throttles
  free schedules: in practice runs came about hourly overnight. Commits results back.
- `claude_strategy.py`: Strategy Claude (trader report card by closing-price value).
- `pick_log.py` (added 2026-11-13, research only, not on the report): logs EVERY game pick,
  bet or not. `pick_log_open.json` = undecided picks (first and last look before the start:
  bettors, against, conviction, bankroll %, $ in, entry, price). `pick_log.csv` = finished
  picks, one row each, append-only: + closing price, result (1/0/0.5) and which strategies
  bet it. Use it to test new strategy ideas on all picks. Rows first seen before 2026-11-13
  were backfilled from the picks.html history: no conviction or against-$ columns, and no
  Against count before ~2026-10-31.
- `index.html`: home page linking to the two reports (GitHub Pages).
- Scanner outputs: `consensus.csv`, `traders.csv`, `picks.html` (sortable
  picks table; open in a browser).
- Forward test outputs: `paper_trades.json` and `paper_trades_b.json` (state;
  don't hand-edit), matching CSVs, `paper_report.html` (results page), `forward_test.log`.
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
7. For each pick: number of bettors, number Against (other side), average "bankroll %" (each holder's
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
  Never bet the minority side or a tie: each pick carries an "Against" count
  (top traders on the other side of the same market, $100+ positions), and a
  pick is skipped if Against >= Bettors (added 2026-11-04, both strategies).
  Per game, at most one moneyline-or-spread bet and at most one total bet
  (added 2026-10-29). If two compete in one run, the higher pick wins.
- Only markets decided within 3 days. Skip games that have already started.
- Price rule: the price actually paid (before fees) must be at most 2c ABOVE
  (1c from 2026-11-10 to 2026-11-13) or 4c BELOW the traders' average entry, and at least 28.6c (+250)
  (price floor added 2026-11-10 at 35c, lowered to +250 on 2026-11-11; all strategies and real bets). Strategy A and real
  bets also never pay more than 75c (A_MAX_PRICE, added 2026-11-11; others uncapped). (The scanner's 4c filter is off
  in the forward test.)
- Underdog sizing (added 2026-10-30, both strategies, top-ups too): picks
  priced under 40c get stake x sqrt((p/(1-p)) / (0.4/0.6)), so each bet has about
  the same luck risk as a 40c bet: 30c x0.80, 20c x0.61, 10c x0.41. No price
  floor until 2026-11-10, then 35c, now 28.6c (+250). Uses the current Polymarket price. Older bets are unchanged.
- Hold until the market is decided. At most 50% of the bankroll in open bets.
- Venues: only US-regulated venues: Polymarket US, Kalshi and (from
  2026-11-08) ProphetX. Each bet goes to whichever has the lowest all-in cost per share
  (fees included) and passes the price rule. The international Polymarket
  price is recorded for comparison only.
- Runs on GitHub Actions, asked for every 10 minutes (actual timing varies).

Strategy B (added 2026-10-29): a second, separate $1,000 paper bankroll with
the same rules except staking by bettor count only: 1% at 4 bettors, 2% at 5+,
avg bankroll % ignored. State in paper_trades_b.json / paper_trades_b.csv.
Strategy settings live in STRATEGIES in forward_test.py.

Strategy C (added 2026-11-04): a third separate $1,000 paper bankroll, same
rules but staked by NET bettors (Bettors minus Against): 1% at net 4, 2% at net
5+, avg bankroll % ignored. State in paper_trades_c.json / .csv.

Strategy D (added 2026-11-10): a fifth $1,000 paper bankroll for late money. Bets only
90 to 15 minutes before the start (GitHub runs come ~40 min apart, so a narrower window
would often be missed). Staked by NET bettors: 1% at net 3-4, 2% at net 5+, and avg
bankroll % must be 10%+ (added the same day: under 10% showed no edge). Skipped
unless 2/3+ of the top traders' money in the market (the scanner's "Total $ in" vs
"Against $ in", by cost) is on our side. State in paper_trades_d.json / .csv.

Strategy E (added 2026-11-10): a sixth $1,000 paper bankroll. Like C, but a trader only
counts if the position shows conviction: worth 10%+ of their open bets (scanner
CONVICTION_PCT; picks carry "Conviction bettors" / "Conviction against"). 1% at net 3-4
conviction bettors, 2% at net 5+. Bets as soon as a pick qualifies (D tests betting
late). A "$5,000+ put in" alternative was dropped the same day, before any bets: replaying
all saved picks (Oct 29 - Nov 11), picks let in only by big dollars had no edge (+1c over
153), while bankroll % held up. Dollar size is NOT a proven signal here.

Strategy "Claude" (added 2026-11-10, paper only, `claude_strategy.py`): a $1,000
bankroll that copies only traders with a proven record of beating the closing price.
Report card (`trader_card.json`): every run notes each scanned trader's game positions
($100+, games within 3 days) with their average entry; when the game starts it is graded
vs the last price before the start (Polymarket /prices-history): CLV = close - entry.
Each game counts once per trader (average CLV of their positions in it). A trader
qualifies at 25+ graded games on 5+ different days, average CLV +1c or better, t-score
2.5+, and not trading at bot speed (>50 trades/hour over their last 500 trades; the
scanner notes this in PACE). Rules version 2 (2026-11-10): v1 counted every position and
had no day minimum or bot filter, so a 300+ trades/hour bot qualified in one evening on
alternate lines of one game; the card and the Claude bankroll restarted automatically
(claude_strategy.VERSION).
Qualified traders who drop off the leaderboard are still followed. Picks: outcomes held
by qualified traders, at least one of whom first bought within 6 hours. 1% with one
qualified trader, 2% with two or more (top-up once); all other rules as A. No bets until
traders qualify (expect 1-3 weeks). Rules fixed before any data; don't tune them to fit.
The report card shows on the Claude tab.

Report tabs (paper_report.html): Summary, Real bets, A, B, C, D, E, Claude, Statistics.
Summary tab (added 2026-11-12 as "Calendar", redesigned the same day after the owner's bet-tracker app;
the first tab and the page a fresh open lands on, while a Refresh keeps the current tab): a picker for
Real bets (default) or any paper strategy; a Performance card (profit, ROI, record W-L-push, running-profit
chart, Week / Month / Year / All = last 7 / 30 / 365 days / everything; tap or drag the chart to see
profit, ROI and record up to that bet, with its date and the bet itself; high and low are labelled); a two-week calendar strip; and
"View full calendar", which opens a month calendar (coloured squares with each day's profit, month total,
the day's bets with odds on tap, back arrow to the summary). A bet's day is its game day in Eastern time,
or the day it settled if earlier (forward_test.day_of / calendar_html / CAL_CSS / CAL_JS).
On computer screens (900px+): Performance and the calendar side by side, the calendar shows the whole
month, squares also show the record and number of bets, and the full calendar has a side panel (month
summary: profit, ROI, record, best/worst day; or the clicked day's bets). Phones are unchanged.
Both report pages have a Refresh button (added 2026-11-11) and reload themselves when reopened
after 5+ minutes: the owner uses the site as an iPhone home-screen app, which has no reload button
(scanner REFRESH_CSS / REFRESH_JS).

Statistics tab (added 2026-10-29, redesigned phone-first 2026-11-10: a card per
strategy, bar charts, breakdown picker, full table tap-to-open). Null hypothesis = the
market price is the true win chance. Per bet, expected profit = shares x price
- cost (minus the fees), variance = shares^2 x p(1-p). Z "beat the market" =
(profit - expected) / SD; Z "make money" = profit / SD. Verdicts need 30+
settled bets; the decision point is fixed at 200 settled bets (no early
stopping). Breakdowns by bet type, stake tier and sport (from the event slug).

Status: see FINDINGS.md (results snapshot and what the data has shown).

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
  Polymarket is discarded as a probable mismatch. UFC (added 2026-11-13): series
  KXUFCFIGHT, matched by date + both fighters' names (venues.same_fighter: full name,
  name without spaces, or last name); Kalshi lists a card about a week ahead.
  More leagues (added 2026-11-13): ~37 extra soccer leagues (venues.KALSHI_LEAGUES: EFL
  Championship, Eredivisie, Liga MX, Brazil, Portugal, Japan, Saudi, Nations League, ...) and
  esports CS2 + LoL (KALSHI_NAMED, matched by team names). Names are matched without accents
  (Brøndby = Broendby). Tested on two weeks of past picks: ~85% found. The new sports are
  PAPER ONLY (venues.PAPER_ONLY): real bets skip them until ~30 settle; then the owner decides.
- ProphetX (CFTC exchange, added 2026-11-08): https://cash.api.prophetx.co/partner,
  docs https://docs.prophetx.co. Needs the API keys even for prices (login ->
  Bearer token). Prices are American odds; each selection lists levels a taker can
  bet now (`value` = dollars that can be staked). Orders are in dollars staked,
  `fillOrKill`, price must be on the price ladder. Games are matched by league,
  start time (+-3h) and team names; >15c from Polymarket = discarded. The fee rate
  is set per account and not published. Seen 2026-11-09: no fee when a bet fills; about
  2% of the winnings is charged when a bet wins (venues.PX_FEE = 0.02). Seen 2026-11-10
  (Maple Leafs; the Oilers bet on 11-09 was the same): some fills DO charge a fee at once, on top
  of the stake (position cost_basis $2.03, fee $0.04, balance fell $2.07). live.py records that fee
  in the bet's cost, and such a bet is not charged again when it wins. Paper bets pay
  the expected fee up front; real bets have it taken from the payout at settlement. No public results: ProphetX bets
  settle by the international Polymarket result. No Premier League seen. UFC (added
  2026-11-13): any tournament named with "ufc", start time +-12h (Polymarket's time is the
  card's start), fight-winner = the moneyline market; untested until a GitHub run.

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
- Kalshi geoblocks orders by the sender's location. GitHub's runners sometimes
  land in a Washington data center, where Kalshi sports trading is blocked
  (HTTP 403, seen 2026-11-07). The owner chose to accept the occasional failed
  bet rather than change anything.

## Ideas / next steps
1. Let every strategy reach 200 settled bets before judging or re-tuning it
   (open questions are listed in FINDINGS.md).
2. Multi-window consistency (traders who rank in day, week AND month) is not
   built yet. `--period` takes one window.
3. Don't raise real-money stakes until A nears 200 settled bets and the
   closing-price (CLV) question is understood.
4. Done: GitHub Actions runs (2026-10-28); real money (2026-11-03); ProphetX
   (2026-11-08).
