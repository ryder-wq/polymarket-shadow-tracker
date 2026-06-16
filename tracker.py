#!/usr/bin/env python3
"""Polymarket shadow-copy paper trader.

Watches top-leaderboard wallets and simulates copying their trades with
realistic fills (we buy at the current best ask, sell at the best bid).
No wallet, no keys, no real money — reads public data only.

Run once per cycle (launchd calls this every 5 minutes). Each cycle:
  1. Poll tracked wallets for new trades
  2. Open/close simulated positions mirroring them
  3. Settle positions whose markets resolved
  4. Mark open positions to market
  5. Regenerate dashboard.html
"""

import json
import os
import sys
import time
import html
import urllib.request
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
STATE_PATH = os.path.join(BASE, "state.json")
DASH_PATH = os.path.join(BASE, "dashboard.html")
LOCK_PATH = os.path.join(BASE, ".lock")

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "shadow-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def best_prices(token_id):
    """Return (best_bid, best_ask) or (None, None) if no orderbook."""
    try:
        book = get_json(f"{CLOB_API}/book?token_id={token_id}")
        bids = [float(x["price"]) for x in book.get("bids", [])]
        asks = [float(x["price"]) for x in book.get("asks", [])]
        return (max(bids) if bids else None, min(asks) if asks else None)
    except Exception:
        return (None, None)


def load(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def acquire_lock():
    if os.path.exists(LOCK_PATH):
        age = time.time() - os.path.getmtime(LOCK_PATH)
        if age < 240:
            log("another cycle is still running, skipping")
            sys.exit(0)
    with open(LOCK_PATH, "w") as f:
        f.write(str(os.getpid()))


def release_lock():
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass


# ---------------------------------------------------------------- core cycle

def poll_wallets(cfg, state):
    now = int(time.time())
    open_pos = state["open"]
    open_keys = {(p["wallet"], p["asset"]) for p in open_pos}
    seen_hashes = set(state["seen_hashes"])

    for w in cfg["wallets"]:
        addr, name = w["address"], w["name"]
        last_ts = state["seen"].get(addr, now)
        try:
            acts = get_json(
                f"{DATA_API}/activity?user={addr}&limit=500&type=TRADE&sortDirection=DESC"
            )
        except Exception as e:
            log(f"activity fetch failed for {name}: {e}")
            continue
        time.sleep(0.2)

        new = [a for a in acts
               if a["timestamp"] > last_ts and a["transactionHash"] not in seen_hashes]
        new.sort(key=lambda a: a["timestamp"])  # process oldest first

        for t in new:
            seen_hashes.add(t["transactionHash"])
            state["seen"][addr] = max(state["seen"].get(addr, 0), t["timestamp"])
            key = (addr, t["asset"])

            if t.get("usdcSize", 0) < cfg["min_their_trade_usd"]:
                continue
            age = now - t["timestamp"]

            if t["side"] == "BUY":
                if key in open_keys:
                    continue  # already shadowing this position
                if age > cfg["max_copy_age_seconds"]:
                    state["missed"] += 1
                    continue
                per_wallet_open = sum(1 for p in open_pos if p["wallet"] == addr)
                if per_wallet_open >= cfg["max_open_per_wallet"]:
                    state["missed"] += 1
                    continue
                bid, ask = best_prices(t["asset"])
                time.sleep(0.2)
                if ask is None or ask >= 0.999:
                    state["missed"] += 1
                    continue
                stake = cfg["stake_per_trade"]
                pos = {
                    "wallet": addr, "wallet_name": name,
                    "asset": t["asset"], "conditionId": t["conditionId"],
                    "title": t["title"], "outcome": t["outcome"],
                    "their_price": t["price"], "our_price": ask,
                    "shares": round(stake / ask, 4), "stake": stake,
                    "opened_at": now, "their_ts": t["timestamp"],
                    "mark": ask,
                }
                open_pos.append(pos)
                open_keys.add(key)
                log(f"COPY BUY  {name}: {t['title']} / {t['outcome']} "
                    f"their {t['price']:.3f} -> ours {ask:.3f}")

            elif t["side"] == "SELL" and key in open_keys:
                pos = next(p for p in open_pos
                           if p["wallet"] == addr and p["asset"] == t["asset"])
                bid, ask = best_prices(t["asset"])
                time.sleep(0.2)
                exit_price = bid if bid is not None else pos["mark"]
                close_position(state, pos, exit_price, "mirror_sell", now)
                open_keys.discard(key)
                log(f"COPY SELL {name}: {pos['title']} at {exit_price:.3f}")

    state["seen_hashes"] = list(seen_hashes)[-5000:]


def close_position(state, pos, exit_price, reason, now):
    pnl = round((exit_price - pos["our_price"]) * pos["shares"], 4)
    closed = dict(pos)
    closed.update({"exit_price": exit_price, "exit_reason": reason,
                   "pnl": pnl, "closed_at": now})
    closed.pop("mark", None)
    state["closed"].append(closed)
    state["open"].remove(pos)


def settle_resolved(state):
    now = int(time.time())
    by_condition = {}
    for p in state["open"]:
        by_condition.setdefault(p["conditionId"], []).append(p)

    for cid, positions in by_condition.items():
        try:
            m = get_json(f"{CLOB_API}/markets/{cid}")
        except Exception:
            continue
        time.sleep(0.2)
        if not m.get("closed"):
            continue
        winners = {t["token_id"]: t.get("winner", False) for t in m.get("tokens", [])}
        if not any(winners.values()):
            continue  # trading closed but not resolved yet
        for pos in positions:
            won = winners.get(pos["asset"], False)
            close_position(state, pos, 1.0 if won else 0.0,
                           "resolved_win" if won else "resolved_loss", now)
            log(f"RESOLVED {'WIN' if won else 'LOSS'}: {pos['title']} / {pos['outcome']}")


def mark_to_market(state):
    for pos in state["open"]:
        bid, _ = best_prices(pos["asset"])
        time.sleep(0.1)
        if bid is not None:
            pos["mark"] = bid


def snapshot_equity(cfg, state):
    realized = sum(c["pnl"] for c in state["closed"])
    unrealized = sum((p["mark"] - p["our_price"]) * p["shares"] for p in state["open"])
    equity = cfg["virtual_bankroll"] + realized + unrealized
    state["equity_history"].append([int(time.time()), round(equity, 2)])
    state["equity_history"] = state["equity_history"][-20000:]
    return realized, unrealized, equity


# ---------------------------------------------------------------- readiness

def readiness(cfg, state):
    days = (time.time() - state["started_at"]) / 86400
    closed = state["closed"]
    realized = sum(c["pnl"] for c in closed)
    top5 = sorted((c["pnl"] for c in closed), reverse=True)[:5]
    pnl_excl_top5 = realized - sum(p for p in top5 if p > 0)
    copied = len(closed) + len(state["open"])
    coverage = 100 * copied / (copied + state.get("missed", 0)) if copied else 0
    eq = [e[1] for e in state["equity_history"]] or [cfg["virtual_bankroll"]]
    peak, max_dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        max_dd = max(max_dd, peak - v)
    dd_pct = 100 * max_dd / cfg["virtual_bankroll"]

    wallet_pnl = {}
    for c in closed:
        wallet_pnl[c["wallet_name"]] = wallet_pnl.get(c["wallet_name"], 0) + c["pnl"]
    n_wallets = len(cfg["wallets"])
    profitable_wallets = sum(1 for v in wallet_pnl.values() if v > 0)

    checks = [
        ("Tracked for at least 21 days", days >= 21, f"{days:.1f} days"),
        ("At least 100 closed trades", len(closed) >= 100, f"{len(closed)} trades"),
        ("Net PnL positive after slippage", realized > 0, f"${realized:+.2f}"),
        ("Still positive without top 5 trades (not luck)", pnl_excl_top5 > 0,
         f"${pnl_excl_top5:+.2f}"),
        ("Copied 33%+ of eligible trades (sample is valid)", coverage >= 33,
         f"{coverage:.0f}%"),
        ("Max drawdown under 25% of bankroll", dd_pct < 25, f"{dd_pct:.1f}%"),
        (f"At least half of wallets profitable to copy",
         profitable_wallets >= (n_wallets + 1) // 2,
         f"{profitable_wallets}/{n_wallets}"),
    ]
    return checks, days


# ---------------------------------------------------------------- dashboard

def esc(s):
    return html.escape(str(s))


def fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%b %d %H:%M")


def equity_svg(cfg, state, width=920, height=220):
    hist = state["equity_history"]
    if len(hist) < 2:
        return "<p class='muted'>Equity curve appears after a few cycles.</p>"
    pts = hist[:: max(1, len(hist) // 400)]
    vals = [p[1] for p in pts]
    lo = min(min(vals), cfg["virtual_bankroll"]) - 1
    hi = max(max(vals), cfg["virtual_bankroll"]) + 1
    t0, t1 = pts[0][0], pts[-1][0]
    span_t = max(1, t1 - t0)

    def xy(p):
        x = 10 + (p[0] - t0) / span_t * (width - 20)
        y = height - 25 - (p[1] - lo) / (hi - lo) * (height - 45)
        return f"{x:.1f},{y:.1f}"

    base_y = height - 25 - (cfg["virtual_bankroll"] - lo) / (hi - lo) * (height - 45)
    poly = " ".join(xy(p) for p in pts)
    last = vals[-1]
    color = "#4ade80" if last >= cfg["virtual_bankroll"] else "#f87171"
    return f"""<svg viewBox="0 0 {width} {height}" style="width:100%;height:auto">
<line x1="10" y1="{base_y:.1f}" x2="{width-10}" y2="{base_y:.1f}"
      stroke="#555" stroke-dasharray="5,5"/>
<text x="{width-10}" y="{base_y-6:.1f}" fill="#888" font-size="11"
      text-anchor="end">start ${cfg['virtual_bankroll']}</text>
<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="2"/>
<text x="{width-10}" y="18" fill="{color}" font-size="14" font-weight="bold"
      text-anchor="end">${last:,.2f}</text>
</svg>"""


def build_dashboard(cfg, state, realized, unrealized, equity):
    checks, days = readiness(cfg, state)
    all_pass = all(ok for _, ok, _ in checks)
    closed = state["closed"]
    wins = sum(1 for c in closed if c["pnl"] > 0)
    win_rate = 100 * wins / len(closed) if closed else 0
    slips = [c["our_price"] - c["their_price"] for c in closed] + \
            [p["our_price"] - p["their_price"] for p in state["open"]]
    avg_slip = 100 * sum(slips) / len(slips) if slips else 0

    check_rows = "".join(
        f"<div class='check {'ok' if ok else 'no'}'>"
        f"<span class='mark'>{'&#10004;' if ok else '&#10008;'}</span>"
        f"<span>{esc(label)}</span><span class='val'>{esc(val)}</span></div>"
        for label, ok, val in checks)

    verdict = ("READY — criteria met. Consider a small real test ($50–100 max)."
               if all_pass else
               "NOT READY — keep paper trading. Do not deposit real money yet.")

    wallet_stats = {}
    for c in closed:
        s = wallet_stats.setdefault(c["wallet_name"], {"n": 0, "pnl": 0.0})
        s["n"] += 1
        s["pnl"] += c["pnl"]
    for p in state["open"]:
        s = wallet_stats.setdefault(p["wallet_name"], {"n": 0, "pnl": 0.0})
    wallet_rows = "".join(
        f"<tr><td>{esc(n)}</td><td>{s['n']}</td>"
        f"<td class='{'pos' if s['pnl']>=0 else 'neg'}'>${s['pnl']:+.2f}</td></tr>"
        for n, s in sorted(wallet_stats.items(), key=lambda kv: -kv[1]["pnl"]))

    open_rows = "".join(
        f"<tr><td>{esc(p['wallet_name'])}</td>"
        f"<td>{esc(p['title'][:60])}</td><td>{esc(p['outcome'])}</td>"
        f"<td>{p['their_price']:.3f}</td><td>{p['our_price']:.3f}</td>"
        f"<td>{p['mark']:.3f}</td>"
        f"<td class='{'pos' if p['mark']>=p['our_price'] else 'neg'}'>"
        f"${(p['mark']-p['our_price'])*p['shares']:+.2f}</td>"
        f"<td>{fmt_ts(p['opened_at'])}</td></tr>"
        for p in sorted(state["open"], key=lambda p: -p["opened_at"]))

    closed_rows = "".join(
        f"<tr><td>{esc(c['wallet_name'])}</td>"
        f"<td>{esc(c['title'][:60])}</td><td>{esc(c['outcome'])}</td>"
        f"<td>{c['our_price']:.3f}</td><td>{c['exit_price']:.3f}</td>"
        f"<td>{esc(c['exit_reason'].replace('_',' '))}</td>"
        f"<td class='{'pos' if c['pnl']>=0 else 'neg'}'>${c['pnl']:+.2f}</td>"
        f"<td>{fmt_ts(c['closed_at'])}</td></tr>"
        for c in sorted(closed, key=lambda c: -c["closed_at"])[:25])

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="120">
<title>Polymarket Shadow Tracker</title>
<style>
 body{{background:#0f1115;color:#e7e7ea;font-family:-apple-system,Segoe UI,sans-serif;
      margin:0;padding:24px;max-width:1000px;margin:auto}}
 h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:15px;color:#d4af37;margin:28px 0 10px;
      text-transform:uppercase;letter-spacing:.08em}}
 .muted{{color:#8a8f98;font-size:13px}}
 .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
      gap:10px;margin:18px 0}}
 .card{{background:#1a1d24;border-radius:10px;padding:14px}}
 .card .k{{font-size:11px;color:#8a8f98;text-transform:uppercase;letter-spacing:.06em}}
 .card .v{{font-size:20px;font-weight:600;margin-top:4px}}
 .pos{{color:#4ade80}} .neg{{color:#f87171}}
 .verdict{{border-radius:10px;padding:14px 16px;font-weight:600;margin:14px 0;
      background:{'#14331f' if all_pass else '#33141a'};
      border:1px solid {'#4ade80' if all_pass else '#f87171'}}}
 .check{{display:flex;gap:10px;align-items:center;background:#1a1d24;
      border-radius:8px;padding:9px 12px;margin:5px 0;font-size:14px}}
 .check .mark{{width:18px}} .check.ok .mark{{color:#4ade80}} .check.no .mark{{color:#f87171}}
 .check .val{{margin-left:auto;color:#8a8f98}}
 table{{width:100%;border-collapse:collapse;font-size:13px}}
 th{{text-align:left;color:#8a8f98;font-weight:500;padding:6px 8px;
      border-bottom:1px solid #2a2e37}}
 td{{padding:6px 8px;border-bottom:1px solid #1e222a}}
</style></head><body>
<h1>Polymarket Shadow Tracker <span class="muted">paper trading — no real money</span></h1>
<p class="muted">Last updated {datetime.now().strftime('%b %d, %Y %H:%M:%S')} ·
 day {days:.1f} · auto-refreshes every 2 min · missed (too slow to copy): {state['missed']}</p>

<div class="cards">
 <div class="card"><div class="k">Virtual equity</div><div class="v">${equity:,.2f}</div></div>
 <div class="card"><div class="k">Realized PnL</div>
   <div class="v {'pos' if realized>=0 else 'neg'}">${realized:+.2f}</div></div>
 <div class="card"><div class="k">Unrealized PnL</div>
   <div class="v {'pos' if unrealized>=0 else 'neg'}">${unrealized:+.2f}</div></div>
 <div class="card"><div class="k">Closed trades</div><div class="v">{len(closed)}</div></div>
 <div class="card"><div class="k">Win rate</div><div class="v">{win_rate:.0f}%</div></div>
 <div class="card"><div class="k">Avg slippage</div><div class="v">{avg_slip:+.1f}&cent;</div></div>
</div>

<h2>Real-money readiness</h2>
<div class="verdict">{verdict}</div>
{check_rows}

<h2>Equity curve</h2>
{equity_svg(cfg, state)}

<h2>Per-wallet results</h2>
<table><tr><th>Wallet</th><th>Closed trades</th><th>PnL</th></tr>{wallet_rows or
 "<tr><td colspan=3 class='muted'>No trades copied yet</td></tr>"}</table>

<h2>Open positions ({len(state['open'])})</h2>
<table><tr><th>Wallet</th><th>Market</th><th>Outcome</th><th>Their&nbsp;entry</th>
<th>Our&nbsp;entry</th><th>Now</th><th>PnL</th><th>Opened</th></tr>{open_rows or
 "<tr><td colspan=8 class='muted'>None yet — positions appear when a tracked wallet trades</td></tr>"}</table>

<h2>Recent closed trades</h2>
<table><tr><th>Wallet</th><th>Market</th><th>Outcome</th><th>Entry</th><th>Exit</th>
<th>Reason</th><th>PnL</th><th>Closed</th></tr>{closed_rows or
 "<tr><td colspan=8 class='muted'>None yet</td></tr>"}</table>

<p class="muted" style="margin-top:24px">Stake ${cfg['stake_per_trade']} per copied trade ·
 copying trades over ${cfg['min_their_trade_usd']} ·
 only copies trades seen within {cfg['max_copy_age_seconds']//60} min (Mac asleep = trades missed, counted above) ·
 log: tracker.log</p>
</body></html>"""


def publish(cfg, state):
    """Copy dashboard to docs/ and push to GitHub so Pages serves it.

    Throttled to push_interval_seconds — GitHub Pages soft-limits builds
    to ~10/hour, so stay well under. Failures never break tracking; they
    log and retry next cycle.
    """
    if not cfg.get("publish"):
        return
    now = int(time.time())
    if now - state.get("last_push", 0) < cfg.get("push_interval_seconds", 900):
        return
    import shutil
    import subprocess
    os.makedirs(os.path.join(BASE, "docs"), exist_ok=True)
    shutil.copyfile(DASH_PATH, os.path.join(BASE, "docs", "index.html"))
    try:
        subprocess.run(["git", "add", "-A"], cwd=BASE, capture_output=True, timeout=30)
        subprocess.run(["git", "commit", "-m", "dashboard update"],
                       cwd=BASE, capture_output=True, timeout=30)
        r = subprocess.run(["git", "push"], cwd=BASE, capture_output=True, timeout=90)
        if r.returncode == 0:
            state["last_push"] = now
            log("pushed dashboard to GitHub")
        else:
            log(f"git push failed: {r.stderr.decode()[:150]}")
    except Exception as e:
        log(f"publish error: {e}")


# ---------------------------------------------------------------- main

def main():
    cfg = load(CONFIG_PATH, None)
    if cfg is None:
        log("config.json missing — run setup first")
        sys.exit(1)
    acquire_lock()
    try:
        state = load(STATE_PATH, {
            "started_at": int(time.time()), "seen": {}, "seen_hashes": [],
            "open": [], "closed": [], "missed": 0, "equity_history": [],
        })
        poll_wallets(cfg, state)
        settle_resolved(state)
        mark_to_market(state)
        realized, unrealized, equity = snapshot_equity(cfg, state)
        save(STATE_PATH, state)
        with open(DASH_PATH, "w") as f:
            f.write(build_dashboard(cfg, state, realized, unrealized, equity))
        publish(cfg, state)
        save(STATE_PATH, state)
        log(f"cycle done: equity ${equity:,.2f} | open {len(state['open'])} "
            f"| closed {len(state['closed'])} | missed {state['missed']}")
    finally:
        release_lock()


if __name__ == "__main__":
    main()
