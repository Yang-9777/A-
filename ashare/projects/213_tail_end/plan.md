# 尾盘打法服务 · 部署与布局计划（PVE 213 · VM 104）

> 说明：**213 = PVE 主机，104 = 其中的虚拟机**；本任务是在 VM 104 上部署一个「杨永兴尾盘打法」筛选服务。
> 战法：十步尾盘买入法，每天只输出 **2只最强**（默认仅主板，因为创业板/科创板/北交所无权限）。

## 1. 已部署内容（工作区 /home/ai/ashare）
| 模块 | 路径 | 说明 |
|---|---|---|
| Web 看板 | `web/app.py` | Flask，端口 8140，沿用之前暗色样式 |
| 数据层 | `tools/screener_data.py` | 新浪涨幅榜 + 腾讯批量量比，稳健 |
| 选股 CLI | `tools/weipan_screen.py` | `--rank --top 2` / `--code 603439` |
| 战法文档 | `strategies/yangyongxing_weipan.md` | 十步法全文 + 公式 + 板块适配 |
| 启动脚本 | `web/start.sh` | 一键启动 |
| systemd | `web/weipan.service` | 开机自启 |

## 2. 服务功能
- **尾盘初筛**：全市场涨幅榜 → 涨幅/换手/流通市值 3 项硬筛 → 腾讯批量补量比 → 量比>1 → 打分 → **TOP2**
- **单股体检**：输入 6 位代码，逐条核对 4 项硬指标 + 强度分
- **默认「仅主板」**：过滤创业板/科创板/北交所/ST（无权限或高风险）

## 3. 部署步骤（在 VM 104 上执行）
```bash
sudo cp /home/ai/ashare/web/weipan.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now weipan
# 访问 http://<VM104_IP>:8140
```
> 若 /home/ai 不在 VM 104 上，把整个 `/home/ai/ashare` 目录拷到 VM 104 的 /home/ai/ 下再执行上面命令。

## 4. 实盘仓位纪律（杨永兴第1步，供参考）
- 大盘**长期上升** → 7成/满仓
- **中期上升** → 3成（约31万）
- **短期上升** → 1成（约10万）
- 三个买点各约 1/3，买点不出现就不补；单票破分时均价线/前低 → 全撤，不补仓摊平。

## 5. 待办
- [ ] 确认 VM 104 的 IP 与端口映射（8140）
- [ ] 可选：14:30–14:55 加 crontab 定时预热缓存
- [ ] 新手建议先模拟/小仓位练手 1 个月再上 104万
