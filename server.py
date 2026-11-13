#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
workmom —— WorkBuddy -> 多协议本地反代（模型由 workmom 面板选定，与账号完全解耦）

原理：
  复用 WorkBuddy 桌面端已登录的渲染进程（CDP 127.0.0.1:9226），
  在页面内调用 window.wb.http.post('/v2/chat/completions', body)，
  把返回的 SSE 文本聚合后，按客户端需要的协议吐回去。

支持的上层协议（同一端口全开，客户端按需选）：
  OpenAI Chat Completions   POST /v1/chat/completions   Marvis / Cherry Studio / 任意 OpenAI 兼容客户端（DSH 等）
  Anthropic Messages        POST /v1/messages           Claude Code / claude-code-router / Anthropic SDK
  Anthropic count_tokens    POST /v1/messages/count_tokens
  OpenAI Responses          POST /v1/responses          Codex CLI / 新版 openai SDK
  GET  /v1/models
  GET  /health

用法：
  python server.py [port]        默认端口 18793
"""

import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import re

import websocket

import relay_protocols as RP

# 直连传输层（P0-1 真流式 / P0-2 并发 / P0-3 卡死免疫 / P0-0 错误透传）
# 导入失败不致命：自动退回 CDP 通道（老行为），保证任何环境都能起来
try:
    import wm_transport as WT
except Exception as _e:                # pragma: no cover
    WT = None
    _WT_IMPORT_ERR = str(_e)
# 增量流式翻译器（把上游每个 chunk 实时翻成三种客户端协议）
try:
    import relay_stream as RS
except Exception as _e:                # pragma: no cover
    RS = None
    _RS_IMPORT_ERR = str(_e)

# pythonw.exe 下 sys.stdout 为 None，print 会崩，兜底重定向到日志
if sys.stdout is None or sys.stderr is None:
    try:
        _logdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        os.makedirs(_logdir, exist_ok=True)
        if sys.stdout is None:
            sys.stdout = open(os.path.join(_logdir, "relay.out.log"), "a", encoding="utf-8", buffering=1)
        if sys.stderr is None:
            sys.stderr = open(os.path.join(_logdir, "relay.err.log"), "a", encoding="utf-8", buffering=1)
    except Exception:
        pass

CDP_HOST = "127.0.0.1"
CDP_PORT = 9226
UPSTREAM_PATH = "/v2/chat/completions"
MIN_COMPLETION_TOKENS = 4096
LOCK = threading.Lock()

# workmom 核心（模型状态 / 账号 / 密钥 / 签到全在这里，反代只做协议转换）
try:
    import workmom_core as WM
except Exception:                      # 极端情况下退回静态配置
    WM = None


def _known_models():
    if WM is not None:
        try:
            m = WM.known_models()
            if m:
                return m
        except Exception:
            pass
    return ["kimi-k3.1-pro", "kimi-k3-2"]


def _active_model():
    if WM is not None:
        try:
            return WM.active_model()
        except Exception:
            pass
    return "kimi-k3.1-pro"


# ----------------------------------------------------------------------------
# 读 config.json —— 陌生机器上唯一需要改的就是这里（cdp_port / relay_port / wb_exe）
# 之前 CDP_PORT 写死 9226，别人改了 config.json 的 cdp_port 反代会连不上，故改成读配置
# ----------------------------------------------------------------------------
def _cfgval(key, default=None):
    if WM is not None:
        try:
            return (WM.cfg() or {}).get(key, default)
        except Exception:
            pass
    return default


try:
    CDP_PORT = int(_cfgval("cdp_port", CDP_PORT) or CDP_PORT)
except Exception:
    pass
try:
    CDP_TIMEOUT_SEC = float(_cfgval("cdp_timeout_sec", 300) or 300)
except Exception:
    CDP_TIMEOUT_SEC = 300.0
UPSTREAM_PATH = str(_cfgval("upstream_path", UPSTREAM_PATH) or UPSTREAM_PATH)

# ---------------------------------------------------------------------------
# P0 运行时配置（全部可在 config.json 覆盖，缺省即可用）
# ---------------------------------------------------------------------------
# transport: direct=永远走直连; cdp=永远走渲染进程; auto=直连优先、失败自动回落 CDP
TRANSPORT = str(_cfgval("transport", "auto") or "auto").lower()
try:
    MAX_CONCURRENCY = max(1, int(_cfgval("max_concurrency", 16) or 16))
except Exception:
    MAX_CONCURRENCY = 16
try:
    DIRECT_TIMEOUT = float(_cfgval("direct_timeout_sec", 300) or 300)
except Exception:
    DIRECT_TIMEOUT = 300.0
# 限流自动降级链：只在「429/6004 且可降级」时按顺序换模型重试，永不改变客户端协议
FALLBACK_MODELS = _cfgval("fallback_models", []) or []
if isinstance(FALLBACK_MODELS, str):
    FALLBACK_MODELS = [x.strip() for x in FALLBACK_MODELS.split(",") if x.strip()]
# auto_heal: report=只上报+记日志; restart=连续探活失败自动重启 WorkBuddy; off=关
AUTO_HEAL = str(_cfgval("auto_heal", "report") or "report").lower()
try:
    HEAL_INTERVAL = max(15, int(_cfgval("heal_interval_sec", 60) or 60))
except Exception:
    HEAL_INTERVAL = 60
try:
    QUOTA_WARN_PERCENT = float(_cfgval("quota_warn_percent", 80) or 80)
except Exception:
    QUOTA_WARN_PERCENT = 80.0
# 流式空闲超时：两行 SSE 之间允许的最长静默（秒），防客户端死等
try:
    STREAM_IDLE_TIMEOUT = float(_cfgval("stream_idle_timeout_sec", 180) or 180)
except Exception:
    STREAM_IDLE_TIMEOUT = 180.0

# ---- 输入 token 审计 -------------------------------------------------------
# audit_requests > 0 → 把接下来 N 次请求的「输入到底塞了什么」dump 到 logs/req_audit.jsonl，
# 抓完自动归零。也可以临时用请求头 X-WM-Audit: 1 强制抓一条。0=关闭，不产生任何开销。
try:
    AUDIT_REQUESTS = max(0, int(_cfgval("audit_requests", 0) or 0))
except Exception:
    AUDIT_REQUESTS = 0
AUDIT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "req_audit.jsonl")
_AUDIT_LOCK = threading.Lock()
_AUDIT_LEFT = [AUDIT_REQUESTS]

# ---- 额度阈值自动换号 ------------------------------------------------------
# 反代只负责「如实报告额度」，做不做换号由 workmom_guard 决定，两边靠 /relay_state 解耦。
# 这里把当前模型的额度百分比塞进每个响应的头里，客户端（含守护）零成本读取。
try:
    QUOTA_SWITCH_PERCENT = float(_cfgval("quota_switch_percent", 92) or 92)
except Exception:
    QUOTA_SWITCH_PERCENT = 92.0

# 并发闸门：ThreadingHTTPServer 本身不限制线程数，上游有账号级频率限制，
# 无节制并发只会把 6004 提前触发，所以用信号量做「排队而不是被拒」
_CONC_SEM = threading.BoundedSemaphore(MAX_CONCURRENCY)

# 直连通道健康度（供 /relay_state 与自愈判断）
_DIRECT_LOCK = threading.Lock()
_DIRECT_STAT = {
    "ok": 0, "fail": 0,
    "last_ok_at": 0, "last_fail_at": 0,
    "last_err": "", "last_ttfb_ms": 0,
    "active": 0,             # 当前在途直连流
}
# CDP 通道探活状态（自愈依据）
_CDP_HEALTH = {"ok": None, "fail_streak": 0, "last_check": 0, "last_err": "",
               "healed_at": 0, "heal_count": 0}

DEFAULT_MODEL = (WM.active_model() if WM is not None else "kimi-k3.1-pro")
KNOWN_MODELS = _known_models()


class CdpTimeout(RuntimeError):
    """CDP 上游超时：WorkBuddy 渲染进程无响应，或没带 --remote-debugging-port 启动。"""

# ----------------------------------------------------------------------------
# 运行状态记录（供 auto_switch.py 守护进程只读消费，零额外 token 成本）
# ----------------------------------------------------------------------------
AUTH_FILE = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local"),
    "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info")

_ERR_LOCK = threading.Lock()
LAST_ERRORS = []          # 最近 20 条上游失败记录
_LAST_OK = [0.0]          # 最近一次成功时间戳
_START_AT = [time.time()]
REQ_STATS = {}            # 各协议调用计数：{"chat": n, "anthropic": n, "responses": n}


def _bump(kind):
    REQ_STATS[kind] = REQ_STATS.get(kind, 0) + 1


def _current_uid():
    """明文读取当前登录账号 uid（不需要解密，account.uid 是明文）。"""
    try:
        with open(AUTH_FILE, encoding="utf-8") as f:
            return ((json.load(f) or {}).get("account") or {}).get("uid") or ""
    except Exception:
        return ""


def _record_upstream_error(model, res):
    detail = str(res.get("detail") or "")
    body = str(res.get("body") or "")
    blob = detail + " " + body
    entry = {
        "at": int(time.time()),
        "model": model,
        "status": res.get("status"),
        "kind": res.get("kind"),
        "code6004": ("6004" in blob),
        "is429": (res.get("status") == 429) or ("429" in detail),
        "detail": detail[:300],
        "body": body[:600],
    }
    with _ERR_LOCK:
        LAST_ERRORS.append(entry)
        del LAST_ERRORS[:-20]
    try:
        sys.stderr.write("[relay] upstream_error model=%s status=%s 6004=%s %s\n"
                         % (model, entry["status"], entry["code6004"], blob.strip()[:300]))
        sys.stderr.flush()
    except Exception:
        pass

# ----------------------------------------------------------------------------
# CDP 通道
# ----------------------------------------------------------------------------


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _find_page_ws():
    url = "http://%s:%d/json" % (CDP_HOST, CDP_PORT)
    with _OPENER.open(url, timeout=5) as r:
        targets = json.loads(r.read().decode("utf-8", "replace"))
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        raise RuntimeError("WorkBuddy 渲染页面没找到，确认 WorkBuddy 已启动且开着 --remote-debugging-port=%d" % CDP_PORT)
    # 优先选 WorkBuddy 主界面（页面 URL 里通常带 workbuddy / app.asar）
    pages.sort(key=lambda t: (1 if "index.html" in (t.get("url") or "") else 0,
                              1 if "workbuddy" in (t.get("url") or "").lower()
                              or "app.asar" in (t.get("url") or "") else 0),
              reverse=True)
    return pages[0]["webSocketDebuggerUrl"]


class CdpSession:
    def __init__(self):
        self.ws = websocket.create_connection(
            _find_page_ws(), timeout=CDP_TIMEOUT_SEC, max_size=128 * 1024 * 1024,
            skip_utf8_validation=True, suppress_origin=True)
        self.mid = 0

    def eval(self, expression, timeout_ms=None):
        if timeout_ms is None:
            timeout_ms = int(CDP_TIMEOUT_SEC * 1000)
        self.mid += 1
        mid = self.mid
        payload = {
            "id": mid,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "timeout": timeout_ms,
            },
        }
        self.ws.send(json.dumps(payload, ensure_ascii=False))
        deadline = time.time() + CDP_TIMEOUT_SEC
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise CdpTimeout("CDP %d 秒无响应，WorkBuddy 渲染进程可能已卡死" % int(CDP_TIMEOUT_SEC))
            try:
                self.ws.settimeout(min(left, 5.0))
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                raise RuntimeError("CDP 连接被关闭")
            msg = json.loads(raw)
            if msg.get("id") != mid:
                continue
            if "error" in msg:
                raise RuntimeError("CDP error: %s" % json.dumps(msg["error"], ensure_ascii=False)[:400])
            res = msg.get("result", {})
            if res.get("exceptionDetails"):
                desc = res["exceptionDetails"].get("exception", {}).get("description")
                raise RuntimeError("页面 JS 异常: %s" % str(desc or res["exceptionDetails"])[:600])
            rv = res.get("result", {})
            if rv.get("type") == "object" and "value" in rv:
                return rv["value"]
            return rv.get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


_CONN = None


def _get_conn(force_new=False):
    global _CONN
    if force_new and _CONN is not None:
        try:
            _CONN.close()
        except Exception:
            pass
        _CONN = None
    if _CONN is None:
        _CONN = CdpSession()
    return _CONN


# ----------------------------------------------------------------------------
# 页面内执行的 JS：调用上游并聚合
# ----------------------------------------------------------------------------

JS_TEMPLATE = r"""
(async () => {
  const body = %s;
  // 切号 / 页面刷新后 window.wb 会短暂缺失，Claude Code、Codex 不该看到这种抖动：
  // 先等页面就绪（最多 90 秒）再决定是否报错。
  let waited = 0;
  while (!(typeof window !== 'undefined' && window.wb && window.wb.http
           && typeof window.wb.http.post === 'function')) {
    if (waited >= 90000) {
      return { ok: false, kind: 'noready', detail: 'window.wb 未就绪（WorkBuddy 页面未加载完或未登录）' };
    }
    await new Promise(r => setTimeout(r, 250));
    waited += 250;
  }
  try {
    const txt = await window.wb.http.post(%s, body);
    if (typeof txt !== 'string') {
      return { ok: false, kind: 'nonstring', detail: String(txt).slice(0, 300) };
    }
    let content = '', reasoning = '', finish = '', upErr = null, usage = null;
    let model = body.model || '', id = '';
    const tcs = {};
    for (const raw of txt.split('\n')) {
      const line = raw.trim();
      if (!line.startsWith('data:')) continue;
      const payload = line.slice(5).trim();
      if (!payload || payload === '[DONE]') continue;
      let j;
      try { j = JSON.parse(payload); } catch (e) { continue; }
      if (j.id) id = j.id;
      if (j.model) model = j.model;
      if (j.usage) usage = j.usage;
      if (j.error) { upErr = j.error; continue; }
      const ch = (j.choices || [])[0];
      if (!ch) continue;
      const d = ch.delta || {};
      if (typeof d.content === 'string' && d.content) content += d.content;
      if (typeof d.reasoning_content === 'string' && d.reasoning_content) reasoning += d.reasoning_content;
      if (Array.isArray(d.tool_calls)) {
        for (const tc of d.tool_calls) {
          const idx = (tc.index === undefined || tc.index === null) ? 0 : tc.index;
          if (!tcs[idx]) tcs[idx] = { id: '', type: 'function', function: { name: '', arguments: '' } };
          if (tc.id) tcs[idx].id = tc.id;
          if (tc.type) tcs[idx].type = tc.type;
          if (tc.function) {
            if (tc.function.name) tcs[idx].function.name = tc.function.name;
            if (tc.function.arguments) tcs[idx].function.arguments += tc.function.arguments;
          }
        }
      }
      if (ch.finish_reason) finish = ch.finish_reason;
    }
    const toolCalls = Object.keys(tcs).sort((a, b) => a - b).map(k => tcs[k]);
    if (!id) id = 'wb-' + Date.now();
    return {
      ok: true, id, model, content, reasoning, finish,
      tool_calls: toolCalls,
      usage: usage ? {
        prompt_tokens: usage.prompt_tokens || 0,
        completion_tokens: usage.completion_tokens || 0,
        total_tokens: usage.total_tokens || 0
      } : null,
      upstream_error: upErr,
      raw_len: txt.length
    };
  } catch (e) {
    const m = String(e && e.message || e);
    if (m.indexOf("reading 'http'") >= 0 || m.indexOf('reading "http"') >= 0) {
      return { ok: false, kind: 'noready', detail: m.slice(0, 300) };
    }
    return {
      ok: false, kind: 'http', detail: m.slice(0, 300),
      status: (e && e.httpStatus) || null,
      body: JSON.stringify(e && e.response && e.response.data || '').slice(0, 500)
    };
  }
})()
"""


def call_upstream(body):
    """在 WorkBuddy 渲染进程里发起一次 chat 请求，返回聚合结果 dict。"""
    js = JS_TEMPLATE % (json.dumps(json.dumps(body, ensure_ascii=False), ensure_ascii=False),
                        json.dumps(UPSTREAM_PATH))
    with LOCK:
        try:
            conn = _get_conn()
            res = conn.eval(js)
        except CdpTimeout:
            # 超时重试只会让客户端再干等一轮，直接向上抛，由 HTTP 层回 504 说清原因
            raise
        except Exception as e:
            msg = str(e)
            # 页面 JS 真实报错不重试；其余（连接断开/页面刷新/WorkBuddy 重启）一律重连后重试一次
            if msg.startswith("页面 JS 异常"):
                raise
            conn = _get_conn(force_new=True)
            return conn.eval(js)
        # 页面刚 reload，CDP 还连在旧执行上下文上：换新连接再要一次（JS 内已自带就绪等待）
        if isinstance(res, dict) and not res.get("ok") and res.get("kind") in ("noready", "nonstring"):
            try:
                conn = _get_conn(force_new=True)
                return conn.eval(js)
            except Exception:
                return res
        return res


# ----------------------------------------------------------------------------
# 模型选择 / 上游请求构造（三种协议共用）
# ----------------------------------------------------------------------------

def _pick_name(name):
    """把客户端传来的模型名归一：去掉 provider 前缀 / 版本后缀，命中 known_models 才认。"""
    km = _known_models()
    t = str(name or "").strip().split("/")[-1].split(":")[-1].strip()
    return t if t in km else ""


def resolve_model(hdr_model=None, req_model=None):
    """模型归属：面板选定的模型优先（与账号完全无关）；
    客户端想临时覆盖可带请求头 X-WM-Model: <模型名>，其次才看请求体 model。"""
    return _pick_name(hdr_model) or _pick_name(_active_model()) or _pick_name(req_model) or DEFAULT_MODEL


# ---------------------------------------------------------------- 输入精简（slim）
# 目的：客户端每轮把整段对话重发一遍（含旧思考、重复块、超长工具输出），
# 输入 token 是输出的上百倍。这里在上游转发前做【不改变语义】的瘦身：
#   1) 剥掉历史 assistant 的 reasoning_content（旧思考对下一轮毫无用处，占量却很大）
#   2) 多条 system 合并成一条（顺序与内容不变，只是少一层结构）
#   3) 删掉 content 为空又无 tool_calls 的废消息
#   4) 完全相同且足够长的文本块，第二次起替换为引用标记
#   5) 可选：超长 tool 结果按头尾保留截断（默认关，开启需显式配置）
SLIM_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "slim.jsonl")
_SLIM_LOCK = threading.Lock()
_SLIM_STAT = {"n": 0, "saved_est": 0, "last": {}}


def _slim_cfg():
    return {
        "enabled": bool(_cfgval("slim_enabled", True)),
        "strip_reasoning": bool(_cfgval("slim_strip_reasoning", True)),
        "merge_system": bool(_cfgval("slim_merge_system", True)),
        "dedupe_min_chars": int(_cfgval("slim_dedupe_min_chars", 3000) or 0),
        "para_min_chars": int(_cfgval("slim_para_min_chars", 500) or 0),
        "tool_max_chars": int(_cfgval("slim_tool_max_chars", 0) or 0),
        "old_tool_chars": int(_cfgval("slim_old_tool_chars", 1500) or 0),
        "keep_recent_tools": int(_cfgval("slim_keep_recent_tools", 3) or 0),
        "keep_last_assistant_reasoning": bool(_cfgval("slim_keep_last_reasoning", False)),
    }


def _slim_truncate(text, limit):
    """头 65% + 尾 25% 保留，中间标注省略量。"""
    if limit <= 0 or len(text) <= limit:
        return text, 0
    head = int(limit * 0.65)
    tail = int(limit * 0.25)
    cut = len(text) - head - tail
    marker = "\n\n…[workmom 已省略中间 %d 字符，保持头尾原文]…\n\n" % cut
    return text[:head] + marker + text[-tail:], cut


def _slim_content(c, cfg, seen, idx, role, stat):
    """处理单条 content（str 或多模态 list），返回 (新content)。副作用写进 stat。"""
    if isinstance(c, str):
        return _slim_str(c, cfg, seen, idx, role, stat)
    if isinstance(c, list):
        out = []
        for p in c:
            if not isinstance(p, dict):
                out.append(p)
                continue
            if p.get("type") == "text" and isinstance(p.get("text"), str):
                q = dict(p)
                q["text"] = _slim_str(p["text"], cfg, seen, idx, role, stat)
                out.append(q)
            else:
                out.append(p)
        return out
    return c


def _slim_paragraphs(text, min_len):
    """段级去重：同一条消息内部若有完全相同的大段（常见于客户端把同一份说明/策略重复拼进 system），
    第二次起替换成一行占位说明。只动 >=min_len 且逐字相同的段，语义零损失。"""
    if min_len <= 0 or len(text) < min_len * 2 or "\n\n" not in text:
        return text, 0
    parts = text.split("\n\n")
    if len(parts) < 3:
        return text, 0
    seen = {}
    out = []
    saved = 0
    for p in parts:
        key = p.strip()
        if len(key) >= min_len:
            h = hash(key)
            hit = seen.get(h)
            if hit and hit[1] == key:
                mark = "[workmom 精简：本段与上文第 %d 段逐字相同（%d 字符），已省略]" % (hit[0] + 1, len(key))
                out.append(mark)
                saved += len(key) - len(mark)
                continue
            if not hit:
                seen[h] = (len(out), key)
        out.append(p)
    if saved <= 0:
        return text, 0
    return "\n\n".join(out), saved


def _slim_str(text, cfg, seen, idx, role, stat=None):
    if not text:
        return text
    if stat is None:
        stat = {}
    # 超长工具/文档结果截断（默认关，slim_tool_max_chars>0 才生效）
    if role == "tool":
        new, cut = _slim_truncate(text, cfg["tool_max_chars"])
        if cut > 0:
            stat["tool_truncated"] = stat.get("tool_truncated", 0) + 1
            text = new
    # 长文本去重：同一段完全相同的长文本，第二次出现只留引用
    minc = cfg["dedupe_min_chars"]
    if minc > 0 and len(text) >= minc:
        h = hash(text)
        if h in seen:
            mark = "[workmom 精简：此处内容与第 %d 条完全相同（%d 字符），已省略]" % (seen[h], len(text))
            stat["deduped"] = stat.get("deduped", 0) + 1
            return mark
        seen[h] = idx
    # 段级去重（防同一条 system 里重复拼接同一份内容）
    new, ps = _slim_paragraphs(text, cfg["para_min_chars"])
    if ps > 0:
        stat["para_deduped"] = stat.get("para_deduped", 0) + 1
        text = new
    return text


def slim_messages(msgs, model=""):
    """对 messages 做语义无损瘦身，返回 (新msgs, stats)。stats 供日志与响应头使用。"""
    cfg = _slim_cfg()
    if not cfg["enabled"] or not isinstance(msgs, list):
        return msgs, {"enabled": False, "before": 0, "after": 0, "saved": 0}

    before_est = 0
    for m in msgs:
        if isinstance(m, dict):
            before_est += _est_tokens(json.dumps(m, ensure_ascii=False))
    if before_est < 1500:   # 小请求不值得动
        return msgs, {"enabled": True, "before": before_est, "after": before_est,
                      "saved": 0, "note": "skip-small"}

    seen = {}
    out = []
    sys_buf = []
    stat = {"reasoning": 0, "system_merged": 0, "empty_dropped": 0,
            "deduped": 0, "tool_truncated": 0, "old_tool_trimmed": 0, "para_deduped": 0}

    # 历史工具结果：最近 N 条保留原样（模型当前真正要用的），更早的压到 old_tool_chars
    keep_set = set()
    if cfg["keep_recent_tools"] > 0:
        t_idx = [i for i, m in enumerate(msgs) if isinstance(m, dict) and m.get("role") == "tool"]
        keep_set = set(t_idx[-cfg["keep_recent_tools"]:])

    # 找最后一条 assistant 的下标（可选用法：保留最后一条的推理）
    last_asst = -1
    if cfg["keep_last_assistant_reasoning"]:
        for i, m in enumerate(msgs):
            if isinstance(m, dict) and m.get("role") == "assistant":
                last_asst = i

    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            out.append(m)
            continue
        m = dict(m)
        role = m.get("role")
        # 1) 剥旧思考
        if cfg["strip_reasoning"] and role == "assistant" and i != last_asst:
            for k in ("reasoning_content", "reasoning", "thinking"):
                if m.pop(k, None) is not None:
                    stat["reasoning"] += 1
        # 2) system 合并（收集，稍后统一放最前）
        if cfg["merge_system"] and role in ("system", "developer"):
            c = m.get("content")
            if isinstance(c, str):
                sys_buf.append(c)
            elif isinstance(c, list):
                sys_buf.append("".join(x.get("text") or "" for x in c if isinstance(x, dict)))
            continue
        # 3) content 瘦身
        #    3a) 历史工具结果先做激进压缩（最近 keep_recent_tools 条不动）
        if role == "tool" and i not in keep_set:
            c0 = m.get("content")
            if isinstance(c0, str) and cfg["old_tool_chars"] > 0 and len(c0) > cfg["old_tool_chars"]:
                nc, cut = _slim_truncate(c0, cfg["old_tool_chars"])
                if cut > 0:
                    m["content"] = nc
                    stat["old_tool_trimmed"] += 1
        c = _slim_content(m.get("content"), cfg, seen, i, role, stat)
        if c is not None:
            m["content"] = c
        # 4) 空废消息
        if (m.get("content") in (None, "", [])) and not m.get("tool_calls"):
            stat["empty_dropped"] += 1
            continue
        out.append(m)

    if sys_buf:
        merged = "\n\n".join(sys_buf)
        if len(sys_buf) > 1:
            stat["system_merged"] = len(sys_buf) - 1
        # 合并后的 system 也做段级去重（客户端常把同一份策略/说明重复拼两遍）
        merged2, psaved = _slim_paragraphs(merged, cfg["para_min_chars"])
        if psaved > 0:
            stat["para_deduped"] += 1
            merged = merged2
        out.insert(0, {"role": "system", "content": merged})

    after_est = 0
    for m in out:
        after_est += _est_tokens(json.dumps(m, ensure_ascii=False))
    saved = max(0, before_est - after_est)
    st = {"enabled": True, "before": before_est, "after": after_est,
          "saved": saved, "pct": round(100.0 * saved / before_est, 1) if before_est else 0,
          "model": model, "msgs_before": len(msgs), "msgs_after": len(out)}
    st.update(stat)
    _slim_record(st)
    return out, st


def _slim_record(st):
    try:
        with _SLIM_LOCK:
            _SLIM_STAT["n"] += 1
            _SLIM_STAT["saved_est"] += int(st.get("saved") or 0)
            _SLIM_STAT["last"] = st
        os.makedirs(os.path.dirname(SLIM_FILE), exist_ok=True)
        rec = dict(st)
        rec["at"] = int(time.time())
        with open(SLIM_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        sys.stderr.write("[relay][slim] est %d→%d（省 %d, %.1f%%）reasoning=%d sys合并=%d 去重=%d\n"
                         % (st.get("before") or 0, st.get("after") or 0, st.get("saved") or 0,
                            st.get("pct") or 0, st.get("reasoning") or 0,
                            st.get("system_merged") or 0, st.get("deduped") or 0))
        sys.stderr.flush()
    except Exception:
        pass


def build_upstream_body(oai_req, model):
    """把【OpenAI 形状】的请求体加工成上游 body（含 max_tokens 抬升策略）。"""
    msgs, _slim_st = slim_messages(oai_req.get("messages") or [], model=model)
    oai_req["_slim"] = _slim_st
    body = {
        "model": model,
        "messages": msgs,
        "stream": True,   # 上游强制流式
    }
    for k in ("temperature", "top_p", "stop", "tools", "tool_choice",
              "presence_penalty", "frequency_penalty", "response_format", "seed"):
        if oai_req.get(k) is not None:
            body[k] = oai_req[k]
    if oai_req.get("parallel_tool_calls") is not None:
        body["parallel_tool_calls"] = oai_req["parallel_tool_calls"]

    # K3.1-pro 是思考型模型，reasoning 计入 completion_tokens；
    # max_tokens 给小了会把预算全烧在思考上导致正文为空。
    # 因此：客户端没给就不限制；给了则抬高到不低于 MIN_COMPLETION_TOKENS。
    mt = oai_req.get("max_tokens")
    if mt is not None:
        try:
            mt = int(mt)
        except Exception:
            mt = None
    if mt is not None and mt > 0:
        body["max_tokens"] = max(mt, MIN_COMPLETION_TOKENS)
    return body


def run_relay(oai_req, hdr_model=None):
    """执行一次【CDP 通道】上游调用 + 空正文放大重试。

    返回 dict：
      成功 {"ok": True, "res": <聚合结果>, "model": <实际模型>, "elapsed": 秒}
      失败 {"ok": False, "err": RelayError, "model": <模型>, "detail": 旧字段兼容}
    """
    model = resolve_model(hdr_model, oai_req.get("model"))
    body = build_upstream_body(oai_req, model)
    t0 = time.time()
    try:
        res = call_upstream(body)
    except CdpTimeout as e:
        detail = ("CDP 上游超时：%s。排查：① WorkBuddy 是否带 --remote-debugging-port=%d 启动"
                  "（浏览器打开 http://127.0.0.1:%d/json/version 应返回 JSON）；"
                  "② WorkBuddy 界面是否卡死，重启一次；"
                  "③ 确认工作时没有人在 WB 里开着长任务占住渲染进程。" % (str(e)[:200], CDP_PORT, CDP_PORT))
        _record_upstream_error(model, {"kind": "cdp_timeout", "detail": detail[:400]})
        err = RelayError(http=504, code="cdp_timeout", type="api_error",
                         message=detail, fallbackable=False)
        return {"ok": False, "model": model, "type": "cdp_timeout",
                "detail": detail, "err": err}
    except Exception as e:
        _record_upstream_error(model, {"kind": "relay", "detail": "relay upstream failed: %s" % str(e)[:400]})
        err = RelayError(http=502, code="relay_failed", type="api_error",
                         message="relay upstream failed: %s" % str(e)[:500], fallbackable=True)
        return {"ok": False, "model": model, "type": "api_error",
                "detail": err.message, "err": err}

    # 正文空但思考有内容 + 被 length 截断 → 放大预算重试一次
    if (res.get("ok") and not (res.get("content") or "").strip()
            and not (res.get("tool_calls") or [])
            and res.get("reasoning")
            and body.get("max_tokens")):
        body["max_tokens"] = max(body["max_tokens"] * 2, 8192)
        try:
            res2 = call_upstream(body)
            if res2.get("ok") and (res2.get("content") or "").strip():
                res = res2
        except Exception:
            pass

    if not res.get("ok"):
        _record_upstream_error(model, res)
        err = _cdp_res_to_error(res, model)
        return {"ok": False, "model": model, "type": "upstream_error",
                "detail": "workbuddy upstream error: %s %s" % (res.get("detail"), res.get("body") or ""),
                "err": err}

    _LAST_OK[0] = time.time()
    return {"ok": True, "res": res, "model": res.get("model") or model,
            "elapsed": round(time.time() - t0, 1)}


# ----------------------------------------------------------------------------
# P0-0 结构化错误：把上游限流原样翻译给客户端
# ----------------------------------------------------------------------------
class RelayError(Exception):
    """反代对外统一错误类型（与 wm_transport.UpstreamError 字段一致）。

    统一成一种类型的好处：CDP 通道、直连通道、协议翻译层的错误
    走同一条出口，HTTP 层只需看 err.http / err.payload()。
    """

    def __init__(self, http=502, status=None, code="", type="api_error",
                 message="upstream error", reset_at="", retry_after=0,
                 raw="", fallbackable=False):
        super().__init__(message)
        self.http = int(http or 502)
        self.status = status
        self.code = str(code or "")
        self.type = type
        self.message = message
        self.reset_at = reset_at or ""
        self.retry_after = int(retry_after or 0)
        self.raw = (raw or "")[:1000]
        self.fallbackable = bool(fallbackable)

    def payload(self):
        err = {"message": self.message, "type": self.type}
        if self.code:
            err["code"] = self.code
        if self.status:
            err["upstream_status"] = self.status
        if self.reset_at:
            err["reset_at"] = self.reset_at
        if self.retry_after:
            err["retry_after"] = self.retry_after
        return {"error": err}

    @property
    def is_quota(self):
        return self.http == 429 or self.code == "6004" or self.type == "rate_limit_error"

    def __str__(self):
        return "RelayError(%s/%s) %s" % (self.http, self.code or "-", self.message[:160])


_RE_RESET = re.compile(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\s*UTC[+-]\d+)?)")


def _cdp_res_to_error(res, model):
    """把 CDP 通道的失败结果翻译成结构化错误（P0-0 的老通道修复）。

    旧实现直接把上游 429/code 6004 包成 502 一行字符串，客户端当网关故障，
    用户只看到「原因暂不明确」。这里把 status/code/重置时间抠出来还原成 429。
    """
    body = str(res.get("body") or "")
    detail = str(res.get("detail") or "")
    status = res.get("status")
    try:
        status = int(status) if status else None
    except Exception:
        status = None
    blob = (detail + " " + body).strip()

    code = ""
    m = re.search(r'"code"\s*:\s*"?(-?\d+)"?', blob)
    if m:
        code = m.group(1)
    reset = ""
    m2 = _RE_RESET.search(blob)
    if m2:
        reset = m2.group(1)

    # 上游业务消息（body 有时是 JSON 字符串套 JSON）
    upmsg = ""
    for cand in (body, detail):
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if isinstance(obj, str):
            try:
                obj = json.loads(obj)
            except Exception:
                continue
        if isinstance(obj, dict):
            disp = obj.get("displayMsg")
            if isinstance(disp, dict) and (disp.get("zh") or disp.get("zh-hans")):
                upmsg = disp.get("zh") or disp.get("zh-hans")
            upmsg = upmsg or obj.get("msg") or obj.get("message") or ""
            if upmsg:
                break

    if status == 429 or code == "6004":
        msg = upmsg or "上游限流：当前账号该模型的使用量已超出频率限制"
        if reset and reset not in msg:
            msg = "%s（将于 %s 重置）" % (msg, reset)
        return RelayError(http=429, status=status or 429, code=code or "6004",
                          type="rate_limit_error", message=msg, reset_at=reset,
                          retry_after=60, raw=blob, fallbackable=True)
    if status in (401, 403):
        return RelayError(http=401, status=status, code=code or "invalid_token",
                          type="authentication_error",
                          message=upmsg or "账号凭证失效，请重新登录 WorkBuddy",
                          raw=blob, fallbackable=False)
    if status in (400, 422):
        return RelayError(http=400, status=status, code=code or "invalid_request",
                          type="invalid_request_error",
                          message=upmsg or "上游拒绝了该请求：参数不合法", raw=blob,
                          fallbackable=False)
    if status == 404:
        return RelayError(http=404, status=status, code=code or "not_found",
                          type="invalid_request_error",
                          message=upmsg or "上游没有该资源（模型名可能写错）", raw=blob,
                          fallbackable=True)
    return RelayError(http=502, status=status, code=code or "upstream_error",
                      type="api_error",
                      message=upmsg or ("workbuddy upstream error: %s" % (blob[:300] or "未知")),
                      raw=blob, fallbackable=True)


def _note_error(model, err, channel):
    """统一记录一次失败（日志 + 熔断 + 状态面板）。"""
    now = int(time.time())
    with _DIRECT_LOCK:
        _DIRECT_STAT["fail"] += 1
        _DIRECT_STAT["last_fail_at"] = now
        _DIRECT_STAT["last_err"] = ("[%s] %s" % (channel, str(err)[:200]))
    _record_upstream_error(model, {
        "kind": channel, "status": err.status, "code6004": err.is_quota,
        "detail": str(err)[:300], "body": err.raw[:600],
    })
    if err.is_quota and WT is not None:
        # 熔断：默认按上游给的重置时间，但至少 60s，最多 30 分钟，避免一直空打上游
        secs = 120
        if err.reset_at:
            try:
                ts = time.mktime(time.strptime(err.reset_at[:19].replace("T", " "),
                                               "%Y-%m-%d %H:%M:%S"))
                secs = max(60, min(1800, int(ts - time.time())))
            except Exception:
                secs = 600
        WT.cooldown_set(model, secs, reset_at=err.reset_at, msg=err.message, code=err.code)


# ----------------------------------------------------------------------------
# P0-1/P0-2 直连通道：真流式 + 无锁并发
# ----------------------------------------------------------------------------
def _model_chain(model):
    """返回降级链：先主模型，再 config 里配的备用模型（去重、只认已知模型）。"""
    chain = [model]
    if not FALLBACK_MODELS:
        return chain
    km = _known_models()
    for m in FALLBACK_MODELS:
        name = _pick_name(m)
        if name and name in km and name not in chain:
            chain.append(name)
    return chain


def open_direct(oai_req, hdr_model=None):
    """建一条直连上游流。

    返回 (stream, model)。stream 已 open，调用方负责 close。
    失败抛 RelayError（已做降级链与熔断判断）。
    """
    if WT is None:
        raise RelayError(http=502, code="no_transport", type="api_error",
                         message="直连传输层不可用：%s" % globals().get("_WT_IMPORT_ERR", "import failed"))
    base_model = resolve_model(hdr_model, oai_req.get("model"))
    chain = _model_chain(base_model)
    errors = []

    for model in chain:
        # 熔断中且不是唯一选项 → 直接跳过（省一次无意义的上游往返）
        cd = WT.cooldown_get(model)
        if cd and len(chain) > 1:
            errors.append(RelayError(http=429, type="rate_limit_error", code="6004",
                                     message="%s 处于限流冷却（剩 %ds）" % (model, cd["left"]),
                                     reset_at=cd.get("reset_at") or "", fallbackable=True))
            continue
        if cd and len(chain) == 1:
            # 唯一模型且正在熔断：立刻把真实原因告诉客户端，不再打上游
            raise RelayError(http=429, type="rate_limit_error", code="6004",
                             message=cd.get("msg") or "上游限流中",
                             reset_at=cd.get("reset_at") or "",
                             retry_after=cd.get("left") or 60, fallbackable=True)

        body = build_upstream_body(oai_req, model)
        st = WT.DirectStream(body, model=model, stream=True, timeout=DIRECT_TIMEOUT)
        try:
            st.open()
        except WT.UpstreamError as e:
            err = RelayError(http=e.http, status=e.status, code=e.code, type=e.type,
                             message=e.message, reset_at=e.reset_at,
                             retry_after=e.retry_after, raw=e.raw,
                             fallbackable=e.fallbackable)
            errors.append(err)
            _note_error(model, err, "direct-open")
            # 429 / 404（模型不存在）→ 换备用模型；其余（400/401）换模型也没用
            if err.http in (429, 404) and model != chain[-1]:
                continue
            raise err
        except Exception as e:
            err = RelayError(http=502, code="direct_error", type="api_error",
                             message="直连上游异常：%s" % str(e)[:200], fallbackable=True)
            errors.append(err)
            _note_error(model, err, "direct-open")
            raise err

        with _DIRECT_LOCK:
            _DIRECT_STAT["last_ttfb_ms"] = int((st.meta or {}).get("ttfb_ms") or 0)
            _DIRECT_STAT["active"] += 1
        return st, model

    # 全部候选都失败：优先把最"真实"的那个错误抛出去（429 优先于 502）
    quota = [e for e in errors if e.is_quota]
    raise (quota[0] if quota else (errors[-1] if errors else
          RelayError(http=502, message="没有可用的上游模型")))



# ----------------------------------------------------------------------------
# HTTP 层
# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# 通用小工具：chunk 解析 / 非流式响应体 / 用量账本（P1-2 持久化）
# ----------------------------------------------------------------------------
def _err_from_chunk_error(obj):
    """上游在流中间塞了一条 error 事件 → 转成统一错误对象。"""
    e = obj.get("error")
    if not isinstance(e, dict):
        e = {"message": str(e)}
    code = str(e.get("code") or "")
    msg = e.get("message") or e.get("msg") or "上游在流中报错"
    if code == "6004" or code == "429":
        return RelayError(http=429, status=429, code=code or "6004",
                          type="rate_limit_error", message=msg, fallbackable=True)
    return RelayError(http=502, status=None, code=code or "upstream_error",
                      type="api_error", message=msg, fallbackable=True)


def _iter_chunks(st):
    """把直连流的 SSE 原始行解成一个个 JSON chunk（遇 [DONE] 结束）。"""
    for raw in st.lines():
        line = raw.strip()
        if not line or line.startswith(b":"):
            continue
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        if payload == b"[DONE]":
            return
        try:
            obj = json.loads(payload.decode("utf-8", "replace"))
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("error"):
            raise _err_from_chunk_error(obj)
        yield obj


def _openai_completion_body(res, model, client_model=None):
    """聚合结果 -> OpenAI 非流式 chat.completion 响应体。"""
    content = res.get("content") or ""
    tool_calls = res.get("tool_calls") or []
    finish = res.get("finish") or ("tool_calls" if tool_calls else "stop")
    msg = {"role": "assistant", "content": content}
    if res.get("reasoning"):
        msg["reasoning_content"] = res["reasoning"]
    if tool_calls:
        msg["tool_calls"] = tool_calls
        if not content:
            msg["content"] = None
    return {
        "id": res.get("id") or "wb-chat",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or client_model or "",
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": res.get("usage") or {"prompt_tokens": 0, "completion_tokens": 0,
                                      "total_tokens": 0},
    }


_USAGE_LOCK = threading.Lock()
_USAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "usage.jsonl")
_USAGE = {"req": 0, "prompt_tokens": 0, "completion_tokens": 0,
          "by_model": {}, "by_kind": {}, "by_day": {}, "since": int(time.time())}


def _usage_add(rec):
    pin = int(rec.get("in") or 0)
    pout = int(rec.get("out") or 0)
    model = rec.get("model") or "-"
    kind = rec.get("kind") or "chat"
    day = rec.get("day") or time.strftime("%Y-%m-%d", time.localtime(rec.get("at") or time.time()))
    _USAGE["req"] += 1
    _USAGE["prompt_tokens"] += pin
    _USAGE["completion_tokens"] += pout
    m = _USAGE["by_model"].setdefault(model, {"req": 0, "in": 0, "out": 0})
    m["req"] += 1
    m["in"] += pin
    m["out"] += pout
    _USAGE["by_kind"][kind] = _USAGE["by_kind"].get(kind, 0) + 1
    d = _USAGE["by_day"].setdefault(day, {"req": 0, "in": 0, "out": 0})
    d["req"] += 1
    d["in"] += pin
    d["out"] += pout


def _load_usage():
    """启动时把历史账本读回来，重启不归零。"""
    try:
        with open(_USAGE_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    _usage_add(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass


def _note_usage(model, usage, kind="chat"):
    """记一笔用量：内存汇总 + 追加 logs/usage.jsonl（崩溃/重启后可恢复）。"""
    usage = usage or {}
    rec = {"at": int(time.time()), "day": time.strftime("%Y-%m-%d"),
           "kind": kind, "model": model or "",
           "in": int(usage.get("prompt_tokens") or 0),
           "out": int(usage.get("completion_tokens") or 0)}
    with _USAGE_LOCK:
        _usage_add(rec)
    try:
        os.makedirs(os.path.dirname(_USAGE_FILE), exist_ok=True)
        with open(_USAGE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


_load_usage()


# ----------------------------------------------------------------------------
# 额度快照 → 响应头：客户端不用额外探测就知道还剩多少，守护进程也靠它判断阈值
# ----------------------------------------------------------------------------
def _quota_headers(extra=None):
    hdrs = dict(extra or {})
    try:
        if WT is None:
            return hdrs
        snap = WT.quota_snapshot() or {}
        if not snap:
            return hdrs
        cur = _active_model()
        item = snap.get(cur)
        if not isinstance(item, dict):
            cand = [v for v in snap.values() if isinstance(v, dict) and v.get("percent") is not None]
            item = max(cand, key=lambda v: v.get("at") or 0) if cand else None
        if not isinstance(item, dict) or item.get("percent") is None:
            return hdrs
        hdrs["X-WM-Quota-Percent"] = str(item.get("percent"))
        ra = str(item.get("reset_at_human") or item.get("reset_at") or "")
        if ra:
            hdrs["X-WM-Quota-Reset"] = ra
        hdrs["X-WM-Quota-Model"] = str(item.get("model") or cur or "")
        if float(item.get("percent") or 0) >= QUOTA_SWITCH_PERCENT:
            hdrs["X-WM-Quota-Near-Limit"] = "1"
    except Exception:
        pass
    # 输入精简战果：before→after 与省下的估算 token，客户端/守护都能读
    try:
        last = _SLIM_STAT.get("last") or {}
        if last.get("before"):
            hdrs["X-WM-Slim-Before"] = str(last.get("before"))
            hdrs["X-WM-Slim-After"] = str(last.get("after"))
            hdrs["X-WM-Slim-Saved"] = str(last.get("saved"))
            hdrs["X-WM-Slim-Pct"] = str(last.get("pct"))
    except Exception:
        pass
    return hdrs


# ----------------------------------------------------------------------------
# 输入 token 审计：回答"每次请求到底把什么塞进了输入"
# 只做统计与采样，不改动任何转发内容
# ----------------------------------------------------------------------------
def _est_tokens(text):
    """中文按 1 字≈1 token、其余按 3.6 字符≈1 token 估算，够用且不依赖分词库。"""
    if not text:
        return 0
    cjk = 0
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" or "\uff00" <= ch <= "\uffef":
            cjk += 1
    return int(cjk + (len(text) - cjk) / 3.6)


def _audit_take():
    """判断这一条要不要审计；返回 True 后自动扣减配额。"""
    with _AUDIT_LOCK:
        if _AUDIT_LEFT[0] > 0:
            _AUDIT_LEFT[0] -= 1
            return True
    return False


def _audit_force():
    with _AUDIT_LOCK:
        return _AUDIT_LEFT[0] > 0


def _audit_request(path, req, forced=False):
    """把一次请求的输入构成统计落盘（roles / tools / 分段长度 / 估算 token）。"""
    try:
        if forced:
            pass
        elif not _audit_take():
            return
        msgs = req.get("messages")
        if msgs is None and req.get("input") is not None:
            msgs = req.get("input")
        if isinstance(msgs, str):
            msgs = [{"role": "user", "content": msgs}]
        if not isinstance(msgs, list):
            msgs = []
        roles = {}
        parts = []
        total_chars = 0
        total_est = 0
        para_dups = []
        for i, m in enumerate(msgs):
            if not isinstance(m, dict):
                continue
            role = m.get("role") or ("user" if "content" in m else "?")
            c = m.get("content")
            if isinstance(c, list):
                text = "".join(x.get("text") or "" for x in c if isinstance(x, dict))
            else:
                text = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
            tc = m.get("tool_calls")
            if tc:
                text += json.dumps(tc, ensure_ascii=False)
            n = len(text)
            est_n = _est_tokens(text)
            total_chars += n
            total_est += est_n
            r = roles.setdefault(role, {"count": 0, "chars": 0, "est": 0})
            r["count"] += 1
            r["chars"] += n
            r["est"] += est_n
            if len(parts) < 40:
                if n >= 400:
                    _, psaved = _slim_paragraphs(text, 200)
                    if psaved > 0:
                        para_dups.append({"i": i, "role": role, "saved": psaved,
                                          "head": " ".join(text[:80].split())})
                parts.append({"i": i, "role": role, "chars": n,
                              "est": _est_tokens(text),
                              "head": " ".join(text[:120].split())})
        sysc = req.get("system")
        sys_text = ""
        if isinstance(sysc, str):
            sys_text = sysc
        elif isinstance(sysc, list):
            sys_text = "".join(x.get("text") or "" for x in sysc if isinstance(x, dict))
        if sys_text:
            total_chars += len(sys_text)
            est_s = _est_tokens(sys_text)
            total_est += est_s
            r = roles.setdefault("system(param)", {"count": 1, "chars": 0, "est": 0})
            r["chars"] += len(sys_text)
            r["est"] += est_s
            if len(parts) < 40:
                parts.append({"i": -1, "role": "system(param)", "chars": len(sys_text),
                              "est": est_s,
                              "head": " ".join(sys_text[:120].split())})
        tools = req.get("tools") or []
        tools_text = json.dumps(tools, ensure_ascii=False) if tools else ""
        rec = {
            "at": int(time.time()),
            "path": path,
            "model": req.get("model") or "",
            "stream": bool(req.get("stream")),
            "msgs": len(msgs),
            "total_chars": total_chars,
            "est_total": total_est + _est_tokens(tools_text),
            "est_messages": total_est,
            "tools_n": len(tools) if isinstance(tools, list) else 0,
            "tools_chars": len(tools_text),
            "est_tools": _est_tokens(tools_text),
            "roles": roles,
            "parts": parts,
            "top": sorted(parts, key=lambda x: -(x.get("chars") or 0))[:8],
            "para_dups": para_dups,
        }
        os.makedirs(os.path.dirname(AUDIT_FILE), exist_ok=True)
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        sys.stderr.write("[relay][audit] %s model=%s msgs=%d chars=%d est_in≈%d tools≈%d\n"
                         % (path, rec["model"], rec["msgs"], total_chars,
                            rec["est_total"], rec["est_tools"]))
        sys.stderr.flush()
    except Exception as e:
        try:
            sys.stderr.write("[relay][audit] 失败: %s\n" % e)
        except Exception:
            pass


def _sse(obj):
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


def _sse_named(event, obj):
    return ("event: %s\ndata: %s\n\n" % (event, json.dumps(obj, ensure_ascii=False))).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "workmom/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[relay] " + (fmt % args) + "\n")

    def _json(self, code, obj, extra_headers=None):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers",
                         "X-WM-Quota-Percent,X-WM-Quota-Reset,X-WM-Quota-Model,X-WM-Quota-Near-Limit")
        for k, v in _quota_headers(extra_headers).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(data)

    def _err_json(self, err, protocol="openai"):
        """把 RelayError 按客户端协议格式返回（P0-0：限额就是 429，不再是 502 空壳）。"""
        hdrs = {}
        if err.retry_after:
            hdrs["Retry-After"] = str(err.retry_after)
        if err.is_quota:
            hdrs["X-WM-Quota-Exceeded"] = "1"
            if err.reset_at:
                hdrs["X-WM-Quota-Reset"] = err.reset_at
        if protocol == "anthropic":
            body = {"type": "error",
                    "error": {"type": err.type or "api_error", "message": err.message}}
            if err.code:
                body["error"]["code"] = err.code
        else:
            body = err.payload()
        try:
            sys.stderr.write("[relay] 返回错误 %s -> HTTP %d %s\n"
                             % ((err.code or err.type), err.http, err.message[:160]))
            sys.stderr.flush()
        except Exception:
            pass
        return self._json(err.http, body, hdrs)

    def _begin_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers",
                         "X-WM-Quota-Percent,X-WM-Quota-Reset,X-WM-Quota-Model,X-WM-Quota-Near-Limit")
        for k, v in _quota_headers().items():
            self.send_header(k, str(v))
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        # 输入 token 审计开关：GET /admin/audit?n=3 抓接下来 3 次请求的输入构成
        # 不加参数只查询剩余次数，不重启反代就能改，省得为了抓一次样本动服务
        if path in ("/admin/audit", "/audit"):
            n = None
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    if kv.startswith("n="):
                        try:
                            n = int(float(kv[2:]))
                        except Exception:
                            n = 0
            if n is not None:
                with _AUDIT_LOCK:
                    _AUDIT_LEFT[0] = max(0, n)
            return self._json(200, {
                "audit_left": _AUDIT_LEFT[0],
                "file": AUDIT_FILE,
                "hint": "GET /admin/audit?n=3 → 抓接下来 3 次请求；请求头 X-WM-Audit: 1 可强制单次",
            })
        # 根路径 = 真·Web 面板（以前返回 /health 的 JSON，README 却说"浏览器打开就是面板"，对不上）
        if path in ("", "/panel", "/index.html", "/dashboard"):
            try:
                html = _panel_html().encode("utf-8")
            except Exception as ex:
                return self._json(500, {"error": {"type": "panel_error", "message": str(ex)}})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(html)
            except Exception:
                pass
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/health":
            # 不再用长连接探活（渲染进程卡死时会把 /health 一起拖死），
            # 优先用看门狗缓存，缓存过期才现场探一次（自带 4s 上限）。
            with _DIRECT_LOCK:
                cached = dict(_CDP_HEALTH)
            if cached["ok"] is None or (int(time.time()) - int(cached["last_check"] or 0)) > 120:
                ok, why = _probe_cdp()
                with _DIRECT_LOCK:
                    _CDP_HEALTH["ok"] = ok
                    _CDP_HEALTH["last_check"] = int(time.time())
                    _CDP_HEALTH["last_err"] = why
            else:
                ok, why = bool(cached["ok"]), cached["last_err"]
            up = "ok" if ok else ("error: " + (why or "unknown"))
            hint = "" if ok else ("CDP %d 不可用：确认 WorkBuddy 已启动并带 --remote-debugging-port=%d 参数，"
                                  "浏览器访问 http://127.0.0.1:%d/json/version 应能返回 JSON。"
                                  "若直连通道正常（transport=direct/auto），反代照常工作。"
                                  % (CDP_PORT, CDP_PORT, CDP_PORT))
            _cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
            _wb = str(_cfgval("wb_exe", "") or "")
            return self._json(200, {"status": "ok", "app": "workmom", "cdp": up, "hint": hint,
                                    "relay_port": self.server.server_address[1],
                                    "relay_base": "http://127.0.0.1:%d" % self.server.server_address[1],
                                    "cdp_port": CDP_PORT,
                                    "cdp_timeout_sec": CDP_TIMEOUT_SEC,
                                    "config_file": _cfg_path,
                                    "config_exists": os.path.exists(_cfg_path),
                                    "wb_exe": _wb,
                                    "wb_exe_exists": bool(_wb) and os.path.exists(_wb),
                                    "python": sys.version.split()[0],
                                    "uptime_sec": int(time.time() - _START_AT[0]),
                                    "default_model": _active_model(),
                                    "model": _active_model(),
                                    "models": _known_models(),
                                    "protocols": ["openai-chat", "anthropic-messages", "openai-responses"],
                                    "endpoints": ["/v1/chat/completions", "/v1/messages",
                                                  "/v1/messages/count_tokens", "/v1/responses", "/v1/models"],
                                    "stats": dict(REQ_STATS),
                                    "uid": _current_uid(),
                                    "upstream": "http://%s:%d%s" % (CDP_HOST, CDP_PORT, UPSTREAM_PATH),
                                    **_health_extra()})
        if path in ("/relay_state", "/state"):
            with _ERR_LOCK:
                errs = list(LAST_ERRORS)
                last_ok = _LAST_OK[0]
            pro_6004 = [e for e in errs if e.get("code6004") and "pro" in str(e.get("model") or "")]
            last_pro_6004 = max([e["at"] for e in pro_6004], default=0)
            return self._json(200, {
                "relay": "ok",
                "app": "workmom",
                "started_at": int(_START_AT[0]),
                "uid": _current_uid(),
                "model": _active_model(),
                "last_ok_at": int(last_ok),
                "error_count": len(errs),
                "last_error": errs[-1] if errs else None,
                "last_pro_6004_at": last_pro_6004,
                "pro_6004_count": len(pro_6004),
                "stats": dict(REQ_STATS),
                "errors": errs[-10:],
                **_health_extra(),
            })
        if path in ("/v1/models", "/models"):
            now = int(time.time())
            cur = _active_model()
            out = [{"id": cur, "object": "model", "created": now, "owned_by": "workmom"}]
            for m in _known_models():
                if m != cur:
                    out.append({"id": m, "object": "model", "created": now, "owned_by": "workmom"})
            return self._json(200, {"object": "list", "data": out})
        return self._json(404, {"error": {"message": "not found: " + path, "type": "invalid_request_error"}})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception as e:
            return self._json(400, {"error": {"message": "invalid json: %s" % e, "type": "invalid_request_error"}})

        # 输入 token 审计：只统计不转发，audit_requests>0 或请求头 X-WM-Audit:1 时才写盘
        if path in ("/v1/chat/completions", "/chat/completions", "/v1/messages", "/messages",
                    "/v1/responses", "/responses"):
            _audit_request(path, req,
                           forced=str(self.headers.get("X-WM-Audit") or "").strip() == "1")

        if path in ("/v1/chat/completions", "/chat/completions"):
            return self.chat(req)
        if path in ("/v1/messages", "/messages"):
            return self.anthropic_messages(req)
        if path in ("/v1/messages/count_tokens", "/messages/count_tokens"):
            _bump("count_tokens")
            return self._json(200, {"input_tokens": RP.anthropic_count_input_tokens(req)})
        if path in ("/v1/responses", "/responses"):
            return self.responses_api(req)
        return self._json(404, {"error": {"message": "not found: " + path, "type": "invalid_request_error"}})

    # ------------------------------------------------------------------
    # P0 统一上游出入口
    # ------------------------------------------------------------------
    def _drop_direct(self, st):
        try:
            st.close()
        except Exception:
            pass
        with _DIRECT_LOCK:
            _DIRECT_STAT["active"] = max(0, _DIRECT_STAT["active"] - 1)

    def _stream_error(self, protocol, err):
        """流已开始后（HTTP 200 已发出）才发现的错误：只能用协议内错误事件表达。"""
        try:
            if protocol == "anthropic":
                self.wfile.write(_sse_named("error", {
                    "type": "error",
                    "error": {"type": err.type or "api_error", "message": err.message}}))
            elif protocol == "responses":
                self.wfile.write(_sse_named("response.failed", {
                    "type": "response.failed",
                    "response": {"status": "failed",
                                 "error": {"code": err.code or "upstream_error",
                                           "message": err.message}}}))
            else:
                self.wfile.write(_sse({"error": {"message": err.message,
                                                 "type": err.type or "api_error",
                                                 "code": err.code or "upstream_error"}}))
                self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except Exception:
            pass

    def _pump(self, st, protocol, client_model):
        """真流式：上游来一个 chunk 就翻一个给客户端，不缓存全文（P0-1）。"""
        tr = RS.make(protocol, client_model)
        try:
            for name, obj in tr.start_events():
                self.wfile.write(RS.sse_bytes(name, obj))
            self.wfile.flush()
            for chunk in _iter_chunks(st):
                out = tr.feed(chunk)
                if out:
                    for name, obj in out:
                        self.wfile.write(RS.sse_bytes(name, obj))
                    self.wfile.flush()
            for name, obj in tr.finish_events():
                self.wfile.write(RS.sse_bytes(name, obj))
            if protocol == "openai":
                self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # 客户端主动断开（Claude Code 按 Esc 是常态），不算错误
            pass
        except RelayError as e:
            _note_error(client_model, e, "direct-stream")
            self._stream_error(protocol, e)
        except WT.UpstreamError as e:
            err = RelayError(http=e.http, status=e.status, code=e.code, type=e.type,
                             message=e.message, reset_at=e.reset_at,
                             retry_after=e.retry_after, raw=e.raw,
                             fallbackable=e.fallbackable)
            _note_error(client_model, err, "direct-stream")
            self._stream_error(protocol, err)
        except Exception as e:
            self._stream_error(protocol, RelayError(
                http=502, code="stream_error", type="api_error",
                message="流式传输异常：%s" % str(e)[:200]))
        return tr.result()

    def _collect_direct(self, st, client_model):
        """非流式：把真流一遍读全，再用同一个聚合器出结果。"""
        tr = RS.make("openai", client_model)
        for chunk in _iter_chunks(st):
            tr.feed(chunk)
        tr.finish_events()
        return tr.result()

    def _try_direct(self, oai_req, hdr_model, protocol, want_stream, client_model):
        """尝试直连路径。

        返回 (done, payload)：
          done=True  → 已完整响应客户端，payload 为 None
          done=False → 直连不可用，payload 为 RelayError（调用方可选择回落 CDP）
        """
        if TRANSPORT not in ("auto", "direct") or WT is None or RS is None:
            return False, None
        try:
            st, model = open_direct(oai_req, hdr_model)
        except RelayError as e:
            # 线路级故障（502/504）在 auto 模式下回落 CDP；语义级故障（429/400/401）如实回
            if TRANSPORT == "auto" and e.fallbackable and e.http in (502, 504):
                return False, None
            self._err_json(e, protocol)
            return True, e

        try:
            if want_stream:
                self._begin_stream()
                res = self._pump(st, protocol, client_model)
            else:
                res = self._collect_direct(st, client_model)
        finally:
            self._drop_direct(st)

        with _DIRECT_LOCK:
            _DIRECT_STAT["ok"] += 1
            _DIRECT_STAT["last_ok_at"] = int(time.time())
        _LAST_OK[0] = time.time()
        _note_usage(model, res.get("usage"), protocol)

        if want_stream:
            return True, None

        # 非流式响应体按协议构造
        if protocol == "anthropic":
            self._json(200, RP.openai_to_anthropic(res, model))
        elif protocol == "responses":
            self._json(200, RP.openai_to_responses(res, model))
        else:
            self._json(200, _openai_completion_body(res, model, client_model))
        return True, None

    # -- OpenAI Chat Completions（原生） -------------------------------------
    def chat(self, req):
        _bump("chat")
        want_stream = bool(req.get("stream"))
        client_model = req.get("model") or _active_model()

        done, _e = self._try_direct(req, self.headers.get("X-WM-Model"),
                                    "openai", want_stream, client_model)
        if done:
            return

        # CDP 回落通道（老逻辑保留：直连被墙/页面内代理可用时兜底）
        r = run_relay(req, self.headers.get("X-WM-Model"))
        if not r["ok"]:
            return self._err_json(r["err"], "openai")

        res = r["res"]
        content = res.get("content") or ""
        tool_calls = res.get("tool_calls") or []
        finish = res.get("finish") or ("tool_calls" if tool_calls else "stop")
        usage = res.get("usage") or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        cid = res.get("id") or "wb-chat"
        cmodel = r["model"]
        _note_usage(cmodel, usage, "chat")

        if not want_stream:
            self._json(200, _openai_completion_body(res, cmodel, client_model))
            return

        self._begin_stream()
        try:
            self.wfile.write(_sse({"id": cid, "object": "chat.completion.chunk",
                                   "created": int(time.time()), "model": cmodel,
                                   "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}))
            if content:
                step = 512
                for i in range(0, len(content), step):
                    self.wfile.write(_sse({"id": cid, "object": "chat.completion.chunk",
                                           "created": int(time.time()), "model": cmodel,
                                           "choices": [{"index": 0,
                                                        "delta": {"content": content[i:i + step]},
                                                        "finish_reason": None}]}))
            if tool_calls:
                self.wfile.write(_sse({"id": cid, "object": "chat.completion.chunk",
                                       "created": int(time.time()), "model": cmodel,
                                       "choices": [{"index": 0,
                                                    "delta": {"tool_calls": tool_calls},
                                                    "finish_reason": None}]}))
            self.wfile.write(_sse({"id": cid, "object": "chat.completion.chunk",
                                   "created": int(time.time()), "model": cmodel,
                                   "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                                   "usage": usage}))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except Exception as e:
            sys.stderr.write("[relay] stream write failed: %s\n" % e)
        return

    # -- Anthropic Messages（Claude Code 等） --------------------------------
    def anthropic_messages(self, req):
        _bump("anthropic")
        oai_req, meta = RP.anthropic_to_openai(req)
        want_stream = bool(meta["stream"])

        done, _e = self._try_direct(oai_req, self.headers.get("X-WM-Model"),
                                    "anthropic", want_stream, req.get("model") or _active_model())
        if done:
            return

        r = run_relay(oai_req, self.headers.get("X-WM-Model"))
        if not r["ok"]:
            return self._err_json(r["err"], "anthropic")
        res, model = r["res"], r["model"]
        _note_usage(model, res.get("usage"), "anthropic")

        if not want_stream:
            return self._json(200, RP.openai_to_anthropic(res, model))

        self._begin_stream()
        try:
            for name, data in RP.anthropic_stream_events(res, model):
                self.wfile.write(_sse_named(name, data))
            self.wfile.flush()
        except Exception as e:
            sys.stderr.write("[relay] anthropic stream write failed: %s\n" % e)
        return

    # -- OpenAI Responses（Codex CLI 等） ------------------------------------
    def responses_api(self, req):
        _bump("responses")
        oai_req, meta = RP.responses_to_openai(req)
        want_stream = bool(meta["stream"])

        done, _e = self._try_direct(oai_req, self.headers.get("X-WM-Model"),
                                    "responses", want_stream, req.get("model") or _active_model())
        if done:
            return

        r = run_relay(oai_req, self.headers.get("X-WM-Model"))
        if not r["ok"]:
            return self._err_json(r["err"], "openai")
        res, model = r["res"], r["model"]
        _note_usage(model, res.get("usage"), "responses")

        if not want_stream:
            return self._json(200, RP.openai_to_responses(res, model))

        self._begin_stream()
        try:
            for name, data in RP.responses_stream_events(res, model):
                self.wfile.write(_sse_named(name, data))
            self.wfile.flush()
        except Exception as e:
            sys.stderr.write("[relay] responses stream write failed: %s\n" % e)
        return


def _probe_cdp():
    """探活渲染进程：CDP 端口 -> 页面 -> 一次极轻的 evaluate。

    分开判三层，是为了区分「WorkBuddy 没启动」「端口没开」「页面卡死」
    三种完全不同的故障，重启动作只在第三层才该做。
    """
    try:
        with _OPENER.open("http://%s:%d/json/version" % (CDP_HOST, CDP_PORT),
                          timeout=5) as r:
            r.read()
    except Exception as e:
        return False, "CDP 端口无响应（WorkBuddy 没启动或没带 --remote-debugging-port=%d）：%s" % (
            CDP_PORT, str(e)[:120])
    s = None
    try:
        s = CdpSession()
        v = s.eval("1+1", timeout_ms=4000)
        if v != 2:
            return False, "CDP 回显异常：%r" % (v,)
        return True, ""
    except Exception as e:
        return False, "页面无响应（渲染进程可能卡死）：%s" % str(e)[:160]
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


def _watchdog():
    """P0-3 渲染进程卡死自愈。

    WorkBuddy 渲染进程卡死后，所有 CDP 调用会一直挂着直到超时，
    用户侧表现为「反代整个不动了」。这里做后台探活：
      report  —— 只探活、记日志、把状态暴露到 /health，不擅自重启；
      restart —— 连续 2 次探活失败且直连通道也不健康时，重启 WorkBuddy。
    只有当「CDP 挂了且直连也不可用」时才重启——否则重启会打断用户正在用的 WB。
    """
    boot_slept = 0
    while True:
        time.sleep(min(HEAL_INTERVAL, 30) if boot_slept < 2 else HEAL_INTERVAL)
        boot_slept += 1
        if AUTO_HEAL == "off":
            continue
        ok, why = _probe_cdp()
        now = int(time.time())
        with _DIRECT_LOCK:
            _CDP_HEALTH["last_check"] = now
            _CDP_HEALTH["ok"] = ok
            _CDP_HEALTH["last_err"] = why
            _CDP_HEALTH["fail_streak"] = 0 if ok else _CDP_HEALTH["fail_streak"] + 1
            streak = _CDP_HEALTH["fail_streak"]
            direct_ok_recent = (now - int(_DIRECT_STAT.get("last_ok_at") or 0)) < 300
        if ok:
            continue
        _boot_log("[watchdog] CDP 异常（连续 %d 次）：%s" % (streak, why))
        if AUTO_HEAL != "restart":
            if streak == 1 or streak % 10 == 0:
                _boot_log("[watchdog] auto_heal=report，仅上报不重启；"
                          "想让反代自动重启 WorkBuddy，把 config.json 的 auto_heal 改成 restart")
            continue
        if direct_ok_recent:
            _boot_log("[watchdog] 直连通道 5 分钟内仍成功，跳过重启（避免打断正在用的 WorkBuddy）")
            continue
        if streak < 2:
            continue
        if WM is None or not hasattr(WM, "restart_wb"):
            _boot_log("[watchdog] workmom_core 无 restart_wb，无法自愈")
            continue
        _boot_log("[watchdog] 连续 %d 次探活失败且直连不可用，重启 WorkBuddy 自愈" % streak)
        try:
            WM.restart_wb()
            with _DIRECT_LOCK:
                _CDP_HEALTH["healed_at"] = int(time.time())
                _CDP_HEALTH["heal_count"] += 1
                _CDP_HEALTH["fail_streak"] = 0
            _boot_log("[watchdog] 已发出重启指令，等待 WorkBuddy 起来")
        except Exception as e:
            _boot_log("[watchdog] 重启失败：%s" % str(e)[:200])
        time.sleep(45)


def _health_extra():
    """给 /health、/relay_state 共用的运行时状态块。"""
    with _DIRECT_LOCK:
        direct = dict(_DIRECT_STAT)
        cdp = dict(_CDP_HEALTH)
    with _USAGE_LOCK:
        usage = {
            "requests": _USAGE["req"],
            "tokens_in": _USAGE["prompt_tokens"],
            "tokens_out": _USAGE["completion_tokens"],
            "by_model": {k: dict(v) for k, v in list(_USAGE["by_model"].items())[:20]},
            "by_kind": dict(_USAGE["by_kind"]),
            "by_day": {k: dict(v) for k, v in sorted(_USAGE["by_day"].items())[-7:]},
            "file": _USAGE_FILE,
        }
    cooldowns = []
    if WT is not None:
        try:
            cooldowns = WT.cooldown_snapshot()
        except Exception:
            cooldowns = []
    quotas = {}
    if WT is not None:
        try:
            quotas = WT.quota_snapshot()
        except Exception:
            quotas = {}
    return {
        "transport": TRANSPORT,
        "transport_ready": {"direct": WT is not None and RS is not None,
                            "cdp": True},
        "max_concurrency": MAX_CONCURRENCY,
        "auto_heal": AUTO_HEAL,
        "fallback_models": FALLBACK_MODELS,
        "direct": direct,
        "cdp_health": cdp,
        "cooldowns": cooldowns,
        "quotas": quotas,
        "quota_switch_percent": QUOTA_SWITCH_PERCENT,
        "quota_warn_percent": QUOTA_WARN_PERCENT,
        "audit_left": _AUDIT_LEFT[0],
        "slim": {"enabled": _slim_cfg()["enabled"],
                 "requests": _SLIM_STAT.get("n") or 0,
                 "saved_est_total": _SLIM_STAT.get("saved_est") or 0,
                 "last": _SLIM_STAT.get("last") or {}},
        "usage": usage,
    }


PANEL_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>workmom 反代面板</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#252a34;--txt:#e6e8ee;--dim:#8b93a6;--ok:#3ecf8e;--warn:#f2b544;--err:#ef5b5b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:22px 18px 60px}
h1{font-size:19px;margin:0 0 2px}
.sub{color:var(--dim);font-size:12px;margin-bottom:18px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:10px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 13px}
.card .k{color:var(--dim);font-size:11px;letter-spacing:.4px;text-transform:uppercase}
.card .v{font-size:17px;margin-top:3px;word-break:break-all}
.card .v.sm{font-size:13px}
.sec{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:13px;margin-bottom:14px}
.sec h2{font-size:13px;margin:0 0 10px;color:var(--dim);font-weight:600;letter-spacing:.5px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--dim);font-weight:500}
tr:last-child td{border-bottom:none}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:1px}
.ok{background:var(--ok)}.warn{background:var(--warn)}.err{background:var(--err)}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
pre{background:#0b0d11;border:1px solid var(--line);border-radius:8px;padding:10px;overflow:auto;font-size:12px;margin:0;max-height:220px}
.chip{display:inline-block;background:#20252f;border:1px solid var(--line);border-radius:20px;padding:1px 9px;font-size:12px;margin:2px 4px 2px 0}
a{color:#6ab7ff;text-decoration:none}
</style></head><body><div class="wrap">
<h1>workmom 反代面板</h1>
<div class="sub">本页每 3 秒自动刷新 · <a href="/health" target="_blank">/health</a> · <a href="/relay_state" target="_blank">/relay_state</a> · <a href="/v1/models" target="_blank">/v1/models</a></div>

<div class="grid" id="grid"></div>

<div class="sec"><h2>通道</h2><div id="chan"></div></div>
<div class="sec"><h2>模型冷却（上游限流自动熔断）</h2><div id="cool"></div></div>
<div class="sec"><h2>用量（持久化到 logs/usage.jsonl）</h2><div id="usage"></div></div>
<div class="sec"><h2>最近上游错误</h2><div id="errs"></div></div>
<div class="sec"><h2>原始 /health</h2><pre id="raw">加载中…</pre></div>

<script>
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function ago(t){if(!t)return "-";var d=Math.floor(Date.now()/1000-t);if(d<0)d=0;if(d<60)return d+" 秒前";if(d<3600)return Math.floor(d/60)+" 分钟前";if(d<86400)return Math.floor(d/3600)+" 小时前";return Math.floor(d/86400)+" 天前";}
function dot(cls){return '<span class="dot '+cls+'"></span>';}
function card(k,v,sm){return '<div class="card"><div class="k">'+k+'</div><div class="v'+(sm?' sm':'')+'">'+v+'</div></div>';}

function render(h,s){
  var cd=h.cdp_health||{}, d=h.direct||{}, tr=h.transport||"?";
  var ready=h.transport_ready||{};
  var cdpOk = String(h.cdp||"").indexOf("ok")===0;
  var directOk = d.last_ok_at && (Math.floor(Date.now()/1000)-d.last_ok_at < 1800);
  var up=h.uptime_sec||0;
  document.getElementById("grid").innerHTML =
    card("反代", dot("ok")+esc(h.relay_port||"")+" 端口，在线 "+(up>3600?Math.floor(up/3600)+"h"+(Math.floor(up%3600/60))+"m":Math.floor(up/60)+" 分钟"),1)+
    card("当前模型", esc(h.model||"-"),1)+
    card("传输", dot(ready.direct?"ok":"warn")+esc(tr),1)+
    card("直连通道", dot(directOk?"ok":"warn")+(directOk?"最近成功 "+ago(d.last_ok_at):"暂无成功记录"),1)+
    card("渲染进程 CDP", dot(cdpOk?"ok":"err")+(cdpOk?"正常":"异常"),1)+
    card("自愈模式", esc(h.auto_heal||"-"),1)+
    card("并发上限", esc(h.max_concurrency||"-"),1)+
    card("累计请求", esc((h.usage||{}).requests||0),1);

  var directLine = '<tr><td>直连（真流式）</td><td>'+(ready.direct?'<span class="dot ok"></span>可用':'<span class="dot err"></span>不可用')+'</td><td>成功 '+esc(d.ok||0)+' · 失败 '+esc(d.fail||0)+' · 最近成功 '+ago(d.last_ok_at)+'</td></tr>';
  var cdpLine = '<tr><td>CDP（WorkBuddy 渲染进程）</td><td>'+(cdpOk?'<span class="dot ok"></span>可用':'<span class="dot err"></span>'+esc(h.cdp||""))+'</td><td>连续失败 '+esc(cd.last_fail_streak||0)+' 次 · 检查于 '+ago(cd.last_check)+(cd.last_err?' · '+esc(cd.last_err):'')+'</td></tr>';
  document.getElementById("chan").innerHTML = '<table><tbody>'+directLine+cdpLine+'</tbody></table>'+
    '<div style="margin-top:8px">降级链：'+((h.fallback_models||[]).map(function(m){return '<span class="chip">'+esc(m)+'</span>';}).join("")||'-')+'</div>';

  var co=h.cooldowns||[];
  document.getElementById("cool").innerHTML = co.length
    ? '<table><thead><tr><th>模型</th><th>剩余</th><th>原因</th><th>重置时间</th></tr></thead><tbody>'+co.map(function(c){
        var left = c.until ? Math.max(0, c.until - Math.floor(Date.now()/1000)) : 0;
        return '<tr><td>'+esc(c.model)+'</td><td>'+left+' 秒</td><td>'+esc(c.reason||c.kind||"-")+'</td><td class="mono">'+esc(c.reset_at||"-")+'</td></tr>';
      }).join("")+'</tbody></table>'
    : '<div style="color:var(--dim)">当前无模型处于冷却。</div>';

  var u=h.usage||{}, bm=u.by_model||{}, keys=Object.keys(bm);
  var head='<table><thead><tr><th>模型</th><th>请求</th><th>输入 tokens</th><th>输出 tokens</th></tr></thead><tbody>';
  var rows = keys.length ? keys.map(function(k){var v=bm[k]||{};return '<tr><td class="mono">'+esc(k)+'</td><td>'+esc(v.req||0)+'</td><td>'+esc(v.prompt_tokens||0)+'</td><td>'+esc(v.completion_tokens||0)+'</td></tr>';}).join("")
           : '<tr><td colspan="4" style="color:var(--dim)">暂无数据</td></tr>';
  document.getElementById("usage").innerHTML='<div style="margin-bottom:8px">合计：'+esc(u.requests||0)+' 请求 · 输入 '+esc(u.tokens_in||0)+' · 输出 '+esc(u.tokens_out||0)+' · 账本 <span class="mono">'+esc(u.file||"")+'</span></div>'+head+rows+'</tbody></table>';

  var er=(s&&s.errors)||[];
  document.getElementById("errs").innerHTML = er.length
    ? '<table><thead><tr><th>时间</th><th>模型</th><th>状态</th><th>信息</th></tr></thead><tbody>'+er.slice().reverse().map(function(e){
        return '<tr><td class="mono">'+ago(e.at)+'</td><td class="mono">'+esc(e.model)+'</td><td>'+esc(e.status||"-")+(e.code6004?' <span class="chip">6004</span>':'')+'</td><td>'+esc(e.msg||e.error||"")+'</td></tr>';
      }).join("")+'</tbody></table>'
    : '<div style="color:var(--dim)">暂无错误。</div>';

  document.getElementById("raw").textContent = JSON.stringify(h,null,2).slice(0,6000);
}

function tick(){
  fetch("/health").then(function(r){return r.json();}).then(function(h){
    return fetch("/relay_state").then(function(r){return r.json();}).then(function(s){render(h,s);})
      .catch(function(){render(h,null);});
  }).catch(function(e){
    document.getElementById("raw").textContent = "无法连接反代：" + e;
  });
}
tick(); setInterval(tick,3000);
</script>
</div></body></html>
"""


def _panel_html():
    """面板页：优先用同目录 panel.html（方便用户自己改），否则用内置版本。"""
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    return PANEL_HTML


def _boot_log(msg):
    """pythonw 下 sys.stdout 是 None，print 会直接抛异常把进程干掉，所以统一走这里。"""
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        os.makedirs(os.path.join(WM.BASE, "logs"), exist_ok=True)
        with open(os.path.join(WM.BASE, "logs", "relay.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _port_taken(port, host="127.0.0.1", timeout=1.0):
    """127.0.0.1:port 上是否已有进程在监听。

    Windows 下 SO_REUSEADDR 允许两个进程同时绑同一端口且都不报错，
    结果是两个反代同时 LISTENING、请求被先启动的那个吞掉，排障时极难查。
    所以启动前先探一下，被占用就明确报错，不做静默双绑。
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def main():
    # 端口优先级：命令行参数 > config.json 的 relay_port > 18793
    # 之前只认 argv，run_relay.vbs 又写死 18793，导致 config.json 里改了 relay_port 不生效
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    else:
        try:
            port = int(_cfgval("relay_port", 18793) or 18793)
        except Exception:
            port = 18793
    if _port_taken(port):
        _boot_log("!! 端口 %d 已有程序在监听，为避免两个反代抢同一端口，本次不启动。"
                  % port)
        _boot_log("   反代可能已在运行（先跑 stop_all.bat 再启动）；"
                  "或改 config.json 的 relay_port 换一个空闲端口。")
        raise SystemExit(2)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except Exception as e:
        _boot_log("!! 启动失败（端口 %d 可能被占用）: %s: %s" % (port, type(e).__name__, e))
        raise SystemExit(1)
    srv.daemon_threads = True
    _boot_log("[relay] %s listening on http://127.0.0.1:%d（当前模型 %s）"
              % (WM.APP_NAME, port, DEFAULT_MODEL))
    _boot_log("[relay] protocols: /v1/chat/completions | /v1/messages | /v1/responses")
    _boot_log("[relay] upstream via CDP %s:%d%s" % (CDP_HOST, CDP_PORT, UPSTREAM_PATH))
    if WT is not None and RS is not None:
        _boot_log("[relay] transport=%s（direct 直连真流式可用，max_concurrency=%d，"
                  "降级链 %s）" % (TRANSPORT, MAX_CONCURRENCY, FALLBACK_MODELS or [DEFAULT_MODEL]))
    else:
        _boot_log("[relay] transport=%s（wm_transport/relay_stream 未就绪，仅 CDP 伪流式）"
                  % TRANSPORT)
    _boot_log("[relay] auto_heal=%s（report=只上报 / restart=卡死自动重启 WorkBuddy / off=关闭）"
              % AUTO_HEAL)
    if AUTO_HEAL != "off":
        threading.Thread(target=_watchdog, name="wm-watchdog", daemon=True).start()
    _boot_log("[relay] state: http://127.0.0.1:%d/health  http://127.0.0.1:%d/relay_state"
              % (port, port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
