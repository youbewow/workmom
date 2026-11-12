# 更新记录

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

setup 自检增强：
- 判断反代是否在跑时，除了 `/health` 通不通，还核对响应里的 `"app": "workmom"`，
  避免端口上蹲着别的程序被误判成「反代已就绪」
- 端口被**非 workmom** 的程序占着时，直接 FAIL 并提示换端口，不再盲目后台启动后干等超时

实测（副本 + `relay_port=18899`）：首次运行自动拉起并绑 18899，`/health` 正常，退出码 0；
重复运行识别「已在运行」不重复启动；双开同一端口退出码 2 并给出说明；
`relay_port` 指向 WorkBuddy 的 9226 时准确报「已被别的程序占用」，退出码 1。

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
