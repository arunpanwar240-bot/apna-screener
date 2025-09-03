from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import pandas as pd
from dhanhq import dhanhq  # ✅ v2.0.2 uses direct init, no DhanContext
from datetime import datetime, timedelta, time
import requests
import json
import os
import time as time_module
from apscheduler.schedulers.background import BackgroundScheduler
import atexit
import traceback


app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "supersecretkey")  # ✅ Render-friendly


# NOTE: On Render, the filesystem is ephemeral. We keep the same structure, but read from ENV.
CONFIG_FILE = "config.json"


def load_config():
    # ✅ Pull from environment to be Render-friendly (no reliance on file persistence)
    return {
        "client_id": os.getenv("CLIENT_ID", ""),
        "access_token": os.getenv("ACCESS_TOKEN", ""),
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", "")
    }


def save_config(cfg):
    # ✅ Keep function to preserve structure; write attempt is allowed but ephemeral on Render.
    # It will not persist across restarts. We still write for local dev parity.
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=4)
    except Exception as e:
        print("⚠ Could not write config.json (expected on Render):", e)


config = load_config()


def get_dhan():
    # ✅ v2.0.2: initialize the client directly
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


TIMEFRAMES_TO_NOTIFY = ["1min","5min", "15min", "30min", "45min", "1h", "2h", "3h", "4h"]
TIMEFRAME_MAP = {
    "1min": 1, "5min": 5, "15min": 15,
    "1h": 60, "1d": "1D", "1w": "1W", "1m": "1M",
    "1W": "1W", "2W": "2W", "1M": "1M"
}
BASE_INTERVAL = {
    "30min": "15min",
    "45min": "15min",
    "2h": "1h",
    "3h": "1h",
    "4h": "1h"
}
RESAMPLE_RULES = {
    "5min": "5min",
    "15min": "15min",
    "30min": "30min",
    "45min": "45min",
    "1h": "1h",
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
    result = pd.concat(out, ignore_index=True)
    result = result[(result["timestamp"].dt.time >= SESSION_START) & (result["timestamp"].dt.time <= SESSION_END)]
    return result


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
    df = df[(df["timestamp"].dt.time >= SESSION_START) & (df["timestamp"].dt.time <= SESSION_END)]
    for _, row in df.iterrows():
        ts = row.get("timestamp")
        if pd.isna(ts):
            continue
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        if pd.isna(o) or pd.isna(h) or pd.isna(l) or pd.isna(c):
            continue
        body_size = abs(c - o)
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
        elif (c - l) >= 2 * (o - c) and (h - o) < 8 * (o - c) and (c - l) >= 2 * (h - c):
            bearish.append({
                "time": ts, "interval": interval_key, "type": "1:2 RISK REWARD CANDLE",
                "index": index_name,
                "stoploss": round(body_size + (h - o), 2),
                "target": round(c - l, 2)
            })
    return bullish, bearish


def resample_weekly_from_month_start(df_daily: pd.DataFrame):
    if df_daily.empty:
        return df_daily
    out = []
    for (year, month), group in df_daily.groupby([df_daily["timestamp"].dt.year, df_daily["timestamp"].dt.month]):
        group = group.sort_values("timestamp")
        start_date = group["timestamp"].iloc[0].normalize()
        resampled = (
            group.set_index("timestamp")
            .resample('7D', label='left', closed='left', origin=start_date)
            .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
            .dropna()
            .reset_index()
        )
        out.append(resampled)
    if not out:
        return pd.DataFrame()
    result = pd.concat(out, ignore_index=True)
    result = result[(result["timestamp"].dt.time >= SESSION_START) & (result["timestamp"].dt.time <= SESSION_END)]
    return result


# ===== New helpers to ensure only completed candles are used =====
def _interval_rule_for_display(interval_key: str) -> str | None:
    """Return a pandas resample rule for the UI interval if applicable."""
    # Only intervals we resample need the rule; 1min returns None
    return RESAMPLE_RULES.get(interval_key)


def _step_offset_for_interval(interval_key: str):
    """Return a pandas DateOffset step for the given interval (UI interval)."""
    rule = RESAMPLE_RULES.get(interval_key)
    if rule:
        return pd.tseries.frequencies.to_offset(rule)
    # For 1min (or raw fetch intervals), treat as 1 minute
    minutes = TIMEFRAME_MAP.get(interval_key)
    if isinstance(minutes, int):
        return pd.tseries.frequencies.to_offset(f"{minutes}min")
    return None


def drop_incomplete_last_bar(df: pd.DataFrame, interval_key: str) -> pd.DataFrame:
    """Drop the last bar if its end time is in the future (i.e., candle still forming)."""
    if df.empty or "timestamp" not in df.columns:
        return df
    step = _step_offset_for_interval(interval_key)
    if step is None:
        return df
    last_ts = df["timestamp"].iloc[-1]
    try:
        now_ist = pd.Timestamp.now(tz="Asia/Kolkata")
    except Exception:
        now_utc = pd.Timestamp.utcnow().tz_localize("UTC")
        now_ist = now_utc.tz_convert("Asia/Kolkata")
    # Bars are left-labeled; completed if start + step <= now
    if pd.isna(last_ts):
        return df
    if (last_ts + step) > now_ist:
        return df.iloc[:-1].copy()
    return df


@app.route('/')
def show_data():
    dhan = get_dhan()
    if not dhan:
        flash("⚠ Please configure your Dhan credentials in Settings (Render env vars).")
        return redirect(url_for("settings"))
    today_str = datetime.now().strftime("%Y-%m-%d")
    from_date = request.args.get('from_date', today_str)
    to_date = request.args.get('to_date', today_str)
    # ✅ Change 1: default load on 15min
    interval_key = request.args.get('interval', '15min').lower()
    selected_index = request.args.get('index', 'NIFTY')

    if interval_key.lower() == "1w":
        interval_key = "1W"
    if interval_key.lower() == "1m":
        interval_key = "1M"

    table_data = []
    all_bullish = []
    all_bearish = []
    all_intraday_signals = []

    for index_name, security_id in INDEX_IDS.items():
        try:
            actual_interval = interval_key
            fetch_interval = BASE_INTERVAL.get(actual_interval, actual_interval)
            interval_value = TIMEFRAME_MAP.get(fetch_interval, actual_interval)
            if interval_value in ["1D", "1W", "1M"]:
                res = dhan.historical_daily_data(
                    security_id=security_id,
                    exchange_segment="IDX_I",
                    instrument_type="INDEX",
                    from_date=from_date,
                    to_date=to_date,
                )
                data_list = extract_data_list_from_response(res)
                if not data_list:
                    continue
                df = pd.DataFrame(data_list)
                if df.empty:
                    continue
                if "timestamp" in df.columns:
                    df["timestamp"] = pd.to_datetime(
                        df["timestamp"], unit="s", errors="coerce", utc=True
                    ).dt.tz_convert("Asia/Kolkata")
                elif "time" in df.columns:
                    df["timestamp"] = pd.to_datetime(
                        df["time"], unit="s", errors="coerce", utc=True
                    ).dt.tz_convert("Asia/Kolkata")
                else:
                    df["timestamp"] = pd.NaT
                for col in ["open", "high", "low", "close"]:
                    df[col] = pd.to_numeric(df.get(col, pd.NA), errors="coerce")

            else:
                res = dhan.intraday_minute_data(
                    security_id=security_id,
                    exchange_segment="IDX_I",
                    instrument_type="INDEX",
                    from_date=from_date,
                    to_date=to_date,
                    interval=interval_value,
                )
                data_list = extract_data_list_from_response(res)
                if not data_list:
                    continue
                df = pd.DataFrame(data_list)
                if df.empty:
                    continue
                if "timestamp" in df.columns:
                    df["timestamp"] = pd.to_datetime(
                        df["timestamp"], unit="s", errors="coerce", utc=True
                    ).dt.tz_convert("Asia/Kolkata")
                elif "time" in df.columns:
                    df["timestamp"] = pd.to_datetime(
                        df["time"], unit="s", errors="coerce", utc=True
                    ).dt.tz_convert("Asia/Kolkata")
                else:
                    df["timestamp"] = pd.NaT
                for col in ["open", "high", "low", "close"]:
                    df[col] = pd.to_numeric(df.get(col, pd.NA), errors="coerce")
                df["volume"] = pd.to_numeric(df.get("volume", 0), errors="coerce").fillna(0)
                if interval_key in RESAMPLE_RULES:
                    df = resample_session_anchored(df, RESAMPLE_RULES[interval_key], offset_minutes=555)
                df = df[(df["timestamp"].dt.time >= SESSION_START) & (df["timestamp"].dt.time <= SESSION_END)]
                # ✅ Change 2: only completed bars shown on dashboard
                df = drop_incomplete_last_bar(df, interval_key)

            # Signals (on completed bars only)
            bullish_signals, bearish_signals = detect_signals_from_df(df, interval_key, index_name)
            all_bullish.extend([dict(d, interval=interval_key) for d in bullish_signals])
            all_bearish.extend([dict(d, interval=interval_key) for d in bearish_signals])
            # For dashboard, always show signal cards interval matching their detected interval for full accuracy.
            if index_name == selected_index:
                table_data.extend(df.assign(index=index_name).to_dict(orient="records"))

        except Exception as e:
            print(f"❌ Error in show_data() for {index_name}: {e}")
            traceback.print_exc()
            continue

    # 3. Today's Signals: Collect from all intervals 5min to 4h, sorted by time
    dhan_independent = get_dhan()
    if dhan_independent:
        from_today = today_str
        to_today = today_str
        all_today_signals = []
        for interval_key_tf in TIMEFRAMES_TO_NOTIFY:  # ✅ Change 3 stays: 5min and above only
            fetch_interval_tf = BASE_INTERVAL.get(interval_key_tf, interval_key_tf)
            interval_value_tf = TIMEFRAME_MAP.get(fetch_interval_tf, interval_key_tf)
            for index_name_tf, security_id_tf in INDEX_IDS.items():
                try:
                    res = dhan_independent.intraday_minute_data(
                        security_id=security_id_tf,
                        exchange_segment="IDX_I",
                        instrument_type="INDEX",
                        from_date=from_today,
                        to_date=to_today,
                        interval=interval_value_tf,
                    )
                    data_list_tf = extract_data_list_from_response(res)
                    if not data_list_tf:
                        continue
                    df_tf = pd.DataFrame(data_list_tf)
                    if df_tf.empty:
                        continue
                    if "timestamp" in df_tf.columns:
                        df_tf["timestamp"] = pd.to_datetime(
                            df_tf["timestamp"], unit="s", errors="coerce", utc=True
                        ).dt.tz_convert("Asia/Kolkata")
                    elif "time" in df_tf.columns:
                        df_tf["timestamp"] = pd.to_datetime(
                            df_tf["time"], unit="s", errors="coerce", utc=True
                        ).dt.tz_convert("Asia/Kolkata")
                    else:
                        df_tf["timestamp"] = pd.NaT
                    for col in ["open", "high", "low", "close"]:
                        df_tf[col] = pd.to_numeric(df_tf.get(col, pd.NA), errors="coerce")
                    df_tf["volume"] = pd.to_numeric(df_tf.get("volume", 0), errors="coerce").fillna(0)
                    if interval_key_tf in RESAMPLE_RULES:
                        df_tf = resample_session_anchored(df_tf, RESAMPLE_RULES[interval_key_tf], offset_minutes=555)
                    df_tf = df_tf[(df_tf["timestamp"].dt.time >= SESSION_START) & (df_tf["timestamp"].dt.time <= SESSION_END)]
                    # ✅ Change 2 applied here as well
                    df_tf = drop_incomplete_last_bar(df_tf, interval_key_tf)
                    bullish_tf, bearish_tf = detect_signals_from_df(df_tf, interval_key_tf, index_name_tf)
                    for sig in bullish_tf + bearish_tf:
                        sig["interval"] = interval_key_tf
                        all_today_signals.append(sig)
                except Exception as e:
                    print(f"❌ Error fetching signals for {index_name_tf} at {interval_key_tf}: {e}")
                    traceback.print_exc()
        # Sort all signals by time for intraday list
        todays_signals_all_timeframes = sorted(all_today_signals, key=lambda x: x["time"])
    else:
        todays_signals_all_timeframes = []

    return render_template(
        "table.html",
        data=table_data,
        bullish_signals=all_bullish,
        bearish_signals=all_bearish,
        todays_signals=todays_signals_all_timeframes,
        from_date=from_date,
        to_date=to_date,
        interval=interval_key,
        index=selected_index,
        alerts_active=True,
        last_alert_sent=last_alert_sent,
        index_choices=INDEX_IDS.keys(),
        show_table=False,  # <-- NEW: pass default hide for OHLC table
    )


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
    # Keep route & structure; use env-backed config; allow in-process update (ephemeral on Render)
    if request.method == 'POST':
        config["client_id"] = request.form.get('client_id', '').strip()
        config["access_token"] = request.form.get('access_token', '').strip()
        config["telegram_bot_token"] = request.form.get('telegram_bot_token', '').strip()
        config["telegram_chat_id"] = request.form.get('telegram_chat_id', '').strip()
        # Save to file for local dev parity (won't persist across Render restarts)
        save_config(config)
        flash("✅ Settings saved in-process (note: set ENV VARS on Render for persistence).")
        return redirect(url_for("settings"))
    return render_template("settings.html", config=config)


scheduler = BackgroundScheduler()
last_sent_times = {}


todays_signals = []
todays_signal_index = 0


def reset_sent_alerts():
    global sent_alerts
    sent_alerts = set()


def reset_todays_signals():
    global todays_signals, todays_signal_index
    todays_signals = []
    todays_signal_index = 0


scheduler.add_job(reset_sent_alerts, 'cron', hour=0, minute=0)
scheduler.add_job(reset_todays_signals, 'cron', hour=9, minute=15)


# ✅ Change 4: One-time 1-minute test flag & routes
ENABLE_1MIN_TEST = False  # toggled by /enable_1min_test; auto resets after first 1min signal is sent


def check_all_timeframes():
    global last_alert_sent, todays_signals, ENABLE_1MIN_TEST
    dhan = get_dhan()
    if not dhan:
        return
    now_time = datetime.now().time()
    if not (SESSION_START <= now_time <= SESSION_END):
        return

    index_bold_map = {
        "NIFTY": "**NIFTY**",
        "BANKNIFTY": "**BANKNIFTY**",
        "SENSEX": "**SENSEX**"
    }
    bullish_emoji = "🐂"
    bearish_emoji = "🐻"

    # Build list, optionally including 1min exactly once for test
    timeframe_loop = list(TIMEFRAMES_TO_NOTIFY)
    if ENABLE_1MIN_TEST and "1min" not in timeframe_loop:
        timeframe_loop = ["1min"] + timeframe_loop  # prioritize quick test send

    for interval_key in timeframe_loop:
        fetch_interval = BASE_INTERVAL.get(interval_key, interval_key)
        interval_value = TIMEFRAME_MAP.get(fetch_interval, 15)
        for index_name, security_id in INDEX_IDS.items():
            try:
                res = dhan.intraday_minute_data(
                    security_id=security_id,
                    exchange_segment="IDX_I",
                    instrument_type="INDEX",
                    from_date=datetime.now().strftime("%Y-%m-%d"),
                    to_date=datetime.now().strftime("%Y-%m-%d"),
                    interval=interval_value,
                )
                data_list = extract_data_list_from_response(res)
                if not data_list:
                    continue
                df = pd.DataFrame(data_list)
                if df.empty:
                    continue
                if "timestamp" in df.columns:
                    df["timestamp"] = pd.to_datetime(
                        df["timestamp"], unit="s", errors="coerce", utc=True
                    ).dt.tz_convert("Asia/Kolkata")
                elif "time" in df.columns:
                    df["timestamp"] = pd.to_datetime(
                        df["time"], unit="s", errors="coerce", utc=True
                    ).dt.tz_convert("Asia/Kolkata")
                else:
                    df["timestamp"] = pd.NaT
                for col in ["open", "high", "low", "close"]:
                    df[col] = pd.to_numeric(df.get(col, pd.NA), errors="coerce")
                df["volume"] = pd.to_numeric(df.get("volume", 0), errors="coerce").fillna(0)
                if interval_key in RESAMPLE_RULES:
                    df = resample_session_anchored(df, RESAMPLE_RULES[interval_key], offset_minutes=555)
                df = df[(df["timestamp"].dt.time >= SESSION_START) & (df["timestamp"].dt.time <= SESSION_END)]

                # ✅ Ensure we signal only after bar completion:
                # For reliability, we still look at the last fully closed bar, i.e., -2 if last may be forming.
                if df.shape[0] < 2:
                    continue
                last_closed_row = df.iloc[-2]
                ts = last_closed_row["timestamp"]
                if pd.isna(ts):
                    continue
                ts_iso = pd.Timestamp(ts).isoformat()
                key = f"{index_name}_{interval_key}"
                if last_sent_times.get(key) == ts_iso:
                    continue

                bullish, bearish = detect_signals_from_df(pd.DataFrame([last_closed_row]), interval_key, index_name)
                signal_msg = None
                if bullish:
                    sig = bullish[0]
                    signal_msg = (
                        f"{bullish_emoji} Bullish Signal - {sig['type']} at {sig['time']} "
                        f"({index_bold_map.get(index_name, index_name)}, *{interval_key}*)\n"
                        f"Stoploss: {sig['stoploss']} pts | Target: {sig['target']} pts"
                    )
                    todays_signals.append(
                        {
                            "time": str(sig["time"]),
                            "index": sig["index"],
                            "interval": sig["interval"],
                            "type": sig["type"],
                            "stoploss": sig["stoploss"],
                            "target": sig["target"],
                        }
                    )
                elif bearish:
                    sig = bearish[0]
                    signal_msg = (
                        f"{bearish_emoji} Bearish Signal - {sig['type']} at {sig['time']} "
                        f"({index_bold_map.get(index_name, index_name)}, *{interval_key}*)\n"
                        f"Stoploss: {sig['stoploss']} pts | Target: {sig['target']} pts"
                    )
                    todays_signals.append(
                        {
                            "time": str(sig["time"]),
                            "index": sig["index"],
                            "interval": sig["interval"],
                            "type": sig["type"],
                            "stoploss": sig["stoploss"],
                            "target": sig["target"],
                        }
                    )
                if signal_msg:
                    time_module.sleep(2)
                    send_telegram_message(signal_msg)
                    last_alert_sent = signal_msg
                    last_sent_times[key] = ts_iso
                    print("Sent:", signal_msg)
                    # ✅ If this was the 1min test, turn it off after first send
                    if ENABLE_1MIN_TEST and interval_key == "1min":
                        ENABLE_1MIN_TEST = False
                        print("🔕 1min test auto-disabled after first signal.")
                else:
                    last_sent_times[key] = ts_iso
            except Exception as e:
                print(f"❌ Scheduler error ({index_name}, {interval_key}): {e}")
                traceback.print_exc()


# Keep same job signature; logic inside handles 1min test toggle
scheduler.add_job(check_all_timeframes, "interval", seconds=5, id="check_all_timeframes", replace_existing=True, max_instances=1)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())


@app.route("/todays_signal")
def todays_signal():
    global todays_signals, todays_signal_index
    if not todays_signals:
        return jsonify({"signal": None})
    signal = todays_signals[todays_signal_index]
    todays_signal_index = (todays_signal_index + 1) % len(todays_signals)
    return jsonify({"signal": signal})


# ✅ Routes to control 1min test
@app.route("/enable_1min_test")
def enable_1min_test():
    global ENABLE_1MIN_TEST
    ENABLE_1MIN_TEST = True
    flash("✅ 1-minute test enabled. A single 1min signal will be sent when the next candle completes.")
    return redirect(url_for("show_data"))

@app.route("/disable_1min_test")
def disable_1min_test():
    global ENABLE_1MIN_TEST
    ENABLE_1MIN_TEST = False
    flash("🔕 1-minute test disabled.")
    return redirect(url_for("show_data"))


if __name__ == "__main__":
    # ✅ Render provides $PORT; bind 0.0.0.0 for external access
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
