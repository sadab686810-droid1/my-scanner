
import os
import json
import time
import requests
from datetime import datetime, time as clock_time
from zoneinfo import ZoneInfo

import pyotp
from SmartApi import SmartConnect

# ==========================================================
# NSE F&O ALL-STOCK 5-MINUTE BREAKOUT SCANNER
# Telegram alerts only - no automatic orders
# ==========================================================

IST = ZoneInfo("Asia/Kolkata")

MASTER_URL = (
    "https://margincalculator.angelone.in/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)

STATE_FILE = "scanner_state.json"

BODY_MIN_PERCENT = 75.0
REQUEST_DELAY = 0.55

API_KEY = os.environ["ANGEL_API_KEY"]
CLIENT_ID = os.environ["ANGEL_CLIENT_ID"]
PASSWORD = os.environ["ANGEL_PASSWORD"]
TOTP_VALUE = os.environ["ANGEL_TOTP"]

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def log(message):
    print(
        f"[{datetime.now(IST):%H:%M:%S}] {message}",
        flush=True,
    )


def telegram(message):
    response = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data={"chat_id": TG_CHAT_ID, "text": message},
        timeout=20,
    )
    response.raise_for_status()

    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram error: {result}")


def login():
    value = TOTP_VALUE.strip()

    if not (len(value) == 6 and value.isdigit()):
        value = pyotp.TOTP(value).now()

    smart = SmartConnect(api_key=API_KEY)

    result = smart.generateSession(
        CLIENT_ID, PASSWORD, value
    )

    if not result or not result.get("status"):
        raise RuntimeError(f"Angel login failed: {result}")

    log("Angel One login successful")
    return smart


def load_stocks():
    log("Downloading NSE F&O instrument master")

    response = requests.get(MASTER_URL, timeout=90)
    response.raise_for_status()
    master = response.json()

    fno_names = {
        str(item.get("name", "")).strip().upper()
        for item in master
        if item.get("exch_seg") == "NFO"
        and item.get("instrumenttype") == "FUTSTK"
        and item.get("name")
    }

    stocks = {}

    for item in master:
        if item.get("exch_seg") != "NSE":
            continue

        symbol = str(item.get("symbol", "")).strip()
        token = str(item.get("token", "")).strip()

        if not symbol.endswith("-EQ") or not token:
            continue

        name = symbol[:-3].upper()

        if name in fno_names:
            stocks[name] = {
                "symbol": symbol,
                "token": token,
            }

    if not stocks:
        raise RuntimeError(
            "No F&O stocks mapped. Check instrument master fields."
        )

    log(f"Mapped F&O stocks: {len(stocks)}")
    return stocks


def load_state(today):
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)

        if state.get("date") == today:
            state.setdefault("last_processed", {})
            state.setdefault("opening_high", {})
            state.setdefault("invalid_open", [])
            return state

    except (OSError, json.JSONDecodeError):
        pass

    return {
        "date": today,
        "last_processed": {},
        "opening_high": {},
        "invalid_open": [],
    }


def save_state(state):
    temp = STATE_FILE + ".tmp"

    with open(temp, "w", encoding="utf-8") as file:
        json.dump(state, file)

    os.replace(temp, STATE_FILE)


def get_candles(smart, token, today, now):
    result = smart.getCandleData({
        "exchange": "NSE",
        "symboltoken": str(token),
        "interval": "FIVE_MINUTE",
        "fromdate": f"{today} 09:15",
        "todate": now.strftime("%Y-%m-%d %H:%M"),
    })

    if not result or not result.get("status"):
        message = (
            result.get("message", "Unknown error")
            if result else "Empty response"
        )
        log(f"Candle API failed for {token}: {message}")
        return None

    return result.get("data") or []


def candle_start(candle):
    return datetime.fromisoformat(
        str(candle[0])
    ).astimezone(IST)


def is_complete(candle, now):
    try:
        return (
            candle_start(candle).timestamp() + 300
            <= now.timestamp()
        )
    except (ValueError, TypeError, IndexError):
        return False


def valid_opening_candle(candle):
    try:
        open_price = float(candle[1])
        high = float(candle[2])
        low = float(candle[3])
        close = float(candle[4])

        total_range = high - low
        if total_range <= 0:
            return False

        body_percent = (
            abs(close - open_price) / total_range * 100
        )

        return (
            close > open_price
            and body_percent >= BODY_MIN_PERCENT
        )

    except (ValueError, TypeError, IndexError):
        return False


def scan_stock(smart, name, stock, state, today, now):
    symbol = stock["symbol"]

    try:
        candles = get_candles(
            smart, stock["token"], today, now
        )

        if candles is None:
            return

        candles = [
            c for c in candles
            if is_complete(c, now)
        ]
        candles.sort(key=lambda c: c[0])

        # Wait until the first 5-minute candle is complete.
        if not candles:
            return

        opening = next(
            (
                c for c in candles
                if candle_start(c).strftime("%H:%M") == "09:15"
            ),
            None,
        )

        if opening is None:
            return

        if name in state["invalid_open"]:
            return

        if name not in state["opening_high"]:
            if not valid_opening_candle(opening):
                state["invalid_open"].append(name)
                state["last_processed"][name] = candles[-1][0]
                return

            state["opening_high"][name] = float(opening[2])

            log(
                f"{symbol}: opening high locked at "
                f"{state['opening_high'][name]:.2f}"
            )

        opening_high = float(state["opening_high"][name])
        last_done = state["last_processed"].get(name)

        for index, candle in enumerate(candles):
            candle_id = candle[0]

            if candle_id == opening[0]:
                continue

            if last_done and candle_id <= last_done:
                continue

            # A prior candle is required for volume comparison.
            if index == 0:
                continue

            previous = candles[index - 1]

            close = float(candle[4])
            volume = float(candle[5])
            previous_volume = float(previous[5])

            if (
                close > opening_high
                and volume > previous_volume
            ):
                candle_time = candle_start(candle)

                message = (
                    "🟢 NSE F&O BREAKOUT BUY\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"📌 STOCK: {symbol}\n"
                    f"⏰ TIME: {candle_time:%H:%M}\n"
                    f"🔒 OPENING HIGH: ₹{opening_high:.2f}\n"
                    f"🚀 CLOSE: ₹{close:.2f}\n"
                    f"📊 VOLUME: {volume:,.0f}\n"
                    f"📉 PREVIOUS VOLUME: "
                    f"{previous_volume:,.0f}\n"
                    "✅ Close above 09:15 high\n"
                    "✅ Volume greater than previous candle\n"
                    "⚠️ Alert only; no order placed."
                )

                telegram(message)
                log(f"BUY ALERT SENT: {symbol}")

        state["last_processed"][name] = candles[-1][0]

    except Exception as error:
        log(f"ERROR {symbol}: {error}")


def main():
    now = datetime.now(IST)

    if now.weekday() >= 5:
        log("Weekend; market scanner stopped")
        return

    if not (
        clock_time(9, 15) <= now.time()
        <= clock_time(15, 30)
    ):
        log("Outside market hours")
        return

    today = now.strftime("%Y-%m-%d")
    state = load_state(today)

    smart = login()
    stocks = load_stocks()

    try:
        for number, (name, stock) in enumerate(
            stocks.items(), start=1
        ):
            scan_stock(
                smart, name, stock, state, today, now
            )

            # Persist progress during the scan.
            if number % 10 == 0:
                save_state(state)
                log(f"Progress: {number}/{len(stocks)}")

            time.sleep(REQUEST_DELAY)

        save_state(state)
        log("Scan completed successfully")

    finally:
        try:
            smart.terminateSession(CLIENT_ID)
        except Exception:
            pass


if __name__ == "__main__":
    main()
