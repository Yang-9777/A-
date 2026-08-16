#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘中监测：读 watch_pool.json（每日Top3）→ 新浪实时行情 → 生成进场/出场信号 → intraday_signals.json
只给时机与风控参考，不自动下单，人做最终决策。
"""
import json
import os
import re
import sys
from datetime import datetime

import requests

_DIR = os.path.dirname(os.path.abspath(__file__))
POOL_PATH = os.path.join(_DIR, "watch_pool.json")
SIGNAL_PATH = os.path.join(_DIR, "intraday_signals.json")
POSITION_PATH = os.path.join(_DIR, "positions.json")
STATE_PATH = os.path.join(_DIR, "signal_state.json")
ACCOUNT_PATH = os.path.join(_DIR, "account.json")
TRADES_PATH = os.path.join(_DIR, "trades.json")

TAKE_PROFIT_PCT = 0.15          # 固定止盈 +15%（ATR 更大时取更远，让利润奔跑）
FIXED_STOP_PCT = 0.07           # 固定止损 -7%（上限，ATR 更紧时取更紧）
ATR_STOP_MULT = 2.0             # ATR 止损倍数
ATR_TAKE_MULT = 3.0             # ATR 止盈倍数
TRAILING_PCT = 0.08             # 移动止盈回撤比例（从最高点回撤 8% 止盈）
HIGH_OPEN_LIMIT = 7.0           # 高开超过 7% 不追
LOW_OPEN_LIMIT = -2.0           # 低开超过 -2% 弱势

SINA_QUOTE_URL = "https://hq.sinajs.cn/list="
HEADERS = {"Referer": "https://finance.sina.com.cn",
           "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_quotes(codes):
    """新浪实时行情（批量），返回 {code: {name, open, pre_close, price, high, low, volume, amount, time}}。"""
    syms = []
    for c in codes:
        c = str(c).zfill(6)
        prefix = "sh" if c[0] in "69" else "sz"
        syms.append(prefix + c)
    if not syms:
        return {}
    try:
        r = requests.get(SINA_QUOTE_URL + ",".join(syms), headers=HEADERS, timeout=15)
        r.encoding = "gbk"
    except requests.RequestException:
        return {}
    out = {}
    for line in r.text.strip().splitlines():
        m = re.match(r'var hq_str_(\w+)="(.*)";', line)
        if not m:
            continue
        sym = m.group(1)
        code = sym[2:]
        f = m.group(2).split(",")
        if len(f) < 32:
            continue
        out[code] = {
            "name": f[0],
            "open": _f(f[1]),
            "pre_close": _f(f[2]),
            "price": _f(f[3]),
            "high": _f(f[4]),
            "low": _f(f[5]),
            "volume": _f(f[8]),
            "amount": _f(f[9]),
            "date": f[30],
            "time": f[31],
        }
    return out


def load_positions():
    if os.path.exists(POSITION_PATH):
        try:
            with open(POSITION_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_positions(pos):
    with open(POSITION_PATH, "w", encoding="utf-8") as f:
        json.dump(pos, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------
# 账户资金：输入现金 → 总金额 = 现金 + 持仓市值
# ----------------------------------------------------------------------
def load_account():
    if os.path.exists(ACCOUNT_PATH):
        try:
            with open(ACCOUNT_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"cash": 0}


def save_account(acct):
    try:
        tmp = ACCOUNT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(acct, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ACCOUNT_PATH)
    except OSError:
        pass


def set_cash(amount):
    acct = load_account()
    acct["cash"] = round(float(amount or 0), 2)
    save_account(acct)
    return acct


# ----------------------------------------------------------------------
# 历史交割单：每次买/卖自动记录，卖出时计算已实现盈亏
# ----------------------------------------------------------------------
def load_trades():
    if os.path.exists(TRADES_PATH):
        try:
            with open(TRADES_PATH, encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, dict) and isinstance(d.get("trades"), list):
                    return d["trades"]
        except (json.JSONDecodeError, OSError):
            pass
    return []


def save_trades(trades):
    try:
        tmp = TRADES_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"trades": trades}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, TRADES_PATH)
    except OSError:
        pass


def record_trade(action, code, name, shares, price, pnl=None, pnl_pct=None, note="", time=None):
    trades = load_trades()
    trades.append({
        "time": time or datetime.now().isoformat(timespec="seconds"),
        "code": str(code).zfill(6),
        "name": name,
        "action": action,  # buy / sell
        "shares": int(shares),
        "price": round(float(price), 3),
        "amount": round(float(shares) * float(price), 2),
        "pnl": round(pnl, 2) if pnl is not None else None,
        "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
        "note": note,
    })
    save_trades(trades)
    return trades


def trade_history():
    """交割单 + 复盘汇总。"""
    trades = sorted(load_trades(), key=lambda t: t.get("time", ""), reverse=True)
    buys = [t for t in trades if t["action"] == "buy"]
    sells = [t for t in trades if t["action"] == "sell"]
    total_buy = sum(t["amount"] for t in buys)
    total_sell = sum(t["amount"] for t in sells)
    realized_pnl = sum(t["pnl"] or 0 for t in sells)
    win = sum(1 for t in sells if (t["pnl"] or 0) > 0)
    return {
        "trades": trades,
        "summary": {
            "count": len(trades),
            "buy_count": len(buys),
            "sell_count": len(sells),
            "total_buy": round(total_buy, 2),
            "total_sell": round(total_sell, 2),
            "realized_pnl": round(realized_pnl, 2),
            "win_rate": round(win / len(sells) * 100, 1) if sells else 0.0,
        },
    }


def rebuild_positions_from_trades(trades):
    """按时间顺序重放全部交割，重算每笔已实现盈亏并重建当前持仓（加权平均成本）。"""
    import datetime as _dt
    trades = sorted(trades, key=lambda t: str(t.get("time", "")))
    pos = {}
    for t in trades:
        code = t["code"]
        p = pos.get(code, {"shares": 0, "avg_cost": 0.0, "name": t.get("name", code),
                           "first_buy": t.get("time")})
        try:
            shares = int(t["shares"])
            price = float(t["price"])
        except (TypeError, ValueError):
            continue
        if t["action"] == "buy":
            total_cost = p["avg_cost"] * p["shares"] + price * shares
            p["shares"] += shares
            p["avg_cost"] = total_cost / p["shares"] if p["shares"] else 0.0
            p["name"] = t.get("name") or p["name"]
        else:  # sell
            sell_shares = min(shares, p["shares"])
            if p["avg_cost"] and sell_shares > 0:
                pnl = (price - p["avg_cost"]) * sell_shares
                pnl_pct = (price / p["avg_cost"] - 1) * 100
                t["pnl"] = round(pnl, 2)
                t["pnl_pct"] = round(pnl_pct, 2)
            p["shares"] = max(0, p["shares"] - sell_shares)
            if p["shares"] == 0:
                p["avg_cost"] = 0.0
        pos[code] = p

    positions = {}
    for code, p in pos.items():
        if p["shares"] > 0:
            positions[code] = {
                "name": p["name"],
                "shares": int(p["shares"]),
                "entry_price": round(p["avg_cost"], 3),
                "entered_at": p.get("first_buy") or datetime.now().isoformat(timespec="seconds"),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
    save_positions(positions)
    return trades


def _action_of(v):
    """从方向值判断买卖。"""
    s = str(v)
    u = s.upper()
    if "卖" in s or "S" in u:
        return "sell"
    if "买" in s or "B" in u:
        return "buy"
    return None


def _is_non_trade(name, dv):
    """过滤非成交记录：登记指定/股息/红利/配股/申购等。"""
    s = f"{name}{dv}"
    for kw in ("登记", "指定", "股息", "红利", "利息", "配股", "申购", "中签", "送股",
               "转增", "分红", "缴款", "融资", "还款", "回购", "逆回购", "转账", "银证",
               "冻结", "解冻", "新股", "国债", "理财", "结息", "权证", "托管"):
        if kw in s:
            return True
    return False


def parse_import_text(text):
    """解析 CSV/文本交割单，返回 [{time,code,name,action,shares,price}]。"""
    import csv as _csv
    import io as _io
    text = (text or "").strip()
    if not text:
        return None, "内容为空"

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None, "内容为空"
    sample = lines[0]
    delim = "\t" if "\t" in sample else ("," if "," in sample else None)
    if delim:
        rows = list(_csv.reader(_io.StringIO(text), delimiter=delim))
        rows = [r for r in rows if r and any(str(x).strip() for x in r)]
    else:
        rows = [ln.split() for ln in lines]

    if not rows:
        return None, "无法解析内容"

    header = [str(h).strip() for h in rows[0]]
    joined = "".join(header)
    has_header = any(k in joined for k in ["代码", "名称", "买卖", "数量", "价格", "方向", "操作", "业务", "标志"])

    def find_col(keywords):
        for i, h in enumerate(header):
            for kw in keywords:
                if kw in h:
                    return i
        return None

    data_rows = rows[1:] if has_header else rows
    if has_header:
        ci_code = find_col(["代码", "证券代码", "股票代码"])
        ci_name = find_col(["名称", "证券名称", "股票名称"])
        ci_business = find_col(["业务名称", "业务"])
        ci_dir = find_col(["买卖", "方向", "操作", "交易类型", "委托标志", "标志"])
        ci_shares = find_col(["数量", "股数", "成交数量"])
        ci_price = find_col(["价格", "成交价", "均价", "成交价格"])
        ci_time = find_col(["时间", "日期", "成交日期", "成交时间"])
    else:
        # 无表头：按常见顺序 时间,代码,名称,方向,数量,价格
        ci_time, ci_code, ci_name, ci_business, ci_dir, ci_shares, ci_price = 0, 1, 2, None, 3, 4, 5

    def cell(row, idx):
        return str(row[idx]).strip() if idx is not None and idx < len(row) else ""

    out = []
    for row in data_rows:
        name = cell(row, ci_name)
        code = re.sub(r"\D", "", cell(row, ci_code))
        if len(code) >= 6:
            code = code[-6:]
        elif len(code) == 0:
            continue
        code = code.zfill(6)
        # 只保留 A 股/ETF 常见代码段，排除 799999 等伪代码
        if code[0] not in "01356":
            continue

        shares_s = cell(row, ci_shares).replace(",", "").replace("股", "")
        price_s = cell(row, ci_price).replace(",", "")
        try:
            shares = int(float(shares_s))
            price = float(price_s)
        except (TypeError, ValueError):
            continue
        if shares == 0 or price <= 0:
            continue
        shares = abs(shares)

        # 方向优先取「业务名称」（证券买入/证券卖出），其次买卖标志，最后数量正负号
        action = None
        business = cell(row, ci_business) if ci_business is not None else ""
        if business:
            if _is_non_trade(name, business):
                continue
            action = _action_of(business)
        if action is None:
            dv = cell(row, ci_dir)
            if _is_non_trade(name, dv):
                continue
            action = _action_of(dv)
        if action is None:
            continue

        tm = cell(row, ci_time)
        if tm:
            tm = tm.replace("/", "-").replace(".", "-").replace("年", "-").replace("月", "-").replace("日", "")
            tm = tm.split()[0] if " " in tm else tm
            if len(tm) == 8 and tm.isdigit():
                tm = f"{tm[:4]}-{tm[4:6]}-{tm[6:]}"
        out.append({
            "time": tm or datetime.now().isoformat(timespec="seconds"),
            "code": code,
            "name": name,
            "action": action,
            "shares": shares,
            "price": price,
        })
    if not out:
        return None, "没有识别到有效的交割记录（请确认包含 代码/方向/数量/价格 列）"
    return out, None


def import_trades(text, mode="merge"):
    """导入交割单：解析→合并(去重)→按时间重放重建持仓与已实现盈亏。返回 (新增条数, 错误)。"""
    parsed, err = parse_import_text(text)
    if err:
        # 记录原始数据到日志，便于排查格式
        try:
            with open(os.path.join(_DIR, "import_raw.txt"), "w", encoding="utf-8") as f:
                f.write(text or "")
        except OSError:
            pass
        return 0, 0, 0, err

    existing = load_trades() if mode == "merge" else []
    # 去重：时间+代码+方向+数量+价格 完全一致的跳过
    seen = {(t["time"], t["code"], t["action"], int(t["shares"]), round(float(t["price"]), 3))
            for t in existing}
    added = 0
    for p in parsed:
        key = (p["time"], p["code"], p["action"], int(p["shares"]), round(float(p["price"]), 3))
        if key in seen:
            continue
        existing.append({
            "time": p["time"], "code": p["code"], "name": p["name"],
            "action": p["action"], "shares": p["shares"], "price": p["price"],
            "amount": round(p["shares"] * p["price"], 2),
            "pnl": None, "pnl_pct": None, "note": "导入",
        })
        seen.add(key)
        added += 1

    # 按时间重放，重算已实现盈亏并重建持仓
    existing = rebuild_positions_from_trades(existing)
    save_trades(existing)
    buy_n = sum(1 for p in parsed if p["action"] == "buy")
    sell_n = sum(1 for p in parsed if p["action"] == "sell")
    return added, buy_n, sell_n, None


def export_trades_csv():
    """导出交割单为 CSV 文本（带表头）。"""
    import csv as _csv
    import io as _io
    trades = sorted(load_trades(), key=lambda t: t.get("time", ""), reverse=False)
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["时间", "代码", "名称", "方向", "数量", "价格", "金额", "已实现盈亏", "盈亏比", "备注"])
    for t in trades:
        w.writerow([
            t.get("time", ""), t.get("code", ""), t.get("name", ""),
            "买入" if t.get("action") == "buy" else "卖出",
            t.get("shares", ""), t.get("price", ""), t.get("amount", ""),
            "" if t.get("pnl") is None else t["pnl"],
            "" if t.get("pnl_pct") is None else t["pnl_pct"],
            t.get("note", ""),
        ])
    return buf.getvalue()


def review():
    """按股票聚合的复盘：累计买卖、已实现盈亏、当前持仓与浮盈亏。"""
    trades = load_trades()
    positions = load_positions()
    agg = {}
    for t in trades:
        code = t["code"]
        a = agg.setdefault(code, {
            "code": code, "name": t.get("name", code),
            "buy_shares": 0, "sell_shares": 0,
            "buy_amount": 0.0, "sell_amount": 0.0,
            "realized_pnl": 0.0, "trade_count": 0,
        })
        a["trade_count"] += 1
        if t["action"] == "buy":
            a["buy_shares"] += int(t.get("shares", 0))
            a["buy_amount"] += float(t.get("amount", 0) or 0)
        else:
            a["sell_shares"] += int(t.get("shares", 0))
            a["sell_amount"] += float(t.get("amount", 0) or 0)
            a["realized_pnl"] += float(t.get("pnl", 0) or 0)

    codes = list(agg.keys())
    quotes = fetch_quotes(codes) if codes else {}
    for code, a in agg.items():
        held = positions.get(code)
        cur_shares = a["buy_shares"] - a["sell_shares"]
        a["current_shares"] = cur_shares
        if held and cur_shares > 0:
            entry = held.get("entry_price")
            price = (quotes.get(code) or {}).get("price")
            if price is None:
                price = entry
            a["current_entry"] = entry
            a["current_price"] = price
            a["floating_pnl"] = round((price - entry) * cur_shares, 2) if entry and price else 0.0
            a["floating_pnl_pct"] = round((price / entry - 1) * 100, 2) if entry else 0.0
        else:
            a["current_shares"] = 0
            a["current_entry"] = None
            a["current_price"] = None
            a["floating_pnl"] = 0.0
            a["floating_pnl_pct"] = 0.0
        a["total_pnl"] = round(a["realized_pnl"] + a["floating_pnl"], 2)

    rows = sorted(agg.values(), key=lambda a: -(a["total_pnl"] or 0))
    total_realized = sum(a["realized_pnl"] for a in rows)
    total_floating = sum(a["floating_pnl"] for a in rows)
    win = sum(1 for a in rows if (a["total_pnl"] or 0) > 0)
    return {
        "stocks": rows,
        "summary": {
            "stock_count": len(rows),
            "total_realized_pnl": round(total_realized, 2),
            "total_floating_pnl": round(total_floating, 2),
            "total_pnl": round(total_realized + total_floating, 2),
            "win_stocks": win,
            "win_rate": round(win / len(rows) * 100, 1) if rows else 0.0,
        },
    }


def analysis():
    """交割单分析 + 操作建议（基于交易行为的规则诊断）。"""
    trades = sorted(load_trades(), key=lambda t: str(t.get("time", "")))
    if not trades:
        return {"error": "暂无交割单数据，先导入或记录交易"}

    sells = [t for t in trades if t["action"] == "sell"]
    buys = [t for t in trades if t["action"] == "buy"]
    wins = [t for t in sells if (t["pnl"] or 0) > 0]
    losses = [t for t in sells if (t["pnl"] or 0) <= 0]

    realized = sum(t["pnl"] or 0 for t in sells)
    win_total = sum(t["pnl"] for t in wins)
    loss_total = abs(sum(t["pnl"] for t in losses))
    win_rate_trade = len(wins) / len(sells) * 100 if sells else 0.0
    avg_win = win_total / len(wins) if wins else 0.0
    avg_loss = -loss_total / len(losses) if losses else 0.0
    profit_factor = win_total / loss_total if loss_total > 0 else (win_total if win_total > 0 else 0.0)
    max_win = max((t["pnl"] for t in wins), default=0.0)
    max_loss = min((t["pnl"] for t in losses), default=0.0)

    months = sorted({str(t.get("time", ""))[:7] for t in trades if t.get("time")})
    avg_per_month = len(trades) / len(months) if months else 0.0

    by_code = {}
    for t in trades:
        a = by_code.setdefault(t["code"], {"name": t.get("name", t["code"]), "count": 0, "pnl": 0.0})
        a["count"] += 1
        if t["action"] == "sell":
            a["pnl"] += t["pnl"] or 0.0
    win_stocks = sum(1 for a in by_code.values() if a["pnl"] > 0)
    loss_stocks = sum(1 for a in by_code.values() if a["pnl"] <= 0)
    repeated_losers = sorted([a for a in by_code.values() if a["count"] >= 4 and a["pnl"] < 0],
                             key=lambda a: a["pnl"])[:5]
    most_traded = sorted(by_code.values(), key=lambda a: -a["count"])[:5]

    fee_est = len(trades) * 5 + sum(t["amount"] for t in sells) * 0.0005  # 佣金约5元/笔 + 卖出印花税0.05%

    problems, suggestions = [], []
    if avg_per_month > 15:
        problems.append(f"交易频率过高：月均 {avg_per_month:.0f} 笔，容易追涨杀跌、被手续费反复侵蚀")
        suggestions.append("把月交易次数压到 8 笔以内，只在明确信号出现时出手")
    if win_rate_trade < 40:
        problems.append(f"胜率偏低：{win_rate_trade:.0f}%，选股或进出场时机胜率不足")
        suggestions.append("提高出手标准：只做低位 + 好基本面 + 明确进场信号（参考每日选股 Top3）")
    if wins and losses and abs(avg_loss) > avg_win:
        problems.append(f"盈亏比倒挂：平均每笔亏 {abs(avg_loss):.0f} 元 > 平均每笔赚 {avg_win:.0f} 元，典型「亏损扛着、盈利早跑」")
        suggestions.append("给每笔设硬止损（如 -8% 无条件走），盈利单用移动止盈让利润奔跑")
    if repeated_losers:
        names = "、".join(f"{a['name']}({a['count']}笔 亏{a['pnl']:.0f})" for a in repeated_losers[:3])
        problems.append(f"亏损股反复进出：{names}")
        suggestions.append("止损后不要立刻买回同一只票，避免情绪化报复交易")
    if len(by_code) > 10:
        problems.append(f"持仓过于分散：交易了 {len(by_code)} 只股票，难以跟踪管理")
        suggestions.append("精选 3-5 只熟悉的票做波段，比广撒网更容易控制风险")
    if fee_est > 0 and realized < 0:
        problems.append(f"手续费损耗约 {fee_est:.0f} 元（佣金+印花税），占已实现亏损的 {abs(fee_est/realized)*100:.0f}%")
        suggestions.append("减少无谓的频繁进出，降低摩擦成本")

    if not problems:
        problems.append("未发现明显行为问题，交易纪律较好")
        suggestions.append("保持现有纪律，继续用每日选股 + 止损止盈执行")

    return {
        "metrics": {
            "trade_count": len(trades),
            "buy_count": len(buys),
            "sell_count": len(sells),
            "stock_count": len(by_code),
            "win_stocks": win_stocks,
            "loss_stocks": loss_stocks,
            "realized_pnl": round(realized, 2),
            "win_rate_trade": round(win_rate_trade, 1),
            "avg_win": round(avg_win, 0),
            "avg_loss": round(avg_loss, 0),
            "profit_factor": round(profit_factor, 2),
            "max_win": round(max_win, 0),
            "max_loss": round(max_loss, 0),
            "avg_per_month": round(avg_per_month, 1),
            "month_span": len(months),
            "fee_est": round(fee_est, 0),
        },
        "most_traded": [{"name": a["name"], "count": a["count"], "pnl": round(a["pnl"], 0)} for a in most_traded],
        "repeated_losers": [{"name": a["name"], "count": a["count"], "pnl": round(a["pnl"], 0)} for a in repeated_losers],
        "problems": problems,
        "suggestions": suggestions,
    }


# ----------------------------------------------------------------------
# 信号状态：记录每只股票当前信号 + 首次出现时间 + 切换轨迹
# ----------------------------------------------------------------------
def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"stocks": {}, "updated_at": None}


def save_state(state):
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


def update_signal_state(signals, now=None):
    """把最新信号写入状态：检测信号切换（进场↔观望↔出场）并记录历史。

    仅在状态发生实际变化时落盘；同一信号持续时只读不改，避免每 3 秒刷新频繁写盘。
    """
    now = now or datetime.now().isoformat(timespec="seconds")
    state = load_state()
    changed = False
    # 跨日重置：信号记录按交易日重新开始，避免把昨天的时间戳带到今天
    if (state.get("updated_at") or "")[:10] != now[:10]:
        state = {"stocks": {}, "updated_at": now}
    for x in signals:
        code = str(x.get("code")).zfill(6)
        sig = x.get("signal")
        if not sig:
            continue
        price = (x.get("quote") or {}).get("price")
        cur = state["stocks"].get(code)
        if cur is None:
            state["stocks"][code] = {
                "name": x.get("name", ""),
                "current_signal": sig,
                "since": now,
                "history": [{"from": None, "to": sig, "at": now, "price": price}],
            }
            changed = True
            x["signal_since"] = now
            continue
        if cur.get("current_signal") != sig:
            hist = cur.setdefault("history", [])
            hist.append({"from": cur.get("current_signal"), "to": sig, "at": now, "price": price})
            cur["history"] = hist[-50:]
            cur["current_signal"] = sig
            cur["since"] = now
            changed = True
        x["signal_since"] = cur.get("since")
    if changed:
        state["updated_at"] = now
        save_state(state)
    return state


def collect_history(state, limit=20):
    """汇总全部股票的切换轨迹，按时间倒序。"""
    out = []
    for code, info in (state.get("stocks") or {}).items():
        for h in (info.get("history") or []):
            out.append({"code": code, "name": info.get("name", ""), **h})
    out.sort(key=lambda h: str(h.get("at", "")), reverse=True)
    return out[:limit]


def compute_stop_take(entry_price, support_levels, atr=None):
    """动态止损/止盈：固定比例 + 支撑位 + ATR 自适应。

    - 止损取「固定-7% / 最近支撑位 / entry-2*ATR」中最高(最紧、最早触发)的
    - 止盈取「固定+15% / entry+3*ATR」中较远(更让利润奔跑)的
    """
    if not entry_price:
        return None, None
    stops = [entry_price * (1 - FIXED_STOP_PCT)]
    for lv in (support_levels or {}).values():
        if lv and entry_price * 0.85 < lv < entry_price:
            stops.append(lv)
    if atr and atr > 0:
        stops.append(entry_price - ATR_STOP_MULT * atr)
    stop = max(stops)

    take = entry_price * (1 + TAKE_PROFIT_PCT)
    if atr and atr > 0:
        take = max(take, entry_price + ATR_TAKE_MULT * atr)
    return round(stop, 2), round(take, 2)


def suggest_position(score, risk):
    """按得分/风险给建议仓位（单票）。"""
    if risk == "极高":
        return 0.0
    if risk == "高":
        base = 0.02
    elif risk == "中":
        base = 0.05
    else:
        base = 0.08 if score >= 70 else 0.05
    return base


def entry_signal(s, q):
    """未进场：给出进场时机判断（Top3 已由六因子选定，这里只看「何时进」）。"""
    reasons = []
    score = s.get("combined_score", 0)
    risk = s.get("risk_level", "低")
    price = q.get("price")
    pre_close = q.get("pre_close")
    chg = ((price / pre_close - 1) * 100) if price and pre_close else None

    ok = True
    if chg is not None:
        if chg > HIGH_OPEN_LIMIT:
            ok = False
            reasons.append(f"高开 {chg:+.1f}% 追高，等回踩")
        elif chg < LOW_OPEN_LIMIT:
            ok = False
            reasons.append(f"低开 {chg:+.1f}% 弱势，先观察")
        else:
            reasons.append(f"涨跌 {chg:+.1f}% 适中")
    # 支撑位是否有效跌破（均线在现价上方属趋势压制，不作进场硬门槛）
    supports = s.get("support") or {}
    broke = []
    below_ma = []
    if price and supports:
        for name, lv in supports.items():
            if lv is None:
                continue
            if "MA" in name and lv > price:
                below_ma.append(f"{name} {lv:.2f}")
                continue
            if price < lv * 0.98:
                broke.append(f"{name} {lv:.2f}")
    if broke:
        ok = False
        reasons.append("已跌破防守位：" + "、".join(broke))
    elif below_ma:
        reasons.append("股价在 " + "、".join(below_ma) + " 下方（低位左侧，趋势偏弱，控制仓位）")
    if risk in ("高", "极高"):
        ok = False
        reasons.append(f"风险等级 {risk}，不建议参与")

    # 技术面反转/趋势确认提示（不硬性拦截，只提示更好/更差的买点）
    atr = s.get("atr14")
    tech_strong = []
    if s.get("ma_bull"):
        tech_strong.append("均线多头")
    if s.get("macd_golden"):
        tech_strong.append("MACD金叉")
    if s.get("breakout_20d") and s.get("volume_surge"):
        tech_strong.append("放量突破20日新高")
    if tech_strong:
        reasons.append("技术面转强：" + "、".join(tech_strong))
    if s.get("rsi14") is not None and s["rsi14"] >= 80:
        reasons.append(f"RSI {s['rsi14']:.0f} 超买，追高风险")

    stop, take = compute_stop_take(price, supports, atr)
    pos = suggest_position(score, risk) if ok else 0.0

    if ok:
        reasons.append(f"参考进场 {price}，止损 {stop}，止盈 {take}，仓位 {pos*100:.0f}%")
    else:
        reasons.append(f"综合分 {score}，风险 {risk}（选股已定，等待更好时机）")
    return {
        "signal": "进场" if ok else "观望",
        "reasons": reasons,
        "entry_price": price,
        "stop_loss": stop,
        "take_profit": take,
        "position_pct": round(pos, 3),
    }


def exit_signal(s, q, held):
    """已进场：给出出场/持有判断（含 ATR 动态止损 + 移动止盈）。"""
    reasons = []
    entry = held.get("entry_price") or s.get("price")
    stop = held.get("stop_loss")
    take = held.get("take_profit")
    price = q.get("price")
    atr = s.get("atr14")

    if stop and take is None:
        stop, take = compute_stop_take(entry, s.get("support") or {}, atr)
    if not stop or not take:
        stop, take = compute_stop_take(entry, s.get("support") or {}, atr)

    # 移动止盈：跟踪入场后最高价，从最高点回撤 TRAILING_PCT 即止盈（让利润奔跑）
    highest = held.get("highest_price") or entry or price or 0
    if price and price > highest:
        highest = price
        held["highest_price"] = round(highest, 2)
    trailing = None
    if highest and entry and highest > entry:
        trailing = round(highest * (1 - TRAILING_PCT), 2)
    eff_stop = stop
    if trailing is not None and (eff_stop is None or trailing > eff_stop):
        eff_stop = trailing

    trigger = "持有"
    if price is not None:
        if eff_stop and price <= eff_stop:
            if trailing is not None and eff_stop == trailing:
                trigger = "出场"
                reasons.append(f"移动止盈触发 {eff_stop}（最高 {highest} 回撤超 {TRAILING_PCT*100:.0f}%）")
            else:
                trigger = "出场"
                reasons.append(f"触发止损 {eff_stop}（现价 {price}）")
        if take and price >= take:
            trigger = "出场"
            reasons.append(f"达到止盈 {take}（现价 {price}）")
    if s.get("risk_level") in ("高", "极高"):
        trigger = "出场" if trigger == "持有" else trigger
        reasons.append(f"风险等级升为 {s.get('risk_level')}")
    # 盘后快照里的「跌破支撑」由实时价格重算；其余基本面类卖出触发仍适用
    for t in (s.get("sell_triggers") or []):
        if "跌破" in t or "支撑" in t:
            continue
        trigger = "出场"
        reasons.append(t)
    # 实时支撑跌破（均线在现价上方不算）
    supports = s.get("support") or {}
    broke = []
    if price and supports:
        for name, lv in supports.items():
            if lv is None:
                continue
            if "MA" in name and lv > price:
                continue
            if price < lv * 0.98:
                broke.append(f"{name} {lv:.2f}")
    if broke:
        trigger = "出场"
        reasons.append("有效跌破防守位：" + "、".join(broke))
    if not reasons:
        stop_note = f"止损 {eff_stop}" + (f"（含移动止盈 {trailing}）" if trailing else "")
        reasons.append(f"持有中，现价 {price}，{stop_note}，止盈 {take}")
    return {
        "signal": trigger,
        "reasons": reasons,
        "entry_price": round(entry, 2) if entry else None,
        "stop_loss": eff_stop,
        "take_profit": take,
        "trailing_stop": trailing,
        "highest_price": held.get("highest_price"),
    }


def _load_pool():
    """读取 watch_pool.json，失败返回空 dict。"""
    try:
        with open(POOL_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def generate(top=3):
    """生成盘中信号（不写文件），返回 payload dict。供 web 端实时调用。"""
    pool = _load_pool()
    stocks = pool.get("stocks", [])[:top]
    if not stocks:
        return {"generated_at": datetime.now().isoformat(), "trading": _is_trading_time(),
                "total": 0, "stocks": [], "error": "watch_pool.json 不存在或为空"}

    quotes = fetch_quotes([s["code"] for s in stocks])
    positions = load_positions()
    positions_before = {k: dict(v) for k, v in positions.items()}

    signals = []
    for s in stocks:
        code = str(s["code"]).zfill(6)
        q = quotes.get(code)
        if not q:
            signals.append({"code": code, "name": s["name"], "error": "实时行情获取失败"})
            continue
        held = positions.get(code)
        if held:
            ex = exit_signal(s, q, held)
            signals.append({"code": code, "name": s["name"], "quote": q, **ex})
        else:
            en = entry_signal(s, q)
            signals.append({"code": code, "name": s["name"], "quote": q, **en})

    # 移动止盈更新了最高价则落盘
    if positions != positions_before:
        save_positions(positions)

    # 更新信号状态（检测切换、记录首次出现时间），并把 since/history 带进 payload
    state = update_signal_state(signals)
    return {
        "generated_at": datetime.now().isoformat(),
        "trading": _is_trading_time(),
        "total": len(signals),
        "stocks": signals,
        "history": collect_history(state),
    }


def run(top=3):
    payload = generate(top)
    with open(SIGNAL_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"已写出 {SIGNAL_PATH}，{len(payload.get('stocks', []))} 只", flush=True)
    for x in payload.get("stocks", []):
        print(f"  {x['code']} {x['name']} 信号:{x.get('signal','?')} 现价:{x.get('quote',{}).get('price')} "
              f"止损:{x.get('stop_loss')} 止盈:{x.get('take_profit')}", flush=True)


# ----------------------------------------------------------------------
# 我的持仓 + 买卖提示
# ----------------------------------------------------------------------
def compute_holding(s, q, held):
    """计算单只持仓的实时市值/盈亏 + 持有/卖出提示。"""
    shares = int(held.get("shares") or 0)
    entry = held.get("entry_price") or s.get("price")
    price = q.get("price") if q else None
    stop = held.get("stop_loss")
    take = held.get("take_profit")
    if (stop is None or take is None) and entry:
        stop, take = compute_stop_take(entry, s.get("support") or {})

    cost = shares * entry if (shares and entry) else 0.0
    mv = shares * price if (shares and price is not None) else 0.0
    pnl = mv - cost
    pnl_pct = ((price / entry - 1) * 100) if (entry and price) else 0.0

    ex = exit_signal(s, q or {}, held) if price is not None else \
        {"signal": "持有", "reasons": ["实时行情获取失败"]}
    return {
        "code": str(s.get("code") or held.get("code", "")).zfill(6),
        "name": s.get("name") or held.get("name", ""),
        "shares": shares,
        "entry_price": round(entry, 2) if entry else None,
        "price": price,
        "market_value": round(mv, 2),
        "cost": round(cost, 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "stop_loss": stop,
        "take_profit": take,
        "signal": ex.get("signal"),
        "reasons": ex.get("reasons", []),
        "entered_at": held.get("entered_at"),
    }


def buy_position(code, name="", shares=100, entry_price=None):
    """记录买入/加仓。entry_price 缺省取实时价；自动算止损止盈并加权平均成本。"""
    code = str(code).zfill(6)
    q = fetch_quotes([code]).get(code) or {}
    price = float(entry_price) if entry_price else q.get("price")
    if not price:
        raise ValueError("无法获取实时价，请手动填写买入价")

    pool = _load_pool()
    s = next((x for x in pool.get("stocks", []) if str(x["code"]).zfill(6) == code), None)
    supports = s.get("support") if s else {}
    stop, take = compute_stop_take(price, supports)

    pos = load_positions()
    old = pos.get(code) or {}
    old_shares = int(old.get("shares") or 0)
    add_shares = max(0, int(shares or 0))
    new_shares = old_shares + add_shares
    # 加权平均成本
    if old.get("entry_price") and old_shares > 0:
        avg = (float(old["entry_price"]) * old_shares + price * add_shares) / new_shares
    else:
        avg = price
    pos[code] = {
        "name": name or (s.get("name") if s else q.get("name") or old.get("name", code)),
        "shares": new_shares,
        "entry_price": round(avg, 3),
        "stop_loss": round(stop, 2) if stop else old.get("stop_loss"),
        "take_profit": round(take, 2) if take else old.get("take_profit"),
        "entered_at": old.get("entered_at") or datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_positions(pos)
    record_trade("buy", code, pos[code]["name"], add_shares, price, note="买入/加仓")
    return pos[code]


def sell_position(code, shares=None, sell_price=None):
    """卖出/减仓。shares=None 表示全部清仓。卖出价缺省取实时价；记录交割单并算已实现盈亏。"""
    code = str(code).zfill(6)
    pos = load_positions()
    held = pos.get(code)
    if not held:
        return None
    cur = int(held.get("shares") or 0)
    sell = cur if shares is None else max(0, int(shares or 0))
    if sell <= 0:
        return held

    price = float(sell_price) if sell_price else None
    if price is None:
        price = (fetch_quotes([code]).get(code) or {}).get("price")
    if price is None:
        price = held.get("entry_price")

    avg = held.get("entry_price") or price
    pnl = (price - avg) * sell
    pnl_pct = (price / avg - 1) * 100 if avg else 0.0
    record_trade("sell", code, held.get("name", code), sell, price,
                 pnl=pnl, pnl_pct=pnl_pct, note="卖出")

    remain = cur - sell
    if remain <= 0:
        pos.pop(code, None)
        save_positions(pos)
        return None
    held["shares"] = remain
    held["updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_positions(pos)
    return held


def portfolio(top=30):
    """我的持仓（实时盈亏 + 卖出提示）+ 候选买入提示。"""
    pool = _load_pool()
    stocks = (pool.get("stocks") or [])[:top]
    pool_map = {str(s["code"]).zfill(6): s for s in stocks}
    positions = load_positions()

    codes = list(positions.keys())
    for s in stocks:
        c = str(s["code"]).zfill(6)
        if c not in codes:
            codes.append(c)
    quotes = fetch_quotes(codes)

    holdings = []
    for code, held in positions.items():
        code = str(code).zfill(6)
        s = pool_map.get(code) or {
            "code": code, "name": held.get("name", code),
            "support": {}, "sell_triggers": [], "risk_level": "低",
            "price": held.get("entry_price"),
        }
        holdings.append(compute_holding(s, quotes.get(code), held))

    # 买入提示：池内未持仓的候选
    candidates = []
    for s in stocks:
        code = str(s["code"]).zfill(6)
        if code in positions:
            continue
        q = quotes.get(code)
        if not q:
            continue
        en = entry_signal(s, q)
        candidates.append({
            "code": code, "name": s.get("name"),
            "industry": s.get("industry", ""),
            "price": q.get("price"),
            "signal": en.get("signal"),
            "position_pct": en.get("position_pct"),
            "stop_loss": en.get("stop_loss"),
            "take_profit": en.get("take_profit"),
            "reasons": en.get("reasons", []),
            "combined_score": s.get("combined_score"),
            "risk_level": s.get("risk_level"),
        })

    total_cost = sum(h["cost"] for h in holdings)
    total_mv = sum(h["market_value"] for h in holdings)
    total_pnl = total_mv - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0.0
    holdings.sort(key=lambda h: -(h["pnl"] or 0))
    candidates.sort(key=lambda c: -(c.get("combined_score") or 0))

    # 账户资金：输入现金 → 总金额(总资产) = 现金 + 持仓市值
    acct = load_account()
    cash = max(0.0, float(acct.get("cash") or 0))
    total_assets = cash + total_mv
    position_ratio = (total_mv / total_assets) if total_assets > 0 else 0.0

    return {
        "generated_at": datetime.now().isoformat(),
        "trading": _is_trading_time(),
        "summary": {
            "count": len(holdings),
            "cash": round(cash, 2),
            "total_cost": round(total_cost, 2),
            "total_market_value": round(total_mv, 2),
            "total_assets": round(total_assets, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "position_ratio": round(position_ratio, 4),
        },
        "holdings": holdings,
        "candidates": candidates,
    }


def _is_trading_time():
    """是否交易时段（工作日 9:30-11:30 / 13:00-15:00）。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return (930 <= hm <= 1130) or (1300 <= hm <= 1500)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=3)
    a = ap.parse_args()
    run(top=a.top)
