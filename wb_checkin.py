# -*- coding: utf-8 -*-
"""
WorkBuddy 云端签到 + 连登打卡 + 积分快照 + 快到期提醒（GitHub Actions 版，零第三方依赖）
==========================================================================
支持 **多账号**。

v3（2026-10-20）：新增「连登打卡」活跃上报。
  背景：签到（daily-checkin）只领积分；活跃地图连登要求当天**完成一次对话**。
  本版签到后逐账号做活跃上报（移植自本地 wb_switch.py do_activate，改写为零依赖）：
    1) GET /activity/growth/heatmap 查今日 is_active，已活跃则跳过（幂等，不耗积分）
    2) POST /console/as/conversations/ 建最小会话（prompt=你好, model=deepseek-r1）
    3) ACP SSE 握手 + session/prompt 发送一句 → 当天被记活跃
  活跃失败不影响签到结论与退出码（与本地 all 命令同口径）。

用法:
  python3 wb_checkin.py             # 签到 + 连登打卡（对配置里每个账号各一次，幂等）
  python3 wb_checkin.py --credits   # 只抓积分+快到期，写 credits_block.txt（供邮件 step 读）
  WB_CHECKIN_ACTIVITY=0             # 关闭连登打卡（只签到）

env（推荐，多账号）:
  WB_CHECKIN_ACCOUNTS   JSON 数组:
      [{"name":"例外.","token":"<accessToken>","uid":"<uid>"}, {"name":"破例.","token":"...","uid":"..."}]
      （也接受 {"accounts":[...]} 包裹形式）

env（回退，单账号，向后兼容旧配置）:
  WB_CHECKIN_TOKEN / WB_CHECKIN_UID / WB_CHECKIN_NAME(可选)

退出码:
  0   至少有一个账号签到成功 / 已签到 / 活动未开放（含"部分账号异常"）
  2   全部账号 token 过期失效（HTTP 401/403）
  1   全部账号失败（非 token 原因）/ 配置缺失

--credits 模式: 全部账号抓到积分返回 0，否则 1（不影响签到 job 结论）

产物:
  checkin_result.json   每个账号的签到+连登结果（status/title/detail/activity），供邮件 step 读
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

# 连登打卡开关（默认开）
ACTIVITY_ENABLED = (os.environ.get("WB_CHECKIN_ACTIVITY") or "1").strip().lower() not in ("0", "false", "no")
ACP_TIMEOUT = 60


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
    accounts: [{"name","uid","status","title","detail","activity"}]，status ∈ ok/already/inactive/expired/failed"""
    data = {"overall": overall, "title": title, "accounts": accounts}
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    p = os.environ.get("GITHUB_OUTPUT")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write("status=%s\ntitle=%s\n" % (overall, title))


# ------------------------------------------------------------------
# 连登打卡（活跃上报）—— 完成一次对话才算当天活跃，移植自本地 do_activate
# ------------------------------------------------------------------

def _host_list(token):
    hosts = []
    iss = token_issuer_origin(token)
    if iss:
        hosts.append(iss)
    hosts += FALLBACK_HOSTS
    return list(dict.fromkeys(hosts))


def _web_headers(token, uid, origin, referer_path="/profile/growth-center"):
    return {
        "Accept": "application/json, text/plain, */*",
        "Origin": origin,
        "Referer": origin + referer_path,
        "Authorization": "Bearer " + token,
        "X-Client-Platform": "web",
        "X-User-Id": uid,
        "User-Agent": UA,
    }


def _http_get(url, headers, timeout=20):
    parsed = urllib.parse.urlsplit(url)
    conn = http.client.HTTPSConnection(parsed.netloc, timeout=timeout,
                                       context=ssl.create_default_context())
    try:
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


def heatmap_today(host, token, uid):
    """查成长中心今日活跃。返回 today dict（含 is_active）或 None。"""
    url = host + "/activity/growth/heatmap"
    o = urllib.parse.urlsplit(url)
    headers = _web_headers(token, uid, o.scheme + "://" + o.netloc)
    try:
        st, obj = _http_get(url, headers)
    except Exception:
        return None
    if st != 200 or not isinstance(obj, dict):
        return None
    return (obj.get("data") or {}).get("today") or None


def _acp_fire(endpoint, runtime_token, sess_id):
    """ACP 握手：SSE GET 拿 connection-id → initialize → session/prompt（发一句即算对话）→ DELETE 收尾。"""
    p = urllib.parse.urlsplit(endpoint)
    host, path = p.netloc, p.path or "/"
    base_hdr = {
        "Origin": "https://" + host,
        "Referer": "https://" + host + "/",
        "Authorization": "Bearer " + runtime_token,
        "User-Agent": UA,
    }
    # 1) SSE 长连接（保持打开，让 connection-id 生效）
    c1 = http.client.HTTPSConnection(host, timeout=ACP_TIMEOUT, context=ssl.create_default_context())
    hdr1 = dict(base_hdr)
    hdr1["Accept"] = "text/event-stream"
    c1.request("GET", path, headers=hdr1)
    r1 = c1.getresponse()
    if r1.status != 200:
        c1.close()
        raise RuntimeError("ACP SSE HTTP %s" % r1.status)
    conn_id = r1.getheader("acp-connection-id") or ""

    rpc_n = [0]

    def rpc(method, params, timeout=ACP_TIMEOUT):
        rpc_n[0] += 1
        body = json.dumps({"jsonrpc": "2.0", "id": rpc_n[0], "method": method, "params": params})
        h2 = dict(base_hdr)
        h2.update({
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Content-Length": str(len(body.encode("utf-8"))),
        })
        if conn_id:
            h2["acp-connection-id"] = conn_id
        c2 = http.client.HTTPSConnection(host, timeout=timeout, context=ssl.create_default_context())
        try:
            c2.request("POST", path, body=body.encode("utf-8"), headers=h2)
            r2 = c2.getresponse()
            buf = b""
            try:
                while True:
                    chunk = r2.read(1024)
                    if not chunk:
                        break
                    buf += chunk
                    if b'"result"' in buf or b'"error"' in buf:
                        break
            except Exception:
                pass
            return buf.decode("utf-8", "replace")[:400]
        finally:
            c2.close()

    try:
        rpc("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "wb-checkin", "version": "3.0"},
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
        })
        out = rpc("session/prompt", {"sessionId": sess_id,
                                     "prompt": [{"type": "text", "text": "你好"}]})
    finally:
        # 收尾：DELETE 会话 + 关 SSE（都 best-effort）
        try:
            c3 = http.client.HTTPSConnection(host, timeout=10, context=ssl.create_default_context())
            c3.request("DELETE", path, headers=base_hdr)
            c3.getresponse().read()
            c3.close()
        except Exception:
            pass
        try:
            c1.close()
        except Exception:
            pass
    return out


def activity_one(acct):
    """连登打卡：查 heatmap → 已活跃跳过 → 否则建最小会话上报。
    返回 (status, title, detail)。status ∈ ok/already/failed"""
    if not ACTIVITY_ENABLED:
        return ("ok", "🔥 连登打卡：未启用（WB_CHECKIN_ACTIVITY=0）", "")
    token, uid = acct["token"], acct["uid"]
    last_err = ""
    for host in _host_list(token):
        # 1) 今日已活跃 → 跳过（幂等，不耗积分）
        today = None
        try:
            today = heatmap_today(host, token, uid)
        except Exception as e:
            last_err = "heatmap 异常: %s" % e
        if isinstance(today, dict) and today.get("is_active") is True:
            return ("already", "🔥 连登打卡 ✅（今日已活跃，跳过）", "")
        # 2) 建最小会话
        o = urllib.parse.urlsplit(host)
        origin = o.scheme + "://" + o.netloc
        headers = _web_headers(token, uid, origin)
        headers["X-CodeBuddy-Request"] = "1"
        try:
            st, created = http_post_json(origin + "/console/as/conversations/",
                                         headers, {"prompt": "你好", "model": "deepseek-r1", "plugins": []},
                                         timeout=30)
        except Exception as e:
            last_err = "建会话异常: %s" % e
            continue
        if st in (401, 403):
            return ("failed", "🔥 连登打卡 ❌：token 失效（HTTP %s）" % st, "活跃上报被 401/403 拒绝")
        if not isinstance(created, dict):
            last_err = "建会话非JSON HTTP %s" % st
            continue
        data = created.get("data") or {}
        conv = (created.get("id") or created.get("conversationId")
                or data.get("id") or data.get("conversationId")
                or (created.get("info") or {}).get("id"))
        if not conv:
            last_err = "建会话失败: %s %s" % (st, str(created)[:160])
            continue
        session = created.get("session") or data.get("session")
        if not session or not session.get("token"):
            try:
                st2, txt2 = _http_get(origin + "/console/as/conversations/%s/session" % conv, headers)
                obj2 = json.loads(txt2) if txt2 else {}
            except Exception as e:
                last_err = "取 session 异常: %s" % e
                continue
            d2 = (obj2.get("data") or {}) if isinstance(obj2, dict) else {}
            session = (obj2.get("session") if isinstance(obj2, dict) else None) \
                or d2.get("session") or d2 or {}
        link = session.get("link") or session.get("endpoint") or session.get("url") or ""
        runtime_token = session.get("token") or session.get("accessToken") or session.get("access_token") or ""
        sess_id = session.get("sessionId") or session.get("session_id") or session.get("id") or conv
        if not link or not runtime_token:
            last_err = "会话响应缺 ACP 凭证: %s" % json.dumps(session, ensure_ascii=False)[:160]
            continue
        endpoint = link.replace("http://", "https://").rstrip("/")
        if "acp" not in endpoint:
            endpoint = endpoint.rstrip("/") + "/api/v1/acp"
        # 3) ACP 发一句
        try:
            _acp_fire(endpoint, runtime_token, sess_id)
        except Exception as e:
            last_err = "ACP 异常: %s" % e
            continue
        return ("ok", "🔥 连登打卡 ✅（最小会话已发起）", "会话 %s… 已上报" % str(conv)[:8])
    return ("failed", "🔥 连登打卡 ❌：%s" % (last_err or "全部端点失败"), last_err)


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
    hosts = _host_list(token)
    last = "无可用端点"
    for host in hosts:
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
    hosts = _host_list(token)

    tried_expired = False
    last_line = ""
    for host in hosts:
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
        # 连登打卡（紧跟签到；失败不影响签到结论）
        ast, ati, ade = activity_one(a)
        print("  ", ati)
        results.append({"name": a["name"], "uid": a["uid"], "status": st,
                        "title": "%s · %s" % (ti, ati), "detail": de,
                        "activity": ast, "activity_title": ati,
                        "activity_detail": ade})

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

    act_all_ok = all(r.get("activity") in ("ok", "already") for r in results)
    if ACTIVITY_ENABLED:
        otitle = otitle + (" · 连登✅" if act_all_ok else " · 连登❌")

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
