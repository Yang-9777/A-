#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""尾盘打法 · 盘中实时监控（买入+卖出+止盈 一体）
交易日盘中每 30 秒：
  1) 卖出/止盈监控：watch.json 里「昨日尾盘选出」的标的
  2) 尾盘买入扫描：14:25–14:57 筛最强2只，TOP2变化或每3分钟推送
非交易时段休眠。用法：python3 monitor.py [--once] [--force]
"""
import os, sys, time, json, datetime, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import screener_data as sd
import push as P

WATCH = os.path.join(HERE, "watch.json")
STATE = os.path.join(HERE, "state.json")
INTERVAL = 30


def _now(): return datetime.datetime.now()
def _today(): return _now().strftime("%Y-%m-%d")
def _hm(): t = _now(); return t.hour * 60 + t.minute


def in_trading_hours():
    t = _hm()
    return _now().weekday() < 5 and ((9 * 60 + 30 <= t <= 11 * 60 + 30) or (13 * 60 <= t <= 15 * 60 + 5))


def load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {"sold": {}, "prompted": {}, "buy_sig": "", "buy_last": 0}


def save_state(st):
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def load_watch():
    try:
        d = json.load(open(WATCH, encoding="utf-8"))
        return (d.get("picks") or []), d.get("date", "")
    except Exception:
        return [], ""


def sym_of(code):
    return ("bj" if code.startswith(("43", "83", "87", "92")) else
            "sh" if code.startswith(("6", "9")) else "sz") + code


def monitor_sell(st):
    picks, _ = load_watch()
    if not picks:
        return
    pairs = [(p, sym_of(p["code"])) for p in picks if not st["sold"].get(p["code"])]
    if not pairs:
        return
    qs = sd.tencent_batch([s for _, s in pairs])
    for p, sym in pairs:
        q = qs.get(sym)
        if not q:
            continue
        code, name = p["code"], p["name"]
        price, prev, vwap, high, pct = q["price"], q["prev_close"], q["vwap"], q["high"], q["pct"]
        reasons = []
        if prev > 0 and price < prev:
            reasons.append(f"跌破昨收/前低(昨收{prev:.2f})")
        elif vwap > 0 and price < vwap:
            reasons.append(f"跌破分时均价线(均价{vwap:.2f})")
        if prev > 0 and high > prev * 1.02 and price < high - 0.015 * prev:
            reasons.append(f"冲高回落(最高{high:.2f}→现{price:.2f})")
        if reasons:
            content = (f"{code} {name}  现价{price}  涨幅{pct:+.2f}%\n" + "\n".join(reasons) +
                       "\n\n纪律：冲高即卖/破位全撤，不补仓，持股不过午。")
            P.push("卖出提示", f"{name} 触发卖出信号", content)
            st["sold"][code] = _today()
        elif prev > 0 and pct >= 2.0 and st["prompted"].get(code) != _today():
            P.push("止盈提示", f"{name} 已冲高 +{pct:.2f}%",
                   f"{code} {name} 现价{price}，较昨收涨{pct:.2f}%。按纪律可分批落袋，持股不过午。")
            st["prompted"][code] = _today()


def monitor_buy(st):
    t = _hm()
    if not (14 * 60 + 25 <= t <= 14 * 60 + 57):
        return
    hits, _ = sd.get_rank(main_only=True)
    top = hits[:2]
    json.dump({"date": _today(), "picks": top}, open(WATCH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    if not top:
        return
    sig = "|".join(f"{h['code']}:{h['score']}" for h in top)
    now_ts = time.time()
    if sig != st["buy_sig"] or now_ts - st.get("buy_last", 0) > 180:
        lines = [f"{i+1}. {h['code']} {h['name']} 现价{h['price']} 涨{h['pct']}% 量比{h['lb']} 换手{h['hsl']}% 强度{h['score']}/10"
                 for i, h in enumerate(top)]
        P.push("买入提示", "尾盘最强2只：" + "、".join(h["name"] for h in top),
               "（仅主板·4项硬指标已满足）\n" + "\n".join(lines) +
               "\n\n纪律：放量创当日新高分批买(各1/3)，破分时均价线/前低止损。")
        st["buy_sig"] = sig
        st["buy_last"] = now_ts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只跑一轮(测试)")
    ap.add_argument("--force", action="store_true", help="非交易时段也跑(测试)")
    a = ap.parse_args()
    st = load_state()
    print("盘中实时监控启动：交易日 9:30-11:30 / 13:00-15:00 每30秒一轮")
    while True:
        try:
            st["heartbeat"] = time.strftime("%Y-%m-%d %H:%M:%S")
            st["trading"] = in_trading_hours()
            if a.force or (st["trading"] and sd.is_trading_day_now()):
                monitor_sell(st)
                monitor_buy(st)
            save_state(st)
            if a.once:
                print("one-shot 完成")
                break
            time.sleep(INTERVAL)
        except Exception as e:
            print("err:", e)
            if a.once:
                break
            time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
