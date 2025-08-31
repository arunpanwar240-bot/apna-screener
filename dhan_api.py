import pandas as pd
from dhanhq import DhanContext, dhanhq
from datetime import datetime
from config import load_config

config = load_config()

INDEX_IDS = {"NIFTY": "13", "BANKNIFTY": "25", "SENSEX": "51"}

SESSION_START = pd.to_datetime("09:15:00").time()
SESSION_END = pd.to_datetime("15:30:00").time()

TIMEFRAME_MAP = {
    "1min": 1, "5min": 5, "15min": 15, "1h": 60,
    "1d": "1D", "1w": "1W", "1m": "1M",
    "1W": "1W", "2W": "2W", "1M": "1M"
}

BASE_INTERVAL = {
    "30min": "15min", "45min": "15min", "2h": "1h",
    "3h": "1h", "4h": "1h"
}

RESAMPLE_RULES = {
    "30min": "30min", "45min": "45min", "2h": "2h",
    "3h": "3h", "4h": "4h"
}

def get_dhan():
    if config.get("client_id") and config.get("access_token"):
        ctx = DhanContext(client_id=config["client_id"], access_token=config["access_token"])
        return dhanhq(ctx)
    return None

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
        if pd.isna(ts) or (interval_key in ["15min","30min","45min","1h","2h","3h","4h"] and not (SESSION_START <= ts.time() <= SESSION_END)):
            if interval_key not in ["15min","30min","45min","1h","2h","3h","4h"]:
                pass
            else:
                continue
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        if pd.isna(o) or pd.isna(h) or pd.isna(l) or pd.isna(c):
            continue
        body_size = abs(c - o)
        # Bullish signals logic...
        # Bearish signals logic...
        # (Copy your existing detect_signals_from_df logic here)
    return bullish, bearish

def resample_session_anchored(df, rule, offset_minutes):
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
            "open":"first", "high":"max", "low":"min", "close":"last", "volume":"sum"
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

def resample_weekly_from_month_start(df_daily):
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
    return pd.concat(out, ignore_index=True)
