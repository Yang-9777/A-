#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
尾盘打法 · 杨永兴 看板（Web）
==============================
- 部署目标：PVE 213 · VM 104
- 数据层：tools/screener_data.py（新浪涨幅榜 + 腾讯量比，稳健）
- UI：沿用之前 A 股看板的暗色样式
- 免责：仅为条件筛选，不构成投资建议，不自动下单
"""

import os
import sys
import json
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

import screener_data as sd
from flask import Flask, jsonify, request, render_template_string

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

CACHE_FILE = os.path.join(HERE, "rank_cache.json")
_mem = {"rank": None, "ts": 0}
CACHE_TTL = 90


@app.after_request
def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/rank")
def api_rank():
    main_only = request.args.get("main_only", "1") == "1"
    now = time.time()
    if _mem["rank"] and (now - _mem["ts"] < CACHE_TTL) and _mem["rank"].get("main_only") == main_only:
        return jsonify(_mem["rank"])
    try:
        hits, meta = sd.get_rank(main_only=main_only)
        payload = {"ok": True, "cache": False, "main_only": main_only, **meta}
        _mem["rank"] = payload
        _mem["ts"] = now
        try:
            json.dump(payload, open(CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            pass
        return jsonify(payload)
    except Exception as e:
        try:
            cached = json.load(open(CACHE_FILE, encoding="utf-8"))
            cached["cache"] = True
            cached["error"] = str(e)
            return jsonify(cached)
        except Exception:
            return jsonify({"ok": False, "error": str(e)})


@app.route("/api/check/<code>")
def api_check(code):
    try:
        d = sd.get_check(code)
        if not d:
            return jsonify({"ok": False, "error": "查不到该代码，请确认6位代码"})
        return jsonify({"ok": True, **d})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/health")
def health():
    return jsonify({"ok": True, "time": time.strftime("%Y-%m-%d %H:%M:%S")})


ALERT_DIR = os.path.join(HERE, "..", "alert")


@app.route("/api/alerts")
def api_alerts():
    try:
        arr = json.load(open(os.path.join(ALERT_DIR, "alerts.json"), encoding="utf-8"))
    except Exception:
        arr = []
    return jsonify({"ok": True, "alerts": arr})


@app.route("/api/watch")
def api_watch():
    try:
        d = json.load(open(os.path.join(ALERT_DIR, "watch.json"), encoding="utf-8"))
    except Exception:
        d = {"date": "", "picks": []}
    return jsonify({"ok": True, **d})


@app.route("/api/state")
def api_state():
    try:
        st = json.load(open(os.path.join(ALERT_DIR, "state.json"), encoding="utf-8"))
    except Exception:
        st = {}
    return jsonify({"ok": True, "state": st})


@app.route("/api/watch_quotes")
def api_watch_quotes():
    try:
        d = json.load(open(os.path.join(ALERT_DIR, "watch.json"), encoding="utf-8"))
        picks = d.get("picks") or []
    except Exception:
        picks = []
    out = []
    for p in picks:
        code = p.get("code")
        sym = ("bj" if code.startswith(("43", "83", "87", "92")) else
               "sh" if code.startswith(("6", "9")) else "sz") + code
        q = sd.tencent_batch([sym]).get(sym) or {}
        q["code"] = code
        q["name"] = p.get("name")
        out.append(q)
    return jsonify({"ok": True, "quotes": out})


HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>尾盘打法 · 杨永兴 看板</title>
<style>
:root{--bg-primary:#0d1117;--bg-secondary:#161b22;--bg-tertiary:#21262d;--border-color:#30363d;
--text-primary:#e6edf3;--text-secondary:#8b949e;--text-tertiary:#6e7681;
--red:#f85149;--green:#3fb950;--blue:#58a6ff;--purple:#d2a8ff;--yellow:#d29922;--orange:#ffa657;--pink:#f778ba}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg-primary);color:var(--text-primary);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;min-height:100vh;padding:20px;font-size:14px}
.container{max-width:1500px;margin:0 auto}
.header{text-align:center;padding:26px 0;border-bottom:1px solid var(--border-color);margin-bottom:24px}
.header h1{font-size:30px;font-weight:700;background:linear-gradient(135deg,var(--blue),var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.header p{color:var(--text-secondary);margin-top:8px;font-size:13px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-bottom:22px}
.card{background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:10px;padding:16px}
.card .label{color:var(--text-tertiary);font-size:12px}
.card .value{font-size:22px;font-weight:700;margin-top:6px}
.tabs{display:flex;gap:8px;margin-bottom:18px;flex-wrap:wrap}
.tab{padding:8px 18px;border:1px solid var(--border-color);border-radius:8px;cursor:pointer;color:var(--text-secondary);background:var(--bg-secondary);user-select:none}
.tab.active{background:var(--bg-tertiary);color:var(--text-primary);border-color:var(--blue)}
table{width:100%;border-collapse:collapse;background:var(--bg-secondary);border-radius:10px;overflow:hidden}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--border-color);white-space:nowrap}
th{color:var(--text-tertiary);font-weight:500;font-size:12px;position:sticky;top:0;background:var(--bg-tertiary)}
tr:hover{background:var(--bg-tertiary)}
.tag{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;margin:1px 2px}
.tag-red{background:rgba(248,81,73,.15);color:var(--red)}
.tag-green{background:rgba(63,185,80,.15);color:var(--green)}
.tag-orange{background:rgba(255,165,87,.15);color:var(--orange)}
.tag-blue{background:rgba(88,166,255,.15);color:var(--blue)}
.tag-purple{background:rgba(210,168,255,.15);color:var(--purple)}
.inp{background:var(--bg-tertiary);border:1px solid var(--border-color);border-radius:6px;color:var(--text-primary);padding:8px 10px;font-size:14px;outline:none}
.inp:focus{border-color:var(--blue)}
.btn{background:var(--bg-tertiary);border:1px solid var(--blue);color:var(--blue);border-radius:6px;padding:8px 16px;cursor:pointer;font-size:14px}
.btn:hover{background:var(--blue);color:#0d1117}
.pos{color:var(--red);font-weight:600}
.neg{color:var(--green);font-weight:600}
.muted{color:var(--text-tertiary);font-size:12px}
.note{background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:8px;padding:12px;margin-top:18px;color:var(--text-secondary);font-size:12px;line-height:1.7}
.bar{height:6px;background:var(--bg-tertiary);border-radius:3px;margin-top:4px;overflow:hidden}
.bar span{display:block;height:6px;border-radius:3px;background:linear-gradient(90deg,var(--blue),var(--purple))}
.top2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:18px}
.top-card{background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:12px;padding:18px;position:relative}
.top-card.gold{border-color:var(--yellow)}
.top-card.silver{border-color:var(--text-tertiary)}
.top-card .rank{position:absolute;top:14px;right:16px;font-size:13px;color:var(--text-tertiary)}
.top-card .nm{font-size:18px;font-weight:700}
.top-card .cd{font-family:monospace;color:var(--text-secondary);font-size:13px}
.top-card .sc{font-size:34px;font-weight:700;background:linear-gradient(135deg,var(--blue),var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.top-card .mtx{display:flex;gap:14px;margin-top:10px;flex-wrap:wrap;font-size:12px;color:var(--text-secondary)}
.chk{display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid var(--border-color)}
.chk .ic{font-weight:700;width:22px}
.ok-ic{color:var(--green)}.bad-ic{color:var(--red)}
@media(max-width:768px){th,td{padding:8px 6px;font-size:12px}.top2{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>尾盘打法 · 杨永兴 看板</h1>
    <p>十步尾盘买入法 · 每天只取 <b>2只最强</b> · 部署于 PVE 213 / VM 104 · 仅条件筛选，不构成投资建议</p>
  </div>

  <div class="stats" id="stats"></div>

  <div class="tabs">
    <div class="tab active" onclick="switchTab('rank',this)">尾盘初筛 · 2只最强</div>
    <div class="tab" onclick="switchTab('check',this)">单股体检</div>
    <div class="tab" onclick="switchTab('method',this)">打法说明</div>
    <div class="tab" onclick="switchTab('deploy',this)">部署信息</div>
    <div class="tab" onclick="switchTab('alerts',this)">盘中提示</div>
  </div>

  <div id="view"></div>

  <div class="note">
    ⚠️ 免责声明：本结果仅为「杨永兴尾盘打法」的条件筛选与打分演示，不是投资建议，不能直接用于交易。
    行情为公开接口数据，非交易日显示的是上一交易日收盘数据；量比存在计算口径差异。市场有风险，决策需独立判断并自负盈亏。
  </div>
</div>

<script>
let curTab='rank';
function switchTab(t,el){
  curTab=t;
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  el.classList.add('active');
  if(t==='rank')loadRank();
  else if(t==='check')loadCheck();
  else if(t==='method')loadMethod();
  else if(t==='alerts')loadAlerts();
  else loadDeploy();
}
function statsCards(cards){
  document.getElementById('stats').innerHTML=cards.map(c=>
    `<div class="card"><div class="label">${c.l}</div><div class="value" style="${c.s||''}">${c.v}</div></div>`).join('');
}
async function loadRank(mainOnly){
  if(mainOnly===undefined)mainOnly=1;
  const v=document.getElementById('view');
  v.innerHTML='<div class="card muted">加载中…（全市场涨幅榜+量比，首次约需数秒）</div>';
  try{
    const r=await fetch('/api/rank?main_only='+mainOnly).then(x=>x.json());
    if(!r.ok){v.innerHTML='<div class="card"><span class="tag tag-red">出错</span> '+ (r.error||'') +'</div>';return}
    const cacheTag=r.cache?'<span class="tag tag-orange">缓存</span>':'<span class="tag tag-green">实时</span>';
    const boardTag=mainOnly?'<span class="tag tag-blue">仅主板</span>':'<span class="tag tag-purple">全部A股</span>';
    statsCards([
      {l:'初筛候选',v:r.cand_count||0},
      {l:'量比通过',v:r.pass_count||0},
      {l:'最强①',v:(r.top2&&r.top2[0]?r.top2[0].name:'-'),s:'font-size:16px'},
      {l:'最强②',v:(r.top2&&r.top2[1]?r.top2[1].name:'-'),s:'font-size:16px'},
    ]);
    let html=`<div style="display:flex;gap:10px;align-items:center;margin-bottom:14px;flex-wrap:wrap">
      <button class="btn" onclick="loadRank(1)">仅主板</button>
      <button class="btn" onclick="loadRank(0)">全部(含无权限·仅查看)</button>
      ${boardTag} ${cacheTag} <span class="muted">${r.time||'-'} · 涨幅3-5%(主板) + 量比>1 + 换手5-10% + 流通50-200亿</span>
      <button class="btn" style="margin-left:auto" onclick="loadRank(${mainOnly})">刷新</button></div>`;
    if(r.top2&&r.top2.length){
      html+='<div class="top2">'+r.top2.map((s,i)=>{
        const cls=i===0?'gold':'silver';
        const rk=i===0?'🥇 第一':'🥈 第二';
        return `<div class="top-card ${cls}">
          <div class="rank">${rk}</div>
          <div class="nm">${s.name}</div>
          <div class="cd">${s.code} · ${s.board} · ${s.buyable?'可买':'买不了'}</div>
          <div class="sc">${s.score}<span style="font-size:13px;color:var(--text-tertiary)">/10</span></div>
          <div class="mtx">
            <span>现价 ${s.price}</span><span class="pos">+${s.pct}%</span>
            <span>量比 ${s.lb}</span><span>换手 ${s.hsl}%</span><span>流通 ${(s.cap/1e8).toFixed(0)}亿</span>
          </div></div>`;
      }).join('')+'</div>';
    }else{
      html+='<div class="card">今日暂无同时满足4个硬指标的个股（可等尾盘 14:30 后再刷新，或切换「全部A股」）。</div>';
    }
    if(r.list&&r.list.length){
      html+='<table><thead><tr><th>#</th><th>代码</th><th>名称</th><th>板块</th><th>现价</th><th>可买</th><th>涨幅</th><th>量比</th><th>换手</th><th>流通(亿)</th><th>强度分</th></tr></thead><tbody>';
      html+=r.list.map((s,i)=>`<tr>
        <td class="muted">${i+1}</td>
        <td class="muted" style="font-family:monospace">${s.code}</td>
        <td><b>${s.name}</b></td>
        <td class="muted">${s.board}</td>
        <td>${s.price}</td>
        <td>${s.buyable?'<span class="tag tag-green">可买</span>':'<span class="tag tag-red">买不了</span>'}</td>
        <td class="pos">+${s.pct}%</td>
        <td>${s.lb}</td><td>${s.hsl}%</td>
        <td>${(s.cap/1e8).toFixed(0)}</td>
        <td><span class="tag ${s.score>=8?'tag-green':(s.score>=6?'tag-blue':'tag-orange')}">${s.score}</span></td>
      </tr>`).join('');
      html+='</tbody></table>';
    }
    v.innerHTML=html;
  }catch(e){v.innerHTML='<div class="card"><span class="tag tag-red">请求失败</span> '+e+'</div>'}
}
async function loadCheck(){
  statsCards([{l:'单股体检',v:'输入代码'},{l:'指标',v:'4项'},{l:'强度分',v:'/10'},{l:'结论',v:'√/×'}]);
  document.getElementById('view').innerHTML=`
    <div class="card" style="max-width:520px">
      <div style="display:flex;gap:10px;margin-bottom:6px">
        <input class="inp" id="codeInp" placeholder="6位代码，如 603439" maxlength="6" onkeydown="if(event.key==='Enter')doCheck()">
        <button class="btn" onclick="doCheck()">体检</button>
      </div>
      <div class="muted">按「十步尾盘买入法」4项硬指标逐条核对（涨幅区间按板块涨跌幅限制自动适配）</div>
      <div id="checkOut" class="mt" style="margin-top:12px"></div>
    </div>`;
  setTimeout(()=>document.getElementById('codeInp').focus(),50);
}
async function doCheck(){
  const c=document.getElementById('codeInp').value.trim();
  if(!/^\\d{6}$/.test(c)){document.getElementById('checkOut').innerHTML='<div class="tag tag-orange">请输入6位数字代码</div>';return}
  document.getElementById('checkOut').innerHTML='<div class="muted">查询中…</div>';
  const r=await fetch('/api/check/'+c).then(x=>x.json());
  if(!r.ok){document.getElementById('checkOut').innerHTML='<div class="tag tag-red">'+r.error+'</div>';return}
  const boardTag=r.board==='沪主板'||r.board==='深主板'?'<span class="tag tag-blue">'+r.board+'</span>':'<span class="tag tag-orange">'+r.board+'</span>';
  const buyTag=r.buyable?'<span class="tag tag-green">可买</span>':'<span class="tag tag-red">无权限·买不了</span>';
  const rows=Object.entries(r.checks).map(([k,v])=>`<div class="chk"><span class="ic ${v?'ok-ic':'bad-ic'}">${v?'√':'×'}</span><span>${k}</span></div>`).join('');
  document.getElementById('checkOut').innerHTML=`
    <div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap">
      <b style="font-size:20px">${r.name}</b> ${boardTag} ${buyTag}
      <span class="muted" style="font-family:monospace">${r.code}</span>
      <span class="muted">${r.time}</span>
    </div>
    <div style="display:flex;gap:18px;margin:12px 0;flex-wrap:wrap;font-size:13px;color:var(--text-secondary)">
      <span>现价 <b class="pos">${r.price}</b></span>
      <span>涨幅 <b class="pos">${r.pct}%</b></span>
      <span>量比 <b>${r.lb}</b></span>
      <span>换手 <b>${r.hsl}%</b></span>
      <span>流通 <b>${(r.cap/1e8).toFixed(1)}亿</b></span>
    </div>
    ${rows}
    <div style="margin-top:12px">
      ${r.passed?'<span class="tag tag-green">符合尾盘布局初筛</span>':'<span class="tag tag-red">不符合（见上方 × 项）</span>'}
      <span class="tag tag-blue">强度分 ${r.score}/10</span>
    </div>`;
}
function loadMethod(){
  statsCards([{l:'核心',v:'确定性>暴利'},{l:'周期',v:'隔日超短'},{l:'买点',v:'14:30后'},{l:'纪律',v:'持股不过午'}]);
  const steps=[
    ['1','选时定仓','大盘趋势线定仓位：短期升1成 / 中期升3成 / 长期升7成·满仓'],
    ['2','13:30看榜','涨幅 3%–5% 全加自选（结合当日主线题材）'],
    ['3','量比','删量比 < 1 的冷门股'],
    ['4','换手率','删换手 >10% 或 <5%'],
    ['5','市值','删流通市值 >200亿 或 <50亿'],
    ['6','量能','删忽高忽低，只留温和放大'],
    ['7','K线','删高位长上影、上方有压力；只留上方无压力'],
    ['8','分时','全天站上分时均价线、盘口强于大盘'],
    ['9','买入三时机','14:30放量创当日新高→第1笔；回踩不破均价勾头→第2笔；突破前高→第3笔'],
    ['10','卖出','次日破分时均价线/趋势线或破前低即卖，持股不过午'],
  ];
  let html='<div class="card" style="margin-bottom:14px"><div class="label" style="margin-bottom:6px">精髓一句话</div><div style="font-size:15px;color:var(--text-primary)">只赚次日早盘那段「惯性冲高」的确定性利润，用尾盘买入把 T+1 变成「伪 T+0」；极简操作、极速流转、极致纪律。</div></div>';
  html+='<table><thead><tr><th>步</th><th>动作</th><th>要点</th></tr></thead><tbody>';
  html+=steps.map(s=>`<tr><td class="muted">${s[0]}</td><td><b>${s[1]}</b></td><td class="muted">${s[2]}</td></tr>`).join('');
  html+='</tbody></table>';
  html+='<div class="note">板块适配：主板用 3%–5%；创业板/科创板(±20%)用 6%–10%；北交所(±30%)用 9%–15%；ST 回避。<br>新手底线：先模拟1个月，再小仓位练手，止损不果断、追高是最大亏损来源。</div>';
  document.getElementById('view').innerHTML=html;
}
function loadDeploy(){
  statsCards([{l:'PVE 主机',v:'213'},{l:'虚拟机',v:'104'},{l:'端口',v:'8140'},{l:'状态',v:'运行中'}]);
  document.getElementById('view').innerHTML=`
    <div class="card" style="margin-bottom:14px">
      <div class="label">部署信息</div>
      <div class="value" style="font-size:16px;margin-top:10px">PVE 213 · VM 104</div>
      <div class="muted mt" style="margin-top:8px;line-height:1.8">
        工作区：<span style="font-family:monospace">/home/ai/ashare</span>（以后所有 A 股项目统一放这里）<br>
        数据层：<span style="font-family:monospace">tools/screener_data.py</span>（新浪涨幅榜 + 腾讯量比）<br>
        启动：<span style="font-family:monospace">bash web/start.sh</span> · 访问 <span style="font-family:monospace">http://&lt;VM104_IP&gt;:8140</span>
      </div>
    </div>
    <div class="card">
      <div class="label">104万 仓位纪律（杨永兴第1步）</div>
      <div class="muted mt" style="margin-top:8px;line-height:1.9">
        大盘长期上升 → 7成/满仓（约73万–104万）<br>
        中期上升 → 3成（约31万）<br>
        短期上升 → 1成（约10万）<br>
        三个买点各约 1/3，买点不出现就不补；单票破均价线/前低 → 全撤，不补仓摊平。
      </div>
    </div>`;
}
let alertTimer=null;
async function loadAlerts(){
  if(alertTimer){clearInterval(alertTimer);alertTimer=null;}
  document.getElementById('view').innerHTML='<div class="card muted">加载监控状态…</div>';
  try{
    const [st,w,a,q]=await Promise.all([
      fetch('/api/state').then(x=>x.json()),
      fetch('/api/watch').then(x=>x.json()),
      fetch('/api/alerts').then(x=>x.json()),
      fetch('/api/watch_quotes').then(x=>x.json())
    ]);
    const s=st.state||{};
    const trading=s.trading?'<span class="tag tag-green">交易中</span>':'<span class="tag tag-orange">休市</span>';
    statsCards([
      {l:'监控心跳',v:s.heartbeat||'-',s:'font-size:14px'},
      {l:'交易状态',v:trading},
      {l:'监控标的',v:(w.picks||[]).length+' 只'},
      {l:'最新提示',v:(a.alerts&&a.alerts[0]?a.alerts[0].time.slice(5,16):'-'),s:'font-size:13px'},
    ]);
    let html='<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">';
    html+='<div class="card"><div class="label">尾盘标的 · 实时（次日卖出监控）</div>';
    const qs=q.quotes||[];
    if(qs.length){
      html+='<table style="margin-top:8px"><thead><tr><th>代码</th><th>名称</th><th>现价</th><th>涨幅</th><th>均价</th><th>状态</th></tr></thead><tbody>';
      html+=qs.map(x=>{
        const prev=x.prev_close||0, price=x.price||0, vwap=x.vwap||0, pct=(x.pct||0);
        let stTxt='持有', sc='tag-green';
        if(prev>0&&price<prev){stTxt='破前低·卖';sc='tag-red';}
        else if(vwap>0&&price<vwap){stTxt='破均价·卖';sc='tag-red';}
        else if(prev>0&&pct>=2){stTxt='冲高·止盈';sc='tag-orange';}
        return `<tr><td class="muted" style="font-family:monospace">${x.code}</td><td><b>${x.name}</b></td><td>${price}</td><td class="${pct>=0?'pos':'neg'}">${pct>=0?'+':''}${pct}%</td><td class="muted">${vwap?vwap.toFixed(2):'-'}</td><td><span class="tag ${sc}">${stTxt}</span></td></tr>`;
      }).join('');
      html+='</tbody></table>';
    }else html+='<div class="muted" style="margin-top:8px">暂无（尾盘 14:25 后自动生成）</div>';
    html+='</div>';
    html+='<div class="card"><div class="label">最近提示</div>';
    if(a.alerts&&a.alerts.length){
      html+='<div style="max-height:460px;overflow:auto;margin-top:8px">'+a.alerts.map(x=>{
        const cls=x.kind.includes('卖')?'tag-red':(x.kind.includes('买')?'tag-green':'tag-orange');
        return `<div style="padding:10px 0;border-bottom:1px solid var(--border-color)"><div><span class="tag ${cls}">${x.kind}</span> <b>${x.title}</b></div><div class="muted" style="margin-top:5px;white-space:pre-wrap">${x.content}</div><div class="muted" style="margin-top:4px">${x.time}</div></div>`;
      }).join('')+'</div>';
    }else html+='<div class="muted" style="margin-top:8px">暂无提示</div>';
    html+='</div></div>';
    html+='<div class="note">显示模式：提示直接在网页展示(不推微信)。监控由 systemd(weipan-monitor) 常驻，交易日盘中每30秒一轮，本页30秒自动刷新。</div>';
    document.getElementById('view').innerHTML=html;
    if(curTab==='alerts')alertTimer=setInterval(loadAlerts,30000);
  }catch(e){
    document.getElementById('view').innerHTML='<div class="card"><span class="tag tag-red">加载失败</span> '+e+'</div>';
  }
}
loadRank();
</script>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8140"))
    print(f"尾盘打法看板已启动: http://0.0.0.0:{port}  (PVE 213 / VM 104)")
    app.run(host="0.0.0.0", port=port, debug=False)
