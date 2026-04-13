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
LOG_FILE             = os.path.join(os.path.dirname(__file__), "copy_trader.log")
TRADES_FILE          = os.path.join(os.path.dirname(__file__), "trades.json")

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
    """Combined score: 60% win rate + 40% log-scaled PnL."""
    win_rate  = t.get("win_rate") or 0.5
    pnl       = float(t.get("pnl") or 0)
    pnl_score = math.log10(max(pnl, 1)) / 10.0
    return 0.6 * win_rate + 0.4 * pnl_score


def get_top_traders(n: int = 10) -> list:
    data = run_with_auth([
        "bullpen", "polymarket", "data", "smart-money",
        "--type", "top_traders", "--output", "json",
    ])
    if not data:
        return []
    traders = [t for t in data.get("traders", []) if not t.get("is_bot")]
    for t in traders:
        t["_score"]    = score_trader(t)
        t["_bet_size"] = trade_size_for(t.get("win_rate") or 0.5)
    traders.sort(key=lambda x: x["_score"], reverse=True)
    top = traders[:n]
    log.info("Top %d traders (win rate + PnL score):", len(top))
    for t in top:
        wr  = (t.get("win_rate") or 0) * 100
        pnl = float(t.get("pnl") or 0)
        log.info("  %-25s  WR: %5.1f%%  PnL: $%.0f  Bet: $%.0f  Score: %.3f",
                 t.get("name", "?"), wr, pnl, t["_bet_size"], t["_score"])
    return top


def get_recent_activity(address: str, side: str, limit: int = 5) -> list:
    data = run_with_auth([
        "bullpen", "polymarket", "activity",
        "--address", address, "--type", "trade",
        "--side", side, "--limit", str(limit), "--output", "json",
    ])
    return data if data and isinstance(data, list) else []


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

        # ── 2. Check balance ──────────────────────────────────────────────────
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

        # ── 3. Collect fresh trades ───────────────────────────────────────────
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

        # ── 6. Sleep ──────────────────────────────────────────────────────────
        log.info("Next check in %ds...\n", POLL_INTERVAL_SEC)
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
