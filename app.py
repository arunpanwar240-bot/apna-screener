from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import pandas as pd
from dhanhq import dhanhq
from datetime import datetime, timedelta, time
import requests
import json
import os
import time as time_module
from apscheduler.schedulers.background import BackgroundScheduler
import atexit
import traceback

app = Flask(__name__)
app.secret_key = "supersecretkey"

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

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=4)

config = load_config()

# ✅ Updated get_dhan()
def get_dhan():
    if config.get("client_id") and config.get("access_token"):
        return dhanhq(config["client_id"], config["access_token"])
    return None

sent_alerts = set()
last_alert_sent = None

def send_telegram_message(message):
    global sent_alerts
    if message in sent_alerts:
        return
    bot_token = config.get("telegram_bot_token")
    chat_id   = config.get("telegram_chat_id")
    if not bot_token or not chat_id:
        print("⚠ Telegram not configured")
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}
    try:
        requests.post(url, data=payload, timeout=8)
        sent_alerts.add(message)
    except Exception as e:
        print("❌ Telegram Error:", e)

TIMEFRAMES_TO_NOTIFY = ["1min", "5min", "15min", "30min", "45min", "1h", "2h", "3h", "4h"]
TIMEFRAME_MAP = {
    "1min": 1, "5min": 5, "15min": 15,
    "1h": 60, "1d": "1D", "1w": "1W", "1M": "1M"
}
BASE_INTERVAL = {
    "30min": "15min",
    "45min": "15min",
    "2h": "1h",
    "3h": "1h",
    "4h": "1h"
}
RESAMPLE_RULES = {
    "30min": "30min",
    "45min": "45min",
    "2h": "2h",
    "3h": "3h",
    "4h": "4h"
}
INDEX_IDS = {"NIFTY": "13", "BANKNIFTY": "25", "SENSEX": "51"}
SESSION_START = time(9, 15)
SESSION_END   = time(15, 30)

def resample_session_anchored(df: pd.DataFrame, rule: str, offset_minutes: int) -> pd.DataFrame:
    if df.empty:
        return df
    out = []
    step = pd.tseries.frequencies.to_offset(rule)
    offset = pd.Timedelta(minutes=offset_minutes)
    for _, day_df in df.groupby(df["timestamp"].dt.date):
        day_df = day_df.sort_values("timestamp")
        day_df = day_df[(day_df["timestamp"].dt.time >= SESSION_START) & (day_df["timestamp"].dt.time <= SESSION_END)]
        if day_df.empty:
            continue
        day_df = day_df.set_index("timestamp")
        res = day_df.resample(rule, label="left", closed="left", offset=offset).agg({
            "open":"first","high":"max","low":"min","close":"last","volume":"sum"
        }).dropna()
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

def extract_data_list_from_response(res):
    if res is None:
        return None
    if isinstance(res, list):
        return res if len(res)>0 else None
    if isinstance(res, pd.DataFrame):
        return res.to_dict(orient="records")
    if isinstance(res, dict):
        for key in ("data","result","candles","items","rows"):
            if key in res and res[key]:
                return res[key]
        keys = set(res.keys())
        if {"open","high","low","close","timestamp"}.issubset(keys):
            return [res]
    try:
        if hasattr(res, "get"):
            maybe = res.get("data") or res.get("result") or res.get("candles")
            if maybe:
                return maybe
    except Exception:
        pass
    return None

def detect_signals_from_df(df: pd.DataFrame, interval_key: str, index_name: str):
    bullish = []
    bearish = []
    for _, row in df.iterrows():
        ts = row.get("timestamp")
        if pd.isna(ts) or not (SESSION_START <= ts.time() <= SESSION_END):
            continue
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        if pd.isna(o) or pd.isna(h) or pd.isna(l) or pd.isna(c):
            continue
        body_size = abs(c - o)
        # Bullish
        if o == l and (h - c) >= 2 * (c - l):
            bullish.append({
                "time": ts, "interval": interval_key, "type": "EXCELLENT CANDLE",
                "index": index_name,
                "stoploss": round(body_size + (o - l), 2),
                "target": round(h - c, 2)
            })
        elif (o - l) <= (c - o) and (h - c) >= 2 * (c - o):
            bullish.append({
                "time": ts, "interval": interval_key, "type": "VERY GOOD CANDLE",
                "index": index_name,
                "stoploss": round(body_size + (o - l), 2),
                "target": round(h - c, 2)
            })
        elif (h - c) >= 2 * (c - o) and (o - l) < 4 * (c - o) and (h - c) >= 2 * (c - l):
            bullish.append({
                "time": ts, "interval": interval_key, "type": "1:2 RISK REWARD CANDLE",
                "index": index_name,
                "stoploss": round(body_size + (o - l), 2),
                "target": round(h - c, 2)
            })
        # Bearish
        if o == h and (c - l) >= 2 * (h - c):
            bearish.append({
                "time": ts, "interval": interval_key, "type": "EXCELLENT CANDLE",
                "index": index_name,
                "stoploss": round(body_size + (h - o), 2),
                "target": round(c - l, 2)
            })
        elif (h - o) <= (o - c) and (c - l) >= 2 * (o - c):
            bearish.append({
                "time": ts, "interval": interval_key, "type": "VERY GOOD CANDLE",
                "index": index_name,
                "stoploss": round(body_size + (h - o), 2),
                "target": round(c - l, 2)
            })
        elif (c - l) >= 2 * (o - c) and (h - o) < 4 * (o - c) and (c - l) >= 2 * (h - c):
            bearish.append({
                "time": ts, "interval": interval_key, "type": "1:2 RISK REWARD CANDLE",
                "index": index_name,
                "stoploss": round(body_size + (h - o), 2),
                "target": round(c - l, 2)
            })
    return bullish, bearish

@app.route('/')
def show_data():
    dhan = get_dhan()
    if not dhan:
        flash("⚠ Please configure your Dhan credentials in Settings.")
        return redirect(url_for("settings"))
    from_date = request.args.get('from_date', (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d"))
    to_date   = request.args.get('to_date', datetime.now().strftime("%Y-%m-%d"))
    interval_key = request.args.get('interval', '15min')
    selected_index = request.args.get('index', 'NIFTY')
    
    table_data = []
    all_bullish = []
    all_bearish = []
    
    for index_name, security_id in INDEX_IDS.items():
        try:
            fetch_interval = BASE_INTERVAL.get(interval_key, interval_key)
            interval_value = TIMEFRAME_MAP.get(fetch_interval, 15)
            if interval_value in ["1D", "1W", "1M"]:
                res = dhan.historical_daily_data(
                    security_id=security_id, exchange_segment="IDX_I", instrument_type="INDEX",
                    from_date=from_date, to_date=to_date
                )
            else:
                res = dhan.intraday_minute_data(
                    security_id=security_id, exchange_segment="IDX_I", instrument_type="INDEX",
                    from_date=from_date, to_date=to_date, interval=interval_value
                )
            data_list = extract_data_list_from_response(res)
            if not data_list:
                continue
            df = pd.DataFrame(data_list)
            if df.empty:
                continue
            if "timestamp" in df.columns:
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", errors="coerce", utc=True).dt.tz_convert("Asia/Kolkata")
            elif "time" in df.columns:
                df["timestamp"] = pd.to_datetime(df["time"], unit="s", errors="coerce", utc=True).dt.tz_convert("Asia/Kolkata")
            else:
                df["timestamp"] = pd.NaT
            for col in ["open", "high", "low", "close"]:
                df[col] = pd.to_numeric(df.get(col, pd.NA), errors="coerce")
            df["volume"] = pd.to_numeric(df.get("volume", 0), errors="coerce").fillna(0)
            if interval_key in RESAMPLE_RULES:
                df = resample_session_anchored(df, RESAMPLE_RULES[interval_key], offset_minutes=555)
            
            bullish_signals, bearish_signals = detect_signals_from_df(df, interval_key, index_name)
            all_bullish.extend(bullish_signals)
            all_bearish.extend(bearish_signals)
            
            if index_name == selected_index:
                table_data.extend(df.assign(index=index_name).to_dict(orient="records"))
                
        except Exception as e:
            print(f"❌ Error in show_data() for {index_name}: {e}")
            traceback.print_exc()
            continue
    
    return render_template("table.html",
                          data=table_data,
                          bullish_signals=all_bullish,
                          bearish_signals=all_bearish,
                          from_date=from_date, to_date=to_date,
                          interval=interval_key, index=selected_index,
                          alerts_active=True, last_alert_sent=last_alert_sent,
                          index_choices=INDEX_IDS.keys())

@app.route('/last_alert')
def last_alert():
    return jsonify({"last_alert_sent": last_alert_sent})

@app.route('/test_alert')
def test_alert():
    send_telegram_message("🚨 Test Alert: Your OHLC Signal Alerts are working! ✅")
    flash("🚨 Test Alert Sent!")
    return redirect(url_for("show_data"))

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        config["client_id"] = request.form.get('client_id', '').strip()
        config["access_token"] = request.form.get('access_token', '').strip()
        config["telegram_bot_token"] = request.form.get('telegram_bot_token', '').strip()
        config["telegram_chat_id"] = request.form.get('telegram_chat_id', '').strip()
        save_config(config)
        flash("✅ Settings saved successfully!")
        return redirect(url_for("settings"))
    return render_template("settings.html", config=config)

scheduler = BackgroundScheduler()
last_sent_times = {}

def check_all_timeframes():
    global last_alert_sent
    dhan = get_dhan()
    if not dhan:
        return
    now_time = datetime.now().time()
    if not (SESSION_START <= now_time <= SESSION_END):
        return
    for interval_key in TIMEFRAMES_TO_NOTIFY:
        fetch_interval = BASE_INTERVAL.get(interval_key, interval_key)
        interval_value = TIMEFRAME_MAP.get(fetch_interval, 15)
        for index_name, security_id in INDEX_IDS.items():
            try:
                if interval_value in ["1D", "1W", "1M"]:
                    res = dhan.historical_daily_data(
                        security_id=security_id, exchange_segment="IDX_I", instrument_type="INDEX",
                        from_date=datetime.now().strftime("%Y-%m-%d"), to_date=datetime.now().strftime("%Y-%m-%d")
                    )
                else:
                    res = dhan.intraday_minute_data(
                        security_id=security_id, exchange_segment="IDX_I", instrument_type="INDEX",
                        from_date=datetime.now().strftime("%Y-%m-%d"), to_date=datetime.now().strftime("%Y-%m-%d"),
                        interval=interval_value
                    )
                data_list = extract_data_list_from_response(res)
                if not data_list:
                    continue
                df = pd.DataFrame(data_list)
                if df.empty:
                    continue
                if "timestamp" in df.columns:
                    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", errors="coerce", utc=True).dt.tz_convert("Asia/Kolkata")
                elif "time" in df.columns:
                    df["timestamp"] = pd.to_datetime(df["time"], unit="s", errors="coerce", utc=True).dt.tz_convert("Asia/Kolkata")
                else:
                    df["timestamp"] = pd.NaT
                for col in ["open", "high", "low", "close"]:
                    df[col] = pd.to_numeric(df.get(col, pd.NA), errors="coerce")
                df["volume"] = pd.to_numeric(df.get("volume", 0), errors="coerce").fillna(0)
                if interval_key in RESAMPLE_RULES:
                    df = resample_session_anchored(df, RESAMPLE_RULES[interval_key], offset_minutes=555)
                df = df[(df["timestamp"].dt.time >= SESSION_START) & (df["timestamp"].dt.time <= SESSION_END)]
                if df.shape[0] < 2:
                    continue
                last_row = df.iloc[-2]
                ts = last_row["timestamp"]
                if pd.isna(ts):
                    continue
                ts_iso = pd.Timestamp(ts).isoformat()
                key = f"{index_name}_{interval_key}"
                if last_sent_times.get(key) == ts_iso:
                    continue
                bullish, bearish = detect_signals_from_df(pd.DataFrame([last_row]), interval_key, index_name)
                signal_msg = None
                if bullish:
                    sig = bullish[0]
                    signal_msg = (f"📈 Bullish Signal - {sig['type']} at {sig['time']} "
                                  f"({index_name}, {interval_key})\n"
                                  f"Stoploss: {sig['stoploss']} pts | Target: {sig['target']} pts")
                elif bearish:
                    sig = bearish[0]
                    signal_msg = (f"📉 Bearish Signal - {sig['type']} at {sig['time']} "
                                  f"({index_name}, {interval_key})\n"
                                  f"Stoploss: {sig['stoploss']} pts | Target: {sig['target']} pts")
                if signal_msg:
                    time_module.sleep(5)
                    send_telegram_message(signal_msg)
                    last_alert_sent = signal_msg
                    last_sent_times[key] = ts_iso
                    print("Sent:", signal_msg)
                else:
                    last_sent_times[key] = ts_iso
            except Exception as e:
                print(f"❌ Scheduler error ({index_name}, {interval_key}): {e}")
                traceback.print_exc()

scheduler.add_job(check_all_timeframes, 'interval', seconds=60, id="check_all_timeframes", replace_existing=True, max_instances=1)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())

def reset_sent_alerts():
    global sent_alerts
    sent_alerts = set()

scheduler.add_job(reset_sent_alerts, 'cron', hour=0, minute=0)

if __name__ == '__main__':
    app.run(debug=True)
