# 更新记录

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
