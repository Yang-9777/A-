# 涨停排行榜 PRO · A股近30日涨停分析系统

机构版A股涨停分析平台，TradingView + 东方财富风格。

## 功能

- 📊 近30日涨停排行榜（300+股票Mock数据）
- 🔍 搜索/日期筛选/市场筛选
- 📋 涨停时间轴详情（每次涨停一张卡片）
- 📈 ECharts图表（涨停统计/资金分析/K线走势）
- 🤖 AI综合分析（评分/止盈止损/结论）
- 🐉 龙虎榜（机构/游资买卖席位）
- 📰 新闻/公告/相似股票

## 技术栈

- Python FastAPI（后端API + Mock数据引擎）
- Vue3（前端SPA）
- ECharts（图表）
- 深色科技风UI

## 运行

```bash
pip install fastapi uvicorn akshare
python app.py
# http://0.0.0.0:8002
```

## 接入真实数据

替换 `app.py` 中的 `generate_*` 函数为真实 akshare/tushare 接口即可，前端无需修改。
