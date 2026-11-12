#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 账号自动轮换守护进程（配合 wb-k3relay 反代）
=========================================================

干什么：
  反代 18793 一旦把上游的 429 / code:6004（kimi-k3.1-pro 额度墙）暴露出来，
  本守护进程会自动按队列把 WorkBuddy 切到下一个还有额度的账号：
      选号 → node switch.js <uid> → 重启 WorkBuddy(带 9226) → 等 CDP → 探 pro 验证
  验证通过就停手；下一个号也爆就继续往下走；整队全爆则退避 15 分钟再来。

为什么零成本：
  不主动轮询探测（那样会烧 token）。反代新增了 GET /relay_state，
  只在真的失败过之后才记录错误，守护进程 5 秒读一次这个本地接口即可。

用法：
  python auto_switch.py                # 常驻守护
  python auto_switch.py --status       # 只看当前状态，不动作
  python auto_switch.py --next         # 手动立刻切下一个号
  python auto_switch.py --once         # 常驻循环但只跑一轮检查（调试）
  python auto_switch.py --dry-run      # 配 --next/--once 用，只打印不执行

配置：
  同目录 config.json（可选，覆盖默认值）：
      {"queue":["uid1","uid2"], "poll_interval":5, "cooldown":120}
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(BASE, "logs")
os.makedirs(LOGDIR, exist_ok=True)

if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    import workmom_core as WM      # 只为了让守护能跟随面板选定的模型
except Exception:                  # 单人可跑，缺模块也不影响守护本体
    WM = None


def _active_model(default):
    """守护探测/轮换时用的模型：一律跟随面板当前选定模型，面板换模型即刻生效。"""
    try:
        if WM:
            m = WM.active_model()
            if m:
                return m
    except Exception:
        pass
    return default

# ---------------------------------------------------------------- 默认配置
DEF = {
    # 反代
    "relay_base": "http://127.0.0.1:18793",
    "probe_model": "kimi-k3.1-pro",
    "probe_timeout": 150,
    # WorkBuddy（本机路径请写进 config.json，或保留空串让核心模块自动探测）
    "wb_exe": "",
    "cdp_port": 9226,
    "cdp_wait": 120,
    # WorkDaddy（可选的切号退路工具，留空表示不用）
    "wd_dir": "",
    # 轮换队列：按额度由多到少、按最近使用由少到多排的
    # 本机实际队列请写到 data/guard_queue.json（格式见下），源码里不放任何账号信息：
    #   ["uid-1", "uid-2"]
    # 或带备注：
    #   [{"uid": "uid-1", "note": "备注 ~2456"}]
    "queue": [],
    # 节奏
    "poll_interval": 5,        # 读 /relay_state 的间隔（秒）
    "cooldown": 120,           # 两次切号之间最小间隔（秒）
    "backoff": 900,            # 整队全爆后的退避时间（秒）
    "switch_cooldown": 0,      # 单个号被判定失败的记忆时长（秒），0=不记忆
}

LOG_FILE = os.path.join(LOGDIR, "workmom_guard.log")
STATE_FILE = os.path.join(BASE, "guard_state.json")
LOCK_FILE = os.path.join(BASE, ".guard.lock")

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------- 基础设施
def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        # 超过 2MB 就截断，避免日志无限膨胀
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 2 * 1024 * 1024:
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-2000:]
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _load_queue(cfg):
    """轮换队列：优先 config.json 的 queue；为空则读 data/guard_queue.json。

    两种文件都支持 ["uid1","uid2"] 与 [{"uid":"uid1","note":"备注"}] 两种写法，
    统一归一化成纯 uid 字符串列表；源码里不含任何真实账号。
    """
    raw = cfg.get("queue") or []
    if not raw:
        qp = os.path.join(BASE, "data", "guard_queue.json")
        if os.path.exists(qp):
            try:
                with open(qp, encoding="utf-8") as f:
                    raw = json.load(f) or []
            except Exception as e:
                log("guard_queue.json 读取失败: %s" % e)
                raw = []
    out = []
    for it in raw:
        uid = it.get("uid") if isinstance(it, dict) else it
        if isinstance(uid, str) and uid.strip():
            out.append(uid.strip())
    return out


def load_cfg():
    cfg = dict(DEF)
    p = os.path.join(BASE, "config.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                cfg.update(json.load(f) or {})
        except Exception as e:
            log("config.json 读取失败，用默认配置: %s" % e)
    cfg["queue"] = _load_queue(cfg)
    return cfg


def http_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            pass
    return {}


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- 单实例
def already_running():
    try:
        with open(LOCK_FILE, encoding="utf-8") as f:
            pid = int((f.read() or "0").strip() or 0)
    except Exception:
        return False
    if not pid:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10).stdout or ""
        return str(pid) in out
    except Exception:
        return False


def take_lock():
    with open(LOCK_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


def drop_lock():
    try:
        os.remove(LOCK_FILE)
    except Exception:
        pass


# ---------------------------------------------------------------- 状态查询
def relay_state(cfg):
    try:
        return http_json(cfg["relay_base"] + "/relay_state", timeout=8)
    except Exception:
        return None


def relay_health(cfg):
    try:
        return http_json(cfg["relay_base"] + "/health", timeout=8)
    except Exception:
        return None


def probe(cfg, model=None):
    """走反代打一次极简请求，返回 (ok, info)。ok=True 表示该账号该模型可用。
    model 缺省时跟随面板当前选定模型（不再死磕 kimi-k3.1-pro）。"""
    model = model or _active_model(cfg["probe_model"])
    body = {
        "model": model,
        "stream": False,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "只回一个字：好"}],
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(cfg["relay_base"] + "/v1/chat/completions",
                                 data=data, headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with _OPENER.open(req, timeout=cfg["probe_timeout"]) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        ch = (d.get("choices") or [{}])[0]
        content = ((ch.get("message") or {}).get("content") or "").strip()
        return True, {"http": 200, "secs": round(time.time() - t0, 1),
                      "model": d.get("model"), "content": content[:60]}
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        nailed = ("6004" in txt) or ("429" in txt)
        return False, {"http": e.code, "secs": round(time.time() - t0, 1),
                       "limit": nailed, "body": txt[:400]}
    except Exception as e:
        return False, {"http": None, "secs": round(time.time() - t0, 1),
                       "error": "%s: %s" % (type(e).__name__, e)}


# ---------------------------------------------------------------- 切号动作
def node_exe(cfg):
    p = os.path.join(cfg["wd_dir"], "scripts", "runtime", "node", "node.exe")
    return p if os.path.exists(p) else "node"


def do_switch(cfg, uid, dry=False):
    """调 WorkDaddy 的 switch.js 把登录信息换成目标账号。"""
    js = os.path.join(cfg["wd_dir"], "scripts", "switch.js")
    if not os.path.exists(js):
        log("!! 找不到 switch.js: %s" % js)
        return False
    cmd = [node_exe(cfg), js, uid]
    if dry:
        log("[dry-run] 将执行: %s" % " ".join(cmd))
        return True
    try:
        r = subprocess.run(cmd, cwd=cfg["wd_dir"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except Exception as e:
        log("!! 执行 switch.js 异常: %s" % e)
        return False
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode == 0:          # switch.js 成功即 exit 0，失败 exit 1
        log("   切号成功: %s" % out.replace("\n", " | ")[:240])
        return True
    log("!! 切号失败 rc=%s out=%s err=%s" % (r.returncode, out[:200], err[:200]))
    return False


# ------------------------------------------------------- 无感切号（不重启 WorkBuddy）
# 为什么能不重启：WorkBuddy 的登录态是渲染进程从 auth 文件 + URL 的 accountSnapshot
# 首屏 hydrate 的。换掉登录文件后，只要经 CDP 让页面重新加载（并清掉 URL 里的旧
# accountSnapshot），渲染进程就会用新号重建 window.wb，反代的下一次请求重连即可。
# 两条路：① 优先复用 WorkDaddy 常驻 daemon 的本地 API（/api/switch，它内部已经处理
# 了原生登录态切换、accountSnapshot、Page.reload、定时任务同步）；② daemon 不在时
# 自己来（switch.js 换文件 + 自研 CDP 刷新）。
WD_API_PORTS = (47832, 47833, 47834, 47835)
_WD_API_BASE = {"base": None}

PROFILE_JS = r"""(async function () {
  try {
    var a = globalThis.wb && globalThis.wb.account;
    if (a && typeof a.profile === 'function') {
      var p = await a.profile();
      if (p) return { uid: String(p.uid || ''), nickname: String(p.nickname || ''),
                      isPro: !!p.isPro, href: String(location.href) };
    }
  } catch (e) {}
  return null;
})()"""

CLEAR_SNAPSHOT_JS = r"""(function () {
  try {
    var url = new URL(location.href);
    url.searchParams.delete('accountSnapshot');
    history.replaceState(history.state, '', url.href);
    return !new URL(location.href).searchParams.has('accountSnapshot');
  } catch (e) { return false; }
})()"""


def wd_api_token(cfg):
    """读 WorkDaddy daemon 的本地 API token（64 位 hex，只落在用户数据目录）。"""
    appdata = os.environ.get("APPDATA", "") or ""
    local = os.environ.get("LOCALAPPDATA", "") or ""
    cands = [
        os.path.join(appdata, "WorkDaddy", ".api-token"),
        os.path.join(appdata, "workdaddy", ".api-token"),
        os.path.join(local, "WorkDaddy", ".api-token"),
    ]
    for p in cands:
        try:
            with open(p, encoding="utf-8") as f:
                tok = (f.read() or "").strip()
            if len(tok) == 64:
                return tok
        except Exception:
            continue
    return ""


def _wd_api_get(url, tok, timeout=5):
    req = urllib.request.Request(url, headers={"X-WorkDaddy-Token": tok,
                                               "Accept": "application/json"})
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def wd_api_base(cfg, refresh=False, timeout=3):
    """探出 WorkDaddy daemon 的本地 API 端口（默认 47832 起）。"""
    if not refresh and _WD_API_BASE.get("base"):
        return _WD_API_BASE["base"]
    tok = wd_api_token(cfg)
    if not tok:
        _WD_API_BASE["base"] = None
        return None
    for port in WD_API_PORTS:
        base = "http://127.0.0.1:%d" % port
        try:
            d = _wd_api_get(base + "/api/accounts", tok, timeout=timeout)
            if d.get("ok"):
                _WD_API_BASE["base"] = base
                return base
        except Exception:
            continue
    _WD_API_BASE["base"] = None
    return None


def wd_api_current_uid(cfg):
    base = wd_api_base(cfg)
    if not base:
        return ""
    try:
        d = _wd_api_get(base + "/api/accounts", wd_api_token(cfg), timeout=8)
        return str((d.get("current") or {}).get("uid") or "")
    except Exception:
        return ""


def wd_api_switch(cfg, uid, reload_page=True, timeout=180):
    """走 WorkDaddy 本地 API 切号：daemon 内部做原生/文件切换 + CDP 刷新页面，不重启客户端。"""
    base = wd_api_base(cfg)
    if not base:
        return False, {"mode": "workdaddy-api",
                       "error": "WorkDaddy 本地 API 不可达（daemon 未运行或 token 读不到）"}
    body = {"uid": uid, "reload": bool(reload_page)}
    req = urllib.request.Request(
        base + "/api/switch", data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"X-WorkDaddy-Token": wd_api_token(cfg),
                 "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        return False, {"mode": "workdaddy-api", "error": "HTTP %s %s" % (e.code, txt[:200])}
    except Exception as e:
        return False, {"mode": "workdaddy-api", "error": "%s: %s" % (type(e).__name__, e)}
    ok = bool(d.get("ok")) and str(d.get("uid") or "") == uid
    info = {"mode": "workdaddy-api", "uid": d.get("uid"), "nickname": d.get("nickname"),
            "reloaded": bool(d.get("reloaded")), "hint": d.get("hint") or ""}
    if not ok:
        info["error"] = str(d.get("error") or d.get("hint") or "切换未成功")[:200]
    return ok, info


def _wb_page_ws(cfg, timeout=8):
    url = "http://127.0.0.1:%d/json" % cfg["cdp_port"]
    with _OPENER.open(url, timeout=timeout) as r:
        targets = json.loads(r.read().decode("utf-8", "replace"))
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        raise RuntimeError("CDP 里没有可用页面（WorkBuddy 是否带 --remote-debugging-port=%d 启动？）"
                           % cfg["cdp_port"])
    pages.sort(key=lambda t: 1 if "index.html" in (t.get("url") or "") else 0, reverse=True)
    return pages[0]["webSocketDebuggerUrl"]


class _Cdp(object):
    """极简 CDP 客户端：只用 Runtime.evaluate / Page.reload，够刷新页面就行。"""

    def __init__(self, ws_url, timeout=20):
        import websocket
        self.ws = websocket.create_connection(ws_url, timeout=timeout, max_size=16 * 1024 * 1024,
                                              skip_utf8_validation=True, suppress_origin=True)
        self.mid = 0

    def call(self, method, params=None, timeout=15):
        self.mid += 1
        mid = self.mid
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}},
                                ensure_ascii=False))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self.ws.recv()
            if not raw:
                raise RuntimeError("CDP 连接被关闭")
            msg = json.loads(raw)
            if msg.get("id") != mid:
                continue
            if "error" in msg:
                raise RuntimeError("CDP error: %s" % json.dumps(msg["error"], ensure_ascii=False)[:300])
            res = msg.get("result", {}) or {}
            if res.get("exceptionDetails"):
                d = res["exceptionDetails"].get("exception", {}).get("description")
                raise RuntimeError("页面 JS 异常: %s" % str(d or res["exceptionDetails"])[:300])
            rv = res.get("result", {}) or {}
            return rv.get("value")
        raise RuntimeError("CDP %s 超时" % method)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def wb_profile(cfg, timeout=12):
    """读渲染进程当前账号（window.wb.account.profile()）。"""
    c = _Cdp(_wb_page_ws(cfg))
    try:
        return c.call("Runtime.evaluate",
                      {"expression": PROFILE_JS, "awaitPromise": True, "returnByValue": True},
                      timeout) or {}
    finally:
        c.close()


def reload_wb_page(cfg, uid="", wait=45):
    """自研无感刷新：清 URL 的 accountSnapshot → Page.reload → 等 profile 变成目标号。"""
    c = _Cdp(_wb_page_ws(cfg))
    try:
        c.call("Runtime.evaluate", {"expression": CLEAR_SNAPSHOT_JS, "returnByValue": True}, 10)
        c.call("Page.reload", {"ignoreCache": False}, 15)
    finally:
        c.close()
    time.sleep(3)
    deadline = time.time() + wait
    err = "页面刷新后没读到目标账号 profile"
    while time.time() < deadline:
        try:
            p = wb_profile(cfg)
            if p.get("uid"):
                if not uid or p["uid"] == uid:
                    return True, {"mode": "self-cdp", "uid": p["uid"],
                                  "nickname": p.get("nickname", ""), "isPro": p.get("isPro")}
                err = "刷新后仍是 %s" % p["uid"][:8]
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, str(e)[:120])
        time.sleep(2)
    return False, {"mode": "self-cdp", "error": err}


def do_switch_headless(cfg, uid, dry=False, allow_restart=False):
    """无感切号总入口：daemon API 优先，失败退自研 CDP 刷新，全程不杀 WorkBuddy。
    allow_restart=True 时两条无感路都失败才退到重启（给无人值守的自动守护用）。"""
    if dry:
        log("[dry-run] 无感切号到 %s（WorkDaddy API 优先，退自研 CDP 刷新）" % uid)
        return True, {"mode": "dry-run"}
    ok, info = wd_api_switch(cfg, uid)
    if ok:
        log("   无感切号成功（WorkDaddy API，reloaded=%s）" % info.get("reloaded"))
        return True, info
    log("   WorkDaddy API 切号没成（%s），改用自研 CDP 刷新" % str(info.get("error"))[:160])
    if not do_switch(cfg, uid):
        return False, {"mode": "switch.js", "error": "switch.js 换登录文件失败"}
    ok2, info2 = reload_wb_page(cfg, uid)
    if ok2:
        log("   无感切号成功（自研 CDP 刷新，%s）" % info2.get("nickname"))
        return True, info2
    if allow_restart:
        log("   两条无感路都没成（%s），退到重启 WorkBuddy" % str(info2.get("error"))[:120])
        if restart_wb(cfg):
            return True, {"mode": "restart-wb"}
    return False, info2


def restart_wb(cfg, dry=False):
    """杀掉 WorkBuddy 全部进程，再带 CDP 参数重启，并等 9226 就绪。
    仅作兜底：正常切号走 do_switch_headless，不需要重启。"""
    exe = cfg["wb_exe"]
    if not os.path.exists(exe):
        log("!! WorkBuddy.exe 不存在: %s" % exe)
        return False
    if dry:
        log("[dry-run] 将重启 WorkBuddy: %s --remote-debugging-port=%d" % (exe, cfg["cdp_port"]))
        return True
    subprocess.run(["taskkill", "/F", "/IM", "WorkBuddy.exe"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    time.sleep(3)
    # 确认真的没了
    for _ in range(10):
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WorkBuddy.exe", "/NH"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        if "WorkBuddy.exe" not in (r.stdout or ""):
            break
        time.sleep(1)
    try:
        DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([exe, "--remote-debugging-port=%d" % cfg["cdp_port"]],
                         close_fds=True, creationflags=DETACHED,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log("!! 启动 WorkBuddy 失败: %s" % e)
        return False
    log("   WorkBuddy 已重启，等 CDP %d ..." % cfg["cdp_port"])
    return wait_cdp(cfg)


def wait_cdp(cfg):
    url = "http://127.0.0.1:%d/json/version" % cfg["cdp_port"]
    deadline = time.time() + cfg["cdp_wait"]
    while time.time() < deadline:
        try:
            http_json(url, timeout=3)
            log("   CDP %d 就绪" % cfg["cdp_port"])
            return True
        except Exception:
            time.sleep(1)
    log("!! 等 CDP 超时（%ss）" % cfg["cdp_wait"])
    return False


def rate_limited_until(cfg, uid):
    """查 WorkDaddy 的 model_rate_limit_records，返回该号 kimi 系被锁到的 unix 秒（无则 0）。"""
    db = os.path.join(cfg["wd_dir"], "data", "credit-usage.db")
    if not os.path.exists(db):
        return 0
    try:
        import sqlite3
        con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
        row = con.execute(
            "select max(reset_at) from model_rate_limit_records "
            "where uid=? and (model_id like '%kimi%' or model_name like '%kimi%')", (uid,)).fetchone()
        con.close()
        v = int(row[0] or 0) if row else 0
        return v // 1000 if v > 10 ** 11 else v    # 毫秒 → 秒
    except Exception:
        return 0


def verify(cfg, rounds=3, gap=8):
    """切号后验证 pro 可用性。
    注意：WB 重启后反代的 CDP 长连接要先坏一次才会重连，所以第一发探测失败很正常，
    只有确认是 6004 才判该号已死，其余（relay/CDP 抖动）一律重试。"""
    info = {}
    for i in range(rounds):
        ok, info = probe(cfg)
        if ok:
            return True, info
        if info.get("limit"):
            log("   -> 确认 6004（额度墙），该号作废")
            return False, info
        if i < rounds - 1:
            brief = info.get("body") or info.get("error") or ("http=%s" % info.get("http"))
            log("   -> 探测异常（%s），%ds 后重试 %d/%d" % (str(brief)[:120], gap, i + 1, rounds))
            time.sleep(gap)
    return False, info


def try_one(cfg, uid, dry=False):
    """切到指定 uid 并验证 pro 是否可用。返回 (ok, info)
    无感优先：换登录态 + 刷新页面即可，不重启 WorkBuddy；只有两条无感路都废了才退重启。"""
    log("→ 尝试账号 %s" % uid)
    until = rate_limited_until(cfg, uid)
    if until and until > time.time():
        log("   跳过：本地限流记录显示锁到 %s" %
            time.strftime("%m-%d %H:%M:%S", time.localtime(until)))
        return False, {"skip": "rate_limited", "until": until}
    ok, info = do_switch_headless(cfg, uid, dry=dry, allow_restart=True)
    if not ok:
        return False, info
    if dry:
        return True, {"dry": True}
    time.sleep(2)          # 页面刚刷新，给渲染进程和反代重连留点时间
    ok, info = verify(cfg)
    log("   探测 %s: %s %s" % (cfg["probe_model"], "可用" if ok else "不可用",
                             str(info)[:200]))
    return ok, info


def switch_next(cfg, st, reason="6004", dry=False):
    """按队列找下一个能用的号。当前 uid 会被跳过，已失败过的号本轮不重复。"""
    queue = list(cfg["queue"])
    cur = st.get("current_uid") or ""
    tried = set(st.get("tried_this_round") or [])
    if not st.get("round_started_at") or time.time() - st.get("round_started_at", 0) > 6 * 3600:
        tried = set()
        st["round_started_at"] = time.time()

    for uid in queue:
        if uid == cur:
            continue
        if uid in tried:
            continue
        ok, info = try_one(cfg, uid, dry)
        tried.add(uid)
        if ok:
            st["current_uid"] = uid
            st["tried_this_round"] = []
            st["last_switch_at"] = time.time()
            st["history"] = (st.get("history") or [])[-30:] + [{
                "at": int(time.time()), "to": uid, "reason": reason, "ok": True}]
            save_state(st)
            if dry:
                log("✔ [dry-run] 流程可走通，目标账号 %s（未真实切换/验证）" % uid)
            else:
                log("✔ 已切到 %s，pro 验证通过" % uid)
            return True
        st["tried_this_round"] = sorted(tried)
        save_state(st)
        log("✘ %s 不可用（%s），继续下一个" % (uid, info.get("skip") or info.get("fail") or "probe_failed"))

    st["tried_this_round"] = []
    st["backoff_until"] = time.time() + cfg["backoff"]
    st["history"] = (st.get("history") or [])[-30:] + [{
        "at": int(time.time()), "to": None, "reason": reason, "ok": False, "note": "整队全爆"}]
    save_state(st)
    log("!! 队列里所有账号都不可用，退避 %d 分钟" % (cfg["backoff"] // 60))
    return False


def sync_current_uid(cfg, st):
    """从反代 /relay_state 或 auth 文件同步当前账号，避免切号目标算错。"""
    rs = relay_state(cfg)
    if rs and rs.get("uid"):
        if st.get("current_uid") != rs["uid"]:
            log("同步当前账号: %s" % rs["uid"])
        st["current_uid"] = rs["uid"]
        return st
    return st


# ---------------------------------------------------------------- 主循环
def do_status(cfg, st):
    rs = relay_state(cfg)
    hl = relay_health(cfg)
    print("=" * 60)
    print("反代 /health  :", "OK" if hl else "不可达")
    if hl:
        print("   cdp        :", hl.get("cdp"))
        print("   默认模型    :", hl.get("default_model"))
    print("反代 /state   :", "OK" if rs else "不可达（反代未更新到带状态的版本？）")
    if rs:
        print("   当前 uid    :", rs.get("uid"))
        print("   最近成功    :", time.strftime("%m-%d %H:%M:%S", time.localtime(rs["last_ok_at"]))
              if rs.get("last_ok_at") else "-")
        print("   pro 6004 次数:", rs.get("pro_6004_count"),
              "最近:", time.strftime("%m-%d %H:%M:%S", time.localtime(rs["last_pro_6004_at"]))
              if rs.get("last_pro_6004_at") else "-")
        le = rs.get("last_error")
        if le:
            print("   最近错误    :", le.get("model"), "status=", le.get("status"),
                  "6004=", le.get("code6004"))
            print("                 ", (le.get("body") or le.get("detail") or "")[:160])
    print("守护状态      :", STATE_FILE)
    print("   当前记录账号:", st.get("current_uid") or "-")
    print("   上次切号    :", time.strftime("%m-%d %H:%M:%S", time.localtime(st["last_switch_at"]))
          if st.get("last_switch_at") else "-")
    print("   退避到      :", time.strftime("%m-%d %H:%M:%S", time.localtime(st["backoff_until"]))
          if st.get("backoff_until") else "-")
    print("=" * 60)


def main():
    cfg = load_cfg()
    args = sys.argv[1:]
    st = load_state()
    st = sync_current_uid(cfg, st)
    save_state(st)

    if "--status" in args:
        do_status(cfg, st)
        return 0

    dry = "--dry-run" in args

    if "--next" in args:
        log("手动切下一个账号（reason=manual）")
        ok = switch_next(cfg, st, reason="manual", dry=dry)
        return 0 if ok else 1

    if already_running():
        log("已有守护进程在跑（%s），退出" % LOCK_FILE)
        return 0
    take_lock()
    log("======== 自动切号守护启动 pid=%d ========" % os.getpid())
    log("队列 %d 个号，轮询 %ss，冷却 %ss" % (len(cfg["queue"]),
                                        cfg["poll_interval"], cfg["cooldown"]))

    handled = st.get("handled_6004_at") or 0
    try:
        while True:
            if "--once" in args:
                pass

            # 1) 反代在不在
            rs = relay_state(cfg)
            if rs is None:
                hl = relay_health(cfg)
                if hl is None:
                    log("反代 18793 不可达，尝试拉起 run_relay.bat")
                    bat = os.path.join(BASE, "run_relay.bat")
                    if os.path.exists(bat) and not dry:
                        try:
                            subprocess.Popen(["cmd", "/c", bat], cwd=BASE,
                                             creationflags=0x00000008,
                                             stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        except Exception as e:
                            log("!! 拉起反代失败: %s" % e)
                    time.sleep(15)
                    if "--once" in args:
                        break
                    continue

            # 2) CDP 掉了 → 重启 WB（同样的冷却约束）
            if (hl := relay_health(cfg)) and str(hl.get("cdp", "")).startswith("error"):
                if time.time() - (st.get("last_wb_fix_at") or 0) > cfg["cooldown"]:
                    log("CDP 异常(%s)，重启 WorkBuddy" % str(hl.get("cdp"))[:80])
                    st["last_wb_fix_at"] = time.time()
                    save_state(st)
                    restart_wb(cfg, dry)
                if "--once" in args:
                    break
                time.sleep(cfg["poll_interval"])
                continue

            # 3) 6004 检测
            if rs:
                st["current_uid"] = rs.get("uid") or st.get("current_uid")
                hit_at = rs.get("last_pro_6004_at") or 0
                cool_ok = time.time() - (st.get("last_switch_at") or 0) > cfg["cooldown"]
                backoff_ok = time.time() > (st.get("backoff_until") or 0)
                if hit_at and hit_at > handled and cool_ok:
                    if not backoff_ok:
                        left = int(st["backoff_until"] - time.time())
                        log("检测到 pro 6004，但还在退避期（剩 %ds），暂不切号" % left)
                    else:
                        log("检测到 pro 6004（%s），开始自动换号" %
                            time.strftime("%m-%d %H:%M:%S", time.localtime(hit_at)))
                        switch_next(cfg, st, reason="6004", dry=dry)
                        if "--once" in args:
                            handled = hit_at
                            st["handled_6004_at"] = handled
                            save_state(st)
                            break
                    handled = hit_at
                    st["handled_6004_at"] = handled
                    save_state(st)

            if "--once" in args:
                break
            time.sleep(cfg["poll_interval"])
    except KeyboardInterrupt:
        log("收到中断，守护退出")
    finally:
        drop_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
