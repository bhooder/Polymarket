# Findings and decisions log

What the data has shown so far, and why each rule exists. Read this with
`CLAUDE.md` (how the project works). Numbers are as of the date on each section;
re-run the analysis before relying on them, because samples are still small.

## How to read these numbers
- **Edge per bet** = (won ? 1 : 0) - price paid, averaged per $1 share. The price is the
  market's own win chance, so +10c means we won 10 percentage points more often than
  the prices implied. **z** = edge / its standard error (sqrt of sum p(1-p), / n).
  |z| < 2 is within normal luck.
- **CLV (closing line value)** = price just before the game started - price we paid.
  Sharp bettors usually show positive CLV. It needs no game results, so it is fast.
- **Multiple comparisons:** many slices were tested. Expect some "findings" to be
  chance. Trust only results that are large, hold in more than one cut, and make sense.
- **The decision point for each strategy is fixed at 200 settled bets.** Don't stop or
  re-tune a strategy early because z touched 2 (that makes flukes likely).

## Results snapshot, 2026-11-10 (settled bets)
| | Bets | Win rate | Profit | Return per $ | z beat market |
|---|---|---|---|---|---|
| A (bettors + avg bankroll %) | 78 | 72% | +$265 | +22% | +2.2 |
| B (bettor count only) | 196 | 56% | +$71 | +3% | +0.8 |
| C (net bettors) | 55 | 56% | +$75 | +11% | +1.0 |
| Real money (copies A) | 33 | 76% | +$16.52 | +19% | +1.2 |
D, E and Claude started 2026-11-10 (no settled bets yet).

## What the data showed (2026-11-10)
Sources: all unique paper bets from A/B/C (258, 242 settled), plus a replay of every
saved picks page (git history of `picks.html`, Oct 29 - Nov 11, results from Gamma).

1. **Avg bankroll % is the strongest signal; bettor count is not.**
   - Picks with avg bankroll % < 10%: 148 bets, +2c (no edge). 10-35%: 83 bets, +20c.
   - By bettor count (3, 4, 5, 6+) there was no steady pattern.
   - That is why A works and B (count only) is about break-even.
2. **It is a threshold, not "more is better".** 35%+ bankroll: 11 bets, +1c. A's 2% tier
   (highest bankroll %) did worse than its 1% tier: +6c (30 bets) vs +26c (48 bets);
   the gap is ~1.8 SD, so suggestive, not proven. Real bets showed the same.
3. **Paying above the traders' entry erases the edge.** A bets at <= entry+1c: +27c (41);
   above +1c: +9c (38). Same pattern outside A. -> price rule tightened to +1c.
4. **Underdogs under 40c lost money** everywhere (32 bets, -7c). -> 35c price floor.
5. **Dollar size is NOT a proven signal.** An early cut (bets A/B/C took) suggested
   $5k+ per bettor helped, but the full replay of all saved picks contradicted it:
   picks let in only by big dollars (bankroll < 10%) had +1c over 153 picks, with no
   steady pattern by $5-10k / $10-20k / $20k+. -> dropped from Strategy E.
6. **Timing:** non-A picks placed < 2h before start did better (+9c vs -2c), but A's
   picks did equally well early or late. Late counter-bets (other side catching up
   after we bet) did not hurt so far (+10c, 25 bets). Strategy D tests betting late.
7. **A is steady over time:** first half of its bets +17c, second half +20c.
8. **Warning: CLV is about zero.** The closing price ended ~0.6c AGAINST our picks on
   average (part may be the US-venue vs international price gap). All of A's edge is
   in results, none in price movement. Either part of it is luck, or top traders know
   something the market never prices. Main reason not to raise real stakes yet.
9. **Replay of A vs approximate E (same history, same price rules):** A 103 picks, +11c
   (z 2.3); E-with-$5k-rule 278 picks, +3c. Picks both took +9c; only A +17c; only E
   +0.3c (200 picks).

## Decisions made (with dates)
- 2026-11-08: ProphetX added as a venue (paper and real). Fee measured 2026-11-09:
  ~2% of winnings, charged when a bet wins (venues.PX_FEE = 0.02).
- 2026-11-10: Strategy Claude (trader report card by CLV), rules fixed before data.
- 2026-11-10: Strategy D (late money: 90-15 min before start, net bettors, 2/3 of the
  money on our side, avg bankroll 10%+).
- 2026-11-10: price rule +2c -> +1c above entry; 35c floor. All strategies + real bets.
- 2026-11-10: Strategy E (per-trader conviction = 10%+ of bankroll, net counts).
  The $5,000 alternative was dropped the same day (finding 5). E's first bet (Celtics,
  Nov 10 7:45 PM) was placed under the old rule and left as is.
- 2026-11-10: Strategy Claude rules v2. Within an hour of starting, a high-speed bot
  (~320 trades/hour, both sides of many markets) "qualified" on 26 positions from one
  evening, mostly alternate spreads of one game, and 7 paper bets were copied. Fix: each
  game counts once per trader, 5+ different days required, traders over 50 trades/hour
  never copied. The report card and the Claude bankroll were restarted.
- 2026-11-10: real bets switched to a FLAT 1.5% of the real bankroll, no top-ups
  (finding 2). Paper Strategy A keeps its 1%/2% tiers so its test stays clean.
- 2026-11-11: owner's choice: Strategy A and real bets never pay over 75c (about -300).
  Not from a data finding; other strategies keep no cap. A's results from here on mix
  old (uncapped) and new bets.
- 2026-11-11: owner's choice: price floor lowered 35c -> 30c, then the same day to
  +250 (~28.6c), all strategies + real bets. Finding 4 (under-40c bets lost money) still
  stands; these bets get stake x0.77-0.90.
- 2026-11-13: owner's choice: price rule back to +2c above entry (all strategies + real
  bets). Re-check: bets paid +1.5-2.5c over entry: A +20c (11 bets), all strategies +4c
  (50), about the same as +1c bets (+3c, 104). Paying BELOW entry is still where the edge is.

## Open questions / what to check next
- Does CLV turn positive for any trader? (Claude tab report card; qualified traders
  expected after 1-3 weeks.)
- E vs A: does checking each trader beat A's average? Needs many settled E bets.
- D: does betting late help once bankroll % is already required?
- Flat 1.5% real stakes vs A's tiers: compare on the Statistics tab ("Real vs paper").
- Don't raise real-money stakes until A nears 200 settled bets and CLV is understood.

## How to redo the analysis
1. Unique bets: merge `paper_trades*.json` by `token_id`; keep `won`/`lost`.
2. Closing price: `GET https://clob.polymarket.com/prices-history?market=<token_id>&startTs=<start-6h>&endTs=<start>&fidelity=1`,
   last point before the start (`ends` field = game start).
3. Pick history: every forward-test commit saved `picks.html`; `git log -- picks.html`
   then `git show <hash>:picks.html` and parse rows (Bettors, Against, Avg bankroll %,
   Total $ in, entry and current price). Results and start times: Gamma
   `/events?slug=<event slug>` -> markets (question, outcomes, outcomePrices,
   gameStartTime).
