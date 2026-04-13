#!/usr/bin/env python3
"""
Polymarket Copy Trading Bot — Fully Optimized
Improvements:
  1. 15-second polling (faster entries, better fill prices)
  2. Dynamic position sizing based on trader win rate
  3. Session expiry alerts (warns you when login is needed)
  4. Spread filter (skips markets with wide bid-ask spreads)
  5. Volume filter (skips markets under $50K total volume)
  6. Tracks top 10 traders instead of 5
  7. Skips markets expiring within 2 hours
  + Consensus boost: 2+ traders agree → bigger bet
  + Copies both entries (buys) and exits (sells)
  + Logs every trade to trades.json for performance tracking
"""

import subprocess
import json
import time
import logging
import os
import math
from datetime import datetime, timezone
from typing import Optional, Union
from collections import Counter
from token_refresh import refresh_token_if_needed

# ── Configuration ─────────────────────────────────────────────────────────────
TOP_TRADERS_COUNT    = 10      # #6: track top 10 traders
STOP_LOSS_BALANCE    = 5.0     # pause if balance drops here
POLL_INTERVAL_SEC    = 15      # #1: poll every 15s for faster entries
LEADERBOARD_REFRESH  = 3600    # refresh trader list every hour
MAX_TRADE_AGE_SEC    = 300     # only copy trades from last 5 minutes
MIN_EXPIRY_HOURS     = 2.0     # #7: skip markets expiring within 2 hours
MAX_SPREAD           = 0.10    # #5: skip markets with spread > 10¢
MIN_VOLUME_USD       = 50000   # #7: skip markets with < $50K total volume
CONSENSUS_MULTIPLIER = 2.0     # multiply bet size when 2+ traders agree
MIN_TRADES_PER_DAY   = 5.0     # skip traders with fewer than 5 trades/day
LEADERBOARD_DAYS     = 7       # smart-money API covers last 7 days
LOG_FILE             = os.path.join(os.path.dirname(__file__), "copy_trader.log")
TRADES_FILE          = os.path.join(os.path.dirname(__file__), "trades.json")
DASHBOARD_FILE       = os.path.join(os.path.dirname(__file__), "dashboard.html")

# #2: Dynamic sizing by win rate
def trade_size_for(win_rate: float) -> float:
    if win_rate >= 0.95:  return 8.0   # 100% win rate
    if win_rate >= 0.75:  return 6.0   # 75%+ win rate
    if win_rate >= 0.50:  return 5.0   # 50%+ win rate
    return 4.0                          # below 50%

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ── JSON trade log ────────────────────────────────────────────────────────────

def log_trade(action: str, slug: str, outcome: str, amount: float,
              shares: float, trader_name: str, success: bool, error: str = "") -> None:
    record = {
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "action":      action,
        "slug":        slug,
        "outcome":     outcome,
        "amount_usd":  amount,
        "shares":      shares,
        "copied_from": trader_name,
        "success":     success,
        "error":       error,
    }
    trades = []
    if os.path.exists(TRADES_FILE):
        try:
            with open(TRADES_FILE, "r") as f:
                trades = json.load(f)
        except Exception:
            trades = []
    trades.append(record)
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2)


# ── Helpers ───────────────────────────────────────────────────────────────────

def run(args: list) -> Optional[Union[dict, list]]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def run_with_auth(args: list) -> Optional[Union[dict, list]]:
    """Run command, detect and alert on auth failures."""
    result = run(args)
    if result is None:
        raw = subprocess.run(args, capture_output=True, text=True, timeout=30)
        stderr = raw.stderr
        if "Token refresh failed" in stderr or "unauthenticated" in stderr.lower():
            # #4: Clear session expiry alert
            log.warning("=" * 50)
            log.warning("AUTH EXPIRED — Bot is paused!")
            log.warning("Run: bullpen login")
            log.warning("Then approve at: https://app.bullpen.fi/device")
            log.warning("=" * 50)
    return result


def get_balance() -> float:
    data = run_with_auth(["bullpen", "portfolio", "balances", "--output", "json"])
    if not data:
        return -1.0
    try:
        for chain in data.get("chains", []):
            if "polygon" in chain.get("chain_name", "").lower():
                for item in chain.get("items", []):
                    if item.get("symbol", "").upper() in ("USDC", "PUSD"):
                        return float(item.get("value_usd", 0))
        return float(data.get("total_usd", 0))
    except Exception:
        return -1.0


def score_trader(t: dict) -> float:
    """Combined score: 50% win rate + 30% log-scaled PnL + 20% trade activity."""
    win_rate     = t.get("win_rate") or 0.5
    pnl          = float(t.get("pnl") or 0)
    trades       = int(t.get("trades_count") or 0)
    pnl_score    = math.log10(max(pnl, 1)) / 10.0
    # Activity score: log scale, cap at 100 trades = 1.0
    activity     = math.log10(max(trades, 1)) / math.log10(100)
    activity     = min(activity, 1.0)
    return 0.50 * win_rate + 0.30 * pnl_score + 0.20 * activity


def get_top_traders(n: int = 10) -> list:
    data = run_with_auth([
        "bullpen", "polymarket", "data", "smart-money",
        "--type", "top_traders", "--output", "json", "--limit", "100",
    ])
    if not data:
        return []
    raw = data.get("traders", [])

    # Attach computed fields before filtering
    for t in raw:
        trades_count         = int(t.get("trades_count") or 0)
        t["_trades_per_day"] = trades_count / LEADERBOARD_DAYS
        t["_score"]          = score_trader(t)
        t["_bet_size"]       = trade_size_for(t.get("win_rate") or 0.5)

    # Filter: no bots, 70%+ win rate, 5+ trades/day
    traders = [
        t for t in raw
        if not t.get("is_bot")
        and (t.get("win_rate") or 0) >= 0.70
        and t["_trades_per_day"] >= MIN_TRADES_PER_DAY
    ]

    traders.sort(key=lambda x: x["_score"], reverse=True)
    top = traders[:n]
    log.info("Top %d traders (≥70%% WR, ≥%.0f trades/day, sorted by score):", len(top), MIN_TRADES_PER_DAY)
    for t in top:
        wr  = (t.get("win_rate") or 0) * 100
        pnl = float(t.get("pnl") or 0)
        tpd = t["_trades_per_day"]
        log.info("  %-25s  WR: %5.1f%%  T/d: %4.1f  PnL: $%.0f  Bet: $%.0f  Score: %.3f",
                 t.get("name", "?"), wr, tpd, pnl, t["_bet_size"], t["_score"])
    return top


def get_recent_activity(address: str, side: str, limit: int = 5) -> list:
    data = run_with_auth([
        "bullpen", "polymarket", "activity",
        "--address", address, "--type", "trade",
        "--side", side, "--limit", str(limit), "--output", "json",
    ])
    return data if data and isinstance(data, list) else []


def auto_redeem() -> None:
    """Redeem any resolved winning positions to free up cash."""
    data = run_with_auth(["bullpen", "polymarket", "positions", "--redeemable", "--output", "json"])
    if not data:
        return
    redeemable = data.get("positions", [])
    value      = float(data.get("summary", {}).get("redeemable_value", 0))
    if not redeemable:
        return
    log.info("Auto-redeeming %d resolved position(s) worth $%.2f...", len(redeemable), value)
    result = subprocess.run(
        ["bullpen", "polymarket", "redeem", "--yes"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        log.info("✓ Redeemed $%.2f back to wallet", value)
    else:
        log.warning("Redeem failed: %s", result.stderr.strip())


def get_my_positions() -> dict:
    data = run_with_auth(["bullpen", "polymarket", "positions", "--output", "json"])
    if not data:
        return {}
    return {
        f"{p['slug']}|{p['outcome']}": float(p.get("size", 0))
        for p in data.get("positions", [])
        if p.get("slug") and p.get("outcome") and float(p.get("size", 0)) > 0
    }


def is_fresh(trade: dict) -> bool:
    ts = trade.get("timestamp", "")
    if not ts:
        return True
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
        return age <= MAX_TRADE_AGE_SEC
    except Exception:
        return True


def market_ok(slug: str, outcome: str) -> bool:
    """
    Returns True if the market passes all filters:
    - Not expiring within MIN_EXPIRY_HOURS     (#7)
    - Total volume >= MIN_VOLUME_USD           (#7)
    - Spread for this outcome <= MAX_SPREAD    (#5)
    """
    # Check expiry + volume from market info
    mkt = run(["bullpen", "polymarket", "market", slug, "--output", "json"])
    if mkt:
        # Expiry check
        end_date = mkt.get("end_date", "")
        if end_date:
            try:
                end_time = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                hours_left = (end_time - datetime.now(timezone.utc)).total_seconds() / 3600
                if hours_left < MIN_EXPIRY_HOURS:
                    log.info("Skip %s — expires in %.1fh", slug, hours_left)
                    return False
            except Exception:
                pass
        # Volume check
        volume = float(mkt.get("volume") or 0)
        if volume < MIN_VOLUME_USD:
            log.info("Skip %s — volume $%.0f below $%.0f min", slug, volume, MIN_VOLUME_USD)
            return False

    # Spread check from price data
    price_data = run(["bullpen", "polymarket", "price", slug, "--output", "json"])
    if price_data:
        for o in price_data.get("outcomes", []):
            if o.get("outcome", "").lower() == outcome.lower():
                spread = float(o.get("spread") or 0)
                if spread > MAX_SPREAD:
                    log.info("Skip %s %s — spread %.3f¢ too wide", slug, outcome, spread)
                    return False
                break

    return True


def execute_buy(slug: str, outcome: str, amount: float, trader_name: str) -> bool:
    result = subprocess.run(
        ["bullpen", "polymarket", "buy", slug, outcome, str(amount), "--yes"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        log.info("✓ BUY  %s | %s | $%.2f  (from %s)", slug, outcome, amount, trader_name)
        log_trade("BUY", slug, outcome, amount, 0, trader_name, True)
        return True
    err = result.stderr.strip()
    if "No opposing orders" in err or "no market price" in err or "404" in err:
        log.info("Skip buy %s %s — no liquidity", slug, outcome)
    else:
        log.warning("✗ BUY failed: %s %s — %s", slug, outcome, err)
        log_trade("BUY", slug, outcome, amount, 0, trader_name, False, err)
    return False


def execute_sell(slug: str, outcome: str, shares: float, trader_name: str) -> bool:
    result = subprocess.run(
        ["bullpen", "polymarket", "sell", slug, outcome, f"{shares:.4f}", "--yes"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        log.info("✓ SELL %s | %s | %.4f shares  (from %s)", slug, outcome, shares, trader_name)
        log_trade("SELL", slug, outcome, 0, shares, trader_name, True)
        return True
    err = result.stderr.strip()
    if "No opposing orders" in err or "no market price" in err or "404" in err:
        log.info("Skip sell %s %s — no liquidity", slug, outcome)
    else:
        log.warning("✗ SELL failed: %s %s — %s", slug, outcome, err)
        log_trade("SELL", slug, outcome, 0, shares, trader_name, False, err)
    return False


# ── Dashboard ─────────────────────────────────────────────────────────────────

def write_dashboard(balance: float, traders: list, buys: int, sells: int) -> None:
    """Write a self-refreshing dashboard.html with live bot data."""
    trades = []
    if os.path.exists(TRADES_FILE):
        try:
            with open(TRADES_FILE) as f:
                trades = json.load(f)
        except Exception:
            trades = []

    # Stats calculation
    total_invested  = sum(t["amount_usd"] for t in trades if t["action"] == "BUY" and t["success"])
    successful_buys = [t for t in trades if t["action"] == "BUY" and t["success"]]
    successful_sells= [t for t in trades if t["action"] == "SELL" and t["success"]]
    failed_trades   = [t for t in trades if not t["success"]]

    # Win rate: a "win" is a completed round-trip (we bought then sold)
    sold_slugs   = {f"{t['slug']}|{t['outcome']}" for t in successful_sells}
    winners      = sum(1 for t in successful_buys if f"{t['slug']}|{t['outcome']}" in sold_slugs)
    losers       = len(successful_sells) - winners if len(successful_sells) > winners else 0
    total_closed = winners + losers
    win_rate     = (winners / total_closed * 100) if total_closed > 0 else 0

    starting_balance = 49.05
    pnl       = balance - starting_balance
    pnl_color = "#00c853" if pnl >= 0 else "#ff1744"
    pnl_sign  = "+" if pnl >= 0 else ""

    # Recent trades rows
    trade_rows = ""
    for t in reversed(trades[-50:]):
        action = t["action"]
        success = t["success"]
        if not success:
            continue
        color  = "#1b5e20" if action == "BUY" else "#b71c1c"
        bg     = "#e8f5e9" if action == "BUY" else "#ffebee"
        label  = "🟢 BUY" if action == "BUY" else "🔴 SELL"
        ts     = t["timestamp"][:16].replace("T", " ")
        slug   = t["slug"]
        outcome = t["outcome"]
        amount  = f"${t['amount_usd']:.2f}" if action == "BUY" else f"{t['shares']:.2f} shares"
        copied  = t["copied_from"]
        trade_rows += f"""
        <tr style="background:{bg}">
          <td style="color:{color};font-weight:bold">{label}</td>
          <td>{ts}</td>
          <td style="font-size:12px">{slug}</td>
          <td><b>{outcome}</b></td>
          <td>{amount}</td>
          <td style="font-size:12px">{copied}</td>
        </tr>"""

    if not trade_rows:
        trade_rows = '<tr><td colspan="6" style="text-align:center;padding:30px;color:#999">No trades yet — bot is watching markets...</td></tr>'

    # Trader rows
    trader_rows = ""
    for t in traders:
        wr  = (t.get("win_rate") or 0) * 100
        pnl_t = float(t.get("pnl") or 0)
        bet = t.get("_bet_size", 5.0)
        tpd = t.get("_trades_per_day", 0.0)
        wr_color  = "#00c853" if wr >= 75 else "#ff6f00" if wr >= 50 else "#ff1744"
        tpd_color = "#00c853" if tpd >= 10 else "#ff6f00" if tpd >= 5 else "#ff1744"
        trader_rows += f"""
        <tr>
          <td>{t.get("name","?")}</td>
          <td style="color:{wr_color};font-weight:bold">{wr:.1f}%</td>
          <td style="color:{tpd_color};font-weight:bold">{tpd:.1f}</td>
          <td style="color:#00c853">+${pnl_t:,.0f}</td>
          <td>${bet:.0f}</td>
        </tr>"""

    updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="30">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Polymarket Copy Bot</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: #0a0e1a; color: #e0e0e0; min-height: 100vh; padding: 24px; }}
    h1 {{ font-size: 26px; font-weight: 700; color: #fff; margin-bottom: 4px; }}
    .subtitle {{ color: #666; font-size: 13px; margin-bottom: 28px; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
              gap: 16px; margin-bottom: 28px; }}
    .card {{ background: #131929; border-radius: 12px; padding: 20px; border: 1px solid #1e2d45; }}
    .card-label {{ font-size: 12px; color: #666; text-transform: uppercase;
                   letter-spacing: 1px; margin-bottom: 8px; }}
    .card-value {{ font-size: 28px; font-weight: 700; color: #fff; }}
    .card-value.green {{ color: #00c853; }}
    .card-value.red   {{ color: #ff1744; }}
    .section {{ background: #131929; border-radius: 12px; padding: 20px;
                border: 1px solid #1e2d45; margin-bottom: 20px; }}
    .section h2 {{ font-size: 15px; font-weight: 600; color: #fff;
                   margin-bottom: 16px; border-bottom: 1px solid #1e2d45; padding-bottom: 10px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th {{ text-align: left; color: #666; font-size: 11px; text-transform: uppercase;
          letter-spacing: 0.5px; padding: 8px 10px; border-bottom: 1px solid #1e2d45; }}
    td {{ padding: 10px; border-bottom: 1px solid #0d1520; }}
    tr:last-child td {{ border-bottom: none; }}
    .status-dot {{ display: inline-block; width: 8px; height: 8px; background: #00c853;
                   border-radius: 50%; margin-right: 8px; animation: pulse 2s infinite; }}
    @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:0.4}} }}
    .updated {{ font-size: 11px; color: #444; text-align: right; margin-top: 16px; }}
  </style>
</head>
<body>
  <h1>🤖 Polymarket Copy Bot</h1>
  <p class="subtitle"><span class="status-dot"></span>Live · Auto-refreshes every 30 seconds · Last updated {updated}</p>

  <div class="cards">
    <div class="card">
      <div class="card-label">Balance</div>
      <div class="card-value">${balance:.2f}</div>
    </div>
    <div class="card">
      <div class="card-label">Total P&amp;L</div>
      <div class="card-value {'green' if pnl >= 0 else 'red'}">{pnl_sign}${abs(pnl):.2f}</div>
    </div>
    <div class="card">
      <div class="card-label">Win Rate</div>
      <div class="card-value {'green' if win_rate >= 50 else 'red'}">{win_rate:.0f}%</div>
    </div>
    <div class="card">
      <div class="card-label">Winners</div>
      <div class="card-value green">{winners}</div>
    </div>
    <div class="card">
      <div class="card-label">Losers</div>
      <div class="card-value {'red' if losers > 0 else ''}">{losers}</div>
    </div>
    <div class="card">
      <div class="card-label">Buys / Sells</div>
      <div class="card-value">{buys} / {sells}</div>
    </div>
    <div class="card">
      <div class="card-label">Total Invested</div>
      <div class="card-value">${total_invested:.2f}</div>
    </div>
    <div class="card">
      <div class="card-label">Traders Tracked</div>
      <div class="card-value">{len(traders)}</div>
    </div>
  </div>

  <div class="section">
    <h2>👥 Traders Being Copied</h2>
    <table>
      <thead>
        <tr>
          <th>Trader</th><th>Win Rate</th><th>Trades/Day</th><th>Weekly PnL</th><th>Bet Size</th>
        </tr>
      </thead>
      <tbody>{trader_rows}</tbody>
    </table>
  </div>

  <div class="section">
    <h2>📋 Trade History</h2>
    <table>
      <thead>
        <tr>
          <th>Action</th><th>Time</th><th>Market</th><th>Outcome</th><th>Amount</th><th>Copied From</th>
        </tr>
      </thead>
      <tbody>{trade_rows}</tbody>
    </table>
  </div>

  <p class="updated">Starting balance: $49.05 · Stop loss: ${STOP_LOSS_BALANCE:.2f} · Poll: every {POLL_INTERVAL_SEC}s · Filters: ≥70% WR, ≥{MIN_TRADES_PER_DAY:.0f} trades/day</p>
</body>
</html>"""

    with open(DASHBOARD_FILE, "w") as f:
        f.write(html)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("Polymarket Copy Trading Bot — Fully Optimized")
    log.info("  Traders:      Top %d (win rate + PnL score)", TOP_TRADERS_COUNT)
    log.info("  Sizing:       Dynamic ($4–$8 by win rate, x%.0f on consensus)", CONSENSUS_MULTIPLIER)
    log.info("  Stop loss:    $%.2f", STOP_LOSS_BALANCE)
    log.info("  Poll:         every %ds", POLL_INTERVAL_SEC)
    log.info("  Spread max:   %.0f¢", MAX_SPREAD * 100)
    log.info("  Volume min:   $%.0f", MIN_VOLUME_USD)
    log.info("  Expiry min:   %.0fh remaining", MIN_EXPIRY_HOURS)
    log.info("=" * 60)

    traders: list        = []
    last_refresh         = 0.0
    seen_trades: set     = set()
    # Store each trader's win_rate and bet_size keyed by address
    trader_info: dict    = {}
    buys_executed        = 0
    sells_executed       = 0

    while True:
        now = time.time()

        # ── 1. Refresh trader list hourly ─────────────────────────────────────
        if now - last_refresh >= LEADERBOARD_REFRESH:
            log.info("Refreshing trader list...")
            new_traders = get_top_traders(TOP_TRADERS_COUNT)
            if new_traders:
                traders      = new_traders
                trader_info  = {
                    t["address"].lower(): {
                        "name":     t.get("name", t["address"][:10]),
                        "win_rate": t.get("win_rate") or 0.5,
                        "bet_size": t.get("_bet_size", 5.0),
                    }
                    for t in traders if t.get("address")
                }
                last_refresh = now
            elif not traders:
                log.warning("Could not load traders — retrying in %ds", POLL_INTERVAL_SEC)
                time.sleep(POLL_INTERVAL_SEC)
                continue

        # ── 2. Auto-refresh token if expiring ────────────────────────────────
        refresh_token_if_needed()

        # ── 3. Check balance ──────────────────────────────────────────────────
        balance = get_balance()
        log.info("Balance: $%.2f  |  Buys: %d  Sells: %d", balance, buys_executed, sells_executed)

        if balance == -1.0:
            log.warning("Balance unavailable — skipping cycle (check auth).")
            time.sleep(POLL_INTERVAL_SEC)
            continue

        if balance < STOP_LOSS_BALANCE:
            log.warning("STOP LOSS — $%.2f below $%.2f. Paused 5 min.", balance, STOP_LOSS_BALANCE)
            time.sleep(300)
            continue

        # ── 4. Auto-redeem resolved winning positions ─────────────────────────
        auto_redeem()

        # ── 5. Collect fresh trades ───────────────────────────────────────────
        my_positions = get_my_positions()
        consensus_buys: Counter = Counter()
        fresh_buys:  list = []
        fresh_sells: list = []

        for trader in traders:
            address  = trader.get("address", "")
            info     = trader_info.get(address.lower(), {})
            username = info.get("name", address[:10])

            if not address:
                continue

            for trade in get_recent_activity(address, "buy"):
                tx = trade.get("transaction_hash", "")
                if not tx or tx in seen_trades:
                    continue
                if not is_fresh(trade):
                    seen_trades.add(tx)
                    continue
                slug    = trade.get("slug", "")
                outcome = trade.get("outcome", "")
                if slug and outcome:
                    key = f"{slug}|{outcome}"
                    consensus_buys[key] += 1
                    fresh_buys.append((slug, outcome, username, tx, address))

            for trade in get_recent_activity(address, "sell"):
                tx = trade.get("transaction_hash", "")
                if not tx or tx in seen_trades:
                    continue
                if not is_fresh(trade):
                    seen_trades.add(tx)
                    continue
                slug    = trade.get("slug", "")
                outcome = trade.get("outcome", "")
                if slug and outcome:
                    fresh_sells.append((slug, outcome, username, tx))

        # ── 4. Execute buys ───────────────────────────────────────────────────
        executed_keys: set = set()

        for slug, outcome, trader_name, tx, address in fresh_buys:
            if tx in seen_trades:
                continue

            key = f"{slug}|{outcome}"
            if key in executed_keys:
                seen_trades.add(tx)
                continue

            # Run all market filters
            if not market_ok(slug, outcome):
                seen_trades.add(tx)
                continue

            # Size the bet
            info      = trader_info.get(address.lower(), {})
            base_size = info.get("bet_size", 5.0)
            agree     = consensus_buys[key]
            amount    = base_size * CONSENSUS_MULTIPLIER if agree >= 2 else base_size

            if agree >= 2:
                log.info("CONSENSUS (%d traders) %s | %s → $%.2f", agree, slug, outcome, amount)
            else:
                log.info("BUY signal from %s: %s | %s → $%.2f", trader_name, slug, outcome, amount)

            if balance - amount < STOP_LOSS_BALANCE:
                log.warning("Skip — balance too low ($%.2f)", balance)
                seen_trades.add(tx)
                continue

            if execute_buy(slug, outcome, amount, trader_name):
                buys_executed += 1
                balance -= amount
                executed_keys.add(key)

            seen_trades.add(tx)
            time.sleep(1)

        # ── 5. Execute sells ──────────────────────────────────────────────────
        for slug, outcome, trader_name, tx in fresh_sells:
            if tx in seen_trades:
                continue

            pos_key   = f"{slug}|{outcome}"
            my_shares = my_positions.get(pos_key, 0)

            if my_shares <= 0:
                seen_trades.add(tx)
                continue

            log.info("EXIT signal from %s: %s | %s → sell %.4f shares",
                     trader_name, slug, outcome, my_shares)

            if execute_sell(slug, outcome, my_shares, trader_name):
                sells_executed += 1
                my_positions.pop(pos_key, None)

            seen_trades.add(tx)
            time.sleep(1)

        # ── 6. Write dashboard ────────────────────────────────────────────────
        write_dashboard(balance, traders, buys_executed, sells_executed)

        # ── 7. Sleep ──────────────────────────────────────────────────────────
        log.info("Next check in %ds...\n", POLL_INTERVAL_SEC)
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
