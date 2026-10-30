"""
Scans Kraken USD pairs for RSI below a threshold and sends a push alert.
Uses only Kraken PUBLIC endpoints: no API key needed.

Config via environment variables (all optional):
  INTERVAL_MIN   candle size in minutes: 60, 240, 1440 (default 60)
  RSI_THRESHOLD  alert when RSI is below this (default 30)
  RSI_PERIOD     RSI period (default 14)
  MIN_USD_VOL    skip pairs with less than this 24h USD volume (default 10000000)
  MAX_SPREAD_PCT skip pairs whose bid/ask spread is wider than this % (default 0.10)
  NTFY_TOPIC     ntfy.sh topic name (long and random, treat as a password)
  TG_TOKEN, TG_CHAT_ID   Telegram bot token and chat id (alternative to ntfy)
"""
import os
import sys
import time

import requests

API = "https://api.kraken.com/0/public"
INTERVAL = int(os.getenv("INTERVAL_MIN", "60"))
THRESHOLD = float(os.getenv("RSI_THRESHOLD", "30"))
PERIOD = int(os.getenv("RSI_PERIOD", "14"))
MIN_USD_VOL = float(os.getenv("MIN_USD_VOL", "10000000"))
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "0.10"))
NTFY_TOPIC = os.getenv("NTFY_TOPIC")
TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

STABLES = {"USDT", "USDC", "DAI", "PYUSD", "EURT", "TUSD", "USDG", "RLUSD"}


def get(endpoint, params=None, retries=4):
    for attempt in range(retries):
        try:
            r = requests.get(f"{API}/{endpoint}", params=params, timeout=20)
            data = r.json()
        except (requests.RequestException, ValueError):
            time.sleep(2 * (attempt + 1))
            continue
        errors = data.get("error", [])
        if errors:
            if any("Rate limit" in e or "Too many" in e for e in errors):
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"Kraken error on {endpoint}: {errors}")
        return data["result"]
    raise RuntimeError(f"Failed to fetch {endpoint} after {retries} attempts")


def rsi(closes, period=PERIOD):
    """Wilder's RSI. Returns None if not enough data."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def liquid_usd_pairs():
    pairs = get("AssetPairs")
    usd = {}
    for key, info in pairs.items():
        if info.get("quote") not in ("ZUSD", "USD"):
            continue
        if info.get("status") != "online":
            continue
        if ".d" in key:  # dark pool pairs
            continue
        base = info.get("wsname", "").split("/")[0]
        if base in STABLES:
            continue
        usd[key] = info.get("wsname", key)

    tickers = get("Ticker")
    selected = {}
    for key, name in usd.items():
        t = tickers.get(key)
        if not t:
            continue
        try:
            usd_vol = float(t["v"][1]) * float(t["p"][1])  # 24h base vol * 24h vwap
            bid = float(t["b"][0])
            ask = float(t["a"][0])
        except (KeyError, ValueError, IndexError):
            continue
        if bid <= 0 or ask <= 0:
            continue
        spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
        # Slippage proxy: keep only high-volume pairs with a tight spread.
        if usd_vol >= MIN_USD_VOL and spread_pct <= MAX_SPREAD_PCT:
            selected[key] = (name, usd_vol)
    return selected


def notify(title, body):
    sent = False
    if NTFY_TOPIC:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={"Title": title},
            timeout=20,
        )
        sent = True
    if TG_TOKEN and TG_CHAT_ID:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT_ID, "text": f"{title}\n{body}"},
            timeout=20,
        )
        sent = True
    if not sent:
        print("No notification channel configured; printing only.")


def main():
    pairs = liquid_usd_pairs()
    print(f"Scanning {len(pairs)} pairs, interval {INTERVAL}m, RSI<{THRESHOLD}")
    print("Pairs: " + ", ".join(sorted(n for n, _ in pairs.values())))
    hits = []
    for key, (name, usd_vol) in pairs.items():
        try:
            result = get("OHLC", {"pair": key, "interval": INTERVAL})
        except RuntimeError as e:
            print(f"skip {name}: {e}")
            continue
        series = next((v for k, v in result.items() if k != "last"), None)
        if not series:
            continue
        # Drop the last candle: it is still forming.
        closes = [float(c[4]) for c in series[:-1]]
        value = rsi(closes)
        if value is not None and value < THRESHOLD:
            hits.append((value, name, closes[-1]))
        time.sleep(1.1)  # stay under Kraken's public rate limit

    hits.sort()
    if not hits:
        print("No pairs below threshold.")
        return
    lines = [f"{name}: RSI {v:.1f}, last close {price:g}" for v, name, price in hits]
    body = "\n".join(lines)
    print(body)
    notify(f"RSI < {THRESHOLD:g} on Kraken ({INTERVAL}m)", body)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Fatal: {e}", file=sys.stderr)
        sys.exit(1)
