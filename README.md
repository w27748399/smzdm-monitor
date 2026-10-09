# 信小兔爆料监控（原「信小兔监控5.exe」的替代实现）

监控**什么值得买**用户「信小兔」的爆料页，有新爆料就推送到手机。

原程序 `信小兔监控5.exe` 是**易语言编译的 Windows GUI 程序**：无源码、只能跑在 Windows、无法上云、还带杀软误报。本脚本用 Python 重写同一件事，一份代码可跑在 **GitHub Actions（免费·1分钟） / 本机 / Docker / Koyeb / 任意 VPS**。

---

## 一、它做什么

```
定时抓取 https://zhiyou.smzdm.com/member/9687682701/baoliao/
      ↓  正则解析（沿用原 EXE 的正则，实测仍然有效）
   商品 ID + 标题 + 价格
      ↓  与 state.json 比对去重
    新爆料  →  推送到微信 / Bark / Telegram / Webhook
```

- **首轮只建基线不推送**，避免刚启动时把历史 10 条全推给你
- **状态持久化** 在 `state.json`，重启不会重复推送
- 目标页**无需登录、公开可访问**，所以不需要任何 Cookie（原 EXE 把登录 Cookie 硬编码在程序里，纯属多余且有泄露风险）

---

## 二、推荐方案：GitHub Actions（免费 · 1 分钟粒度 · 电脑不用开）

这是**零成本拿到 1 分钟粒度**的方案。但有个技术障碍要先讲清楚：

### 2.1 为什么不能直接写 `cron: '* * * * *'`

GitHub 官方限制：

| 限制 | 实际值 |
|---|---|
| schedule 最短间隔 | **5 分钟**（写 `* * * * *` 会被静默忽略） |
| 实际触发延迟 | 高峰期 **15~30 分钟** |

所以靠 cron 本身**根本做不到 1 分钟**。本方案改用「**长跑接力**」：

```
cron 每 2 小时只负责"点名"启动一个 job
      ↓
job 起来后自己连续跑 5 小时，每 60 秒抓一轮（loop.sh）
      ↓
concurrency 组让后续触发的 job 在队列里排队，而不是并发
      ↓
前一个刚结束 → 后一个立刻接上 → 宏观粒度稳定在 1 分钟
```

```
时间轴（示意）
t=0h  ┌─── Job A 运行中 ───────────────┐(跑5小时)
t=2h  │        Job B 排队等待          │
t=4h  │        Job C 排队等待          │
t=5h  └───────────────────────────────┘
      ┌─── Job C 接上 ────────────────┐
      ↓                               ↓
      全程连续，每分钟一轮，无空档
```

**为什么必须公开仓库**：这个方案 7×24 小时都在跑 ≈ 1,440 分钟/天 ≈ 43,200 分钟/月。
- ✅ **公开仓库**：Actions 分钟数**无限免费**
- ❌ **私有仓库**：免费仅 2,000 分钟/月 → 连 2 天都撑不住

代码公开没有风险：推送密钥走 GitHub Secrets，仓库里只有公开的页面地址。

### 2.2 部署步骤

**① 建仓库并上传**

新建一个**公开（Public）**仓库，把本目录下**所有文件**（含隐藏的 `.github/` 目录）推到**仓库根目录**：

```bash
cd smzdm-monitor
git init
git add -A
git commit -m "init"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

> ⚠️ `config.json` 已在 `.gitignore` 里，不会被推上去。**千万别把它提交**。

**② 配置推送密钥**

仓库 → `Settings` → `Secrets and variables` → `Actions` → `New repository secret`，按第五节的表添加，例如企业微信机器人：

| Name | Secret |
|---|---|
| `PUSH_CHANNEL` | `wecom` |
| `WECOM_WEBHOOK` | `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx` |

**③ 启用并试跑**

- 进 `Actions` 标签页，若顶部有黄色提示，点 *I understand my workflows, go ahead and enable them*
- 左侧选「信小兔爆料监控」→ 右侧 `Run workflow` 手动跑一次
- 看日志出现 `首轮建基线：记录 10 条，不推送` 就成功了（首轮刻意不推送，避免刷屏）

**④ 完成**

之后全自动。想验证是否在工作：看 Actions 的运行列表，正常状态是**一个 job 一直显示转圈（运行中）**，这是长跑模式的特征，不是卡住了。

### 2.3 这个方案的真实代价

| 项目 | 说明 |
|---|---|
| 费用 | ¥0（公开仓库 Actions 无限免费） |
| 速度 | 稳定 1 分钟（不是精确到秒的 1 分钟，但不会漂到 5 分钟） |
| 空档 | 每次接力换班约 20~40 秒（一天约 5 次） |
| 出口 IP | GitHub 美国机房 IP，非住宅 IP |
| 仓库要求 | **必须公开** |

---

## 三、备选方案

### 方案 B：本机常驻（要电脑开机）

```bash
pip install requests
python monitor.py --once --no-push     # 先干跑一轮，验证能抓到
python monitor.py --test-push          # 测试推送通道是否通
POLL_INTERVAL=60 python monitor.py     # 正式常驻（Ctrl+C 停止）
```

Windows 用户可直接双击 `run.bat`。住宅 IP 最不容易触发风控，想调成 10 秒一轮也行。

### 方案 C：Cloudflare Workers Cron（**不公开代码也能免费 1 分钟**）

如果你不想公开仓库，这是唯一的免费替代：Cloudflare 免费版 cron 触发间隔正好是 1 分钟，且不要求开源。
代价是需要把逻辑移植成 JS/TS，并用 Workers KV 存去重状态（KV 免费写入额度 1,000 次/天，因为只在有新爆料时才写，够用）。需要的话我可以再写一版。

### 方案 D：Koyeb 常驻（免费且永不休眠）

Koyeb 免费层给 **1 个常驻 web service（512 MB / 0.1 vCPU）**，**不会休眠**，多数用户无需绑卡。

1. 把本项目推到 GitHub
2. 登录 [koyeb.com](https://www.koyeb.com) → Create Service → GitHub → 选仓库
3. Builder 选 **Dockerfile**（仓库里已带）
4. 端口填 `8899`，健康检查路径 `/`
5. 在 **Environment variables** 里填推送配置，`POLL_INTERVAL = 60`

> 注意：Koyeb 免费层区域只有法兰克福 / 华盛顿，出口是海外 IP。若遇到 403/验证码，把 `POLL_INTERVAL` 调大到 120 秒。

### 方案 E：自己的 VPS / Oracle Cloud 永久免费机

Oracle Cloud Always Free 目前给 **2 OCPU + 12 GB 内存的 ARM 机器**（网上流传的"4 核 24G"已经缩水），200 GB 存储、10 TB 月流量。**注册需国际信用卡、常被拒、7 天闲置会被回收**，门槛较高。

```bash
pip install -r requirements.txt
POLL_INTERVAL=60 PUSH_CHANNEL=wecom WECOM_WEBHOOK=... nohup python monitor.py &
```

建议用 systemd 托管（开机自启、崩溃重拉）。

---

## 四、关于轮询间隔的现实建议

| 间隔 | 每天请求数 | 评价 |
|---|---|---|
| 10 秒 | 8,640 | 激进，对单站偏频繁 |
| 30 秒 | 2,880 | 安全 |
| **60 秒** | **1,440** | **推荐：够快，风控友好** |
| 120 秒 | 720 | 最保守 |

改间隔的方法：GitHub Actions 改 `.github/workflows/monitor.yml` 里的 `POLL_INTERVAL`；本机改环境变量或 `config.json`。

注意：`POLL_INTERVAL` 在 GitHub 方案里控制的是**每轮抓取的间隔**，接力换班的时间由 `loop.sh` 的参数决定，两者独立。

---

## 五、推送渠道配置

设置 `PUSH_CHANNEL` 为下列之一，并填对应凭据。

| 通道 | `PUSH_CHANNEL` | 必填配置 | 说明 |
|---|---|---|---|
| **企业微信机器人** | `wecom` | `WECOM_WEBHOOK` | **最省事**：微信群 → 添加群机器人 → 复制 Webhook 地址即可，无需注册第三方 |
| WxPusher | `wxpusher` | `WXPUSHER_TOKEN` `WXPUSHER_UID` | 微信推送，需注册 appToken |
| Server 酱 | `serverchan` | `SERVERCHAN_KEY` | sct.ftqq.com 申请 SendKey |
| Bark | `bark` | `BARK_URL` | iOS 专用，如 `https://api.day.app/你的key` |
| Telegram | `telegram` | `TELEGRAM_TOKEN` `TELEGRAM_CHAT` | 需科学上网 |
| 自定义 Webhook | `webhook` | `WEBHOOK_URL` | POST JSON：`{title, price, url, time, text}` |

> 一条都不配也能跑，只是新爆料只打进运行日志。

---

## 六、全部配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `MEMBER_ID` | `9687682701` | 监控的 smzdm 用户 ID（信小兔） |
| `POLL_INTERVAL` | `10` | 轮询间隔（秒）；GitHub 方案里建议 `60` |
| `PUSH_CHANNEL` | 空 | 推送通道，空 = 只打印 |
| `FIRST_RUN_PUSH` | `0` | 首次运行是否推送历史条目 |
| `PORT` | `8899` | 健康检查端口（被占用会自动 +1） |
| `NO_HEALTH_SERVER` | `0` | 本地/GitHub 可设 `1` 关掉健康检查 |
| `STATE_FILE` | `state.json` | 去重状态文件路径 |
| `MAX_STATE` | `800` | 状态最多保留多少条 ID |
| `TIMEOUT` / `RETRY` | `15` / `3` | 请求超时与重试 |
| `USER_AGENT` | Chrome 122 | 请求 UA |

配置优先级：**环境变量 > `config.json` > 默认值**。

---

## 七、文件说明

| 文件 | 作用 |
|---|---|
| `monitor.py` | 主程序：抓取 → 解析 → 去重 → 推送 |
| `loop.sh` | GitHub Actions 的接力循环（本机也能跑） |
| `.github/workflows/monitor.yml` | Actions 工作流定义 |
| `Dockerfile` | 容器化部署（Koyeb 等） |
| `run.bat` | Windows 双击启动 |
| `state.json` | 去重状态，**故意纳入版本管理**（Actions 靠它跨次运行记住已推送的爆料） |
| `config.example.json` | 本地配置模板，复制为 `config.json` 后使用 |

> `state.json` 为什么提交进仓库？因为 Actions 的 runner 是一次性的，跑完就销毁。状态必须靠 git 回写仓库才能持久化。里面只有已推送过的爆料 ID，不含任何凭据。
>
> 另外 `monitor.py` 做了特殊处理：**只有去重集合真正变化时才写盘**。所以"每分钟一轮"不会产生每分钟一次的空提交 —— 提交频率 = 新爆料频率。

---

## 八、常见问题

**Q：Actions 里那个 job 一直黄圈转着，是卡住了吗？**
不是。长跑接力模式下单个 job 本来就要跑 5 小时，这是设计如此。看日志有没有在每分钟输出新轮次即可。

**Q：会不会被 GitHub 判定滥用？**
这是 Actions 的标准用法（公开仓库 + 每分钟一次的网络请求），负载很轻（一轮只抓一个 121 KB 的页面）。但请不要把间隔调到 5 秒以内。

**Q：仓库 60 天没活动，schedule 会被自动禁用吗？**
GitHub 确实会对 60 天无提交的公开仓库禁用定时任务。但本方案每次发现新爆料都会提交 `state.json`，信小兔的发帖频率足以保持活跃。

**Q：页面改版了怎么办？**
只需要改 `monitor.py` 里的 `ITEM_RE` 正则 —— 代码在手，随时可修。原 EXE 遇到同样问题就彻底没辙。

**Q：原 EXE 的正则为什么还能用？**
实测 2026-10-09，原正则
`https://www\.smzdm\.com/p/(\d+)/" target="_blank"\>(.+?)\<\/a\>\<\/div\>`
对当前页面**仍然完全有效**（一次命中 10 条），所以本脚本直接沿用了它。

**Q：健康检查有什么用？**
云平台（Koyeb 等）靠它判断服务是否存活。返回：
```json
{"service":"xxt-smzdm-monitor","status":"ok","rounds":12,"last_item_count":10,"interval_sec":60}
```

---

## 九、与原 EXE 的对比

| | 原 信小兔监控5.exe | 本项目 |
|---|---|---|
| 源码 | ❌ 易语言字节码，无法还原 | ✅ 纯 Python，可读可改 |
| 平台 | 仅 Windows GUI | GitHub Actions / 本机 / Docker / 云 |
| 杀软误报 | ⚠️ 易语言产物高误报 | ✅ 无 |
| 手机推送 | 未知（中文串加密） | ✅ 6 种通道可配 |
| 电脑关机 | ❌ 停摆 | ✅ GitHub Actions 版不受影响 |
| 首页改版 | ❌ 只能等作者 | ✅ 自己改正则即可 |
| 凭据安全 | ⚠️ 登录 Cookie 硬编码在 EXE 里 | ✅ 密钥走 Secrets，不入库 |
