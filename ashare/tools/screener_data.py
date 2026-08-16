#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
尾盘打法 · 数据层（稳健版）
============================
- 全市场涨幅榜：新浪 Market_Center 接口（东财 clist 被限流时的兜底，数据源稳定）
- 量比：腾讯行情批量接口（qt.gtimg.cn，一次可查约50只）
- 单股体检：腾讯行情

对外函数：
  get_rank(main_only=True) -> (hits, meta)
  get_check(code)           -> dict
"""

import json
import time
import urllib.request

# —— 尾盘打法参数 ——
PCT_MAIN = (3.0, 5.0)    # 主板 ±10%
PCT_20CM = (6.0, 10.0)   # 创业板/科创板 ±20%
PCT_BJ   = (9.0, 15.0)   # 北交所 ±30%
LB_MIN   = 1.0
LB_BEST  = (1.5, 3.0)
HSL_MIN  = 5.0
HSL_MAX  = 10.0
CAP_MIN  = 50e8
CAP_MAX  = 200e8

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
       "Referer": "https://finance.sina.com.cn/"}


def _http(url, enc="utf-8", retries=4, raw=False):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
            return data if raw else json.loads(data.decode(enc))
        except Exception as e:
            last = e
            time.sleep(0.7 * (i + 1))
    raise last


def board_of(code):
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301", "302")):
        return "创业板"
    if code.startswith("60"):
        return "沪主板"
    if code.startswith("00"):
        return "深主板"
    if code.startswith(("83", "87", "43", "92")):
        return "北交所"
    if code.startswith("900"):
        return "沪B"
    if code.startswith("200"):
        return "深B"
    return "其他"


def is_main_board(code):
    return code.startswith("00") or (code.startswith("60") and not code.startswith(("688", "689")))


def pct_range(code):
    if code.startswith(("688", "689", "300", "301", "302")):
        return PCT_20CM
    if code.startswith(("83", "87", "43", "92")):
        return PCT_BJ
    return PCT_MAIN


def _sina_symbol(sym):
    # 新浪 symbol 形如 sh600213 / sz002213 / bj920083
    return sym


def fetch_sina_rank(max_pages=8, page_size=100):
    """返回按涨幅降序的全A列表，拉到涨幅低于3%即停。"""
    rows, page = [], 1
    while page <= max_pages:
        url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               "Market_Center.getHQNodeData?page=%d&num=%d&sort=changepercent&asc=0"
               "&node=hs_a&symbol=&_s_r_a=page" % (page, page_size))
        try:
            data = _http(url)
        except Exception:
            break
        if not data:
            break
        rows.extend(data)
        last = data[-1]
        if float(last.get("changepercent", 0) or 0) < PCT_MAIN[0]:
            break
        if len(data) < page_size:
            break
        page += 1
    return rows


def tencent_batch(symbols):
    """symbols: ['sh600213', 'sz002213', ...] -> {symbol: {...}}"""
    out = {}
    for i in range(0, len(symbols), 50):
        chunk = symbols[i:i + 50]
        url = "http://qt.gtimg.cn/q=" + ",".join(chunk)
        try:
            txt = _http(url, enc="gbk", raw=True).decode("gbk", "ignore")
        except Exception:
            continue
        for line in txt.split(";"):
            line = line.strip()
            if "=" not in line:
                continue
            sym, body = line.split("=", 1)
            sym = sym.replace("v_", "").strip()
            body = body.strip().strip('"')
            f = body.split("~")
            if len(f) < 50:
                continue
            try:
                out[sym] = {
                    "name": f[1],
                    "price": float(f[3] or 0),
                    "prev_close": float(f[4] or 0),
                    "open": float(f[5] or 0),
                    "pct": float(f[32] or 0),
                    "high": float(f[33] or 0),
                    "low": float(f[34] or 0),
                    "hsl": float(f[38] or 0),
                    "lb": float(f[49] or 0),
                    "vwap": float(f[51] or 0),       # 分时均价
                    "ts": f[30].strip(),             # 行情时间 YYYYMMDDHHMMSS
                    "cap": float(f[44] or 0) * 1e8,  # 亿元 -> 元
                }
            except (ValueError, IndexError):
                continue
    return out


def is_trading_day_now():
    """用上证指数行情时间判断今天是否交易日（顺带识别节假日）。"""
    import datetime
    try:
        q = tencent_batch(["sh000001"]).get("sh000001")
        if not q or not q.get("ts"):
            return datetime.datetime.now().weekday() < 5
        return q["ts"][:8] == datetime.datetime.now().strftime("%Y%m%d")
    except Exception:
        return datetime.datetime.now().weekday() < 5


def score(pct, lb, hsl, cap, price, lo=PCT_MAIN[0], hi=PCT_MAIN[1]):
    s = 0.0
    s += 2 if LB_BEST[0] <= lb <= LB_BEST[1] else (1 if lb > 1 else 0)
    s += 2 if 7.0 <= hsl <= 8.5 else (1 if 6.0 <= hsl < 7.0 or 8.5 < hsl <= 9.5 else 0)
    lo2 = lo + (hi - lo) * 0.2       # 涨幅位置：区间中部偏上最优(有空间且未过热)
    hi2 = hi - (hi - lo) * 0.08
    s += 2 if lo2 <= pct <= hi2 else (1 if lo <= pct < lo2 else 0)
    s += 2 if 70e8 <= cap <= 150e8 else (1 if CAP_MIN <= cap < 70e8 or 150e8 < cap <= CAP_MAX else 0)
    s += 2 if 5 <= price <= 80 else (1 if 3 <= price < 5 else 0)
    return round(s, 1)


def _checks(pct, lb, hsl, cap):
    return {
        "涨幅区间": PCT_MAIN[0] <= pct <= PCT_MAIN[1],
        "量比>1": lb > LB_MIN,
        "换手5-10%": HSL_MIN <= hsl <= HSL_MAX,
        "流通50-200亿": CAP_MIN <= cap <= CAP_MAX,
    }


def get_rank(main_only=True):
    rows = fetch_sina_rank()
    hits = []
    for r in rows:
        try:
            sym = r.get("symbol", "")
            code = r.get("code", "") or sym[2:]
            name = r.get("name", "")
            pct = float(r.get("changepercent", 0) or 0)
            hsl = float(r.get("turnoverratio", 0) or 0)
            cap = float(r.get("nmc", 0) or 0) * 10000  # 万元 -> 元
            price = float(r.get("trade", 0) or 0)
        except (ValueError, TypeError):
            continue
        if not code or "ST" in name.upper() or "退" in name:
            continue
        if main_only and not is_main_board(code):
            continue
        lo, hi = pct_range(code)
        if not (lo <= pct <= hi):
            continue
        if not (HSL_MIN <= hsl <= HSL_MAX):
            continue
        if not (CAP_MIN <= cap <= CAP_MAX):
            continue
        hits.append({"sym": sym, "code": code, "name": name, "price": price,
                     "pct": pct, "hsl": hsl, "cap": cap, "board": board_of(code)})

    # 批量补量比
    symbols = [h["sym"] for h in hits]
    quotes = tencent_batch(symbols) if symbols else {}
    warning = None
    if symbols and not quotes:
        warning = "量比数据源(腾讯)无响应，本批结果可能为空"

    result = []
    for h in hits:
        code = h["code"]
        q = quotes.get(h["sym"]) or {}
        lb = q.get("lb", 0) or 0
        # 腾讯量比缺失则跳过（拿不到量比，宁缺毋滥）
        if lb <= LB_MIN:
            continue
        pct = q.get("pct", h["pct"])
        hsl = q.get("hsl", h["hsl"])
        cap = q.get("cap", h["cap"])
        price = q.get("price", h["price"])
        if not (HSL_MIN <= hsl <= HSL_MAX):
            continue
        if not (CAP_MIN <= cap <= CAP_MAX):
            continue
        lo, hi = pct_range(code)
        h.update({"lb": lb, "pct": pct, "hsl": hsl, "cap": cap, "price": price,
                  "buyable": is_main_board(code),
                  "score": score(pct, lb, hsl, cap, price, lo, hi)})
        result.append(h)

    result.sort(key=lambda x: (x["score"], x["pct"]), reverse=True)
    meta = {
        "cand_count": len(hits),
        "pass_count": len(result),
        "top2": result[:2],
        "list": result,
        "main_only": main_only,
        "warning": warning,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return result, meta


def get_check(code):
    code = str(code).strip()
    sym = ("bj" if code.startswith(("43", "83", "87", "92")) else
           "sh" if code.startswith(("6", "9")) else "sz") + code
    q = tencent_batch([sym]).get(sym)
    if not q:
        return None
    pct, lb, hsl, cap, price = q["pct"], q["lb"], q["hsl"], q["cap"], q["price"]
    passed = all(_checks(pct, lb, hsl, cap).values())
    return {
        "code": code,
        "name": q["name"],
        "board": board_of(code),
        "price": price,
        "pct": pct,
        "lb": lb,
        "hsl": hsl,
        "cap": cap,
        "checks": _checks(pct, lb, hsl, cap),
        "buyable": is_main_board(code),
        "passed": passed,
        "score": score(pct, lb, hsl, cap, price, *pct_range(code)) if passed else 0,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


if __name__ == "__main__":
    hits, meta = get_rank()
    print("候选", meta["cand_count"], "通过", meta["pass_count"])
    for i, h in enumerate(meta["top2"], 1):
        print(f"  TOP{i} {h['code']} {h['name']} 涨{h['pct']}% 量比{h['lb']} 换手{h['hsl']}% 市值{h['cap']/1e8:.0f}亿 分{h['score']}")
