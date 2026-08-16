#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
杨永兴「尾盘打法」选股工具
=========================
功能：
  1) --rank          全市场按"十步尾盘买入法"初筛，输出强度分最高的 N 只（默认 2）
  2) --code 002213   单只股票逐条体检，判断是否符合尾盘布局条件

数据源：东方财富 clist（全市场排行）+ 腾讯行情（单股），均为公开接口。
注意：非交易日(周末/节假日)返回的是上一交易日收盘数据，仅供参考。

用法：
  python3 weipan_screen.py --rank --top 2
  python3 weipan_screen.py --code 002213
"""

import argparse
import json
import sys
import urllib.request

ALL_A = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
FIELDS = "f12,f14,f2,f3,f8,f10,f20,f21"

PCT_MIN = 3.0
PCT_MAX = 5.0
LB_MIN  = 1.0
LB_BEST = (1.5, 3.0)
HSL_MIN = 5.0
HSL_MAX = 10.0
CAP_MIN = 50e8
CAP_MAX = 200e8


def http_get(url, enc="utf-8", raw=False, retries=4):
    import time
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "http://quote.eastmoney.com/",
            })
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
            return data if raw else json.loads(data.decode(enc))
        except Exception as e:
            last = e
            time.sleep(0.8 * (i + 1))
    raise last


def analyze(pct, lb, hsl, cap, price):
    checks = {
        f"涨幅{PCT_MIN:g}-{PCT_MAX:g}%":   PCT_MIN <= pct <= PCT_MAX,
        f"量比>{LB_MIN:g}":                lb > LB_MIN,
        f"换手{HSL_MIN:g}-{HSL_MAX:g}%":   HSL_MIN <= hsl <= HSL_MAX,
        f"流通{CAP_MIN/1e8:.0f}-{CAP_MAX/1e8:.0f}亿": CAP_MIN <= cap <= CAP_MAX,
    }
    passed = all(checks.values())
    score = 0.0
    if passed:
        score += 2 if LB_BEST[0] <= lb <= LB_BEST[1] else (1 if lb > 1 else 0)
        score += 2 if 7.0 <= hsl <= 8.5 else (1 if 6.0 <= hsl < 7.0 or 8.5 < hsl <= 9.5 else 0)
        score += 2 if 3.5 <= pct <= 4.8 else (1 if PCT_MIN <= pct < 3.5 else 0)
        score += 2 if 70e8 <= cap <= 150e8 else (1 if CAP_MIN <= cap < 70e8 or 150e8 < cap <= CAP_MAX else 0)
        score += 2 if 5 <= price <= 80 else (1 if 3 <= price < 5 else 0)
    return passed, checks, round(score, 1)


def tencent_quote(code):
    """腾讯行情单股，返回 (name, price, pct, lb, hsl, floatcap_元)"""
    if code[0] in ("6", "9"):
        sym = "sh" + code
    elif code[0] in ("4", "8"):
        sym = "bj" + code
    else:
        sym = "sz" + code
    data = http_get(f"http://qt.gtimg.cn/q={sym}", enc="gbk", raw=True).decode("gbk", "ignore")
    body = data.split('="', 1)[1].rstrip('";\n')
    f = body.split("~")
    name  = f[1] if len(f) > 1 else "?"
    price = float(f[3] or 0)
    pct   = float(f[32] or 0)
    hsl   = float(f[38] or 0)
    lb    = float(f[49] or 0)
    cap   = float(f[44] or 0) * 1e8   # 腾讯该字段单位：亿元
    return name, price, pct, lb, hsl, cap


def check_code(code):
    name, price, pct, lb, hsl, cap = tencent_quote(code)
    passed, checks, score = analyze(pct, lb, hsl, cap, price)
    print(f"\n【单股体检】{code} {name}  现价 {price}  涨幅 {pct}%  "
          f"量比 {lb}  换手 {hsl}%  流通市值 {cap/1e8:.1f}亿")
    print("-" * 52)
    for k, v in checks.items():
        print(f"  {'[√]' if v else '[×]'}  {k}")
    print("-" * 52)
    if passed:
        print(f"  结论：符合尾盘布局初筛 | 强度分 {score}/10")
    else:
        print("  结论：不符合（见上方 × 项）")
        tips = []
        if not (PCT_MIN <= pct <= PCT_MAX):
            tips.append(f"涨幅{pct}%")
        if lb <= LB_MIN:
            tips.append(f"量比{lb}")
        if not (HSL_MIN <= hsl <= HSL_MAX):
            tips.append(f"换手{hsl}%")
        if not (CAP_MIN <= cap <= CAP_MAX):
            tips.append(f"流通市值{cap/1e8:.1f}亿")
        print("  待修正：" + "；".join(tips))


def fetch_market():
    """按涨幅榜降序翻页拉取，拉到涨幅低于下限即停。"""
    rows, pn = [], 1
    while pn <= 60:
        url = (f"http://push2.eastmoney.com/api/qt/clist/get?"
               f"pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3&fs={ALL_A}&fields={FIELDS}")
        data = http_get(url)
        d = data.get("data") or {}
        diff = d.get("diff") or []
        if not diff:
            break
        rows.extend(diff)
        total = d.get("total", 0)
        last_pct = float(diff[-1].get("f3", 0) or 0)
        if pn * 100 >= total or last_pct < PCT_MIN:
            break
        pn += 1
    return rows


def run_rank(top):
    rows = fetch_market()
    print(f"拉取全A行情 {len(rows)} 条（按涨幅榜排序），执行尾盘初筛……")
    hits = []
    for r in rows:
        pct   = float(r.get("f3", 0) or 0)
        lb    = float(r.get("f10", 0) or 0)
        hsl   = float(r.get("f8", 0) or 0)
        cap   = float(r.get("f21", 0) or 0)
        price = float(r.get("f2", 0) or 0)
        passed, checks, score = analyze(pct, lb, hsl, cap, price)
        if passed:
            hits.append((score, r.get("f12"), r.get("f14"), pct, lb, hsl, cap, price))
    hits.sort(reverse=True)
    print(f"初筛通过 {len(hits)} 只（涨幅{PCT_MIN:g}-{PCT_MAX:g}% + 量比>{LB_MIN:g} + "
          f"换手{HSL_MIN:g}-{HSL_MAX:g}% + 流通{CAP_MIN/1e8:.0f}-{CAP_MAX/1e8:.0f}亿）")
    print(f"\n得分最高的 {top} 只：\n")
    hdr = f"{'#':<3}{'代码':<8}{'名称':<10}{'现价':<8}{'涨幅%':<8}{'量比':<7}{'换手%':<7}{'流通(亿)':<9}{'强度分':<7}"
    print(hdr)
    print("-" * 60)
    for i, (score, code, name, pct, lb, hsl, cap, price) in enumerate(hits[:top], 1):
        print(f"{i:<3}{code:<8}{name:<10}{price:<8}{pct:<8}{lb:<7}{hsl:<7}{cap/1e8:<9.1f}{score:<7}")
    if not hits:
        print("（无符合条件个股，可放宽参数，或等尾盘 14:30 后再跑）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rank", action="store_true", help="全市场初筛(默认)") 
    ap.add_argument("--code")
    ap.add_argument("--top", type=int, default=2)
    args = ap.parse_args()
    if args.code:
        try:
            check_code(args.code.strip())
        except Exception as e:
            print(f"查询失败: {e}")
            sys.exit(1)
    else:
        run_rank(args.top)


if __name__ == "__main__":
    main()
