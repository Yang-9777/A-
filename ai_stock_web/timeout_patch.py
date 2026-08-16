#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 requests 加默认超时（akshare 内部请求不带 timeout，新浪限流时会挂死）。"""
import requests


def patch_default_timeout(seconds=15):
    _orig = requests.sessions.Session.request

    def _patched(self, method, url, **kwargs):
        kwargs.setdefault("timeout", seconds)
        return _orig(self, method, url, **kwargs)

    requests.sessions.Session.request = _patched


patch_default_timeout(15)
