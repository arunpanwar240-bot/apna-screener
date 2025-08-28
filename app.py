from flask import Flask, render_template, request, redirect, url_for, flash
import pandas as pd
from dhanhq import DhanContext, dhanhq
from datetime import datetime, timedelta
import requests
import json
import os

app = Flask(__name__)
app.secret_key = "supersecretkey"  # required for flash messages

# ---------- Config Storage ----------
CONFIG_FILE = "config.json"

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {
        "client_id": "",
        "access_token": "",
        "telegram_bot_token": "",
        "telegram_chat_id": ""
    }

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

config = load_config()

# ---------- Initialize Dhan & Telegram ----------
def get_dhan():
    if config["client_id"] and config["access_token"]:
        dhan_context = DhanContext(client_id=config["client_id"], access_token=config["access_token"])
        return dhanhq(dhan_context)
    return None

def send_telegram_message(message):
    if not config["telegram_bot_token"] or not config["telegram_chat_id"]:
        print("⚠ Telegram not configured")
        return
    url = f"https://api.telegram.org/bot{config['telegram_bot_token']}/sendMessage"
    payload = {"chat_id": config["telegram_chat_id"], "text": message}
    try:
        requests.post(url, data=payload)
    except Exception as e:
        print("❌ Telegram Error:", e)

# ---------- Constants ----------
TIMEFRAME_MAP = {
    "1min": 1, "5min": 5, "15min": 15,
    "1h": 60, "1d": "1D", "1w": "1W", "1M": "1M"
}
INDEX_IDS = {"NIFTY": "13", "BANKNIFTY": "25"}

# Custom intervals mapping (what to fetch from Dhan)
BASE_INTERVAL = {
    "30min": "15min",
    "45min": "15min",
    "2h": "1h",
    "3h": "1h",
    "4h": "1h"
}
# How we resample locally
RESAMPLE_RULES = {
    "30min": "30T",
    "45min": "45T",
    "2h": "2H",
    "3h": "3H",
    "4h": "4H"
}

SESSION_START = datetime.strptime("09:15", "%H:%M").time()
SESSION_END   = datetime.strptime("15:30", "%H:%M").time()

# ---------- Helper: per-day, session-anchored resample ----------
def resample_session_anchored(df: pd.DataFrame, rule: str, offset_minutes: int) -> pd.DataFrame:
    """
    Resample df (columns: timestamp, open, high, low, close, volume) to `rule`
    anchored to the exchange session start. Works per day so bars don't cross days.
    Drops any partial bar that would end after 15:30.

    Use offset_minutes=555 to anchor bins at 09:15 (9h15m from start of day).
    """
    if df.empty:
        return df

    out = []
    step = pd.tseries.frequencies.to_offset(rule)
    offset = pd.Timedelta(minutes=offset_minutes)

    # group by trading date (local)
    for _, day_df in df.groupby(df["timestamp"].dt.date):
        day_df = day_df.sort_values("timestamp")

        # keep only regular session
        day_df = day_df[(day_df["timestamp"].dt.time >= SESSION_START) &
                        (day_df["timestamp"].dt.time <= SESSION_END)]
        if day_df.empty:
            continue

        day_df = day_df.set_index("timestamp")

        # resample with bins shifted so first bin starts at 09:15
        res = day_df.resample(
            rule,
            label="left",
            closed="left",
            offset=offset
        ).agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum"
        }).dropna()

        if res.empty:
            continue

        # Keep only bars fully inside the session window
        left_ok = res.index.time >= SESSION_START
        right_edges = (res.index + step)
        right_ok = right_edges.time <= SESSION_END
        res = res[left_ok & right_ok]

        if not res.empty:
            res = res.reset_index()
            out.append(res)

    if not out:
        return df.iloc[0:0].copy()

    return pd.concat(out, ignore_index=True)

# ---------- Routes ----------
@app.route('/')
def show_data():
    dhan = get_dhan()
    if not dhan:
        flash("⚠ Please configure your Dhan credentials in Settings.")
        return redirect(url_for("settings"))

    from_date = request.args.get('from_date', (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d"))
    to_date   = request.args.get('to_date', datetime.now().strftime("%Y-%m-%d"))
    interval_key = request.args.get('interval', '15min')
    index_name   = request.args.get('index', 'NIFTY')
    security_id  = INDEX_IDS.get(index_name, "13")

    # Decide what interval to fetch from Dhan
    fetch_interval = BASE_INTERVAL.get(interval_key, interval_key)
    interval_value = TIMEFRAME_MAP.get(fetch_interval, 15)

    bullish_signals, bearish_signals, data = [], [], []

    try:
        # --- Fetch from Dhan ---
        if interval_value in ["1D", "1W", "1M"]:
            res = dhan.historical_daily_data(
                security_id=security_id, exchange_segment="IDX_I",
                instrument_type="INDEX", from_date=from_date, to_date=to_date
            )
        else:
            res = dhan.intraday_minute_data(
                security_id=security_id, exchange_segment="IDX_I",
                instrument_type="INDEX", from_date=from_date, to_date=to_date,
                interval=interval_value
            )

        df = pd.DataFrame(res.get("data", []))
        if df.empty:
            return render_template(
                "table.html",
                data=[],
                bullish_signals=[],
                bearish_signals=[],
                from_date=from_date, to_date=to_date,
                interval=interval_key, index=index_name
            )

        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")

        # Market hours filter first
        df = df[(df["timestamp"].dt.time >= SESSION_START) &
                (df["timestamp"].dt.time <= SESSION_END)]

        for col in ["open", "high", "low", "close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        if "volume" in df.columns:
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
        else:
            df["volume"] = 0

        # --- Resample only for synthetic intervals ---
        if interval_key in RESAMPLE_RULES:
            # ✅ Anchor all synthetic bars to 09:15 using 555-minute offset (9h15m)
            df = resample_session_anchored(
                df,
                RESAMPLE_RULES[interval_key],
                offset_minutes=555
            )

        # --- Detect signals on the (possibly resampled) df ---
        for _, row in df.iterrows():
            o, h, l, c = row["open"], row["high"], row["low"], row["close"]

            if pd.isna(o) or pd.isna(h) or pd.isna(l) or pd.isna(c):
                continue

            # Bullish
            if o == l and (h - c) >= 2 * (c - l):
                bullish_signals.append({"time": row["timestamp"], "interval": interval_key, "type": "Condition 1"})
            elif (o - l) <= (c - o) and (h - c) >= 2 * (c - o):
                bullish_signals.append({"time": row["timestamp"], "interval": interval_key, "type": "Condition 2"})

            # Bearish
            if o == h and (c - l) >= 2 * (h - c):
                bearish_signals.append({"time": row["timestamp"], "interval": interval_key, "type": "Condition 1"})
            elif (h - o) <= (o - c) and (c - l) >= 2 * (o - c):
                bearish_signals.append({"time": row["timestamp"], "interval": interval_key, "type": "Condition 2"})

        data = df.to_dict(orient="records")

    except Exception as e:
        print("❌ Error:", e)

    return render_template(
        "table.html",
        data=data,
        bullish_signals=bullish_signals,
        bearish_signals=bearish_signals,
        from_date=from_date, to_date=to_date,
        interval=interval_key, index=index_name
    )

@app.route('/test_alert')
def test_alert():
    send_telegram_message("🚨 Test Alert: Your OHLC Signal Alerts are working! ✅")
    return "✅ Test alert sent to Telegram!"

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        config["client_id"] = request.form['client_id']
        config["access_token"] = request.form['access_token']
        config["telegram_bot_token"] = request.form['telegram_bot_token']
        config["telegram_chat_id"] = request.form['telegram_chat_id']
        save_config(config)
        flash("✅ Settings saved successfully!")
        return redirect(url_for("settings"))

    return render_template("settings.html", config=config)

if __name__ == '__main__':
    app.run(debug=True)