#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行情数据层（新浪源为主，东财源可选降级）——免费、无需 token"""
import os
import time
import akshare as ak
import pandas as pd
import numpy as np

from stock_scoring import StockSnapshot, attach_support, detect_board, MAIN


def fetch_spot():
    """全A实时行情(新浪源)。返回带 pure_code 列的 DataFrame。"""
    df = ak.stock_zh_a_spot()
    df["pure_code"] = df["代码"].str[-6:]
    return df


def fetch_daily(code: str, years: int = 6, retries: int = 2) -> pd.DataFrame:
    """单只股票前复权日线(新浪源)，带重试。"""
    c = str(code).zfill(6)
    if c[0] in "69":
        sym = "sh" + c
    elif c[0] in "023":
        sym = "sz" + c
    else:
        sym = "bj" + c
    last = None
    for i in range(retries + 1):
        try:
            return ak.stock_zh_a_daily(symbol=sym, adjust="qfq")
        except Exception as e:
            last = e
            time.sleep(0.3 * (i + 1))
    raise last


def _f(v):
    """把 NaN/inf 转 None，保留普通数值。"""
    if v is None:
        return None
    try:
        f = float(v)
        if pd.isna(f) or np.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def price_percentile(daily: pd.DataFrame, years: int = 5) -> float:
    """最新收盘价在近 N 年日线中的分位(0~1)。"""
    if daily is None or len(daily) < 120:
        return None
    closes = daily["close"].dropna().reset_index(drop=True)
    win = closes.iloc[-min(len(closes), int(years * 250)):]
    last = closes.iloc[-1]
    return float((win <= last).mean())


def volume_price(daily: pd.DataFrame) -> dict:
    """从日线推算量价指标。"""
    out = {"volume_ratio": None, "ret_20d": None, "annual_volatility": None,
           "turnover_rate": None, "abnormal_turnover_long": False}
    if daily is None or len(daily) < 30:
        return out
    closes = daily["close"]
    vols = daily["volume"] if "volume" in daily.columns else None
    if vols is not None and len(vols) >= 6 and vols.iloc[-6:-1].mean() > 0:
        out["volume_ratio"] = _f(vols.iloc[-1] / vols.iloc[-6:-1].mean())
    if len(closes) >= 21:
        out["ret_20d"] = _f((closes.iloc[-1] / closes.iloc[-21] - 1) * 100)
    rets = closes.pct_change().dropna()
    if len(rets) > 60:
        out["annual_volatility"] = _f(rets.iloc[-250:].std() * np.sqrt(250) * 100)
    if "turnover" in daily.columns:
        out["turnover_rate"] = _f(daily["turnover"].iloc[-1] * 100)
        avg_turn = daily["turnover"].iloc[-20:].mean()
        out["abnormal_turnover_long"] = bool(avg_turn > 0.05)  # 近20日均换手>5%
    return out


def derive_chip(daily: pd.DataFrame, pct: float) -> dict:
    """用价格分位近似筹码分布。"""
    if pct is None:
        return {"chip_peak": "mid", "low_chip_concentration": None,
                "high_trapped_trend": "stable", "trapped_ratio": None}
    chip_peak = "low" if pct < 0.3 else ("mid" if pct < 0.7 else "high")
    high_trapped_trend = "decreasing" if pct < 0.3 else ("increasing" if pct > 0.7 else "stable")
    return {"chip_peak": chip_peak,
            "low_chip_concentration": _f(max(0.0, 1.0 - pct)),
            "high_trapped_trend": high_trapped_trend,
            "trapped_ratio": _f(pct)}


def compute_indicators(daily: pd.DataFrame) -> dict:
    """技术指标：ATR/均线/MACD/RSI/放量突破（用于买卖点与趋势过滤）。"""
    out = {"atr14": None, "ma5": None, "ma10": None, "ma20": None, "ma60": None,
           "macd_dif": None, "macd_dea": None, "macd_hist": None,
           "rsi14": None, "breakout_20d": False, "volume_surge": False,
           "ma_bull": False, "macd_golden": False}
    if daily is None or len(daily) < 30:
        return out

    c = daily["close"]
    h = daily["high"] if "high" in daily.columns else c
    l = daily["low"] if "low" in daily.columns else c
    v = daily["volume"] if "volume" in daily.columns else None

    # 均线 + 多头排列
    for n, key in ((5, "ma5"), (10, "ma10"), (20, "ma20"), (60, "ma60")):
        if len(c) >= n:
            out[key] = _f(c.iloc[-n:].mean())
    if all(out[k] is not None for k in ("ma5", "ma10", "ma20", "ma60")):
        out["ma_bull"] = out["ma5"] > out["ma10"] > out["ma20"] > out["ma60"]

    # ATR(14)
    if len(c) >= 15:
        pc = c.shift(1)
        tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        out["atr14"] = _f(tr.iloc[-14:].mean())

    # RSI(14)
    if len(c) >= 15:
        diff = c.diff()
        gain = diff.clip(lower=0).iloc[-14:].mean()
        loss = (-diff.clip(upper=0)).iloc[-14:].mean()
        out["rsi14"] = _f(100 - 100 / (1 + gain / loss)) if loss > 0 else 100.0

    # MACD(12,26,9) + 金叉
    if len(c) >= 35:
        ema12 = c.ewm(span=12, adjust=False).mean()
        ema26 = c.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        out["macd_dif"] = _f(dif.iloc[-1])
        out["macd_dea"] = _f(dea.iloc[-1])
        out["macd_hist"] = _f((dif.iloc[-1] - dea.iloc[-1]) * 2)
        out["macd_golden"] = bool(dif.iloc[-1] > dea.iloc[-1] and dif.iloc[-2] <= dea.iloc[-2])

    # 突破20日新高
    if len(c) >= 21:
        out["breakout_20d"] = bool(c.iloc[-1] >= c.iloc[-21:-1].max())

    # 放量：今日量 > 5日均量*1.5
    if v is not None and len(v) >= 6 and v.iloc[-6:-1].mean() > 0:
        out["volume_surge"] = bool(v.iloc[-1] > v.iloc[-6:-1].mean() * 1.5)

    return out


def build_snapshot(code: str, name: str, daily: pd.DataFrame, industry: str = "") -> StockSnapshot:
    """由真实日线构造 StockSnapshot（估值用价格分位近似，量价/筹码/技术指标真实）。"""
    pct = price_percentile(daily)
    vp = volume_price(daily)
    chip = derive_chip(daily, pct)
    ind = compute_indicators(daily)

    s = StockSnapshot(
        code=code, name=name, industry=industry,
        price_percentile=_f(pct),
        bottom_consolidation=bool(pct is not None and pct < 0.3 and
                                  (vp["ret_20d"] is None or -5 <= vp["ret_20d"] <= 10)),
        chip_peak=chip["chip_peak"],
        low_chip_concentration=chip["low_chip_concentration"],
        high_trapped_trend=chip["high_trapped_trend"],
        trapped_ratio=chip["trapped_ratio"],
        volume_ratio=vp["volume_ratio"],
        ret_20d=vp["ret_20d"],
        annual_volatility=vp["annual_volatility"],
        turnover_rate=vp["turnover_rate"],
        abnormal_turnover_long=vp["abnormal_turnover_long"],
        atr14=ind["atr14"],
        ma5=ind["ma5"], ma10=ind["ma10"], ma20=ind["ma20"], ma60=ind["ma60"],
        macd_dif=ind["macd_dif"], macd_dea=ind["macd_dea"], macd_hist=ind["macd_hist"],
        rsi14=ind["rsi14"],
        breakout_20d=ind["breakout_20d"], volume_surge=ind["volume_surge"],
        ma_bull=ind["ma_bull"], macd_golden=ind["macd_golden"],
    )
    if daily is not None and len(daily) > 0:
        closes = daily["close"].dropna()
        if len(closes) > 0:
            attach_support(s, [float(x) for x in closes.tolist()], float(closes.iloc[-1]))
    return s
