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

    # 注意：这里必须用 status --porcelain 而不是 diff --quiet。
    # state.json 首次生成时是"未跟踪文件"，git diff 对它返回空差异，
    # 会导致首轮状态永远回写不上去（实测踩过这个坑）。
    [ -n "$(git status --porcelain -- state.json 2>/dev/null)" ] || return 0

    git add state.json
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

START=$(date +%s)
END=$((START + DURATION))
ROUND=0

echo "=================================================================="
echo "长跑接力开始  总时长 ${DURATION}s  轮询间隔 ${INTERVAL}s"
echo "仓库内回写状态：$([ "$IN_GIT" = "1" ] && echo 是 || echo 否)"
echo "起跑时间：$(date '+%F %T %Z')"
echo "=================================================================="

while :; do
    NOW=$(date +%s)
    [ "$NOW" -ge "$END" ] && break

    ROUND=$((ROUND + 1))
    echo "── 轮次 $ROUND  $(date '+%F %T') ──"

    # 单轮：抓取 -> 解析 -> 去重 -> 推送 -> 写状态
    "$PY" monitor.py --once || echo "  ! 本轮执行异常，继续下一轮"

    commit_state

    # 对齐到下一个整点间隔，避免抓取耗时逐轮累积成漂移
    TARGET=$((NOW + INTERVAL))
    ELAPSED=$(( $(date +%s) - NOW ))
    WAIT=$((TARGET - NOW - ELAPSED))
    [ "$WAIT" -gt 0 ] && sleep "$WAIT"
done

echo "=================================================================="
echo "接力区间结束（共 $ROUND 轮），收尾再抓一次"
"$PY" monitor.py --once >/dev/null 2>&1 || true
commit_state
echo "退出时间：$(date '+%F %T %Z')"
echo "=================================================================="
