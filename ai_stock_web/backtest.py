#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回测：用历史交割单验证改进策略（ATR止损+移动止盈 / 趋势反转过滤）。"""
import os
import sys
import json
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from intraday import load_trades
from data_api import fetch_daily, compute_indicators


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _date(v):
    return str(v or "")[:10]


def _norm_daily(daily):
    """把 date 列统一转成 'YYYY-MM-DD' 字符串，方便比较。"""
    if daily is None or "date" not in daily.columns:
        return daily
    daily = daily.copy()
    daily["date"] = daily["date"].apply(lambda x: x.strftime("%Y-%m-%d") if hasattr(x, "strftime") else str(x))
    return daily


def round_trips(trades):
    """FIFO 配对买入→卖出，返回已平仓交易列表。"""
    by_code = defaultdict(list)
    for t in sorted(trades, key=lambda t: str(t.get("time", ""))):
        by_code[t["code"]].append(t)

    trips = []
    for code, ts in by_code.items():
        queue = []
        name = ts[0].get("name", code)
        for t in ts:
            if t["action"] == "buy":
                queue.append({"shares": int(t.get("shares", 0)),
                              "price": float(t.get("price", 0)),
                              "date": _date(t.get("time"))})
            else:
                sell_shares = int(t.get("shares", 0))
                sell_price = float(t.get("price", 0))
                sell_date = _date(t.get("time"))
                while sell_shares > 0 and queue:
                    lot = queue[0]
                    take = min(lot["shares"], sell_shares)
                    trips.append({
                        "code": code, "name": name,
                        "buy_date": lot["date"], "buy_price": lot["price"],
                        "sell_date": sell_date, "sell_price": sell_price,
                        "shares": take,
                        "pnl": (sell_price - lot["price"]) * take,
                        "pnl_pct": (sell_price / lot["price"] - 1) * 100 if lot["price"] else 0,
                    })
                    lot["shares"] -= take
                    sell_shares -= take
                    if lot["shares"] <= 0:
                        queue.pop(0)
    return trips


def reversal_signals_at(daily, buy_date):
    """在买入日当天，用截至当日的数据判断是否有趋势反转信号。"""
    sig = {"macd_golden": False, "ma_bull": False, "breakout_20d": False, "volume_surge": False, "any": False}
    if daily is None or len(daily) == 0:
        return sig
    sub = daily[daily["date"] <= buy_date]
    if len(sub) < 30:
        return sig
    ind = compute_indicators(sub)
    sig["macd_golden"] = ind["macd_golden"]
    sig["ma_bull"] = ind["ma_bull"]
    sig["breakout_20d"] = ind["breakout_20d"]
    sig["volume_surge"] = ind["volume_surge"]
    sig["any"] = ind["macd_golden"] or ind["ma_bull"] or (ind["breakout_20d"] and ind["volume_surge"])
    return sig


def simulate_exit(daily, buy_date, buy_price, atr_mult=2.0, trailing_pct=0.08, take_pct=0.15):
    """模拟改进版止损止盈：ATR止损 + 移动止盈 + 固定止盈。返回 (退出价, 原因) 或 (None,None)。"""
    if daily is None or len(daily) == 0:
        return None, None
    sub = daily[daily["date"] <= buy_date]
    atr = None
    if len(sub) >= 15:
        ind = compute_indicators(sub)
        atr = ind["atr14"]

    stop = buy_price * (1 - 0.07)
    if atr:
        stop = max(stop, buy_price - atr_mult * atr)   # 取更紧的
    take = buy_price * (1 + take_pct)
    if atr:
        take = max(take, buy_price + 3.0 * atr)

    d2 = daily[daily["date"] > buy_date]
    highest = buy_price
    for _, row in d2.iterrows():
        high = _f(row.get("high"))
        low = _f(row.get("low"))
        close = _f(row.get("close"))
        if close is None:
            continue
        if high and high > highest:
            highest = high
        if low is not None and stop and low <= stop:
            return stop, "ATR止损"
        trail = highest * (1 - trailing_pct)
        if highest > buy_price and low is not None and trail > stop and low <= trail:
            return trail, "移动止盈"
        if high is not None and take and high >= take:
            return take, "止盈"
        if stop and close <= stop:
            return stop, "ATR止损"
    return None, None


def run_backtest():
    trades = load_trades()
    if not trades:
        return {"error": "暂无交割单数据"}
    trips = round_trips(trades)
    if not trips:
        return {"error": "无已平仓交易"}

    codes = sorted({t["code"] for t in trips})
    daily_cache = {}
    for c in codes:
        try:
            daily_cache[c] = _norm_daily(fetch_daily(c))
        except Exception:
            daily_cache[c] = None

    actual_total = 0.0
    sim_total = 0.0
    actual_wins = 0
    sim_wins = 0
    improved_better = 0
    with_sig = {"pnl": 0.0, "wins": 0, "n": 0}
    without_sig = {"pnl": 0.0, "wins": 0, "n": 0}
    rows = []

    for t in trips:
        daily = daily_cache.get(t["code"])
        actual = t["pnl"]
        actual_total += actual
        if actual > 0:
            actual_wins += 1

        sim_price, reason = simulate_exit(daily, t["buy_date"], t["buy_price"])
        sim = (sim_price - t["buy_price"]) * t["shares"] if sim_price else actual
        sim_total += sim
        if sim > 0:
            sim_wins += 1
        if sim > actual:
            improved_better += 1

        sig = reversal_signals_at(daily, t["buy_date"])
        if sig["any"]:
            with_sig["pnl"] += actual
            with_sig["n"] += 1
            if actual > 0:
                with_sig["wins"] += 1
        else:
            without_sig["pnl"] += actual
            without_sig["n"] += 1
            if actual > 0:
                without_sig["wins"] += 1

        rows.append({
            "code": t["code"], "name": t["name"],
            "buy_date": t["buy_date"], "buy_price": t["buy_price"],
            "sell_date": t["sell_date"], "sell_price": t["sell_price"],
            "shares": t["shares"], "actual_pnl": round(actual, 0),
            "sim_price": round(sim_price, 2) if sim_price else None,
            "sim_pnl": round(sim, 0), "reason": reason, "reversal": sig["any"],
        })

    n = len(trips)
    return {
        "trip_count": n,
        "actual": {"total_pnl": round(actual_total, 0), "win_rate": round(actual_wins/n*100, 1), "avg_pnl": round(actual_total/n, 0)},
        "improved": {"total_pnl": round(sim_total, 0), "win_rate": round(sim_wins/n*100, 1), "avg_pnl": round(sim_total/n, 0),
                     "better_ratio": round(improved_better/n*100, 1)},
        "reversal": {
            "with_signal": {"n": with_sig["n"], "pnl": round(with_sig["pnl"], 0), "win_rate": round(with_sig["wins"]/with_sig["n"]*100, 1) if with_sig["n"] else 0},
            "without_signal": {"n": without_sig["n"], "pnl": round(without_sig["pnl"], 0), "win_rate": round(without_sig["wins"]/without_sig["n"]*100, 1) if without_sig["n"] else 0},
        },
        "rows": rows,
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    a = ap.parse_args()
    r = run_backtest()
    print(json.dumps(r, ensure_ascii=False, indent=2))
