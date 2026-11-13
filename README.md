# workmom —— WorkBuddy 账号 / 模型 总控（自研，不依赖 WorkDaddy）

一个本地小工具集，把 WorkBuddy 桌面端的「账号」和「模型」拆成两个互不干扰的维度来管：

- **账号**：无感登录新号、无感切号（不重启 WorkBuddy）、每日自动领积分
- **模型**：随时切换，持久化在本地；换账号**不会**改模型，改模型**不会**动账号
- **反代**：`http://127.0.0.1:18793`，OpenAI 兼容，给 Marvis 等客户端当上游

---

## 零、它解决什么问题

WorkBuddy 桌面端把「用哪个账号」和「用哪个模型」绑在一块：换号要退出重登、换模型要重新点、额度用满了只能干等。
workmom 把这两件事拆开，并且把 WorkBuddy 已经登录好的渲染进程直接当成本地 LLM 网关来用：

- 对外只暴露一个本机回环地址 `http://127.0.0.1:18793`，同时说三种协议（OpenAI Chat / Anthropic Messages / OpenAI Responses）；
- Claude Code、Codex CLI、各类 OpenAI 兼容客户端只要把 base_url 指过来，就能借 WorkBuddy 的额度干活；
- 账号、模型各自独立切换，互不影响；额度爆了自动换号，客户端全程无感。

## 一、装起来（三步）

前面不需要看别的东西，就三步。

**第 0 步：把整个目录弄到本地**（二选一）

| 方式 | 怎么做 | 适合谁 |
| --- | --- | --- |
| 下载 ZIP | 页面右上绿色 `Code` → `Download ZIP` → 解压到任意普通文件夹，比如 `D:\workmom` | 不装 git 的人 |
| git clone | `git clone https://github.com/youbewow/workmom.git D:\workmom` | 想随时 `git pull` 更新的人 |

两点要留意：
- **要整个目录，不要只下单个文件。** `server.py` 离开 `workmom_core.py` 等同伴跑不起来。
- **别放在 `C:\Program Files` 这类受保护目录。** 脚本要写 `config.json` 和 `logs\`，放这些地方会因为权限失败。桌面、D 盘、用户目录都可以。

**第 1 步：双击 `setup.bat`**

它会把该做的事一次做完，全程中文提示，缺什么当场问你：

| 步骤 | 做什么 | 出问题会怎样 |
| --- | --- | --- |
| 1 | 检查 Python 版本（要 3.9+）和目录完整性 | 提示去哪装、装的时候要勾什么 |
| 2 | 检查并安装 `websocket-client`、`cryptography` | 给出换清华源重试的命令 |
| 3 | **自动定位 WorkBuddy.exe**（注册表 → 常见目录 → 运行中进程 → App Paths） | 给下载地址，也可手工粘路径 |
| 4 | 生成 `config.json`（保留你改过的项）和 `pythonw.txt` | 坏掉的 config 改名 `.broken` 保留，不删 |
| 5 | 检查 CDP 调试端口，不通会先问你 | 给出手动启动命令 |
| 6 | 拉起反代跑一次 `/health` 自检 | 告诉你去哪个日志里找 |
| 7 | 在桌面建 `workmom` 快捷方式 | 提示直接双击 `start_all.vbs` |

两个以前最劝退的点，现在都不用管了：

- **WorkBuddy 装在哪都行。** 以前要手工把 `wb_exe` 改成实际路径，而这台机器可能装在 D 盘、E 盘或改过文件夹名。现在脚本从注册表卸载信息、App Paths、常见安装目录、正在运行的进程四个地方找，找到就自动写进配置。
- **调试端口不用自己加参数。** 以前必须手动退出 WorkBuddy，再用命令行带 `--remote-debugging-port=9226` 启动。现在脚本检测到端口不通会**先问你**：WorkBuddy 在跑就问「要重启它吗（默认否）」，没跑就问「现在启动吗」。关掉它会丢未保存的东西，所以默认不替你做主。

**第 2 步：双击桌面的 `workmom` 快捷方式**

等价于双击仓库里的 `start_all.vbs`，一把拉起反代 + 守护 + 面板。浏览器打开 `http://127.0.0.1:18793` 就是面板。

要在 Claude Code / Codex CLI / Marvis 里用，把 base_url 指向 `http://127.0.0.1:18793`，详见「四·五」。

**就这三步。** 下面是备查细节。

### 手工部署（不想用脚本，或脚本卡住时）

```powershell
cd <你 clone 下来的目录>
pip install -r requirements.txt
copy config.example.json config.json     # wb_exe 留空也行，运行时会自动探测
python server.py 18793                   # 反代
python dashboard.py                      # 总控面板（另开窗口）
```

同一个脚本的非交互用法：

```powershell
python setup.py                # 交互式，等同双击 setup.bat
python setup.py -y             # 全用默认值；关 WorkBuddy 这类动作仍然默认不执行
python setup.py --no-selftest  # 跳过第 6 步反代自检
python setup.py --no-shortcut  # 不建桌面快捷方式
```

脚本是幂等的，重复跑不会破坏已有配置，随时可以再来一次。

**WorkBuddy 必须带调试端口启动**（用 `setup.bat` 会自动处理，手工部署时的验证方法）：

```powershell
# 路径换成你自己的实际位置，不知道在哪就直接搜 WorkBuddy.exe
& "D:\Nworkbuddy\WorkBuddy.exe" --remote-debugging-port=9226
```

浏览器打开 `http://127.0.0.1:9226/json/version` 能返回 JSON 即通。想常驻就改快捷方式的目标，在 exe 路径后加
` --remote-debugging-port=9226`，或用 `boot_silent.vbs` 接管启动。

想后台常驻：双击 `start_all.vbs`（反代 + 守护 + 面板），或把 `boot_silent.vbs` 的快捷方式丢进
`shell:startup` 做开机静默自启。

**pythonw 从哪来**：几个 `run_*.vbs` 按顺序找解释器——同目录 `pythonw.txt`（一行，写 pythonw.exe 的完整路径）
→ 同目录 `.venv\Scripts\pythonw.exe` → 系统 `PATH`。都没有时不会弹窗，而是把原因写进
`logs/launch_error.log`。`setup.bat` 会自动把 `pythonw.txt` 写好；装了 Marvis 之类自带 Python 的客户端，
想换成它的 `pythonw.exe`，改这一个文件就行。

## 一·五、别人 clone 下来能直接用吗

**能，双击 `setup.bat` 就行。** 以前有四道手工门槛，现在都被脚本接管了：

| 以前要你手工做的事 | 现在 |
| --- | --- |
| 自己找出 WorkBuddy 装在哪，改 `wb_exe` | 四路自动探测（注册表 / 常见目录 / 运行中进程 / App Paths），装任意盘都认 |
| 自己退出 WB，再用命令行带 `--remote-debugging-port=9226` 重启 | 检测端口，不通会问你要不要重启，并自带等待就绪（最多 120 秒） |
| 自己 `pip install -r requirements.txt` | 自动检查缺哪个装哪个，失败给换源命令 |
| 自己 `copy config.example.json config.json` 再逐项改 | 自动生成，保留已改过的值；解析失败的文件改名 `.broken` 留证，不删 |

剩下真正属于「这台机器自己」的、脚本没法替你解决的：

| 前提 | 缺了会怎样 |
| --- | --- |
| 本机装了 WorkBuddy，并登录了**你自己的**账号 | 拿不到登录态和密钥，请求全部失败（脚本会提示没装并给下载地址） |
| 端口 9226、18793 没被占用 | 9226 被占改 `config.json` 的 `cdp_port`；18793 被占改 `relay_port`（**改一处就够**，反代 / 守护 / 面板 / 快捷方式全跟着走）。setup 会先探测端口：发现是别的程序占着会直接报出来让你换，不会傻启动 |
| 账号本身有可用额度 | 这是 WorkBuddy 官方账号体系决定的，跟本仓库无关 |

**仓库里没有任何个人数据**：`config.json`、`data/`、`logs/`、`backups/`、`guard_state.json`、
`rate_limit_ledger.json`、`pythonw.txt` 全在 `.gitignore`，`git clone` 下来是干净的，不会带出别人的账号 uid 或本机路径。

**这版专门修掉的三个「下了用不了」**：

- `CDP_PORT` 以前写死 9226，`config.json` 里改 `cdp_port` 不生效（9226 被占用的人直接废）→ 现在真的读 config；
- `config.json` 带 BOM（Windows 记事本 / PowerShell 存盘默认带 BOM）时配置被静默吞掉，改了不生效 → 现在按 `utf-8-sig` 读；
- `wb_exe` 只认 `%LOCALAPPDATA%\Programs\WorkBuddy` 一种安装位置，装在别的盘就找不到 → 现在四路探测，找不到还会在运行时自动回填。

**做不到的事**：WorkBuddy 官方接口或密钥机制一旦变化，上游调用就会挂。这不在本仓库能控制的范围，
届时更新 `workmom_core.py` 的探测 / 解密逻辑。

## 二、文件说明

| 文件 | 作用 |
| --- | --- |
| `workmom_core.py` | 核心：凭据加解密、账号库、无感切号、无感登录 OAuth、每日签到、模型状态 |
| `server.py` | 反代（默认 listen `127.0.0.1:18793`），经 CDP 复用 WorkBuddy 登录态转发到上游 |
| `dashboard.py` | 总控面板（Tkinter），账号表 + 模型表，双击即切 |
| `workmom_guard.py` | 自动切号守护：盯反代 `/relay_state`，遇 6004 限流自动换号 |
| `config.json` | 配置：WB 路径、CDP 端口、反代端口、默认模型 |
| `data/accounts/*.info` | 各账号登录态备份（与 WorkBuddy 登录文件同格式，密文原样） |
| `data/current_model.json` | **当前模型**（与账号完全解耦，唯一权威来源） |
| `data/checkin.json` | 各账号每日领积分结果（按 uid + 日期去重） |
| `rate_limit_ledger.json` | 限流账本：哪个号哪个模型、限流到什么时候 |
| `guard_state.json` | 守护状态：上次切号、退避、历史 |
| `logs/` | `workmom.log` / `workmom_guard.log` / `relay.out.log` / `relay.err.log` |
| `relay_protocols.py` | 三协议转换层：Anthropic Messages / OpenAI Responses ↔ OpenAI Chat（非流式 / 兼容用） |
| `wm_transport.py` | **直连传输层**：直接拿 token 走 httpx 打上游，真流式逐行产出、无全局锁、结构化识别 429/6004、模型级熔断 |
| `relay_stream.py` | **增量流式翻译器**：上游来一个 chunk 就吐一个，不再「攒全文再切片」 |
| `panel.html` | （可选）自定义 Web 面板；没有这个文件时用 `server.py` 内置的默认面板 |
| `tests/test_relay.py` | 回归测试：13 项覆盖真流式 / 并发 / 限额透传 / 面板 / 账本 / 看门狗 |
| `run_relay.bat` | 守护发现反代掉了时用它拉起（`run_relay.vbs` 的批处理壳） |
| `config.example.json` | 配置模板，复制成 `config.json` 再改 |
| `pythonw.txt` | （可选，不入库）一行写死 pythonw.exe 路径，供 `.vbs` 启动器使用 |
| `setup.bat` | **一键初始化入口，双击这个即可**。纯 ASCII + CRLF，只负责找 Python 并调 `setup.py` |
| `setup.py` | 初始化脚本本体：定位 WB、生成配置、装依赖、查 CDP、跑自检、建快捷方式（可重复执行） |
| `requirements.txt` | 运行依赖，只有两个第三方包 |
| `docs/ARCHITECTURE.md` | 架构与实现要点：链路、协议层、加密、切号原理、踩过的坑、自测清单 |
| `CHANGELOG.md` | 更新记录 |
| `LICENSE` | MIT |

## 三、启动 / 停止

| 操作 | 方式 |
| --- | --- |
| 全量启动（反代 + 守护 + 面板） | 桌面 `workmom` 快捷方式（`setup.bat` 自动创建）→ `start_all.vbs` |
| 首次安装 / 重装 / 配置坏了 | 双击 `setup.bat`，幂等，随时可重跑 |
| 只开面板 | 桌面 `workmom 总控面板.lnk` → `run_panel.vbs` |
| 开机自启（静默，只反代+守护，不弹面板） | 启动文件夹 `workmom 自启.lnk` → `boot_silent.vbs` |
| 全停 | `stop_all.bat`（只杀**本目录**下的 python 进程，按 `%~dp0` 定位，不认盘符） |

> 所有 `.vbs` 都是**纯 ASCII**写的。别往里面加中文注释——WScript 按 GBK 解析会把下一行吞掉，报「缺少对象: 's'」。

## 四、面板怎么用

- **双击账号行** = 无感切到那个号（写登录文件 + CDP 刷新页面，**不重启 WorkBuddy**，约 3 秒）
  - 那个号正在限流里，会提示；5 秒内再双击一次 = 强切
- **双击模型行** = 把全部请求切到这个模型。跟换账号**完全无关**，切完账号还是原来那个号
- `+ 无感登录新号` = 生成官方 OAuth 链接并打开浏览器，登录完自动入库，全程不打断 WorkBuddy
- `一键领全部积分` = 给所有号领当日积分（凭证过期会先自动 refresh）
- `重启 WB(兜底)` = 只有无感切号失败、CDP 不可用时才用，会重启 WorkBuddy

## 四·五、模型选在哪、怎么接进 agent

一条链路三个点，各管一段，别搞混：

| # | 在哪做 | 干什么 | 落到哪 |
| --- | --- | --- | --- |
| ① 选模型 | `dashboard.py` 面板 → **双击「模型使用情况」表里的那一行** | 决定反代所有请求默认用哪个模型 | `data/current_model.json` |
| ② 接 agent | agent 客户端里填 `base_url` = `http://127.0.0.1:18793` | 让 agent 把请求发到反代而不是官方 API | 该 agent 自己的配置文件 |
| ③ 单次覆盖 | 请求头 `X-WM-Model: glm-5.3` | 这一次请求换个模型，不动面板 | 仅本次请求 |

各类 agent 的落点：

| agent / 客户端 | 配在哪 | 关键字段 |
| --- | --- | --- |
| Claude Code | `%USERPROFILE%\.claude\settings.json` | `env.ANTHROPIC_BASE_URL` = `http://127.0.0.1:18793`；`env.ANTHROPIC_AUTH_TOKEN` = 任意非空串（如 `wb-workmom`）；`model` = 面板选的那个 |
| Codex CLI | `~/.codex/config.toml` | `base_url` = `http://127.0.0.1:18793/v1`，`wire_api = "responses"` |
| Marvis / Cherry Studio / Cline 等 OpenAI 兼容客户端 | 客户端「自定义模型 / API 设置」 | Base URL `http://127.0.0.1:18793/v1`；API Key 随便填一个非空串；模型名填面板当前模型 |

要点：**API Key 填任意非空串就行**——反代不校验 key，真正用的凭证是 WorkBuddy 桌面端已经登好的那套。
完整片段见 `多协议接入说明.md`。

## 五、两条硬规则（设计核心）

1. **选定模型后，换任何账号，模型都是当前这个。**
   模型只存在 `data/current_model.json`，切号逻辑从头到尾不碰它。
2. **选定账号后，改任何模型，账号都是当前这个。**
   切模型只写 `current_model.json`，不碰登录文件、不刷新页面、不动 WB。

## 五·五、配置项速查（`config.json`）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `wb_exe` | 自动探测 | WorkBuddy.exe 路径 |
| `cdp_port` | `9226` | WorkBuddy 的远程调试端口（必须带 `--remote-debugging-port` 启动） |
| `relay_port` | `18793` | 反代监听端口（命令行参数优先） |
| `default_model` | `kimi-k3.1-pro` | 兜底默认模型；真正生效的是 `data/current_model.json` |
| `cdp_timeout_sec` | `300` | 单次 CDP 往返超时 |
| `transport` | `auto` | `auto` 直连优先失败回落 / `direct` 只直连 / `cdp` 只走渲染进程 |
| `max_concurrency` | `4` | 直连通道最大并发（超出排队） |
| `direct_timeout_sec` | `600` | 直连单次请求总超时 |
| `stream_idle_timeout_sec` | `180` | 流式「多久没新数据算断」 |
| `fallback_models` | pro → k3-2 → k3-1 → deepseek → glm | 上游 6004 / 429 时的自动降级顺序 |
| `auto_heal` | `report` | 看门狗：`off` 关闭 / `report` 只上报 / `restart` 自动重启 WorkBuddy |
| `heal_interval_sec` | `60` | 看门狗探活间隔 |
| `quota_switch_percent` | `92` | 已用额度到多少就主动换号（`92` = 用掉 92% 才换；想早点换就调小，如 `80`） |
| `quota_switch_min_secs` | `900` | 两次主动换号的最小间隔（秒），防多号同窗限流原地打转 |
| `quota_warn_percent` | `80` | 用量过这个比例在 `/health` 与面板上标黄预警 |
| `quota_sweep_secs` | `600` | 全号额度巡检间隔（0.5.2）：每隔这么久把所有账号的额度都探一遍写进 `/relay_state` |
| `switch_6004_min_secs` | `45` | 撞到 6004 后紧急换号的最小间隔（0.5.2）：已真撞墙，比主动换号宽松得多 |
| `audit_requests` | `3` | 请求审计保留最近几条完整请求体，`0` 关闭（落 `logs/req_audit.jsonl`） |
| `slim_enabled` | `true` | 输入瘦身总开关（0.5.1，见下节） |
| `slim_keep_recent_tools` | `3` | 最近 N 条工具结果原样保留，更早的按 `slim_old_tool_chars` 压缩 |
| `slim_old_tool_chars` | `1500` | 老工具结果压缩后保留的字符数（头尾各半） |
| `slim_strip_reasoning` | `true` | 剥掉历史里的 reasoning/思考字段（上游不认，纯占位） |
| `slim_merge_system` | `true` | 多条 system 消息合并成一条 |
| `slim_dedupe_min_chars` | `3000` | 超这个长度的重复段落去重 |
| `slim_para_min_chars` | `500` | 长段落里的空白/重复换行折叠阈值 |

改完不需要重装，重启反代生效：`stop_all.bat` 后双击桌面 `workmom`。

## 六、反代接口

- `GET /health` —— 存活 + 当前模型 + 当前 uid + 模型清单；另带自检字段 `cdp` / `hint` / `cdp_port` /
  `cdp_timeout_sec` / `config_exists` / `wb_exe_exists` / `python` / `uptime_sec`，装完先看这一个就够定位。
  0.5.0 起再加 `transport` / `transport_ready` / `max_concurrency` / `direct` / `cdp_health` /
  `cooldowns` / `usage` / `auto_heal` / `fallback_models`；
  0.5.1 起再加 `quotas`（各号各模型用量）/ `quota_switch_percent` / `quota_warn_percent` /
  `audit_left`（审计还留几条）/ `slim`（瘦身开关与累计节省）；
  0.5.2 起 `quotas` 的完整明细也进了 `/relay_state`（见下条），`/health` 另带 `last_pro_6004_uid`
- `GET /` `GET /panel` —— **Web 面板**（浏览器直接看，不用开 Tkinter）：通道健康、CDP 探活、限流冷却倒计时、
  用量账本、近期错误表，10 秒自动刷新
- `GET /relay_state` —— 运行状态（守护只读消费，零额外 token）。0.5.2 起带全账号额度明细：
  `uid`（当前有效号）/ `quotas_by_uid`（`{uid: {model: {percent, reset_at, at}}}`，面板靠它逐行显示「额度已用」）/
  `last_pro_6004_uid`（最近一次撞 6004 的是哪个号）
- `GET /v1/models` —— OpenAI 格式模型列表
- `POST /v1/chat/completions` —— OpenAI Chat 协议，`model` 字段留空则用当前模型
- `POST /v1/messages` —— Anthropic Messages 协议（Claude Code 等）
- `POST /v1/messages/count_tokens` —— Anthropic token 估算（本地算，不耗额度）
- `POST /v1/responses` —— OpenAI Responses 协议（Codex CLI 等）

三套协议共用同一上游调用，转换逻辑在 `relay_protocols.py`；接入配置见 `多协议接入说明.md`。
请求头 `X-WM-Model: <模型名>` 可临时覆盖面板模型。

### 流式与并发（0.5.0 起）

| 项 | 0.4.x 之前 | 0.5.0 |
| --- | --- | --- |
| 流式 | 上游全量生成完，Python 侧再按 512 字符切片发 → 首字延迟 = 全量耗时 | 直连上游 SSE，**来一个 chunk 吐一个**，首字节 ≈ 2 秒 |
| 并发 | `threading.Lock()` 包住整个 CDP 往返，多客户端排队，等效并发 1 | 直连无锁，`max_concurrency` 限流（默认 4） |
| 传输 | 只能过 WorkBuddy 渲染进程（`window.wb.http.post`），WB 卡死即全挂 | `transport=auto` 优先直连，直连不可用才回落 CDP |

`config.json` 里 `transport` 可选 `auto`（默认）/ `direct`（只直连，失败直报错）/ `cdp`（只走渲染进程，旧行为）。

### 错误怎么透传（这条是 0.5.0 的重点）

以前上游 429 频率限制会被兜成 **502 + 空 body**，客户端只能显示「原因暂不明确」，看不出是额度问题。
现在按协议返回 **HTTP 429**，body 是结构化错误，带重置时间：

```json
{
  "error": {
    "type": "rate_limit_error",
    "code": "6004",
    "message": "您的使用量已超出频率限制，将在 2026-11-13 05:56:45 UTC+8 重置，您也可以切换其他模型继续使用。",
    "reset_at": "2026-11-13 05:56:45"
  }
}
```

响应头另带 `Retry-After`（秒）与 `X-WM-Quota-Exceeded: 1`。
**0.5.2 改了策略：额度类错误（429/6004）不再换模型顶。** 额度是账号维度的，换模型等于把同一条上下文丢给
另一个模型（上下文被来回搅坏），同源备用模型还会被顺手烧掉。现在 429 原样透传给客户端 —— 看到 429 说明
**该换账号**了，守护会在 `switch_6004_min_secs`（默认 45 秒）内完成无感切号，客户端重试即可。
只有 404（模型名写错/不存在）才会在同一账号内按 `fallback_models` 顺序降级。

### 输入瘦身 slim（0.5.1 起）

客户端每轮都把整段历史重发一遍，而工具返回（读文件、grep、日志、代码）动辄上万字，一轮轮叠上去输入 token 涨得比输出快得多。
实测这类请求里**工具返回占输入 token 的 42.5%**，是第一大膨胀源。
0.5.1 起反代在转发前对入参做四层**无副作用压缩**（只改发出去的副本，不改客户端、不改落盘）：

| 层 | 做什么 | 开关 |
| --- | --- | --- |
| 1 老工具结果压缩 | 最近 `slim_keep_recent_tools` 条原样保留，更早的压成「头 65% + 尾 25% + 中间省略量标注」 | `slim_old_tool_chars` |
| 2 剥 reasoning | 历史里的思考字段上游不认，纯占位，直接剥掉 | `slim_strip_reasoning` / `slim_keep_last_reasoning` |
| 3 长重复去重 | 超 `slim_dedupe_min_chars` 的重复段落只留一份 | `slim_dedupe_min_chars` |
| 4 system 合并 | 多条 system 消息合并成一条 | `slim_merge_system` |

实测节省：**消息部分 −36%，含工具定义的全量 −29.8%**。上线头 23 次请求累计省下 **23.7 万 tok**，
最近一次真实请求 59,792 → 42,735 tok（−28.5%）。

不会打坏 prompt cache：瘦身是确定性的（同样的输入必然得到同样的输出），上游缓存照常命中。

观测：响应头带 `X-WM-Slim-Before` / `X-WM-Slim-After` / `X-WM-Slim-Saved` / `X-WM-Slim-Pct`；
`GET /health` 的 `slim` 块里有累计 `requests` / `saved_est_total` 和最后一次明细。想关掉设 `slim_enabled=false`。

### 额度主动轮换（0.5.1 起）

0.5.0 是「撞到 6004 才换号」，等于每次都要先浪费一次失败请求。0.5.1 起守护在主循环里做**前置判断**，四个条件同时满足才动手：

1. 当前号该模型已用额度 ≥ `quota_switch_percent`（默认 92%）
2. 距上次「额度换号」≥ `quota_switch_min_secs`（默认 900 秒，防抖）
3. 当前不在限流退避窗口里
4. 号池里存在比当前更干净的号

任何一条不满足就原地不动 —— 尤其第 4 条，避免多号同窗限流时来回横跳却换不出额度。
想早点换就把阈值调小，比如 `quota_switch_percent=80` 表示剩余 20% 就开始换。

先看它想干什么（**零副作用，只判断不切号**）：

```bat
python workmom_guard.py --quota-check
```

会逐条打印阈值、当前用量、距上次换号多久、有没有更干净的号，最后给一句结论。加 `--probe` 会实打一次上游拿真实额度（那一次会计费）。

### 额度可见 + 撞墙只换号不换模型（0.5.2 起）

三个坑一次填掉：

**1. 额度摊到面板上，不再只有当前号一个数字。**
面板「额度已用」有两个数据源，按可信度排：

| 来源 | 谁写的 | 覆盖范围 | 优先级 |
| --- | --- | --- | --- |
| 反代实测 | 反代每次成功响应顺手记上游 `usage.quota` → `/relay_state` 的 `quotas_by_uid` | 只覆盖**真正被用过**的号 | 高（最准） |
| 守护探测 | `quota_sweep_secs`（默认 600 秒）一轮全号巡检，落 `data/quota_probe_cache.json`，2 小时内有效 | 覆盖**号池全部**账号 | 低（兜底） |

`/relay_state` 同时带 `uid`（当前有效号）与 `last_pro_6004_uid`（最近一次撞 6004 的是哪个号）。
面板显示 `84%` 正常 / 黄色预警（过 `quota_warn_percent`）/ ≥99.5% 红；两个源都没有就显示 `—`，不会留空行。

**2. 撞 6004 只换账号，绝不换模型。**
`server.py` 里 429/6004 不再走 `fallback_models` 降级（那会把同一条上下文丢给另一个模型，还会把
`kimi-k3-1` / `kimi-k3-2` 这类同源备用号一起烧掉），改为原样透传 429 + 打 `quota` 熔断标记；
守护看到后走紧急换号通道（`switch_6004_min_secs`，默认 45 秒），额度到 **100%** 更是立即切、不等冷却。
切号后旧号的额度读数一律作废（`quota_stale_since`），不会拿过期数字判断新号。

**3. 面板刷新不再一断就死。**
以前定时刷新把数据直接往 `Listbox` 里铺，列定义与数据长度必须严格相等 —— 加了「额度已用」列的这一版
一开始就是「刷新一下就不动」的状态。现在统一走 `safe_refresh()`，`_tick_body()` 把取数、格式化、渲染分段隔离，
任何一列缺失按 `—` 补位，异常写 `logs/panel_error.log` 后继续排下一次刷新，不用重开面板。

## 七、排障

| 现象 | 先看哪 |
| --- | --- |
| 客户端只报「原因暂不明确」/「未知错误」，看不出是不是额度 | 0.4.x 的老问题（上游 429 被兜成空 502）。0.5.0 起已改成结构化 429，可直接看到 `code 6004` + 重置时间。若还看到空 body：`curl -s -i -X POST http://127.0.0.1:18793/v1/chat/completions -H "Content-Type: application/json" -d "{\"model\":\"kimi-k3.1-pro\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}"` 亲眼确认状态码与 body；仍是 502 说明跑的还是旧进程，`stop_all.bat` 后重起 |
| 明明上游限流了，反代却像「没发生」 | 这是降级在起作用：反代按 `fallback_models` 自动换到下一个没用满的模型，客户端拿到的是 200。想确认当下真实状态看 `GET /`（Web 面板「限流冷却」区块）或 `/health` 里的 `cooldowns` / `pro_6004` 计数 |
| 只有部分请求能出字、偶发卡死，重启 WorkBuddy 就好 | 渲染进程主线程卡死的典型症状。0.5.0 起有看门狗：`/health` 的 `cdp_health` 里看 `ok` / `fail_streak` / `last_err`；`auto_heal=report` 只上报，想让它自动重启 WorkBuddy 就改 `auto_heal=restart`（连续 2 次探活失败且直连 5 分钟没成功才动手） |
| 双击 `setup.bat` 窗口一闪就没 | 多半是没装 Python 或没勾 Add to PATH。开个 cmd 把 bat 拖进去运行，就能看到报错 |
| `setup.bat` 说没找到 WorkBuddy | 先装 WorkBuddy；脚本会问你要路径，也可以自己在 `config.json` 填 `wb_exe`（留空则运行时自动探测） |
| `setup.bat` 说 CDP 不通、自检失败 | 按它打印的手动命令带 `--remote-debugging-port=9226` 重启 WorkBuddy，再跑一次 `setup.bat` |
| 反代没起来 / `端口监听 False` | `logs/relay.out.log`、`logs/relay.err.log`；多半是端口被旧进程占着，跑 `stop_all.bat` 再起 |
| 切号没反应 | `logs/workmom.log` 里找「无感切号完成」；失败时看是不是 CDP 9226 不通（WorkBuddy 没带 `--remote-debugging-port=9226` 启动） |
| 面板「额度已用」列一直显示 `—` | 守护还没跑过一轮额度巡检（等 `quota_sweep_secs`，默认 10 分钟），或探测被上游拒。手动触发一次：`python workmom_guard.py --quota-check`（零副作用）或加 `--probe` 实打一次；看 `logs/workmom.log` 里有没有「全号额度」相关行 |
| 面板刷新一下就不动了 / 按钮全失灵 | 0.5.2 已修（列数与数据长度不等导致 `_tick` 抛异常中断）。若仍出现：看 `logs/panel_error.log` 最新一行；确认跑的是新进程，`stop_all.bat` 后重起 |
| 日志里切号成功刚 10 秒就又报「额度 93% 超阈值但暂不动」 | 正常。切号瞬间反代进程还按旧号视角回 `/relay_state`，那几秒读到的仍是旧号残留读数；`current_quota` 有 uid 校验 + `quota_stale_since` 双重过滤，判不出当前号就 `None` 不动，不会瞎切。等反代刷新到新号即恢复（通常十几秒），想立刻确认跑 `--quota-check` |
| 每轮输入 token 都很大，像历史被重发了一整遍 | 先确认瘦身是开着的：`curl -s http://127.0.0.1:18793/health` 看 `slim.enabled` 与 `slim.requests` 有没有在涨；再看响应头 `X-WM-Slim-Pct`（省了多少百分比）。如果 `saved_est_total` 一直是 0，多半是 `config.json` 里 `slim_enabled=false` 或进程没重启 |
| 想确认「额度主动换号」到底判没判、为什么没换 | 跑 `python workmom_guard.py --quota-check`（零副作用，绝不切号）。它会逐条打印：阈值多少、当前号该模型已用多少、距上次换号多久、号池里有没有更干净的号，最后一句结论。四条件缺哪条一目了然 |
| 领积分说"已签到" | 正常，`data/checkin.json` 里已有当天记录，别重复请求 |
| 一直 6004 | 该账号该模型在滚动窗口里用完，看面板「限流倒计时」；换模型或等重置 |
| 弹 `Windows Script Host` / `run_guard.vbs 第2行` / `缺少对象: 's'` / `800A01A8` | **换行符不是 CRLF**。VBScript 的注释行只认 `CR` 结束，纯 `LF` 的文件会让第 1 行注释把 `Set s = ...` 吞掉，于是 `s` 从未创建。修复：所有 `.vbs`/`.bat` 必须是 **CRLF + 纯 ASCII**（无中文注释、无 BOM）。批量转法：`$t = ($t -replace "`r`n","`n") -replace "`n","`r`n"` 后按单字节编码写回；改完用 `cscript //nologo xxx.vbs` 干跑验证退出码 0 |
| 双击 `run_*.vbs` 没任何反应 | `logs/launch_error.log`：一定是找不到 `pythonw.exe`。把它写进同目录 `pythonw.txt`，或建 `.venv` |
| 反代在、`/health` 也 ok，但请求卡很久后回 **504 `cdp_timeout`** | WorkBuddy 渲染进程主线程卡死（CDP 浏览器级能连、页面级 `Runtime.evaluate` 超时）。错误文案里已列出排查顺序；重启 WorkBuddy，`Page.reload` 救不回来。超时时长用 `config.json` 的 `cdp_timeout_sec` 调（默认 300 秒） |
| `/health` 里 `cdp: error` | 看同响应里的 `hint`：WorkBuddy 没带 `--remote-debugging-port` 启动，或 `config.json` 的 `cdp_port` 跟实际不一致 |
| `/health` 里 `config_exists: false` / `wb_exe_exists: false` | 还没 `copy config.example.json config.json`，或 `wb_exe` 路径写错（面板切号要用它） |

当前只以本仓库为准。想回退到改造前的版本，用你自己 `backups\` 下的 `server.py.bak-*`（该目录不入库）。

## 八、License 与免责

MIT License，见 `LICENSE`。

- 本项目只是把**你自己机器上、你自己已经登录的**桌面客户端复用到本地回环口上，不破解、不分发任何凭证；
- 它不提供、也不绕过任何账号或额度限制，所有额度仍由 WorkBuddy 官方账号体系决定；
- 请自行确认你的使用方式符合 WorkBuddy 服务条款；由此产生的任何后果由使用者自负。
