# -*- coding: utf-8 -*-
"""
WorkBuddy 云端签到（GitHub Actions 版，零第三方依赖）
=====================================================
env:
  WB_CHECKIN_TOKEN  当前号 accessToken（Secrets）
  WB_CHECKIN_UID    当前号 uid（Secrets）

退出码:
  0   签到成功 / 已签到 / 活动未开放（不算失败）
  2   HTTP 401/403 —— token 过期或无效，触发 workflow 邮件告警
  1   其他失败
"""
import os
import sys
import json
import base64
import ssl
import re
import http.client
import urllib.parse

sys.stdout.reconfigure(encoding="utf-8")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
CHECKIN_PATHS = ["/billing/meter/daily-checkin", "/v2/billing/meter/daily-checkin"]
FALLBACK_HOSTS = ["https://www.workbuddy.cn", "https://www.codebuddy.cn"]

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


def main():
    token = (os.environ.get("WB_CHECKIN_TOKEN") or "").strip()
    uid = (os.environ.get("WB_CHECKIN_UID") or "").strip()
    if not token or not uid:
        print("❌ 缺少 WB_CHECKIN_TOKEN / WB_CHECKIN_UID 环境变量")
        summary("### WorkBuddy 签到 ❌\n\n缺少 Secrets（WB_CHECKIN_TOKEN / WB_CHECKIN_UID）")
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
                    return 0
                if res.get("inactive"):
                    print("ℹ️ 签到活动未开放（不算失败）")
                    summary("### WorkBuddy 签到 ℹ️ 活动未开放\n\n- 响应: %s" % res.get("message"))
                    return 0
            else:
                print("  !", url, "-> 非JSON响应 HTTP", status)

    if tried_expired:
        print("❌ token 已过期或无效 —— 需本地重新提取并更新 Secrets")
        summary("### WorkBuddy 签到 ❌ token 失效\n\n请本地运行 memscan_sign.py 重新提取 token 并更新仓库 Secrets。")
        return 2
    print("❌ 所有签到端点都未成功", last_line)
    summary("### WorkBuddy 签到 ❌ 全部端点失败\n\n- 最后: %s" % last_line)
    return 1


if __name__ == "__main__":
    sys.exit(main())
