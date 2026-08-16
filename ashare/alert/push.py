#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推送模块：Server酱 / PushPlus / 企业微信机器人；未配置则只落日志+alerts.json"""
import os, json, time, urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, "config.json")
ALERTS = os.path.join(HERE, "alerts.json")


def load_cfg():
    try:
        return json.load(open(CFG, encoding="utf-8"))
    except Exception:
        return {}


def _post(url, payload, as_json=True):
    data = json.dumps(payload).encode("utf-8") if as_json else urllib.parse.urlencode(payload).encode()
    hdr = {"Content-Type": "application/json"} if as_json else {"Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(url, data=data, headers=hdr)
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False


def save_alert(kind, title, content):
    rec = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "title": title, "content": content}
    arr = []
    try:
        arr = json.load(open(ALERTS, encoding="utf-8"))
    except Exception:
        arr = []
    arr.insert(0, rec)
    json.dump(arr[:200], open(ALERTS, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return rec


def push(kind, title, content):
    save_alert(kind, title, content)
    full = f"[{kind}] {title}\n{content}"
    print(full)
    c = load_cfg().get("channels", {}) or {}
    sent = []
    if c.get("serverchan_key"):
        sent.append(("Server酱", _post(f"https://sctapi.ftqq.com/{c['serverchan_key']}.send",
                                        {"title": title, "desp": content}, as_json=False)))
    if c.get("pushplus_token"):
        sent.append(("PushPlus", _post("http://www.pushplus.plus/send",
                                       {"token": c["pushplus_token"], "title": title, "content": content})))
    if c.get("wecom_webhook"):
        sent.append(("企业微信", _post(c["wecom_webhook"],
                                       {"msgtype": "text", "text": {"content": f"{title}\n{content}"}})))
    if not sent:
        print("  [提示] 未配置推送通道，仅记录到 alerts.json；在 config.json 填 key 后可推到微信")
    else:
        for name, ok in sent:
            print(f"  推送[{name}] {'成功' if ok else '失败'}")
    return full
