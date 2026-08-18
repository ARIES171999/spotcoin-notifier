#!/usr/bin/env python3
"""
Dip Watcher — structural buy-signal notifier
==============================================
Checks configured coins for a *confluence* of dip signals (drawdown from
recent high, RSI oversold, price below moving average) and pushes a
notification to your phone via ntfy.sh when enough signals align AND
those conditions hold across two consecutive checks (to filter wicks —
see CONFIRMATION LOGIC below).

Runs entirely off your own machine: pair with the included GitHub
Actions workflow (.github/workflows/dip_check.yml) to run this on a
free timer in the cloud. Your phone doesn't run anything — it just
needs the ntfy app installed to receive the push.

SETUP
-----
1. pip install requests
2. Install the "ntfy" app on your phone (iOS/Android), open it, and
   subscribe to a topic name of your choosing (make it hard to guess —
   anyone who knows your topic name can see/send to it).
3. Set that topic name as the NTFY_TOPIC environment variable (or, for
   local testing only, edit NTFY_TOPIC_DEFAULT below).
4. Edit CONFIG below and watchlist.json to taste (watchlist.json is the one
   you'll actually edit day-to-day — see "ADDING COINS" below).
5. Test it locally: NTFY_TOPIC=your-topic python3 dip_watcher.py

ADDING COINS ON THE GO
------------------------
The watchlist lives in watchlist.json, not in this script, specifically so
you can add a coin from your phone: open the repo in the GitHub app, tap
watchlist.json, tap edit, add the CoinGecko id (not the ticker — e.g.
"chainlink", not "LINK") to the "coins" list, commit. The next scheduled
run picks it up automatically. To find a coin's CoinGecko id, search for
it on coingecko.com — the id is the slug in the coin's URL.
Adding coins doesn't affect existing ones: each coin's confirmation state
and cooldown are tracked independently.

CONFIRMATION LOGIC (avoiding wicks)
------------------------------------
A single brief spike below a threshold ("wick") can satisfy the dip
conditions for a moment and then reverse. To avoid alerting on noise,
a coin must show a qualifying score on two CONSECUTIVE scheduled runs
before it alerts. If the score drops back below threshold on the next
check, the pending signal is cleared and nothing is sent. Your alert
lag is roughly one run interval (e.g. ~4h) — a worthwhile trade for
not chasing wicks.

FUTURE: ETFs (VOO, VXUS, etc.)
-------------------------------
CoinGecko only covers crypto. To extend this to ETFs, swap the
fetch_price_history() function for one that pulls daily closes from a
free source like Stooq (https://stooq.com/db/h/) or the `yfinance`
Python package (`pip install yfinance`), keep everything else
(RSI/SMA/drawdown/scoring/confirmation/notify) identical, and add the
tickers to a second WATCHLIST.
"""

import json
import os
import time
from datetime import datetime, timezone

import requests

# ── CONFIG ───────────────────────────────────────────────────────────
NTFY_TOPIC_DEFAULT = "your-secret-topic-name-here"  # local-testing fallback only
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", NTFY_TOPIC_DEFAULT)
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

# Set by the GitHub Actions workflow to "workflow_dispatch" when you manually
# trigger a run (vs. "schedule" for the automatic timer runs). Used to send a
# one-off "watcher is alive" ping only when you're actively checking it.
RUN_TRIGGER = os.environ.get("RUN_TRIGGER", "manual")

# CoinGecko coin ids (not ticker symbols) — see https://api.coingecko.com/api/v3/coins/list
# Loaded from watchlist.json (kept separate from this script so you can add/remove
# coins from your phone via the GitHub app without touching any code).
WATCHLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")
WATCHLIST_DEFAULT = ["bitcoin", "ethereum", "solana"]

def load_watchlist() -> list[str]:
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE, "r") as f:
            data = json.load(f)
        coins = data.get("coins", [])
        if coins:
            return coins
    return WATCHLIST_DEFAULT

WATCHLIST = load_watchlist()

LOOKBACK_DAYS = 90          # how much history to pull
HIGH_LOOKBACK_DAYS = 30     # window for "recent high" used in drawdown calc
DRAWDOWN_THRESHOLD = 0.10   # 10% down from recent high counts as a condition
RSI_PERIOD = 14
RSI_OVERSOLD = 35           # RSI below this counts as a condition
SMA_PERIOD = 50             # price below this SMA counts as a condition

MIN_CONDITIONS_TO_ALERT = 2   # need at least this many of the 3 conditions true
COOLDOWN_HOURS = 24           # don't re-alert on the same coin within this window after firing

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dip_watcher_state.json")

# ── DATA ─────────────────────────────────────────────────────────────

def fetch_price_history(coin_id: str, days: int) -> list[float]:
    """Daily closing prices for a coin over the last `days` days, oldest first."""
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    params = {"vs_currency": "usd", "days": days, "interval": "daily"}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return [p[1] for p in data["prices"]]

# ── INDICATORS ───────────────────────────────────────────────────────

def compute_rsi(prices: list[float], period: int = 14):
    if len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    recent = deltas[-period:]
    gains = [d for d in recent if d > 0]
    losses = [-d for d in recent if d < 0]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def compute_sma(prices: list[float], period: int):
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period

def compute_drawdown(prices: list[float], high_lookback_days: int):
    """Returns (drawdown_fraction, recent_high). drawdown_fraction is positive when down."""
    window = prices[-high_lookback_days:] if len(prices) >= high_lookback_days else prices
    recent_high = max(window)
    current = prices[-1]
    drawdown = (recent_high - current) / recent_high
    return drawdown, recent_high

# ── EVALUATION ───────────────────────────────────────────────────────

def evaluate_coin(coin_id: str) -> dict:
    prices = fetch_price_history(coin_id, LOOKBACK_DAYS)
    current_price = prices[-1]

    drawdown, recent_high = compute_drawdown(prices, HIGH_LOOKBACK_DAYS)
    rsi = compute_rsi(prices, RSI_PERIOD)
    sma = compute_sma(prices, SMA_PERIOD)

    conditions = {
        "drawdown": drawdown >= DRAWDOWN_THRESHOLD,
        "rsi_oversold": (rsi is not None) and (rsi <= RSI_OVERSOLD),
        "below_sma": (sma is not None) and (current_price < sma),
    }
    score = sum(conditions.values())

    return {
        "coin_id": coin_id,
        "price": current_price,
        "recent_high": recent_high,
        "drawdown_pct": drawdown * 100,
        "rsi": rsi,
        "sma": sma,
        "conditions": conditions,
        "score": score,
    }

# ── STATE (cooldown + confirmation tracking) ────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def in_cooldown(coin_state: dict) -> bool:
    last = coin_state.get("last_alert")
    if not last:
        return False
    elapsed_hours = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600
    return elapsed_hours < COOLDOWN_HOURS

# ── NOTIFY ───────────────────────────────────────────────────────────

def send_notification(title: str, message: str) -> None:
    requests.post(
        NTFY_URL,
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": "high", "Tags": "chart_with_downwards_trend"},
        timeout=10,
    )

# ── MAIN ─────────────────────────────────────────────────────────────

def main():
    state = load_state()

    if RUN_TRIGGER == "workflow_dispatch":
        send_notification(
            "Dip Watcher: online",
            f"Manually triggered — watcher is running and checking: {', '.join(WATCHLIST)}",
        )
        print("Sent startup/test notification (manual trigger).")

    for coin_id in WATCHLIST:
        coin_state = state.get(coin_id, {"pending_since": None, "last_alert": None})

        try:
            result = evaluate_coin(coin_id)
        except Exception as e:
            print(f"[{coin_id}] error fetching/evaluating: {e}")
            continue

        met = [name for name, ok in result["conditions"].items() if ok]
        qualifies = result["score"] >= MIN_CONDITIONS_TO_ALERT
        print(
            f"[{coin_id}] price=${result['price']:.2f} "
            f"drawdown={result['drawdown_pct']:.1f}% "
            f"rsi={result['rsi']} score={result['score']} "
            f"conditions_met={met} pending={coin_state['pending_since'] is not None}"
        )

        if not qualifies:
            # Conditions didn't hold this run — clear any pending confirmation (it was a wick).
            coin_state["pending_since"] = None
            state[coin_id] = coin_state
            continue

        if in_cooldown(coin_state):
            print(f"  -> qualifies but in cooldown, skipping")
            state[coin_id] = coin_state
            continue

        if coin_state["pending_since"] is None:
            # First qualifying run — wait for confirmation on the next check.
            coin_state["pending_since"] = datetime.now(timezone.utc).isoformat()
            state[coin_id] = coin_state
            watch_title = f"👀 Watching: {coin_id.upper()} — possible dip forming"
            watch_message = (
                f"Coin: {coin_id.upper()}\n"
                f"Price: ${result['price']:.2f} "
                f"(down {result['drawdown_pct']:.1f}% from {HIGH_LOOKBACK_DAYS}d high)\n"
                f"Conditions met: {', '.join(met)}\n"
                f"Watching for confirmation on the next check — "
                f"no action needed yet."
            )
            send_notification(watch_title, watch_message)
            print(f"  -> conditions met, awaiting confirmation next run (watch-notice sent)")
            continue

        # Second consecutive qualifying run — confirmed, alert.
        title = f"🟢 BUY SIGNAL: {coin_id.upper()} ({result['score']}/3 confirmed)"
        message = (
            f"Coin to buy: {coin_id.upper()}\n"
            f"Price: ${result['price']:.2f}\n"
            f"Down {result['drawdown_pct']:.1f}% from {HIGH_LOOKBACK_DAYS}d high "
            f"(${result['recent_high']:.2f})\n"
            f"RSI({RSI_PERIOD}): {result['rsi']:.1f}\n"
            f"Conditions met: {', '.join(met)}\n"
            f"Held across 2 consecutive checks."
        )
        send_notification(title, message)
        coin_state["last_alert"] = datetime.now(timezone.utc).isoformat()
        coin_state["pending_since"] = None
        state[coin_id] = coin_state
        print(f"  -> CONFIRMED, notification sent for {coin_id}")

        time.sleep(1.5)  # be gentle with CoinGecko's free rate limit

    save_state(state)


if __name__ == "__main__":
    main()
