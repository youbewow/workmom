---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 9786fb266a4a0ba7e741af4014ef6d27_05237d1fde5211f19280525400393706
    ReservedCode1: XO/fHg9sVibmzyZBp3WfGJSVnjDSPxZGBLCfZUmnrf7mlldWqShPaTuOA/uH74KxMbpq+zrqhauBWRAr+cUP2V8KSMzPaGAZVctvQt/OIR72QS3Tjfs4EQd3UE1VxNOWoxpdkBYXkTKaezdbi+CHL3eIh382hXPGELyBZm7mNLDak7xNHGWCpUW6S2I=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 9786fb266a4a0ba7e741af4014ef6d27_05237d1fde5211f19280525400393706
    ReservedCode2: XO/fHg9sVibmzyZBp3WfGJSVnjDSPxZGBLCfZUmnrf7mlldWqShPaTuOA/uH74KxMbpq+zrqhauBWRAr+cUP2V8KSMzPaGAZVctvQt/OIR72QS3Tjfs4EQd3UE1VxNOWoxpdkBYXkTKaezdbi+CHL3eIh382hXPGELyBZm7mNLDak7xNHGWCpUW6S2I=
---

# 更新记录

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
- 旧目录 `D:\K3.1pro反代` 下线，回退靠 `backups\server.py.bak-*`

## 0.1.0 — 2026-11-11

**首版：本地反代 + 自动切号守护**

- `server.py` 经 CDP 复用 WorkBuddy 登录态，在 `127.0.0.1:18793` 提供 OpenAI 兼容接口
- `workmom_guard.py` 盯 `/relay_state`，遇 429/6004 自动按额度队列换号
- `dashboard.py` 总控面板、`start_all.vbs`/`stop_all.bat` 一键启停、`boot_silent.vbs` 开机静默自启
*（内容由AI生成，仅供参考）*
