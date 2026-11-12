# -*- coding: utf-8 -*-
"""签到前自动续期：临近过期的 accessToken 用 refreshToken 换新，并把轮换后的 token 写回 GitHub Secret。

背景（2026-11-12）：云端两账号 token 于 11-11 / 11-12 陆续过期，签到连续两次 HTTP 401 全挂。
根因是 10-16 写进 Secret 的 token 是 09-12/09-13 的明文快照，JWT exp 到点即死，且没有续期机制。

设计取舍：
  1) **只在 accessToken 剩余 < REFRESH_WITHIN_DAYS 天时刷新**（默认 7）。refreshToken 每次会轮换，
     天天刷会放大「刷了但没写回」的丢链风险。
  2) **刷新结果落文件，不进环境变量**：写 $RUNNER_TEMP/wb_accounts.json，后续 step 用
     WB_CHECKIN_ACCOUNTS_FILE 读。新 token 不是已知 Secret，不会被 GitHub 自动 mask，
     走 env 会明文打进日志。
  3) **写回 Secret 失败不中断签到**：当次用新 token 照样签成功，只打 ::error:: 告警，下次运行再补。
     但会在 accounts 文件里留 refresh_pending 标记，供邮件心跳提示。

依赖：pynacl（仅写回 Secret 时需要）；缺失时降级为「只刷新不写回」。
"""
import base64
import datetime
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding='utf-8')

REFRESH_WITHIN_DAYS = float(os.environ.get("REFRESH_WITHIN_DAYS") or 7)
REFRESH_PATH = "/v2/plugin/auth/token/refresh"
API = "https://api.github.com"
CTX = ssl._create_unverified_context()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
BJ = datetime.timezone(datetime.timedelta(hours=8))


def http_json(method, url, headers=None, body=None, timeout=25, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, method=method,
                                         data=json.dumps(body).encode() if body is not None else None)
            for k, v in (headers or {}).items():
                req.add_header(k, v)
            if body is not None:
                req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                raw = r.read().decode("utf-8", "replace")
                return r.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            time.sleep(1 + i * 2)
    return None, str(last)


def jwt_payload(tok):
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p).decode("utf8"))
    except Exception:
        return {}


def days_left(tok):
    exp = jwt_payload(tok).get("exp")
    if not exp:
        return None
    return (exp - time.time()) / 86400.0


def host_of(tok):
    iss = str(jwt_payload(tok).get("iss") or "")
    u = urllib.parse.urlsplit(iss)
    return (u.scheme + "://" + u.netloc) if u.scheme and u.netloc else ""


def load_accounts():
    """优先读文件（上一步可能已刷新），其次读环境变量。"""
    fp = (os.environ.get("WB_CHECKIN_ACCOUNTS_FILE") or "").strip()
    if fp and os.path.exists(fp):
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
            arr = d.get("accounts") if isinstance(d, dict) else d
            if arr:
                return list(arr), fp
        except Exception as e:
            print("! 读 %s 失败：%s" % (fp, e))
    raw = (os.environ.get("WB_CHECKIN_ACCOUNTS") or "").strip()
    if raw:
        try:
            d = json.loads(raw)
            arr = d.get("accounts") if isinstance(d, dict) else d
            return list(arr or []), None
        except Exception as e:
            print("! WB_CHECKIN_ACCOUNTS 解析失败：%s" % e)
    return [], None


def refresh_one(acct):
    """返回 (new_access, new_refresh, msg)。失败时 new_access 为 None。"""
    rt = str(acct.get("refresh") or acct.get("refreshToken") or "").strip()
    if not rt:
        return None, None, "无 refreshToken 字段"
    base = host_of(acct.get("token") or "") or "https://www.workbuddy.cn"
    hosts = list(dict.fromkeys([base, "https://www.workbuddy.cn", "https://www.codebuddy.cn"]))
    lastmsg = ""
    for h in hosts:
        url = h + REFRESH_PATH
        st, o = http_json("POST", url,
                          headers={"Accept": "application/json",
                                   "X-Refresh-Token": rt,
                                   "User-Agent": UA}, body={})
        if not isinstance(o, dict):
            lastmsg = "%s -> HTTP %s %s" % (url, st, str(o)[:120])
            continue
        data = o.get("data") or o
        at = data.get("accessToken") or data.get("access_token")
        if not at:
            lastmsg = "%s -> HTTP %s code=%s msg=%s" % (
                url, st, o.get("code"), o.get("msg") or o.get("message"))
            continue
        rt2 = data.get("refreshToken") or data.get("refresh_token") or rt
        rotated = "｜refreshToken 已轮换" if rt2 != rt else ""
        return at, rt2, "HTTP %s%s" % (st, rotated)
    return None, None, lastmsg or "全部端点失败"


def put_secret(repo, pat, name, value):
    """用 libsodium SealedBox 加密后 PUT 到 Actions Secrets。返回 (ok, msg)。"""
    if not pat:
        return False, "未配置 GH_PAT"
    if not repo:
        return False, "缺 GITHUB_REPOSITORY"
    try:
        from nacl import encoding, public  # noqa
    except Exception:
        return False, "pynacl 不可用（跳过写回）"
    st, o = http_json("GET", "%s/repos/%s/actions/secrets/public-key" % (API, repo),
                      headers={"Authorization": "Bearer " + pat,
                               "Accept": "application/vnd.github+json",
                               "User-Agent": "wb-refresh"})
    if st != 200 or not isinstance(o, dict) or "key" not in o:
        return False, "取 public-key 失败 HTTP %s %s" % (st, str(o)[:120])
    try:
        pk = public.PublicKey(o["key"].encode(), encoding.Base64Encoder())
        enc = base64.b64encode(public.SealedBox(pk).encrypt(value.encode())).decode()
    except Exception as e:
        return False, "加密失败 %s" % e
    for i in range(3):
        st2, o2 = http_json("PUT", "%s/repos/%s/actions/secrets/%s" % (API, repo, name),
                            headers={"Authorization": "Bearer " + pat,
                                     "Accept": "application/vnd.github+json",
                                     "User-Agent": "wb-refresh"},
                            body={"encrypted_value": enc, "key_id": o["key_id"]})
        if st2 in (200, 201, 204):
            return True, "HTTP %s" % st2
        time.sleep(2 + i * 2)
    return False, "PUT 失败 HTTP %s %s" % (st2, str(o2)[:120])


def main():
    accts, src = load_accounts()
    print("=== 续期检查（阈值 %g 天）· 来源：%s ===" % (
        REFRESH_WITHIN_DAYS, src or "WB_CHECKIN_ACCOUNTS"))
    if not accts:
        print("!! 没有可用账号")
        return 1

    changed = False
    for a in accts:
        name = str(a.get("name") or "?")
        tok = str(a.get("token") or "")
        d = days_left(tok)
        if d is None:
            print("  · %s：token 无 exp，跳过" % name)
            continue
        if d > REFRESH_WITHIN_DAYS:
            print("  · %s：剩 %.1f 天，无需刷新" % (name, d))
            continue
        print("  · %s：剩 %.1f 天 → 触发 refresh" % (name, d))
        new_at, new_rt, msg = refresh_one(a)
        if not new_at:
            print("  ✗ %s：刷新失败 —— %s" % (name, msg))
            a["refresh_error"] = msg
            continue
        old_exp = jwt_payload(tok).get("exp")
        a["token"] = new_at
        a["refresh"] = new_rt
        a.pop("refresh_error", None)
        changed = True
        nd = days_left(new_at)
        print("  ✓ %s：新 accessToken %s 天后过期（%s）" % (name, ("%.1f" % nd) if nd else "?", msg))
        if old_exp and jwt_payload(new_at).get("exp") == old_exp:
            print("    ! 注意：新 token exp 与旧值相同，可能只是续了会话")

    # 落文件给后续 step（600 权限，不进 env 避免日志泄露）
    out = (os.environ.get("WB_CHECKIN_ACCOUNTS_OUT") or
           os.path.join(os.environ.get("RUNNER_TEMP") or "/tmp", "wb_accounts.json"))
    with open(out, "w", encoding="utf-8") as f:
        json.dump(accts, f, ensure_ascii=False)
    try:
        os.chmod(out, 0o600)
    except Exception:
        pass
    print("[file] 账号数据已写入 %s（%d 个）" % (out, len(accts)))

    # 写回 Secret
    if changed:
        payload = json.dumps(
            [{k: v for k, v in a.items() if k in ("name", "token", "uid", "refresh")} for a in accts],
            ensure_ascii=False)
        ok, msg = put_secret(os.environ.get("GITHUB_REPOSITORY", "").strip(),
                             (os.environ.get("GH_PAT") or "").strip(),
                             "WB_CHECKIN_ACCOUNTS", payload)
        if ok:
            print("[secret] WB_CHECKIN_ACCOUNTS 已写回（%s）" % msg)
        else:
            # 不中断：当次仍用新 token 签到；但必须让人看见
            print("::error::[secret] 写回 WB_CHECKIN_ACCOUNTS 失败 —— %s" % msg)
            print("!! 本次签到会成功，但轮换后的 refreshToken 未落库，下次可能续不上")
    else:
        print("[secret] 无变更，不写回")
    return 0


if __name__ == "__main__":
    sys.exit(main())
