#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股选股 + 游资数据 + 开盘监测 + 进场/出场提醒 —— 决策支持系统
================================================================
定位（务必理解）：
  - 本系统只输出"观察/提醒"，绝不自动下单，人做最终决策
  - 游资数据作为"情绪/确认因子"（小权重、只加减分），不做核心进场依据
  - 核心进场信号 = 位置(估值分位) + 量价 + 基本面；核心防线 = 风控

模块：股票池过滤 → 综合评分排序 → 竞价监测 → 盘中监测 → 进场提醒 → 风控 → 出场提醒 → 复盘
依赖：复用 stock_scoring.py 的 v3 评分引擎
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, List, Tuple, Dict
from enum import Enum

from stock_scoring import (
    StockSnapshot, ScoringEngine, Config,
    attach_support, detect_board, MAIN, Backtest,
)

# ======================================================================
# 0. 游资席位库（可替换成你昨天部署的真实数据）
# ======================================================================
FAMOUS_HOT_MONEY_SEATS = ["章盟主", "赵老哥", "炒股养家", "深圳益田路",
                          "宁波桑田路", "杭州上塘路", "成都北一环路", "上海溧阳路"]
RETAIL_SEATS = ["拉萨团结路", "拉萨东环路", "拉萨金珠西路"]  # 常被视为散户/接盘


def classify_seat(name: str) -> Tuple[str, float]:
    """席位分类：机构专用 / 知名游资 / 散户接盘 / 普通。"""
    if "机构专用" in name:
        return "institution", 30.0
    if any(k in name for k in FAMOUS_HOT_MONEY_SEATS):
        return "famous", 15.0
    if any(k in name for k in RETAIL_SEATS):
        return "retail", -10.0
    return "normal", 0.0


# ======================================================================
# 1. 游资数据模型
# ======================================================================
@dataclass
class HotMoneyRecord:
    date: str
    code: str
    name: str
    reason: str                                   # 上榜原因
    net_buy: float = 0.0                          # 龙虎榜净买入(万元)
    buy_brokers: List[Tuple[str, float]] = field(default_factory=list)   # [(席位, 买入额万元)]
    sell_brokers: List[Tuple[str, float]] = field(default_factory=list)  # [(席位, 卖出额万元)]
    turnover_value: float = 0.0                   # 当日成交额(万元)
    float_mv: float = 0.0                         # 流通市值(万元)
    consecutive_days_on_list: int = 1             # 连续上榜天数


class HotMoneyAnalyzer:
    """把龙虎榜/游资数据转成：调整分(±20) + 风险标记 + 明细。

    核心逻辑：
      - 净买入占比越高越加分；净卖出大幅减分
      - 机构专用/知名游资买入加分；拉萨帮接盘减分
      - 连续多日上榜(高位接力)减分；首次上榜+净买入加分
      - 涨幅偏离类上榜(追高)减分
    """

    def __init__(self, max_adjust: float = 20.0):
        self.max_adjust = max_adjust

    def analyze(self, rec: HotMoneyRecord) -> Dict:
        adj = 0.0
        flags: List[str] = []
        detail: List[str] = []

        tv = max(rec.turnover_value, 1.0)
        ratio = rec.net_buy / tv
        detail.append(f"龙虎榜净买入 {rec.net_buy:.0f}万 / 成交 {tv:.0f}万 = {ratio*100:.1f}%")

        if ratio > 0.05:
            adj += 8; detail.append("净买入占比>5% +8")
        elif ratio > 0.02:
            adj += 5; detail.append("净买入占比>2% +5")
        elif ratio > 0:
            adj += 2; detail.append("小幅净买入 +2")
        elif ratio < -0.05:
            adj -= 10; flags.append("游资大幅净卖出"); detail.append("净卖出占比<-5% -10")
        elif ratio < -0.02:
            adj -= 5; flags.append("游资净卖出"); detail.append("净卖出占比<-2% -5")

        # 席位质量
        buy_score = 0.0
        inst_buy = famous_buy = retail_buy = 0.0
        for name, amt in rec.buy_brokers:
            kind, sc = classify_seat(name)
            if kind == "institution":
                inst_buy += amt; buy_score += sc
            elif kind == "famous":
                famous_buy += amt; buy_score += sc
            elif kind == "retail":
                retail_buy += amt
            else:
                buy_score += sc
        if inst_buy > 0:
            adj += 5; detail.append(f"机构专用席位买入 {inst_buy:.0f}万 +5")
        if famous_buy > 0:
            adj += 3; detail.append(f"知名游资席位买入 {famous_buy:.0f}万 +3")
        if retail_buy > 0 and retail_buy >= sum(a for _, a in rec.buy_brokers) * 0.5:
            adj -= 5; flags.append("拉萨帮接盘特征"); detail.append("散户接盘席位占主导 -5")

        # 买方集中度（买方前5占买卖合计比例）
        buy_total = sum(a for _, a in rec.buy_brokers)
        sell_total = sum(a for _, a in rec.sell_brokers)
        if buy_total + sell_total > 0:
            conc = buy_total / (buy_total + sell_total)
            if conc > 0.7:
                adj += 3; detail.append(f"买方集中度 {conc*100:.0f}% 抢筹明显 +3")
            elif conc < 0.35:
                adj -= 5; flags.append("卖方占优，出货迹象"); detail.append(f"买方集中度 {conc*100:.0f}% 偏低 -5")

        # 连续性
        if rec.consecutive_days_on_list >= 3:
            adj -= 5; flags.append("连续多日上榜，高位接力风险"); detail.append("连续上榜≥3天 -5")
        elif rec.consecutive_days_on_list == 1 and ratio > 0:
            adj += 3; detail.append("首次上榜且净买入 +3")

        # 上榜原因
        if any(k in rec.reason for k in ("涨幅偏离", "振幅", "连续三个交易日")):
            adj -= 3; flags.append("追高型上榜"); detail.append(f"上榜原因[{rec.reason}] 追高 -3")

        adj = max(-self.max_adjust, min(self.max_adjust, adj))
        return {"adjustment": round(adj, 1),
                "flags": flags,
                "detail": detail,
                "net_buy_ratio": round(ratio * 100, 2)}


# ======================================================================
# 2. 盘中/竞价数据模型
# ======================================================================
@dataclass
class RealtimeQuote:
    code: str
    time: str                  # "09:25" 竞价 / "10:30" 盘中
    price: float               # 现价/竞价价
    pre_close: float
    open_price: float
    high: float
    low: float
    volume: float              # 当日累计成交量(手)
    volume_ratio: float        # 量比
    auction_amount: float = 0.0  # 竞价金额(万元)
    bid1_volume: float = 0.0     # 买一量(手)
    ask1_volume: float = 0.0     # 卖一量(手)


# ======================================================================
# 3. 账户与风控
# ======================================================================
@dataclass
class Account:
    equity: float = 1_000_000.0        # 总资金
    cash: float = 1_000_000.0
    positions: Dict[str, Dict] = field(default_factory=dict)
    max_single_position: float = 0.10  # 单票上限
    max_total_position: float = 0.80   # 总仓位上限
    max_daily_trades: int = 3          # 每日最多操作数
    trades_today: int = 0


class RiskManager:
    """仓位管理 + 止损止盈计算 + 熔断。这是整个系统最重要的一层。"""

    def __init__(self, fixed_stop: float = 0.07, fixed_take: float = 0.15):
        self.fixed_stop = fixed_stop
        self.fixed_take = fixed_take

    def suggest_position(self, score: float, risk_level: str, acct: Account) -> float:
        """按得分和风险等级建议单票仓位比例。"""
        if risk_level == "极高" or acct.trades_today >= acct.max_daily_trades:
            return 0.0
        if risk_level == "高":
            base = 0.02
        elif risk_level == "中":
            base = 0.05
        else:
            base = 0.08 if score >= 70 else 0.05
        # 已用仓位约束
        used = sum(p.get("market_value", 0) for p in acct.positions.values()) / acct.equity
        room = acct.max_total_position - used
        return max(0.0, min(base, acct.max_single_position, room))

    def stop_take(self, entry_price: float, support_levels: Dict[str, float]) -> Tuple[float, float]:
        """止损价(取更高、更早触发)，止盈价。"""
        fixed_stop = entry_price * (1 - self.fixed_stop)
        stops = [fixed_stop]
        for lv in support_levels.values():
            if lv < entry_price and lv > fixed_stop:
                stops.append(lv)
        stop = max(stops)
        take = entry_price * (1 + self.fixed_take)
        return round(stop, 3), round(take, 3)


# ======================================================================
# 4. 选股器
# ======================================================================
@dataclass
class RankedStock:
    snapshot: StockSnapshot
    base_score: float
    hm_adjustment: float
    combined_score: float
    hm_flags: List[str]
    hm_detail: List[str]


class StockSelector:
    def __init__(self, engine: Optional[ScoringEngine] = None,
                 hm: Optional[HotMoneyAnalyzer] = None,
                 hm_weight: float = 0.25):
        self.engine = engine or ScoringEngine()
        self.hm = hm or HotMoneyAnalyzer()
        self.hm_weight = hm_weight  # 游资调整分在综合分里的权重(注意: 只做加减, 上限±20*0.25)

    def filter_universe(self, snaps: List[StockSnapshot]) -> List[StockSnapshot]:
        """排除非主板、ST风险、重大负面。"""
        keep = []
        for s in snaps:
            if detect_board(s.code) != MAIN:
                continue
            if s.is_st_risk or s.has_major_negative_news:
                continue
            keep.append(s)
        return keep

    def score_and_rank(self, snaps: List[StockSnapshot],
                       hm_records: Dict[str, HotMoneyRecord]) -> List[RankedStock]:
        out: List[RankedStock] = []
        for s in snaps:
            res = self.engine.evaluate(s)
            if not res["valid"]:
                continue
            base = res["final_score"]
            rec = hm_records.get(s.code)
            adj, flags, detail = 0.0, [], []
            if rec is not None:
                r = self.hm.analyze(rec)
                adj, flags, detail = r["adjustment"], r["flags"], r["detail"]
            combined = max(0.0, min(100.0, base + adj * self.hm_weight))
            out.append(RankedStock(s, base, adj, round(combined, 1), flags, detail))
        out.sort(key=lambda x: x.combined_score, reverse=True)
        return out


# ======================================================================
# 5. 信号引擎（进场/出场提醒）
# ======================================================================
class SignalType(Enum):
    WATCH = "观察"
    ENTRY_ALERT = "进场观察提醒"
    HOLD = "持有"
    EXIT_ALERT = "出场/减仓提醒"
    SKIP = "排除"


@dataclass
class Signal:
    stype: SignalType
    code: str
    name: str
    score: float
    reasons: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    entry_ref_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    position_pct: Optional[float] = None


class SignalEngine:
    """把 评分+竞价/盘中+游资+风控 转成可读的提醒。"""

    def __init__(self, selector: StockSelector, risk: RiskManager):
        self.selector = selector
        self.risk = risk

    def auction_check(self, q: RealtimeQuote) -> Tuple[bool, List[str]]:
        """开盘竞价是否健康：高开幅度 + 竞价量能 + 非一字板。"""
        chg = (q.price / q.pre_close - 1) * 100
        notes = [f"竞价 {q.price:.2f} 高开 {chg:+.1f}%"]
        ok = True
        if chg > 7:
            ok = False; notes.append("高开超7%，追高风险")
        elif chg < -2:
            ok = False; notes.append("低开超2%，弱势")
        if q.auction_amount < 500:
            notes.append("竞价金额偏低(参考)")
        if q.bid1_volume > 0 and q.ask1_volume > 0:
            if q.ask1_volume > q.bid1_volume * 3:
                ok = False; notes.append("竞价卖压明显(卖一量/买一量>3)")
        return ok, notes

    def entry_alert(self, ranked: RankedStock, q: RealtimeQuote, acct: Account) -> Signal:
        s = ranked.snapshot
        base = self.selector.engine.evaluate(s)
        risk_level = base["risk_level"]
        notes: List[str] = []
        risks: List[str] = list(ranked.hm_flags)
        ok = True

        if ranked.combined_score < 60:
            ok = False; notes.append(f"综合分 {ranked.combined_score} < 60，不进观察池")
        if risk_level in ("高", "极高"):
            ok = False; notes.append(f"风险等级 {risk_level}，不进场")
        # 量价确认：盘中放量 + 站上均价/突破参考
        if q.volume_ratio < 1.0:
            ok = False; notes.append(f"量比 {q.volume_ratio:.1f} 不足，未见放量")
        elif q.volume_ratio > 4.0:
            risks.append("量比过高，警惕情绪过热")
        if q.price < q.pre_close * 0.98:
            ok = False; notes.append("盘中跌幅过大，不进场")

        # 竞价健康度
        auction_ok, auction_notes = self.auction_check(q)
        notes += auction_notes
        if not auction_ok:
            ok = False

        pos = self.risk.suggest_position(ranked.combined_score, risk_level, acct)
        if pos <= 0:
            ok = False
            notes.append("仓位不可用（单票/总仓位/日操作次数受限）")

        if not ok:
            return Signal(SignalType.WATCH, s.code, s.name, ranked.combined_score,
                          reasons=notes, risks=risks)

        stop, take = self.risk.stop_take(q.price, s.support_levels)
        entry = q.price
        notes.append(f"综合分 {ranked.combined_score}，风险 {risk_level}")
        notes.append(f"进场参考价 {entry:.2f}，止损 {stop:.2f}，止盈 {take:.2f}")
        notes.append(f"建议仓位 {pos*100:.1f}%（单票上限 {acct.max_single_position*100:.0f}%）")
        return Signal(SignalType.ENTRY_ALERT, s.code, s.name, ranked.combined_score,
                      reasons=notes, risks=risks,
                      entry_ref_price=entry, stop_loss=stop, take_profit=take, position_pct=pos)

    def exit_check(self, held: Dict, q: RealtimeQuote,
                   hm_rec: Optional[HotMoneyRecord], base_result: Dict) -> Signal:
        """持有中检查：止损/止盈/风险触发/游资出货 → 出场提醒。"""
        code = q.code
        entry = held.get("entry_price", q.pre_close)
        stop = held.get("stop_loss")
        take = held.get("take_profit")
        reasons: List[str] = []
        risks: List[str] = []

        if stop and q.price <= stop:
            reasons.append(f"触发止损 {stop:.2f}（现价 {q.price:.2f}）")
        if take and q.price >= take:
            reasons.append(f"达到止盈位 {take:.2f}（现价 {q.price:.2f}）")
        if base_result["risk_level"] in ("高", "极高"):
            risks.append(f"风险等级升为 {base_result['risk_level']}")
        for t in base_result["sell_triggers"]:
            reasons.append(t)
        if hm_rec is not None:
            r = self.selector.hm.analyze(hm_rec)
            for f in r["flags"]:
                risks.append(f"游资信号：{f}")
            if r["adjustment"] <= -10:
                reasons.append("游资明显出货，建议减仓")

        if reasons:
            return Signal(SignalType.EXIT_ALERT, code, held.get("name", code),
                          base_result["final_score"], reasons=reasons, risks=risks)
        return Signal(SignalType.HOLD, code, held.get("name", code),
                      base_result["final_score"],
                      reasons=[f"持有中，现价 {q.price:.2f}，止损 {stop}，止盈 {take}"],
                      risks=risks)


# ======================================================================
# 6. 演示
# ======================================================================
def _mk_snap(code, name, industry, *, pe, pb, px, profit, roe, debt, goodw, chip,
             inst_qoq, inst_hold, north, vol_ratio, ret20, vol_ann, sup_prices, cur_price):
    s = StockSnapshot(code=code, name=name, industry=industry,
                      pe_percentile=pe, pb_percentile=pb, price_percentile=px,
                      consecutive_profit_years=profit, roe=roe, debt_ratio=debt,
                      goodwill_to_equity=goodw, industry_quality=60.0,
                      chip_peak=chip, low_chip_concentration=0.6, high_trapped_trend="decreasing",
                      inst_count_qoq=inst_qoq, inst_holding_qoq=inst_hold, inst_rising_quarters=1,
                      north_inflow_quarters=north, north_holding_change_3m=0.3,
                      volume_ratio=vol_ratio, ret_20d=ret20, annual_volatility=vol_ann,
                      turnover_rate=3.0)
    return attach_support(s, sup_prices, cur_price)


def demo():
    engine = ScoringEngine()
    selector = StockSelector(engine)
    risk = RiskManager()
    sig_engine = SignalEngine(selector, risk)

    # ---- 候选股票池（3只主板） ----
    snaps = [
        _mk_snap("600001", "低位蓝筹A", "银行", pe=0.15, pb=0.12, px=0.18, profit=5,
                 roe=12, debt=90, goodw=2, chip="low", inst_qoq=5, inst_hold=0.6,
                 north=3, vol_ratio=1.6, ret20=4, vol_ann=22,
                 sup_prices=[10+i*0.01 for i in range(60)], cur_price=10.8),
        _mk_snap("600002", "题材炒作B", "传媒", pe=0.92, pb=0.90, px=0.95, profit=-1,
                 roe=-5, debt=70, goodw=45, chip="high", inst_qoq=-6, inst_hold=-5,
                 north=-3, vol_ratio=6, ret20=70, vol_ann=60,
                 sup_prices=[20-i*0.1 for i in range(60)], cur_price=15.0),
        _mk_snap("600003", "低位转强C", "半导体", pe=0.35, pb=0.40, px=0.42, profit=3,
                 roe=11, debt=38, goodw=12, chip="low", inst_qoq=8, inst_hold=1.2,
                 north=2, vol_ratio=2.2, ret20=12, vol_ann=30,
                 sup_prices=[30+i*0.06 for i in range(60)], cur_price=33.5),
    ]

    # ---- 游资数据（模拟你昨天部署的数据源） ----
    hm_records = {
        "600001": HotMoneyRecord("2026-08-14", "600001", "低位蓝筹A", "日换手率达15%",
                                 net_buy=8000, turnover_value=200000, float_mv=50_000_000,
                                 buy_brokers=[("机构专用", 5000), ("章盟主", 3000)],
                                 sell_brokers=[("普通营业部A", 800)],
                                 consecutive_days_on_list=1),
        "600002": HotMoneyRecord("2026-08-14", "600002", "题材炒作B", "连续三个交易日涨幅偏离20%",
                                 net_buy=-15000, turnover_value=300000, float_mv=8_000_000,
                                 buy_brokers=[("拉萨团结路", 6000)],
                                 sell_brokers=[("知名游资席位", 18000)],
                                 consecutive_days_on_list=4),
        "600003": HotMoneyRecord("2026-08-14", "600003", "低位转强C", "日换手率达20%",
                                 net_buy=6000, turnover_value=120000, float_mv=20_000_000,
                                 buy_brokers=[("机构专用", 2500), ("宁波桑田路", 3500)],
                                 sell_brokers=[("普通营业部B", 1000)],
                                 consecutive_days_on_list=1),
    }

    # ---- 选股 ----
    universe = selector.filter_universe(snaps)
    ranked = selector.score_and_rank(universe, hm_records)
    print("=" * 66)
    print("  第一步：收盘后选股（v3 基本面评分 + 游资因子）")
    print("=" * 66)
    for r in ranked:
        print(f"  {r.snapshot.code} {r.snapshot.name}  "
              f"基础分 {r.base_score:.0f}  游资调整 {r.hm_adjustment:+.0f}  "
              f"综合 {r.combined_score}")
        if r.hm_flags:
            print(f"      ⚠ 游资风险标记：{'、'.join(r.hm_flags)}")
        for d in r.hm_detail:
            print(f"        · {d}")
    print()

    # ---- 开盘竞价 + 进场提醒（针对第一名） ----
    top = ranked[0]
    print("=" * 66)
    print(f"  第二步：次日开盘监测 → {top.snapshot.name}")
    print("=" * 66)
    acct = Account()
    q = RealtimeQuote(code=top.snapshot.code, time="09:25",
                      price=top.snapshot.current_price * 1.02,   # 高开2%
                      pre_close=top.snapshot.current_price,
                      open_price=top.snapshot.current_price * 1.02,
                      high=top.snapshot.current_price * 1.03,
                      low=top.snapshot.current_price * 1.01,
                      volume=50000, volume_ratio=2.0,
                      auction_amount=1500, bid1_volume=3000, ask1_volume=1500)
    sig = sig_engine.entry_alert(top, q, acct)
    print(f"  信号：{sig.stype.value}")
    for r in sig.reasons:
        print(f"    - {r}")
    for r in sig.risks:
        print(f"    ⚠ {r}")
    if sig.stype == SignalType.ENTRY_ALERT:
        held = {"name": top.snapshot.name, "entry_price": q.price,
                "stop_loss": sig.stop_loss, "take_profit": sig.take_profit}
        print(f"    落地：以 {q.price:.2f} 进场，仓位 {sig.position_pct*100:.1f}%，"
              f"止损 {sig.stop_loss:.2f}，止盈 {sig.take_profit:.2f}")
    print()

    # ---- 持仓中的出场检查 ----
    print("=" * 66)
    print("  第三步：持仓中的出场监测（模拟后续行情）")
    print("=" * 66)
    if sig.stype == SignalType.ENTRY_ALERT:
        # 场景1：盘中跌破止损
        q2 = RealtimeQuote(code=top.snapshot.code, time="14:00",
                           price=sig.stop_loss * 0.99,
                           pre_close=q.price, open_price=q.price,
                           high=q.price, low=sig.stop_loss * 0.98,
                           volume=80000, volume_ratio=3.0)
        base2 = engine.evaluate(top.snapshot)
        ex = sig_engine.exit_check(held, q2, hm_records.get(top.snapshot.code), base2)
        print(f"  场景1(跌破止损)：信号 {ex.stype.value}")
        for r in ex.reasons:
            print(f"    - {r}")

        # 场景2：游资出货 + 风险触发
        hm_bad = HotMoneyRecord("2026-08-20", top.snapshot.code, top.snapshot.name,
                                "日振幅达15%", net_buy=-9000, turnover_value=200000,
                                float_mv=50_000_000,
                                buy_brokers=[("普通营业部", 2000)],
                                sell_brokers=[("知名游资席位", 11000)],
                                consecutive_days_on_list=2)
        q3 = RealtimeQuote(code=top.snapshot.code, time="14:30", price=q.price * 1.01,
                           pre_close=q.price, open_price=q.price, high=q.price * 1.02,
                           low=q.price * 0.99, volume=60000, volume_ratio=2.5)
        ex2 = sig_engine.exit_check(held, q3, hm_bad, base2)
        print(f"  场景2(游资出货)：信号 {ex2.stype.value}")
        for r in ex2.reasons:
            print(f"    - {r}")
        for r in ex2.risks:
            print(f"    ⚠ {r}")
    print()

    print("=" * 66)
    print("  免责声明")
    print("=" * 66)
    print("  本系统只做条件打分与风险提示，不构成投资建议，不自动下单。")
    print("  游资数据存在滞后性与欺骗性，跟单风险极高，请自行独立决策。")
    print("  市场有风险，入市需谨慎；赚钱的前提是先活下来（风控）。")


if __name__ == "__main__":
    demo()
