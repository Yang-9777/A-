#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 定性分析（TODO#3）：读 watch_pool.json → DeepSeek 生成「一句话逻辑/赛道解读/风险」→ llm_report.json
设计原则（与评分引擎一致）：
  - LLM 只做定性解读，不打数值分、不给买卖指令（数值分由六因子引擎负责）
  - 位置/估值分位/基本面/筹码/量价/游资 作为事实输入，LLM 输出逻辑与风险
"""
import json
import os
import re
import sys

import requests

DEEPSEEK_URL = os.environ.get("DEEPSEEK_URL", "https://api.deepseek.com/v1/chat/completions")
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
# deepseek-chat(=v4-flash) 对结构化 JSON 抽取更稳定、更快；v4-pro 推理更深但偶尔 content 为空
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

POOL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watch_pool.json")
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm_report.json")


def _pct(v):
    return "-" if v is None else f"{v * 100:.0f}%"


def _num(v, suffix=""):
    return "-" if v is None else f"{v:.1f}{suffix}"


def build_stock_line(s):
    """把一只股票的六因子事实压成一行，喂给 LLM。"""
    parts = [
        f"{s['code']} {s['name']}",
        f"行业:{s.get('industry') or '-'}",
        f"现价:{s.get('price', '-')}",
        f"价格分位:{_pct(s.get('price_percentile'))}",
        f"PE分位:{_pct(s.get('pe_percentile'))}",
        f"PB分位:{_pct(s.get('pb_percentile'))}",
        f"ROE:{_num(s.get('roe'), '%')}",
        f"资产负债率:{_num(s.get('debt_ratio'), '%')}",
        f"商誉/净资产:{_num(s.get('goodwill_to_equity'), '%')}",
        f"连续盈利:{s.get('consecutive_profit_years')}年" if s.get('consecutive_profit_years') is not None else "盈利:未知",
        f"综合分:{s.get('combined_score')}",
        f"风险:{s.get('risk_level')}",
    ]
    if s.get("hm_flags"):
        parts.append("游资标记:" + "、".join(s["hm_flags"]))
    if s.get("hm_net"):
        parts.append(f"游资净买:{s['hm_net']:.0f}万")
    if s.get("sell_triggers"):
        parts.append("卖出触发:" + "、".join(s["sell_triggers"])[:80])
    return " | ".join(parts)


def build_prompt(stocks):
    lines = [
        "你是一位A股基本面+情绪研究员。下面是选股引擎（六因子：估值分位/基本面/筹码/量价/游资）筛选出的低位候选股。",
        "请对每一只股票，用一句话给出：",
        "  thesis（核心逻辑：为什么它现在值得关注/为什么不值得）",
        "  sector（赛道解读：所属行业景气与位置）",
        "  risk（主要风险：最需要盯的一个点）",
        "",
        "要求：",
        "1. 只做定性解读，不打分、不给买卖指令、不给目标价。",
        "2. 输出严格 JSON 数组，每项字段：code,name,thesis,sector,risk，thesis/sector/risk 各 15~40 字。",
        "3. 只输出 JSON，不要任何解释、markdown 代码块。",
        "",
        "候选股如下：",
    ]
    for s in stocks:
        lines.append(build_stock_line(s))
    return "\n".join(lines)


def call_deepseek(prompt):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {DEEPSEEK_KEY}"}
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 4000,
    }
    resp = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=180)
    resp.raise_for_status()
    js = resp.json()
    msg = js["choices"][0]["message"]
    # 推理模型(v4-pro)可能把内容放进 content 为空、reasoning_content 有值；优先取 content
    content = msg.get("content") or ""
    if not content.strip():
        content = msg.get("reasoning_content") or ""
    if not content.strip():
        raise RuntimeError("LLM 返回空内容")
    return content


def parse_json_array(text):
    """从 LLM 输出里抠出 JSON 数组。"""
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group())
        return arr if isinstance(arr, list) else []
    except json.JSONDecodeError:
        return []


def main(top=None, push=False):
    if not os.path.exists(POOL_PATH):
        print("未找到 watch_pool.json", file=sys.stderr)
        return
    with open(POOL_PATH, encoding="utf-8") as f:
        pool = json.load(f)
    stocks = pool.get("stocks", [])
    if top:
        stocks = stocks[:top]
    if not stocks:
        print("选股池为空", file=sys.stderr)
        return

    print(f"LLM 定性：{len(stocks)} 只候选股，模型 {DEEPSEEK_MODEL}", flush=True)
    prompt = build_prompt(stocks)
    text = call_deepseek(prompt)
    arr = parse_json_array(text)

    # 按 code 对齐回原列表（LLM 可能漏/乱序），补充缺省项
    by_code = {str(x.get("code")).zfill(6): x for x in arr}
    merged = []
    for s in stocks:
        c = str(s["code"]).zfill(6)
        note = by_code.get(c, {})
        merged.append({
            "code": c,
            "name": s["name"],
            "thesis": note.get("thesis", ""),
            "sector": note.get("sector", ""),
            "risk": note.get("risk", ""),
        })

    payload = {
        "generated_at": __import__("datetime").datetime.now().isoformat(),
        "model": DEEPSEEK_MODEL,
        "source": "watch_pool.json 六因子事实 + DeepSeek 定性",
        "total": len(merged),
        "stocks": merged,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"已写出 {OUT_PATH}，{len(merged)} 条", flush=True)
    for m in merged:
        print(f"  {m['code']} {m['name']}: {m['thesis'][:40]} | {m['risk'][:30]}", flush=True)

    if push and WEBHOOK_URL:
        _push_webhook(merged)


def _push_webhook(merged):
    lines = ["📊 选股池 LLM 定性（一句话逻辑/风险）"]
    for m in merged:
        lines.append(f"• {m['name']}({m['code']}): {m['thesis']}｜风险:{m['risk']}")
    try:
        requests.post(WEBHOOK_URL, json={"msgtype": "text", "text": {"content": "\n".join(lines)}}, timeout=15)
        print("已推送 webhook", flush=True)
    except Exception as e:
        print(f"webhook 推送失败: {e}", file=sys.stderr)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=0, help="只分析前N只(0=全部)")
    ap.add_argument("--push", action="store_true", help="推送 webhook")
    a = ap.parse_args()
    main(top=a.top, push=a.push)
