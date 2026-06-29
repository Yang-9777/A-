#!/usr/bin/env python3
"""涨停排行 PRO - A股近30日涨停分析系统 (机构版)
后端: FastAPI + Mock数据生成
前端: Vue3 + ECharts 单页应用
"""

import json, os, random, sys, math
from datetime import datetime, timedelta
from collections import defaultdict

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="涨停排行PRO", version="2.0")
PORT = int(os.environ.get("PORT", 8002))
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

# ============ Mock Data Engine ============

INDUSTRIES = ["半导体", "人工智能", "机器人", "新能源车", "光伏", "储能", "低空经济", "算力", "数据要素",
              "生物医药", "创新药", "消费电子", "军工", "稀土永磁", "工业母机", "卫星导航", "CPO", "液冷"]

CONCEPTS = ["ChatGPT", "人形机器人", "固态电池", "飞行汽车", "量子计算", "6G", "元宇宙", "Web3",
            "数字孪生", "东数西算", "信创", "国资云", "新型工业化", "AI手机", "MR混合现实"]

STOCK_NAMES = [
    ("科大讯飞","002230"),("中科曙光","603019"),("浪潮信息","000977"),("三六零","601360"),
    ("昆仑万维","300418"),("拓尔思","300229"),("海光信息","688041"),("寒武纪","688256"),
    ("景嘉微","300474"),("中际旭创","300308"),("新易盛","300502"),("天孚通信","300394"),
    ("机器人","300024"),("绿的谐波","688017"),("埃斯顿","002747"),("汇川技术","300124"),
    ("宁德时代","300750"),("比亚迪","002594"),("阳光电源","300274"),("隆基绿能","601012"),
    ("中芯国际","688981"),("韦尔股份","603501"),("兆易创新","603986"),("北京君正","300223"),
    ("金山办公","688111"),("用友网络","600588"),("广联达","002410"),("恒生电子","600570"),
    ("工业富联","601138"),("中兴通讯","000063"),("紫光股份","000938"),("华工科技","000988"),
    ("赛力斯","601127"),("长安汽车","000625"),("中科创达","300496"),("德赛西威","002920"),
    ("药明康德","603259"),("百济神州","688235"),("恒瑞医药","600276"),("迈瑞医疗","300760"),
    ("北方华创","002371"),("中微公司","688012"),("拓荆科技","688072"),("盛美上海","688082"),
    ("沪电股份","002463"),("深南电路","002916"),("生益科技","600183"),("鹏鼎控股","002938"),
    ("航天彩虹","002389"),("中航沈飞","600760"),("中航西飞","000768"),("航发动力","600893"),
    ("万丰奥威","002085"),("中信海直","000099"),("宗申动力","001696"),("中直股份","600038"),
    ("江淮汽车","600418"),("北汽蓝谷","600733"),("上汽集团","600104"),("广汽集团","601238"),
    ("TCL科技","000100"),("京东方A","000725"),("三安光电","600703"),("华灿光电","300323"),
    ("中国软件","600536"),("中国长城","000066"),("太极股份","002368"),("电科网安","002268"),
    ("大族激光","002002"),("华中数控","300161"),("科德数控","688305"),("海天精工","601882"),
    ("剑桥科技","603083"),("联特科技","301205"),("源杰科技","688498"),("光库科技","300620"),
    ("剑桥科技","603083"),("联特科技","301205"),("源杰科技","688498"),("光库科技","300620"),
    ("拓维信息","002261"),("中科信息","300678"),("云从科技","688327"),("格灵深瞳","688207"),
    ("科大智能","300222"),("昊志机电","300503"),("丰立智能","301368"),("通力科技","301255"),
    ("金桥信息","603918"),("万兴科技","300624"),("福昕软件","688095"),("汉王科技","002362"),
    ("焦点科技","002315"),("返利科技","600228"),("若羽臣","003010"),("遥望科技","002291"),
    ("汤姆猫","300459"),("盛天网络","300494"),("三七互娱","002555"),("完美世界","002624"),
    ("中船科技","600072"),("中国船舶","600150"),("中船防务","600685"),("中国动力","600482"),
    ("中科星图","688568"),("航天宏图","688066"),("超图软件","300036"),("四维图新","002405"),
    ("人民网","603000"),("新华网","603888"),("浙数文化","600633"),("视觉中国","000681"),
    ("首都在线","300846"),("光环新网","300383"),("奥飞数据","300738"),("数据港","603881"),
] * 3  # 300+ stocks

random.seed(42)


def generate_stocks():
    stocks = []
    used_codes = set()
    for name, code in STOCK_NAMES[:300]:
        if code in used_codes:
            continue
        used_codes.add(code)
        price = round(random.uniform(5, 200), 2)
        chg = round(random.uniform(-10, 10), 2)
        zt_count = random.choices([0,1,2,3,4,5,6,7,8], weights=[10,30,25,15,10,5,3,1,1])[0]
        if zt_count == 0:
            zt_count = random.choices([0,1,2,3], weights=[5,20,5,2])[0]

        market_cap = round(price * random.uniform(1, 500), 2)  # 亿
        stocks.append({
            "代码": code,
            "名称": name,
            "现价": price,
            "涨跌幅": round(chg, 2),
            "行业": random.choice(INDUSTRIES),
            "概念": random.sample(CONCEPTS, random.randint(1, 4)),
            "涨停次数": zt_count,
            "最高连板": min(zt_count, random.randint(1, min(8, zt_count+1))),
            "最近连板": min(zt_count, random.randint(0, min(4, zt_count))),
            "封板率": round(random.uniform(60, 100), 1) if zt_count > 0 else 0,
            "炸板次数": max(0, zt_count - random.randint(0, zt_count)),
            "成交额": round(random.uniform(1, 80), 1),
            "主力净流入": round(random.uniform(-10, 20), 2),
            "换手率": round(random.uniform(1, 35), 2),
            "流通市值": round(market_cap * random.uniform(0.3, 0.9), 2),
            "总市值": round(market_cap, 2),
            "量比": round(random.uniform(0.5, 5), 2),
            "振幅": round(random.uniform(2, 18), 2),
            "AI评分": round(random.uniform(40, 99), 1) if zt_count > 0 else round(random.uniform(20, 70), 1),
            "上市日期": f"{random.randint(1995,2024)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}",
            "市盈率": round(random.uniform(10, 300), 1),
            "北向资金": round(random.uniform(-5, 15), 2),
        })
    return sorted(stocks, key=lambda x: (x["涨停次数"], x["AI评分"]), reverse=True)


def generate_zt_detail(code, name, zt_count):
    """为一只股票生成30天内的涨停记录"""
    records = []
    today = datetime.now()
    for i, day_offset in enumerate(sorted(random.sample(range(0, 22), min(zt_count, 22)))):
        d = today - timedelta(days=day_offset)
        if d.weekday() >= 5:
            d = d - timedelta(days=d.weekday() - 4)
        board_num = i + 1
        is_zhaban = random.random() < 0.15
        records.append({
            "日期": d.strftime("%Y%m%d"),
            "第几板": board_num,
            "涨停价": round(random.uniform(5, 200), 2),
            "封板时间": f"{random.randint(9,10):02d}:{random.randint(30,59):02d}",
            "开板时间": f"{random.randint(10,14):02d}:{random.randint(0,59):02d}" if is_zhaban else "-",
            "封单金额": round(random.uniform(1, 15), 2),
            "成交额": round(random.uniform(5, 60), 1),
            "换手率": round(random.uniform(2, 30), 2),
            "振幅": round(random.uniform(3, 18), 2),
            "炸板": is_zhaban,
            "开板次数": random.randint(0, 3) if is_zhaban else 0,
            "涨停原因": random.sample(["机器人", "AI", "算力", "新能源", "半导体", "低空经济", "信创", "军工", "医药"], random.randint(1, 3)),
            "龙虎榜": random.random() < 0.4,
            "主力净流入": round(random.uniform(-5, 15), 2),
            "北向资金": round(random.uniform(-3, 8), 2),
            "新闻摘要": f"【{name}】{random.choice(['业绩超预期','重大合同签署','新产品发布','行业政策利好','机构密集调研'])}，市场关注度提升",
        })
    return sorted(records, key=lambda x: x["日期"], reverse=True)


def generate_kline(days=250):
    """生成模拟K线数据"""
    data = []
    price = random.uniform(20, 100)
    for i in range(days):
        chg = random.gauss(0, 2.5)
        price = max(3, price * (1 + chg / 100))
        open_p = round(price * random.uniform(0.98, 1.02), 2)
        close_p = round(price, 2)
        high_p = round(max(open_p, close_p) * random.uniform(1.0, 1.04), 2)
        low_p = round(min(open_p, close_p) * random.uniform(0.96, 1.0), 2)
        vol = random.randint(50000, 500000)
        d = (datetime.now() - timedelta(days=days - i)).strftime("%Y%m%d")
        data.append([d, open_p, close_p, low_p, high_p, vol])
    return data


def generate_fund_flow(days=30):
    """生成资金流数据"""
    data = []
    for i in range(days):
        d = (datetime.now() - timedelta(days=days - i)).strftime("%m%d")
        data.append({
            "日期": d,
            "主力": round(random.uniform(-8, 12), 2),
            "超大单": round(random.uniform(-5, 8), 2),
            "大单": round(random.uniform(-4, 6), 2),
            "中单": round(random.uniform(-3, 4), 2),
            "小单": round(random.uniform(-3, 3), 2),
            "北向": round(random.uniform(-3, 6), 2),
        })
    return data


def generate_news(count=20):
    news_sources = ["财联社", "证券时报", "第一财经", "每日经济新闻", "21世纪经济报道", "上海证券报"]
    sentiments = ["利好", "中性", "利空"]
    news = []
    for i in range(count):
        d = (datetime.now() - timedelta(hours=random.randint(1, 720))).strftime("%m-%d %H:%M")
        s = random.choice(sentiments)
        news.append({
            "时间": d,
            "标题": random.choice([
                "机构调研频次创新高，多家券商给予买入评级",
                "行业景气度持续提升，龙头企业受益明显",
                "业绩预告超预期，净利润同比大幅增长",
                "公司与头部客户签署战略合作协议",
                "新产品获得重大突破，市场空间进一步打开",
                "行业政策密集出台，板块迎来催化",
                "机构席位大举买入，龙虎榜资金活跃",
                "北向资金连续加仓，外资看好长期价值",
            ]),
            "来源": random.choice(news_sources),
            "情绪": s,
            "摘要": "机构认为该公司基本面向好，未来增长可期，建议投资者关注回调布局机会。"
        })
    return sorted(news, key=lambda x: x["时间"], reverse=True)


def generate_longhu():
    return {
        "上榜次数": random.randint(1, 10),
        "机构净买入": round(random.uniform(-3, 8), 2),
        "游资净买入": round(random.uniform(-2, 5), 2),
        "买入前五": [
            {"席位": random.choice(["机构专用", "中信上海", "华泰益田路", "银河绍兴", "国泰宁波"]),
             "金额": round(random.uniform(1, 5), 2)}
            for _ in range(5)
        ],
        "卖出前五": [
            {"席位": random.choice(["机构专用", "中信上海", "华泰益田路", "银河绍兴", "国泰宁波"]),
             "金额": round(random.uniform(1, 5), 2)}
            for _ in range(5)
        ],
    }


def generate_ai_analysis(stock):
    score = stock.get("AI评分", 70)
    if score >= 90:
        level = "★★★★★"
        role = "主线龙头"
        advice = "建议持有或分歧低吸加仓"
        risk = "低"
    elif score >= 80:
        level = "★★★★☆"
        role = "情绪龙头"
        advice = "可关注回调机会，适度参与"
        risk = "中低"
    elif score >= 70:
        level = "★★★★"
        role = "趋势龙头"
        advice = "观望为主，等待明确信号"
        risk = "中"
    elif score >= 60:
        level = "★★★"
        role = "补涨股"
        advice = "谨慎参与，快进快出"
        risk = "中高"
    else:
        level = "★★"
        role = "跟风股"
        advice = "不建议参与"
        risk = "高"

    return {
        "评分": score,
        "星级": level,
        "角色": role,
        "为什么涨停": random.choice(["行业政策催化", "主力资金推动", "题材发酵", "业绩超预期", "消息面刺激"]),
        "资金逻辑": random.choice(["机构持续加仓", "游资接力炒作", "北向资金流入", "主力吸筹后拉升"]),
        "是否龙头": score >= 80,
        "建议": advice,
        "风险等级": risk,
        "止盈价": round(stock["现价"] * random.uniform(1.1, 1.3), 2),
        "止损价": round(stock["现价"] * random.uniform(0.85, 0.95), 2),
        "高开概率": random.randint(30, 80),
        "连板概率": random.randint(10, 70),
        "综合总结": f"该股属于{stock['行业']}{role}，近30天涨停{stock['涨停次数']}次，封板率{stock['封板率']}%，资金面{'偏多' if score>70 else '中性'}，{advice}。",
    }


def generate_comparison(stock):
    """相似股票"""
    similar = []
    for i in range(3):
        similar.append({
            "名称": random.choice(["中大力德","拓斯达","鸣志电器","步科股份","禾川科技","伟创电气"]),
            "相似度": random.randint(75, 95),
            "表现": f"之后上涨{random.randint(15, 45)}%",
        })
    return similar


# ============ API ============

STOCKS_CACHE = None

def get_stocks():
    global STOCKS_CACHE
    if STOCKS_CACHE is None:
        STOCKS_CACHE = generate_stocks()
    return STOCKS_CACHE


@app.get("/api/stats")
async def api_stats():
    stocks = get_stocks()
    total = len(stocks)
    zt_all = sum(s["涨停次数"] for s in stocks)
    has_zt = [s for s in stocks if s["涨停次数"] > 0]
    return {
        "涨停股票数": len(has_zt),
        "涨停总次数": zt_all,
        "平均封板率": round(sum(s["封板率"] for s in has_zt) / max(1, len(has_zt)), 1),
        "平均炸板率": round(100 - sum(s["封板率"] for s in has_zt) / max(1, len(has_zt)), 1),
        "最高连板": max(s["最高连板"] for s in stocks),
        "平均连板": round(sum(s["最高连板"] for s in has_zt) / max(1, len(has_zt)), 1),
        "今日仍上涨": len([s for s in stocks if s["涨跌幅"] > 0]),
        "AI推荐数": len([s for s in has_zt if s["AI评分"] >= 80]),
    }


@app.get("/api/stocks")
async def api_stocks(
    market: str = Query("全部"),
    sort_by: str = Query("涨停次数"),
    order: str = Query("desc"),
    search: str = Query(""),
    page: int = Query(1),
    page_size: int = Query(50),
):
    stocks = get_stocks()

    # Market filter (mock - all pass for now)
    # Search filter
    if search:
        stocks = [s for s in stocks if search.lower() in s["名称"].lower() or search in s["代码"]]

    # Sort
    key_map = {
        "涨停次数": "涨停次数", "AI评分": "AI评分", "涨跌幅": "涨跌幅",
        "现价": "现价", "成交额": "成交额", "封板率": "封板率",
        "换手率": "换手率", "总市值": "总市值", "最高连板": "最高连板",
    }
    key = key_map.get(sort_by, "涨停次数")
    reverse = order == "desc"
    stocks = sorted(stocks, key=lambda x: x[key], reverse=reverse)

    total = len(stocks)
    start = (page - 1) * page_size
    end = start + page_size
    page_data = stocks[start:end]

    return {
        "总数": total,
        "页": page,
        "页大小": page_size,
        "数据": page_data,
    }


@app.get("/api/stock/{code}")
async def api_stock_detail(code: str):
    stocks = get_stocks()
    stock = next((s for s in stocks if s["代码"] == code), None)
    if not stock:
        return JSONResponse({"error": "未找到"}, 404)

    zt_count = stock["涨停次数"]
    return {
        **stock,
        "涨停明细": generate_zt_detail(code, stock["名称"], zt_count),
        "K线": generate_kline(),
        "资金流": generate_fund_flow(),
        "新闻": generate_news(),
        "公告": generate_news(5),
        "龙虎榜": generate_longhu(),
        "AI分析": generate_ai_analysis(stock),
        "相似股票": generate_comparison(stock),
        "涨停统计": {
            "首板": max(0, zt_count - random.randint(0, min(2, zt_count))),
            "二板": max(0, min(random.randint(1, 3), zt_count - 1)),
            "三板": max(0, min(random.randint(0, 2), zt_count - 2)),
            "四板": max(0, min(random.randint(0, 1), zt_count - 3)),
            "五板以上": max(0, zt_count - 4),
            "炸板": stock["炸板次数"],
            "封板率": stock["封板率"],
        },
    }


# ============ HTML ============

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_CONTENT


HTML_CONTENT = open(os.path.join(os.path.dirname(__file__), "index.html"), "r", encoding="utf-8").read() if os.path.exists(os.path.join(os.path.dirname(__file__), "index.html")) else "LOADING..."


if __name__ == "__main__":
    import uvicorn
    print(f"🚀 涨停排行PRO: http://0.0.0.0:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
