# 架构与实现要点

> 这份文档回答一个事：workmom 凭什么能用、每个部件在干什么、坏的时候该往哪看。

## 一、总体链路

```
Claude Code / Codex CLI / OpenAI SDK / Cline / Cherry Studio ...
        │  OpenAI Chat  ·  Anthropic Messages  ·  OpenAI Responses
        ▼
┌──────────────────────────────────────────────┐
│  server.py  127.0.0.1:18793                  │
│  ├─ resolve_model()   决定这次用哪个模型      │
│  ├─ relay_protocols   三协议 ↔ OpenAI Chat    │
│  └─ call_upstream()   经 CDP 打上游           │
└──────────────────────────────────────────────┘
        │  WebSocket (CDP) → Runtime.evaluate
        ▼
┌──────────────────────────────────────────────┐
│  WorkBuddy 渲染进程  127.0.0.1:9226           │
│  页面内 window.wb.http.post(...) ← 官方 SDK   │
└──────────────────────────────────────────────┘
        ▼
https://www.workbuddy.cn  （额度、风控、登录态全在官方侧）
```

## 二、为什么走 CDP，而不是直接 HTTP 打上游

| 做法 | 问题 |
| --- | --- |
| 直接 HTTP 打 `www.workbuddy.cn` | 要自己复刻签名、设备标识、风控头，官方一改就废；还要自己存 access/refresh token |
| 走 CDP 复用渲染进程 | **零凭证落地**：反代自己不持有任何 token，请求签名由官方客户端完成；页面里已经 hydrate 好的 `window.wb.http` 就是官方 SDK 实例 |

代价是上游是「单渲染进程」，天然串行：`server.py` 里对上游调用加了锁，多客户端同时打会排队。这也是已知边界，不是 bug。

## 三、协议适配层（relay_protocols.py）

三套协议最终都归一到 OpenAI Chat 的请求/响应结构，只有最外层壳子不同：

| 方向 | 函数 | 说明 |
| --- | --- | --- |
| Anthropic → Chat | `anthropic_to_openai` | `system` 字符串、`content` blocks（text/image/tool_use/tool_result）→ Chat 消息；`tools[].input_schema` → `function.parameters` |
| Chat → Anthropic | `openai_to_anthropic` | 反向；`tool_calls` → `tool_use` 块，`finish_reason` → `stop_reason`（有工具时 `tool_use`） |
| Anthropic 流式 | `anthropic_stream_events` | 构造 `message_start` / `content_block_delta` / `message_stop` |
| Responses → Chat | `responses_to_openai` | `instructions` → system；`input` 里 `message`/`function_call`/`function_call_output` 三类 item；扁平工具格式归一 |
| Chat → Responses | `openai_to_responses` / `_resp_output_items` | 输出 item 化，`max_output_tokens` 回填 |
| Responses 流式 | `responses_stream_events` | `response.created` → `output_item.added` → `output_text.delta` / `function_call_arguments.delta` → `response.completed` |
| token 估算 | `estimate_tokens` / `anthropic_count_input_tokens` | 本地按字符与 CJK 加权估，不打上游 |

Anthropic 的 `thinking` 块上游不认，转发前丢弃；若上游返回 reasoning，再包回 `thinking` 块给客户端。
内置工具类型（`web_search_20250305` 之类）跳过，只转自定义函数工具。
`/v1/messages?beta=true` 的 query 会被剥掉再路由。

## 四、模型是怎么定的（resolve_model）

优先级从高到低：

1. 请求头 `X-WM-Model: <模型名>` —— 临时覆盖，不改面板
2. 面板当前模型 `data/current_model.json`
3. 请求体里的 `model` —— **仅当**能被识别为 WorkBuddy 已知模型时才采用
4. 兜底 `default_model`

所以客户端里写 `claude-sonnet-4-5`、`gpt-5-codex` 这类名字不会被当真，一律走面板选的模型。这是刻意的：
**模型选择的唯一权威来源在面板**，客户端说什么都不算。

## 五、账号与模型为什么能解耦

- 账号态 = `data/accounts/<uid>.info`（抄的 WorkBuddy 登录文件）+ 当前 `auth` 文件指向谁
- 模型态 = `data/current_model.json`
- 两边的写路径完全不相交：切号只写登录文件 + reload 页面；切模型只写一个 JSON

结果就是 README 里那两条硬规则。改这两块代码时**不要**让它们互相 touch，这是整个项目的设计红线。

## 六、凭据与加密（workmom_core.py）

WorkBuddy 的 `.info` 登录文件不是明文，是一棵被信封加密过的树：

- 算法：AES-256-GCM，`sym-v1` 帧头 + 自定义 AAD（`envelope_aad`）
- 密钥：`SHA256(atRestSecretKey)`
- `atRestSecretKey` 拿不到磁盘文件，只能用 WorkBuddy 自带的 electron binding：以 `ELECTRON_RUN_AS_NODE` 起一个探针进程（`_PROBE_SRC` / `data/_keyprobe.js`），让官方代码自己把 key 吐出来
- 解/加密只碰白名单字段 `CRED_KEYS`（accessToken / refreshToken / 设备信息等），其余原样保留，回写时用 `encrypt_tree(value, template)` 对着原结构封回去，避免把文件写坏
- key 有缓存，失败后 60 秒再试（`KEY_RETRY_SECONDS`）

## 七、无感切号（seamless_switch）

```
write_login_from_backup(uid)      把目标号的 .info 写成当前 auth 文件
        ↓
清掉页面 URL 里的 accountSnapshot  否则 reload 完还是老号（这一步最容易漏）
        ↓
CDP Page.reload                  官方前端重新 hydrate，约 3 秒
        ↓
wb_profile() 校验当前 uid          确认真的换过来了
```

全程 WorkBuddy 主进程不重启。失败时才退到 `wd_api_switch`（WorkDaddy API）或 `restart_wb()` 兜底。

## 八、无感登录（OAuth）

`oauth_start()` 生成官方 state + 登录链接丢浏览器；`oauth_poll_once()` 按 state 轮询回调；
拿到 token 后用 `build_session` / `save_oauth_account` 落成标准 `.info` 进账号库。
**全程不退出、不打断正在用的 WorkBuddy。**

## 九、每日签到（幂等）

- 端点：`/billing/meter/daily-checkin` 与 `/v2/billing/meter/daily-checkin`，按 token 的 iss 选域名，多域名兜底
- 幂等：`data/checkin.json` 按 `uid + 日期` 记一条，重复调用直接跳过
- 回执归类 `classify_checkin`：已签到 / 活动未开放 / 未登录三种
- token 快过期（`min_ttl_h` 阈值）先 `ensure_fresh_token` 用 refresh_token 换新并回写 .info

## 十、限流守护（workmom_guard.py）

- **不主动探测**（探测要烧 token）。反代把失败写进内存态，守护每 5 秒读一次 `GET /relay_state`
- 命中 `429` / `code:6004` → 按 `queue` 顺序 `seamless_switch` 到下一个有额度的号 → 探一次确认 → 停手
- 冷却 `cooldown` 秒；整队全爆则 `backoff` 秒后再来
- 队列来自 `config.json.queue` 或 `data/guard_queue.json`，源码里不含任何账号 uid

## 十一、踩过的坑（改代码前务必看）

| 坑 | 现象 | 根因 / 处理 |
| --- | --- | --- |
| VBS 换行 | `run_guard.vbs 第2行` / `缺少对象: 's'` / `800A01A8` | VBScript 注释行只认 `CR` 结尾，纯 `LF` 会让第一行注释把 `Set s = ...` 吞掉。`.vbs`/`.bat` 必须 CRLF + 纯 ASCII + 无 BOM |
| CDP WebSocket 403 | 握手返回 `Rejected an incoming WebSocket connection from ... origin` | 必须绕开 Origin 校验：python 侧 `create_connection(..., suppress_origin=True)`，或给 WorkBuddy 加 `--remote-allow-origins=*` |
| 页面没开调试端口 | 什么都连不上 | WorkBuddy 启动必须带 `--remote-debugging-port=9226` |
| **渲染进程主线程卡死** | 反代报 `relay upstream failed: Connection timed out`，`/health` 里 `cdp: ok` 但请求全挂；用 CDP 连页面后 `Runtime.enable` / `Runtime.evaluate` 一律超时无响应 | 页面 JS 卡死（死循环/超大渲染），CDP 命令排不到主线程。`Page.reload` 也救不回来，唯一解是重启 WorkBuddy。判断法：浏览器级 `Browser.getVersion` 秒回、页面级 `Runtime.evaluate` 超时 = 渲染器卡死，不是网络问题 |
| 切号后还是老号 | 切了没效果 | 页面 URL 里的 `accountSnapshot` 没清 |
| 端口复用 | 反代起不来 | 旧进程占着 18793，跑 `stop_all.bat` |
| 日志膨胀 | 磁盘慢慢被吃 | 守护日志超 2MB 自动留尾部 2000 行 |

## 十二、自测清单

改完协议层 / 上游调用后，按这个顺序自证：

1. `GET /health` → `status: ok`、`cdp: ok`、`protocols` 三项齐全
2. `GET /v1/models` → 返回已知模型列表
3. `POST /v1/chat/completions` 非流式 + 流式各一发
4. `POST /v1/messages` 非流式 + 流式；带 `tools` 验证 `stop_reason: tool_use`
5. `POST /v1/messages/count_tokens` → 本地估算，不打上游
6. `POST /v1/responses` 非流式 + 流式
7. `X-WM-Model` 头覆盖验证
8. 面板切模型后再打一次，确认模型跟着变
9. 面板切账号后 `/health` 的 `uid` 变了、模型没变
