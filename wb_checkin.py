# -*- coding: utf-8 -*-
"""
WorkBuddy 云端签到 + 积分快照 + 快到期提醒（GitHub Actions 版，零第三方依赖）
==========================================================================
支持 **多账号**。

用法:
  python3 wb_checkin.py             # 签到（对配置里的每个账号各签一次，幂等）
  python3 wb_checkin.py --credits   # 只抓积分+快到期，写 credits_block.txt（供邮件 step 读）

env（推荐，多账号）:
  WB_CHECKIN_ACCOUNTS   JSON 数组:
      [{"name":"例外.","token":"<accessToken>","uid":"<uid>"}, {"name":"破例.","token":"...","uid":"..."}]
      （也接受 {"accounts":[...]} 包裹形式）

env（回退，单账号，向后兼容旧配置）:
  WB_CHECKIN_TOKEN / WB_CHECKIN_UID / WB_CHECKIN_NAME(可选)

退出码:
  0   至少有一个账号签到成功 / 已签到 / 活动未开放（含“部分账号异常”）
  2   全部账号 token 过期失效（HTTP 401/403）
  1   全部账号失败（非 token 原因）/ 配置缺失

--credits 模式: 全部账号抓到积分返回 0，否则 1（不影响签到 job 结论）

产物:
  checkin_result.json   每个账号的签到结果（status/title/detail），供邮件 step 读
  credits_block.txt     每账号一段的积分快照 + 快到期明细
  credits_expire_flag.txt  "1|<合计清零分>" 或 "0|0"，供邮件主题警示用
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

RESULT_FILE = "checkin_result.json"
CREDITS_FILE = "credits_block.txt"
# 供 workflow 邮件 step 读取的到期汇总（供主题行判断）
EXPIRE_FLAG = "credits_expire_flag.txt"


# ------------------------------------------------------------------
# 账号配置
# ------------------------------------------------------------------

def load_accounts():
    """返回 [{"name","token","uid"}]。优先 WB_CHECKIN_ACCOUNTS(JSON)，否则回退单账号 env。"""
    out = []
    raw = (os.environ.get("WB_CHECKIN_ACCOUNTS") or "").strip()
    if raw:
        try:
            arr = json.loads(raw)
            if isinstance(arr, dict):
                arr = arr.get("accounts") or []
            for i, a in enumerate(arr or []):
                if not isinstance(a, dict):
                    continue
                t = str(a.get("token") or "").strip()
                u = str(a.get("uid") or "").strip()
                if t and u:
                    out.append({
                        "name": (str(a.get("name") or ("账号%d" % (i + 1))).strip() or ("账号%d" % (i + 1))),
                        "token": t,
                        "uid": u,
                    })
        except Exception as e:
            print("! WB_CHECKIN_ACCOUNTS 解析失败（回退单账号）:", e)
    if not out:
        t = (os.environ.get("WB_CHECKIN_TOKEN") or "").strip()
        u = (os.environ.get("WB_CHECKIN_UID") or "").strip()
        if t and u:
            out.append({
                "name": (os.environ.get("WB_CHECKIN_NAME") or "账号1").strip() or "账号1",
                "token": t,
                "uid": u,
            })
    return out


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


def write_result(overall, title, accounts):
    """把多账号签到结果落盘（JSON）给邮件 step 读。
    accounts: [{"name","uid","status","title","detail"}]，status ∈ ok/already/inactive/expired/failed"""
    data = {"overall": overall, "title": title, "accounts": accounts}
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    p = os.environ.get("GITHUB_OUTPUT")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write("status=%s\ntitle=%s\n" % (overall, title))


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
    """把单个账号的积分快照 + 到期明细拼成多行文本块。"""
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
    """--credits 模式：逐账号抓积分 → 拼成多段 credits_block.txt（+ 到期标记），并写 Step Summary。"""
    accounts = load_accounts()
    if not accounts:
        print("❌ 未配置账号，无法抓积分（缺 WB_CHECKIN_ACCOUNTS 或 WB_CHECKIN_TOKEN/UID）")
        with open(CREDITS_FILE, "w", encoding="utf-8") as f:
            f.write("【积分快照】\n⚠️ 缺少 Secrets（WB_CHECKIN_ACCOUNTS）\n")
        with open(EXPIRE_FLAG, "w", encoding="utf-8") as f:
            f.write("0|0")
        return 1

    all_ok = True
    all_items = []
    blocks = []
    for a in accounts:
        credits = fetch_credits(a["token"], a["uid"])
        if not credits.get("ok"):
            all_ok = False
        else:
            items, _total = expiring_pools(credits["pools"])
            all_items += items
        blocks.append("【账号 %s】\n%s" % (a["name"], build_credits_block(credits).rstrip()))

    text = "\n\n".join(blocks) + "\n"
    with open(CREDITS_FILE, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    summary("### 积分快照（%d 个账号）\n\n%s" % (len(accounts), text))
    total_clear = round(sum(x["remain"] for x in all_items), 2)
    with open(EXPIRE_FLAG, "w", encoding="utf-8") as f:
        f.write("%s|%s" % ("1" if all_items else "0", total_clear))
    return 0 if all_ok else 1


# ------------------------------------------------------------------
# 签到
# ------------------------------------------------------------------

def checkin_one(acct):
    """对单个账号签到，返回 (status, title, detail)。status ∈ ok/already/inactive/expired/failed"""
    token, uid = acct["token"], acct["uid"]
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
                    if res.get("already"):
                        print("  ☑️ 今日已签到（幂等跳过）")
                        return ("already", "☑️ 今日已签到", "响应：%s\n接口：%s" % (res.get("message"), pth))
                    print("  ✅ 签到成功")
                    return ("ok", "✅ 签到成功", "响应：%s\n接口：%s" % (res.get("message"), pth))
                if res.get("inactive"):
                    print("  ℹ️ 签到活动未开放（不算失败）")
                    return ("inactive", "ℹ️ 活动未开放", "响应：%s" % res.get("message"))
            else:
                print("  !", url, "-> 非JSON响应 HTTP", status)

    if tried_expired:
        return ("expired", "❌ token 已失效",
                "HTTP 401/403，接口拒绝。\n处理：本地重新提取该账号 accessToken 并更新 WB_CHECKIN_ACCOUNTS。")
    return ("failed", "❌ 全部端点失败", "最后响应：%s" % last_line)


def do_checkin():
    accounts = load_accounts()
    if not accounts:
        print("❌ 未配置账号（缺 WB_CHECKIN_ACCOUNTS 或 WB_CHECKIN_TOKEN/UID）")
        summary("### WorkBuddy 签到 ❌\n\n缺少 Secrets（WB_CHECKIN_ACCOUNTS）")
        write_result("failed", "❌ 配置缺失",
                     [{"name": "?", "uid": "", "status": "failed", "title": "❌ 配置缺失",
                       "detail": "缺少 WB_CHECKIN_ACCOUNTS 或 WB_CHECKIN_TOKEN/UID"}])
        return 1

    results = []
    for a in accounts:
        print("=== 账号 %s (uid %s) ===" % (a["name"], a["uid"][:8]))
        st, ti, de = checkin_one(a)
        results.append({"name": a["name"], "uid": a["uid"], "status": st, "title": ti, "detail": de})

    statuses = [r["status"] for r in results]
    if all(s == "expired" for s in statuses):
        overall, otitle = "expired", "❌ 全部账号 token 失效"
    elif all(s in ("expired", "failed") for s in statuses):
        overall, otitle = "failed", "❌ 全部账号失败"
    elif any(s in ("expired", "failed") for s in statuses):
        overall, otitle = "partial", "⚠️ 部分账号异常"
    elif any(s == "ok" for s in statuses):
        overall, otitle = "ok", "✅ 全部签到成功"
    else:
        overall, otitle = "already", "☑️ 今日已签到"

    write_result(overall, otitle, results)
    summary("### WorkBuddy 签到 %s\n\n%s" % (
        otitle, "\n".join("- **%s**：%s" % (r["name"], r["title"]) for r in results)))
    print("\n" + otitle)
    for r in results:
        print("  · %s：%s" % (r["name"], r["title"]))

    if overall == "expired":
        return 2
    if overall == "failed":
        return 1
    return 0


def main():
    if "--credits" in sys.argv[1:]:
        return do_credits_report()
    return do_checkin()


if __name__ == "__main__":
    sys.exit(main())
