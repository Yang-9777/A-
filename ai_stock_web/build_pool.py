#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘后真实选股流水线：新浪行情(全主板) + 龙虎榜游资因子 → watch_pool.json
思路：广撒网抓全主板日线 → 算股价历史分位(位置) + 量价 + 筹码 → v3打分 → 游资加减分 → 排名
"""
import os
import sys
import json
import time
import random
import argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import timeout_patch  # noqa: F401  给 requests 加默认超时，避免新浪限流挂死

from stock_scoring import ScoringEngine, detect_board, MAIN
from data_api import fetch_spot, fetch_daily, build_snapshot
from fundamentals import (
    fetch_valuation_percentile, fetch_fundamentals, fetch_industry_map, enrich_snapshot,
    DiskCache, FUND_CACHE_PATH, VAL_CACHE_PATH, IND_CACHE_PATH,
)

DATA_DIR = os.environ.get("LHB_DATA_DIR") or (
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    if os.path.isdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
    else "/opt/longhubang-server/data"
)
TIER_W = {"S级": 5, "A级": 3, "B级": 1}
engine = ScoringEngine()


def parse_amount(a):
    a = str(a).strip().replace(",", "")
    if not a:
        return 0.0
    if a.endswith("亿"):
        return float(a[:-1]) * 10000.0
    if a.endswith("万"):
        return float(a[:-1])
    try:
        return float(a)
    except ValueError:
        return 0.0


def load_lhb():
    files = sorted([f for f in os.listdir(DATA_DIR) if f.endswith(".json")], reverse=True) if os.path.isdir(DATA_DIR) else []
    if not files:
        return {}, None
    with open(os.path.join(DATA_DIR, files[0]), "r", encoding="utf-8") as f:
        data = json.load(f)
    return data, data.get("date", "")


def build_lhb_map():
    data, date = load_lhb()
    m = {}
    if not data:
        return m, date
    for yz_name, yz in data.get("youzi", {}).items():
        tier = yz.get("tier", "")
        for item in yz.get("buy", []):
            r = m.setdefault(item.get("code", ""), {"buys": [], "sells": []})
            r["buys"].append({"yz": yz_name, "tier": tier, "amount": parse_amount(item.get("amount", "0")), "note": item.get("note", "")})
        for item in yz.get("sell", []):
            r = m.setdefault(item.get("code", ""), {"buys": [], "sells": []})
            r["sells"].append({"yz": yz_name, "tier": tier, "amount": parse_amount(item.get("amount", "0")), "note": item.get("note", "")})
    return m, date


def score_youzi(rec):
    buys, sells = rec["buys"], rec["sells"]
    buy_total = sum(b["amount"] for b in buys)
    sell_total = sum(s["amount"] for s in sells)
    net = buy_total - sell_total
    adj = 0.0
    flags, detail = [], []
    if net > 0:
        adj += 4
        detail.append(f"净买入 {net:.0f}万")
    elif net < 0:
        adj -= 8
        flags.append("游资净卖出")
        detail.append(f"净卖出 {net:.0f}万")
    bq = sum(TIER_W.get(b["tier"], 1) for b in buys)
    if bq > 0:
        adj += min(6.0, bq)
        top = max(buys, key=lambda b: b["amount"])
        detail.append(f"买入主力 {top['yz']}({top['tier']}) {top['amount']:.0f}万")
    if any("三日榜" in (b.get("note", "") or "") for b in buys) or any("三日榜" in (s.get("note", "") or "") for s in sells):
        adj -= 3
        flags.append("三日榜(接力)")
    sq = sum(TIER_W.get(s["tier"], 1) for s in sells)
    if sq > 0 and net < 0:
        adj -= 4
        flags.append("多游资出逃")
    return round(max(-20.0, min(20.0, adj)), 1), flags, detail, round(net, 0)


def prefilter(spot):
    df = spot.copy()
    df = df[df["pure_code"].apply(lambda c: detect_board(c) == MAIN)]
    df = df[~df["名称"].str.contains("ST|退", na=False)]
    df = df[df["最新价"].between(2, 100)]
    df = df[df["成交额"] > 3e7]  # 流动性地板 3000万
    return df


def process_one(code, name, price, lhb_map, industry_map, fund_cache, val_cache):
    """单只股票：抓日线→估值分位+基本面→构造快照→打分→游资合并。失败返回 None。"""
    try:
        daily = fetch_daily(code)
        snap = build_snapshot(code, name, daily, industry=industry_map.get(code, ""))
        valuation = fetch_valuation_percentile(code, val_cache)
        fundamentals = fetch_fundamentals(code, fund_cache)
        enrich_snapshot(snap, valuation, fundamentals)
        res = engine.evaluate(snap)
        adj, flags, hd, net = 0.0, [], [], 0.0
        if code in lhb_map:
            adj, flags, hd, net = score_youzi(lhb_map[code])
        combined = max(0.0, min(100.0, res["final_score"] + adj * 0.25))
        return {
            "code": code, "name": name, "industry": snap.industry, "price": round(price, 2),
            "price_percentile": snap.price_percentile,
            "pe_percentile": snap.pe_percentile, "pb_percentile": snap.pb_percentile,
            "roe": snap.roe, "debt_ratio": snap.debt_ratio,
            "goodwill_to_equity": snap.goodwill_to_equity,
            "consecutive_profit_years": snap.consecutive_profit_years,
            "base_score": res["final_score"], "hm_adjustment": adj, "hm_net": net,
            "combined_score": round(combined, 1), "risk_level": res["risk_level"],
            "hm_flags": flags, "hm_detail": hd, "sell_triggers": res["sell_triggers"],
            "factors": {k: {"score": round(v["score"], 0), "weight": v["weight"], "detail": v["detail"]}
                        for k, v in res["factor_details"].items()},
            "deductions": res["deductions"], "deduction_total": res["deduction_total"],
            "veto": res["veto_items"], "support": res["support_levels"],
            "data_notes": res["data_notes"], "industry_note": res["industry_template_note"],
            # 技术指标（供盘中买卖点使用）
            "atr14": snap.atr14, "ma5": snap.ma5, "ma10": snap.ma10,
            "ma20": snap.ma20, "ma60": snap.ma60,
            "macd_dif": snap.macd_dif, "macd_dea": snap.macd_dea, "macd_hist": snap.macd_hist,
            "rsi14": snap.rsi14, "breakout_20d": snap.breakout_20d,
            "volume_surge": snap.volume_surge, "ma_bull": snap.ma_bull, "macd_golden": snap.macd_golden,
        }
    except Exception:
        return None


def run(n=0, top=30, out="watch_pool.json", workers=12, seed=None):
    t0 = time.time()
    print(f"[{datetime.now():%H:%M:%S}] 拉取全A实时行情...", flush=True)
    spot = fetch_spot()
    cand = prefilter(spot)
    print(f"主板初筛 {len(cand)} 只（价格2-100、成交额>3000万、非ST）", flush=True)

    # 采样：n=0 表示全部；否则均匀随机采样 n 只，避免只取高成交额热门股
    if n and 0 < n < len(cand):
        rng = random.Random(seed)
        cand = cand.sample(n=n, random_state=int(rng.random() * 1e9)).reset_index(drop=True)
        print(f"随机采样 {n} 只进行打分", flush=True)

    lhb_map, lhb_date = build_lhb_map()
    print(f"龙虎榜日期 {lhb_date}，游资股票 {len(lhb_map)} 只", flush=True)

    # 磁盘缓存：基本面(7天) / 估值分位(1天) / 行业(30天)
    fund_cache = DiskCache(FUND_CACHE_PATH)
    val_cache = DiskCache(VAL_CACHE_PATH)
    ind_cache = DiskCache(IND_CACHE_PATH)

    # 批量取行业（东财F10，稳定），用于行业负债基准
    codes = [str(r["pure_code"]).zfill(6) for _, r in cand.iterrows()]
    print(f"批量获取行业 ... {len(codes)} 只（缓存{len(ind_cache.data)}只）", flush=True)
    industry_map = fetch_industry_map(codes, ind_cache)
    print(f"行业命中 {len(industry_map)} 只", flush=True)

    rows = [(r["pure_code"], r["名称"], float(r["最新价"])) for _, r in cand.iterrows()]
    results = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(process_one, c, nm, px, lhb_map, industry_map, fund_cache, val_cache): (c, nm)
                for c, nm, px in rows}
        for fut in as_completed(futs):
            done += 1
            r = fut.result()
            if r:
                results.append(r)
            if done % 100 == 0:
                print(f"  进度 {done}/{len(rows)}", flush=True)
            if done % 200 == 0:
                fund_cache.save()
                val_cache.save()
                ind_cache.save()

    # 落盘缓存（基本面/估值分位/行业），供下次复用
    fund_cache.save()
    val_cache.save()
    ind_cache.save()

    results.sort(key=lambda x: -x["combined_score"])
    results = results[:top]
    payload = {
        "generated_at": datetime.now().isoformat(),
        "source": "sina行情+百度估值分位+新浪财务+龙虎榜游资因子",
        "lhb_date": lhb_date,
        "total": len(results),
        "stocks": results,
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[{datetime.now():%H:%M:%S}] 完成，用时 {time.time()-t0:.0f}s，输出 {out}，Top {len(results)}", flush=True)
    for r in results[:10]:
        pct = "" if r["price_percentile"] is None else f"{r['price_percentile']*100:.0f}%"
        ppct = "" if r["pe_percentile"] is None else f"PE{r['pe_percentile']*100:.0f}%"
        roe = "" if r["roe"] is None else f"ROE{r['roe']:.1f}%"
        print(f"  {r['code']} {r['name']}[{r['industry'] or '-'}] 价分位{pct} {ppct} {roe} "
              f"综合{r['combined_score']} 风险{r['risk_level']} "
              f"{'游资'+str(r['hm_net'])+'万' if r['hm_net'] else ''}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=0, help="打分样本数(0=全部)")
    ap.add_argument("--top", type=int, default=30, help="输出前N只")
    ap.add_argument("-o", type=str, default="watch_pool.json")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    run(n=a.n, top=a.top, out=a.o, workers=a.workers, seed=a.seed)
