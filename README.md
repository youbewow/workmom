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

## 一、环境准备与首次部署

| 前提 | 要求 |
| --- | --- |
| 系统 | Windows 10/11（依赖 WorkBuddy 桌面端 + CDP） |
| Python | 3.9+（标准库 + `websocket-client`、`cryptography`） |
| WorkBuddy | 已安装并至少登录一个账号，且**启动时必须带 CDP 端口** |

**WorkBuddy 必须带调试端口启动**，否则一切免谈：

```powershell
# 常见安装位置，换成你自己的实际路径
& "$env:LOCALAPPDATA\Programs\WorkBuddy\WorkBuddy.exe" --remote-debugging-port=9226
```

验证：浏览器打开 `http://127.0.0.1:9226/json/version` 能返回 JSON 即通。想常驻就改快捷方式的目标，
在 exe 路径后加 ` --remote-debugging-port=9226`，或用 `boot_silent.vbs` 接管启动。

**部署四步**

```powershell
cd D:\workmom
pip install -r requirements.txt
copy config.example.json config.json     # 再改里面的 wb_exe 等本机路径
python server.py 18793                   # 反代
python dashboard.py                      # 总控面板（另开窗口）
```

想后台常驻：双击 `start_all.vbs`（反代 + 守护 + 面板），或把 `boot_silent.vbs` 的快捷方式丢进
`shell:startup` 做开机静默自启。

**pythonw 从哪来**：几个 `run_*.vbs` 按顺序找解释器——同目录 `pythonw.txt`（一行，写 pythonw.exe 的完整路径）
→ 同目录 `.venv\Scripts\pythonw.exe` → 系统 `PATH`。都没有时不会弹窗，而是把原因写进
`logs/launch_error.log`。装了 Marvis 之类自带 Python 的客户端，把它的 `pythonw.exe` 路径写进
`pythonw.txt` 就行。所有 `.vbs` 只依赖脚本自身所在目录，不写死盘符。

**哪些文件是机密**

`config.json`、`data/accounts/*.info`、`data/checkin.json`、`guard_state.json`、`rate_limit_ledger.json`
含账号凭证或本机路径，已在 `.gitignore` 里排除，**不要提交、不要截图外发**。
轮换队列里的账号 uid 也放在 `data/guard_queue.json`（不入库），格式见该文件注释。

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
| `relay_protocols.py` | 三协议转换层：Anthropic Messages / OpenAI Responses ↔ OpenAI Chat（含流式事件序列） |
| `config.example.json` | 配置模板，复制成 `config.json` 再改 |
| `pythonw.txt` | （可选，不入库）一行写死 pythonw.exe 路径，供 `.vbs` 启动器使用 |
| `requirements.txt` | 运行依赖，只有两个第三方包 |
| `docs/ARCHITECTURE.md` | 架构与实现要点：链路、协议层、加密、切号原理、踩过的坑、自测清单 |
| `CHANGELOG.md` | 更新记录 |
| `LICENSE` | MIT |

## 三、启动 / 停止

| 操作 | 方式 |
| --- | --- |
| 全量启动（反代 + 守护 + 面板） | 桌面 `workmom 一键启动.lnk` → `start_all.vbs` |
| 只开面板 | 桌面 `workmom 总控面板.lnk` → `run_panel.vbs` |
| 开机自启（静默，只反代+守护，不弹面板） | 启动文件夹 `workmom 自启.lnk` → `boot_silent.vbs` |
| 全停 | `stop_all.bat`（只杀 `D:\workmom` 下的 python 进程） |

> 所有 `.vbs` 都是**纯 ASCII**写的。别往里面加中文注释——WScript 按 GBK 解析会把下一行吞掉，报「缺少对象: 's'」。

## 四、面板怎么用

- **双击账号行** = 无感切到那个号（写登录文件 + CDP 刷新页面，**不重启 WorkBuddy**，约 3 秒）
  - 那个号正在限流里，会提示；5 秒内再双击一次 = 强切
- **双击模型行** = 把全部请求切到这个模型。跟换账号**完全无关**，切完账号还是原来那个号
- `+ 无感登录新号` = 生成官方 OAuth 链接并打开浏览器，登录完自动入库，全程不打断 WorkBuddy
- `一键领全部积分` = 给所有号领当日积分（凭证过期会先自动 refresh）
- `重启 WB(兜底)` = 只有无感切号失败、CDP 不可用时才用，会重启 WorkBuddy

## 五、两条硬规则（设计核心）

1. **选定模型后，换任何账号，模型都是当前这个。**
   模型只存在 `data/current_model.json`，切号逻辑从头到尾不碰它。
2. **选定账号后，改任何模型，账号都是当前这个。**
   切模型只写 `current_model.json`，不碰登录文件、不刷新页面、不动 WB。

## 六、反代接口

- `GET /health` —— 存活 + 当前模型 + 当前 uid + 模型清单
- `GET /relay_state` —— 运行状态（守护只读消费，零额外 token）
- `GET /v1/models` —— OpenAI 格式模型列表
- `POST /v1/chat/completions` —— OpenAI Chat 协议，`model` 字段留空则用当前模型
- `POST /v1/messages` —— Anthropic Messages 协议（Claude Code 等）
- `POST /v1/messages/count_tokens` —— Anthropic token 估算（本地算，不耗额度）
- `POST /v1/responses` —— OpenAI Responses 协议（Codex CLI 等）

三套协议共用同一上游调用，转换逻辑在 `relay_protocols.py`；接入配置见 `多协议接入说明.md`。
请求头 `X-WM-Model: <模型名>` 可临时覆盖面板模型。

上游 6004（频率限制）会原样透出 `code/msg`，不再吞成空 502。

## 七、排障

| 现象 | 先看哪 |
| --- | --- |
| 反代没起来 / `端口监听 False` | `logs/relay.out.log`、`logs/relay.err.log`；多半是端口被旧进程占着，跑 `stop_all.bat` 再起 |
| 切号没反应 | `logs/workmom.log` 里找「无感切号完成」；失败时看是不是 CDP 9226 不通（WorkBuddy 没带 `--remote-debugging-port=9226` 启动） |
| 领积分说"已签到" | 正常，`data/checkin.json` 里已有当天记录，别重复请求 |
| 一直 6004 | 该账号该模型在滚动窗口里用完，看面板「限流倒计时」；换模型或等重置 |
| 弹 `Windows Script Host` / `run_guard.vbs 第2行` / `缺少对象: 's'` / `800A01A8` | **换行符不是 CRLF**。VBScript 的注释行只认 `CR` 结束，纯 `LF` 的文件会让第 1 行注释把 `Set s = ...` 吞掉，于是 `s` 从未创建。修复：所有 `.vbs`/`.bat` 必须是 **CRLF + 纯 ASCII**（无中文注释、无 BOM）。批量转法：`$t = ($t -replace "`r`n","`n") -replace "`n","`r`n"` 后按单字节编码写回；改完用 `cscript //nologo xxx.vbs` 干跑验证退出码 0 |
| 双击 `run_*.vbs` 没任何反应 | `logs/launch_error.log`：一定是找不到 `pythonw.exe`。把它写进同目录 `pythonw.txt`，或建 `.venv` |
| 反代在、`/health` 也 ok，但请求全 60s 超时 | 大概率是 WorkBuddy 渲染进程主线程卡死（CDP 浏览器级能连、页面级 `Runtime.evaluate` 超时）。重启 WorkBuddy，`Page.reload` 救不回来 |

旧目录 `D:\K3.1pro反代`（原 K3.1-pro 反代 + 老面板 + 老守护）已于 2026-11-12 删除，当前只以 `D:\workmom` 为准；需要回退可用 `backups\` 下的 `server.py.bak-*`。

## 八、License 与免责

MIT License，见 `LICENSE`。

- 本项目只是把**你自己机器上、你自己已经登录的**桌面客户端复用到本地回环口上，不破解、不分发任何凭证；
- 它不提供、也不绕过任何账号或额度限制，所有额度仍由 WorkBuddy 官方账号体系决定；
- 请自行确认你的使用方式符合 WorkBuddy 服务条款；由此产生的任何后果由使用者自负。
