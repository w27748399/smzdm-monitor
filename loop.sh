#!/usr/bin/env bash
# ============================================================================
# 信小兔爆料监控 · 长跑接力循环
# ============================================================================
# 为什么需要它：
#   GitHub Actions 的 schedule cron 最短只支持 5 分钟一次，而且高峰期还会
#   延迟 15~30 分钟 —— 靠 cron 本身做不到 1 分钟粒度。
#
#   所以改成"长跑接力"：让一个 job 起来之后自己每 60 秒跑一轮，跑 150 分钟
#   再退出；同时 workflow 里的 concurrency 组会让新触发的 job 在队列里排队，
#   前一个刚结束、后一个立刻接上 —— 宏观上就是持续不断的 1 分钟粒度。
#
# 用法：
#   bash loop.sh [总时长(秒)]        默认 9000 秒（2.5 小时）
#   POLL_INTERVAL=60 bash loop.sh
#
# 本机也能直接跑（Git Bash / WSL / Linux / macOS 均可），此时不会执行 git 回写。
# ============================================================================
set -uo pipefail

DURATION="${1:-9000}"
INTERVAL="${POLL_INTERVAL:-60}"
PY="${PYTHON:-python3}"

# ----------------------------------------------------------------------------
# 夜间停机窗口（北京时间）：默认 23:00 ~ 次日 08:00 不监控。
#   - 每轮开始前检查，命中窗口就优雅退出接力（job 提前收工，不硬睡占机时）
#   - 与 monitor.yml 的 cron 白天点火窗口（0-14 UTC = 北京 8:00-22:30）双保险
#   - 临时强制监控：ALLOW_NIGHT=1 bash loop.sh
# ----------------------------------------------------------------------------
NIGHT_START="${NIGHT_START:-23}" # 北京时间 >= 该小时 -> 停
NIGHT_END="${NIGHT_END:-7}"      # 北京时间 <  该小时 -> 不起（7点起：天津限行提醒首封 7 点要发）
ALLOW_NIGHT="${ALLOW_NIGHT:-0}"

is_night() {
    [ "$ALLOW_NIGHT" = "1" ] && return 1
    local h
    h=$(TZ='Asia/Shanghai' date +%H)
    [ "$h" -ge "$NIGHT_START" ] || [ "$h" -lt "$NIGHT_END" ]
}

# 只有在真正的 git 仓库里才做状态回写（本机测试时自动跳过）
IN_GIT=0
if git rev-parse --git-dir >/dev/null 2>&1; then
    IN_GIT=1
fi

BOT_NAME="github-actions[bot]"
BOT_MAIL="41898282+github-actions[bot]@users.noreply.github.com"

# 把去重状态回写仓库。只有 state.json 真的变了才提交（monitor.py 保证了
# 无新爆料时不写盘），所以提交频率 = 新爆料频率，不会刷屏。
commit_state() {
    [ "$IN_GIT" = "1" ] || return 0

    # ⚠️ 2026-10-15 踩坑修复：pathspec 不能写死 limit_state.json。
    # 它只有限行日整点提醒发出后才会生成；非限行日 runner 上根本不存在，
    # git status 对不存在的 pathspec 直接 fatal（stdout 为空）→ 本函数误判
    # "无变化"提前 return → state.json 永不回写 → 每根新棒 checkout 到旧
    # 状态，把已发过的爆料当新条目重发（10-15 上午线报连发两遍的根因）。
    local files="state.json"
    [ -f limit_state.json ] && files="state.json limit_state.json"

    [ -n "$(git status --porcelain -- $files 2>/dev/null)" ] || return 0

    git add $files
    git -c user.name="$BOT_NAME" -c user.email="$BOT_MAIL" \
        commit -q -m "chore(state): 更新去重状态 $(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
        || return 0

    local i
    for i in 1 2 3; do
        if git push -q 2>/dev/null; then
            echo "  ↑ 去重状态已回写仓库"
            return 0
        fi
        git pull --rebase --autostash -q 2>/dev/null || true
        sleep 2
    done
    echo "  ! 状态回写失败（本轮推送不受影响，下一轮会重试）"
}

# ----------------------------------------------------------------------------
# 接力棒自续：本棒跑完时用 PAT 调 API 点火下一棒。
# 为什么：GitHub schedule 高峰期会静默丢弃 cron 点火（官方文档承认
# "may be delayed or dropped"），实测 10-13 晚间 18:00-23:48 五个多小时
# 黑窗、10-14 上午 08:00-12:30 又黑 4.5 小时 —— */30 一天 36 发被丢大半。
# self-dispatch 走 API 不经过那个调度器，点火必达；cron 降级为兜底
# （仅在本棒被 runner 回收等意外死亡时才用得上）。
# ----------------------------------------------------------------------------
dispatch_relay() {
    [ -n "$RELAY_PAT" ] || { echo "  ! 无 RELAY_PAT，跳过自续（靠 cron 兜底）"; return 0; }
    local repo="${GITHUB_REPOSITORY:-w27748399/smzdm-monitor}"
    local i code ok=0
    for i in 1 2 3; do
        code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
            -H "Authorization: Bearer $RELAY_PAT" \
            -H "Accept: application/vnd.github+json" \
            "https://api.github.com/repos/$repo/actions/workflows/monitor.yml/dispatches" \
            -d '{"ref":"main"}') || code=000
        if [ "$code" = "204" ]; then
            echo "  → 已亲手点火下一棒接力（self-dispatch 204）"
            ok=1
            break
        fi
        echo "  ! 续接力尝试 $i 失败（HTTP $code），重试..."
        sleep 3
    done
    [ "$ok" = "1" ] || echo "  ! 自续失败，靠 cron 兜底"
}

START=$(date +%s)
END=$((START + DURATION))
ROUND=0

echo "=================================================================="
echo "长跑接力开始  总时长 ${DURATION}s  轮询间隔 ${INTERVAL}s"
echo "仓库内回写状态：$([ "$IN_GIT" = "1" ] && echo 是 || echo 否)"
echo "起跑时间：$(date '+%F %T %Z')"
echo "=================================================================="

while :; do
    # 夜间停机：命中窗口就收工退出（下一次白天 cron 点火自然接上）
    if is_night; then
        echo ""
        echo "=================================================================="
        echo "🌙 夜间停机：北京时间 $(TZ='Asia/Shanghai' date '+%F %T')，"
        echo "   停机窗口 ${NIGHT_START}:00 ~ 次日 ${NIGHT_END}:00（北京时间）不监控。"
        echo "   本接力共完成 $ROUND 轮，提前收工。"
        echo "=================================================================="
        break
    fi

    NOW=$(date +%s)
    [ "$NOW" -ge "$END" ] && break

    ROUND=$((ROUND + 1))
    echo "── 轮次 $ROUND  $(date '+%F %T') ──"

    # 单轮：抓取 -> 解析 -> 去重 -> 推送 -> 写状态
    "$PY" monitor.py --once || echo "  ! 本轮执行异常，继续下一轮"

    commit_state

    # 对齐到下一个整点间隔，避免抓取耗时逐轮累积成漂移；
    # 再加 0~44s 随机抖动，打散固定节拍的机器人特征（smzdm 风控取证教训）
    TARGET=$((NOW + INTERVAL))
    ELAPSED=$(( $(date +%s) - NOW ))
    WAIT=$((TARGET - NOW - ELAPSED))
    WAIT=$((WAIT + RANDOM % 45))
    [ "$WAIT" -gt 0 ] && sleep "$WAIT"
done

echo "=================================================================="
# 收尾补抓只在白天做；夜间退出就不补（不违反停机窗口）
if is_night; then
    echo "接力区间结束（共 $ROUND 轮），夜间停机不补抓"
    echo "🌙 夜间停机，不续接力（明早 ${NIGHT_END}:00 由 cron 恢复）"
else
    echo "接力区间结束（共 $ROUND 轮），收尾再抓一次"
    "$PY" monitor.py --once >/dev/null 2>&1 || true
    commit_state
    # 亲手点火下一棒（夜间不点）
    dispatch_relay
fi
echo "退出时间：$(TZ='Asia/Shanghai' date '+%F %T') 北京时间 / $(date '+%F %T %Z')"
echo "=================================================================="
