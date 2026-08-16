# A股项目工作区（统一部署）

> 约定：以后所有 A 股相关项目统一放在本目录 `/home/ai/ashare` 下。
> 部署目标：**PVE 213 · VM 104**。

## 目录结构
```
ashare/
├── README.md           本说明
├── strategies/         战法 / 策略文档
├── projects/           具体项目（如 213_tail_end）
├── tools/              数据层 + 选股工具
├── web/                Web 看板（Flask）
└── data/               缓存数据
```

## 当前项目：尾盘打法服务（杨永兴）
| 模块 | 路径 | 说明 |
|---|---|---|
| Web 看板 | `web/app.py` | 端口 8140，沿用之前暗色样式 |
| 数据层 | `tools/screener_data.py` | 新浪涨幅榜 + 腾讯量比 |
| 选股 CLI | `tools/weipan_screen.py` | 全市场初筛 / 单股体检 |
| 战法文档 | `strategies/yangyongxing_weipan.md` | 十步尾盘买入法 |

## 快速启动
```bash
# 开发/前台
bash /home/ai/ashare/web/start.sh            # http://0.0.0.0:8140

# 生产（systemd 开机自启）
sudo cp /home/ai/ashare/web/weipan.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now weipan
```

## CLI 工具
```bash
python3 tools/weipan_screen.py --rank --top 2   # 筛出最强2只
python3 tools/weipan_screen.py --code 603439    # 单股体检
python3 tools/screener_data.py                  # 数据层自测
```

> 免责：本工作区所有工具仅为条件筛选与风险提示，不构成投资建议，不自动下单。
