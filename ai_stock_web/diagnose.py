#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持仓诊断：对任意一只股票跑完整六因子（估值分位/基本面/筹码/量价/游资），
结合 LLM 给出「上涨可能性 + 买卖信号 + 理由 + 风险」，以及智能止损位（替代固定 -7%）。
只做分析参考，不构成投资建议，不自动下单。
"""
import os
import sys
import json
import re

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stock_scoring import ScoringEngine
from data_api import fetch_daily, build_snapshot
from fundamentals import (
    fetch_valuation_percentile, fetch_fundamentals, fetch_industry_map,
    enrich_snapshot, DiskCache, FUND_CACHE_PATH, VAL_CACHE_PATH, IND_CACHE_PATH,
)

engine = ScoringEngine()
_DEEPSEEK_URL = os.environ.get("DEEPSEEK_URL", "https://api.deepseek.com/v1/chat/completions")
_DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
_DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


def _f(v):
    return None if v is None else v


def _pct(v):
    return None if v is None else round(v * 100, 1)


def suggest_stop(entry_price, support_levels):
    """智能止损：默认 -7%，若下方有更近的防守位（近20日低点/前低平台/均线）则上移。"""
    if not entry_price:
        return None
    stops = [entry_price * 0.93]
    for lv in (support_levels or {}).values():
        if lv and entry_price * 0.85 < lv < entry_price:
            stops.append(lv)
    return round(max(stops), 2)


def diagnose(code, name=""):
    """六因子定量诊断（不调 LLM）。"""
    code = str(code).zfill(6)
    fund_cache = DiskCache(FUND_CACHE_PATH)
    val_cache = DiskCache(VAL_CACHE_PATH)
    ind_cache = DiskCache(IND_CACHE_PATH)

    try:
        daily = fetch_daily(code)
    except Exception as e:
        return {"code": code, "name": name, "error": f"日线获取失败: {e}"}
    if daily is None or len(daily) < 30:
        return {"code": code, "name": name, "error": "日线数据不足"}

    industry_map = fetch_industry_map([code], ind_cache)
    snap = build_snapshot(code, name, daily, industry=industry_map.get(code, ""))
    valuation = fetch_valuation_percentile(code, val_cache)
    fundamental = fetch_fundamentals(code, fund_cache)
    enrich_snapshot(snap, valuation, fundamental)
    res = engine.evaluate(snap)

    return {
        "code": code,
        "name": snap.name or name,
        "industry": snap.industry or "",
        "price": _f(snap.current_price),
        "price_percentile": _pct(snap.price_percentile),
        "pe_percentile": _pct(snap.pe_percentile),
        "pb_percentile": _pct(snap.pb_percentile),
        "roe": _f(snap.roe),
        "debt_ratio": _f(snap.debt_ratio),
        "goodwill_to_equity": _f(snap.goodwill_to_equity),
        "consecutive_profit_years": snap.consecutive_profit_years,
        "volume_ratio": _f(snap.volume_ratio),
        "ret_20d": _f(snap.ret_20d),
        "annual_volatility": _f(snap.annual_volatility),
        "turnover_rate": _f(snap.turnover_rate),
        "chip_peak": snap.chip_peak,
        "trapped_ratio": _pct(snap.trapped_ratio),
        "atr14": _f(snap.atr14),
        "ma5": _f(snap.ma5), "ma10": _f(snap.ma10),
        "ma20": _f(snap.ma20), "ma60": _f(snap.ma60),
        "macd_dif": _f(snap.macd_dif), "macd_dea": _f(snap.macd_dea), "macd_hist": _f(snap.macd_hist),
        "rsi14": _f(snap.rsi14),
        "ma_bull": snap.ma_bull, "macd_golden": snap.macd_golden,
        "breakout_20d": snap.breakout_20d, "volume_surge": snap.volume_surge,
        "score": res["final_score"],
        "risk_level": res["risk_level"],
        "factor_details": {k: {"score": round(v["score"], 0), "weight": v["weight"], "detail": v["detail"]}
                           for k, v in res["factor_details"].items()},
        "deductions": res["deductions"],
        "deduction_total": res["deduction_total"],
        "veto": res["veto_items"],
        "sell_triggers": res["sell_triggers"],
        "support": res["support_levels"],
        "data_notes": res["data_notes"],
        "industry_note": res["industry_template_note"],
    }


def _num(v, suffix=""):
    return "-" if v is None else f"{v:.1f}{suffix}"


def build_fact_line(d, entry_price=None, stop_loss=None):
    parts = [
        f"{d.get('code')} {d.get('name')}",
        f"行业:{d.get('industry') or '-'}",
        f"现价:{d.get('price', '-')}",
        f"价格分位:{_num(d.get('price_percentile'), '%')}",
        f"PE分位:{_num(d.get('pe_percentile'), '%')}",
        f"PB分位:{_num(d.get('pb_percentile'), '%')}",
        f"ROE:{_num(d.get('roe'), '%')}",
        f"资产负债率:{_num(d.get('debt_ratio'), '%')}",
        f"商誉/净资产:{_num(d.get('goodwill_to_equity'), '%')}",
        f"连续盈利:{d.get('consecutive_profit_years')}年" if d.get('consecutive_profit_years') is not None else "盈利:未知",
        f"量比:{_num(d.get('volume_ratio'))}",
        f"近20日涨幅:{_num(d.get('ret_20d'), '%')}",
        f"换手率:{_num(d.get('turnover_rate'), '%')}",
        f"筹码峰:{d.get('chip_peak')}",
        f"综合分:{d.get('score')}",
        f"风险:{d.get('risk_level')}",
    ]
    if d.get("sell_triggers"):
        parts.append("卖出触发:" + "、".join(d["sell_triggers"])[:100])
    if d.get("support"):
        parts.append("支撑位:" + "、".join(f"{k}{v:.2f}" for k, v in d["support"].items()))
    if entry_price:
        parts.append(f"持仓成本:{entry_price}")
        if d.get("price"):
            pnl = (d["price"] / entry_price - 1) * 100
            parts.append(f"现价相对成本:{'浮盈' if pnl >= 0 else '浮亏'}{abs(pnl):.1f}%")
    if stop_loss:
        parts.append(f"当前止损:{stop_loss}")
        if d.get("price"):
            parts.append(f"现价与止损关系:{'已跌破止损' if d['price'] <= stop_loss else '未跌破止损'}")
    return " | ".join(parts)


def derive_signal(d, entry_price=None, stop_loss=None):
    """定量兜底信号（LLM 失败时使用）。"""
    price = d.get("price")
    if d.get("veto"):
        return "卖出", ["存在一票否决：" + "；".join(d["veto"])]
    if d.get("risk_level") == "极高":
        return "卖出", ["风险等级极高"]
    if stop_loss and price and price <= stop_loss:
        return "卖出", [f"已跌破止损 {stop_loss}"]
    n = len(d.get("sell_triggers") or [])
    if n >= 2:
        return "减仓", list(d["sell_triggers"])
    if n == 1:
        return "持有(偏谨慎)", list(d["sell_triggers"])
    score = d.get("score", 0)
    if score >= 60 and d.get("risk_level") == "低":
        return "持有/可加仓", [f"综合分 {score}，估值/基本面/技术面均偏正面，无卖出触发"]
    if score >= 50:
        return "持有", [f"综合分 {score}，风险 {d.get('risk_level')}"]
    return "持有(偏弱)", [f"综合分 {score} 偏低，需等待基本面或情绪面改善"]


def diagnose_with_llm(code, name="", entry_price=None, stop_loss=None):
    """定量诊断 + LLM 定性（上涨可能性/买卖信号/理由/风险）。LLM 失败自动降级到定量信号。"""
    d = diagnose(code, name)
    if d.get("error"):
        d["signal"] = "未知"
        d["upside"] = "-"
        d["reasons"] = [d["error"]]
        return d

    smart_stop = suggest_stop(entry_price, d.get("support"))
    d["suggest_stop"] = smart_stop

    prompt = (
        "你是一位A股基本面+情绪面+技术面研究员。下面是某只股票的真实六因子数据。\n"
        "请分析：该股还有没有上涨可能性？并给出明确的买卖信号。\n"
        "输出严格 JSON 对象（不要任何解释、不要 markdown 代码块），字段：\n"
        "  upside: 上涨可能性（高/中/低）\n"
        "  upside_reason: 一句话理由（为什么有/没有上涨空间）\n"
        "  signal: 买卖信号（买入/加仓/持有/减仓/卖出）\n"
        "  reason: 信号理由，结合基本面、情绪面、技术面各说一句\n"
        "  risk: 最需要盯的一个风险点\n"
        "要求：客观、不吹票、不打目标价、不保证收益。\n\n"
        f"事实数据：\n{build_fact_line(d, entry_price, stop_loss)}\n"
    )
    fallback_signal, fallback_reasons = derive_signal(d, entry_price, stop_loss)
    d["signal"] = fallback_signal
    d["reasons"] = fallback_reasons
    d["upside"] = "-"
    d["llm"] = None
    try:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {_DEEPSEEK_KEY}"}
        payload = {"model": _DEEPSEEK_MODEL,
                   "messages": [{"role": "user", "content": prompt}],
                   "temperature": 0.2, "max_tokens": 1200}
        resp = requests.post(_DEEPSEEK_URL, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        content = msg.get("content") or ""
        if not content.strip():
            content = msg.get("reasoning_content") or ""
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if m:
            obj = json.loads(m.group())
            d["upside"] = obj.get("upside", "-")
            d["upside_reason"] = obj.get("upside_reason", "")
            d["signal"] = obj.get("signal", fallback_signal)
            d["reasons"] = [obj.get("reason", "")] if obj.get("reason") else fallback_reasons
            d["risk"] = obj.get("risk", "")
            d["llm"] = {"model": _DEEPSEEK_MODEL}
    except Exception:
        pass
    return d


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("code")
    ap.add_argument("--entry", type=float, default=None)
    ap.add_argument("--stop", type=float, default=None)
    a = ap.parse_args()
    r = diagnose_with_llm(a.code, entry_price=a.entry, stop_loss=a.stop)
    print(json.dumps(r, ensure_ascii=False, indent=2))
