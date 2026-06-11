# Polymarket Shadow Tracker

Paper-trading bot that watches the top 5 wallets on Polymarket's 30-day PnL
leaderboard and simulates copying their trades with realistic fills. **No
wallet, no private keys, no real money** — it reads public data only and
"trades" with virtual dollars.

The point: find out whether copy-trading the best Polymarket traders is still
profitable *after slippage* (the worse price you get because you're slower
than them), before risking a cent.

## How to check it

Open **dashboard.html** in your browser (double-click it). Leave the tab open —
it refreshes itself every 2 minutes. The tracker regenerates it every 5 minutes.

## How it works

- Runs every 5 minutes via macOS launchd (`com.ryder.polymarket-shadow`).
  No terminal needed; restarts automatically after reboot. Costs $0 to run —
  no AI calls, just public API reads.
- When a tracked wallet **buys** (trades over $100), it simulates buying $10
  of the same outcome **at the current best ask** — the price *you* would
  actually get, not the price they got. The gap is recorded as slippage.
- When they **sell**, it closes the simulated position at the current best bid.
- When a market **resolves**, positions settle at $1 (win) or $0 (loss).
- Trades seen more than 10 minutes late (e.g. Mac was asleep) are **not**
  copied — they're counted as "missed," because a real bot would miss them too.

## When is real money justified?

The dashboard's **Real-money readiness** panel answers this automatically.
ALL six must pass:

1. **21+ days tracked** — one week is noise; variance dominates short samples.
2. **100+ closed trades** — sample size matters more than calendar time.
3. **Net PnL positive after slippage** — the whole question.
4. **Still positive without the best single trade** — profit from one lucky
   hit is luck, not edge.
5. **Max drawdown under 25%** — if paper trading nearly busts the bankroll,
   real money would have.
6. **At least half the wallets individually profitable** — one good wallet
   among five duds means you got lucky picking, not that copying works.

Even with all six green: start with $50–100 you can fully afford to lose,
and remember Polymarket is a legal gray area in Quebec (Ontario is banned
outright; Canadian regulators warned the industry in 2026).

## Caveats

- **Mac asleep = blind.** It only sees trades while the Mac is awake. Missed
  trades are counted on the dashboard. If gaps get big, consider running it
  on something always-on.
- Top-of-book fills for a $10 stake are realistic; real money in size would
  get worse fills than this simulation shows.
- Past leaderboard performance is survivorship-biased — that's exactly what
  the checklist is designed to catch.

## Commands

```bash
# stop
launchctl bootout gui/$(id -u)/com.ryder.polymarket-shadow
# start
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ryder.polymarket-shadow.plist
# run one cycle manually / watch the log
python3 tracker.py
tail -f tracker.log
```

## Config (config.json)

- `wallets` — who to shadow (top 5 by 30-day PnL at setup, June 11 2026).
  Edit freely; the tracker picks up changes next cycle.
- `stake_per_trade` — virtual $ per copy (default 10)
- `min_their_trade_usd` — ignore their trades smaller than this (default 100)
- `max_copy_age_seconds` — don't copy stale trades (default 600)
