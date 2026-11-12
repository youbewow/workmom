# 更新记录

## 0.5.0 — 2026-11-13

**P0 三项全部补齐：能真流式、能并发、卡死能自愈，另把「限流看不到原因」连根拔掉**

这一版把文档里承认过的几个硬边界全动了。核心改动是**新增一条直连传输通道**：不再绕 WorkBuddy
渲染进程的 `window.wb.http.post`，而是自己拿账号 token 直接打上游（本机实测上游本身就是
`text/event-stream` 真流式，之前是反代自己把它拍平了）。

### P0-1 真流式（原来是伪流式）

- 旧实现：JS 侧把上游 SSE **聚合成全文**再 return，Python 侧拿到整段后**按 512 字符切片**发 SSE
  → 首字延迟 = 全量生成耗时，长回答等于「转圈等到死再一瞬间全出」
- 现在（`wm_transport.py` + `relay_stream.py`）：直连上游逐行读 SSE，**来一个 chunk 翻译一个吐一个**
  - 三协议都走同一套增量翻译器：OpenAI `chat.completion.chunk` 原样透传、Anthropic
    `message_start / content_block_delta / message_delta / message_stop` 按增量发、Responses
    `response.output_text.delta` 按增量发
  - 实测（kimi-k3-2，「从1数到40」）：chat 首字节 **1.80s** / 总 7.26s / 500 行；
    anthropic 首字节 2.27s / 6.54s；responses 首字节 2.28s / 5.60s。旧实现这三个数会几乎相等
- 工具调用（`tool_calls`）在流式里同样按增量拼装，不走「等全文」

### P0-2 并发（原来恒为 1）

- 旧实现：`threading.Lock()` 包住**整个** CDP eval 往返（含重试），多客户端同时请求直接排队，
  再快的机器也只是串行
- 现在：直连通道不吃那把锁，改用信号量限流（`max_concurrency`，默认 4）。CDP 回落路径仍保留锁
  （渲染进程只有一条主线程，这是物理限制，锁是必要的）
- 实测 3 路同时流式：墙钟 **4.69s**，三路各自 3.66/4.01/4.69s，串行相加需 12.37s —— 确认并行
- `transport=auto` 时直连不可用会自动回落 CDP，老用户环境（比如 token 读不出来）不会因此挂掉

### P0-3 渲染进程卡死自愈（原来只能手动重启 WorkBuddy）

- 新增看门狗线程（`_probe_cdp` / `_watchdog`），三层探活：端口能不能连 → 页面能不能拿到 → 
  在页面里 `Runtime.evaluate` 一个 `1+1` 能不能回（第三层才抓得到「主线程卡死」这种半死状态）
- 三种模式（`auto_heal`）：
  - `off` —— 关掉，纯直连用户不需要
  - `report` —— **默认**，只把状态挂到 `/health` 的 `cdp_health`（`ok` / `fail_streak` / `last_err`）
  - `restart` —— 连续 2 次探活失败**且**直连 5 分钟内没有一次成功，才 `taskkill` + 带
    `--remote-debugging-port` 重启 WorkBuddy
- 加了双保险避免误杀：只失败一次不动手；直连正健康时不动手（渲染进程死了但直连还能干活，就不该打扰你）
- `/health` 不再对 CDP 做长连接探活（旧版渲染进程卡死时连 `/health` 都跟着超时），改成读看门狗
  缓存，超过 120s 才现场探一次且上限 4 秒

### 附带：上游限流「看不到原因」彻底修掉（用户本轮主诉）

旧版 `chat()` 把上游 429 转成 **502 + 空 body**，客户端只能报「原因暂不明确」。现在：

- `wm_transport.parse_error()` 把 429 / `code 6004` / 401 / 其他 4xx 统一翻成结构化 `UpstreamError`，
  从上游原文里正则抠出 `reset time: ...` → `reset_at`
- 按协议回 **HTTP 429**，body 型如：
  `{"error":{"type":"rate_limit_error","code":"6004","message":"您的使用量已超出频率限制，将在 ... 重置","reset_at":"2026-11-13 05:56:45"}}`
- 响应头加 `Retry-After`（秒）与 `X-WM-Quota-Exceeded: 1`
- **自动降级**：按 `fallback_models` 顺序换到下一个没用满的模型继续出字，只有全链都限才真的报 429；
  降级过的请求会在响应日志里写清「实际使用的模型」，不改写客户端传进来的 `model` 字段
- 模型级熔断：命中 6004 的模型在重置时间前**直接跳过**（不再每请求都去撞一次墙，省时间也省日志）
- 实测抓到线上真实 6004：日志里
  `upstream_error model=kimi-k3.1-pro status=429 6004=True RelayError(429/6004) 您的使用量已超出频率限制，将在 2026-11-13 05:56:45 UTC+8 重置...`，
  随后自动降级成功返回 200

### P1-1 Web 面板（原来根路径只回一段 JSON）

- `GET /` `/panel` `/index.html` `/dashboard` 现在返回真面板 HTML（内置在 `server.py`，也可放同目录
  `panel.html` 覆盖），暗色系，10 秒自动刷新
- 内容：反代通道状态（direct / cdp 各自可用性）、当前模型与 uid、CDP 探活与失败连击、
  **限流冷却倒计时**、用量账本、近期错误表
- README 里「浏览器打开就是面板」这句话现在是真的了（以前那句是错的，真面板只有 Tkinter）
- `/favicon.ico` 返回 204，浏览器控制台不再刷 404

### P1-2 用量账本（原来重启归零）

- 每次成功请求记 `requests / tokens_in / tokens_out / 按模型分组`，内存汇总 + 追加写
  `logs/usage.jsonl`，启动时回读 —— **重启不归零**
- `/health` 与 `/relay_state` 都带 `usage{requests, tokens_in, tokens_out, file}`

### P1-3 回归测试（原来只有文档里一张「自测清单」）

- 新增 `tests/test_relay.py`，**13 项**全自动：临时端口自起独立实例，跑完自己收摊
  - A `/health` 字段齐全 / B 非流式 / C chat 真流式 / D anthropic 真流式 / E responses 真流式 /
    F 真实限额透传 / F2 6004 错误翻译 / F3 错误体结构化 / F4 流式有 `[DONE]` / G 3 路并发 /
    H 用量账本落盘 / I CDP 探活 / J Web 面板
- 用法：`python tests/test_relay.py`（自起实例）、`python tests/test_relay.py 18800`（换端口）、
  `python tests/test_relay.py --url http://127.0.0.1:18793`（只测已跑着的，不动进程）
- 联网依赖：需要本机 WorkBuddy 已登录（会真打上游），不联网时只有离线项（F2/F3/J）能过
- 本机实跑结果：**13 / 13 通过**

### 其他

- 补 `run_relay.bat`：`workmom_guard.py` 发现反代不可达时会调它拉起，之前这个文件根本不存在
  （守护形同虚设，掉线后不会自愈）
- `config.json` / `config.example.json` 增加 `transport` / `max_concurrency` / `direct_timeout_sec` /
  `stream_idle_timeout_sec` / `fallback_models` / `auto_heal` / `heal_interval_sec`，README 新增
  「五·五、配置项速查」表
- 排障表新增三条：客户端只报「原因暂不明确」、明明限流却像没发生、偶发卡死重启就好

### 升级注意

- 旧 `config.json` 不改也能跑：新键全部有默认值（`transport` 缺省即 `auto`，`auto_heal` 缺省 `report`）
- 想让卡死自动恢复，手动把 `"auto_heal": "report"` 改成 `"restart"`
- 直连需要能读到账号 `accessToken`（`workmom_core.ensure_fresh_token`），读不到时自动回落 CDP，
  不会比 0.4.1 更差

## 0.4.1 — 2026-11-12

**修掉「改了 relay_port 没用」——端口现在真的能换**

0.4.0 的 `config.json` 里有 `relay_port`，但 `run_relay.vbs` 启动反代时写死了 `18793` 参数，
`server.py` 只认命令行参数、不读配置，于是**改配置等于白改**，端口被占的人照样起不来。
（干净 clone 实测：改成 18899 后 setup 报「启动失败」，反代实际仍监听 18793。）

修复：
- `server.py` 的 `main()` 端口优先级改为：命令行参数 → `config.json` 的 `relay_port` → 18793；
  启动前先探测端口占用，已有程序在听就打印原因并 `exit(2)`，**不再出现两个反代抢同一端口**
- `run_relay.vbs` 去掉写死的 `18793`，把端口交给 `server.py` 自己从配置读
- `workmom_guard.py`：`config.json` 没显式写 `relay_base` 时，用 `relay_port` 自动拼出反代地址，
  不再默认钉死 18793
- `setup.py` 跑完的「下一步」提示改用实际 `relay_port`，不再固定印 18793
- `GET /health` 新增 `relay_port` / `relay_base` 两个字段：排查「到底听在哪个端口」时
  不用再猜，浏览器打开就知道

setup 自检增强：
- 判断反代是否在跑时，除了 `/health` 通不通，还核对响应里的 `"app": "workmom"`，
  避免端口上蹲着别的程序被误判成「反代已就绪」
- 端口被**非 workmom** 的程序占着时，直接 FAIL 并提示换端口，不再盲目后台启动后干等超时

实测（副本 + `relay_port=18899`）：首次运行自动拉起并绑 18899，`/health` 正常，退出码 0；
重复运行识别「已在运行」不重复启动；双开同一端口退出码 2 并给出说明；
`relay_port` 指向 WorkBuddy 的 9226 时准确报「已被别的程序占用」，退出码 1。

陌生人路径实测（从 GitHub 匿名 `Download ZIP` 解压，目录内**没有 `.git`**，等同外部用户）：
`setup.bat -y` 七步全绿、耗时 7.4s、退出码 0，自动认出 `D:\Nworkbuddy\WorkBuddy.exe`，
按改过的 `relay_port=18899` 拉起反代并绑定成功，`POST /v1/chat/completions` 端到端真的出字。
ZIP 包内 `*.vbs` / `*.bat` 被 GitHub 按 `.gitattributes` 正确还原成 CRLF（VBScript 必须）。

## 0.4.0 — 2026-11-12

**一键安装：把「怎么用」从五个手工步骤压成双击一次**

以前 clone 下来要自己找 WorkBuddy 路径、自己带参数重启 WB、自己装依赖、自己复制改 config，
这四件事劝退了绝大多数人。0.4.0 全部接管。

新增：
- `setup.bat` / `setup.py` —— 一键初始化，七步走完：检查 Python 版本与目录完整性 → 装依赖 →
  **自动定位 WorkBuddy** → 生成 `config.json` 与 `pythonw.txt` → 检查 CDP 端口 → 跑反代 `/health` 自检 →
  建桌面快捷方式。
  - 幂等：可反复执行，已改过的配置项不会被覆盖
  - 解析失败的 `config.json` 改名 `.broken` 留证，**不删除**
  - `setup.bat` 纯 ASCII + CRLF，`chcp 65001` 保证中文不乱码；`setup.py` 也可直接跑
  - `-y` 无人值守；`--no-selftest` / `--no-shortcut` 跳过最后两步
- `workmom_core._guess_wb_exe()` 升级为**四路探测 + 进程内缓存**：
  注册表（`App Paths` + 卸载信息 `DisplayIcon` / `InstallLocation`）→ 常见安装目录 →
  运行中 `WorkBuddy.exe` 进程路径 → 兜底。装在 D 盘 / E 盘 / 改过文件夹名的都能认出来
  （实测注册表路径命中耗时约 2ms）
- `workmom_core.DEF["wb_exe"]` 默认改空串，`cfg()` 读到空值自动探测回填；
  `workmom_guard.load_cfg()` 同样兜底 —— **`wb_exe` 现在可以不填了**
- `config.example.json` 的 `wb_exe` 由示例路径改为空串

安全约束：
- 关闭 WorkBuddy 有副作用（会丢未保存内容），`setup` 检测到 CDP 端口不通时**只询问、不擅自执行**；
  WorkBuddy 正在运行时这个问题的默认答案是「否」，并打印手动启动命令供复制

文档：
- README 第一节重写为「装起来（两步）」：双击 `setup.bat` → 双击桌面 `workmom`。
  手工部署与 `setup.py` 参数降级为备查小节
- README「一·五」重写：原来五个手工前提改为「已由脚本接管」对照表，只保留真正剩下的前置条件
- README 清除文件头遗留的 AIGC 元数据；CHANGELOG 同步清除
- README 的文件说明 / 启动停止 / 排障三张表补充 `setup` 相关条目

不改的东西：反代与协议层（`server.py`、`relay_protocols.py`）**零改动**，老用户升级后行为完全一致。


## 0.3.1 — 2026-11-12

**可移植性修复：让 clone 下来的人真能跑**

- `server.py`：`CDP_PORT` 不再写死 9226，改为读 `config.json` 的 `cdp_port`（此前改 config 无效，9226 被占用的机器直接不可用）；
  同理 `upstream_path` 也走 config
- `server.py`：新增 CDP 调用总超时（`cdp_timeout_sec`，默认 300 秒）。以前 `Runtime.evaluate` 默认 30 分钟超时，
  WorkBuddy 渲染进程卡死时客户端会静默挂半小时；现在超时抛 `CdpTimeout`，**不重试**，直接返回 `504` + `type:"cdp_timeout"` + 分步排查提示
- `workmom_core.py` / `workmom_guard.py`：读 `config.json` 与 `pythonw.txt` 改 `utf-8-sig`。
  Windows 记事本 / PowerShell 存盘默认带 BOM，旧的 `utf-8` 硬读会解析失败并被 `except` 静默吞掉，
  表现为「改了 config 却不生效」，排查极难
- `GET /health` 升级为自检口：新增 `hint`、`cdp_port`、`cdp_timeout_sec`、`config_file/exists`、
  `wb_exe/exists`、`python`、`uptime_sec`；`cdp` 连不上时直接给排查顺序
- `_find_page_ws` 超时 15 秒 → 5 秒；报错信息用实际 `cdp_port`；页面挑选不区分大小写（`Nworkbuddy`/`workbuddy` 都能命中）
- `config.example.json` 增加 `cdp_timeout_sec`
- README 新增「一·五、别人 clone 下来能直接用吗」（五个前提 + 不含个人数据说明）、
  「四·五、模型选在哪、怎么接进 agent」（面板双击选模型 / 客户端填 base_url / `X-WM-Model` 单次覆盖三张表），
  排障表补 504 `cdp_timeout`、`/health` 各字段两个条目

## 0.3.0 — 2026-11-12

**多协议反代 + Claude Code 托管 + 开源整理**

- 新增 `relay_protocols.py`：在原有 OpenAI Chat 之外，同端口再开 Anthropic Messages（`/v1/messages`、
  `/v1/messages/count_tokens`）与 OpenAI Responses（`/v1/responses`）两套协议，非流式与 SSE 流式都支持
- 自测：协议转换 39 项、端到端 16 项全通过（含 Anthropic 工具调用、流式事件序列、token 估算）
- Claude Code 纳入本仓库托管：`~/.claude/settings.json` 的 `ANTHROPIC_BASE_URL` 指向 `http://127.0.0.1:18793`
- 新增 `多协议接入说明.md`：Claude Code / Codex CLI / OpenAI SDK 等客户端逐项配置
- 仓库开源整理：
  - 源码脱敏——移除硬编码的个人安装路径与 8 个账号 uid，账号队列改由 `data/guard_queue.json` 提供
  - 新增 `.gitignore`（配置、凭证、日志、备份全部排除）、`.gitattributes`（VBS/BAT 强制 CRLF）
  - 新增 `LICENSE`(MIT)、`requirements.txt`、`config.example.json`、`docs/ARCHITECTURE.md`
  - README 补「它解决什么问题 / 环境准备与首次部署 / License 与免责」
  - 启动器通用化：`run_*.vbs` / `start_all.vbs` / `boot_silent.vbs` 改为按脚本自身目录推导路径，
    pythonw 依次探测 `pythonw.txt` → `.venv` → `PATH`，找不到写 `logs/launch_error.log` 而不弹窗；
    `stop_all.bat` 改用 `%~dp0`，整套脚本不再写死任何盘符
  - `workmom_core.py` / `dashboard.py` / `server.py` 去掉硬编码的个人安装路径，改为环境变量 + 自动探测

## 0.2.0 — 2026-11-12

**账号与模型解耦，彻底摆脱 WorkDaddy 依赖**

- `workmom_core.py` 用 Python 重写 WorkDaddy 的三大能力：无感登录（官方 OAuth + 轮询）、
  无感切号（写登录文件 + 清 accountSnapshot + CDP reload）、每日自动领积分（多域名兜底 + 幂等去重）
- 模型状态独立持久化到 `data/current_model.json`：换账号不改模型、改模型不动账号
- 新增 `.info` 信封加解密（AES-256-GCM + 自定义 AAD），密钥由 `ELECTRON_RUN_AS_NODE` 探针向官方代码取
- 面板双击行分别触发「切模型 / 切账号」；守护改为跟随面板所选模型，不再硬编码
- 旧版反代工作目录下线，回退靠本地 `backups\server.py.bak-*`

## 0.1.0 — 2026-11-11

**首版：本地反代 + 自动切号守护**

- `server.py` 经 CDP 复用 WorkBuddy 登录态，在 `127.0.0.1:18793` 提供 OpenAI 兼容接口
- `workmom_guard.py` 盯 `/relay_state`，遇 429/6004 自动按额度队列换号
- `dashboard.py` 总控面板、`start_all.vbs`/`stop_all.bat` 一键启停、`boot_silent.vbs` 开机静默自启
