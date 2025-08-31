import pandas as pd
from datetime import time
import json
from pathlib import Path
import os

# Constants for indexes
INDEX_IDS = {"NIFTY": "13", "BANKNIFTY": "25", "SENSEX": "51"}

# Session timings for intraday
SESSION_START = time(9, 15)
SESSION_END = time(15, 30)

# Mapping for intervals
TIMEFRAME_MAP = {
    "1min": 1,
    "5min": 5,
    "15min": 15,
    "1h": 60,
    "1d": "D",
    "1w": "W",
    "1m": "M",
    "1W": "W",
    "2W": "2W",
    "1M": "M"
}

# Base intervals for building higher intervals
BASE_INTERVAL = {
    "30min": "15min",
    "45min": "15min",
    "2h": "1h",
    "3h": "1h",
    "4h": "1h"
}

# Rules for resampling data
RESAMPLE_RULES = {
    "30min": "30min",
    "45min": "45min",
    "2h": "2h",
    "3h": "3h",
    "4h": "4h"
}

# Intervals for which alerts/notifications are sent
TIMEFRAMES_NOTIFY = ["15min", "30min", "45min", "1h", "2h", "3h", "4h"]


def load_config(file_path="config.json"):
    if os.path.exists(file_path):
        try:
            with open(file_path, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            print(f"Warning: Config file {file_path} is invalid JSON. Loading defaults.")
    return {
        "client_id": "",
        "access_token": "",
        "telegram_bot_token": "",
        "telegram_chat_id": ""
    }


def extract_data_list_from_response(res):
    if res is None:
        return None
    if isinstance(res, list):
        return res if len(res) > 0 else None
    if isinstance(res, pd.DataFrame):
        return res.to_dict(orient="records")
    if isinstance(res, dict):
        for key in ("data", "result", "candles", "items", "rows"):
            if key in res and res[key]:
                return res[key]
        keys = set(res.keys())
        if {"open", "high", "low", "close", "timestamp"}.issubset(keys):
            return [res]
    try:
        if hasattr(res, "get"):
            maybe = res.get("data") or res.get("result") or res.get("candles")
            if maybe:
                return maybe
    except Exception:
        pass
    return None


def detect_signals_from_df(df, interval_key, index_name):
    bullish = []
    bearish = []
    for _, row in df.iterrows():
        ts = row.get("timestamp")
        if pd.isna(ts) or (interval_key in TIMEFRAMES_NOTIFY and not (SESSION_START <= ts.time() <= SESSION_END)):
            if interval_key not in TIMEFRAMES_NOTIFY:
                pass
            else:
                continue
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        if pd.isna(o) or pd.isna(h) or pd.isna(l) or pd.isna(c):
            continue
        body_size = abs(c - o)
        # Bullish signals detection
        if o == l and (h - c) >= 2 * (c - l):
            bullish.append({
                "time": ts,
                "interval": interval_key,
                "type": "EXCELLENT CANDLE",
                "index": index_name,
                "stoploss": round(body_size + (o - l), 2),
                "target": round(h - c, 2)
            })
        elif (o - l) <= (c - o) and (h - c) >= 2 * (c - o):
            bullish.append({
                "time": ts,
                "interval": interval_key,
                "type": "VERY GOOD CANDLE",
                "index": index_name,
                "stoploss": round(body_size + (o - l), 2),
                "target": round(h - c, 2)
            })
        elif (h - c) >= 2 * (c - o) and (o - l) < 4 * (c - o) and (h - c) >= 2 * (c - l):
            bullish.append({
                "time": ts,
                "interval": interval_key,
                "type": "1:2 RISK REWARD CANDLE",
                "index": index_name,
                "stoploss": round(body_size + (o - l), 2),
                "target": round(h - c, 2)
            })
        # Bearish signals detection
        if o == h and (c - l) >= 2 * (h - c):
            bearish.append({
                "time": ts,
                "interval": interval_key,
                "type": "EXCELLENT CANDLE",
                "index": index_name,
                "stoploss": round(body_size + (h - o), 2),
                "target": round(c - l, 2)
            })
        elif (h - o) <= (o - c) and (c - l) >= 2 * (o - c):
            bearish.append({
                "time": ts,
                "interval": interval_key,
                "type": "VERY GOOD CANDLE",
                "index": index_name,
                "stoploss": round(body_size + (h - o), 2),
                "target": round(c - l, 2)
            })
        elif (c - l) >= 2 * (o - c) and (h - o) < 4 * (o - c) and (c - l) >= 2 * (h - c):
            bearish.append({
                "time": ts,
                "interval": interval_key,
                "type": "1:2 RISK REWARD CANDLE",
                "index": index_name,
                "stoploss": round(body_size + (h - o), 2),
                "target": round(c - l, 2)
            })
    return bullish, bearish


def resample_session_anchored(df, rule, offset_minutes):
    if df.empty:
        return df
    out = []
    step = pd.Timedelta(minutes=offset_minutes)
    pd_offset = pd.tseries.frequencies.to_offset(rule)
    for _, day_df in df.groupby(df["timestamp"].dt.date):
        day_df = day_df.sort_values("timestamp")
        day_df = day_df[(day_df["timestamp"].dt.time >= SESSION_START) &
                        (day_df["timestamp"].dt.time <= SESSION_END)]
        if day_df.empty:
            continue
        day_df = day_df.set_index("timestamp")
        resampled = day_df.resample(rule, label="left",
                                   closed="left", offset=step).agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum"
        }).dropna()
        valid = ((resampled.index.time >= SESSION_START) &
                 ((resampled.index + pd_offset).time <= SESSION_END))
        resampled = resampled[valid]
        if not resampled.empty:
            resampled = resampled.reset_index()
            out.append(resampled)
    if not out:
        return df.iloc[0:0].copy()
    return pd.concat(out, ignore_index=True)


def resample_weekly_from_start(df):
    if df.empty:
        return df
    out = []
    for (_, month), group in df.groupby([df["timestamp"].dt.year, df["timestamp"].dt.month]):
        group = group.sort_values("timestamp")
        start_date = group["timestamp"].iloc[0].normalize()
        resampled = group.set_index("timestamp").resample("7D",
                                                          label="left",
                                                          closed="left",
                                                          origin=start_date).agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last"
        }).dropna().reset_index()
        out.append(resampled)
    if not out:
        return pd.DataFrame()
    return pd.concat(out, ignore_index=True)
