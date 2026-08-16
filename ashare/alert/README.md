# 盘中实时监控 + 买卖提示

## 推送时间表
| 类型 | 时间 | 逻辑 |
|---|---|---|
| 买入提示 | 交易日 14:25–14:57 | 筛出最强2只，TOP2变化/每3分钟推一次 |
| 止盈提示 | 次日盘中 | 已冲高 +2% 以上 → 提醒落袋 |
| 卖出提示 | 次日盘中 | 跌破昨收/前低、破分时均价线、冲高回落 → 提醒卖出 |

盘中每 30 秒监测一次（9:30–11:30、13:00–15:00），非交易时段休眠。

## 微信推送配置（config.json 三选一）
- Server酱：https://sct.ftqq.com 拿 SendKey → 填 `serverchan_key`
- PushPlus：http://www.pushplus.plus 拿 token → 填 `pushplus_token`
- 企业微信机器人：群里加机器人拿 webhook → 填 `wecom_webhook`

不填也能用，提示会记录在 `alerts.json` 并在网页「盘中提示」页显示。

## 运行
```bash
python3 monitor.py            # 常驻(建议用 systemd)
python3 monitor.py --once --force   # 单轮测试
```
