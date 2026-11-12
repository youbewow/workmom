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

import websocket

import relay_protocols as RP

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


def build_upstream_body(oai_req, model):
    """把【OpenAI 形状】的请求体加工成上游 body（含 max_tokens 抬升策略）。"""
    body = {
        "model": model,
        "messages": oai_req.get("messages") or [],
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
    """执行一次上游调用 + 空正文放大重试。

    返回 dict：
      成功 {"ok": True, "res": <聚合结果>, "model": <实际模型>, "elapsed": 秒}
      失败 {"ok": False, "detail": "错误描述", "model": <模型>}
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
        return {"ok": False, "model": model, "type": "cdp_timeout", "detail": detail}
    except Exception as e:
        _record_upstream_error(model, {"kind": "relay", "detail": "relay upstream failed: %s" % str(e)[:400]})
        return {"ok": False, "model": model, "type": "api_error",
                "detail": "relay upstream failed: %s" % str(e)[:500]}

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
        return {"ok": False, "model": model, "type": "upstream_error",
                "detail": "workbuddy upstream error: %s %s" % (res.get("detail"), res.get("body") or "")}

    _LAST_OK[0] = time.time()
    return {"ok": True, "res": res, "model": res.get("model") or model,
            "elapsed": round(time.time() - t0, 1)}


# ----------------------------------------------------------------------------
# HTTP 层
# ----------------------------------------------------------------------------

def _sse(obj):
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


def _sse_named(event, obj):
    return ("event: %s\ndata: %s\n\n" % (event, json.dumps(obj, ensure_ascii=False))).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "workmom/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[relay] " + (fmt % args) + "\n")

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _begin_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
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
        if path in ("/health", ""):
            try:
                _get_conn()
                up = "ok"
                hint = ""
            except Exception as e:
                up = "error: " + str(e)[:200]
                hint = ("CDP %d 连不上：确认 WorkBuddy 已启动并带 --remote-debugging-port=%d 参数，"
                        "浏览器访问 http://127.0.0.1:%d/json/version 应能返回 JSON。"
                        % (CDP_PORT, CDP_PORT, CDP_PORT))
            _cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
            _wb = str(_cfgval("wb_exe", "") or "")
            return self._json(200, {"status": "ok", "app": "workmom", "cdp": up, "hint": hint,
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
                                    "upstream": "http://%s:%d%s" % (CDP_HOST, CDP_PORT, UPSTREAM_PATH)})
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

    # -- OpenAI Chat Completions（原生） -------------------------------------
    def chat(self, req):
        _bump("chat")
        want_stream = bool(req.get("stream"))
        r = run_relay(req, self.headers.get("X-WM-Model"))
        if not r["ok"]:
            code = 504 if r.get("type") == "cdp_timeout" else 502
            return self._json(code, {"error": {"message": r["detail"], "type": r.get("type") or "api_error"}})

        res = r["res"]
        content = res.get("content") or ""
        tool_calls = res.get("tool_calls") or []
        finish = res.get("finish") or ("tool_calls" if tool_calls else "stop")
        usage = res.get("usage") or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        cid = res.get("id") or "wb-chat"
        cmodel = r["model"]

        if not want_stream:
            msg = {"role": "assistant", "content": content}
            if res.get("reasoning"):
                msg["reasoning_content"] = res["reasoning"]
            if tool_calls:
                msg["tool_calls"] = tool_calls
                if not content:
                    msg["content"] = None
            return self._json(200, {
                "id": cid, "object": "chat.completion", "created": int(time.time()),
                "model": cmodel,
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": usage,
            })

        # 流式：整体拿到后再按 chunk 吐出（上游是一次性返回全文）
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
        r = run_relay(oai_req, self.headers.get("X-WM-Model"))
        if not r["ok"]:
            code = 504 if r.get("type") == "cdp_timeout" else 502
            return self._json(code, {"type": "error",
                                     "error": {"type": r.get("type") or "api_error",
                                               "message": r["detail"]}})
        res, model = r["res"], r["model"]

        if not meta["stream"]:
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
        r = run_relay(oai_req, self.headers.get("X-WM-Model"))
        if not r["ok"]:
            code = 504 if r.get("type") == "cdp_timeout" else 502
            return self._json(code, {"error": {"message": r["detail"],
                                               "type": r.get("type") or "api_error",
                                               "code": "upstream_error"}})
        res, model = r["res"], r["model"]

        if not meta["stream"]:
            return self._json(200, RP.openai_to_responses(res, model))

        self._begin_stream()
        try:
            for name, data in RP.responses_stream_events(res, model):
                self.wfile.write(_sse_named(name, data))
            self.wfile.flush()
        except Exception as e:
            sys.stderr.write("[relay] responses stream write failed: %s\n" % e)
        return


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


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18793
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
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
