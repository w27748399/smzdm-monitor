#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信小兔爆料监控 · 轻量替代实现
================================
原「信小兔监控5.exe」是易语言编译的 Windows GUI 程序 —— 无源码、绑定本机、无法上云。
本脚本用 Python 重写同一件事：定时抓取什么值得买用户爆料页 -> 解析 -> 去重 -> 推送手机。

特性：
  * 纯 Python + requests，无框架，单文件
  * 本地常驻 / Docker / Koyeb / 任意 VPS 通用同一份代码
  * 7 种推送通道：企业微信机器人 / WxPusher / Server酱 / Bark / Telegram / 自定义 Webhook / 邮件SMTP
  * 内置健康检查 HTTP 服务（云平台保活用，如 Koyeb 要求监听端口）
  * 首轮只建基线不推送，避免开机刷屏

用法：
  pip install requests
  python monitor.py               # 常驻轮询
  python monitor.py --once        # 只跑一轮（验证解析是否正常）
  python monitor.py --test-push   # 测试推送通道
  python monitor.py --no-push     # 只打印，不推送

配置：优先读环境变量；也可在同目录放 config.json（键名同环境变量）。
"""

import os
import re
import sys
import json
import time
import html
import smtplib
import logging
import threading
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from datetime import datetime, timezone, timedelta

try:
    import requests
except ImportError:
    print("缺少依赖，请先执行：pip install requests")
    sys.exit(1)

# ─────────────────────────── 配置 ───────────────────────────
CST = timezone(timedelta(hours=8))          # 北京时间
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

_DEFAULTS = {
    "MEMBER_ID": "9687682701",              # 什么值得买用户 ID（信小兔）
    "POLL_INTERVAL": "10",                  # 轮询间隔（秒）—— 真正决定"秒级"的参数
    "STATE_FILE": "state.json",             # 去重状态文件
    "MAX_STATE": "800",                     # 状态文件最多保留多少条 ID
    "PORT": "8899",                         # 健康检查端口（云平台需要；本地默认 8899 避免常见冲突）
    "NO_HEALTH_SERVER": "0",                # 本地跑可设 1 关掉健康检查
    "FIRST_RUN_PUSH": "0",                  # 首次运行是否推送历史（默认不推）
    "TIMEOUT": "15",                        # 单次请求超时（秒）
    "RETRY": "3",                           # 单轮失败重试次数
    "PUSH_CHANNEL": "",                     # wecom / wxpusher / serverchan / bark / telegram / webhook / email
    "USER_AGENT": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    # --- 各推送渠道凭据（按需填一个） ---
    "WECOM_WEBHOOK": "",                    # 企业微信群机器人 Webhook 地址
    "WXPUSHER_TOKEN": "",                   # WxPusher appToken
    "WXPUSHER_UID": "",                     # WxPusher UID
    "SERVERCHAN_KEY": "",                   # Server酱 SendKey
    "BARK_URL": "",                         # Bark 地址，如 https://api.day.app/xxxxxx
    "TELEGRAM_TOKEN": "",                   # Telegram Bot Token
    "TELEGRAM_CHAT": "",                    # Telegram chat_id
    "WEBHOOK_URL": "",                      # 通用 Webhook
    # --- 邮件通道（PUSH_CHANNEL=email 时生效）---
    "EMAIL_USER": "",                       # 发件邮箱，如 xxx@qq.com
    "EMAIL_PASS": "",                       # SMTP 授权码（不是邮箱登录密码！）
    "EMAIL_TO": "",                         # 收件邮箱，留空 = 发给自己
    "SMTP_HOST": "",                        # 留空则按邮箱域名自动推断
    "SMTP_PORT": "",                        # 留空则自动（SSL 465 / STARTTLS 587）
    "SMTP_SSL": "",                         # 留空自动；"0" 强制 STARTTLS，"1" 强制 SSL
}


def _load_config():
    cfg = dict(_DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg.update({k: str(v) for k, v in json.load(f).items()})
        except Exception as e:
            print("[warn] config.json 解析失败，忽略：%s" % e)
    for k in list(cfg.keys()):              # 环境变量优先级最高
        if os.getenv(k) not in (None, ""):
            cfg[k] = os.getenv(k)
    return cfg


CFG = _load_config()

MEMBER_ID = CFG["MEMBER_ID"]
TARGET_URL = CFG.get("TARGET_URL") or (
    "https://zhiyou.smzdm.com/member/%s/baoliao/" % MEMBER_ID
)
POLL_INTERVAL = max(3, int(CFG["POLL_INTERVAL"]))
STATE_PATH = CFG["STATE_FILE"]
if not os.path.isabs(STATE_PATH):
    STATE_PATH = os.path.join(BASE_DIR, STATE_PATH)
MAX_STATE = int(CFG["MAX_STATE"])
PORT = int(CFG["PORT"])
TIMEOUT = int(CFG["TIMEOUT"])
RETRY = int(CFG["RETRY"])
FIRST_RUN_PUSH = CFG["FIRST_RUN_PUSH"] == "1"
CHANNEL = (CFG["PUSH_CHANNEL"] or "").strip().lower()

# ───────────────────── 解析规则（沿用原 EXE 的正则） ─────────────────────
# 实测 2026-10-09：该正则对当前页面仍然完全有效，一次可命中 10 条
ITEM_RE = re.compile(
    r'https://www\.smzdm\.com/p/(\d+)/" target="_blank"\>(.+?)\<\/a\>\<\/div\>'
)
PRICE_RE = re.compile(
    r'/p/(\d+)/" class="price[^"]*" target="_blank">(.*?)</a>'
)
# 备用：某些情况下 class 写法变化
PRICE_RE2 = re.compile(r'/p/(\d+)/"[^>]*class="price[^"]*"[^>]*>(.*?)</a>')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("xxt")


def now_str():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


# ─────────────────────────── 状态 ───────────────────────────
def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
                return list(d.get("seen", [])), bool(d.get("initialized", False))
        except Exception as e:
            log.warning("状态文件损坏，重建：%s", e)
    return [], False


def save_state(seen, initialized=True):
    """写盘只在去重集合真正变化时发生（返回 True 表示写了）。

    这一点对 GitHub Actions 部署是必需的：runner 是一次性的，状态要靠
    git 回写仓库来持久化。如果每次轮询都刷新时间戳，就会产生每分钟一次
    的空提交。改为"无变化不写盘"后，提交频率 = 新爆料频率。
    """
    seen = seen[-MAX_STATE:]
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                old = json.load(f)
            if (old.get("seen") == seen
                    and bool(old.get("initialized", False)) == initialized):
                return False
        except Exception:
            pass
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"seen": seen, "initialized": initialized,
                   "updated": now_str()}, f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)
    return True


# ─────────────────────────── 抓取 ───────────────────────────
def fetch(url):
    headers = {
        "User-Agent": CFG["USER_AGENT"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.smzdm.com/",
        "Connection": "close",
    }
    last = None
    for i in range(RETRY):
        try:
            r = requests.get(url, headers=headers, timeout=TIMEOUT)
            if r.status_code == 200:
                r.encoding = r.apparent_encoding or "utf-8"
                return r.text
            last = "HTTP %s" % r.status_code
        except Exception as e:
            last = str(e)
        if i < RETRY - 1:
            time.sleep(1.5 * (i + 1))
    log.warning("抓取失败：%s", last)
    return None


def parse(page):
    """返回 [{id,title,price,url}]，按页面顺序（新->旧）"""
    items = []
    prices = {}
    for rx in (PRICE_RE, PRICE_RE2):
        for pid, p in rx.findall(page):
            prices.setdefault(pid, html.unescape(_strip(p)).strip())
        if prices:
            break
    for pid, title in ITEM_RE.findall(page):
        items.append({
            "id": pid,
            "title": html.unescape(_strip(title)).strip(),
            "price": prices.get(pid, ""),
            "url": "https://www.smzdm.com/p/%s/" % pid,
        })
    return items


def _strip(s):
    return re.sub(r"<[^>]+>", "", s or "")


# ─────────────────────────── 推送 ───────────────────────────
def _post(url, payload, ok_desc):
    try:
        r = requests.post(url, json=payload, timeout=TIMEOUT)
        log.info("推送[%s] HTTP %s %s", ok_desc, r.status_code,
                 "" if r.status_code < 300 else r.text[:200])
        return r.status_code < 300
    except Exception as e:
        log.warning("推送失败[%s]：%s", ok_desc, e)
        return False


def push_item(item):
    title = item["title"]
    price = item["price"]
    link = item["url"]
    text = "%s%s\n%s" % (title, ("　" + price) if price else "", link)

    if not CHANNEL:
        log.info("[未配置推送] %s", text.replace("\n", " | "))
        return True

    if CHANNEL == "wecom":
        md = "**%s**\n> 价格：%s\n> [点此查看详情](%s)\n> <font color=\"comment\">%s</font>" % (
            title, price or "—", link, now_str())
        return _post(CFG["WECOM_WEBHOOK"],
                     {"msgtype": "markdown", "markdown": {"content": md}}, "wecom")

    if CHANNEL == "wxpusher":
        return _post("https://wxpusher.zjiecode.com/api/send/message",
                     {"appToken": CFG["WXPUSHER_TOKEN"],
                      "content": "<h3>%s</h3><p>价格：%s</p><p><a href=\"%s\">查看详情</a></p><p>%s</p>"
                                 % (title, price or "—", link, now_str()),
                      "summary": title, "contentType": 2,
                      "uids": [u for u in CFG["WXPUSHER_UID"].split(",") if u]}, "wxpusher")

    if CHANNEL == "serverchan":
        return _post("https://sctapi.ftqq.com/%s.send" % CFG["SERVERCHAN_KEY"],
                     {"title": title, "desp": "**价格**：%s\n\n[点此查看](%s)\n\n%s"
                                               % (price or "—", link, now_str())}, "serverchan")

    if CHANNEL == "bark":
        base = CFG["BARK_URL"].rstrip("/")
        return _post("%s/%s/%s" % (base, requests.utils.quote(title),
                                   requests.utils.quote(price or "点击查看")),
                     {"url": link}, "bark")

    if CHANNEL == "telegram":
        return _post("https://api.telegram.org/bot%s/sendMessage" % CFG["TELEGRAM_TOKEN"],
                     {"chat_id": CFG["TELEGRAM_CHAT"],
                      "text": "%s\n价格：%s\n%s" % (title, price or "—", link),
                      "disable_web_page_preview": False}, "telegram")

    if CHANNEL == "webhook":
        return _post(CFG["WEBHOOK_URL"],
                     {"title": title, "price": price, "url": link,
                      "time": now_str(), "text": text}, "webhook")

    if CHANNEL == "email":
        return _send_email(item)

    log.warning("未知推送通道：%s", CHANNEL)
    return False


# 常见邮箱 -> (SMTP 服务器, 端口, 是否 SSL)
_SMTP_TABLE = {
    "qq.com":      ("smtp.qq.com", 465, True),
    "foxmail.com": ("smtp.qq.com", 465, True),
    "163.com":     ("smtp.163.com", 465, True),
    "126.com":     ("smtp.126.com", 465, True),
    "sina.com":    ("smtp.sina.com", 465, True),
    "sohu.com":    ("smtp.sohu.com", 465, True),
    "gmail.com":   ("smtp.gmail.com", 465, True),
    "outlook.com": ("smtp.office365.com", 587, False),
    "hotmail.com": ("smtp.office365.com", 587, False),
    "live.com":    ("smtp.office365.com", 587, False),
}


def _send_email(item):
    """SMTP 发信。EMAIL_PASS 必须是『授权码』，不是邮箱登录密码。"""
    user = (CFG["EMAIL_USER"] or "").strip()
    password = (CFG["EMAIL_PASS"] or "").strip()
    to = (CFG["EMAIL_TO"] or "").strip() or user
    if not user or not password:
        log.warning("推送失败[email]：EMAIL_USER / EMAIL_PASS 未配置")
        return False

    if CFG["SMTP_HOST"]:
        host = CFG["SMTP_HOST"].strip()
        port = int(CFG["SMTP_PORT"] or 465)
        use_ssl = CFG["SMTP_SSL"] != "0"
    else:
        domain = user.split("@")[-1].lower()
        host, port, use_ssl = _SMTP_TABLE.get(domain, ("", 465, True))
        if not host:
            log.warning("推送失败[email]：不认识的邮箱域名 %s，请手动配 SMTP_HOST", domain)
            return False

    title, price, link = item["title"], item["price"], item["url"]
    body_html = (
        '<div style="font-family:sans-serif;max-width:520px">'
        '<h2 style="margin:0 0 8px">%s</h2>'
        '<p style="margin:4px 0;color:#c0392b;font-size:15px">💰 价格：%s</p>'
        '<p style="margin:12px 0"><a href="%s" style="background:#e74c3c;color:#fff;'
        'padding:8px 18px;border-radius:4px;text-decoration:none">点此查看详情</a></p>'
        '<p style="color:#999;font-size:12px">%s · 信小兔爆料监控</p></div>'
    ) % (html.escape(title), html.escape(price or "—"), html.escape(link), now_str())

    msg = MIMEText(body_html, "html", "utf-8")
    msg["Subject"] = Header("爆料: %s%s" % (title, ("　" + price) if price else ""), "utf-8")
    msg["From"] = formataddr((Header("信小兔爆料监控", "utf-8").encode(), user))
    msg["To"] = to

    try:
        if use_ssl:
            s = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT)
        else:
            s = smtplib.SMTP(host, port, timeout=TIMEOUT)
            s.starttls()
        try:
            s.login(user, password)
            s.sendmail(user, [x.strip() for x in to.split(",") if x.strip()],
                       msg.as_string())
        finally:
            s.quit()
        log.info("推送[email] 已发送 -> %s", to)
        return True
    except Exception as e:
        log.warning("推送失败[email]：%s", e)
        return False


# ────────────────────── 健康检查 HTTP 服务 ──────────────────────
_HEALTH = {"last_ok": None, "last_items": 0, "rounds": 0, "err": ""}


def start_health_server():
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({
                "service": "xxt-smzdm-monitor",
                "member": MEMBER_ID,
                "status": "ok",
                "rounds": _HEALTH["rounds"],
                "last_success": _HEALTH["last_ok"],
                "last_item_count": _HEALTH["last_items"],
                "last_error": _HEALTH["err"],
                "interval_sec": POLL_INTERVAL,
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = None
    for p in range(PORT, PORT + 10):        # 端口被占则自动 +1 避让
        try:
            srv = HTTPServer(("0.0.0.0", p), H)
            break
        except OSError:
            continue
    if srv is None:
        log.warning("健康检查端口 %d~%d 全部被占用，跳过健康检查", PORT, PORT + 9)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("健康检查已启动：http://0.0.0.0:%d/", srv.server_address[1])


# ─────────────────────────── 主循环 ───────────────────────────
def one_round(seen, initialized, do_push=True):
    page = fetch(TARGET_URL)
    if page is None:
        _HEALTH["err"] = "fetch failed"
        return seen, initialized

    items = parse(page)
    _HEALTH["last_items"] = len(items)
    _HEALTH["last_ok"] = now_str()
    _HEALTH["err"] = ""

    if not items:
        log.warning("解析到 0 条，页面结构可能变了（HTML %d 字节）", len(page))
        return seen, initialized

    new = [it for it in items if it["id"] not in seen]

    if not initialized and not FIRST_RUN_PUSH:
        log.info("首轮建基线：记录 %d 条，不推送。最新一条：%s",
                 len(items), items[0]["title"])
        for it in items:
            if it["id"] not in seen:
                seen.append(it["id"])
        return seen, True

    if not new:
        log.debug("无新爆料（共 %d 条）", len(items))
        return seen, True

    for it in reversed(new):        # 从旧到新推，手机上顺序自然
        log.info("★ 新爆料 [%s] %s %s", it["id"], it["title"], it["price"])
        if do_push:
            push_item(it)
        seen.append(it["id"])

    return seen, True


def main():
    args = sys.argv[1:]
    once = "--once" in args
    no_push = "--no-push" in args
    test_push = "--test-push" in args

    log.info("=" * 62)
    log.info("信小兔爆料监控  |  用户 %s", MEMBER_ID)
    log.info("目标：%s", TARGET_URL)
    log.info("间隔：%d 秒  |  推送：%s  |  状态：%s",
             POLL_INTERVAL, CHANNEL or "(未配置，仅打印)", STATE_PATH)
    log.info("=" * 62)

    if test_push:
        if not CHANNEL:
            log.error("未配置 PUSH_CHANNEL，无法测试。")
            return
        ok = push_item({"title": "【测试】信小兔监控已接通",
                        "price": "—", "url": TARGET_URL})
        log.info("测试推送结果：%s", "成功" if ok else "失败")
        return

    seen, initialized = load_state()
    if seen:
        log.info("已加载历史状态：%d 条 ID", len(seen))

    if not CFG["NO_HEALTH_SERVER"] == "1":
        try:
            start_health_server()
        except OSError as e:
            log.warning("健康检查端口 %d 占用，跳过：%s", PORT, e)

    if once:
        seen, initialized = one_round(seen, initialized, do_push=not no_push)
        if save_state(seen, initialized):
            log.info("状态已更新（%d 条记录）。", len(seen[-MAX_STATE:]))
        log.info("单轮完成。")
        return

    log.info("进入常驻轮询（Ctrl+C 停止）…")
    while True:
        t0 = time.time()
        try:
            seen, initialized = one_round(seen, initialized, do_push=not no_push)
            if save_state(seen, initialized):
                log.info("状态已更新（%d 条记录）。", len(seen[-MAX_STATE:]))
            _HEALTH["rounds"] += 1
        except KeyboardInterrupt:
            log.info("已停止。")
            break
        except Exception as e:
            log.exception("轮询异常：%s", e)
            _HEALTH["err"] = str(e)
        time.sleep(max(1.0, POLL_INTERVAL - (time.time() - t0)))


if __name__ == "__main__":
    main()
