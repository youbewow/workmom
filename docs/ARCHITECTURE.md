# 架构与实现要点

> 这份文档回答一个事：workmom 凭什么能用、每个部件在干什么、坏的时候该往哪看。

## 一、总体链路

```
Claude Code / Codex CLI / OpenAI SDK / Cline / Cherry Studio ...
        │  OpenAI Chat  ·  Anthropic Messages  ·  OpenAI Responses
        ▼
┌───────────────────────────────────────────────────────────────┐
│  server.py  127.0.0.1:18793                                   │
│  ├─ resolve_model()    决定这次用哪个模型                      │
│  ├─ relay_stream.py    增量流式翻译（来一个 chunk 吐一个）      │
│  ├─ wm_transport.py    直连通道（首选）                        │
│  ├─ call_upstream()    回落通道（经 CDP 打上游）               │
│  └─ _watchdog()        看门狗：探测渲染进程是否卡死             │
└───────────────────────────────────────────────────────────────┘
        │ ① 直连（默认）                        │ ② 回落（CDP）
        │    httpx + Bearer accessToken          │    WebSocket → Runtime.evaluate
        ▼                                        ▼
https://www.workbuddy.cn  ◄────  WorkBuddy 渲染进程 127.0.0.1:9226
（额度、风控、登录态全在官方侧）     页面内 window.wb.http.post(...) = 官方 SDK
```

两条通道的关系（0.5.0 起）：

| 通道 | 什么时候用 | 流式 | 并发 | 依赖渲染进程 |
| --- | --- | --- | --- | --- |
| 直连（`wm_transport`） | 默认（`transport=auto` / `direct`） | **真流式** | 无锁，`max_concurrency` 限流 | 否 |
| CDP 回落（`call_upstream`） | 直连不可用（token 读不出 / 网络异常）、或 `transport=cdp` | 伪流式（全量后切片） | 恒为 1（渲染进程单主线程） | 是 |

## 二、为什么还留着 CDP，以及 0.5.0 为什么敢直连

| 做法 | 问题 |
| --- | --- |
| 直接 HTTP 打 `www.workbuddy.cn` | 要自己复刻签名、设备标识、风控头，官方一改就废；还要自己存 access/refresh token |
| 走 CDP 复用渲染进程 | **零凭证落地**：反代自己不持有任何 token，请求签名由官方客户端完成；页面里已经 hydrate 好的 `window.wb.http` 就是官方 SDK 实例 |

代价是上游是「单渲染进程」，天然串行：`server.py` 里对上游调用加了锁，多客户端同时打会排队。这也是已知边界，不是 bug。

**0.5.0 的变化**：实测两件事让「直连」从冒险方案变成更优解——

1. 凭证本来就在本机：`workmom_core.ensure_fresh_token(uid)` 能拿明文 `accessToken`（面板切号、签到、
   守护探活一直在用），所以「零凭证落地」是相对优势，不是绝对红线；
2. **上游本身就是真流式**：拿 token 直连 `/v2/chat/completions` 拿到的是
   `Content-Type: text/event-stream`、首字节 2.18s。之前的「伪流式」是反代自己把 SSE 聚合了，
   不是上游不支持。

现在的关系是：**直连优先，CDP 兜底**。渲染进程从「唯一通道」降级成「备用通道 + 登录态维护者」
（切号仍靠它刷新页面）。它卡死时直连照样出字，这才是 P0-3 真正的解法。

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

### 三·五、增量流式（relay_stream.py）

老的做法是「上游聚合 → 转成完整响应 → 再按 512 字符切片发 SSE」，客户端看到的节奏是
「长时间没动静 → 一瞬间全出来」。0.5.0 起换成增量翻译：

| 类 | 用途 | 关键点 |
| --- | --- | --- |
| `OpenAIPassthrough` | `/v1/chat/completions?stream=1` | 上游 chunk 基本原样透传，只补齐 `model` / `id` / `first_chunk` 的 role |
| `AnthropicIncremental` | `/v1/messages?stream=1` | `message_start` 先发（带 role + 空 content），每个文本增量发一个 `content_block_delta`，工具调用单独起 block，收尾 `message_delta` + `message_stop` |
| `ResponsesIncremental` | `/v1/responses?stream=1` | `response.created` → `output_item.added` → 每个增量一个 `response.output_text.delta` → `response.completed` |

统一接口是 `start_events()` / `feed(chunk)` / `finish_events()`，`feed()` 返回本次要发的字节，
**有就发、没有就返回空**，Python 层直接 `write + flush`，不做任何缓冲聚合。
`finish_events()` 负责补 `tool_calls` 的收尾 index、`finish_reason` 与 `[DONE]`。

断流保护：`stream_idle_timeout_sec`（默认 180s）内一个字节都没来就判死并正常收尾，避免客户端无限挂。

### 三·六、错误怎么透传（0.5.0 修的重点）

旧版最坑的一点：上游回 429 / `code 6004`（模型频率限制）时，JS 侧 `catch` 取 `e.response.data`
拿到空对象，反代把 `detail` 拼成空串再以 **502 + 空 body** 返回。客户端只能显示
「自定义模型调用失败，原因暂不明确」，用户根本看不出是额度问题 —— 排障全靠猜。

现在的链路：

```
上游 429 {"code":6004,"msg":"...重置时间 2026-11-13 05:56:45 UTC+8 ..."}
   → wm_transport.parse_error(status, body)
       → UpstreamError(http=429, type="rate_limit_error", code="6004",
                       reset_at="2026-11-13 05:56:45", message=上游原文)
   → 按协议回 HTTP 429 + 结构化 body + Retry-After + X-WM-Quota-Exceeded: 1
```

`reset_at` 是用正则从中文原文里抠出来的（`reset time: ...` / `将在 ... 重置`），算不出就不编，
宁可只给 `message`。另外：

- **模型级熔断**：命中 6004 的模型记下重置时间，之前所有请求直接跳过它，不再每请求都去撞一次墙
- **自动降级**：`fallback_models` 顺序换下一个没用满的模型，客户端拿到 200 且内容正常；
  日志里写清「请求模型 A → 实际模型 B」
- 全链都限时才把最后一个模型的 429 原样吐给客户端，带真实重置时间

### 三·七、看门狗（P0-3）

渲染进程主线程卡死的表征是：CDP 浏览器级 `Browser.getVersion` 秒回，页面级 `Runtime.evaluate`
永远不回。所以探活分三层：端口 TCP → `/json/list` 拿页面 → 页面里 evaluate 一个 `1+1`。
第三层失败才算「卡死」。

`auto_heal` 三档：`off` / `report`（默认，只把状态写进 `/health.cdp_health`）/ `restart`。
`restart` 要同时满足「连续 2 次探活失败」和「直连最近 5 分钟没有一次成功」才动手，
避免把还能用的环境来回重启。重启动作复用 `workmom_core.restart_wb()`（`taskkill` + 带
`--remote-debugging-port` 重新拉起）。

`/health` 自己也依赖 CDP 探活，旧版在渲染进程卡死时**连 `/health` 都跟着超时**（探活把 handler 拖死）。
现在 `/health` 只读看门狗缓存，缓存超过 120s 才现场探一次，且现场探活上限 4 秒。

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
| **渲染进程主线程卡死** | 反代报 `relay upstream failed: Connection timed out`，`/health` 里 `cdp: ok` 但请求全挂；用 CDP 连页面后 `Runtime.enable` / `Runtime.evaluate` 一律超时无响应 | 页面 JS 卡死（死循环/超大渲染），CDP 命令排不到主线程。`Page.reload` 也救不回来。判断法：浏览器级 `Browser.getVersion` 秒回、页面级 `Runtime.evaluate` 超时 = 渲染器卡死，不是网络问题。**0.5.0 起**：直连通道不受影响，请求照常出字；看门狗会把状态写进 `/health.cdp_health`，`auto_heal=restart` 时可自动 `taskkill` + 带调试端口重启 WorkBuddy |
| 上游限流但客户端「看不出原因」 | 客户端只显示「原因暂不明确 / 未知错误」 | 0.4.x 的老坑：429 被兜成 502 空 body。0.5.0 起翻译成 429 + 结构化 body（`code` / `reset_at` / `Retry-After`），并先按 `fallback_models` 自动降级。若仍是空 502，说明跑的是旧进程 |
| 切号后还是老号 | 切了没效果 | 页面 URL 里的 `accountSnapshot` 没清 |
| 端口复用 | 反代起不来，日志报 `端口 xxxx 已有程序在监听` | `server.py` 启动前会探测，被占就 `exit(2)` 不硬绑。先跑 `stop_all.bat`；若是别的软件占着，改 `config.json` 的 `relay_port` 换端口（**只改这一处**，`run_relay.vbs` / 守护 / 面板 / 快捷方式都跟着走） |
| 改了 `relay_port` 不生效 | 配置写 18899，实际仍监听 18793 | 0.4.0 及更早版本的坑：`run_relay.vbs` 写死 `server.py 18793` 而 `server.py` 只认命令行参数。已在 0.4.1 修复，端口优先级为 命令行参数 → `config.json.relay_port` → 18793 |
| 日志膨胀 | 磁盘慢慢被吃 | 守护日志超 2MB 自动留尾部 2000 行 |

## 十二、自测清单

**0.5.0 起先跑自动化那套**，一条命令，13 项：

```
python tests/test_relay.py                                  # 临时端口自起实例，跑完自动收摊
python tests/test_relay.py --url http://127.0.0.1:18793     # 只测已在跑的，不动进程
```

覆盖：`/health` 字段 / 非流式 / 三协议真流式（首字节 vs 总时长）/ 限额透传 / 6004 翻译 /
错误体结构化 / 流式 `[DONE]` / 3 路并发（墙钟 < 串行相加）/ 用量账本落盘 / CDP 探活 / Web 面板。
需要本机 WorkBuddy 已登录（会真打上游）；离线时只有 F2、F3、J 能过。

改完协议层 / 上游调用后，再按顺序手工补这几条：

1. `GET /health` → `status: ok`、`cdp: ok`、`transport_ready` 两项齐全、`protocols` 三项齐全
2. `GET /v1/models` → 返回已知模型列表
3. `POST /v1/chat/completions` 非流式 + 流式各一发
4. `POST /v1/messages` 非流式 + 流式；带 `tools` 验证 `stop_reason: tool_use`
5. `POST /v1/messages/count_tokens` → 本地估算，不打上游
6. `POST /v1/responses` 非流式 + 流式
7. `X-WM-Model` 头覆盖验证
8. 面板切模型后再打一次，确认模型跟着变
9. 面板切账号后 `/health` 的 `uid` 变了、模型没变
10. 故意把某模型打到 6004（或等自然限流），确认客户端看到的是 429 + 重置时间，而不是空 502
11. 浏览器打开 `http://127.0.0.1:18793/` → 面板出得来，限流冷却区块有数据
