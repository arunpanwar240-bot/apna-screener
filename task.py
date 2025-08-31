from apscheduler.schedulers.background import BackgroundScheduler
import time as time_module
from datetime import datetime
import traceback
from dhan_api import get_dhan, detect_signals_from_df, resample_session_anchored, TIMEFRAMES_TO_NOTIFY, BASE_INTERVAL, TIMEFRAME_MAP, INDEX_IDS, SESSION_START, SESSION_END
from config import load_config
import requests

scheduler = BackgroundScheduler()
sent_alerts = set()
last_alert_sent = None
last_sent_times = {}
todays_signals = []
todays_signal_index = 0
config = load_config()

def reset_sent_alerts():
    global sent_alerts
    sent_alerts = set()

def reset_todays_signals():
    global todays_signals, todays_signal_index
    todays_signals = []
    todays_signal_index = 0

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

def check_all_timeframes():
    global last_alert_sent, todays_signals
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
                        security_id=security_id,
                        exchange_segment="IDX_I",
                        instrument_type="INDEX",
                        from_date=datetime.now().strftime("%Y-%m-%d"),
                        to_date=datetime.now().strftime("%Y-%m-%d"),
                    )
                else:
                    res = dhan.intraday_minute_data(
                        security_id=security_id,
                        exchange_segment="IDX_I",
                        instrument_type="INDEX",
                        from_date=datetime.now().strftime("%Y-%m-%d"),
                        to_date=datetime.now().strftime("%Y-%m-%d"),
                        interval=interval_value,
                    )
                data_list = detect_signals_from_df.extract_data_list_from_response(res)
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
                    signal_msg = (
                        f"📈 Bullish Signal - {sig['type']} at {sig['time']} "
                        f"({index_name}, {interval_key})\n"
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
                        f"📉 Bearish Signal - {sig['type']} at {sig['time']} "
                        f"({index_name}, {interval_key})\n"
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
                    time_module.sleep(5)
                    send_telegram_message(signal_msg)
                    last_alert_sent = signal_msg
                    last_sent_times[key] = ts_iso
                else:
                    last_sent_times[key] = ts_iso
            except Exception as e:
                print(f"❌ Scheduler error ({index_name}, {interval_key}): {e}")
                traceback.print_exc()

scheduler.add_job(reset_sent_alerts, 'cron', hour=0, minute=0)
scheduler.add_job(reset_todays_signals, 'cron', hour=9, minute=15)
scheduler.add_job(check_all_timeframes, "interval", seconds=60, id="check_all_timeframes", replace_existing=True, max_instances=1)
scheduler.start()
import atexit
atexit.register(lambda: scheduler.shutdown())
