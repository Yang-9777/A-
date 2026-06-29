# 涨停排行榜 PRO · A股近30日涨停分析系统

机构版A股涨停分析平台，TradingView + 东方财富风格。

## 功能

- 📊 近30日涨停排行榜（**真实 akshare 数据** + 实时行情）
- 🔍 搜索/日期筛选/市场筛选
- 📋 涨停时间轴详情（每次涨停一张卡片）
- 📈 ECharts图表（涨停统计/资金分析/K线走势）
- 🤖 AI综合分析（评分/止盈止损/结论）
- 🐉 龙虎榜（机构/游资买卖席位）
- 📰 新闻/公告/相似股票

## 技术栈

- Python FastAPI（后端API + **真实akshare数据引擎** + 实时行情）
- Vue3（前端SPA）
- ECharts（图表）
- 深色科技风UI

## 数据源

- **涨停数据**: `akshare.stock_zt_pool_em()` 近30日真实涨停记录
- **实时行情**: `akshare.stock_zh_a_spot_em()` 现价/涨跌幅/PE/市值/换手率
- **补充Mock**: K线/资金流/新闻/龙虎榜/AI分析

## 运行

```bash
pip install fastapi uvicorn akshare
python app.py
# http://0.0.0.0:8002
```

## 接入真实数据

涨停和行情已接入真实 akshare，其余模块（K线/资金流/新闻等）可替换 `enrich_stock_detail()` 中的 `gen_*` 函数。
