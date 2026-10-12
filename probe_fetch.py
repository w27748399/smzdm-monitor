# -*- coding: utf-8 -*-
"""一次性诊断：三种抓取方式对比，看机房 IP 能否拿到最新爆料页
验收标准：能否解析到本地已确认的最新爆料 183863145（特仑苏）
"""
import re
import time

ITEM_RE = re.compile(r'https://www\.smzdm\.com/p/(\d+)/" target="_blank"\>(.+?)\<\/a\>\<\/div\>')
EXPECTED = "183863145"
URL = "https://zhiyou.smzdm.com/member/9687682701/baoliao/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.smzdm.com/",
}


def report(tag, page):
    if not page:
        print("[%s] 抓取失败" % tag)
        return
    ids = ITEM_RE.findall(page)
    uniq = list(dict.fromkeys(i for i, _ in ids))
    hit = "✅ 能看到最新爆料" if EXPECTED in uniq else "❌ 只有旧缓存页"
    print("[%s] %d 字节 | 解析 %d 条 | 最大ID %s | %s" % (
        tag, len(page), len(uniq), max(uniq) if uniq else "-", hit))
    if uniq:
        print("    前三条:", uniq[:3])


# ── 方式1：requests 现状 ──
import requests
s = requests.Session()
r = s.get(URL, headers=HEADERS, timeout=15)
print("[requests 现状] HTTP", r.status_code, "| 关键响应头:",
      {k: v for k, v in r.headers.items() if k.lower() in ("x-cache", "age", "cf-cache-status", "x-via", "server")})
report("requests 现状", r.text if r.status_code == 200 else None)

# ── 方式2：curl_cffi 伪装 Chrome TLS 指纹 ──
try:
    from curl_cffi import requests as creq
    r2 = creq.get(URL, headers=HEADERS, timeout=15, impersonate="chrome124")
    print("[curl_cffi chrome] HTTP", r2.status_code)
    report("curl_cffi chrome", r2.text if r2.status_code == 200 else None)
except Exception as e:
    print("[curl_cffi chrome] 异常:", e)

# ── 方式3：手机 UA（requests） ──
h3 = dict(HEADERS)
h3["User-Agent"] = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
r3 = s.get(URL, headers=h3, timeout=15)
print("[requests 手机UA] HTTP", r3.status_code)
report("requests 手机UA", r3.text if r3.status_code == 200 else None)

print()
print("本地基准：最新爆料应为", EXPECTED, "（特仑苏 有机纯牛奶 30.97元）")
