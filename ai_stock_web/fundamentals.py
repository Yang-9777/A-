#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基本面 + 估值分位 + 行业 数据层（免费、无需 token，带磁盘缓存）
----------------------------------------------------------------
三个数据源，全部避开被限流的 push2.eastmoney.com：
  1. 百度股市通  stock_zh_valuation_baidu  → PE(TTM)/PB 近5年历史分位
  2. 新浪财经    stock_financial_abstract   → ROE / 资产负债率 / 商誉 / 连续盈利年数
  3. 东财数据中心 F10(RPT_F10_BASIC_ORGINFO) → 行业（批量，datacenter 稳定）

缓存策略（写盘 JSON，跨日复用）：
  - 基本面（新浪）：季度才变 → 缓存 7 天
  - 估值分位（百度）：随价格日变 → 缓存 1 天
  - 行业（东财F10）：基本不变 → 缓存 30 天

新浪财务摘要接口有 ~1 请求/秒 的服务端限流，全市场首跑约 50 分钟；
缓存后每日增量极快（只有新上市/过期代码才需重新抓取）。
"""
import json
import os
import random
import time

import akshare as ak
import pandas as pd
import numpy as np
import requests

_DIR = os.path.dirname(os.path.abspath(__file__))
FUND_CACHE_PATH = os.path.join(_DIR, "fund_cache.json")
VAL_CACHE_PATH = os.path.join(_DIR, "val_cache.json")
IND_CACHE_PATH = os.path.join(_DIR, "industry_cache.json")


# ----------------------------------------------------------------------
# 通用磁盘缓存
# ----------------------------------------------------------------------
class DiskCache:
    def __init__(self, path):
        self.path = path
        self.data = {}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def get(self, key, ttl_seconds):
        ent = self.data.get(str(key))
        if not ent or not isinstance(ent, dict):
            return None
        if time.time() - float(ent.get("ts", 0)) > ttl_seconds:
            return None
        return ent.get("v")

    def set(self, key, value):
        self.data[str(key)] = {"ts": time.time(), "v": value}

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------
def _to_f(v):
    """NaN/inf/None → None，否则 float。"""
    if v is None:
        return None
    try:
        f = float(v)
        return None if pd.isna(f) or np.isinf(f) else f
    except (TypeError, ValueError):
        return None


def _pct(vals, last):
    """last 在 vals 中的分位（0~1，越大越高）。"""
    if not vals or last is None:
        return None
    arr = np.asarray([x for x in vals if x is not None], dtype=float)
    if len(arr) < 60:
        return None
    return float((arr <= last).mean())


# ----------------------------------------------------------------------
# 1. 估值分位（百度股市通）
# ----------------------------------------------------------------------
def fetch_valuation_percentile(code: str, cache: DiskCache = None):
    """返回 dict(pe_percentile, pb_percentile)，均为 0~1 或 None。"""
    code = str(code).zfill(6)
    if cache is not None:
        hit = cache.get(code, 86400)  # 1 天 TTL
        if hit is not None:
            return hit
    out = {"pe_percentile": None, "pb_percentile": None}
    for key, indicator in (("pe_percentile", "市盈率(TTM)"), ("pb_percentile", "市净率")):
        try:
            df = ak.stock_zh_valuation_baidu(symbol=code, indicator=indicator, period="近五年")
            vals = pd.to_numeric(df["value"], errors="coerce").dropna().tolist()
            if vals:
                out[key] = _pct(vals, vals[-1])
        except Exception:
            out[key] = None
    if cache is not None:
        cache.set(code, out)
    return out


# ----------------------------------------------------------------------
# 2. 基本面（新浪财务摘要）
# ----------------------------------------------------------------------
def _extract_fundamentals(code):
    """从新浪财务摘要提取基本面指标（不缓存，纯抓取+解析）。"""
    out = {"roe": None, "debt_ratio": None,
           "goodwill_to_equity": None, "consecutive_profit_years": None}
    df = ak.stock_financial_abstract(symbol=code)
    if df is None or df.empty or "指标" not in df.columns:
        return out

    cols = df.columns.tolist()
    datecols = [c for c in cols[2:] if str(c)[:4].isdigit()]
    annual = [c for c in datecols if str(c).endswith("1231")]

    def _latest(name, group=None, cols_=datecols):
        m = df[df["指标"] == name]
        if group:
            m = m[m["选项"] == group]
        if m.empty:
            return None
        row = m.iloc[0]
        for c in cols_:
            v = _to_f(row[c])
            if v is not None:
                return v
        return None

    def _annual_series(name, group=None):
        m = df[df["指标"] == name]
        if group:
            m = m[m["选项"] == group]
        if m.empty:
            return []
        row = m.iloc[0]
        return [_to_f(row[c]) for c in annual]

    out["roe"] = _latest("净资产收益率(ROE)", "常用指标", annual)
    out["debt_ratio"] = _latest("资产负债率", "财务风险")
    goodwill = _latest("商誉", "常用指标")
    equity = _latest("股东权益合计(净资产)", "常用指标")
    if goodwill is not None and equity not in (None, 0):
        out["goodwill_to_equity"] = goodwill / equity * 100.0

    prof = _annual_series("扣非净利润", "常用指标")
    if not any(v is not None and v > 0 for v in prof):
        prof = _annual_series("归母净利润", "常用指标")
    # 区分「无数据(未知)」与「最新年度亏损」：避免把取不到净利润误判成亏损
    valid = [v for v in prof if v is not None]
    if not valid:
        out["consecutive_profit_years"] = None
        return out
    consec = 0
    for v in prof:  # annual 已按最新在前排列
        if v is not None and v > 0:
            consec += 1
        else:
            break
    out["consecutive_profit_years"] = consec
    return out


def fetch_fundamentals(code: str, cache: DiskCache = None):
    """返回 dict(roe, debt_ratio, goodwill_to_equity, consecutive_profit_years)。"""
    code = str(code).zfill(6)
    if cache is not None:
        hit = cache.get(code, 7 * 86400)  # 7 天 TTL
        if hit is not None:
            return hit
    try:
        out = _extract_fundamentals(code)
    except Exception:
        out = {"roe": None, "debt_ratio": None,
               "goodwill_to_equity": None, "consecutive_profit_years": None}
    # 仅在至少取到一项有效数据时才缓存，避免把"抓取失败(全None)"污染进 7 天缓存
    if cache is not None and any(v is not None for v in out.values()):
        cache.set(code, out)
    return out


# ----------------------------------------------------------------------
# 3. 行业（东财数据中心 F10，批量，稳定）
# ----------------------------------------------------------------------
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://data.eastmoney.com/stock/lhb.html",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
_BASE_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_session = None


def _get_session():
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def _secucode(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith("6"):
        return f"{code}.SH"
    if code.startswith(("4", "8", "9")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def _parse_industry(em2016):
    parts = (em2016 or "").split("-")
    if len(parts) >= 2:
        return parts[1]
    return parts[0] if parts else ""


def fetch_industry_map(codes, cache: DiskCache = None):
    """批量获取行业，返回 {code: industry}。codes 为股票代码集合。"""
    codes = [str(c).zfill(6) for c in codes]
    result = {}
    missing = []
    if cache is not None:
        for c in codes:
            v = cache.get(c, 30 * 86400)  # 30 天 TTL
            if v:
                result[c] = v
            else:
                missing.append(c)
    else:
        missing = codes

    for i in range(0, len(missing), 50):
        batch = missing[i:i + 50]
        secucodes = ",".join(f'"{_secucode(c)}"' for c in batch)
        params = {
            "reportName": "RPT_F10_BASIC_ORGINFO",
            "columns": "SECUCODE,SECURITY_CODE,EM2016",
            "filter": f"(SECUCODE in ({secucodes}))",
            "pageNumber": "1",
            "pageSize": str(len(batch) + 5),
            "source": "WEB",
            "client": "WEB",
        }
        try:
            time.sleep(random.uniform(0.2, 0.8))
            resp = _get_session().get(_BASE_URL, params=params, headers=_HEADERS, timeout=15)
            if resp.status_code in (403, 429, 418):
                continue
            resp.raise_for_status()
            data = ((resp.json() or {}).get("result") or {}).get("data") or []
            for d in data:
                c = str(d.get("SECURITY_CODE", "")).zfill(6)
                sec = _parse_industry(d.get("EM2016", ""))
                if c and sec:
                    result[c] = sec
                    if cache is not None:
                        cache.set(c, sec)
        except (requests.RequestException, ValueError):
            continue
    return result


# ----------------------------------------------------------------------
# 4. 一键增强快照
# ----------------------------------------------------------------------
def enrich_snapshot(snap, valuation=None, fundamentals=None, industry=None):
    """把估值分位/基本面/行业写入 StockSnapshot。"""
    if valuation:
        if snap.pe_percentile is None:
            snap.pe_percentile = valuation.get("pe_percentile")
        if snap.pb_percentile is None:
            snap.pb_percentile = valuation.get("pb_percentile")
    if fundamentals:
        if snap.roe is None:
            snap.roe = fundamentals.get("roe")
        if snap.debt_ratio is None:
            snap.debt_ratio = fundamentals.get("debt_ratio")
        if snap.goodwill_to_equity is None:
            snap.goodwill_to_equity = fundamentals.get("goodwill_to_equity")
        if snap.consecutive_profit_years is None:
            snap.consecutive_profit_years = fundamentals.get("consecutive_profit_years")
    if industry:
        snap.industry = industry
    return snap
