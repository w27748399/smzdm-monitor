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
from datetime import datetime, date, timezone, timedelta

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
    # --- 天津尾号限行提醒（附加功能，随监控链每轮顺带检查）---
    "LIMIT_TAIL": "1",                      # 关注的尾号（单组旧配置，保留兼容）
    "LIMIT_EMAIL_TO": "",                   # 限行提醒额外同步的收件邮箱（单组旧配置）
    # 多组规则（推荐）：JSON 数组，每组独立判断、独立去重、独立收件人
    #   例：[{"tails":"1","to":""},{"tails":"3,6","to":"a@qq.com,b@qq.com"}]
    #   to 留空 = 回退 EMAIL_TO + LIMIT_EMAIL_TO；tails 支持逗号/斜杠/顿号分隔
    #   ⚠️ 公开仓库里必须存 Secrets，不要写进 workflow 明文
    "LIMIT_RULES": "",
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
    }
    # ⚠️ 2026-10-12 取证结论：不要发 "Connection: close"，也不要 60s 高频连击。
    # smzdm 对机房 IP 的高频抓取会喂"冻结的旧缓存页"——页面结构完好、解析不报
    # 错，只是永远没有新爆料（静默坑，监控连跑两天 0 新爆料）。实测低频 + 普通
    # keep-alive 请求即可拿到实时页（诊断 run 38265342850：requests 单发就能
    # 看到最新爆料）。轮询间隔因此从 60s 提到 300s。
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


# ────────────── 天津尾号限行提醒（尾号1 · 限行日 7-19 时整点）──────────────
# 规则数据源：知识库单一数据源 tianjin_restriction_data.py（G:\DSH\tianjin_car_restriction\）
# 锚点算法：2026-06-29（周一）限行 1/6，每 13 周（91 天）周一组递减 1 位（mod 5）。
# 已与天津公安局通告 4 期官方表逐期核对通过；权威期表见通告
# （2026-03-30 ~ 2027-03-28，工作日 7-19 时，外环线(不含)以内）。
# ⚠️ 2027 节假日官方安排公布后，需同步更新 _LIMIT_HOLIDAYS（与单一数据源同步改）。
LIMIT_TAIL = (CFG.get("LIMIT_TAIL") or "1").strip()
LIMIT_EMAIL_TO = (CFG.get("LIMIT_EMAIL_TO") or "").strip()
LIMIT_RULES_RAW = (CFG.get("LIMIT_RULES") or "").strip()
LIMIT_STATE_PATH = os.path.join(BASE_DIR, "limit_state.json")

_LIMIT_ANCHOR = date(2026, 6, 29)   # 组0 周一 = 1/6
_LIMIT_CYCLE = 91                   # 13 周一周期
_LIMIT_PAIRS = ["1/6", "2/7", "3/8", "4/9", "5/0"]   # 组 g 的周一排序
_LIMIT_HOLIDAYS = [                  # 放假区间内不限行（天津调休补班周末也不限）
    (date(2026, 1, 1),  date(2026, 1, 3)),   # 元旦
    (date(2026, 2, 15), date(2026, 2, 23)),  # 春节
    (date(2026, 4, 4),  date(2026, 4, 6)),   # 清明
    (date(2026, 5, 1),  date(2026, 5, 5)),   # 劳动节
    (date(2026, 6, 19), date(2026, 6, 21)),  # 端午
    (date(2026, 9, 25), date(2026, 9, 27)),  # 中秋
    (date(2026, 10, 1), date(2026, 10, 7)),  # 国庆
    (date(2027, 1, 1),  date(2027, 1, 1)),   # 2027 元旦（框架保证 1 天，其余待官方公布）
]


def _limit_pair(d):
    """返回 d 当天限行的尾号串（如 '5/0'）；不限行返回 None。"""
    if d.weekday() >= 5:                        # 周六日（含调休补班）天津不限尾号
        return None
    for a, b in _LIMIT_HOLIDAYS:
        if a <= d <= b:
            return None
    n = (d - _LIMIT_ANCHOR).days // _LIMIT_CYCLE
    g = (-n) % 5
    return _LIMIT_PAIRS[(g + d.weekday()) % 5]


def _limit_period_rows(d):
    """返回当期（d 所在 13 周期）周一~周五的限行尾号行，供邮件表格用。"""
    n = (d - _LIMIT_ANCHOR).days // _LIMIT_CYCLE
    g = (-n) % 5
    return [(["一", "二", "三", "四", "五"][i], _LIMIT_PAIRS[(g + i) % 5])
            for i in range(5)]


def _parse_tails(s):
    """'3,6' / '1/6' / '3、6' -> {'3','6'}"""
    return {t for t in re.split(r"[,/、，\s]+", str(s or "")) if t.strip()}


def _limit_rules():
    """解析限行提醒规则组，返回 [{key, tails:set, to:list}]。

    优先用 LIMIT_RULES（JSON）；没配则回退到老的 LIMIT_TAIL + EMAIL_TO/LIMIT_EMAIL_TO，
    保证老部署在没改配置前行为完全不变。
    """
    rules = []
    if LIMIT_RULES_RAW:
        try:
            data = json.loads(LIMIT_RULES_RAW)
            if isinstance(data, dict):
                data = data.get("rules") or []
            for i, r in enumerate(data):
                if isinstance(r, str):                  # 允许写成纯尾号串
                    tails_s, to_s = r, ""
                elif isinstance(r, dict):
                    tails_s, to_s = r.get("tails", ""), r.get("to", "")
                else:
                    continue
                tails = _parse_tails(tails_s)
                if not tails:
                    continue
                rules.append({
                    "key": "r%d" % i,
                    "tails": tails,
                    "to": [x.strip() for x in str(to_s or "").split(",") if x.strip()],
                })
        except Exception as e:
            log.warning("LIMIT_RULES 解析失败（%s），回退到 LIMIT_TAIL", e)
            rules = []
    if not rules:
        tails = _parse_tails(LIMIT_TAIL) or {"1"}
        rules = [{"key": "r0", "tails": tails, "to": []}]
    return rules


def _limit_state_load():
    """返回 {rule_key: 'YYYY-MM-DD-HH'}；兼容老格式 {"last": "..."} -> {"r0": ...}"""
    try:
        with open(LIMIT_STATE_PATH, "r", encoding="utf-8") as f:
            obj = json.load(f) or {}
    except Exception:
        return {}
    rules = obj.get("rules")
    if isinstance(rules, dict):
        return dict(rules)
    last = obj.get("last")
    if isinstance(last, str) and last:
        return {"r0": last}
    return {}


def _limit_state_save(state):
    tmp = LIMIT_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"rules": state, "updated": now_str()}, f, ensure_ascii=False)
    os.replace(tmp, LIMIT_STATE_PATH)


def _smtp_endpoint():
    """SMTP 服务器的 (host, port, use_ssl)。"""
    if CFG["SMTP_HOST"]:
        return (CFG["SMTP_HOST"].strip(), int(CFG["SMTP_PORT"] or 465),
                CFG["SMTP_SSL"] != "0")
    domain = (CFG["EMAIL_USER"] or "").split("@")[-1].lower()
    return _SMTP_TABLE.get(domain, ("", 465, True))


def _send_limit_email(now_bj, pair, label, to_list=None, tag=""):
    """发限行提醒邮件。to_list 为空时回退 EMAIL_TO + LIMIT_EMAIL_TO。"""
    user = (CFG["EMAIL_USER"] or "").strip()
    password = (CFG["EMAIL_PASS"] or "").strip()
    if not user or not password:
        log.warning("限行提醒失败：EMAIL_USER / EMAIL_PASS 未配置")
        return False
    if not to_list:
        to_list = [x.strip() for x in
                   ((CFG["EMAIL_TO"] or "") + "," + LIMIT_EMAIL_TO).split(",")
                   if x.strip()] or [user]
    host, port, use_ssl = _smtp_endpoint()
    if not host:
        log.warning("限行提醒失败：无法确定 SMTP 服务器")
        return False

    wd = "一二三四五六日"[now_bj.weekday()]
    tails = pair.split("/")
    subject = "🚗 今日限行尾号%s（周%s）· %s" % (
        "/".join(tails), wd, label)
    if tag:                                     # 多组规则时标明这封是给哪组尾号的
        subject += " · 你的尾号 %s" % tag
    rows = _limit_period_rows(now_bj.date())
    tr = "".join(
        '<tr style="background:%s"><td style="padding:4px 14px">周%s</td>'
        '<td style="padding:4px 14px;font-weight:%s">%s</td></tr>'
        % ("#fdecea" if t == pair else "transparent", w,
           "700;color:#c0392b" if t == pair else "400", t.replace("/", " 和 "))
        for w, t in rows)
    n = (now_bj.date() - _LIMIT_ANCHOR).days // _LIMIT_CYCLE
    next_rotate = _LIMIT_ANCHOR + timedelta(days=(n + 1) * _LIMIT_CYCLE)
    body_html = (
        '<div style="font-family:sans-serif;max-width:560px">'
        '<h2 style="margin:0 0 6px;color:#c0392b">🚗 今日限行尾号 %s（周%s）</h2>'
        '<p style="margin:4px 0">%s · <b>%s</b></p>'
        '<table style="border-collapse:collapse;margin:10px 0">%s</table>'
        '<p style="margin:4px 0">⏰ 限行时间：<b>7:00 – 19:00</b>　📍 范围：外环线(不含)以内</p>'
        '<p style="margin:4px 0">尾号为英文字母的按 0 号管理；周末及法定节假日不限行。</p>'
        '<p style="margin:10px 0 4px;color:#e67e22">⚠️ %s 起尾号轮换，届时提醒自动跟随新表。</p>'
        '<p style="color:#999;font-size:12px">%s · 天津尾号限行提醒（GitHub Actions）</p></div>'
    ) % ("/".join(tails), wd, now_bj.strftime("%Y-%m-%d"), label,
         tr, next_rotate.isoformat(), now_str())

    msg = MIMEText(body_html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((Header("天津限行提醒", "utf-8").encode(), user))
    msg["To"] = ", ".join(to_list)
    try:
        if use_ssl:
            s = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT)
        else:
            s = smtplib.SMTP(host, port, timeout=TIMEOUT)
            s.starttls()
        try:
            s.login(user, password)
            s.sendmail(user, to_list, msg.as_string())
        finally:
            s.quit()
        log.info("限行提醒[email] 已发送 -> %s", ", ".join(to_list))
        return True
    except Exception as e:
        log.warning("限行提醒失败[email]：%s", e)
        return False


def limit_reminder_tick(now_bj=None):
    """每个轮次调用。限行日 7-19 时每小时一封；当天迟到的首跑也会补发一条。
    支持多组规则（LIMIT_RULES）：每组关注不同尾号、独立去重、独立收件人。
    横幅提醒走 QQ 邮箱 App 通知 / 微信"QQ邮箱提醒"公众号（邮件通道自带，免费无限量）；
    pushplus 实名收费已淘汰（2026-10-14）。"""
    now_bj = now_bj or datetime.now(CST)
    d, h = now_bj.date(), now_bj.hour
    pair = _limit_pair(d)
    if not pair:
        return                                  # 周末 / 法定节假日不限行
    digits = set(pair.replace("/", ""))         # '1/6' -> {'1','6'}
    today = d.isoformat()
    key = "%s-%02d" % (today, h)

    in_window = 7 <= h <= 19
    if in_window:
        label = "第%d次提醒 %02d:00" % (h - 6, h)
    else:
        label = "出行前预告 %02d:%02d" % (h, now_bj.minute)

    rules = _limit_rules()
    multi = len(rules) > 1                      # 多组时才在主题里标尾号，单组保持原样
    state = _limit_state_load()
    wd = "一二三四五六日"[now_bj.weekday()]

    for rule in rules:
        if not (rule["tails"] & digits):
            continue                            # 该组关注的尾号今天不限行
        last = state.get(rule["key"], "")
        if last == key:
            continue                            # 本组本小时已发
        if not in_window and (last[:10] == today or h > 19):
            continue                            # 时段外：今天已发过 / 已过 19 点不补
        tag = "、".join(sorted(rule["tails"])) if multi else ""
        if _send_limit_email(now_bj, pair, label, rule["to"] or None, tag):
            state[rule["key"]] = key
            _limit_state_save(state)
            log.info("限行提醒已记录（%s 规则%s 尾号%s 命中 %s，email=OK 周%s）",
                     key, rule["key"], tag or "-", pair, wd)


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

    if "--test-limit" in args:
        now_bj = datetime.now(CST)
        pair = _limit_pair(now_bj.date()) or "5/0"
        rules = _limit_rules()
        multi = len(rules) > 1
        ok_all = True
        for rule in rules:
            tag = "、".join(sorted(rule["tails"])) if multi else ""
            ok = _send_limit_email(now_bj, pair, "测试邮件（模拟限行日）",
                                   rule["to"] or None, tag)
            log.info("限行提醒测试[规则%s 尾号%s]：%s",
                     rule["key"], tag or "-", "已发送" if ok else "失败")
            ok_all = ok_all and ok
        log.info("限行提醒测试邮件：%s", "全部成功" if ok_all else "有失败")
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
        limit_reminder_tick()
        log.info("单轮完成。")
        return

    log.info("进入常驻轮询（Ctrl+C 停止）…")
    while True:
        t0 = time.time()
        try:
            seen, initialized = one_round(seen, initialized, do_push=not no_push)
            if save_state(seen, initialized):
                log.info("状态已更新（%d 条记录）。", len(seen[-MAX_STATE:]))
            limit_reminder_tick()
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
