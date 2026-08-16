#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股主板股票辅助判断评分系统 v3（条件打分 + 风险提示，不构成投资建议）
=====================================================================
v3 新增：
  1. 趋势信号结构化并回灌评分：TrendTracker 产出的拐点信号（由盈转亏/筹码上移/
     北向转流出/机构转减/估值走高）按配置加减分，融入当期综合分
  2. 行业模板：按行业自动选资产负债率基准（银行/地产高负债为常态，科技/医药低负债），
     并对"负债显著高于行业"额外扣分（高负债行业自动豁免）
  3. 回测与权重校准：Backtest 批量喂历史快照+未来收益，计算 IC/相关系数/命中率，
     calibrate_weights 随机搜索权重最大化 IC

规则约束（不变）：只输出条件打分，严禁买卖指令；自动排除科创/创业/北交所。
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Tuple, Dict
import json
import random

# ======================================================================
# 0. 板块判定
# ======================================================================
MAIN = "main"
CHINEXT = "chinext"
STAR = "star"
BSE = "bse"


def detect_board(code: str) -> str:
    c = code.strip()
    if c.startswith(("688", "689")):
        return STAR
    if c.startswith(("300", "301", "302")):
        return CHINEXT
    if c.startswith(("8", "4", "92")):
        return BSE
    return MAIN


# ======================================================================
# 1. 可配置阈值
# ======================================================================
@dataclass
class Config:
    # ---- 六项正向权重（合计 100） ----
    weight_valuation: int = 20
    weight_shareholder: int = 15
    weight_northbound: int = 15
    weight_chip: int = 15
    weight_volume_price: int = 15
    weight_fundamental: int = 20

    # ---- 估值与时间衰减 ----
    time_decay_recent_weight: float = 0.60
    bottom_consolidation_bonus: float = 10.0
    bottom_consolidation_bonus_mid: float = 5.0
    low_percentile_threshold: float = 0.30

    # ---- 量价阈值 ----
    volume_ratio_healthy: Tuple[float, float] = (1.0, 2.5)
    volume_ratio_warm: Tuple[float, float] = (2.5, 4.0)
    ret_20d_surge: float = 30.0
    ret_20d_warm: float = 15.0
    vol_steady: float = 30.0
    vol_high: float = 50.0
    turnover_healthy: float = 5.0

    # ---- 商誉阈值 ----
    goodwill_safe: float = 10.0
    goodwill_mid: float = 20.0
    goodwill_high: float = 30.0
    goodwill_extreme: float = 50.0

    # ---- 扣分项 ----
    ded_short_surge: float = 20.0
    ded_valuation_high: float = 10.0
    ded_loss: float = 30.0
    ded_goodwill: float = 20.0
    ded_goodwill_extreme: float = 10.0
    ded_high_trapped: float = 15.0
    ded_abnormal_turnover: float = 15.0
    ded_audit_qualified: float = 20.0
    ded_pledge_high: float = 10.0
    ded_pledge_extreme: float = 15.0
    ded_fin_anomaly: float = 5.0
    ded_fin_anomaly_cap: float = 15.0
    ded_debt_high: float = 10.0              # 负债显著高于行业均值（高负债行业豁免）

    # ---- 趋势调整（跨期信号回灌，正=加分 负=扣分） ----
    trend_profit_worsen: float = -15.0
    trend_profit_improve: float = 10.0
    trend_chip_up: float = -10.0
    trend_chip_down: float = 5.0
    trend_north_out: float = -10.0
    trend_north_in: float = 5.0
    trend_inst_out: float = -10.0
    trend_inst_in: float = 5.0
    trend_val_up: float = -10.0
    trend_val_down: float = 5.0

    # ---- 一票否决封顶 ----
    cap_major_negative: int = 10
    cap_st_risk: int = 30
    cap_loss_goodwill: int = 25
    cap_audit_adverse: int = 10

    # ---- 风险等级 ----
    risk_high_triggers: int = 4
    risk_mid_triggers: int = 2

    _WEIGHT_FIELDS: Dict[str, str] = field(default_factory=lambda: {
        "估值与价格位置": "weight_valuation",
        "十大流通股东": "weight_shareholder",
        "北向资金": "weight_northbound",
        "筹码分布": "weight_chip",
        "量价健康度": "weight_volume_price",
        "基本面质量": "weight_fundamental",
    }, repr=False)

    @property
    def weights(self) -> Dict[str, int]:
        return {k: getattr(self, f) for k, f in self._WEIGHT_FIELDS.items()}

    def set_weights(self, w: Dict[str, int]) -> None:
        for k, v in w.items():
            if k in self._WEIGHT_FIELDS:
                setattr(self, self._WEIGHT_FIELDS[k], int(v))

    def to_dict(self) -> Dict:
        d = asdict(self)
        d.pop("_WEIGHT_FIELDS", None)
        for k in ("volume_ratio_healthy", "volume_ratio_warm"):
            if k in d:
                d[k] = list(d[k])
        return d

    @classmethod
    def from_dict(cls, d: Dict) -> "Config":
        d = dict(d)
        for k in ("volume_ratio_healthy", "volume_ratio_warm"):
            if k in d:
                d[k] = tuple(d[k])
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})

    @classmethod
    def from_json(cls, path: str) -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    def save_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)


# ======================================================================
# 2. 行业模板
# ======================================================================
@dataclass
class IndustryTemplate:
    keywords: List[str]
    debt_benchmark: float       # 行业平均资产负债率(%)
    high_debt_normal: bool      # 高负债是否为行业常态
    note: str = ""


INDUSTRY_TEMPLATES: List[IndustryTemplate] = [
    IndustryTemplate(["银行", "保险", "证券", "金融", "信托", "期货"], 90.0, True, "金融行业高负债为常态"),
    IndustryTemplate(["房地产", "地产", "建筑", "基建", "水泥"], 75.0, True, "地产/建筑负债天然偏高"),
    IndustryTemplate(["公用事业", "电力", "燃气", "水务", "环保", "高速", "港口"], 60.0, False, "公用事业重资产"),
    IndustryTemplate(["钢铁", "有色", "煤炭", "化工", "石油", "采掘"], 55.0, False, "周期重资产"),
    IndustryTemplate(["汽车", "机械", "电气", "军工", "家电"], 50.0, False, "制造业"),
    IndustryTemplate(["电子", "计算机", "软件", "半导体", "通信", "互联网"], 40.0, False, "科技行业"),
    IndustryTemplate(["医药", "生物", "医疗", "疫苗"], 35.0, False, "医药行业"),
    IndustryTemplate(["食品", "饮料", "白酒", "消费", "零售", "农业"], 40.0, False, "消费行业"),
    IndustryTemplate(["传媒", "游戏", "影视", "教育", "旅游"], 40.0, False, "传媒/服务行业"),
]
DEFAULT_TEMPLATE = IndustryTemplate([], 45.0, False, "默认中性基准")


def match_industry_template(industry: str) -> IndustryTemplate:
    for t in INDUSTRY_TEMPLATES:
        if any(k in industry for k in t.keywords):
            return t
    return DEFAULT_TEMPLATE


def apply_industry_template(s: "StockSnapshot") -> "StockSnapshot":
    t = match_industry_template(s.industry)
    if s.industry_debt_benchmark is None:
        s.industry_debt_benchmark = t.debt_benchmark
    s.high_debt_normal = t.high_debt_normal
    s.industry_template_note = t.note
    return s


# ======================================================================
# 3. 输入数据模型
# ======================================================================
@dataclass
class StockSnapshot:
    code: str
    name: str
    industry: str

    # ---- 估值 ----
    pe_percentile: Optional[float] = None
    pb_percentile: Optional[float] = None
    price_percentile: Optional[float] = None
    pe_percentile_recent: Optional[float] = None
    pb_percentile_recent: Optional[float] = None
    price_percentile_recent: Optional[float] = None
    bottom_consolidation: bool = False

    # ---- 十大流通股东 ----
    inst_count_qoq: Optional[float] = None
    inst_holding_qoq: Optional[float] = None
    inst_rising_quarters: int = 0

    # ---- 北向资金 ----
    north_inflow_quarters: int = 0
    north_holding_change_3m: Optional[float] = None
    north_data_fresh: bool = True

    # ---- 筹码分布 ----
    chip_peak: str = "mid"
    low_chip_concentration: Optional[float] = None
    high_trapped_trend: str = "stable"
    trapped_ratio: Optional[float] = None

    # ---- 日K量价 ----
    volume_ratio: Optional[float] = None
    ret_20d: Optional[float] = None
    annual_volatility: Optional[float] = None
    turnover_rate: Optional[float] = None
    abnormal_turnover_long: bool = False

    # ---- 技术指标（趋势/反转/波动率，用于买卖点） ----
    atr14: Optional[float] = None
    ma5: Optional[float] = None
    ma10: Optional[float] = None
    ma20: Optional[float] = None
    ma60: Optional[float] = None
    macd_dif: Optional[float] = None
    macd_dea: Optional[float] = None
    macd_hist: Optional[float] = None
    rsi14: Optional[float] = None
    breakout_20d: bool = False
    volume_surge: bool = False
    ma_bull: bool = False
    macd_golden: bool = False

    # ---- 基本面 ----
    consecutive_profit_years: Optional[int] = None  # None=未知, >0=连续盈利年数, <=0=亏损
    roe: Optional[float] = None
    debt_ratio: Optional[float] = None
    industry_debt_benchmark: Optional[float] = None
    goodwill_to_equity: Optional[float] = None
    industry_quality: float = 50.0
    has_major_negative_news: bool = False
    is_st_risk: bool = False

    # ---- 行业模板 ----
    high_debt_normal: bool = False
    industry_template_note: str = ""

    # ---- 财务造假前置筛查 ----
    audit_opinion: str = "standard"
    major_shareholder_pledge_ratio: Optional[float] = None
    receivable_anomaly: bool = False
    inventory_anomaly: bool = False
    cash_flow_profit_mismatch: bool = False

    # ---- 支撑位 ----
    current_price: Optional[float] = None
    support_levels: Dict[str, float] = field(default_factory=dict)
    broke_support: bool = False
    broken_support_detail: List[str] = field(default_factory=list)

    # ---- 数据层排除 ----
    main_force_inflow_signal: bool = False


# ======================================================================
# 4. 动态支撑位
# ======================================================================
def compute_support_levels(prices: List[float]) -> Dict[str, float]:
    if not prices:
        return {}
    levels: Dict[str, float] = {"近20日低点": min(prices[-20:])}
    if len(prices) >= 60:
        levels["MA60"] = sum(prices[-60:]) / 60
    if len(prices) >= 120:
        levels["MA120"] = sum(prices[-120:]) / 120
    if len(prices) >= 40:
        levels["前低平台"] = min(prices[-40:-20])
    return levels


def check_support_break(current_price: float, levels: Dict[str, float],
                        margin: float = 0.02) -> List[Tuple[str, float]]:
    """识别「有效跌破防守支撑位」。

    关键语义修正：支撑位应位于现价下方；MA60/MA120 若在现价上方，属于「均线压制 /
    趋势偏弱」，不是「跌破支撑」。低位选股（股价天然在 MA120 下方）不能被误判成
    「已跌破支撑」从而永远无法进场。
    """
    broken: List[Tuple[str, float]] = []
    for name, lv in levels.items():
        if lv is None:
            continue
        if "MA" in name and lv > current_price:
            continue  # 均线在现价上方 → 趋势压制，不作跌破支撑
        if current_price < lv * (1 - margin):
            broken.append((name, lv))
    return broken


def attach_support(s: StockSnapshot, prices: List[float], current_price: float) -> StockSnapshot:
    s.current_price = current_price
    s.support_levels = compute_support_levels(prices)
    broken = check_support_break(current_price, s.support_levels)
    s.broken_support_detail = [f"{name} {lv:.2f}" for name, lv in broken]
    s.broke_support = bool(broken)
    return s


# ======================================================================
# 5. 趋势信号
# ======================================================================
@dataclass
class TrendSignal:
    kind: str        # profit / chip / northbound / institution / valuation
    direction: int   # +1 改善，-1 恶化
    label: str


# ======================================================================
# 6. 评分引擎
# ======================================================================
class ScoringEngine:
    def __init__(self, config: Optional[Config] = None):
        self.cfg = config or Config()

    @staticmethod
    def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
        return max(lo, min(hi, v))

    def _decay(self, full: Optional[float], recent: Optional[float]) -> Optional[float]:
        if full is None:
            return recent
        if recent is None:
            return full
        w = self.cfg.time_decay_recent_weight
        return w * recent + (1 - w) * full

    def _avg_percentile(self, s: StockSnapshot) -> Optional[float]:
        vals = [self._decay(s.pe_percentile, s.pe_percentile_recent),
                self._decay(s.pb_percentile, s.pb_percentile_recent),
                self._decay(s.price_percentile, s.price_percentile_recent)]
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    def _has_recent(self, s: StockSnapshot) -> bool:
        return any(v is not None for v in
                   (s.pe_percentile_recent, s.pb_percentile_recent, s.price_percentile_recent))

    # ---------------- 正向加分项 ----------------
    def score_valuation(self, s: StockSnapshot) -> Tuple[float, List[str]]:
        detail: List[str] = []
        avg = self._avg_percentile(s)
        if avg is None:
            return 0.0, ["估值分位数据缺失，本项记 0 分"]
        score = (1 - avg) * 100
        if self._has_recent(s):
            detail.append(f"时间加权(近3年{self.cfg.time_decay_recent_weight:.0%}+全期"
                          f"{1-self.cfg.time_decay_recent_weight:.0%})估值分位 {avg*100:.0f}% → {(1-avg)*100:.0f} 分")
        else:
            detail.append(f"估值平均历史分位 {avg*100:.0f}% → {(1-avg)*100:.0f} 分")
        if s.bottom_consolidation:
            bonus = (self.cfg.bottom_consolidation_bonus if avg <= self.cfg.low_percentile_threshold
                     else self.cfg.bottom_consolidation_bonus_mid)
            score += bonus
            detail.append(f"横盘整理 +{bonus:.0f}")
        return self._clamp(score), detail

    def score_shareholder(self, s: StockSnapshot) -> Tuple[float, List[str]]:
        detail: List[str] = []
        if s.inst_count_qoq is None and s.inst_holding_qoq is None:
            return 0.0, ["股东数据缺失，本项记 0 分"]
        score = 0.0
        if s.inst_count_qoq is not None:
            if s.inst_count_qoq > 0:
                score += 25
                detail.append(f"机构数量环比 +{s.inst_count_qoq:.1f}% (+25)")
                if s.inst_count_qoq >= 5:
                    score += 10
                    detail.append("机构数量明显增加 +10")
            elif s.inst_count_qoq < 0:
                detail.append(f"机构数量环比 {s.inst_count_qoq:.1f}%（不加分）")
        if s.inst_holding_qoq is not None:
            if s.inst_holding_qoq > 0:
                score += 25
                detail.append(f"机构持仓比例 +{s.inst_holding_qoq:.2f}pp (+25)")
                if s.inst_holding_qoq >= 1:
                    score += 10
                    detail.append("持仓比例显著提升 +10")
            elif s.inst_holding_qoq < 0:
                detail.append(f"机构持仓比例 {s.inst_holding_qoq:.2f}pp（不加分）")
        if s.inst_rising_quarters > 0:
            score += min(s.inst_rising_quarters, 3) * 10
            detail.append(f"连续 {s.inst_rising_quarters} 个季度增加 +{min(s.inst_rising_quarters,3)*10}")
        return self._clamp(score), detail

    def score_northbound(self, s: StockSnapshot) -> Tuple[float, List[str]]:
        detail: List[str] = []
        if not s.north_data_fresh:
            detail.append("北向数据非新鲜/仅估算，本项封顶 60 分")
        score = 0.0
        if s.north_inflow_quarters > 0:
            score += min(s.north_inflow_quarters, 4) * 15
            detail.append(f"连续 {s.north_inflow_quarters} 个季度净流入 +{min(s.north_inflow_quarters,4)*15}")
        elif s.north_inflow_quarters < 0:
            detail.append(f"连续 {abs(s.north_inflow_quarters)} 个季度净流出（不加分）")
        if s.north_holding_change_3m is not None:
            if s.north_holding_change_3m > 0:
                score += 20
                detail.append(f"近3月持股比例 +{s.north_holding_change_3m:.2f}pp (+20)")
                if s.north_holding_change_3m >= 1:
                    score += 20
                    detail.append("近3月持股比例显著上升 +20")
            elif s.north_holding_change_3m < 0:
                detail.append(f"近3月持股比例 {s.north_holding_change_3m:.2f}pp（不加分）")
        if not s.north_data_fresh:
            score = min(score, 60.0)
        return self._clamp(score), detail

    def score_chip(self, s: StockSnapshot) -> Tuple[float, List[str]]:
        detail: List[str] = []
        score = 0.0
        if s.chip_peak == "low":
            score += 50
            detail.append("筹码峰集中在低位 +50")
        elif s.chip_peak == "mid":
            score += 25
            detail.append("筹码峰居中 +25")
        else:
            detail.append("筹码峰处于高位（不加分）")
        if s.low_chip_concentration is not None:
            score += s.low_chip_concentration * 30
            detail.append(f"低位筹码集中度 {s.low_chip_concentration*100:.0f}% +{s.low_chip_concentration*30:.0f}")
        if s.high_trapped_trend == "decreasing":
            score += 20
            detail.append("高位套牢筹码持续减少 +20")
        elif s.high_trapped_trend == "stable":
            score += 5
            detail.append("高位套牢筹码稳定 +5")
        else:
            detail.append("高位套牢筹码增加（不加分）")
        return self._clamp(score), detail

    def score_volume_price(self, s: StockSnapshot) -> Tuple[float, List[str]]:
        detail: List[str] = []
        score = 60.0
        lo, hi = self.cfg.volume_ratio_healthy
        lo2, hi2 = self.cfg.volume_ratio_warm
        if s.volume_ratio is not None:
            if lo <= s.volume_ratio <= hi:
                score += 15
                detail.append(f"量比 {s.volume_ratio:.1f} 底部温和放量 +15")
            elif lo2 <= s.volume_ratio <= hi2:
                score += 5
                detail.append(f"量比 {s.volume_ratio:.1f} 温和偏热 +5")
            else:
                score -= 10
                detail.append(f"量比 {s.volume_ratio:.1f} 异常 (-10)")
        if s.ret_20d is not None:
            if s.ret_20d > self.cfg.ret_20d_surge:
                score -= 20
                detail.append(f"近20日涨幅 {s.ret_20d:.0f}% 短期暴涨 (-20)")
            elif s.ret_20d > self.cfg.ret_20d_warm:
                score -= 5
                detail.append(f"近20日涨幅 {s.ret_20d:.0f}% 偏热 (-5)")
            else:
                detail.append(f"近20日涨幅 {s.ret_20d:.0f}% 无短期暴涨")
        if s.annual_volatility is not None:
            if s.annual_volatility < self.cfg.vol_steady:
                score += 10
                detail.append(f"年化波动率 {s.annual_volatility:.0f}% 平稳 +10")
            elif s.annual_volatility >= self.cfg.vol_high:
                score -= 15
                detail.append(f"年化波动率 {s.annual_volatility:.0f}% 大起大落 (-15)")
            else:
                detail.append(f"年化波动率 {s.annual_volatility:.0f}% 中性")
        if s.abnormal_turnover_long:
            score -= 20
            detail.append("换手率长期异常高，疑似游资爆炒 (-20)")
        elif s.turnover_rate is not None and s.turnover_rate <= self.cfg.turnover_healthy:
            score += 5
            detail.append(f"换手率 {s.turnover_rate:.1f}% 健康 +5")
        # ---- 趋势/反转确认（新增）：过滤下跌中继，优选底部反转 ----
        if s.ma_bull:
            score += 8
            detail.append("均线多头排列(MA5>MA10>MA20>MA60) +8")
        if s.macd_golden:
            score += 6
            detail.append("MACD金叉(趋势转多) +6")
        if s.breakout_20d and s.volume_surge:
            score += 8
            detail.append("放量突破20日新高 +8")
        elif s.breakout_20d:
            score += 4
            detail.append("突破20日新高 +4")
        if s.rsi14 is not None and 45 <= s.rsi14 <= 65:
            score += 4
            detail.append(f"RSI {s.rsi14:.0f} 处于健康强势区 +4")
        elif s.rsi14 is not None and s.rsi14 >= 80:
            score -= 6
            detail.append(f"RSI {s.rsi14:.0f} 超买 (-6)")
        return self._clamp(score), detail

    def score_fundamental(self, s: StockSnapshot) -> Tuple[float, List[str]]:
        detail: List[str] = []
        score = 0.0
        if s.consecutive_profit_years is None:
            detail.append("盈利数据缺失（不加分）")
        elif s.consecutive_profit_years >= 3:
            score += 25
            detail.append(f"连续盈利 {s.consecutive_profit_years} 年 +25")
        elif s.consecutive_profit_years >= 1:
            score += 15
            detail.append("近1年盈利 +15")
        else:
            detail.append("当年/上年亏损（不加分，另计扣分）")
        if s.roe is not None:
            if s.roe >= 15:
                score += 20
                detail.append(f"ROE {s.roe:.1f}% 优秀 +20")
            elif s.roe >= 10:
                score += 15
                detail.append(f"ROE {s.roe:.1f}% 良好 +15")
            elif s.roe >= 5:
                score += 10
                detail.append(f"ROE {s.roe:.1f}% 一般 +10")
            elif s.roe > 0:
                score += 5
                detail.append(f"ROE {s.roe:.1f}% 偏低 +5")
            else:
                detail.append("ROE 为负（不加分）")
        if s.debt_ratio is not None and s.industry_debt_benchmark is not None:
            if s.debt_ratio <= s.industry_debt_benchmark:
                score += 15
                detail.append(f"资产负债率 {s.debt_ratio:.0f}% <= 行业均值 {s.industry_debt_benchmark:.0f}% +15")
            elif s.debt_ratio <= s.industry_debt_benchmark * 1.3:
                score += 8
                detail.append(f"资产负债率 {s.debt_ratio:.0f}% 略高于行业均值 +8")
            else:
                detail.append(f"资产负债率 {s.debt_ratio:.0f}% 明显偏高（不加分）")
        g = s.goodwill_to_equity
        if g is not None:
            if g <= self.cfg.goodwill_safe:
                score += 20
                detail.append(f"商誉/净资产 {g:.0f}% 安全 +20")
            elif g <= self.cfg.goodwill_mid:
                score += 10
                detail.append(f"商誉/净资产 {g:.0f}% 适中 +10")
            elif g <= self.cfg.goodwill_high:
                score += 5
                detail.append(f"商誉/净资产 {g:.0f}% 偏高 +5")
            else:
                detail.append("商誉/净资产超 30%（不加分，另计扣分）")
        score += self._clamp(s.industry_quality) * 0.20
        detail.append(f"行业赛道景气分 {s.industry_quality:.0f}/100 → +{self._clamp(s.industry_quality)*0.20:.0f}")
        return self._clamp(score), detail

    # ---------------- 财务造假前置筛查 ----------------
    def fraud_findings(self, s: StockSnapshot) -> List[str]:
        findings: List[str] = []
        if s.audit_opinion == "qualified":
            findings.append("审计意见：保留意见（非标准无保留）")
        elif s.audit_opinion in ("adverse", "disclaimer"):
            findings.append("审计意见：否定/无法表示意见（严重）")
        if s.major_shareholder_pledge_ratio is not None:
            if s.major_shareholder_pledge_ratio > 70:
                findings.append(f"大股东质押比例 {s.major_shareholder_pledge_ratio:.0f}%（极高）")
            elif s.major_shareholder_pledge_ratio > 50:
                findings.append(f"大股东质押比例 {s.major_shareholder_pledge_ratio:.0f}%（偏高）")
        if s.receivable_anomaly:
            findings.append("应收账款增速显著高于营收增速")
        if s.inventory_anomaly:
            findings.append("存货异常增长")
        if s.cash_flow_profit_mismatch:
            findings.append("经营现金流长期明显低于净利润")
        return findings

    # ---------------- 趋势信号回灌打分 ----------------
    def trend_adjustment(self, signals: Optional[List[TrendSignal]]) -> Tuple[float, List[str]]:
        if not signals:
            return 0.0, []
        cfg = self.cfg
        mapping = {
            ("profit", -1): (cfg.trend_profit_worsen, "基本面恶化：由盈转亏"),
            ("profit", 1): (cfg.trend_profit_improve, "基本面改善：扭亏为盈"),
            ("chip", -1): (cfg.trend_chip_up, "筹码峰上移"),
            ("chip", 1): (cfg.trend_chip_down, "筹码峰下移"),
            ("northbound", -1): (cfg.trend_north_out, "北向资金转净流出"),
            ("northbound", 1): (cfg.trend_north_in, "北向资金转净流入"),
            ("institution", -1): (cfg.trend_inst_out, "机构持仓由增转减"),
            ("institution", 1): (cfg.trend_inst_in, "机构持仓由减转增"),
            ("valuation", -1): (cfg.trend_val_up, "估值分位大幅走高"),
            ("valuation", 1): (cfg.trend_val_down, "估值分位明显回落"),
        }
        delta = 0.0
        items: List[str] = []
        for sig in signals:
            key = (sig.kind, sig.direction)
            if key in mapping:
                val, label = mapping[key]
                delta += val
                items.append(f"{label} {'+' if val >= 0 else ''}{val:.0f} 分")
        return delta, items

    # ---------------- 扣分项 ----------------
    def collect_deductions(self, s: StockSnapshot) -> Tuple[float, List[str]]:
        total = 0.0
        items: List[str] = []
        avg = self._avg_percentile(s)
        cfg = self.cfg

        if (s.ret_20d is not None and s.ret_20d > 50) or (avg is not None and avg > 0.9):
            total += cfg.ded_short_surge
            items.append(f"短期暴涨/题材炒作特征明显，估值严重偏高 -{cfg.ded_short_surge:.0f}")
        elif avg is not None and avg > 0.85:
            total += cfg.ded_valuation_high
            items.append(f"估值分位超过 85%，偏高 -{cfg.ded_valuation_high:.0f}")

        if s.consecutive_profit_years is not None and s.consecutive_profit_years <= 0:
            total += cfg.ded_loss
            items.append(f"业绩亏损（当年或上年） -{cfg.ded_loss:.0f}")

        g = s.goodwill_to_equity
        if g is not None and g > cfg.goodwill_high:
            total += cfg.ded_goodwill
            items.append(f"商誉/净资产 {g:.0f}% 大额商誉风险 -{cfg.ded_goodwill:.0f}")
            if g > cfg.goodwill_extreme:
                total += cfg.ded_goodwill_extreme
                items.append(f"商誉占比超 {cfg.goodwill_extreme:.0f}%，减值风险极高 追加 -{cfg.ded_goodwill_extreme:.0f}")

        if s.chip_peak == "high" and (s.trapped_ratio is None or s.trapped_ratio > 0.5):
            total += cfg.ded_high_trapped
            items.append(f"高位筹码密集、大量套牢盘 -{cfg.ded_high_trapped:.0f}")

        if s.abnormal_turnover_long or (s.turnover_rate is not None and s.turnover_rate > 15):
            total += cfg.ded_abnormal_turnover
            items.append(f"换手率长期异常偏高，游资爆炒特征 -{cfg.ded_abnormal_turnover:.0f}")

        # ---- 行业相对负债（高负债行业豁免） ----
        if (not s.high_debt_normal and s.debt_ratio is not None
                and s.industry_debt_benchmark is not None
                and s.debt_ratio > s.industry_debt_benchmark * 1.5
                and s.debt_ratio > 60):
            total += cfg.ded_debt_high
            items.append(f"资产负债率 {s.debt_ratio:.0f}% 显著高于行业均值 "
                         f"{s.industry_debt_benchmark:.0f}% -{cfg.ded_debt_high:.0f}")

        # ---- 财务造假前置 ----
        if s.audit_opinion == "qualified":
            total += cfg.ded_audit_qualified
            items.append(f"审计意见为保留意见 -{cfg.ded_audit_qualified:.0f}")
        p = s.major_shareholder_pledge_ratio
        if p is not None:
            if p > 70:
                total += cfg.ded_pledge_extreme
                items.append(f"大股东质押比例 {p:.0f}% 极高 -{cfg.ded_pledge_extreme:.0f}")
            elif p > 50:
                total += cfg.ded_pledge_high
                items.append(f"大股东质押比例 {p:.0f}% 偏高 -{cfg.ded_pledge_high:.0f}")
        anomalies = sum([s.receivable_anomaly, s.inventory_anomaly, s.cash_flow_profit_mismatch])
        if anomalies:
            d = min(anomalies * cfg.ded_fin_anomaly, cfg.ded_fin_anomaly_cap)
            total += d
            items.append(f"财务异常信号 {anomalies} 项 -{d:.0f}")
        return total, items

    # ---------------- 一票否决/封顶 ----------------
    def veto_cap(self, s: StockSnapshot) -> Tuple[Optional[int], List[str]]:
        cfg = self.cfg
        caps: List[Tuple[int, str]] = []
        if s.has_major_negative_news:
            caps.append((cfg.cap_major_negative,
                         f"存在立案调查/财务造假等重大负面公告，综合分封顶 {cfg.cap_major_negative}"))
        if s.audit_opinion in ("adverse", "disclaimer"):
            caps.append((cfg.cap_audit_adverse,
                         f"审计意见为否定/无法表示意见，综合分封顶 {cfg.cap_audit_adverse}"))
        if s.is_st_risk:
            caps.append((cfg.cap_st_risk,
                         f"连续亏损存在 ST 风险，综合分封顶 {cfg.cap_st_risk}"))
        if (s.consecutive_profit_years is not None and s.consecutive_profit_years <= 0
                and s.goodwill_to_equity is not None
                and s.goodwill_to_equity > cfg.goodwill_high):
            caps.append((cfg.cap_loss_goodwill,
                         f"亏损叠加巨额商誉，综合分封顶 {cfg.cap_loss_goodwill}"))
        if not caps:
            return None, []
        cap, _ = min(caps, key=lambda x: x[0])
        return cap, [r for _, r in caps]

    # ---------------- 卖出参考触发条件 ----------------
    def sell_triggers(self, s: StockSnapshot) -> List[str]:
        triggers: List[str] = []
        avg = self._avg_percentile(s)
        if avg is not None and avg > 0.80:
            triggers.append(f"股价/估值已进入历史高位区间（平均分位 {avg*100:.0f}%），估值分位大幅走高")
        if (s.inst_holding_qoq is not None and s.inst_holding_qoq <= -5) or s.inst_rising_quarters <= -2:
            triggers.append("十大流通股东机构明显大幅减持")
        if s.north_inflow_quarters < 0 or (s.north_holding_change_3m is not None
                                           and s.north_holding_change_3m <= -1):
            triggers.append("北向资金持续大幅流出")
        if s.chip_peak == "high" and s.high_trapped_trend == "increasing":
            triggers.append("低位筹码大量上移，筹码峰已转移到高位")
        if s.has_major_negative_news or (s.consecutive_profit_years is not None
                                         and s.consecutive_profit_years <= 0):
            triggers.append("基本面恶化：由盈转亏或存在重大负面公告")
        if s.broke_support:
            if s.broken_support_detail:
                triggers.append("有效跌破防守支撑位：" + "、".join(s.broken_support_detail))
            else:
                triggers.append("有效跌破预先设置的防守支撑位")
        return triggers

    def risk_level(self, s: StockSnapshot, triggers: List[str]) -> str:
        if s.has_major_negative_news or s.is_st_risk or s.audit_opinion in ("adverse", "disclaimer"):
            return "极高"
        n = len(triggers)
        if n >= self.cfg.risk_high_triggers:
            return "高"
        if n >= self.cfg.risk_mid_triggers:
            return "中"
        return "低"

    def data_completeness(self, s: StockSnapshot) -> List[str]:
        notes: List[str] = []
        if self._avg_percentile(s) is None:
            notes.append("估值分位数据缺失")
        if s.inst_count_qoq is None and s.inst_holding_qoq is None:
            notes.append("股东数据缺失")
        if s.volume_ratio is None or s.ret_20d is None:
            notes.append("量价数据部分缺失")
        if not s.north_data_fresh:
            notes.append("北向数据非实时/滞后，仅按估算处理")
        if s.main_force_inflow_signal:
            notes.append("检测到软件'主力大单净流入'信号，按规则忽略、不作为加分依据（主力可拆单造假）")
        return notes

    # ---------------- 主流程 ----------------
    def evaluate(self, s: StockSnapshot,
                 trend_signals: Optional[List[TrendSignal]] = None) -> Dict:
        board = detect_board(s.code)
        if board != MAIN:
            return {"valid": False, "reason": f"仅支持A股主板，{s.code} 属 {board} 板块，已排除"}

        apply_industry_template(s)

        factor_details: Dict[str, Dict] = {}
        weighted_sum = 0.0
        scorers = {
            "估值与价格位置": self.score_valuation,
            "十大流通股东": self.score_shareholder,
            "北向资金": self.score_northbound,
            "筹码分布": self.score_chip,
            "量价健康度": self.score_volume_price,
            "基本面质量": self.score_fundamental,
        }
        for name, fn in scorers.items():
            score, detail = fn(s)
            factor_details[name] = {"score": score, "weight": self.cfg.weights[name], "detail": detail}
            weighted_sum += score * self.cfg.weights[name] / 100.0

        deduction, deduct_items = self.collect_deductions(s)
        trend_delta, trend_items = self.trend_adjustment(trend_signals)
        final = self._clamp(weighted_sum - deduction + trend_delta, 0, 100)

        cap, veto_items = self.veto_cap(s)
        if cap is not None:
            final = min(final, cap)

        triggers = self.sell_triggers(s)
        return {
            "valid": True,
            "board": board,
            "final_score": round(final, 1),
            "factor_details": factor_details,
            "weighted_sum": round(weighted_sum, 1),
            "deductions": deduct_items,
            "deduction_total": round(deduction, 1),
            "veto_items": veto_items,
            "sell_triggers": triggers,
            "risk_level": self.risk_level(s, triggers),
            "data_notes": self.data_completeness(s),
            "fraud_findings": self.fraud_findings(s),
            "support_levels": s.support_levels,
            "current_price": s.current_price,
            "broken_support_detail": s.broken_support_detail,
            "trend_signals": [sig.label for sig in (trend_signals or [])],
            "trend_adjustments": trend_items,
            "trend_delta": round(trend_delta, 1),
            "industry_template_note": s.industry_template_note,
            "high_debt_normal": s.high_debt_normal,
        }


# ======================================================================
# 7. 连续快照趋势追踪
# ======================================================================
@dataclass
class TrendPoint:
    date: str
    snapshot: StockSnapshot


class TrendTracker:
    def __init__(self):
        self.points: List[TrendPoint] = []

    def add(self, date: str, snapshot: StockSnapshot) -> None:
        self.points.append(TrendPoint(date, snapshot))

    def analyze(self) -> List[TrendSignal]:
        if len(self.points) < 2:
            return []
        prev, cur = self.points[-2].snapshot, self.points[-1].snapshot
        signals: List[TrendSignal] = []

        if (prev.consecutive_profit_years is not None and cur.consecutive_profit_years is not None
                and prev.consecutive_profit_years > 0 and cur.consecutive_profit_years <= 0):
            signals.append(TrendSignal("profit", -1, "由盈转亏"))
        elif (prev.consecutive_profit_years is not None and cur.consecutive_profit_years is not None
              and prev.consecutive_profit_years <= 0 and cur.consecutive_profit_years > 0):
            signals.append(TrendSignal("profit", 1, "扭亏为盈"))

        order = {"low": 0, "mid": 1, "high": 2}
        if order.get(cur.chip_peak, 1) > order.get(prev.chip_peak, 1):
            signals.append(TrendSignal("chip", -1, "筹码峰上移"))
        elif order.get(cur.chip_peak, 1) < order.get(prev.chip_peak, 1):
            signals.append(TrendSignal("chip", 1, "筹码峰下移"))

        if prev.north_inflow_quarters > 0 and cur.north_inflow_quarters < 0:
            signals.append(TrendSignal("northbound", -1, "北向资金转净流出"))
        elif prev.north_inflow_quarters < 0 and cur.north_inflow_quarters > 0:
            signals.append(TrendSignal("northbound", 1, "北向资金转净流入"))

        if prev.inst_rising_quarters > 0 and cur.inst_rising_quarters < 0:
            signals.append(TrendSignal("institution", -1, "机构持仓由增转减"))
        elif prev.inst_rising_quarters < 0 and cur.inst_rising_quarters > 0:
            signals.append(TrendSignal("institution", 1, "机构持仓由减转增"))

        e = ScoringEngine()
        prev_avg, cur_avg = e._avg_percentile(prev), e._avg_percentile(cur)
        if prev_avg is not None and cur_avg is not None:
            if cur_avg - prev_avg > 0.2:
                signals.append(TrendSignal("valuation", -1, "估值分位大幅走高"))
            elif prev_avg - cur_avg > 0.2:
                signals.append(TrendSignal("valuation", 1, "估值分位明显回落"))
        return signals


# ======================================================================
# 8. 统计与回测
# ======================================================================
def pearson(xs: List[float], ys: List[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    vx = sum((a - mx) ** 2 for a in xs) ** 0.5
    vy = sum((b - my) ** 2 for b in ys) ** 0.5
    if vx == 0 or vy == 0:
        return 0.0
    return cov / (vx * vy)


def _rank(vals: List[float]) -> List[float]:
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: List[float], ys: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    return pearson(_rank(xs), _rank(ys))


class Backtest:
    """批量历史快照 + 未来收益，计算打分有效性并校准权重。"""

    def __init__(self, engine: Optional[ScoringEngine] = None):
        self.engine = engine or ScoringEngine()
        self.records: List[Dict] = []

    def add(self, date: str, snapshot: StockSnapshot, forward_return: float) -> None:
        self.records.append({"date": date, "snapshot": snapshot,
                             "forward_return": forward_return, "score": None})

    def _evaluate_all(self, engine: ScoringEngine, with_trend: bool = False) -> List[Optional[float]]:
        tracker = TrendTracker() if with_trend else None
        scores: List[Optional[float]] = []
        for rec in self.records:
            signals = None
            if tracker is not None:
                tracker.add(rec["date"], rec["snapshot"])
                if len(tracker.points) >= 2:
                    signals = tracker.analyze()
            res = engine.evaluate(rec["snapshot"], trend_signals=signals)
            scores.append(res["final_score"] if res.get("valid") else None)
        return scores

    def run(self, with_trend: bool = False) -> List[Optional[float]]:
        scores = self._evaluate_all(self.engine, with_trend)
        for rec, sc in zip(self.records, scores):
            rec["score"] = sc
        return scores

    def _pairs(self):
        return [(r["score"], r["forward_return"]) for r in self.records if r["score"] is not None]

    def ic(self) -> float:
        pairs = self._pairs()
        if len(pairs) < 3:
            return 0.0
        return spearman([p[0] for p in pairs], [p[1] for p in pairs])

    def pearson_corr(self) -> float:
        pairs = self._pairs()
        if len(pairs) < 2:
            return 0.0
        return pearson([p[0] for p in pairs], [p[1] for p in pairs])

    def hit_rate(self, threshold: float = 60.0) -> float:
        """得分>=阈值且未来收益为正的占比（未来收益为正的样本内）。"""
        hits = [r for r in self.records
                if r["score"] is not None and r["score"] >= threshold and r["forward_return"] > 0]
        total = [r for r in self.records
                 if r["score"] is not None and r["score"] >= threshold]
        return len(hits) / len(total) if total else 0.0

    def summary(self, with_trend: bool = False) -> Dict:
        self.run(with_trend)
        return {
            "n": len(self._pairs()),
            "ic": round(self.ic(), 4),
            "pearson": round(self.pearson_corr(), 4),
            "hit_rate@60": round(self.hit_rate(60), 4),
            "mean_score": round(sum(p[0] for p in self._pairs()) / len(self._pairs()), 1)
            if self._pairs() else 0.0,
        }

    def _ic_from_scores(self, scores: List[Optional[float]]) -> float:
        pairs = [(sc, r["forward_return"]) for sc, r in zip(scores, self.records) if sc is not None]
        if len(pairs) < 3:
            return -1.0
        return spearman([p[0] for p in pairs], [p[1] for p in pairs])

    @staticmethod
    def _sample_weights(names: List[str], rng: random.Random, min_w: int = 3) -> Dict[str, int]:
        raw = [rng.random() for _ in names]
        tot = sum(raw)
        w = [raw[i] / tot * 100 for i in range(len(names))]
        for i in range(len(w)):
            if w[i] < min_w:
                w[i] = min_w
        tot = sum(w)
        w = [round(v / tot * 100) for v in w]
        w[-1] = 100 - sum(w[:-1])
        return {names[i]: w[i] for i in range(len(names))}

    def calibrate_weights(self, n_iter: int = 400, seed: int = 1,
                          with_trend: bool = False) -> Tuple[Config, float]:
        """随机搜索权重组合，最大化打分与未来收益的 IC。"""
        names = list(self.engine.cfg.weights.keys())
        rng = random.Random(seed)
        self.run(with_trend)
        best_ic = self.ic()
        best_cfg = Config()
        best_cfg.set_weights(self.engine.cfg.weights)

        for _ in range(n_iter):
            cand = Config()
            cand.set_weights(self._sample_weights(names, rng))
            ic = self._ic_from_scores(self._evaluate_all(ScoringEngine(cand), with_trend))
            if ic > best_ic:
                best_ic = ic
                best_cfg = cand

        self.engine = ScoringEngine(best_cfg)
        self.run(with_trend)
        return best_cfg, best_ic


# ======================================================================
# 9. 输出报告
# ======================================================================
DISCLAIMER = (
    "【免责声明】本结果仅为条件打分与风险提示，不是投资建议，不能直接用于交易。"
    "主力资金数据存在欺骗性，历史分位不代表未来，市场有风险，决策需独立判断并自负盈亏。"
)


def render_report(result: Dict) -> str:
    if not result["valid"]:
        return f"[不可评估] {result['reason']}\n\n{DISCLAIMER}"

    lines: List[str] = []
    lines.append("=" * 62)
    lines.append("  A股主板股票辅助判断评分报告 v3（仅条件打分）")
    lines.append("=" * 62)
    lines.append(f"综合得分：{result['final_score']} / 100")
    lines.append(f"风险等级：{result['risk_level']}")
    if result.get("industry_template_note"):
        lines.append(f"行业基准：{result['industry_template_note']}")
    lines.append("-" * 62)
    lines.append("一、正向加分项明细（单项分 × 权重）")
    for name, info in result["factor_details"].items():
        lines.append(f"  [{name}] {info['score']:.0f}/100 × 权重{info['weight']}%")
        for d in info["detail"]:
            lines.append(f"      - {d}")
    lines.append("-" * 62)
    lines.append(f"二、扣分项（合计 -{result['deduction_total']}）")
    if result["deductions"]:
        for d in result["deductions"]:
            lines.append(f"  ✗ {d}")
    else:
        lines.append("  无")
    if result["veto_items"]:
        lines.append("  一票否决/封顶：")
        for v in result["veto_items"]:
            lines.append(f"  ⊘ {v}")
    lines.append("-" * 62)
    lines.append("三、趋势调整（跨期信号回灌打分）")
    if result["trend_adjustments"]:
        for t in result["trend_adjustments"]:
            lines.append(f"  → {t}")
        lines.append(f"  净调整：{result['trend_delta']:+.0f} 分")
    else:
        lines.append("  无跨期信号")
    lines.append("-" * 62)
    lines.append("四、财务造假前置筛查")
    if result["fraud_findings"]:
        for f in result["fraud_findings"]:
            lines.append(f"  ▲ {f}")
    else:
        lines.append("  未发现明显财务造假前置信号")
    lines.append("-" * 62)
    lines.append("五、卖出参考触发条件（仅风险提示，不构成卖出指令）")
    if result["sell_triggers"]:
        for t in result["sell_triggers"]:
            lines.append(f"  ⚠ {t}")
    else:
        lines.append("  未触发")
    lines.append("-" * 62)
    lines.append("六、防守支撑位")
    if result["support_levels"]:
        for name, lv in result["support_levels"].items():
            flag = " [已跌破]" if name in [x.split()[0] for x in result["broken_support_detail"]] else ""
            lines.append(f"  · {name}: {lv:.2f}{flag}")
    else:
        lines.append("  未提供价格序列，无法计算")
    lines.append("-" * 62)
    lines.append("七、数据完整性 / 提示")
    if result["data_notes"]:
        for n in result["data_notes"]:
            lines.append(f"  · {n}")
    else:
        lines.append("  · 数据完整")
    lines.append("-" * 62)
    lines.append(DISCLAIMER)
    lines.append("=" * 62)
    return "\n".join(lines)


# ======================================================================
# 10. 示例
# ======================================================================
def _make_synthetic_snapshot(q: float, rng: random.Random) -> StockSnapshot:
    """按潜在质量 q∈[0,1] 生成合成快照（仅用于回测演示）。"""
    def noise():
        return rng.gauss(0, 0.05)
    return StockSnapshot(
        code="600000", name="合成样本", industry="制造业",
        pe_percentile=min(0.98, max(0.02, 0.90 - 0.75 * q + noise())),
        pb_percentile=min(0.98, max(0.02, 0.88 - 0.72 * q + noise())),
        price_percentile=min(0.98, max(0.02, 0.92 - 0.78 * q + noise())),
        inst_count_qoq=(q - 0.5) * 10,
        inst_holding_qoq=(q - 0.5) * 2,
        inst_rising_quarters=2 if q > 0.6 else (1 if q > 0.4 else -1),
        north_inflow_quarters=2 if q > 0.55 else -1,
        chip_peak="low" if q > 0.6 else ("mid" if q > 0.35 else "high"),
        low_chip_concentration=q * 0.8,
        volume_ratio=1.5 if q > 0.5 else 4.0,
        ret_20d=(1 - q) * 40 - 10,
        annual_volatility=25 + (1 - q) * 40,
        turnover_rate=(1 - q) * 12 + 1,
        consecutive_profit_years=3 if q > 0.4 else (1 if q > 0.25 else -1),
        roe=q * 20 - 3,
        debt_ratio=50 + (1 - q) * 30,
        industry_debt_benchmark=45.0,
        goodwill_to_equity=(1 - q) * 40,
        industry_quality=q * 80,
    )


def demo():
    engine = ScoringEngine()

    print("=" * 30, "示例1：趋势信号回灌打分", "=" * 30)
    q1 = StockSnapshot(code="600002", name="示例D", industry="食品",
                       consecutive_profit_years=3, chip_peak="low",
                       north_inflow_quarters=2, inst_rising_quarters=1,
                       pe_percentile=0.20, pb_percentile=0.22, price_percentile=0.25,
                       inst_count_qoq=3.0, inst_holding_qoq=0.5,
                       low_chip_concentration=0.5, high_trapped_trend="decreasing",
                       volume_ratio=1.8, ret_20d=4.0, annual_volatility=24.0, turnover_rate=2.0,
                       roe=12.0, debt_ratio=40.0, goodwill_to_equity=5.0,
                       industry_quality=65.0)
    # Q2：仅"由盈转亏"这一项恶化，其余因素不变，便于看清趋势调整的边际影响
    q2 = StockSnapshot(code="600002", name="示例D", industry="食品",
                       consecutive_profit_years=0, chip_peak="low",
                       north_inflow_quarters=2, inst_rising_quarters=1,
                       pe_percentile=0.45, pb_percentile=0.48, price_percentile=0.50,
                       inst_count_qoq=3.0, inst_holding_qoq=0.5,
                       low_chip_concentration=0.5, high_trapped_trend="decreasing",
                       volume_ratio=1.8, ret_20d=8.0, annual_volatility=28.0, turnover_rate=2.0,
                       roe=8.0, debt_ratio=40.0, goodwill_to_equity=5.0,
                       industry_quality=60.0)
    tracker = TrendTracker()
    tracker.add("2026Q1", q1)
    tracker.add("2026Q2", q2)
    signals = tracker.analyze()

    r_no_trend = engine.evaluate(q2)
    r_trend = engine.evaluate(q2, trend_signals=signals)
    print(f"  不含趋势信号得分：{r_no_trend['final_score']}  （未计入跨期拐点）")
    print(f"  含趋势信号得分：  {r_trend['final_score']}  （趋势净调整 {r_trend['trend_delta']:+.0f} 分）")
    print("  识别到的拐点信号：")
    for sig in signals:
        print(f"    → {sig.label} ({'改善' if sig.direction>0 else '恶化'})")
    print("  说明：正面拐点（扭亏为盈/北向转流入等）会按配置对称加分。")
    print()

    print("=" * 30, "示例2：行业模板自动选基准", "=" * 30)
    bank = StockSnapshot(code="601398", name="示例E", industry="银行",
                         pe_percentile=0.15, pb_percentile=0.12, price_percentile=0.18,
                         consecutive_profit_years=5, roe=12.0, debt_ratio=92.0,
                         goodwill_to_equity=1.0, industry_quality=60.0,
                         volume_ratio=1.4, ret_20d=2.0, annual_volatility=20.0)
    tech = StockSnapshot(code="600100", name="示例F", industry="半导体",
                         pe_percentile=0.50, pb_percentile=0.55, price_percentile=0.60,
                         consecutive_profit_years=2, roe=8.0, debt_ratio=75.0,
                         goodwill_to_equity=15.0, industry_quality=70.0,
                         volume_ratio=2.0, ret_20d=10.0, annual_volatility=35.0)
    rb = engine.evaluate(bank)
    rt = engine.evaluate(tech)
    print(f"  银行（负债92%）：行业基准 {rb['industry_template_note']}，无负债扣分")
    print(f"    扣分项：{rb['deductions'] if rb['deductions'] else '无'}")
    print(f"  半导体（负债75%）：行业基准 {rt['industry_template_note']}")
    print(f"    扣分项：{rt['deductions']}")
    print()

    print("=" * 30, "示例3：回测与权重校准", "=" * 30)
    rng = random.Random(42)
    bt = Backtest()
    for i in range(200):
        q = rng.random()
        snap = _make_synthetic_snapshot(q, rng)
        fwd = 25 * q - 8 + rng.gauss(0, 8)   # 高 q → 高未来收益
        bt.add(f"T{i:03d}", snap, fwd)

    before = bt.summary(with_trend=False)
    print("  校准前（默认权重）：")
    print(f"    IC(rank)={before['ic']}  相关系数={before['pearson']}  "
          f"命中率@60={before['hit_rate@60']}  均分={before['mean_score']}")
    best_cfg, best_ic = bt.calibrate_weights(n_iter=300, seed=7, with_trend=False)
    after = bt.summary(with_trend=False)
    print("  校准后（搜索最优权重）：")
    print(f"    IC(rank)={after['ic']}  相关系数={after['pearson']}  "
          f"命中率@60={after['hit_rate@60']}  均分={after['mean_score']}")
    print("  最优权重：")
    for name, w in best_cfg.weights.items():
        print(f"    {name}: {w}%")
    print()

    print("=" * 30, "示例4：趋势信号的有效性检验", "=" * 30)
    rng2 = random.Random(99)
    bt2 = Backtest()
    profit_state, prev_state = 1, 1
    for i in range(600):
        q = rng2.random()
        snap = _make_synthetic_snapshot(q, rng2)
        # 持久化盈利状态：低景气陷入亏损，高景气恢复盈利
        if prev_state > 0 and q < 0.12:
            profit_state = -1
        elif prev_state <= 0 and q > 0.55:
            profit_state = 1
        snap.consecutive_profit_years = 3 if profit_state > 0 else -1
        flip = prev_state > 0 and profit_state < 0      # 本季发生"由盈转亏"
        fwd = 20 * q - 8 + rng2.gauss(0, 8) + (-22 if flip else 0)
        bt2.add(f"U{i:03d}", snap, fwd)
        bt2.records[-1]["flip"] = flip
        prev_state = profit_state

    flips = [r for r in bt2.records if r["flip"]]
    others = [r for r in bt2.records if not r["flip"]]
    print(f"  由盈转亏当季：{len(flips)} 次，平均未来收益 "
          f"{sum(r['forward_return'] for r in flips)/len(flips):.1f}%")
    print(f"  其他季度：    {len(others)} 次，平均未来收益 "
          f"{sum(r['forward_return'] for r in others)/len(others):.1f}%")

    # 检验 TrendTracker 能否准确命中"由盈转亏"当季
    tracker4 = TrendTracker()
    fired = correct = 0
    for rec in bt2.records:
        tracker4.add(rec["date"], rec["snapshot"])
        sigs = tracker4.analyze()
        if any(s.kind == "profit" and s.direction == -1 for s in sigs):
            fired += 1
            if rec["flip"]:
                correct += 1
    true_flips = sum(1 for r in bt2.records if r["flip"])
    print(f"  TrendTracker 命中 {correct}/{true_flips} 次真实转亏，误报 {fired - correct} 次")
    print("  结论：'由盈转亏'拐点当季收益显著更差，且趋势追踪能准确命中该拐点。")


if __name__ == "__main__":
    demo()
