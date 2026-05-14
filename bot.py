# -*- coding: utf-8 -*-
"""
RFD TradingView Logic Telegram Bot
Exchanges: OKX, Gate.io, Bybit
CoinMarketCap: optional symbol filter/watchlist

Strategy logic converted from PineScript:
// Long  = Daily close diff > threshold AND RSI > 30
// Short = Daily close diff < -threshold AND RSI < 70

Runs continuously and sends Telegram alerts.
"""

import os
import time
import json
import math
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests
import pandas as pd
from flask import Flask, jsonify

# =========================
# Basic Config
# =========================

BOT_NAME = os.getenv("BOT_NAME", "RFD TradingView Logic Bot")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

CMC_API_KEY = os.getenv("CMC_API_KEY", "").strip()
USE_CMC = os.getenv("USE_CMC", "true").lower() in ("1", "true", "yes", "y")

EXCHANGES = [
    x.strip().lower()
    for x in os.getenv("EXCHANGES", "okx,gate,bybit").split(",")
    if x.strip()
]

# Main scan timeframe. Your indicator uses daily comparison + RSI.
# RSI here is calculated on this timeframe.
TIMEFRAME = os.getenv("TIMEFRAME", "4h").lower()

# Daily comparison threshold from TradingView code.
# Pine default is 0, meaning any positive daily diff = buying.
THRESHOLD = float(os.getenv("THRESHOLD", "0"))

RSI_LENGTH = int(os.getenv("RSI_LENGTH", "14"))
LONG_RSI_MIN = float(os.getenv("LONG_RSI_MIN", "30"))
SHORT_RSI_MAX = float(os.getenv("SHORT_RSI_MAX", "70"))

STOP_LOSS_PERCENT = float(os.getenv("STOP_LOSS_PERCENT", "20"))
TAKE_PROFIT_PERCENT = float(os.getenv("TAKE_PROFIT_PERCENT", "40"))

ENABLE_LONG = os.getenv("ENABLE_LONG", "true").lower() in ("1", "true", "yes", "y")
ENABLE_SHORT = os.getenv("ENABLE_SHORT", "true").lower() in ("1", "true", "yes", "y")

SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "900"))
KLINE_LIMIT = int(os.getenv("KLINE_LIMIT", "200"))

# CMC watchlist settings
CMC_LIMIT = int(os.getenv("CMC_LIMIT", "300"))
CMC_MIN_MARKET_CAP = float(os.getenv("CMC_MIN_MARKET_CAP", "0"))
CMC_MAX_RANK = int(os.getenv("CMC_MAX_RANK", "1000"))

# Optional manual symbols: BTC,ETH,SOL
MANUAL_SYMBOLS = [
    s.strip().upper().replace("USDT", "")
    for s in os.getenv("SYMBOLS", "").split(",")
    if s.strip()
]

# Prevent repeated alerts for same symbol/direction/candle
ALERTS_FILE = os.getenv("ALERTS_FILE", "sent_alerts.json")

# Flask health server for Railway
PORT = int(os.getenv("PORT", "8080"))
app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s — %(levelname)s — %(message)s"
)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 RFD-TV-Telegram-Bot/1.0"
})


# =========================
# Helpers
# =========================

def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def safe_float(x) -> Optional[float]:
    try:
        if x is None:
            return None
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except Exception:
        return None


def load_sent_alerts() -> Dict[str, str]:
    if not os.path.exists(ALERTS_FILE):
        return {}
    try:
        with open(ALERTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_sent_alerts(data: Dict[str, str]) -> None:
    try:
        # keep file small
        if len(data) > 5000:
            keys = list(data.keys())[-3000:]
            data = {k: data[k] for k in keys}
        with open(ALERTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"Could not save alerts file: {e}")


def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram variables missing. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }

    try:
        r = SESSION.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            logging.warning(f"Telegram failed: {r.status_code} {r.text[:300]}")
            return False
        return True
    except Exception as e:
        logging.warning(f"Telegram exception: {e}")
        return False


def timeframe_to_okx_bar(tf: str) -> str:
    mapping = {
        "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H",
        "1d": "1D", "d": "1D"
    }
    return mapping.get(tf, "4H")


def timeframe_to_gate_interval(tf: str) -> str:
    mapping = {
        "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1h": "1h", "4h": "4h", "8h": "8h", "1d": "1d", "d": "1d"
    }
    return mapping.get(tf, "4h")


def timeframe_to_bybit_interval(tf: str) -> str:
    mapping = {
        "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
        "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
        "1d": "D", "d": "D"
    }
    return mapping.get(tf, "240")


# =========================
# CoinMarketCap Watchlist
# =========================

def get_cmc_symbols() -> List[str]:
    """
    Gets top symbols from CoinMarketCap.
    Used only as a watchlist/filter, not candle source.
    """
    if not USE_CMC or not CMC_API_KEY:
        return []

    url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"
    headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY}
    params = {
        "start": 1,
        "limit": CMC_LIMIT,
        "convert": "USD",
        "sort": "market_cap"
    }

    try:
        r = SESSION.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json().get("data", [])
        symbols = []
        for item in data:
            symbol = str(item.get("symbol", "")).upper().strip()
            rank = int(item.get("cmc_rank") or 999999)
            quote = item.get("quote", {}).get("USD", {})
            market_cap = float(quote.get("market_cap") or 0)

            if not symbol:
                continue
            if rank > CMC_MAX_RANK:
                continue
            if market_cap < CMC_MIN_MARKET_CAP:
                continue
            symbols.append(symbol)

        logging.info(f"CMC symbols loaded: {len(symbols)}")
        return symbols

    except Exception as e:
        logging.warning(f"CMC failed: {e}")
        return []


# =========================
# Exchange Pairs
# =========================

def get_okx_pairs() -> Dict[str, str]:
    url = "https://www.okx.com/api/v5/public/instruments"
    params = {"instType": "SPOT"}
    out = {}
    try:
        r = SESSION.get(url, params=params, timeout=30)
        r.raise_for_status()
        for item in r.json().get("data", []):
            if item.get("quoteCcy") == "USDT" and item.get("state") == "live":
                base = item.get("baseCcy", "").upper()
                inst_id = item.get("instId", "")
                if base and inst_id:
                    out[base] = inst_id
        logging.info(f"OKX Spot USDT pairs loaded: {len(out)}")
    except Exception as e:
        logging.warning(f"OKX pairs failed: {e}")
    return out


def get_gate_pairs() -> Dict[str, str]:
    url = "https://api.gateio.ws/api/v4/spot/currency_pairs"
    out = {}
    try:
        r = SESSION.get(url, timeout=30)
        r.raise_for_status()
        for item in r.json():
            pair_id = item.get("id", "")
            trade_status = item.get("trade_status", "")
            if pair_id.endswith("_USDT") and trade_status == "tradable":
                base = pair_id.replace("_USDT", "").upper()
                out[base] = pair_id
        logging.info(f"Gate Spot USDT pairs loaded: {len(out)}")
    except Exception as e:
        logging.warning(f"Gate pairs failed: {e}")
    return out


def get_bybit_pairs() -> Dict[str, str]:
    url = "https://api.bybit.com/v5/market/instruments-info"
    params = {"category": "spot"}
    out = {}
    try:
        cursor = None
        while True:
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            r = SESSION.get(url, params=p, timeout=30)
            r.raise_for_status()
            result = r.json().get("result", {})
            for item in result.get("list", []):
                symbol = item.get("symbol", "")
                status = item.get("status", "")
                quote = item.get("quoteCoin", "")
                base = item.get("baseCoin", "")
                if quote == "USDT" and status == "Trading" and base and symbol:
                    out[base.upper()] = symbol
            cursor = result.get("nextPageCursor")
            if not cursor:
                break
        logging.info(f"Bybit Spot USDT pairs loaded: {len(out)}")
    except Exception as e:
        logging.warning(f"Bybit pairs failed: {e}")
    return out


# =========================
# Klines
# =========================

def normalize_ohlcv(rows: List[List], source: str) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["ts"] = pd.to_numeric(df["ts"], errors="coerce")
    df = df.dropna(subset=["ts", "open", "high", "low", "close"])
    df = df.sort_values("ts").reset_index(drop=True)
    df["source"] = source
    return df


def fetch_okx_klines(inst_id: str, tf: str, limit: int) -> pd.DataFrame:
    url = "https://www.okx.com/api/v5/market/candles"
    params = {
        "instId": inst_id,
        "bar": timeframe_to_okx_bar(tf),
        "limit": str(limit)
    }
    r = SESSION.get(url, params=params, timeout=20)
    r.raise_for_status()
    raw = r.json().get("data", [])

    rows = []
    for x in raw:
        # OKX: [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]
        rows.append([x[0], x[1], x[2], x[3], x[4], x[5]])

    return normalize_ohlcv(rows, "OKX")


def fetch_gate_klines(pair_id: str, tf: str, limit: int) -> pd.DataFrame:
    url = "https://api.gateio.ws/api/v4/spot/candlesticks"
    params = {
        "currency_pair": pair_id,
        "interval": timeframe_to_gate_interval(tf),
        "limit": limit
    }
    r = SESSION.get(url, params=params, timeout=20)
    r.raise_for_status()
    raw = r.json()

    rows = []
    for x in raw:
        # Gate spot candlestick commonly: [timestamp, volume, close, high, low, open]
        rows.append([int(x[0]) * 1000, x[5], x[3], x[4], x[2], x[1]])

    return normalize_ohlcv(rows, "Gate")


def fetch_bybit_klines(symbol: str, tf: str, limit: int) -> pd.DataFrame:
    url = "https://api.bybit.com/v5/market/kline"
    params = {
        "category": "spot",
        "symbol": symbol,
        "interval": timeframe_to_bybit_interval(tf),
        "limit": limit
    }
    r = SESSION.get(url, params=params, timeout=20)
    r.raise_for_status()
    raw = r.json().get("result", {}).get("list", [])

    rows = []
    for x in raw:
        # Bybit: [startTime, openPrice, highPrice, lowPrice, closePrice, volume, turnover]
        rows.append([x[0], x[1], x[2], x[3], x[4], x[5]])

    return normalize_ohlcv(rows, "Bybit")


# =========================
# Daily close comparison
# =========================

def fetch_daily_close_diff(exchange: str, pair: str) -> Optional[float]:
    """
    Replicates TradingView getDiff():
    yesterday = security(tickerid, 'D', close[1])
    today = security(tickerid, 'D', close)
    percentage = (today - yesterday) / yesterday
    """
    try:
        if exchange == "okx":
            df = fetch_okx_klines(pair, "1d", 5)
        elif exchange == "gate":
            df = fetch_gate_klines(pair, "1d", 5)
        elif exchange == "bybit":
            df = fetch_bybit_klines(pair, "1d", 5)
        else:
            return None

        if len(df) < 2:
            return None

        yesterday = safe_float(df["close"].iloc[-2])
        today = safe_float(df["close"].iloc[-1])
        if yesterday is None or today is None or yesterday == 0:
            return None

        return (today - yesterday) / yesterday

    except Exception as e:
        logging.debug(f"{exchange} daily diff failed for {pair}: {e}")
        return None


# =========================
# Indicators
# =========================

def calc_rsi(close: pd.Series, length: int = 14) -> pd.Series:
    """
    RSI using Wilder smoothing, close to common TradingView RSI behavior.
    """
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    return rsi.astype(float)


def analyze_symbol(exchange: str, symbol: str, pair: str) -> Optional[Dict]:
    try:
        if exchange == "okx":
            df = fetch_okx_klines(pair, TIMEFRAME, KLINE_LIMIT)
            exchange_name = "OKX"
        elif exchange == "gate":
            df = fetch_gate_klines(pair, TIMEFRAME, KLINE_LIMIT)
            exchange_name = "Gate"
        elif exchange == "bybit":
            df = fetch_bybit_klines(pair, TIMEFRAME, KLINE_LIMIT)
            exchange_name = "Bybit"
        else:
            return None

        if df.empty or len(df) < RSI_LENGTH + 5:
            return None

        df["rsi"] = calc_rsi(df["close"], RSI_LENGTH)
        last = df.iloc[-1]
        price = safe_float(last["close"])
        rsi = safe_float(last["rsi"])
        candle_ts = int(last["ts"])

        if price is None or rsi is None:
            return None

        close_diff = fetch_daily_close_diff(exchange, pair)
        if close_diff is None:
            return None

        buying = close_diff > THRESHOLD
        selling = close_diff < -THRESHOLD

        long_condition = ENABLE_LONG and buying and rsi > LONG_RSI_MIN
        short_condition = ENABLE_SHORT and selling and rsi < SHORT_RSI_MAX

        if not long_condition and not short_condition:
            return None

        direction = "LONG" if long_condition else "SHORT"

        if direction == "LONG":
            stop = price * (1 - STOP_LOSS_PERCENT / 100)
            take = price * (1 + TAKE_PROFIT_PERCENT / 100)
        else:
            stop = price * (1 + STOP_LOSS_PERCENT / 100)
            take = price * (1 - TAKE_PROFIT_PERCENT / 100)

        return {
            "exchange": exchange_name,
            "exchange_key": exchange,
            "symbol": symbol,
            "pair": pair,
            "direction": direction,
            "price": price,
            "rsi": rsi,
            "close_diff": close_diff,
            "stop": stop,
            "take": take,
            "timeframe": TIMEFRAME,
            "candle_ts": candle_ts
        }

    except requests.HTTPError as e:
        logging.debug(f"{exchange} HTTP failed for {symbol}: {e}")
        return None
    except Exception as e:
        logging.debug(f"{exchange} analyze failed for {symbol}: {e}")
        return None


def format_signal_message(sig: Dict) -> str:
    direction_ar = "شراء / LONG" if sig["direction"] == "LONG" else "بيع / SHORT"
    emoji = "🟢" if sig["direction"] == "LONG" else "🔴"
    diff_pct = sig["close_diff"] * 100

    return (
        f"{emoji} *تنبيه مؤشر أبو علاوي*\n\n"
        f"*العملة:* `{sig['symbol']}USDT`\n"
        f"*المنصة:* `{sig['exchange']}`\n"
        f"*الاتجاه:* *{direction_ar}*\n"
        f"*الفريم:* `{sig['timeframe']}`\n\n"
        f"*السعر الحالي:* `{sig['price']:.8f}`\n"
        f"*RSI:* `{sig['rsi']:.2f}`\n"
        f"*فرق إغلاق اليوم عن أمس:* `{diff_pct:.2f}%`\n\n"
        f"*وقف الخسارة التقريبي:* `{sig['stop']:.8f}`\n"
        f"*جني الربح التقريبي:* `{sig['take']:.8f}`\n\n"
        f"الشروط:\n"
        f"• Daily Diff {'>' if sig['direction']=='LONG' else '<'} Threshold\n"
        f"• RSI {'>' if sig['direction']=='LONG' else '<'} "
        f"{LONG_RSI_MIN if sig['direction']=='LONG' else SHORT_RSI_MAX}\n\n"
        f"`{now_utc()}`"
    )


# =========================
# Scanner
# =========================

def build_watchlists() -> Dict[str, Dict[str, str]]:
    cmc_symbols = set(get_cmc_symbols())
    manual_symbols = set(MANUAL_SYMBOLS)

    watchlists: Dict[str, Dict[str, str]] = {}

    if "okx" in EXCHANGES:
        pairs = get_okx_pairs()
        watchlists["okx"] = filter_pairs(pairs, cmc_symbols, manual_symbols)
        logging.info(f"Final OKX watchlist: {len(watchlists['okx'])}")

    if "gate" in EXCHANGES:
        pairs = get_gate_pairs()
        watchlists["gate"] = filter_pairs(pairs, cmc_symbols, manual_symbols)
        logging.info(f"Final Gate watchlist: {len(watchlists['gate'])}")

    if "bybit" in EXCHANGES:
        pairs = get_bybit_pairs()
        watchlists["bybit"] = filter_pairs(pairs, cmc_symbols, manual_symbols)
        logging.info(f"Final Bybit watchlist: {len(watchlists['bybit'])}")

    return watchlists


def filter_pairs(pairs: Dict[str, str], cmc_symbols: set, manual_symbols: set) -> Dict[str, str]:
    if manual_symbols:
        return {sym: pair for sym, pair in pairs.items() if sym in manual_symbols}

    if USE_CMC and cmc_symbols:
        return {sym: pair for sym, pair in pairs.items() if sym in cmc_symbols}

    return pairs


def scan_once(watchlists: Dict[str, Dict[str, str]]) -> int:
    sent_alerts = load_sent_alerts()
    total_signals = 0

    for exchange, pairs in watchlists.items():
        items = list(pairs.items())
        logging.info(f"Scanning {exchange.upper()} symbols: {len(items)}")

        for i, (symbol, pair) in enumerate(items, start=1):
            logging.info(f"[{i}/{len(items)}] تحليل {TIMEFRAME} {symbol} على {exchange.upper()}")

            sig = analyze_symbol(exchange, symbol, pair)
            if not sig:
                continue

            alert_key = f"{sig['exchange_key']}:{sig['symbol']}:{sig['direction']}:{sig['timeframe']}:{sig['candle_ts']}"
            if alert_key in sent_alerts:
                continue

            msg = format_signal_message(sig)
            ok = send_telegram(msg)

            if ok:
                sent_alerts[alert_key] = now_utc()
                save_sent_alerts(sent_alerts)
                total_signals += 1
                logging.info(f"Signal sent: {sig['exchange']} {sig['symbol']} {sig['direction']}")
            else:
                logging.warning(f"Signal not sent: {sig}")

            time.sleep(0.25)

    return total_signals


def main_loop():
    logging.info(f"{BOT_NAME} Started")
    send_telegram(f"✅ *{BOT_NAME} Started*\n\nالفريم: `{TIMEFRAME}`\nالمنصات: `{', '.join(EXCHANGES)}`\n`{now_utc()}`")

    watchlists = build_watchlists()

    while True:
        try:
            signals = scan_once(watchlists)
            logging.info(f"Scan done. Signals sent: {signals}. Sleeping {SCAN_INTERVAL_SECONDS}s")
            time.sleep(SCAN_INTERVAL_SECONDS)

            # Refresh watchlists after every cycle in case pairs change
            watchlists = build_watchlists()

        except KeyboardInterrupt:
            logging.info("Bot stopped manually.")
            break
        except Exception as e:
            logging.exception(f"Main loop error: {e}")
            send_telegram(f"⚠️ *Bot Error*\n`{str(e)[:500]}`")
            time.sleep(60)


# =========================
# Flask Health Endpoints
# =========================

@app.route("/")
def home():
    return jsonify({
        "ok": True,
        "bot": BOT_NAME,
        "timeframe": TIMEFRAME,
        "exchanges": EXCHANGES,
        "use_cmc": USE_CMC,
        "time": now_utc()
    })


@app.route("/health")
def health():
    return jsonify({"ok": True, "time": now_utc()})


if __name__ == "__main__":
    # Run Flask server in background for Railway health check
    import threading

    t = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False),
        daemon=True
    )
    t.start()

    main_loop()
