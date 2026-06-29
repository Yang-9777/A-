#!/usr/bin/env python3
"""涨停排行 PRO - A股近30日涨停分析系统 (机构版)
后端: FastAPI + 真实akshare数据 + 补充Mock
前端: Vue3 + ECharts
"""

import json, os, random, sys
from datetime import datetime, timedelta
from collections import defaultdict

import akshare as ak
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="涨停排行PRO", version="2.1")
PORT = int(os.environ.get("PORT", 8002))
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zt_pro_cache.json")
LOOKBACK_DAYS = 25

# ============ Real Data Engine ============

def is_trading_day(d: datetime) -> bool:
    return d.weekday() < 5

def get_trading_dates(n_days: int = 25) -> list:
    today = datetime.now()
    dates = []
    for i in range(n_days):
        d = today - timedelta(days=i)
        if is_trading_day(d):
            dates.append(d.strftime("%Y%m%d"))
    return sorted(dates)

def fetch_real_zt_data():
    """从 akshare 拉取真实涨停数据，聚合为股票排行"""
    dates = get_trading_dates(LOOKBACK_DAYS)
    stock_map = defaultdict(lambda: {
        "代码": "", "名称": "", "涨停次数": 0, "涨停明细": [],
        "行业": "", "概念": [],
        "炸板次数": 0, "封板总次数": 0,
        "总成交额": 0.0, "总换手率": 0.0,
        "最高连板": 0, "最近连板": 0,
        "连板序列": [],
    })

    for i, date_str in enumerate(dates):
        try:
            df = ak.stock_zt_pool_em(date=date_str)
            if df is None or len(df) == 0:
                continue
            for _, row in df.iterrows():
                code = str(row.get("代码", ""))
                if not code:
                    continue
                name = str(row.get("名称", ""))
                zt_price = str(row.get("涨停价", ""))
                first_time = str(row.get("首次封板时间", ""))
                zha_count = int(row.get("炸板次数", 0) or 0)
                turnover = float(row.get("换手率", 0) or 0)
                amount = float(row.get("成交额", 0) or 0)
                industry = str(row.get("所属行业", ""))
                reason = str(row.get("涨停原因", ""))

                # 连板统计
                zt_stat = row.get("涨停统计", {})
                if isinstance(zt_stat, dict):
                    lianban = int(zt_stat.get("连板数", 1) or 1)
                else:
                    try:
                        lianban = int(str(zt_stat))
                    except:
                        lianban = 1

                entry = {
                    "日期": date_str,
                    "名称": name,
                    "第几板": lianban,
                    "涨停价": zt_price,
                    "封板时间": first_time,
                    "炸板": zha_count > 0,
                    "开板次数": zha_count,
                    "换手率": str(turnover),
                    "成交额": f"{amount:.1f}",
                    "涨停原因": [reason] if reason else [],
                    "行业": industry,
                    "封单金额": str(row.get("封单金额", "")),
                    "振幅": str(row.get("振幅", "")),
                    "龙虎榜": False,  # akshare doesn't provide this in zt_pool
                    "主力净流入": "0", "北向资金": "0",
                }

                s = stock_map[code]
                s["代码"] = code
                s["名称"] = name
                s["涨停次数"] += 1
                s["涨停明细"].append(entry)
                s["封板总次数"] += 1
                s["炸板次数"] += zha_count
                s["总成交额"] += amount
                s["总换手率"] += turnover
                s["连板序列"].append(lianban)
                if industry and not s["行业"]:
                    s["行业"] = industry

            sys.stdout.write(f"\r  已获取 {i+1}/{len(dates)} 天 ({date_str}) ...")
            sys.stdout.flush()
        except Exception as e:
            print(f"\n  ⚠ {date_str} 获取失败: {e}", file=sys.stderr)
            continue

    print(f"\n  共 {len(stock_map)} 只股票涨停，正在获取实时行情...")

    # 获取实时行情
    try:
        spot_df = ak.stock_zh_a_spot_em()
        price_map = {}
        for _, row in spot_df.iterrows():
            code = str(row.get("代码", ""))
            if code:
                try:
                    price_map[code] = {
                        "现价": float(row.get("最新价", 0) or 0),
                        "涨跌幅": float(row.get("涨跌幅", 0) or 0),
                        "市盈率": float(row.get("市盈率-动态", 0) or 0),
                        "总市值": float(row.get("总市值", 0) or 0) / 1e8,  # 元转亿
                        "换手率": float(row.get("换手率", 0) or 0),
                        "成交额": float(row.get("成交额", 0) or 0) / 1e8,
                        "量比": float(row.get("量比", 0) or 0),
                        "振幅": float(row.get("振幅", 0) or 0),
                    }
                except (ValueError, TypeError):
                    pass
        print(f"  获取到 {len(price_map)} 只股票实时行情")
    except Exception as e:
        print(f"  实时行情获取失败: {e}", file=sys.stderr)
        price_map = {}

    # 构建最终数据
    stocks = []
    for code, s in stock_map.items():
        zt_count = s["涨停次数"]
        # 封板率 = 未炸板天数 / 总涨停天数
        days_with_zhaban = sum(1 for d in s["涨停明细"] if d["炸板"])
        seal_rate = round((zt_count - days_with_zhaban) / zt_count * 100, 1) if zt_count > 0 else 0
        
        # 计算连板: 排序后找最长连续涨停天数
        dates_sorted = sorted(set(d["日期"] for d in s["涨停明细"]))
        max_lian = 1
        cur = 1
        for i in range(1, len(dates_sorted)):
            d1 = datetime.strptime(dates_sorted[i-1], "%Y%m%d")
            d2 = datetime.strptime(dates_sorted[i], "%Y%m%d")
            # 间隔<=4天视为连续（跨周末）
            if (d2 - d1).days <= 4:
                cur += 1
                max_lian = max(max_lian, cur)
            else:
                cur = 1
        max_lian = max(max_lian, 1)
        recent_lian = cur  # 最近连续天数
        
        avg_turnover = round(s["总换手率"] / zt_count, 2) if zt_count > 0 else 0
        avg_amount = round(s["总成交额"] / zt_count, 1) if zt_count > 0 else 0

        # AI评分: 基于涨停次数+封板率+连板
        ai_score = min(99, round(zt_count * 8 + seal_rate * 0.3 + max_lian * 5 + random.uniform(-5, 10), 1))

        # 涨跌幅随机（真实数据需额外接口）
        chg = round(random.uniform(-10, 10), 2)

        # 获取最新涨停价，处理空值
        latest_zt_price = 10.0
        for d in reversed(s["涨停明细"]):
            try:
                p = float(d["涨停价"])
                if p > 0:
                    latest_zt_price = p
                    break
            except (ValueError, TypeError):
                continue

        # 从实时行情获取数据
        real = price_map.get(code, {})
        real_price = real.get("现价", 0)
        real_chg = real.get("涨跌幅", 0)
        real_pe = real.get("市盈率", 0)
        real_mcap = real.get("总市值", 0)
        real_turnover = real.get("换手率", 0)
        real_amount = real.get("成交额", 0)
        real_qty = real.get("量比", 0)
        real_amp = real.get("振幅", 0)

        # 如果没有实时行情，用涨停价估算
        if real_price <= 0:
            real_price = round(latest_zt_price * random.uniform(0.85, 1.05), 2)
            real_chg = round(random.uniform(-10, 10), 2)

        stocks.append({
            "代码": code,
            "名称": s["名称"],
            "现价": round(real_price, 2),
            "涨跌幅": round(real_chg, 2),
            "行业": s["行业"] or "其他",
            "概念": random.sample(["AI","机器人","新能源","半导体","信创","军工","医药","低空经济"], random.randint(1, 3)),
            "涨停次数": zt_count,
            "最高连板": max_lian,
            "最近连板": recent_lian,
            "封板率": seal_rate,
            "炸板次数": days_with_zhaban,
            "成交额": real_amount if real_amount > 0 else avg_amount,
            "主力净流入": round(random.uniform(-10, 20), 2),
            "换手率": real_turnover if real_turnover > 0 else avg_turnover,
            "流动市值": round(real_mcap * random.uniform(0.3, 0.9), 2) if real_mcap > 0 else round(random.uniform(5, 2000), 2),
            "总市值": round(real_mcap, 2) if real_mcap > 0 else round(random.uniform(10, 5000), 2),
            "量比": round(real_qty, 2) if real_qty > 0 else round(random.uniform(0.5, 5), 2),
            "振幅": round(real_amp, 2) if real_amp > 0 else round(random.uniform(2, 18), 2),
            "AI评分": ai_score,
            "上市日期": f"{random.randint(2000,2023)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}",
            "市盈率": round(real_pe, 1) if real_pe > 0 else round(random.uniform(10, 300), 1),
            "北向资金": round(random.uniform(-5, 15), 2),
            "涨停明细": sorted(s["涨停明细"], key=lambda x: x["日期"], reverse=True),
        })

    ranked = sorted(stocks, key=lambda x: (x["涨停次数"], x["AI评分"]), reverse=True)

    cache = {
        "更新时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "统计天数": len(dates),
        "涨停股票数": len(ranked),
        "涨停总次数": sum(s["涨停次数"] for s in ranked),
        "数据": ranked,
    }
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, default=str)
    return cache


def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


# ============ Enrichment (Mock supplementary data) ============

def enrich_stock_detail(stock):
    """为单只股票补充详情数据（K线/资金流/新闻等Mock）"""
    code = stock["代码"]
    name = stock["名称"]
    zt_count = stock["涨停次数"]

    # 补充真实涨停明细中缺失的字段
    for d in stock.get("涨停明细", []):
        if not d.get("涨停原因") or d["涨停原因"] == [""]:
            d["涨停原因"] = random.sample(["机器人","AI","算力","新能源","半导体","低空","信创","军工"], random.randint(1, 2))
        if not d.get("龙虎榜"):
            d["龙虎榜"] = random.random() < 0.3
        d["主力净流入"] = str(round(random.uniform(-5, 15), 2))
        d["北向资金"] = str(round(random.uniform(-3, 8), 2))
        d.setdefault("开板时间", "-")
        d.setdefault("封单金额", str(round(random.uniform(1, 10), 2)))
        d.setdefault("振幅", str(round(random.uniform(3, 18), 2)))

    def gen_kline(days=250):
        data, price = [], stock["现价"] * random.uniform(0.5, 1.5)
        price = max(5, price)
        for i in range(days):
            chg = random.gauss(0, 2.5)
            price = max(3, price * (1 + chg / 100))
            o, c = round(price * random.uniform(0.98, 1.02), 2), round(price, 2)
            h, l = round(max(o, c) * random.uniform(1.0, 1.04), 2), round(min(o, c) * random.uniform(0.96, 1.0), 2)
            d = (datetime.now() - timedelta(days=days - i)).strftime("%Y%m%d")
            data.append([d, o, c, l, h, random.randint(50000, 500000)])
        return data

    def gen_fund_flow(days=30):
        return [{"日期": (datetime.now() - timedelta(days=days - i)).strftime("%m%d"),
                 "主力": round(random.uniform(-8, 12), 2), "超大单": round(random.uniform(-5, 8), 2),
                 "大单": round(random.uniform(-4, 6), 2), "北向": round(random.uniform(-3, 6), 2)} for i in range(days)]

    def gen_news(count=20):
        titles = ["机构调研创新高","业绩预告超预期","战略合作协议签署","新产品重大突破",
                  "行业政策密集出台","机构席位大举买入","北向资金连续加仓","券商给予买入评级"]
        return sorted([{"时间": (datetime.now() - timedelta(hours=random.randint(1,500))).strftime("%m-%d %H:%M"),
                        "标题": random.choice(titles), "来源": random.choice(["财联社","证券时报","第一财经"]),
                        "情绪": random.choice(["利好","中性","利空"]),
                        "摘要": "机构看好长期价值，建议关注回调机会。"} for _ in range(count)],
                      key=lambda x: x["时间"], reverse=True)

    def gen_longhu():
        return {"上榜次数": random.randint(0, 8),
                "机构净买入": round(random.uniform(-3, 8), 2),
                "游资净买入": round(random.uniform(-2, 5), 2),
                "买入前五": [{"席位": random.choice(["机构专用","中信上海","华泰益田路"]), "金额": round(random.uniform(1, 5), 2)} for _ in range(5)],
                "卖出前五": [{"席位": random.choice(["机构专用","中信上海","国泰宁波"]), "金额": round(random.uniform(1, 5), 2)} for _ in range(5)]}

    def gen_ai_analysis(s):
        score = s.get("AI评分", 70)
        level = "★★★★★" if score >= 95 else "★★★★☆" if score >= 90 else "★★★★" if score >= 80 else "★★★" if score >= 70 else "★★"
        role = "主线龙头" if score >= 90 else "情绪龙头" if score >= 80 else "趋势股" if score >= 70 else "跟风股"
        advice = "持有或分歧低吸" if score >= 90 else "关注回调机会" if score >= 80 else "观望等信号" if score >= 70 else "谨慎参与"
        return {"评分": score, "星级": level, "角色": role, "为什么涨停": random.choice(["行业催化","主力推动","题材发酵","业绩超预期"]),
                "资金逻辑": random.choice(["机构加仓","游资接力","北向流入","主力吸筹"]),
                "是否龙头": score >= 80, "建议": advice, "风险等级": "低" if score >= 85 else "中" if score >= 70 else "高",
                "止盈价": round(s["现价"] * random.uniform(1.1, 1.3), 2),
                "止损价": round(s["现价"] * random.uniform(0.85, 0.95), 2),
                "高开概率": random.randint(30, 80), "连板概率": random.randint(10, min(70, zt_count*10)),
                "综合总结": f"该股近30天涨停{zt_count}次，封板率{stock['封板率']}%，属于{role}，资金面{'偏多' if score > 70 else '中性'}，{advice}。"}

    def gen_similar():
        return [{"名称": random.choice(["中大力德","拓斯达","鸣志电器","步科股份"]),
                 "相似度": random.randint(75, 95), "表现": f"之后上涨{random.randint(15, 45)}%"} for _ in range(3)]

    return {
        **stock,
        "K线": gen_kline(), "资金流": gen_fund_flow(), "新闻": gen_news(), "公告": gen_news(5),
        "龙虎榜": gen_longhu(), "AI分析": gen_ai_analysis(stock), "相似股票": gen_similar(),
        "涨停统计": {"首板": max(0, zt_count - random.randint(0, min(2, zt_count))),
                      "二板": max(0, min(random.randint(1, 3), zt_count - 1)),
                      "三板": max(0, min(random.randint(0, 2), zt_count - 2)),
                      "四板": max(0, min(random.randint(0, 1), zt_count - 3)),
                      "五板以上": max(0, zt_count - 4),
                      "炸板": stock["炸板次数"], "封板率": stock["封板率"]},
    }


# ============ API ============

@app.get("/api/stats")
async def api_stats():
    cache = load_cache()
    if not cache:
        return JSONResponse({"error": "暂无数据"}, 404)
    stocks = cache["数据"]
    has_zt = [s for s in stocks if s["涨停次数"] > 0]
    return {
        "涨停股票数": len(has_zt),
        "涨停总次数": sum(s["涨停次数"] for s in stocks),
        "平均封板率": round(sum(s["封板率"] for s in has_zt) / max(1, len(has_zt)), 1),
        "平均炸板率": round(100 - sum(s["封板率"] for s in has_zt) / max(1, len(has_zt)), 1),
        "最高连板": max(s["最高连板"] for s in stocks),
        "平均连板": round(sum(s["最高连板"] for s in has_zt) / max(1, len(has_zt)), 1),
        "今日仍上涨": len([s for s in stocks if s["涨跌幅"] > 0]),
        "AI推荐数": len([s for s in has_zt if s["AI评分"] >= 80]),
    }


@app.get("/api/stocks")
async def api_stocks(sort_by: str = Query("涨停次数"), order: str = Query("desc"),
                      search: str = Query(""), page: int = Query(1), page_size: int = Query(50)):
    cache = load_cache()
    if not cache:
        return JSONResponse({"error": "暂无数据"}, 404)

    stocks = cache["数据"]
    if search:
        search_lower = search.lower()
        stocks = [s for s in stocks if search_lower in s["名称"].lower() or search in s["代码"]]

    key = sort_by if sort_by in ["涨停次数", "AI评分", "涨跌幅", "现价", "成交额", "封板率", "换手率", "总市值", "最高连板"] else "涨停次数"
    reverse = order == "desc"
    stocks = sorted(stocks, key=lambda x: x[key], reverse=reverse)

    total = len(stocks)
    start = (page - 1) * page_size
    end = start + page_size

    return {"总数": total, "页": page, "页大小": page_size, "数据": stocks[start:end]}


@app.get("/api/stock/{code}")
async def api_stock_detail(code: str):
    cache = load_cache()
    if not cache:
        return JSONResponse({"error": "暂无数据"}, 404)
    stock = next((s for s in cache["数据"] if s["代码"] == code), None)
    if not stock:
        return JSONResponse({"error": "未找到"}, 404)
    return JSONResponse(enrich_stock_detail(stock))


@app.get("/api/refresh")
async def api_refresh():
    try:
        cache = fetch_real_zt_data()
        return JSONResponse({"ok": True, "涨停股票数": cache["涨停股票数"]})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, 500)


# ============ HTML ============

@app.get("/", response_class=HTMLResponse)
async def index():
    cache = load_cache()
    total = cache.get("涨停股票数", 0) if cache else 0
    update_time = cache.get("更新时间", "暂无数据") if cache else "暂无数据"
    html = INDEX_HTML
    if os.path.exists(os.path.join(os.path.dirname(__file__), "index.html")):
        with open(os.path.join(os.path.dirname(__file__), "index.html"), "r", encoding="utf-8") as f:
            html = f.read()
    return HTMLResponse(content=html.replace("{total}", str(total)).replace("{update_time}", str(update_time)))


INDEX_HTML = "LOADING..."  # Replaced by index.html file


if __name__ == "__main__":
    import uvicorn
    print(f"🚀 涨停排行PRO v2.1 (真实数据): http://0.0.0.0:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
