# -*- coding: utf-8 -*-
"""wm_transport.py — workmom 直连传输层（P0 架构升级核心）

背景
----
旧实现把上游调用塞进 WorkBuddy 渲染进程（CDP -> window.wb.http.post），
带来四个硬伤：

  P0-1 伪流式：wb.http.post 是「整段 await 回字符串」，页面里既不走 XHR 也不走
       window.fetch（实测两者 hook 计数恒为 0，走的是 Electron 主进程 IPC），
       所以页面侧根本挂不到传输层；Python 侧只能等全文到齐再按 512 字符切片吐。
  P0-2 恒串行：server.py 用一个全局 LOCK 包住整个 CDP eval 往返，并发恒为 1。
  P0-3 卡死：渲染进程主线程一卡，所有请求一起挂，只能重启 WorkBuddy。
  P0-0 错误失真：上游 429/code 6004 被统一翻成 502 空壳，客户端当网关故障处理，
       用户看不到「额度超限、X 点重置」。

本模块用账号凭证直接打上游 HTTPS API（与 WB 主进程同一条路），
一次解决上述四点：真流式、无锁并发、不依赖渲染进程、状态码原样结构化。

对外接口
--------
    resolve_token(force=False)  -> access token 字符串（带缓存/自动刷新）
    DirectStream(...)           -> 上下文管理器，真流式逐行产出上游 SSE
    parse_error(status, body, headers) -> UpstreamError（结构化错误）
    note_quota(model, quota)    -> 记录配额百分比与重置时间
    quota_snapshot()            -> 供 /relay_state 展示
    cooldown_*                  -> 模型级熔断（6004 后快速失败，不乱打上游）
"""

import json
import os
import re
import sys
import time
import threading
import urllib.error
import urllib.request

# workmom_core 与本文件同目录，直接 import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import workmom_core as WM  # noqa: E402


# ====================================================================== 常量
DEFAULT_TIMEOUT = 300          # socket 级 idle 超时（真流式下等于「两行之间最长静默」）
CONNECT_TIMEOUT = 30           # 建连超时
TOKEN_MIN_TTL_MS = 6 * 3600 * 1000   # 剩余有效期低于 6h 就顺势刷新
QUOTA_TTL = 1800               # 配额快照保留 30 分钟

# 429/code 6004 消息里抓重置时间，例：将在 2026-11-13 05:56:45 UTC+8 重置
_RE_RESET = re.compile(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")

_LOG = lambda *_a: None        # 由 server.py 注入


def set_logger(fn):
    global _LOG
    _LOG = fn or (lambda *_a: None)


# ====================================================================== 错误
class UpstreamError(Exception):
    """上游错误的结构化表达。

    http     建议回给客户端的 HTTP 状态码（429/401/400/502/504）
    status   上游真实状态码（可能为 None，表示未拿到响应）
    code     上游业务码字符串，如 "6004" / "11102"
    type     OpenAI 风格 error.type，如 rate_limit_error
    message  给用户看的中文原文（优先上游 displayMsg.zh / msg）
    reset_at 限流重置时间（人可读字符串）
    retry_after 建议等待秒数
    raw      上游原始 body（截断）
    """

    def __init__(self, http=502, status=None, code="", type="api_error",
                 message="upstream error", reset_at="", retry_after=0, raw="",
                 fallbackable=False):
        super().__init__(message)
        self.http = http
        self.status = status
        self.code = str(code or "")
        self.type = type
        self.message = message
        self.reset_at = reset_at or ""
        self.retry_after = int(retry_after or 0)
        self.raw = raw or ""
        # fallbackable=True 表示「换个模型/换条通道还有救」，上层可尝试降级
        self.fallbackable = bool(fallbackable)

    def payload(self):
        err = {"message": self.message, "type": self.type}
        if self.code:
            err["code"] = self.code
        if self.status:
            err["upstream_status"] = self.status
        if self.reset_at:
            err["reset_at"] = self.reset_at
        return {"error": err}

    def __str__(self):
        bits = ["http=%s" % self.http]
        if self.status:
            bits.append("upstream=%s" % self.status)
        if self.code:
            bits.append("code=%s" % self.code)
        return "UpstreamError(%s) %s" % (", ".join(bits), self.message[:160])


def _extract_json(body_bytes):
    """尽力把上游 body 解析成 dict（上游有的错误是 JSON，有的是 openresty HTML）。"""
    if not body_bytes:
        return {}
    try:
        txt = body_bytes.decode("utf-8", "replace").strip()
    except Exception:
        return {}
    if not txt:
        return {}
    try:
        obj = json.loads(txt)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def parse_error(status, body_bytes, headers=None):
    """把上游任意错误响应翻译成 UpstreamError。"""
    headers = headers or {}
    obj = _extract_json(body_bytes)
    raw = (body_bytes or b"")[:800].decode("utf-8", "replace")
    code = obj.get("code") if obj.get("code") is not None else ""
    msg = obj.get("msg") or obj.get("message") or ""

    # 上游给了本地化提示就用本地化提示（displayMsg.zh），比 msg 更像人话
    disp = obj.get("displayMsg") or {}
    if isinstance(disp, dict):
        zh = disp.get("zh") or disp.get("zh-hans") or ""
        tips = obj.get("displayTips") or {}
        tip_zh = tips.get("zh") if isinstance(tips, dict) else ""
        if zh:
            msg = zh if not tip_zh else "%s（%s）" % (zh, tip_zh)

    reset_at = ""
    m = _RE_RESET.search(str(obj.get("msg") or ""))
    if m:
        reset_at = m.group(1)

    retry_after = 0
    try:
        retry_after = int(headers.get("Retry-After") or 0)
    except Exception:
        retry_after = 0

    # --- 401/403：凭证问题 ---------------------------------------------
    if status in (401, 403):
        wa = headers.get("WWW-Authenticate") or ""
        detail = "凭证已失效或无权访问上游"
        if "invalid_token" in wa:
            detail = "账号凭证失效（invalid_token），请重新登录 WorkBuddy 或让中转刷新 token"
        return UpstreamError(http=401, status=status, code=code or "invalid_token",
                             type="authentication_error", message=detail, raw=raw,
                             fallbackable=False)

    # --- 429 / 6004：限流（本项目的核心场景） ---------------------------
    if status == 429 or str(code) == "6004":
        if not msg:
            msg = "上游限流：当前账号该模型的使用量已超出频率限制"
        if reset_at:
            msg_cn = "%s（将于 %s 重置）" % (msg, reset_at)
        else:
            msg_cn = msg
        return UpstreamError(http=429, status=status or 429, code=code or "6004",
                             type="rate_limit_error", message=msg_cn,
                             reset_at=reset_at, retry_after=retry_after or 60,
                             raw=raw, fallbackable=True)

    # --- 400 / 422：请求非法 -------------------------------------------
    if status in (400, 422):
        if not msg:
            msg = "上游拒绝了该请求：参数不合法"
        return UpstreamError(http=400, status=status, code=code or "invalid_request",
                             type="invalid_request_error", message=msg, raw=raw,
                             fallbackable=(str(code) == "11102"))

    # --- 404：模型不存在 -----------------------------------------------
    if status == 404:
        return UpstreamError(http=404, status=status, code=code or "not_found",
                             type="invalid_request_error",
                             message=msg or "上游不存在该资源（模型名可能写错）",
                             raw=raw, fallbackable=True)

    # --- 5xx / 其它 ----------------------------------------------------
    if not msg:
        msg = "上游服务异常（HTTP %s）" % status
    return UpstreamError(http=502, status=status, code=code or "upstream_5xx",
                         type="api_error", message=msg, raw=raw, fallbackable=True)


# ====================================================================== 凭证
class _TokenManager(object):
    """access token 缓存：同一 uid 复用，临期自动刷新，刷新失败则强制重试一次。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._token = ""
        self._uid = ""
        self._got_at = 0.0

    def _load(self, force=False):
        uid = WM.current_uid()
        if not uid:
            raise UpstreamError(http=502, code="no_account",
                                type="api_error",
                                message="未找到 WorkBuddy 登录账号，请先登录 WorkBuddy 桌面端",
                                fallbackable=False)
        r = WM.ensure_fresh_token(uid, force=force)
        token = ""
        err = ""
        if isinstance(r, (tuple, list)):
            for x in r:
                if isinstance(x, str) and len(x) > 200:
                    token = x
                elif isinstance(x, str) and not err:
                    err = x
        elif isinstance(r, str):
            token = r
        if not token:
            raise UpstreamError(http=401, code="token_unavailable",
                                type="authentication_error",
                                message="取不到可用凭证：%s" % (err or "备份不存在"),
                                fallbackable=False)
        self._token = token
        self._uid = uid
        self._got_at = time.time()
        return token

    def get(self, force=False, stale_token=""):
        """stale_token 非空表示「这个 token 刚被上游拒绝」，需要强制换新。"""
        with self._lock:
            uid_now = WM.current_uid()
            need = (force or not self._token or self._uid != uid_now
                    or (stale_token and stale_token == self._token)
                    or (time.time() - self._got_at) > 3600)
            if need:
                self._load(force=bool(force or stale_token))
            return self._token

    def invalidate(self, token=""):
        with self._lock:
            if not token or token == self._token:
                self._token = ""
                self._got_at = 0.0


TOKEN = _TokenManager()


def resolve_token(force=False):
    return TOKEN.get(force=force)


# ====================================================================== 配额
_QUOTA_LOCK = threading.Lock()
_QUOTA = {}   # model -> {"percent":..,"reset_at":..,"at":ts,"raw":..}


def note_quota(model, quota):
    """从响应尾部的 usage.quota 记录配额，供面板展示与预警。

    记下 uid 很关键：额度是按「账号×模型」算的，但 _QUOTA 的 key 只有 model，
    换号之后同一个 key 上挂的还是上一个号的旧百分比。守护靠 uid 判断这条记录是不是当前号的，
    否则会把旧号的 92% 当成新号的额度，刚切过去就又被判死。
    """
    if not isinstance(quota, dict) or not quota:
        return
    key = model or quota.get("model") or ""
    try:
        uid = WM.current_uid() or ""
    except Exception:
        uid = ""
    with _QUOTA_LOCK:
        _QUOTA[key] = {
            "model": quota.get("model") or key,
            "percent": quota.get("percent"),
            "reset_at": quota.get("reset_at"),
            "shared_models": quota.get("shared_models") or [],
            "uid": uid,
            "at": int(time.time()),
        }


def quota_snapshot():
    out = {}
    now = int(time.time())
    with _QUOTA_LOCK:
        for k, v in _QUOTA.items():
            if now - v.get("at", 0) > QUOTA_TTL:
                continue
            item = dict(v)
            # reset_at 是秒级时间戳（字符串），顺手转人可读
            ra = str(item.get("reset_at") or "")
            if ra.isdigit():
                item["reset_at_human"] = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(int(ra)))
            out[k] = item
    return out


# ====================================================================== 熔断
_COOLDOWN_LOCK = threading.Lock()
_COOLDOWN = {}   # model -> {"until":ts,"reset_at":str,"msg":str,"code":str}


def cooldown_set(model, secs, reset_at="", msg="", code=""):
    if not model or secs <= 0:
        return
    with _COOLDOWN_LOCK:
        _COOLDOWN[model] = {"until": time.time() + secs,
                            "reset_at": reset_at or "", "msg": msg or "",
                            "code": str(code or "")}
    _LOG("熔断 %s：%.0fs 内不再打上游（%s）" % (model, secs, reset_at or msg[:40]))


def cooldown_get(model):
    with _COOLDOWN_LOCK:
        v = _COOLDOWN.get(model)
        if not v:
            return None
        if v["until"] <= time.time():
            _COOLDOWN.pop(model, None)
            return None
        item = dict(v)
        item["left"] = max(0, int(v["until"] - time.time()))
        return item


def cooldown_clear(model=""):
    with _COOLDOWN_LOCK:
        if model:
            _COOLDOWN.pop(model, None)
        else:
            _COOLDOWN.clear()


def cooldown_snapshot():
    with _COOLDOWN_LOCK:
        now = time.time()
        return {k: {"left": max(0, int(v["until"] - now)),
                    "reset_at": v.get("reset_at", ""),
                    "msg": v.get("msg", "")[:120]}
                for k, v in _COOLDOWN.items() if v["until"] > now}


# ====================================================================== 域名
def _hosts():
    """主域名 + 备用域名（国内版 workbuddy.cn / 国际版 workbuddy.ai / 同源 codebuddy.cn）。"""
    try:
        primary = (WM.cfg().get("api_host") or "https://www.workbuddy.cn").rstrip("/")
    except Exception:
        primary = "https://www.workbuddy.cn"
    cands = [primary]
    for h in ("https://www.workbuddy.cn", "https://www.codebuddy.cn",
              "https://www.workbuddy.ai"):
        if h not in cands:
            cands.append(h)
    return cands


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
HOST_STATE = {"good": ""}      # 记住最后一次成功的域名，减少切换成本


def _headers(token, stream=True, extra=None):
    h = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/event-stream" if stream else "application/json",
        # 不要压缩：压缩流会让逐行读变成整块解压，真流式就没了
        "Accept-Encoding": "identity",
    }
    if extra:
        h.update(extra)
    return h


# ====================================================================== 流式
class DirectStream(object):
    """真流式上游调用。

    用法：
        with DirectStream(body_bytes, model="kimi-k3.1-pro", stream=True) as st:
            st.meta           # {"status":200,"host":...,"headers":{...}}
            for line in st.lines():   # bytes，逐行，实时
                ...
            st.usage / st.quota

    非流式：
        with DirectStream(...) as  st:
            data = st.read_all()      # bytes（完整 JSON）
    """

    def __init__(self, body, model="", stream=True, timeout=DEFAULT_TIMEOUT, path=None):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.model = model or ""
        self.stream = bool(stream)
        self.timeout = timeout or DEFAULT_TIMEOUT
        self.path = path or "/v2/chat/completions"
        self.meta = {}
        self.usage = None
        self.quota = None
        self.token = ""
        self._resp = None
        self._host = ""
        self._retried_token = False
        self._retried_host = 0

    # ---------------- 上下文管理 ----------------
    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self):
        r, self._resp = self._resp, None
        if r is not None:
            try:
                r.close()
            except Exception:
                pass

    # ---------------- 建连 ----------------
    def open(self):
        """建连；401 会自动刷新 token 重试一次，域名连接失败会顺延备用域名。"""
        hosts = _hosts()
        if HOST_STATE["good"] in hosts:
            hosts.remove(HOST_STATE["good"])
            hosts.insert(0, HOST_STATE["good"])

        last_err = None
        for host in hosts:
            try:
                self._open_one(host)
                HOST_STATE["good"] = host
                return self
            except UpstreamError as e:
                last_err = e
                if e.fallbackable and e.http == 502:
                    # 网络层问题：换域名再试
                    _LOG("直连 %s 失败（%s），尝试下一个域名" % (host, e.message[:60]))
                    continue
                raise
            except Exception as e:
                last_err = UpstreamError(
                    http=502, status=None, code="connect_failed", type="api_error",
                    message="连接上游失败：%s" % str(e)[:160], fallbackable=True)
                _LOG("直连 %s 异常：%s" % (host, str(e)[:120]))
                continue
        raise last_err or UpstreamError(http=502, message="无法连接上游")

    def _open_one(self, host):
        token = TOKEN.get()
        url = host + self.path
        req = urllib.request.Request(
            url, data=self.body, method="POST",
            headers=_headers(token, stream=self.stream))
        t0 = time.time()
        try:
            resp = _OPENER.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            # 4xx/5xx：读 body 解析成结构化错误
            try:
                body = e.read()
            except Exception:
                body = b""
            hdrs = dict(e.headers.items()) if e.headers else {}
            err = parse_error(e.code, body, hdrs)
            # token 过期：强制刷新后原地重试一次
            if err.http == 401 and not self._retried_token:
                self._retried_token = True
                TOKEN.invalidate(token)
                _LOG("凭证被上游拒绝，刷新后重试一次")
                return self._open_one(host)
            raise err
        except urllib.error.URLError as e:
            raise UpstreamError(http=502, status=None, code="connect_failed",
                                type="api_error",
                                message="连接上游失败：%s" % str(e.reason)[:160],
                                fallbackable=True)
        except Exception as e:
            raise UpstreamError(http=502, status=None, code="connect_failed",
                                type="api_error",
                                message="连接上游异常：%s" % str(e)[:160],
                                fallbackable=True)

        self._resp = resp
        self._host = host
        self.token = token
        hdrs = {}
        try:
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
        except Exception:
            pass
        self.meta = {"status": getattr(resp, "status", 200), "host": host,
                     "ctype": hdrs.get("content-type", ""),
                     "headers": hdrs,
                     "ttfb_ms": int((time.time() - t0) * 1000)}
        _LOG("直连 %s 建连成功 ttfb=%dms" % (host, self.meta["ttfb_ms"]))

    # ---------------- 读流 ----------------
    def lines(self):
        """逐行产出上游 SSE 原始字节行（实时，不缓存）。

        顺带从 chunk 里捞 usage / quota，供配额面板使用。
        """
        resp = self._resp
        if resp is None:
            return
        try:
            for raw in resp:
                if not raw:
                    continue
                # 捞配额：只有含 quota 的行才解析，避免每行都 json.loads
                if b"quota" in raw or b"usage" in raw:
                    self._sniff(raw)
                yield raw
        except Exception as e:
            # 流中途断开要显式抛出，让上层能生成一条「上游中断」错误事件
            raise UpstreamError(http=502, status=self.meta.get("status"),
                                code="stream_interrupted", type="api_error",
                                message="上游流式响应中断：%s" % str(e)[:160],
                                fallbackable=False)

    def read_all(self):
        """非流式：把 body 读完（bytes）。"""
        resp = self._resp
        if resp is None:
            return b""
        try:
            data = resp.read()
        except Exception as e:
            raise UpstreamError(http=502, status=self.meta.get("status"),
                                code="read_failed", type="api_error",
                                message="读取上游响应失败：%s" % str(e)[:160])
        if b"quota" in data or b"usage" in data:
            for ln in data.split(b"\n"):
                if ln.startswith(b"data:"):
                    self._sniff(ln)
        return data

    def _sniff(self, raw):
        """从一行 SSE 里抽取 usage.quota。"""
        try:
            s = raw.decode("utf-8", "replace").strip()
            if s.startswith("data:"):
                s = s[5:].strip()
            if not s or s == "[DONE]" or not s.startswith("{"):
                return
            obj = json.loads(s)
        except Exception:
            return
        u = obj.get("usage")
        if isinstance(u, dict):
            self.usage = u
            note_quota(self.model, u.get("quota"))


# ====================================================================== 便捷
def stream_chat(body, model="", timeout=DEFAULT_TIMEOUT):
    """便捷入口：返回已建连的 DirectStream（调用方负责 close / with）。"""
    return DirectStream(body, model=model, stream=True, timeout=timeout)


def post_json(body, model="", timeout=CONNECT_TIMEOUT + 240):
    """非流式一次取全（内部仍走 SSE 读取后聚合，调用方一般不用）。"""
    st = DirectStream(body, model=model, stream=False, timeout=timeout)
    st.open()
    return st


def health(timeout=8):
    """轻量探活：验证 token 可用（打一个极小请求，失败也算拿到状态码）。"""
    ok = True
    detail = {}
    try:
        tok = resolve_token()
        detail["token_len"] = len(tok)
        detail["uid"] = WM.current_uid()
    except UpstreamError as e:
        return False, {"error": e.message, "code": e.code}
    except Exception as e:
        return False, {"error": str(e)[:160]}
    # 用 wb-lite（最省的模型）试一发
    try:
        body = json.dumps({"model": "wb-lite",
                           "messages": [{"role": "user", "content": "hi"}],
                           "stream": True, "max_tokens": 8}).encode()
        with DirectStream(body, model="wb-lite", stream=True, timeout=timeout) as st:
            n = 0
            for _ in st.lines():
                n += 1
                if n >= 2:
                    break
        detail["probe"] = "ok"
    except UpstreamError as e:
        ok = e.http in (429,)   # 限流也说明链路通
        detail["probe"] = {"http": e.http, "code": e.code, "msg": e.message[:160]}
    except Exception as e:
        ok = False
        detail["probe"] = str(e)[:160]
    return ok, detail
