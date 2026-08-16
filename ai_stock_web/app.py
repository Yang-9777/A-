#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股选股评分 + 游资因子 决策支持 Web 看板
==========================================
- 复用 stock_scoring.py(v3评分) + stock_system.py(游资/选股/信号)
- 读取龙虎榜真实数据(默认 /opt/longhubang-server/data) 做游资因子分析
- 内置示例选股池演示完整评分(基本面数据待接入真实数据源)
- 暗色看板风格，对齐龙虎榜追踪 UI
"""

import os
import sys
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, request, render_template_string, Response

from stock_scoring import StockSnapshot, ScoringEngine, Config, detect_board, MAIN
from stock_system import HotMoneyRecord, HotMoneyAnalyzer, StockSelector, RealtimeQuote, RiskManager, SignalEngine, Account

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False


@app.after_request
def _no_cache(resp):
    """禁用缓存，确保前端每次拿到最新页面/接口。"""
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

# 龙虎榜数据目录：优先环境变量，其次同目录 data，最后 /opt/longhubang-server/data
DATA_DIR = os.environ.get("LHB_DATA_DIR") or (
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    if os.path.isdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
    else "/opt/longhubang-server/data"
)

engine = ScoringEngine()
selector = StockSelector(engine)
risk = RiskManager()
signal_engine = SignalEngine(selector, risk)

TIER_W = {"S级": 5, "A级": 3, "B级": 1}


# ======================================================================
# 龙虎榜数据解析
# ======================================================================
def parse_amount(a: str) -> float:
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


def load_lhb_data():
    """加载最新一天的龙虎榜 JSON。"""
    files = sorted([f for f in os.listdir(DATA_DIR) if f.endswith(".json")], reverse=True) \
        if os.path.isdir(DATA_DIR) else []
    if not files:
        return None
    path = os.path.join(DATA_DIR, files[0])
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def parse_lhb_records(data: dict) -> dict:
    """把龙虎榜 JSON 转成 {code: {name,sector,buys,sells}}，金额单位万元。"""
    records = {}
    for yz_name, yz in data.get("youzi", {}).items():
        tier = yz.get("tier", "")
        for item in yz.get("buy", []):
            code = item.get("code", "")
            if not code:
                continue
            r = records.setdefault(code, {"name": item.get("name", ""),
                                          "sector": item.get("sector", ""),
                                          "buys": [], "sells": []})
            r["buys"].append({"yz": yz_name, "tier": tier,
                              "amount": parse_amount(item.get("amount", "0")),
                              "note": item.get("note", "")})
        for item in yz.get("sell", []):
            code = item.get("code", "")
            if not code:
                continue
            r = records.setdefault(code, {"name": item.get("name", ""),
                                          "sector": item.get("sector", ""),
                                          "buys": [], "sells": []})
            r["sells"].append({"yz": yz_name, "tier": tier,
                               "amount": parse_amount(item.get("amount", "0")),
                               "note": item.get("note", "")})
    return records


def score_youzi(rec: dict) -> dict:
    """游资因子打分(±20)，不依赖成交额(龙虎榜数据里没有)。"""
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

    buy_quality = sum(TIER_W.get(b["tier"], 1) for b in buys)
    if buy_quality > 0:
        adj += min(6.0, buy_quality)
        top = max(buys, key=lambda b: b["amount"]) if buys else None
        if top:
            detail.append(f"买入主力：{top['yz']}({top['tier']}) {top['amount']:.0f}万")

    has_3day = any("三日榜" in (b.get("note", "") or "") for b in buys) or \
               any("三日榜" in (s.get("note", "") or "") for s in sells)
    if has_3day:
        adj -= 3
        flags.append("三日榜(接力风险)")

    sell_quality = sum(TIER_W.get(s["tier"], 1) for s in sells)
    if sell_quality > 0 and net < 0:
        adj -= 4
        flags.append("多游资出逃")

    adj = max(-20.0, min(20.0, adj))
    return {
        "net": round(net, 0),
        "buy_total": round(buy_total, 0),
        "sell_total": round(sell_total, 0),
        "adjustment": round(adj, 1),
        "flags": flags,
        "detail": detail,
        "buy_yz": [{"name": b["yz"], "tier": b["tier"], "amount": round(b["amount"], 0)} for b in buys],
        "sell_yz": [{"name": s["yz"], "tier": s["tier"], "amount": round(s["amount"], 0)} for s in sells],
    }


# ======================================================================
# 示例选股池（基本面数据为示例，用于演示完整评分；真实数据需接数据源）
# ======================================================================
def _sample_snap(code, name, industry, pe, pb, px, profit, roe, debt, goodw, chip,
                 inst_qoq, inst_hold, north, vol_ratio, ret20, vol_ann, prices, cur):
    s = StockSnapshot(code=code, name=name, industry=industry,
                      pe_percentile=pe, pb_percentile=pb, price_percentile=px,
                      consecutive_profit_years=profit, roe=roe, debt_ratio=debt,
                      goodwill_to_equity=goodw, industry_quality=60.0,
                      chip_peak=chip, low_chip_concentration=0.6,
                      high_trapped_trend="decreasing",
                      inst_count_qoq=inst_qoq, inst_holding_qoq=inst_hold,
                      inst_rising_quarters=1, north_inflow_quarters=north,
                      north_holding_change_3m=0.3, volume_ratio=vol_ratio,
                      ret_20d=ret20, annual_volatility=vol_ann, turnover_rate=3.0)
    from stock_scoring import attach_support
    return attach_support(s, prices, cur)


def build_demo_pool():
    snaps = [
        _sample_snap("600001", "低位蓝筹A(示例)", "银行", 0.15, 0.12, 0.18, 5, 12, 90, 2,
                     "low", 5, 0.6, 3, 1.6, 4, 22, [10 + i * 0.01 for i in range(60)], 10.8),
        _sample_snap("600003", "低位转强C(示例)", "半导体", 0.35, 0.40, 0.42, 3, 11, 38, 12,
                     "low", 8, 1.2, 2, 2.2, 12, 30, [30 + i * 0.06 for i in range(60)], 33.5),
        _sample_snap("600004", "周期修复D(示例)", "煤炭", 0.40, 0.45, 0.48, 2, 9, 52, 8,
                     "mid", 2, 0.4, 1, 1.2, 6, 28, [8 + i * 0.02 for i in range(60)], 8.9),
        _sample_snap("600005", "消费白马E(示例)", "食品饮料", 0.30, 0.33, 0.36, 4, 15, 33, 5,
                     "low", 4, 0.7, 2, 1.5, 5, 20, [20 + i * 0.03 for i in range(60)], 21.5),
        _sample_snap("600006", "高位题材F(示例)", "传媒", 0.92, 0.90, 0.95, -1, -5, 70, 45,
                     "high", -6, -5, -3, 6, 70, 60, [20 - i * 0.1 for i in range(60)], 15.0),
    ]
    # 给部分示例股挂上示例游资记录
    hm = {
        "600001": HotMoneyRecord("2026-08-14", "600001", "低位蓝筹A", "日换手率达15%",
                                 net_buy=8000, turnover_value=200000, float_mv=50_000_000,
                                 buy_brokers=[("机构专用", 5000), ("章盟主", 3000)],
                                 sell_brokers=[("普通营业部", 800)], consecutive_days_on_list=1),
        "600006": HotMoneyRecord("2026-08-14", "600006", "高位题材F", "连续三个交易日涨幅偏离20%",
                                 net_buy=-15000, turnover_value=300000, float_mv=8_000_000,
                                 buy_brokers=[("拉萨团结路", 6000)],
                                 sell_brokers=[("知名游资席位", 18000)], consecutive_days_on_list=4),
    }
    out = []
    for s in snaps:
        res = engine.evaluate(s)
        rec = hm.get(s.code)
        adj, flags, hd = 0.0, [], []
        if rec:
            r = selector.hm.analyze(rec)
            adj, flags, hd = r["adjustment"], r["flags"], r["detail"]
        combined = max(0.0, min(100.0, res["final_score"] + adj * 0.25))
        out.append({
            "code": s.code, "name": s.name, "industry": s.industry,
            "base_score": res["final_score"], "hm_adjustment": adj,
            "combined_score": round(combined, 1), "risk_level": res["risk_level"],
            "hm_flags": flags, "hm_detail": hd,
            "sell_triggers": res["sell_triggers"],
            "factors": {k: {"score": round(v["score"], 0), "weight": v["weight"], "detail": v["detail"]}
                        for k, v in res["factor_details"].items()},
            "deductions": res["deductions"], "deduction_total": res["deduction_total"],
            "veto": res["veto_items"], "support": res["support_levels"],
            "data_notes": res["data_notes"], "industry_note": res["industry_template_note"],
        })
    return out


DEMO_POOL = build_demo_pool()

POOL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watch_pool.json")
LLM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm_report.json")
INTRADAY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intraday_signals.json")
POSITION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "positions.json")


def load_pool():
    """优先加载真实选股结果 watch_pool.json，否则回退到示例池。"""
    if os.path.exists(POOL_PATH):
        try:
            with open(POOL_PATH, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("stocks"):
                return d, True
        except Exception:
            pass
    return {"stocks": DEMO_POOL, "total": len(DEMO_POOL),
            "source": "示例数据（未接入真实行情）", "generated_at": None, "lhb_date": None}, False


def load_llm_report():
    """加载 LLM 定性报告（可选）。"""
    if os.path.exists(LLM_PATH):
        try:
            with open(LLM_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"stocks": [], "total": 0, "model": None, "generated_at": None}


def load_intraday():
    """加载盘中监测信号（可选）。"""
    if os.path.exists(INTRADAY_PATH):
        try:
            with open(INTRADAY_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"stocks": [], "total": 0, "generated_at": None, "trading": False}


def load_positions():
    """加载持仓状态。"""
    if os.path.exists(POSITION_PATH):
        try:
            with open(POSITION_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


# ======================================================================
# 路由
# ======================================================================
@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/youzi")
def api_youzi():
    data = load_lhb_data()
    if not data:
        return jsonify({"ok": False, "msg": "未找到龙虎榜数据"})
    records = parse_lhb_records(data)
    out = []
    for code, rec in records.items():
        if detect_board(code) != MAIN:
            continue  # 只保留主板（科创/创业板在龙虎榜里很多，这里过滤显示）
        s = score_youzi(rec)
        out.append({"code": code, "name": rec["name"], "sector": rec["sector"], **s})
    out.sort(key=lambda x: (-x["net"],))
    return jsonify({"ok": True, "date": data.get("date", ""),
                    "overview": data.get("overview", {}),
                    "total": len(out), "stocks": out})


@app.route("/api/demo")
def api_demo():
    pool, is_real = load_pool()
    return jsonify({"ok": True, "is_real": is_real, **pool})


@app.route("/api/llm")
def api_llm():
    return jsonify({"ok": True, **load_llm_report()})


@app.route("/api/intraday")
def api_intraday():
    # 优先实时抓取（新浪 hq），失败则回退到上次 cron 落盘的结果
    try:
        import intraday
        d = intraday.generate(top=3)
        d["fresh"] = True
    except Exception:
        d = load_intraday()
        d["fresh"] = False
    d["positions"] = load_positions()
    return jsonify({"ok": True, **d})


@app.route("/api/portfolio")
def api_portfolio():
    """我的持仓（实时盈亏）+ 买卖提示。"""
    try:
        import intraday
        d = intraday.portfolio(top=30)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"持仓计算失败: {e}"})
    return jsonify({"ok": True, **d})


@app.route("/api/account", methods=["POST"])
def api_account():
    """设置账户现金（总金额 = 现金 + 持仓市值 由系统自动计算）。"""
    body = request.get_json(silent=True) or {}
    amount = body.get("cash")
    if amount is None:
        return jsonify({"ok": False, "msg": "缺少 cash"})
    try:
        import intraday
        acct = intraday.set_cash(amount)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"保存失败: {e}"})
    return jsonify({"ok": True, "account": acct})


@app.route("/api/diagnose/<code>")
def api_diagnose(code):
    """持仓诊断：六因子 + LLM 买卖信号。"""
    code = str(code).zfill(6)
    import intraday
    held = intraday.load_positions().get(code) or {}
    try:
        import diagnose
        d = diagnose.diagnose_with_llm(code, name=held.get("name", ""),
                                       entry_price=held.get("entry_price"),
                                       stop_loss=held.get("stop_loss"))
    except Exception as e:
        return jsonify({"ok": False, "msg": f"诊断失败: {e}"})
    return jsonify({"ok": True, "diagnosis": d})


@app.route("/api/position/enter", methods=["POST"])
def api_position_enter():
    """「标记进场」快捷买入：缺省 100 股、价格取实时。"""
    return api_position_buy()


@app.route("/api/position/buy", methods=["POST"])
def api_position_buy():
    body = request.get_json(silent=True) or {}
    code = str(body.get("code", "")).zfill(6)
    if not code:
        return jsonify({"ok": False, "msg": "缺少 code"})
    shares = body.get("shares", 100)
    price = body.get("price")
    name = body.get("name", "")
    try:
        import intraday
        pos = intraday.buy_position(code, name=name, shares=shares, entry_price=price)
    except ValueError as e:
        return jsonify({"ok": False, "msg": str(e)})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"买入失败: {e}"})
    return jsonify({"ok": True, "position": pos})


@app.route("/api/position/sell", methods=["POST"])
def api_position_sell():
    body = request.get_json(silent=True) or {}
    code = str(body.get("code", "")).zfill(6)
    if not code:
        return jsonify({"ok": False, "msg": "缺少 code"})
    shares = body.get("shares")  # None = 全部清仓
    sell_price = body.get("price")
    try:
        import intraday
        remain = intraday.sell_position(code, shares=shares, sell_price=sell_price)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"卖出失败: {e}"})
    return jsonify({"ok": True, "position": remain, "cleared": remain is None})


@app.route("/api/trades")
def api_trades():
    """历史交割单 + 复盘汇总。"""
    try:
        import intraday
        return jsonify({"ok": True, **intraday.trade_history()})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"读取交割单失败: {e}"})


@app.route("/api/trades/export")
def api_trades_export():
    """导出交割单为 CSV（Excel 可直接打开）。"""
    try:
        import intraday
        csv_text = intraday.export_trades_csv()
    except Exception as e:
        return jsonify({"ok": False, "msg": f"导出失败: {e}"})
    return Response("﻿" + csv_text, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=trades.csv"})


@app.route("/api/trades/import", methods=["POST"])
def api_trades_import():
    """导入交割单（CSV/文本粘贴）。"""
    body = request.get_json(silent=True) or {}
    text = body.get("text", "")
    mode = body.get("mode", "merge")
    try:
        import intraday
        added, buy_n, sell_n, err = intraday.import_trades(text, mode=mode)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"导入失败: {e}"})
    if err:
        return jsonify({"ok": False, "msg": err})
    return jsonify({"ok": True, "added": added, "buy": buy_n, "sell": sell_n})


@app.route("/api/review")
def api_review():
    """按股票聚合的复盘。"""
    try:
        import intraday
        return jsonify({"ok": True, **intraday.review()})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"复盘失败: {e}"})


@app.route("/api/analysis")
def api_analysis():
    """交割单分析 + 操作建议。"""
    try:
        import intraday
        return jsonify({"ok": True, **intraday.analysis()})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"分析失败: {e}"})


@app.route("/api/backtest")
def api_backtest():
    """回测结果（读缓存；可传 run=1 强制重跑）。"""
    import backtest
    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_result.json")
    force = request.args.get("run") == "1"
    if not force and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                return jsonify({"ok": True, **json.load(f)})
        except Exception:
            pass
    try:
        r = backtest.run_backtest()
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
        return jsonify({"ok": True, **r})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"回测失败: {e}"})


@app.route("/api/position/clear", methods=["POST"])
def api_position_clear():
    code = str((request.get_json(silent=True) or {}).get("code", "")).zfill(6)
    pos = load_positions()
    if code:
        pos.pop(code, None)
    else:
        pos = {}
    with open(POSITION_PATH, "w", encoding="utf-8") as f:
        json.dump(pos, f, ensure_ascii=False, indent=2)
    return jsonify({"ok": True, "positions": pos})


@app.route("/api/stock/<code>")
def api_stock(code):
    pool, _ = load_pool()
    for s in pool["stocks"]:
        if s["code"] == code:
            note = None
            for x in load_llm_report().get("stocks", []):
                if x["code"] == code:
                    note = x
                    break
            return jsonify({"ok": True, "stock": s, "llm": note})
    return jsonify({"ok": False, "msg": "未找到"})


@app.route("/health")
def health():
    data = load_lhb_data()
    return jsonify({"ok": True, "lhb_data": bool(data),
                    "data_dir": DATA_DIR, "time": datetime.now().isoformat()})


# ======================================================================
# 前端（暗色看板，对齐龙虎榜 UI）
# ======================================================================
HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>选股评分 · 游资因子看板</title>
<style>
:root{--bg-primary:#0d1117;--bg-secondary:#161b22;--bg-tertiary:#21262d;--border-color:#30363d;
--text-primary:#e6edf3;--text-secondary:#8b949e;--text-tertiary:#6e7681;
--red:#f85149;--green:#3fb950;--blue:#58a6ff;--purple:#d2a8ff;--yellow:#d29922;--orange:#ffa657;--pink:#f778ba}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg-primary);color:var(--text-primary);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;min-height:100vh;padding:20px;font-size:14px}
.container{max-width:1500px;margin:0 auto}
.header{text-align:center;padding:26px 0;border-bottom:1px solid var(--border-color);margin-bottom:24px}
.header h1{font-size:30px;font-weight:700;background:linear-gradient(135deg,var(--blue),var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.header p{color:var(--text-secondary);margin-top:8px;font-size:13px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-bottom:22px}
.card{background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:10px;padding:16px}
.card .label{color:var(--text-tertiary);font-size:12px}
.card .value{font-size:22px;font-weight:700;margin-top:6px}
.tabs{display:flex;gap:8px;margin-bottom:18px;flex-wrap:wrap}
.tab{padding:8px 18px;border:1px solid var(--border-color);border-radius:8px;cursor:pointer;color:var(--text-secondary);background:var(--bg-secondary);user-select:none}
.tab.active{background:var(--bg-tertiary);color:var(--text-primary);border-color:var(--blue)}
table{width:100%;border-collapse:collapse;background:var(--bg-secondary);border-radius:10px;overflow:hidden}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--border-color);white-space:nowrap}
th{color:var(--text-tertiary);font-weight:500;font-size:12px;position:sticky;top:0;background:var(--bg-tertiary)}
tr:hover{background:var(--bg-tertiary)}
.tag{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;margin:1px 2px}
.tag-red{background:rgba(248,81,73,.15);color:var(--red)}
.tag-green{background:rgba(63,185,80,.15);color:var(--green)}
.tag-orange{background:rgba(255,165,87,.15);color:var(--orange)}
.inp{background:var(--bg-tertiary);border:1px solid var(--border-color);border-radius:6px;color:var(--text-primary);padding:8px 10px;font-size:14px;outline:none}
.inp:focus{border-color:var(--blue)}
.inp-code{width:140px}.inp-num{width:110px}
.tag-blue{background:rgba(88,166,255,.15);color:var(--blue)}
.pos{color:var(--red);font-weight:600}
.neg{color:var(--green);font-weight:600}
.muted{color:var(--text-tertiary);font-size:12px}
.mt{margin-top:14px}
.detail-row{display:none}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;z-index:50}
.modal.open{display:flex}
.modal-box{background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:12px;max-width:760px;width:92%;max-height:85vh;overflow:auto;padding:22px}
.modal-box h3{margin-bottom:6px}
.close{float:right;cursor:pointer;color:var(--text-secondary);font-size:20px}
.factor{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border-color)}
.bar{height:6px;background:var(--bg-tertiary);border-radius:3px;margin-top:4px}
.bar span{display:block;height:6px;border-radius:3px;background:linear-gradient(90deg,var(--blue),var(--purple))}
.note{background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:8px;padding:12px;margin-top:18px;color:var(--text-secondary);font-size:12px;line-height:1.7}
@media(max-width:768px){th,td{padding:8px 6px;font-size:12px}}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>选股评分 · 游资因子看板</h1>
    <p>仅条件打分与风险提示 · 不构成投资建议 · 不自动下单 · 主力/游资数据存在欺骗性</p>
  </div>

  <div class="stats" id="stats"></div>

  <div class="tabs">
    <div class="tab active" onclick="switchTab('youzi',this)">游资动向（龙虎榜）</div>
    <div class="tab" onclick="switchTab('demo',this)">选股评分</div>
    <div class="tab" onclick="switchTab('intraday',this)">盘中监测</div>
    <div class="tab" id="tab-add" onclick="switchTab('add',this)">➕ 添加持仓</div>
    <div class="tab" id="tab-portfolio" onclick="switchTab('portfolio',this)">我的持仓</div>
    <div class="tab" id="tab-trades" onclick="switchTab('trades',this)">交割单</div>
    <div class="tab" id="tab-review" onclick="switchTab('review',this)">复盘</div>
    <div class="tab" id="tab-analysis" onclick="switchTab('analysis',this)">分析建议</div>
    <div class="tab" id="tab-backtest" onclick="switchTab('backtest',this)">回测</div>
    <div class="tab" onclick="switchTab('llm',this)">AI 定性</div>
  </div>

  <div id="view"></div>

  <div class="note">
    ⚠️ 免责声明：本结果仅为条件打分与风险提示，不是投资建议，不能直接用于交易。游资数据存在滞后性与欺骗性（拆单/换席位/对倒），
    龙虎榜为 T+1 盘后披露，跟单风险极高。市场有风险，决策需独立判断并自负盈亏。
  </div>
</div>

<div class="modal" id="modal">
  <div class="modal-box">
    <span class="close" onclick="closeModal()">×</span>
    <div id="modalBody"></div>
  </div>
</div>

<script>
let currentTab = 'youzi';
async function loadYouzi(){
  const r = await fetch('/api/youzi').then(x=>x.json());
  if(!r.ok){document.getElementById('view').innerHTML='<div class="card">未找到龙虎榜数据</div>';return}
  document.getElementById('stats').innerHTML = `
    <div class="card"><div class="label">数据日期</div><div class="value">${r.date||'-'}</div></div>
    <div class="card"><div class="label">上榜股票</div><div class="value">${r.overview.total_stocks||'-'}</div></div>
    <div class="card"><div class="label">机构净买</div><div class="value">${r.overview.jigou_net_buy||'-'}</div></div>
    <div class="card"><div class="label">热门板块</div><div class="value" style="font-size:16px">${r.overview.hot_sector||'-'}</div></div>`;
  let html = `<table><thead><tr><th>代码</th><th>名称</th><th>板块</th><th>游资净买(万)</th><th>买入主力</th><th>卖出</th><th>调整分</th><th>风险标记</th></tr></thead><tbody>`;
  for(const s of r.stocks){
    const buyMain = s.buy_yz && s.buy_yz.length ? `${s.buy_yz[0].name}(${s.buy_yz[0].tier})` : '-';
    const sellMain = s.sell_yz && s.sell_yz.length ? `${s.sell_yz[0].name}` : '-';
    const net = s.net;
    const netCls = net>=0 ? 'pos' : 'neg';
    const adjCls = s.adjustment>=0 ? 'tag-green' : 'tag-red';
    html += `<tr>
      <td class="muted">${s.code}</td><td>${s.name}</td><td class="muted">${s.sector||'-'}</td>
      <td class="${netCls}">${net>0?'+':''}${net}</td>
      <td>${buyMain}</td><td>${sellMain}</td>
      <td><span class="tag ${adjCls}">${s.adjustment>0?'+':''}${s.adjustment}</span></td>
      <td>${(s.flags||[]).map(f=>`<span class="tag tag-orange">${f}</span>`).join('')||'-'}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  document.getElementById('view').innerHTML = html;
}
async function loadDemo(){
  const r = await fetch('/api/demo').then(x=>x.json());
  const label = r.is_real ? '真实选股池' : '示例选股池';
  const src = r.source || (r.is_real ? '新浪行情+龙虎榜' : '示例数据');
  const date = r.generated_at ? r.generated_at.slice(0,16).replace('T',' ') : (r.lhb_date || '-');
  document.getElementById('stats').innerHTML = `
    <div class="card"><div class="label">${label}</div><div class="value">${r.stocks.length}</div></div>
    <div class="card"><div class="label">数据源</div><div class="value" style="font-size:13px">${src}</div></div>
    <div class="card"><div class="label">生成时间</div><div class="value" style="font-size:14px">${date}</div></div>
    <div class="card"><div class="label">说明</div><div class="value" style="font-size:12px">${r.is_real?'位置+估值分位+基本面+量价+筹码+游资':'示例数据'}</div></div>`;
  let html = `<table><thead><tr><th>代码</th><th>名称</th><th>行业</th><th>现价</th><th>价格分位</th><th>PE分位</th><th>PB分位</th><th>ROE</th><th>基础分</th><th>游资</th><th>综合分</th><th>风险</th><th></th></tr></thead><tbody>`;
  for(const s of r.stocks){
    const adjCls = s.hm_adjustment>=0 ? 'tag-green' : 'tag-red';
    const riskCls = s.risk_level==='低'?'tag-green':(s.risk_level==='中'?'tag-blue':'tag-red');
    const pct = s.price_percentile!=null ? (s.price_percentile*100).toFixed(0)+'%' : '-';
    const pctCls = s.price_percentile!=null && s.price_percentile<0.3 ? 'tag-green' : (s.price_percentile!=null && s.price_percentile>0.7 ? 'tag-red':'tag-orange');
    const pepct = s.pe_percentile!=null ? (s.pe_percentile*100).toFixed(0)+'%' : '-';
    const peCls = s.pe_percentile!=null && s.pe_percentile<0.3 ? 'tag-green' : (s.pe_percentile!=null && s.pe_percentile>0.7 ? 'tag-red':'tag-orange');
    const pb = s.pb_percentile!=null ? (s.pb_percentile*100).toFixed(0)+'%' : '-';
    const pbCls = s.pb_percentile!=null && s.pb_percentile<0.3 ? 'tag-green' : (s.pb_percentile!=null && s.pb_percentile>0.7 ? 'tag-red':'tag-orange');
    const roe = s.roe!=null ? (s.roe>0?'+':'')+s.roe.toFixed(1)+'%' : '-';
    html += `<tr>
      <td class="muted">${s.code}</td><td>${s.name}</td>
      <td class="muted">${s.industry||'-'}</td>
      <td>${s.price!=null?s.price:'-'}</td>
      <td><span class="tag ${pctCls}">${pct}</span></td>
      <td><span class="tag ${peCls}">${pepct}</span></td>
      <td><span class="tag ${pbCls}">${pb}</span></td>
      <td>${roe}</td>
      <td>${s.base_score}</td>
      <td><span class="tag ${adjCls}">${s.hm_adjustment>0?'+':''}${s.hm_adjustment}</span></td>
      <td><b>${s.combined_score}</b></td>
      <td><span class="tag ${riskCls}">${s.risk_level}</span></td>
      <td><span class="tag tag-blue" style="cursor:pointer" onclick="showDetail('${s.code}')">详情</span></td>
    </tr>`;
  }
  html += '</tbody></table>';
  document.getElementById('view').innerHTML = html;
}
async function loadIntraday(){
  const r = await fetch('/api/intraday').then(x=>x.json());
  const date = r.generated_at ? r.generated_at.slice(0,19).replace('T',' ') : '-';
  const fresh = r.fresh ? '<span class="tag tag-green">实时</span>' : '<span class="tag tag-orange">缓存</span>';
  const trading = r.trading ? '<span class="tag tag-green">交易中</span>' : '<span class="tag tag-orange">非交易时段</span>';
  document.getElementById('stats').innerHTML = `
    <div class="card"><div class="label">盘中监测</div><div class="value">${r.total||0} 只</div></div>
    <div class="card"><div class="label">数据</div><div class="value" style="font-size:14px">${fresh}</div></div>
    <div class="card"><div class="label">状态</div><div class="value" style="font-size:14px">${trading}</div></div>
    <div class="card"><div class="label">更新时间</div><div class="value" style="font-size:13px">${date}</div></div>`;
  let html = `<div class="muted" style="margin-bottom:10px">🔴 价格每 3 秒自动刷新 · 进场/出场为时机参考，不自动下单</div>`;
  html += `<table><thead><tr><th>代码</th><th>名称</th><th>现价</th><th>涨跌</th><th>信号</th><th>止损</th><th>止盈</th><th>仓位</th><th></th></tr></thead><tbody>`;
  for(const s of r.stocks){
    const q = s.quote||{};
    const chg = (q.price!=null && q.pre_close) ? ((q.price/q.pre_close-1)*100) : null;
    const chgCls = chg==null?'muted':(chg>0?'pos':'neg');
    const sig = s.signal||'-';
    const sigCls = sig==='进场'?'tag-green':(sig==='观望'?'tag-orange':(sig==='出场'?'tag-red':'tag-blue'));
    const since = s.signal_since ? s.signal_since.slice(11,16) : '';
    const held = r.positions && r.positions[s.code];
    const btn = held
      ? `<span class="tag tag-blue" style="cursor:pointer" onclick="clearPos('${s.code}')">已进场·清除</span>`
      : (sig==='进场' ? `<span class="tag tag-green" style="cursor:pointer" onclick="enterPos('${s.code}')">标记进场</span>` : '');
    html += `<tr>
      <td class="muted">${s.code}</td><td>${s.name}</td>
      <td><b>${q.price!=null?q.price:'-'}</b></td>
      <td class="${chgCls}">${chg!=null?(chg>0?'+':'')+chg.toFixed(2)+'%':'-'}</td>
      <td><span class="tag ${sigCls}">${sig}</span>${since?' <span class="muted">'+since+'起</span>':''}</td>
      <td>${s.stop_loss!=null?s.stop_loss:'-'}</td>
      <td>${s.take_profit!=null?s.take_profit:'-'}</td>
      <td>${s.position_pct!=null?(s.position_pct*100).toFixed(0)+'%':'-'}</td>
      <td>${btn}</td>
    </tr>`;
    if(s.reasons && s.reasons.length){
      html += `<tr class="detail-row"><td></td><td colspan="8" class="muted">${s.reasons.map(x=>'· '+x).join('<br>')}</td></tr>`;
    }
  }
  html += '</tbody></table>';
  if(r.history && r.history.length){
    html += `<div class="mt"><b>信号切换记录</b></div>`;
    html += `<table><thead><tr><th>时间</th><th>代码</th><th>名称</th><th>切换</th><th>现价</th></tr></thead><tbody>`;
    for(const h of r.history){
      const from = h.from||'—';
      const cls = h.to==='进场'?'tag-green':(h.to==='出场'?'tag-red':'tag-orange');
      html += `<tr>
        <td class="muted">${(h.at||'').slice(5,16).replace('T',' ')}</td>
        <td class="muted">${h.code}</td><td>${h.name}</td>
        <td><span class="tag tag-blue">${from}</span> → <span class="tag ${cls}">${h.to}</span></td>
        <td>${h.price!=null?h.price:'-'}</td>
      </tr>`;
    }
    html += '</tbody></table>';
  }
  if(!r.stocks.length) html = '<div class="card">暂无盘中信号（等待每日选股结果）</div>';
  document.getElementById('view').innerHTML = html;
}
async function enterPos(code){
  const r = await fetch('/api/position/enter',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})}).then(x=>x.json());
  if(r.ok) loadIntraday();
}
async function clearPos(code){
  const r = await fetch('/api/position/clear',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})}).then(x=>x.json());
  if(r.ok) loadIntraday();
}
function fmtMoney(v){
  if(v==null) return '-';
  const n = Number(v);
  const abs = Math.abs(n);
  if(abs>=1e8) return (n/1e8).toFixed(2)+'亿';
  if(abs>=1e4) return (n/1e4).toFixed(2)+'万';
  return n.toFixed(0);
}
async function loadPortfolio(){
  const r = await fetch('/api/portfolio').then(x=>x.json());
  if(!r.ok){document.getElementById('view').innerHTML='<div class="card">'+ (r.msg||'加载失败') +'</div>';return}
  const sm = r.summary||{};
  const sumPnlCls = (sm.total_pnl||0)>=0?'pos':'neg';
  document.getElementById('stats').innerHTML = `
    <div class="card"><div class="label">现金</div><div class="value" style="font-size:16px">${fmtMoney(sm.cash)}</div></div>
    <div class="card"><div class="label">持仓市值</div><div class="value" style="font-size:16px">${fmtMoney(sm.total_market_value)}</div></div>
    <div class="card"><div class="label">总金额(总资产)</div><div class="value" style="font-size:16px">${fmtMoney(sm.total_assets)}</div></div>
    <div class="card"><div class="label">仓位比例</div><div class="value" style="font-size:16px">${(sm.position_ratio*100).toFixed(1)}%</div></div>
    <div class="card"><div class="label">浮动盈亏</div><div class="value ${sumPnlCls}" style="font-size:16px">${(sm.total_pnl||0)>=0?'+':''}${fmtMoney(sm.total_pnl)} (${(sm.total_pnl_pct||0)>=0?'+':''}${(sm.total_pnl_pct||0).toFixed(2)}%)</div></div>`;
  let html = `<div class="muted" style="margin-bottom:10px">💰 持仓盈亏每 5 秒自动刷新 · 买卖提示仅供时机参考，不自动下单 · <span class="tag tag-green" style="cursor:pointer;font-size:13px;padding:4px 12px" onclick="switchTab('add', document.getElementById('tab-add'))">➕ 添加持仓</span> <span class="tag tag-blue" style="cursor:pointer;font-size:13px;padding:4px 12px" onclick="switchTab('add', document.getElementById('tab-add'))">💰 设置现金</span></div>`;
  html += `<table><thead><tr><th>代码</th><th>名称</th><th>股数</th><th>成本</th><th>现价</th><th>市值</th><th>盈亏</th><th>止损</th><th>止盈</th><th>提示</th><th></th></tr></thead><tbody>`;
  for(const h of r.holdings||[]){
    const hPnlCls = h.pnl>=0?'pos':'neg';
    const sigCls = h.signal==='出场'?'tag-red':(h.signal==='持有'?'tag-blue':'tag-green');
    html += `<tr>
      <td class="muted">${h.code}</td><td>${h.name}</td>
      <td>${h.shares}</td>
      <td>${h.entry_price!=null?h.entry_price:'-'}</td>
      <td><b>${h.price!=null?h.price:'-'}</b></td>
      <td>${fmtMoney(h.market_value)}</td>
      <td class="${hPnlCls}">${h.pnl>=0?'+':''}${fmtMoney(h.pnl)} (${h.pnl_pct>=0?'+':''}${h.pnl_pct.toFixed(2)}%)</td>
      <td>${h.stop_loss!=null?h.stop_loss:'-'}</td>
      <td>${h.take_profit!=null?h.take_profit:'-'}</td>
      <td><span class="tag ${sigCls}">${h.signal}</span></td>
      <td>
        <span class="tag tag-blue" style="cursor:pointer" onclick="diagPos('${h.code}')">诊断</span>
        <span class="tag tag-green" style="cursor:pointer" onclick="buyPos('${h.code}')">加仓</span>
        <span class="tag tag-red" style="cursor:pointer" onclick="sellPos('${h.code}', this)">卖出</span>
      </td>
    </tr>`;
    if(h.reasons && h.reasons.length){
      html += `<tr class="detail-row"><td></td><td colspan="11" class="muted">${h.reasons.map(x=>'· '+x).join('<br>')}</td></tr>`;
    }
  }
  html += '</tbody></table>';
  if(!(r.holdings||[]).length) html = '<div class="card">暂无持仓（在下方候选股点「买入」记录）</div>';
  if(r.candidates && r.candidates.length){
    html += `<div class="mt"><b>买入提示（选股池候选）</b></div>`;
    html += `<table><thead><tr><th>代码</th><th>名称</th><th>行业</th><th>现价</th><th>综合分</th><th>建议仓位</th><th>止损</th><th>止盈</th><th>信号</th><th></th></tr></thead><tbody>`;
    for(const c of r.candidates){
      const cSigCls = c.signal==='进场'?'tag-green':(c.signal==='观望'?'tag-orange':'tag-red');
      html += `<tr>
        <td class="muted">${c.code}</td><td>${c.name}</td>
        <td class="muted">${c.industry||'-'}</td>
        <td><b>${c.price!=null?c.price:'-'}</b></td>
        <td>${c.combined_score!=null?c.combined_score:'-'}</td>
        <td>${c.position_pct!=null?(c.position_pct*100).toFixed(0)+'%':'-'}</td>
        <td>${c.stop_loss!=null?c.stop_loss:'-'}</td>
        <td>${c.take_profit!=null?c.take_profit:'-'}</td>
        <td><span class="tag ${cSigCls}">${c.signal}</span></td>
        <td>${c.signal==='进场'?`<span class="tag tag-green" style="cursor:pointer" onclick="buyPos('${c.code}')">买入</span>`:''}</td>
      </tr>`;
      if(c.reasons && c.reasons.length){
        html += `<tr class="detail-row"><td></td><td colspan="10" class="muted">${c.reasons.map(x=>'· '+x).join('<br>')}</td></tr>`;
      }
    }
    html += '</tbody></table>';
  }
  document.getElementById('view').innerHTML = html;
}
async function loadAddForm(){
  let cash = 0;
  try{ const r = await fetch('/api/portfolio').then(x=>x.json()); cash = (r.summary||{}).cash||0; }catch(e){}
  document.getElementById('stats').innerHTML = `
    <div class="card"><div class="label">添加持仓</div><div class="value" style="font-size:16px">现金 + 持仓</div></div>
    <div class="card"><div class="label">说明</div><div class="value" style="font-size:13px">现金 / 代码 / 数量 / 买入价</div></div>`;
  document.getElementById('view').innerHTML = `
    <div class="card" style="max-width:560px">
      <div style="font-size:17px;font-weight:700;margin-bottom:14px">💰 账户现金</div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <div class="muted" style="white-space:nowrap">现金</div>
        <input class="inp" id="cashInput" type="number" step="0.01" min="0" value="${cash||''}" placeholder="如 500000" style="flex:1;min-width:180px">
        <span class="tag tag-green" style="cursor:pointer;font-size:14px;padding:8px 16px" onclick="saveCash()">保存</span>
      </div>
      <div class="muted" style="margin-top:8px">总金额（总资产）= 现金 + 持仓市值，由系统自动计算</div>

      <hr style="border:none;border-top:1px solid var(--border-color);margin:22px 0">

      <div style="font-size:17px;font-weight:700;margin-bottom:16px">📝 添加我的持仓</div>
      <div style="display:flex;flex-direction:column;gap:14px">
        <div>
          <div class="muted" style="margin-bottom:6px">股票代码</div>
          <input class="inp" id="addCode" placeholder="如 600519 / 002690" style="width:100%">
        </div>
        <div>
          <div class="muted" style="margin-bottom:6px">数量（股）</div>
          <input class="inp" id="addShares" type="number" min="1" placeholder="如 1000" style="width:100%">
        </div>
        <div>
          <div class="muted" style="margin-bottom:6px">买入价格（元，选填，留空自动取实时价）</div>
          <input class="inp" id="addPrice" type="number" step="0.01" placeholder="如 15.25" style="width:100%">
        </div>
        <div class="tag tag-green" style="cursor:pointer;font-size:16px;padding:12px 0;text-align:center" onclick="submitAdd()">＋ 添加持仓</div>
        <div class="muted">添加后自动跳转到「我的持仓」，按实时行情显示盈亏比、盈亏金额和「卖出 / 继续持有」信号</div>
      </div>
    </div>`;
}
async function saveCash(){
  const v = document.getElementById('cashInput').value;
  const n = parseFloat(v);
  if(v==='' || isNaN(n) || n<0){ document.getElementById('cashInput').focus(); return; }
  const r = await fetch('/api/account',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cash:n})}).then(x=>x.json());
  if(!r.ok){ alert(r.msg||'保存失败'); return; }
  loadAddForm();
}
function fillAddForm(code){
  switchTab('add', document.getElementById('tab-add'));
  document.getElementById('addCode').value = code || '';
  document.getElementById('addShares').value = '';
  document.getElementById('addPrice').value = '';
  document.getElementById('addShares').focus();
}
async function submitAdd(){
  const code = document.getElementById('addCode').value.trim();
  const shares = parseInt(document.getElementById('addShares').value);
  const price = document.getElementById('addPrice').value.trim();
  if(!code || !shares || shares<=0){ alert('请填写有效的代码和股数'); return; }
  const body = {code, shares};
  if(price) body.price = parseFloat(price);
  const r = await fetch('/api/position/buy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(x=>x.json());
  if(r.ok){
    switchTab('portfolio', document.getElementById('tab-portfolio'));
    loadPortfolio();
  } else {
    alert(r.msg||'添加失败');
  }
}
async function buyPos(code){ fillAddForm(code); }
async function addPos(code){ fillAddForm(code); }
async function addManual(){ fillAddForm(''); }
async function sellPos(code, btn){
  if(!btn) btn = {dataset:{}, textContent:''};
  if(btn.dataset.confirm!=='1'){
    btn.dataset.confirm='1';
    btn.textContent='确认卖出?';
    setTimeout(()=>{ btn.dataset.confirm=''; btn.textContent='卖出'; }, 3000);
    return;
  }
  const r = await fetch('/api/position/sell',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})}).then(x=>x.json());
  if(r.ok) loadPortfolio(); else alert(r.msg||'卖出失败');
}
async function diagPos(code){
  document.getElementById('modalBody').innerHTML = '<h3>诊断中…</h3><div class="muted">正在抓取行情、基本面、估值分位并调用 AI 分析，约 10-30 秒，请稍候</div>';
  document.getElementById('modal').classList.add('open');
  try{
    const r = await fetch('/api/diagnose/'+code).then(x=>x.json());
    if(!r.ok){ document.getElementById('modalBody').innerHTML = '<h3>诊断失败</h3><div class="muted">'+ (r.msg||'') +'</div>'; return; }
    renderDiagnosis(r.diagnosis);
  }catch(e){
    document.getElementById('modalBody').innerHTML = '<h3>诊断失败</h3><div class="muted">网络错误</div>';
  }
}
function renderDiagnosis(d){
  const pct = v => (v==null?'-':v+'%');
  const sig = d.signal||'-';
  const sigCls = sig.indexOf('卖')>=0 ? 'tag-red' : (sig.indexOf('加')>=0 || sig.indexOf('买')>=0 ? 'tag-green' : 'tag-blue');
  const upCls = d.upside==='高'?'tag-green':(d.upside==='低'?'tag-red':'tag-orange');
  let factors = '';
  for(const [k,v] of Object.entries(d.factor_details||{})){
    factors += `<div class="factor"><span>${k} <span class="muted">(权重${v.weight}%)</span></span><b>${v.score}</b></div>`;
  }
  const ded = (d.deductions||[]).length ? (d.deductions||[]).map(x=>`<div class="muted">✗ ${x}</div>`).join('') : '<div class="muted">无</div>';
  const trig = (d.sell_triggers||[]).length ? (d.sell_triggers||[]).map(t=>`<div class="muted">⚠ ${t}</div>`).join('') : '<div class="muted">未触发</div>';
  const sup = Object.entries(d.support||{}).map(([k,v])=>`<div class="muted">· ${k}: ${v.toFixed(2)}</div>`).join('') || '<div class="muted">无</div>';
  document.getElementById('modalBody').innerHTML = `
    <h3>${d.code} ${d.name} <span class="muted" style="font-size:13px">${d.industry||'-'}</span></h3>
    <div class="muted" style="margin:6px 0">综合分 <b>${d.score}</b> · 风险 <b>${d.risk_level}</b> · 现价 <b>${d.price!=null?d.price:'-'}</b></div>
    <div style="margin:10px 0">
      <span class="tag ${upCls}">上涨可能性 ${d.upside||'-'}</span>
      <span class="tag ${sigCls}" style="font-size:13px">信号：${sig}</span>
      ${d.suggest_stop!=null?`<span class="tag tag-orange">建议止损 ${d.suggest_stop}</span>`:''}
    </div>
    ${d.upside_reason?`<div class="muted" style="margin-bottom:6px">📈 ${d.upside_reason}</div>`:''}
    ${(d.reasons||[]).map(x=>`<div class="muted" style="margin-bottom:2px">· ${x}</div>`).join('')}
    ${d.risk?`<div class="muted" style="margin-top:6px">⚠️ 风险：${d.risk}</div>`:''}
    <div class="mt"><b>估值分位</b><div class="muted">价格 ${pct(d.price_percentile)} · PE ${pct(d.pe_percentile)} · PB ${pct(d.pb_percentile)}</div></div>
    <div class="mt"><b>基本面</b><div class="muted">ROE ${d.roe!=null?d.roe+'%':'-'} · 负债率 ${d.debt_ratio!=null?d.debt_ratio+'%':'-'} · 商誉/净资产 ${d.goodwill_to_equity!=null?d.goodwill_to_equity+'%':'-'} · 连续盈利 ${d.consecutive_profit_years!=null?d.consecutive_profit_years+'年':'-'}</div></div>
    <div class="mt"><b>情绪/量价</b><div class="muted">量比 ${d.volume_ratio!=null?d.volume_ratio.toFixed(2):'-'} · 近20日 ${d.ret_20d!=null?d.ret_20d.toFixed(1)+'%':'-'} · 换手率 ${d.turnover_rate!=null?d.turnover_rate.toFixed(1)+'%':'-'} · 年化波动 ${d.annual_volatility!=null?d.annual_volatility.toFixed(0)+'%':'-'}</div></div>
    <div class="mt"><b>技术指标</b><div class="muted">ATR ${d.atr14!=null?d.atr14.toFixed(2):'-'} · MA20 ${d.ma20!=null?d.ma20.toFixed(2):'-'} · MA60 ${d.ma60!=null?d.ma60.toFixed(2):'-'} · RSI ${d.rsi14!=null?d.rsi14.toFixed(0):'-'}</div>
      <div class="muted">${d.ma_bull?'✅ 均线多头 ':'均线非多头 '}${d.macd_golden?'✅ MACD金叉 ':'MACD未金叉 '}${d.breakout_20d?'✅ 突破20日新高':'未突破'}${d.volume_surge?' · 放量':''}</div></div>
    <div class="mt"><b>筹码</b><div class="muted">筹码峰 ${d.chip_peak||'-'} · 套牢盘 ${d.trapped_ratio!=null?d.trapped_ratio+'%':'-'}</div></div>
    <div class="mt"><b>六因子明细</b>${factors}</div>
    <div class="mt"><b>扣分项（合计 -${d.deduction_total||0}）</b>${ded}</div>
    <div class="mt"><b>卖出触发</b>${trig}</div>
    <div class="mt"><b>防守支撑位</b>${sup}</div>
    ${d.industry_note?`<div class="muted mt">${d.industry_note}</div>`:''}
    <div class="muted mt">⚠️ 以上仅为数据化分析与 AI 定性，不构成投资建议，不自动下单。</div>
  `;
}
async function loadTrades(){
  const r = await fetch('/api/trades').then(x=>x.json());
  if(!r.ok){document.getElementById('view').innerHTML='<div class="card">'+ (r.msg||'加载失败') +'</div>';return}
  const sm = r.summary||{};
  const sumPnlCls = (sm.realized_pnl||0)>=0?'pos':'neg';
  document.getElementById('stats').innerHTML = `
    <div class="card"><div class="label">交易笔数</div><div class="value">${sm.count||0}</div></div>
    <div class="card"><div class="label">买入/卖出</div><div class="value" style="font-size:16px">${sm.buy_count||0} / ${sm.sell_count||0}</div></div>
    <div class="card"><div class="label">已实现盈亏</div><div class="value ${sumPnlCls}" style="font-size:16px">${(sm.realized_pnl||0)>=0?'+':''}${fmtMoney(sm.realized_pnl)}</div></div>
    <div class="card"><div class="label">胜率</div><div class="value" style="font-size:16px">${sm.win_rate||0}%</div></div>
    <div class="card"><div class="label">总买入/总卖出</div><div class="value" style="font-size:13px">${fmtMoney(sm.total_buy)} / ${fmtMoney(sm.total_sell)}</div></div>`;
  let html = `<div style="margin-bottom:10px">
      <span class="tag tag-green" style="cursor:pointer;font-size:13px;padding:6px 14px" onclick="openImport()">📥 导入交割单</span>
      <a class="tag tag-blue" style="cursor:pointer;font-size:13px;padding:6px 14px;text-decoration:none" href="/api/trades/export">📤 导出CSV</a>
    </div>`;
  html += `<table><thead><tr><th>时间</th><th>代码</th><th>名称</th><th>方向</th><th>数量</th><th>价格</th><th>金额</th><th>已实现盈亏</th><th>盈亏比</th><th>备注</th></tr></thead><tbody>`;
  for(const t of r.trades||[]){
    const act = t.action==='buy'?'买入':'卖出';
    const actCls = t.action==='buy'?'tag-red':'tag-green';
    const tPnlCls = t.pnl==null?'muted':(t.pnl>=0?'pos':'neg');
    html += `<tr>
      <td class="muted">${(t.time||'').slice(0,16).replace('T',' ')}</td>
      <td class="muted">${t.code}</td><td>${t.name||'-'}</td>
      <td><span class="tag ${actCls}">${act}</span></td>
      <td>${t.shares}</td>
      <td>${t.price!=null?t.price:'-'}</td>
      <td>${fmtMoney(t.amount)}</td>
      <td class="${tPnlCls}">${t.pnl==null?'-':((t.pnl>=0?'+':'')+fmtMoney(t.pnl))}</td>
      <td class="${tPnlCls}">${t.pnl_pct==null?'-':((t.pnl_pct>=0?'+':'')+t.pnl_pct.toFixed(2)+'%')}</td>
      <td class="muted">${t.note||'-'}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  if(!(r.trades||[]).length) html = '<div class="card">暂无交割单（买入/卖出后自动记录，或点「导入交割单」）</div>';
  document.getElementById('view').innerHTML = html;
}
function openImport(){
  document.getElementById('modalBody').innerHTML = `
    <h3>📥 导入交割单</h3>
    <div class="muted" style="margin:8px 0">粘贴券商导出的 CSV/文本，需包含「代码、方向(买卖)、数量、价格」列（时间、名称可选）。</div>
    <textarea id="importText" style="width:100%;height:160px;background:var(--bg-tertiary);border:1px solid var(--border-color);border-radius:6px;color:var(--text-primary);padding:10px;font-size:13px;font-family:monospace" placeholder="代码,名称,方向,数量,价格,时间&#10;603466,风语筑,买入,1400,13.88,2026-08-16"></textarea>
    <div style="margin-top:10px;display:flex;gap:8px;align-items:center">
      <input type="file" id="importFile" accept=".csv,.txt" style="color:var(--text-secondary)">
      <span class="tag tag-green" style="cursor:pointer;font-size:14px;padding:8px 16px" onclick="doImport()">导入</span>
    </div>
    <div class="muted" style="margin-top:8px">导入后自动合并去重，按时间重算已实现盈亏并重建当前持仓。</div>
  `;
  document.getElementById('modal').classList.add('open');
  document.getElementById('importFile').addEventListener('change', function(e){
    const f = e.target.files[0];
    if(!f) return;
    const rd = new FileReader();
    rd.onload = function(){ document.getElementById('importText').value = rd.result; };
    rd.readAsText(f, 'UTF-8');
  });
}
async function doImport(){
  const text = document.getElementById('importText').value.trim();
  if(!text){ alert('请粘贴或选择要导入的内容'); return; }
  const r = await fetch('/api/trades/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})}).then(x=>x.json());
  if(!r.ok){ alert(r.msg||'导入失败'); return; }
  document.getElementById('modal').classList.remove('open');
  loadTrades();
}
async function loadReview(){
  const r = await fetch('/api/review').then(x=>x.json());
  if(!r.ok){document.getElementById('view').innerHTML='<div class="card">'+ (r.msg||'加载失败') +'</div>';return}
  const sm = r.summary||{};
  const sumPnlCls = (sm.total_pnl||0)>=0?'pos':'neg';
  document.getElementById('stats').innerHTML = `
    <div class="card"><div class="label">交易股票数</div><div class="value">${sm.stock_count||0}</div></div>
    <div class="card"><div class="label">已实现盈亏</div><div class="value" style="font-size:16px">${(sm.total_realized_pnl||0)>=0?'+':''}${fmtMoney(sm.total_realized_pnl)}</div></div>
    <div class="card"><div class="label">浮动盈亏</div><div class="value" style="font-size:16px">${(sm.total_floating_pnl||0)>=0?'+':''}${fmtMoney(sm.total_floating_pnl)}</div></div>
    <div class="card"><div class="label">总盈亏</div><div class="value ${sumPnlCls}" style="font-size:16px">${(sm.total_pnl||0)>=0?'+':''}${fmtMoney(sm.total_pnl)}</div></div>
    <div class="card"><div class="label">盈利票数/胜率</div><div class="value" style="font-size:16px">${sm.win_stocks||0} / ${sm.win_rate||0}%</div></div>`;
  let html = `<table><thead><tr><th>代码</th><th>名称</th><th>交易笔数</th><th>累计买入</th><th>累计卖出</th><th>已实现盈亏</th><th>当前持仓</th><th>浮盈亏</th><th>总盈亏</th></tr></thead><tbody>`;
  for(const s of r.stocks||[]){
    const tpCls = s.total_pnl>=0?'pos':'neg';
    const fpCls = s.floating_pnl>=0?'pos':'neg';
    const rpCls = s.realized_pnl>=0?'pos':'neg';
    html += `<tr>
      <td class="muted">${s.code}</td><td>${s.name}</td>
      <td>${s.trade_count}</td>
      <td>${fmtMoney(s.buy_amount)}</td>
      <td>${fmtMoney(s.sell_amount)}</td>
      <td class="${rpCls}">${s.realized_pnl>=0?'+':''}${fmtMoney(s.realized_pnl)}</td>
      <td>${s.current_shares>0?`<b>${s.current_shares}股</b>`:'已清仓'}</td>
      <td class="${fpCls}">${s.current_shares>0?((s.floating_pnl>=0?'+':'')+fmtMoney(s.floating_pnl)+' ('+s.floating_pnl_pct.toFixed(2)+'%)'):'-'}</td>
      <td class="${tpCls}"><b>${s.total_pnl>=0?'+':''}${fmtMoney(s.total_pnl)}</b></td>
    </tr>`;
  }
  html += '</tbody></table>';
  if(!(r.stocks||[]).length) html = '<div class="card">暂无复盘数据（先有交易记录）</div>';
  document.getElementById('view').innerHTML = html;
}
async function loadAnalysis(){
  const r = await fetch('/api/analysis').then(x=>x.json());
  if(!r.ok){document.getElementById('view').innerHTML='<div class="card">'+ (r.msg||'加载失败') +'</div>';return}
  if(r.error){document.getElementById('view').innerHTML='<div class="card">'+r.error+'</div>';return}
  const m = r.metrics||{};
  const pnlCls = m.realized_pnl>=0?'pos':'neg';
  document.getElementById('stats').innerHTML = `
    <div class="card"><div class="label">交易笔数</div><div class="value">${m.trade_count||0}</div></div>
    <div class="card"><div class="label">月均交易</div><div class="value">${m.avg_per_month||0} 笔</div></div>
    <div class="card"><div class="label">已实现盈亏</div><div class="value ${pnlCls}">${(m.realized_pnl||0)>=0?'+':''}${fmtMoney(m.realized_pnl)}</div></div>
    <div class="card"><div class="label">胜率</div><div class="value">${m.win_rate_trade||0}%</div></div>
    <div class="card"><div class="label">盈亏比(盈/亏)</div><div class="value">${m.profit_factor||0}</div></div>
    <div class="card"><div class="label">手续费估算</div><div class="value" style="font-size:16px">${fmtMoney(m.fee_est)}</div></div>`;
  let html = '';
  if(r.problems && r.problems.length){
    html += `<div class="card" style="margin-bottom:14px"><div style="font-size:16px;font-weight:700;margin-bottom:10px">🔍 问题诊断</div>`;
    for(const p of r.problems) html += `<div class="muted" style="margin:6px 0">✗ ${p}</div>`;
    html += `</div>`;
  }
  if(r.suggestions && r.suggestions.length){
    html += `<div class="card" style="margin-bottom:14px;border-color:rgba(63,185,80,.4)"><div style="font-size:16px;font-weight:700;margin-bottom:10px;color:var(--green)">✅ 操作建议</div>`;
    r.suggestions.forEach((s,i)=>html += `<div class="muted" style="margin:6px 0">${i+1}. ${s}</div>`);
    html += `</div>`;
  }
  if(r.most_traded && r.most_traded.length){
    html += `<div class="card" style="margin-bottom:14px"><div style="font-size:15px;font-weight:700;margin-bottom:8px">交易最频繁的股票</div>`;
    for(const s of r.most_traded){
      const cls = s.pnl>=0?'pos':'neg';
      html += `<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border-color)"><span>${s.name}</span><span class="muted">${s.count} 笔</span><span class="${cls}">${s.pnl>=0?'+':''}${s.pnl}</span></div>`;
    }
    html += `</div>`;
  }
  document.getElementById('view').innerHTML = html;
}
async function loadBacktest(){
  document.getElementById('stats').innerHTML = `<div class="card"><div class="label">回测</div><div class="value" style="font-size:15px">加载中…</div></div>`;
  const r = await fetch('/api/backtest').then(x=>x.json());
  if(!r.ok){document.getElementById('view').innerHTML='<div class="card">'+ (r.msg||'加载失败') +'</div>';return}
  const a = r.actual||{}, im = r.improved||{}, rv = r.reversal||{};
  const aCls = a.total_pnl>=0?'pos':'neg';
  const iCls = im.total_pnl>=0?'pos':'neg';
  document.getElementById('stats').innerHTML = `
    <div class="card"><div class="label">已平仓交易</div><div class="value">${r.trip_count||0} 笔</div></div>
    <div class="card"><div class="label">实际总盈亏</div><div class="value ${aCls}" style="font-size:16px">${(a.total_pnl||0)>=0?'+':''}${fmtMoney(a.total_pnl)}</div></div>
    <div class="card"><div class="label">改进后总盈亏</div><div class="value ${iCls}" style="font-size:16px">${(im.total_pnl||0)>=0?'+':''}${fmtMoney(im.total_pnl)}</div></div>
    <div class="card"><div class="label">改善比例</div><div class="value">${im.better_ratio||0}%</div></div>
    <div class="card"><div class="label">实际胜率/改进胜率</div><div class="value" style="font-size:16px">${a.win_rate||0}% / ${im.win_rate||0}%</div></div>
    <div class="card"><div class="label">平均每笔(实际→改进)</div><div class="value" style="font-size:16px">${a.avg_pnl||0} → ${im.avg_pnl||0}</div></div>`;
  let html = `<div class="muted" style="margin-bottom:10px">用你的历史交割单，模拟「ATR止损 + 移动止盈」后的卖出点，对比实际结果 · <span class="tag tag-blue" style="cursor:pointer" onclick="loadBacktest()">刷新</span></div>`;
  html += `<div class="card" style="margin-bottom:14px"><div style="font-size:16px;font-weight:700;margin-bottom:10px">📊 核心对比</div>
    <table><thead><tr><th>指标</th><th>实际交易</th><th>改进后(ATR+移动止盈)</th></tr></thead><tbody>
    <tr><td>总盈亏</td><td class="${aCls}">${(a.total_pnl||0)>=0?'+':''}${fmtMoney(a.total_pnl)}</td><td class="${iCls}">${(im.total_pnl||0)>=0?'+':''}${fmtMoney(im.total_pnl)}</td></tr>
    <tr><td>胜率</td><td>${a.win_rate||0}%</td><td>${im.win_rate||0}%</td></tr>
    <tr><td>平均每笔</td><td>${a.avg_pnl||0}</td><td>${im.avg_pnl||0}</td></tr>
    </tbody></table></div>`;
  if(rv.with_signal || rv.without_signal){
    html += `<div class="card" style="margin-bottom:14px"><div style="font-size:15px;font-weight:700;margin-bottom:8px">🔍 趋势反转过滤验证</div>
      <div class="muted" style="margin:4px 0">有反转信号买入：${rv.with_signal.n} 笔，胜率 ${rv.with_signal.win_rate}%，盈亏 ${rv.with_signal.pnl}</div>
      <div class="muted" style="margin:4px 0">无反转信号买入：${rv.without_signal.n} 笔，胜率 ${rv.without_signal.win_rate}%，盈亏 ${rv.without_signal.pnl}</div>
      <div class="muted" style="margin-top:8px">结论：你过去大部分是「无信号」接下跌刀，有信号时胜率更高。</div></div>`;
  }
  const rows = r.rows||[];
  const better = rows.slice().sort((x,y)=>(y.sim_pnl-y.actual_pnl)-(x.sim_pnl-x.actual_pnl)).slice(0,8);
  if(better.length){
    html += `<div class="card"><div style="font-size:15px;font-weight:700;margin-bottom:8px">💡 改进最明显的 8 笔</div>
      <table><thead><tr><th>股票</th><th>买入</th><th>实际盈亏</th><th>模拟盈亏</th><th>触发</th></tr></thead><tbody>`;
    for(const x of better){
      const bCls = x.sim_pnl>=x.actual_pnl?'pos':'neg';
      html += `<tr><td>${x.name}</td><td class="muted">${x.buy_date} @${x.buy_price}</td>
        <td class="${x.actual_pnl>=0?'pos':'neg'}">${x.actual_pnl>=0?'+':''}${x.actual_pnl}</td>
        <td class="${x.sim_pnl>=0?'pos':'neg'}">${x.sim_pnl>=0?'+':''}${x.sim_pnl}</td>
        <td class="muted">${x.reason||'-'}</td></tr>`;
    }
    html += '</tbody></table></div>';
  }
  document.getElementById('view').innerHTML = html;
}
async function loadLlm(){
  const r = await fetch('/api/llm').then(x=>x.json());
  const date = r.generated_at ? r.generated_at.slice(0,16).replace('T',' ') : '-';
  document.getElementById('stats').innerHTML = `
    <div class="card"><div class="label">AI 定性报告</div><div class="value">${r.total||0} 只</div></div>
    <div class="card"><div class="label">模型</div><div class="value" style="font-size:14px">${r.model||'-'}</div></div>
    <div class="card"><div class="label">生成时间</div><div class="value" style="font-size:14px">${date}</div></div>
    <div class="card"><div class="label">说明</div><div class="value" style="font-size:12px">只定性不打分，不构成投资建议</div></div>`;
  let html = `<table><thead><tr><th>代码</th><th>名称</th><th>核心逻辑（一句话）</th><th>赛道解读</th><th>主要风险</th></tr></thead><tbody>`;
  for(const s of r.stocks){
    html += `<tr>
      <td class="muted">${s.code}</td><td>${s.name}</td>
      <td>${s.thesis||'-'}</td><td class="muted">${s.sector||'-'}</td><td class="muted">${s.risk||'-'}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  if(!r.stocks.length) html = '<div class="card">暂无 AI 定性报告（运行 llm_report.py 生成）</div>';
  document.getElementById('view').innerHTML = html;
}
async function showDetail(code){
  const r = await fetch('/api/stock/'+code).then(x=>x.json());
  if(!r.ok) return;
  const s = r.stock;
  let factors = '';
  for(const [k,v] of Object.entries(s.factors)){
    factors += `<div class="factor"><span>${k} <span class="muted">(权重${v.weight}%)</span></span><b>${v.score}</b></div>
      <div class="bar"><span style="width:${v.score}%"></span></div>`;
  }
  const ded = s.deductions.length ? s.deductions.map(d=>`<div class="muted">✗ ${d}</div>`).join('') : '<div class="muted">无</div>';
  const veto = s.veto.length ? s.veto.map(v=>`<div class="tag tag-red">${v}</div>`).join('') : '';
  const trig = s.sell_triggers.length ? s.sell_triggers.map(t=>`<div class="muted">⚠ ${t}</div>`).join('') : '<div class="muted">未触发</div>';
  const flags = s.hm_flags.length ? s.hm_flags.map(f=>`<span class="tag tag-orange">${f}</span>`).join('') : '<span class="muted">无</span>';
  const llm = r.llm && (r.llm.thesis||r.llm.sector||r.llm.risk) ? `
    <div class="mt"><b>AI 定性</b>
      <div class="muted">💡 ${r.llm.thesis||'-'}</div>
      <div class="muted">🏭 ${r.llm.sector||'-'}</div>
      <div class="muted">⚠️ ${r.llm.risk||'-'}</div>
    </div>` : '';
  document.getElementById('modalBody').innerHTML = `
    <h3>${s.code} ${s.name}</h3>
    <div class="muted">${s.industry} · 综合分 <b>${s.combined_score}</b> · 风险 <b>${s.risk_level}</b> ${s.industry_note?'· '+s.industry_note:''}</div>
    ${llm}
    <div class="mt"><b>加分项明细</b>${factors}</div>
    <div class="mt"><b>扣分项（合计 -${s.deduction_total}）</b>${ded}${veto}</div>
    <div class="mt"><b>游资因子</b> 调整 ${s.hm_adjustment>0?'+':''}${s.hm_adjustment} ${flags}</div>
    <div class="mt"><b>卖出参考触发</b>${trig}</div>
    <div class="mt"><b>防守支撑位</b>${Object.entries(s.support||{}).map(([k,v])=>`<div class="muted">· ${k}: ${v.toFixed(2)}</div>`).join('')||'<div class="muted">未提供</div>'}</div>`;
  document.getElementById('modal').classList.add('open');
}
function closeModal(){document.getElementById('modal').classList.remove('open')}
let timer = null;
function switchTab(t,el){
  currentTab=t;
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  el.classList.add('active');
  if(timer){clearInterval(timer); timer=null;}
  if(t==='youzi') loadYouzi();
  else if(t==='intraday'){loadIntraday(); timer=setInterval(loadIntraday, 3000);}
  else if(t==='add') loadAddForm();
  else if(t==='portfolio'){loadPortfolio(); timer=setInterval(loadPortfolio, 5000);}
  else if(t==='trades') loadTrades();
  else if(t==='review') loadReview();
  else if(t==='analysis') loadAnalysis();
  else if(t==='backtest') loadBacktest();
  else if(t==='llm') loadLlm();
  else loadDemo();
}
loadYouzi();
</script>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
