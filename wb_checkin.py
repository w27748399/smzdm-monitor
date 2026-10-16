# -*- coding: utf-8 -*-
"""
WorkBuddy 云端签到 + 积分快照 + 快到期提醒（GitHub Actions 版，零第三方依赖）
==========================================================================
用法:
  python3 wb_checkin.py             # 签到（原行为不变）
  python3 wb_checkin.py --credits   # 只抓积分+快到期，写 credits_block.txt（供邮件 step 读）

env:
  WB_CHECKIN_TOKEN  当前号 accessToken（Secrets）
  WB_CHECKIN_UID    当前号 uid（Secrets）

退出码:
  0   签到成功 / 已签到 / 活动未开放（不算失败）
  2   HTTP 401/403 —— token 过期或无效，触发 workflow 邮件告警
  1   其他失败

--credits 模式: 抓到积分返回 0，抓不到返回 1（不影响签到 job 结论）
"""
import os
import sys
import json
import time
import base64
import ssl
import re
import http.client
import urllib.parse

sys.stdout.reconfigure(encoding="utf-8")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
CHECKIN_PATHS = ["/billing/meter/daily-checkin", "/v2/billing/meter/daily-checkin"]
CREDITS_PATH = "/v2/billing/meter/get-user-resource"
FALLBACK_HOSTS = ["https://www.workbuddy.cn", "https://www.codebuddy.cn"]

EXPIRING_WINDOW_DAYS = 7

INACTIVE_RE = re.compile(r"未开启|未开始|未开放|已过期|无.*活动|活动.*(结束|关闭|暂停)", re.I)
ALREADY_RE = re.compile(r"已签到|已领取|已经.*(签到|领取)|重复签到|already\s*(checked[- ]?in|claimed)|already", re.I)


def token_issuer_origin(token):
    try:
        part = token.split(".")[1]
        padded = part.replace("-", "+").replace("_", "/") + "=" * ((4 - len(part) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf8"))
        u = urllib.parse.urlsplit(str(payload.get("iss", "")))
        return u.scheme + "://" + u.netloc
    except Exception:
        return None


def http_post_json(url, headers, body_obj, timeout=20):
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc
    path = parsed.path + ("?" + parsed.query if parsed.query else "")
    body = json.dumps(body_obj)
    req_headers = dict(headers)
    req_headers["Content-Type"] = "application/json"
    req_headers["Content-Length"] = str(len(body.encode("utf-8")))
    conn = http.client.HTTPSConnection(host, timeout=timeout, context=ssl.create_default_context())
    try:
        conn.request("POST", path, body=body.encode("utf-8"), headers=req_headers)
        resp = conn.getresponse()
        status = resp.status
        text = resp.read().decode("utf-8", "replace")
    finally:
        conn.close()
    try:
        return status, json.loads(text)
    except Exception:
        return status, text


def classify(http_ok, code, message):
    try:
        code = int(code)
    except Exception:
        code = None
    text = str(message or "")
    inactive = bool(INACTIVE_RE.search(text))
    already = (code == 10001) and (not inactive) and bool(ALREADY_RE.search(text))
    ok = (not inactive) and ((code == 0 and http_ok) or already)
    return {"ok": ok, "already": already, "inactive": inactive, "code": code, "message": text}


def summary(line):
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")


RESULT_FILE = "checkin_result.txt"
CREDITS_FILE = "credits_block.txt"
# 供 workflow 邮件 step 读取的到期汇总（供主题行判断）
EXPIRE_FLAG = "credits_expire_flag.txt"


def write_result(status, title, detail):
    """把签到结果落盘给邮件 step 读（status: ok/already/inactive/expired/failed）"""
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        f.write("status=%s\ntitle=%s\ndetail=%s\n" % (status, title, detail))
    p = os.environ.get("GITHUB_OUTPUT")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write("status=%s\ntitle=%s\n" % (status, title))


# ------------------------------------------------------------------
# 积分快照 + 快到期提醒
# ------------------------------------------------------------------

def _parse_endtime(v):
    """CycleEndTime/EndTime 可能是毫秒(或秒)时间戳(int/str)，或 'YYYY-MM-DD HH:MM:SS' 字符串。返回 epoch 秒。"""
    if v in (None, ""):
        return None
    try:
        f = float(v)
        return f / 1000 if f > 1e12 else f
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(str(v).strip(), fmt))
        except Exception:
            continue
    return None


def fetch_credits(token, uid):
    """调 get-user-resource。返回 dict: {ok, pool_sum, server_total, pool_count, pools, err}"""
    hosts = []
    iss = token_issuer_origin(token)
    if iss:
        hosts.append(iss)
    hosts += FALLBACK_HOSTS
    last = "无可用端点"
    for host in dict.fromkeys(hosts):
        url = host + CREDITS_PATH
        o = urllib.parse.urlsplit(url)
        origin = o.scheme + "://" + o.netloc
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": origin,
            "Referer": origin + "/profile/plans-usage",
            "Authorization": "Bearer " + token,
            "X-Client-Platform": "web",
            "X-User-Id": uid,
            "User-Agent": UA,
        }
        try:
            status, obj = http_post_json(url, headers, {}, timeout=20)
        except Exception as e:
            last = "请求异常: %s" % e
            continue
        if status in (401, 403):
            last = "HTTP %s token 失效" % status
            continue
        if not isinstance(obj, dict):
            last = "HTTP %s 非JSON" % status
            continue
        if obj.get("code") not in (0, None):
            last = obj.get("msg") or ("code=%s" % obj.get("code"))
            continue
        data = (((obj.get("data") or {}).get("Response") or {}).get("Data")
                or (((obj.get("data") or {}).get("data") or {}).get("Response", {}).get("Data"))
                or {})
        pools = data.get("Accounts") or []
        pool_sum = 0.0
        for a in pools:
            for key in ("CycleCapacityRemainPrecise", "CycleCapacityRemain",
                        "CapacityRemainPrecise", "CapacityRemain"):
                if a.get(key) not in (None, ""):
                    try:
                        pool_sum += float(a.get(key))
                        break
                    except Exception:
                        pass
        return {"ok": True, "pool_sum": round(pool_sum, 2),
                "server_total": data.get("TotalDosage"),
                "pool_count": len(pools), "pools": pools, "err": None}
    return {"ok": False, "err": last}


def expiring_pools(pools, days=EXPIRING_WINDOW_DAYS):
    """7 天内到期且有余额的有效池，按到期日升序。返回 (items, total_clear)。"""
    now_s = time.time()
    items = []
    for a in pools:
        remain = None
        for key in ("CycleCapacityRemainPrecise", "CycleCapacityRemain",
                    "CapacityRemainPrecise", "CapacityRemain"):
            if a.get(key) not in (None, ""):
                try:
                    remain = float(a.get(key))
                    break
                except Exception:
                    pass
        if remain is None or remain <= 0:
            continue
        if a.get("Status") not in (0, "0", None):
            continue
        end_s = _parse_endtime(a.get("CycleEndTime") or a.get("EndTime"))
        if end_s is None:
            continue
        left = (end_s - now_s) / 86400.0
        if left > days:
            continue
        items.append({
            "end_str": time.strftime("%Y-%m-%d", time.localtime(end_s)),
            "left_days": max(0, round(left, 1)),
            "remain": round(remain, 2),
            "name": (a.get("PackageName") or a.get("ProductName")
                     or a.get("SubProductName") or a.get("DealName") or "?"),
        })
    items.sort(key=lambda x: x["end_str"])
    return items, round(sum(x["remain"] for x in items), 2)


def build_credits_block(credits):
    """把积分快照 + 到期明细拼成多行文本块（供邮件正文 / Step Summary 使用）。"""
    if not credits.get("ok"):
        return "【积分快照】\n⚠️ 获取失败：%s\n" % credits.get("err")
    lines = ["【积分快照】"]
    lines.append("· 池加总：%s（%d 个资源池）" % (credits["pool_sum"], credits["pool_count"]))
    lines.append("· 服务端总额 / UI 口径：%s" % credits.get("server_total"))
    items, total_clear = expiring_pools(credits["pools"])
    if items:
        lines.append("")
        lines.append("⚠️ 快到期积分（%d 天内到期将清零 %s 分）：" % (EXPIRING_WINDOW_DAYS, total_clear))
        for it in items:
            lines.append("  · %s（剩 %s 天）清零 %s · %s" % (
                it["end_str"], it["left_days"], it["remain"], it["name"]))
    else:
        lines.append("")
        lines.append("✅ 快到期积分：无到期风险（%d 天内无到期且有余额的池）" % EXPIRING_WINDOW_DAYS)
    return "\n".join(lines) + "\n"


def do_credits_report():
    """--credits 模式：抓积分 → 写 credits_block.txt（+ 到期标记文件），并写 Step Summary。"""
    token = (os.environ.get("WB_CHECKIN_TOKEN") or "").strip()
    uid = (os.environ.get("WB_CHECKIN_UID") or "").strip()
    if not token or not uid:
        print("❌ 缺少 WB_CHECKIN_TOKEN / WB_CHECKIN_UID，无法抓积分")
        with open(CREDITS_FILE, "w", encoding="utf-8") as f:
            f.write("【积分快照】\n⚠️ 缺少 Secrets（WB_CHECKIN_TOKEN / WB_CHECKIN_UID）\n")
        return 1
    credits = fetch_credits(token, uid)
    block = build_credits_block(credits)
    with open(CREDITS_FILE, "w", encoding="utf-8") as f:
        f.write(block)
    print(block)
    summary("### " + block.replace("\n", "\n"))
    # 到期标记：有到期则写 "1"，否则 "0"（供邮件主题用）
    try:
        items, total_clear = expiring_pools(credits["pools"]) if credits.get("ok") else ([], 0)
        with open(EXPIRE_FLAG, "w", encoding="utf-8") as f:
            f.write("%s|%s" % ("1" if items else "0", total_clear))
    except Exception:
        with open(EXPIRE_FLAG, "w", encoding="utf-8") as f:
            f.write("0|0")
    return 0 if credits.get("ok") else 1


# ------------------------------------------------------------------
# 签到
# ------------------------------------------------------------------

def do_checkin():
    token = (os.environ.get("WB_CHECKIN_TOKEN") or "").strip()
    uid = (os.environ.get("WB_CHECKIN_UID") or "").strip()
    if not token or not uid:
        print("❌ 缺少 WB_CHECKIN_TOKEN / WB_CHECKIN_UID 环境变量")
        summary("### WorkBuddy 签到 ❌\n\n缺少 Secrets（WB_CHECKIN_TOKEN / WB_CHECKIN_UID）")
        write_result("failed", "❌ 配置缺失", "缺少 Secrets（WB_CHECKIN_TOKEN / WB_CHECKIN_UID）")
        return 1

    hosts = []
    iss = token_issuer_origin(token)
    if iss:
        hosts.append(iss)
    hosts += FALLBACK_HOSTS

    tried_expired = False
    last_line = ""
    for host in dict.fromkeys(hosts):
        for pth in CHECKIN_PATHS:
            url = host + pth
            o = urllib.parse.urlsplit(url)
            origin = o.scheme + "://" + o.netloc
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Origin": origin,
                "Referer": origin + "/profile/plans-usage",
                "Authorization": "Bearer " + token,
                "X-Client-Platform": "web",
                "X-User-Id": uid,
                "X-Domain": "",
                "User-Agent": UA,
            }
            try:
                status, obj = http_post_json(url, headers, {}, timeout=20)
            except Exception as e:
                print("  !", url, "->", e)
                continue
            if status in (401, 403):
                tried_expired = True
                print("  !", url, "-> HTTP", status, "(token 过期或无效)")
                continue
            if isinstance(obj, dict):
                msg = obj.get("msg") or obj.get("message") or ("ok" if status < 400 else "HTTP %s" % status)
                res = classify(status < 400, obj.get("code"), msg)
                last_line = "%s -> %s" % (url, res.get("message"))
                print(" ", last_line)
                if res.get("ok"):
                    tag = "✅ 签到成功" if not res.get("already") else "☑️ 今日已签到（幂等跳过）"
                    print(tag)
                    summary("### WorkBuddy 签到 %s\n\n- 账号 uid: `%s`\n- 接口: `%s`\n- 响应: %s" % (
                        tag, uid, pth, res.get("message")))
                    if res.get("already"):
                        write_result("already", "☑️ 今日已签到", "响应：%s\n接口：%s" % (res.get("message"), pth))
                    else:
                        write_result("ok", "✅ 签到成功", "响应：%s\n接口：%s" % (res.get("message"), pth))
                    return 0
                if res.get("inactive"):
                    print("ℹ️ 签到活动未开放（不算失败）")
                    summary("### WorkBuddy 签到 ℹ️ 活动未开放\n\n- 响应: %s" % res.get("message"))
                    write_result("inactive", "ℹ️ 活动未开放", "响应：%s" % res.get("message"))
                    return 0
            else:
                print("  !", url, "-> 非JSON响应 HTTP", status)

    if tried_expired:
        print("❌ token 已过期或无效 —— 需本地重新提取并更新 Secrets")
        summary("### WorkBuddy 签到 ❌ token 失效\n\n请本地运行 memscan_sign.py 重新提取 token 并更新仓库 Secrets。")
        write_result("expired", "❌ token 已失效", "HTTP 401/403，接口拒绝。\n处理：本地运行 memscan_sign.py 重新提取 token 并更新仓库 Secrets。")
        return 2
    print("❌ 所有签到端点都未成功", last_line)
    summary("### WorkBuddy 签到 ❌ 全部端点失败\n\n- 最后: %s" % last_line)
    write_result("failed", "❌ 全部端点失败", "最后响应：%s" % last_line)
    return 1


def main():
    if "--credits" in sys.argv[1:]:
        return do_credits_report()
    return do_checkin()


if __name__ == "__main__":
    sys.exit(main())
