#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
workmom core  —— WorkBuddy 账号/模型控制核心（纯自研，不依赖 WorkDaddy）

能力：
  1) 无感登录 WorkBuddy 账号（官方 OAuth state 轮询，全程不退出 WorkBuddy）
  2) 无感切换账号（自研写登录文件 + CDP 刷新，不重启 WorkBuddy）
  3) 自动领积分（每日签到，多域名兜底、幂等去重）
  4) 凭证保活（refresh_token 换新 accessToken，密文原位回写）
  5) 模型状态（当前模型独立持久化，与账号完全解耦）

加密：WorkBuddy 5.6+ 的 workbuddy-desktop.info 里凭据字段是 $wbEncrypted 信封，
      内层 AES-256-GCM（sym-v1 帧 + 自定义 AAD），密钥 = SHA256(atRestSecretKey)，
      atRestSecretKey 只能通过 WorkBuddy 自身的 electron binding 取（ELECTRON_RUN_AS_NODE 探针）。
"""

import base64
import hashlib
import json
import os
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

# ------------------------------------------------------------------ 路径与常量
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
ACCOUNTS_DIR = os.path.join(DATA, "accounts")
LOGS_DIR = os.path.join(DATA, "logs")
META_FILE = os.path.join(DATA, "meta.json")
CHECKIN_FILE = os.path.join(DATA, "checkin.json")
MODEL_FILE = os.path.join(DATA, "current_model.json")
CONFIG_FILE = os.path.join(BASE, "config.json")
LOG_FILE = os.path.join(LOGS_DIR, "workmom.log")
KEY_PROBE_JS = os.path.join(DATA, "_keyprobe.js")

APP_NAME = "workmom"


_WB_EXE_CACHE = {"v": None}
_WB_IMAGE = "WorkBuddy.exe"


def _wb_exe_from_registry():
    """从注册表找 WorkBuddy：App Paths → 卸载信息 DisplayIcon / InstallLocation。

    WorkBuddy 允许自定义安装目录（D:\\xxx 这类），靠枚举常见路径必然漏，
    但装过它的机器在卸载信息里一定留着 DisplayIcon 完整路径且与盘符、目录名无关，
    所以把它作为首选探测手段。只读，无副作用。
    """
    try:
        import winreg
    except Exception:
        return ""

    def _as_exe(raw):
        p = str(raw or "").strip().strip('"')
        if "," in p:                     # DisplayIcon 形如 "D:\\x\\WorkBuddy.exe,0"
            p = p.split(",")[0]
        p = p.strip().strip('"')
        return p if p and os.path.basename(p).lower() == _WB_IMAGE.lower() and os.path.isfile(p) else ""

    roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY, 0)

    # 1) App Paths
    for root in roots:
        for view in views:
            try:
                k = winreg.OpenKey(root,
                                   r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\\" + _WB_IMAGE,
                                   0, winreg.KEY_READ | view)
                p = _as_exe(winreg.QueryValueEx(k, "")[0])
                if p:
                    return p
            except Exception:
                pass

    # 2) 卸载信息：DisplayName 含 workbuddy 的条目
    for root in roots:
        for view in views:
            try:
                base = winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                                      0, winreg.KEY_READ | view)
            except Exception:
                continue
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(base, i)
                except OSError:
                    break
                i += 1
                try:
                    sk = winreg.OpenKey(base, sub)
                    name = str(winreg.QueryValueEx(sk, "DisplayName")[0] or "")
                except Exception:
                    continue
                if "workbuddy" not in name.lower():
                    continue
                for field in ("DisplayIcon", "InstallLocation"):
                    try:
                        val = str(winreg.QueryValueEx(sk, field)[0] or "")
                    except Exception:
                        continue
                    p = _as_exe(val)
                    if p:
                        return p
                    if val.strip():
                        cand = os.path.join(val.strip().strip('"'), _WB_IMAGE)
                        if os.path.isfile(cand):
                            return cand
    return ""


def _wb_exe_from_common_paths():
    """常见安装目录兜底枚举（覆盖安装器默认位置 + 各盘根下常见目录名）。"""
    la = os.environ.get("LOCALAPPDATA") or ""
    ad = os.environ.get("APPDATA") or ""
    pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
    pf86 = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    pd = os.environ.get("ProgramData") or r"C:\ProgramData"

    direct = [os.path.join(la, "Programs", "WorkBuddy", _WB_IMAGE) if la else "",
              os.path.join(la, "WorkBuddy", _WB_IMAGE),
              os.path.join(ad, "WorkBuddy", _WB_IMAGE),
              os.path.join(pf, "WorkBuddy", _WB_IMAGE),
              os.path.join(pf86, "WorkBuddy", _WB_IMAGE),
              os.path.join(pd, "WorkBuddy", _WB_IMAGE)]
    for c in direct:
        if c and os.path.isfile(c):
            return c

    # 各盘根目录下的常见目录名（自定义安装常落在这里，如 D:\\Nworkbuddy）
    dirnames = ("WorkBuddy", "workbuddy", "Nworkbuddy", "WorkBuddyApp", "工作助手")
    for base in ("C:\\", "D:\\", "E:\\", "F:\\"):
        if not os.path.isdir(base):
            continue
        for n in dirnames:
            p = os.path.join(base, n, _WB_IMAGE)
            if os.path.isfile(p):
                return p
    return ""


def _wb_exe_from_process():
    """从正在运行的 WorkBuddy 进程取 exe 路径。最准，但要拉起 PowerShell，放最后兜底。"""
    if os.name != "nt":
        return ""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-Process -Name WorkBuddy -ErrorAction SilentlyContinue | "
             "Where-Object { $_.Path } | Select-Object -First 1).Path"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, creationflags=0x08000000)
        p = (r.stdout or "").strip().splitlines()
        p = p[-1].strip() if p else ""
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    return ""


def _guess_wb_exe():
    """探测 WorkBuddy 主程序路径；探测不到返回空串。

    顺序：注册表（对自定义安装盘最有效）→ 常见目录 → 运行中进程（慢，兜底）。
    结果缓存，避免 server / dashboard / guard 反复探测。
    注意：config.json 里显式写了 wb_exe 的永远优先，本函数只是没配时的兜底。
    """
    if _WB_EXE_CACHE["v"] is not None:
        return _WB_EXE_CACHE["v"]
    found = ""
    for fn in (_wb_exe_from_registry, _wb_exe_from_common_paths, _wb_exe_from_process):
        try:
            found = fn()
        except Exception:
            found = ""
        if found:
            break
    _WB_EXE_CACHE["v"] = found
    return found


DEF = {
    # 留空表示由 _guess_wb_exe() 自动探测（比写死路径可移植）；
    # config.json 里显式写了就以此为准，setup.bat 会帮你写进去
    "wb_exe": "",
    "cdp_port": 9226,
    "api_host": "https://www.workbuddy.cn",   # 国内版；国际版填 https://www.workbuddy.ai
    "oauth_platform": "workbuddy",
    "relay_port": 18793,
    "wd_dir": "",   # WorkDaddy 目录（可选退路），留空表示不用
    "default_model": "kimi-k3.1-pro",
    "known_models": [
        "kimi-k3.1-pro", "kimi-k3-2", "kimi-k3-1",
        "deepseek-v4.1-flash", "deepseek-v4-pro",
        "glm-5.3", "glm-5.3-flash", "step-5-preview",
        "hy4-free", "hy4-preview", "hy3", "wb-lite",
    ],
    "checkin_concurrency": 3,
}

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_LOG_LOCK = threading.Lock()
KEY_CACHE = {"key": None, "fail_at": 0.0, "reason": ""}
KEY_RETRY_SECONDS = 60


def cfg():
    c = dict(DEF)
    if os.path.exists(CONFIG_FILE):
        try:
            # utf-8-sig：Windows 记事本 / PowerShell 写出来的 config.json 常带 BOM，
            # 用 utf-8 硬读会解析失败并静默退回默认值，导致改了配置却不生效
            with open(CONFIG_FILE, encoding="utf-8-sig") as f:
                c.update(json.load(f) or {})
        except Exception:
            pass
    # wb_exe 没配（或配了空串）时自动探测，保证 clone 下来不改配置也能跑
    if not c.get("wb_exe"):
        c["wb_exe"] = _guess_wb_exe()
    return c


def auth_file():
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    return os.path.join(local, "CodeBuddyExtension", "Data", "Public", "auth",
                        "workbuddy-desktop.info")


def ensure_dirs():
    for d in (DATA, ACCOUNTS_DIR, LOGS_DIR):
        os.makedirs(d, exist_ok=True)


def log(msg):
    ensure_dirs()
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with _LOG_LOCK:
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 2 * 1024 * 1024:
                with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
                    tail = f.readlines()[-2000:]
                with open(LOG_FILE, "w", encoding="utf-8") as f:
                    f.writelines(tail)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


# ------------------------------------------------------------------ 密钥探针
_PROBE_SRC = r"""try {
  var binding = process._linkedBinding('electron_browser_workbuddy_storage');
  var payload = JSON.parse(binding.loggerGet());
  if (payload && payload.version === 1 && typeof payload.atRestSecretKey === 'string') {
    var crypto = require('crypto');
    var key = crypto.createHash('sha256').update(payload.atRestSecretKey, 'utf8').digest();
    process.stdout.write('WORKMOM_KEY ' + key.toString('base64') + '\n');
    process.exit(0);
  }
  process.stderr.write('payload schema mismatch');
  process.exit(4);
} catch (e) {
  process.stderr.write(String((e && e.message) || e).slice(0, 200));
  process.exit(3);
}
"""


def secret_key(force=False):
    """取 AES-256 主密钥（32 字节）。失败缓存 60 秒，避免反复拉进程。"""
    if KEY_CACHE["key"] and not force:
        return KEY_CACHE["key"]
    if not force and KEY_CACHE["fail_at"] and time.time() - KEY_CACHE["fail_at"] < KEY_RETRY_SECONDS:
        raise RuntimeError(KEY_CACHE["reason"] or "取密钥暂不可用")
    exe = cfg()["wb_exe"]
    if not os.path.exists(exe):
        KEY_CACHE["fail_at"] = time.time()
        KEY_CACHE["reason"] = "找不到 WorkBuddy.exe：%s" % exe
        raise RuntimeError(KEY_CACHE["reason"])
    ensure_dirs()
    with open(KEY_PROBE_JS, "w", encoding="utf-8") as f:
        f.write(_PROBE_SRC)
    env = dict(os.environ)
    env["ELECTRON_RUN_AS_NODE"] = "1"
    try:
        # 探针进程的 stderr 可能混入非 UTF-8 字节，一律按 bytes 取回再自己解码，
        # 避免 subprocess 在 reader 线程里抛 UnicodeDecodeError
        r = subprocess.run([exe, KEY_PROBE_JS], env=env, capture_output=True,
                           text=False, timeout=25)
        r.stdout = (r.stdout or b"").decode("utf-8", "replace")
        r.stderr = (r.stderr or b"").decode("utf-8", "replace")
    except Exception as e:
        KEY_CACHE["fail_at"] = time.time()
        KEY_CACHE["reason"] = "取密钥进程异常: %s" % e
        raise RuntimeError(KEY_CACHE["reason"])
    out = r.stdout or ""
    for line in out.splitlines():
        if line.startswith("WORKMOM_KEY "):
            key = base64.b64decode(line.split(" ", 1)[1].strip())
            if len(key) != 32:
                break
            KEY_CACHE["key"] = key
            KEY_CACHE["fail_at"] = 0.0
            return key
    KEY_CACHE["fail_at"] = time.time()
    KEY_CACHE["reason"] = "取密钥失败: %s %s" % ((r.stderr or "").strip()[:150], out.strip()[:80])
    raise RuntimeError(KEY_CACHE["reason"])


# ------------------------------------------------------------------ 信封加解密
def _b64(b):
    return base64.b64encode(b).decode("ascii")


def _lp(s):
    b = (s or "").encode("utf-8")
    return struct.pack(">I", len(b)) + b


def _u32(n):
    return struct.pack(">I", int(n))


def envelope_aad(env):
    return (b"WB-AAD\x00" + b"\x01" + _lp("WBEV1") + _lp("sym-v1")
            + _u32(env.get("suite") or 1) + _lp(env.get("keyId") or "")
            + b"\x02" + b"\x00" + b"\x00")


def _parse_env(node):
    raw = base64.b64decode(node["envelope"])
    return json.loads(raw.decode("utf-8"))


def open_envelope(env, key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = base64.b64decode(env["nonce"])
    ct = base64.b64decode(env["ciphertext"])
    tag = base64.b64decode(env["authTag"])
    return AESGCM(key).decrypt(nonce, ct + tag, envelope_aad(env)).decode("utf-8")


def seal(value, env, key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    blob = AESGCM(key).encrypt(nonce, value.encode("utf-8"), envelope_aad(env))
    ct, tag = blob[:-16], blob[-16:]
    inner = {
        "suite": env.get("suite") or 1,
        "keyId": env.get("keyId") or "",
        "nonce": _b64(nonce),
        "authTag": _b64(tag),
        "ciphertext": _b64(ct),
    }
    return {"$wbEncrypted": 1, "envelope": _b64(json.dumps(inner, ensure_ascii=False).encode("utf-8"))}


def _is_env(node):
    return isinstance(node, dict) and node.get("$wbEncrypted") == 1 and isinstance(node.get("envelope"), str)


def decrypt_tree(node, key=None, stats=None):
    """原地语义解密：把所有 $wbEncrypted 信封替换成明文值；失败保留原值。"""
    if stats is None:
        stats = {"ok": 0, "fail": 0}
    if isinstance(node, list):
        return [decrypt_tree(x, key, stats) for x in node]
    if isinstance(node, dict):
        if _is_env(node):
            try:
                k = key or secret_key()
                stats["ok"] += 1
                return open_envelope(_parse_env(node), k)
            except Exception as e:
                stats["fail"] += 1
                log("解密失败: %s" % str(e)[:120])
                return node
        return {k2: decrypt_tree(v, key, stats) for k2, v in node.items()}
    return node


CRED_KEYS = {"accessToken", "access_token", "refreshToken", "refresh_token",
             "idToken", "id_token", "token"}


def encrypt_tree(value, template, key=None):
    """按模板的字段信封结构重新加密（凭据字段缺模板时用首个信封兜底）。"""
    if not _contains_env(template):
        raise RuntimeError("没有可复用的加密模板，拒绝降级明文写入")
    k = key or secret_key()
    fallback = {"env": None}

    def find_env(node):
        if _is_env(node):
            env = _parse_env(node)
            try:
                open_envelope(env, k)          # 先验证密钥与信封匹配
            except Exception as e:
                raise RuntimeError("模板信封校验失败: %s" % str(e)[:120])
            if fallback["env"] is None:
                fallback["env"] = env
        elif isinstance(node, (dict, list)):
            for v in (node.values() if isinstance(node, dict) else node):
                find_env(v)

    find_env(template)

    def walk(val, orig, in_auth=False, field=""):
        if _is_env(orig):
            if _is_env(val) and val.get("envelope") == orig.get("envelope"):
                return dict(orig)
            if not isinstance(val, str):
                raise RuntimeError("加密字段类型非法: %s" % field)
            return seal(val, _parse_env(orig), k)
        if in_auth and field in CRED_KEYS and isinstance(val, str):
            return seal(val, fallback["env"], k)
        if isinstance(val, list):
            o = orig if isinstance(orig, list) else []
            return [walk(v, o[i] if i < len(o) else None, in_auth, "") for i, v in enumerate(val)]
        if isinstance(val, dict):
            o = orig if isinstance(orig, dict) else {}
            return {n: walk(v, o.get(n), in_auth or n == "auth", n) for n, v in val.items()}
        return val

    return walk(value, template)


def _contains_env(node):
    if _is_env(node):
        return True
    if isinstance(node, (dict, list)):
        return any(_contains_env(v) for v in (node.values() if isinstance(node, dict) else node))
    return False


# ------------------------------------------------------------------ 登录文件读写
_RAW_CACHE = {"path": None, "mtime": 0.0, "size": -1, "raw": None, "plain": None}


def read_raw(path=None):
    """读登录文件原始 JSON（密文原样）。"""
    p = path or auth_file()
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def read_plain(path=None, use_cache=True):
    """读并解密登录文件（明文 dict），带 mtime 缓存。"""
    p = path or auth_file()
    try:
        st = os.stat(p)
    except Exception:
        return None
    c = _RAW_CACHE
    if (use_cache and c["path"] == p and c["mtime"] == st.st_mtime and c["size"] == st.st_size
            and c["plain"] is not None):
        return c["plain"]
    raw = read_raw(p)
    plain = decrypt_tree(raw)
    _RAW_CACHE.update({"path": p, "mtime": st.st_mtime, "size": st.st_size, "raw": raw, "plain": plain})
    return plain


def _atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
    os.replace(tmp, path)


def current_uid():
    """当前登录账号 uid（account.uid 是明文字段，不需要解密）。"""
    try:
        obj = read_raw()
    except Exception:
        return ""
    return str(((obj or {}).get("account") or {}).get("uid") or "")


def current_account():
    plain = read_plain() or {}
    acc = dict(plain.get("account") or {})
    auth = dict(plain.get("auth") or {})
    return {
        "uid": str(acc.get("uid") or ""),
        "nickname": str(acc.get("nickname") or ""),
        "phone": str(acc.get("phoneNumber") or ""),
        "uin": str(acc.get("uin") or ""),
        "expiresAt": auth.get("expiresAt") or auth.get("expires_at") or 0,
        "domain": str(auth.get("domain") or ""),
    }


# ------------------------------------------------------------------ 账号备份库
def backup_path(uid):
    return os.path.join(ACCOUNTS_DIR, "%s.info" % uid)


def load_meta():
    try:
        with open(META_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_meta(meta):
    ensure_dirs()
    _atomic_write(META_FILE, json.dumps(meta, ensure_ascii=False, indent=2))


def update_meta(uid, **kw):
    meta = load_meta()
    item = dict(meta.get(uid) or {})
    item.update({k: v for k, v in kw.items() if v is not None})
    item["updated_at"] = int(time.time())
    meta[uid] = item
    save_meta(meta)
    return item


def backup_current():
    """把当前登录文件原样复制到 accounts/<uid>.info（密文原字节，切回即用）。"""
    uid = current_uid()
    if not uid:
        return ""
    src = auth_file()
    try:
        with open(src, "rb") as f:
            raw = f.read()
    except Exception as e:
        log("备份当前账号失败（读不到登录文件）: %s" % e)
        return ""
    dst = backup_path(uid)
    old = b""
    if os.path.exists(dst):
        try:
            with open(dst, "rb") as f:
                old = f.read()
        except Exception:
            old = b""
    if old != raw:
        _atomic_write(dst, raw)
        log("已备份账号 %s" % uid[:8])
    info = current_account()
    update_meta(uid, nickname=info.get("nickname"), phone=info.get("phone"),
                uin=info.get("uin"), domain=info.get("domain"))
    return uid


def import_external_backups():
    """首次运行把 WorkDaddy 的历史备份导入本应用（只补缺，不覆盖本地）。"""
    ensure_dirs()
    wd = cfg().get("wd_dir") or ""
    src = os.path.join(wd, "data", "accounts") if wd else ""
    if not src or not os.path.isdir(src):
        return 0
    n = 0
    for name in os.listdir(src):
        if not name.endswith(".info"):
            continue
        dst = os.path.join(ACCOUNTS_DIR, name)
        if os.path.exists(dst):
            continue
        try:
            with open(os.path.join(src, name), "rb") as f:
                data = f.read()
            _atomic_write(dst, data)
            n += 1
        except Exception:
            continue
    if n:
        log("从旧目录导入 %d 个账号备份" % n)
    return n


def list_backups():
    ensure_dirs()
    out = []
    for name in sorted(os.listdir(ACCOUNTS_DIR)):
        if name.endswith(".info"):
            out.append(name[:-5])
    return out


def account_summary(uid):
    """从备份解密出账号展示信息（解密失败则退回 meta 缓存）。"""
    info = {"uid": uid, "nickname": "", "phone": "", "uin": "", "expiresAt": 0}
    meta = load_meta().get(uid) or {}
    info.update({k: meta.get(k) or "" for k in ("nickname", "phone", "uin")})
    p = backup_path(uid)
    try:
        plain = decrypt_tree(read_raw(p))
        acc = dict(plain.get("account") or {})
        auth = dict(plain.get("auth") or {})
        if acc.get("nickname") or acc.get("uid"):
            info["nickname"] = str(acc.get("nickname") or info["nickname"] or "")
            info["phone"] = str(acc.get("phoneNumber") or info["phone"] or "")
            info["uin"] = str(acc.get("uin") or info["uin"] or "")
        info["expiresAt"] = auth.get("expiresAt") or auth.get("expires_at") or 0
        info["domain"] = str(auth.get("domain") or "")
        info["hasToken"] = bool(auth.get("accessToken") or auth.get("access_token"))
    except Exception as e:
        info["error"] = str(e)[:120]
    return info


def list_accounts():
    cur = current_uid()
    rows = []
    for uid in list_backups():
        s = account_summary(uid)
        s["is_current"] = (uid == cur)
        rows.append(s)
    rows.sort(key=lambda r: (not r["is_current"], (r.get("nickname") or r["uid"])[:1]))
    return rows


# ------------------------------------------------------------------ 无感切换
def write_login_from_backup(uid):
    """把备份文件原字节写回登录文件（真正的换号动作）。"""
    src = backup_path(uid)
    if not os.path.exists(src):
        raise RuntimeError("账号备份不存在: %s" % uid)
    with open(src, "rb") as f:
        raw = f.read()
    obj = json.loads(raw.decode("utf-8"))
    got = str(((obj or {}).get("account") or {}).get("uid") or "")
    if got and got != uid:
        raise RuntimeError("备份文件 uid 不匹配（%s != %s）" % (got[:8], uid[:8]))
    cur = auth_file()
    try:
        if os.path.exists(cur):
            _atomic_write(cur + ".prev", open(cur, "rb").read())
    except Exception:
        pass
    _atomic_write(cur, raw)
    _RAW_CACHE.update({"path": None, "plain": None, "mtime": 0.0, "size": -1})
    return True


# ---- CDP 刷新（自研，不依赖任何外部工具）---------------------------------
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


def _page_ws(timeout=8):
    c = cfg()
    url = "http://127.0.0.1:%d/json" % int(c["cdp_port"])
    with _OPENER.open(url, timeout=timeout) as r:
        targets = json.loads(r.read().decode("utf-8", "replace"))
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        raise RuntimeError("CDP 里没有可用页面（WorkBuddy 启动时带 --remote-debugging-port=%d 吗？）"
                           % c["cdp_port"])
    pages.sort(key=lambda t: 1 if "index.html" in (t.get("url") or "") else 0, reverse=True)
    return pages[0]["webSocketDebuggerUrl"]


class _Cdp(object):
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
            return (res.get("result", {}) or {}).get("value")
        raise RuntimeError("CDP %s 超时" % method)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def wb_profile(timeout=12):
    c = _Cdp(_page_ws())
    try:
        return c.call("Runtime.evaluate",
                      {"expression": PROFILE_JS, "awaitPromise": True, "returnByValue": True},
                      timeout) or {}
    finally:
        c.close()


def reload_wb_page(uid="", wait=45):
    """清 URL accountSnapshot → Page.reload → 等 profile 变目标号。全程不重启 WB。"""
    c = _Cdp(_page_ws())
    try:
        c.call("Runtime.evaluate", {"expression": CLEAR_SNAPSHOT_JS, "returnByValue": True}, 10)
        c.call("Page.reload", {"ignoreCache": False}, 15)
    finally:
        c.close()
    time.sleep(3)
    deadline = time.time() + wait
    err = "刷新后没读到目标账号 profile"
    while time.time() < deadline:
        try:
            p = wb_profile()
            if p.get("uid"):
                if not uid or p["uid"] == uid:
                    return True, {"uid": p["uid"], "nickname": p.get("nickname", ""),
                                  "isPro": p.get("isPro")}
                err = "刷新后仍是 %s" % str(p["uid"])[:8]
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, str(e)[:120])
        time.sleep(2)
    return False, {"error": err}


def wd_api_switch(uid, timeout=180):
    """可选退路：WorkDaddy daemon 在跑时借它的 /api/switch（它内部也带 CDP 刷新）。"""
    wd = cfg().get("wd_dir") or ""
    tok = ""
    appdata = os.environ.get("APPDATA") or ""
    local = os.environ.get("LOCALAPPDATA") or ""
    for p in (os.path.join(appdata, "WorkDaddy", ".api-token"),
              os.path.join(local, "WorkDaddy", ".api-token")):
        try:
            with open(p, encoding="utf-8") as f:
                t = (f.read() or "").strip()
            if len(t) == 64:
                tok = t
                break
        except Exception:
            continue
    if not tok:
        return False, {"error": "WorkDaddy daemon 不可用（无 token）"}
    for port in (47832, 47833, 47834, 47835):
        base = "http://127.0.0.1:%d" % port
        try:
            req = urllib.request.Request(base + "/api/accounts",
                                         headers={"X-WorkDaddy-Token": tok})
            with _OPENER.open(req, timeout=3) as r:
                if not json.loads(r.read().decode("utf-8", "replace")).get("ok"):
                    raise RuntimeError("not ok")
        except Exception:
            continue
        try:
            body = json.dumps({"uid": uid, "reload": True}).encode("utf-8")
            req = urllib.request.Request(base + "/api/switch", data=body, method="POST",
                                         headers={"X-WorkDaddy-Token": tok,
                                                  "Content-Type": "application/json"})
            with _OPENER.open(req, timeout=timeout) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            if d.get("ok") and str(d.get("uid") or "") == uid:
                return True, {"mode": "wd-api", "nickname": d.get("nickname"), "reloaded": d.get("reloaded")}
            return False, {"error": str(d.get("error") or d.get("hint") or "切换未成功")[:160]}
        except Exception as e:
            return False, {"error": "%s: %s" % (type(e).__name__, str(e)[:120])}
    return False, {"error": "WorkDaddy daemon 未监听"}


def seamless_switch(uid, reload=True, allow_restart=False):
    """无感切号总入口：自研写文件 + 自研 CDP 刷新；daemon 退路；不默认重启。"""
    uid = str(uid or "").strip()
    if not uid:
        return False, {"error": "缺少 uid"}
    if uid == current_uid():
        return True, {"uid": uid, "skipped": True, "nickname": current_account().get("nickname", "")}
    t0 = time.time()
    try:
        backup_current()
        write_login_from_backup(uid)
    except Exception as e:
        log("写登录文件失败: %s" % e)
        return False, {"error": "写登录文件失败: %s" % str(e)[:160]}
    if not reload:
        return True, {"uid": uid, "reloaded": False, "secs": round(time.time() - t0, 1)}
    ok, info = reload_wb_page(uid)
    if ok:
        log("无感切号完成 → %s (%s) 用时 %.1fs" % (info.get("nickname"), uid[:8], time.time() - t0))
        update_meta(uid, nickname=info.get("nickname"))
        return True, dict(info, reloaded=True, secs=round(time.time() - t0, 1))
    log("自研 CDP 刷新未成（%s），试 WorkDaddy daemon 退路" % str(info.get("error"))[:120])
    ok2, info2 = wd_api_switch(uid)
    if ok2:
        return True, dict(info2, fallback="wd-api", secs=round(time.time() - t0, 1))
    if allow_restart:
        log("两条无感路都失败，兜底重启 WorkBuddy")
        if restart_wb():
            return True, {"uid": uid, "mode": "restart-wb", "secs": round(time.time() - t0, 1)}
    return False, {"error": info.get("error") or info2.get("error") or "切换失败", "fallback_tried": True}


def restart_wb():
    """兜底：重启 WorkBuddy（带 CDP 端口）。正常切号不需要。"""
    c = cfg()
    exe = c["wb_exe"]
    if not os.path.exists(exe):
        return False
    subprocess.run(["taskkill", "/F", "/IM", "WorkBuddy.exe"], capture_output=True, timeout=30)
    time.sleep(3)
    DETACHED = 0x00000008 | 0x00000200
    try:
        subprocess.Popen([exe, "--remote-debugging-port=%d" % int(c["cdp_port"])],
                         close_fds=True, creationflags=DETACHED,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log("重启 WorkBuddy 失败: %s" % e)
        return False
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            with _OPENER.open("http://127.0.0.1:%d/json/version" % int(c["cdp_port"]), timeout=3) as r:
                r.read()
            log("WorkBuddy 已重启，CDP 就绪")
            return True
        except Exception:
            time.sleep(1)
    return False


# ------------------------------------------------------------------ 凭证保活
def _norm_ts(v):
    try:
        n = float(v)
    except Exception:
        return None
    if n <= 0:
        return None
    return int(n * 1000) if n < 1e11 else int(n)


def _tv(d, *names):
    for n in names:
        v = (d or {}).get(n)
        if v not in (None, ""):
            return str(v)
    return ""


def refresh_token_of(auth):
    """用 refreshToken 换新凭证，返回 (ok, new_auth_dict, err)。"""
    c = cfg()
    host = str(c["api_host"]).rstrip("/")
    rt = _tv(auth, "refreshToken", "refresh_token")
    if not rt:
        return False, auth, "缺少 refresh token"
    at = _tv(auth, "accessToken", "access_token")
    headers = {"accept": "application/json", "content-type": "application/json",
               "X-Refresh-Token": rt}
    if at:
        headers["authorization"] = "Bearer " + at
    req = urllib.request.Request(host + "/v2/plugin/auth/token/refresh",
                                 data=b"{}", method="POST", headers=headers)
    try:
        with _OPENER.open(req, timeout=30) as r:
            payload = json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        return False, auth, "HTTP %s %s" % (e.code, txt[:160])
    except Exception as e:
        return False, auth, "%s: %s" % (type(e).__name__, str(e)[:160])
    code = payload.get("code")
    if not (code in (0, 200, None)):
        return False, auth, str(payload.get("msg") or payload.get("message") or "刷新失败")[:160]
    data = payload.get("data") or {}
    n_at = _tv(data, "accessToken", "access_token")
    if not n_at:
        return False, auth, "刷新响应缺 accessToken"
    now = int(time.time() * 1000)
    nxt = dict(auth)
    nxt["accessToken"] = n_at
    nxt["refreshToken"] = _tv(data, "refreshToken", "refresh_token") or rt
    nxt["lastRefreshTime"] = now
    exp = _norm_ts(data.get("expiresAt") or data.get("expires_at"))
    if exp is None:
        try:
            ei = float(data.get("expiresIn") or data.get("expires_in") or 0)
            exp = now + int(ei * 1000) if ei > 0 else _norm_ts(auth.get("expiresAt") or auth.get("expires_at"))
        except Exception:
            exp = None
    if exp:
        nxt["expiresAt"] = exp
        nxt["expiresIn"] = max(0, int((exp - now) / 1000))
    rexp = _norm_ts(data.get("refreshExpiresAt") or data.get("refresh_expires_at"))
    if rexp:
        nxt["refreshExpiresAt"] = rexp
        nxt["refreshExpiresIn"] = max(0, int((rexp - now) / 1000))
    for k in ("tokenType", "domain", "scope", "sessionState", "idToken"):
        if data.get(k) is not None:
            nxt[k] = data[k]
    return True, nxt, ""


def update_backup_auth(uid, new_auth):
    """把刷新后的 auth 密文回写进备份文件（用备份自身结构作模板）。"""
    p = backup_path(uid)
    raw = read_raw(p)
    plain = decrypt_tree(raw)
    plain["auth"] = new_auth
    sealed = encrypt_tree(plain, raw)
    _atomic_write(p, json.dumps(sealed, ensure_ascii=False, indent=2).encode("utf-8"))
    return True


def ensure_fresh_token(uid, force=False, min_ttl_h=20):
    """凭证保活：临期（默认 <20h）或 force 时刷新备份里的 accessToken。"""
    p = backup_path(uid)
    if not os.path.exists(p):
        return False, "", "备份不存在"
    try:
        plain = decrypt_tree(read_raw(p))
    except Exception as e:
        return False, "", "备份解密失败: %s" % str(e)[:120]
    auth = dict(plain.get("auth") or {})
    at = _tv(auth, "accessToken", "access_token")
    exp = _norm_ts(auth.get("expiresAt") or auth.get("expires_at")) or 0
    now = int(time.time() * 1000)
    fresh = exp and (exp - now) > min_ttl_h * 3600 * 1000
    if at and fresh and not force:
        return True, at, ""
    ok, nxt, err = refresh_token_of(auth)
    if not ok:
        return (bool(at) and not force), at, err
    try:
        update_backup_auth(uid, nxt)
    except Exception as e:
        return True, nxt.get("accessToken", at), "回写备份失败: %s" % str(e)[:100]
    return True, _tv(nxt, "accessToken", "access_token"), ""


# ------------------------------------------------------------------ 自动领积分
def load_checkin():
    try:
        with open(CHECKIN_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_checkin(d):
    ensure_dirs()
    _atomic_write(CHECKIN_FILE, json.dumps(d, ensure_ascii=False, indent=2))


def _jwt_iss_origin(token):
    try:
        part = str(token or "").split(".")[1]
        if not part:
            return None
        pad = "=" * ((4 - len(part) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part.replace("-", "+").replace("_", "/") + pad))
        iss = str(payload.get("iss") or "")
        if not iss:
            return None
        from urllib.parse import urlparse
        u = urlparse(iss)
        return "%s://%s" % (u.scheme, u.netloc)
    except Exception:
        return None


ISSUER_HOSTS = {"https://www.workbuddy.ai", "https://www.workbuddy.cn",
                "https://www.codebuddy.cn", "https://www.codebuddy.ai"}
CHECKIN_PATHS = ["/billing/meter/daily-checkin", "/v2/billing/meter/daily-checkin"]
INACTIVE_RE = ("未开始", "未开放", "已过期", "暂无活动", "活动结束", "活动关闭", "活动暂停")
ALREADY_RE = ("已签到", "已领取", "已经签到", "重复签到", "already")


def checkin_endpoints(token):
    c = cfg()
    host = _jwt_iss_origin(token)
    hosts = [host] if host in ISSUER_HOSTS else [str(c["api_host"]).rstrip("/")]
    if not (host in ISSUER_HOSTS):
        for h in ("https://www.codebuddy.cn", "https://www.workbuddy.cn"):
            if h not in hosts:
                hosts.append(h)
    return [h + p for h in hosts for p in CHECKIN_PATHS]


def classify_checkin(http_ok, code, message):
    text = str(message or "")
    inactive = any(w in text for w in INACTIVE_RE)
    try:
        cnum = int(code)
    except Exception:
        cnum = None
    already = (cnum == 10001) and (not inactive) and any(w in text.lower() for w in ALREADY_RE)
    ok = (not inactive) and ((cnum == 0 and http_ok) or already)
    return {"ok": ok, "already": already, "inactive": inactive, "code": cnum, "message": text}


def daily_checkin(token, uid, domain=""):
    last = ""
    first401 = None
    for url in checkin_endpoints(token):
        origin = "/".join(url.split("/")[:3])
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "x-client-platform": "web",
            "origin": origin,
            "referer": origin + "/profile/plans-usage",
            "authorization": "Bearer " + token,
            "x-user-id": str(uid or ""),
            "x-domain": str(domain or ""),
            "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"),
        }
        req = urllib.request.Request(url, data=b"{}", method="POST", headers=headers)
        try:
            with _OPENER.open(req, timeout=20) as r:
                body = r.read().decode("utf-8", "replace")
                status = r.status
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            status = e.code
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, str(e)[:100])
            continue
        try:
            o = json.loads(body or "{}")
        except Exception:
            o = {}
        msg = o.get("msg") or o.get("message") or ("ok" if status == 200 else "HTTP %s" % status)
        res = classify_checkin(status == 200, o.get("code"), msg)
        res.update({"status": status, "url": url, "httpOk": status == 200})
        if res["ok"]:
            return res
        if status == 401:
            first401 = res
            last = "登录身份过期（401）"
            continue
        if 400 <= status < 500 and status != 404:
            return res
        if status == 200 and status != 404:
            return res
        last = str(msg)[:120]
    if first401:
        return first401
    return {"ok": False, "already": False, "inactive": False, "code": None,
            "message": last or "签到失败", "status": None}


def _record_claim(r):
    """把单次领取结果落盘（data/checkin.json，按 uid 记当天状态）。"""
    try:
        uid = str(r.get("uid") or "")
        if not uid:
            return
        store = load_checkin()
        m = store.get(uid) if isinstance(store.get(uid), dict) else {}
        m = dict(m)
        m["date"] = time.strftime("%Y-%m-%d")
        m["ok"] = bool(r.get("ok"))
        m["already"] = bool(r.get("already"))
        m["message"] = str(r.get("message") or "")[:160]
        m["at"] = r.get("at") or int(time.time())
        store[uid] = m
        save_checkin(store)
    except Exception as e:
        log("签到结果落盘失败: %s" % e)


def claim_one(uid, force_refresh=False):
    """给单个账号领今日积分（token 临期自动保活，401 自动强刷一次）。"""
    okr, token, err = ensure_fresh_token(uid, force=force_refresh)
    if not token:
        out = {"uid": uid, "ok": False, "already": False, "inactive": False,
               "message": err or "没有可用凭证", "at": int(time.time())}
        _record_claim(out)
        return out
    res = daily_checkin(token, uid)
    if (not res.get("ok")) and res.get("status") == 401:
        ok2, token2, err2 = ensure_fresh_token(uid, force=True)
        if token2 and token2 != token:
            res = daily_checkin(token2, uid)
    out = {"uid": uid, "ok": bool(res.get("ok")), "already": bool(res.get("already")),
           "inactive": bool(res.get("inactive")), "message": str(res.get("message") or "")[:160],
           "at": int(time.time())}
    _record_claim(out)
    return out


def claim_all(uids=None, concurrency=3):
    """批量领积分，结果落 data/checkin.json（按 uid 记当天状态）。"""
    uids = uids or list_backups()
    try:
        concurrency = max(1, int(concurrency))
    except Exception:
        concurrency = 3
    results = []
    lock = threading.Lock()
    queue = list(uids)

    def worker():
        while True:
            with lock:
                if not queue:
                    return
                uid = queue.pop(0)
            try:
                r = claim_one(uid)
            except Exception as e:
                r = {"uid": uid, "ok": False, "message": str(e)[:160], "at": int(time.time())}
            with lock:
                results.append(r)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(min(concurrency, len(uids) or 1))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    today = time.strftime("%Y-%m-%d")
    # 单个结果已由 claim_one→_record_claim 落盘，这里只补并发里异常中断的那几条
    store = load_checkin()
    for r in results:
        if store.get(r["uid"], {}).get("date") == today:
            continue
        m = store.get(r["uid"]) if isinstance(store.get(r["uid"]), dict) else {}
        m = dict(m)
        m["date"] = today
        m["ok"] = bool(r.get("ok"))
        m["already"] = r.get("already", False)
        m["message"] = r.get("message", "")
        m["at"] = r.get("at", int(time.time()))
        store[r["uid"]] = m
    save_checkin(store)
    ok_n = sum(1 for r in results if r["ok"])
    log("自动领积分：%d/%d 成功" % (ok_n, len(results)))
    return results


def checkin_state(uid):
    """面板展示用：今日状态。"""
    m = load_checkin().get(uid) or {}
    today = time.strftime("%Y-%m-%d")
    if m.get("date") != today:
        return {"today": False, "text": "-", "ok": False}
    if m.get("ok"):
        return {"today": True, "text": "已领", "ok": True, "message": m.get("message", "")}
    return {"today": True, "text": "失败", "ok": False, "message": m.get("message", "")}


# ------------------------------------------------------------------ 无感登录（OAuth）
def _http_json(url, method="GET", body=None, headers=None, timeout=25):
    h = {"accept": "application/json", "user-agent": "workmom/1.0"}
    if body is not None:
        h["content-type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    else:
        data = None
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
    try:
        return json.loads(txt or "{}")
    except Exception:
        return {"code": -1, "message": txt[:300]}


def oauth_start():
    """第一步：申请 state + 授权链接（不退出、不打断当前 WorkBuddy）。"""
    c = cfg()
    host = str(c["api_host"]).rstrip("/")
    platform = c.get("oauth_platform") or "workbuddy"
    resp = _http_json("%s/v2/plugin/auth/state?platform=%s" % (host, platform), "POST", {})
    d = resp.get("data") or {}
    state = str(d.get("state") or "")
    if not state:
        raise RuntimeError("auth/state 无 state: %s" % json.dumps(resp, ensure_ascii=False)[:200])
    url = (d.get("authUrl") or d.get("auth_url") or d.get("url")
           or "%s/login/started?platform=%s&state=%s" % (host, platform, state))
    return state, url


def oauth_poll_once(state):
    """轮询一次：未完成返回 done=False；完成则把账号写入 accounts/<uid>.info。"""
    c = cfg()
    host = str(c["api_host"]).rstrip("/")
    tr = _http_json("%s/v2/plugin/auth/token?state=%s" % (host, state), "GET")
    code = tr.get("code")
    if code not in (0, 200):
        return {"done": False}
    data = tr.get("data") or {}
    at = _tv(data, "accessToken", "access_token")
    if not at:
        return {"done": False}
    h = {"authorization": "Bearer " + at}
    if data.get("domain"):
        h["x-domain"] = str(data["domain"])
    ar = _http_json("%s/v2/plugin/login/account?state=%s" % (host, state), "GET", None, h)
    acc = ar.get("data") or {}
    uid = str(acc.get("uid") or "")
    if not uid:
        return {"done": True, "ok": False, "error": "官方接口未返回 uid"}
    saved = save_oauth_account(data, acc)
    return {"done": True, "ok": True, "result": saved}


def build_session(token_data, acc_data):
    """拼官方 workbuddy-desktop.info 结构（account / auth / accounts / allAccounts）。"""
    now = int(time.time() * 1000)
    raw = dict(token_data or {})
    exp = _norm_ts(raw.get("expiresAt") if raw.get("expiresAt") is not None else raw.get("expires_at"))
    if exp is None:
        try:
            ei = float(raw.get("expiresIn") if raw.get("expiresIn") is not None else raw.get("expires_in"))
            exp = now + int(ei * 1000) if ei > 0 else None
        except Exception:
            exp = None
    rexp = _norm_ts(raw.get("refreshExpiresAt") if raw.get("refreshExpiresAt") is not None
                    else raw.get("refresh_expires_at"))
    if rexp is None:
        try:
            ri = float(raw.get("refreshExpiresIn") if raw.get("refreshExpiresIn") is not None
                       else raw.get("refresh_expires_in"))
            rexp = now + int(ri * 1000) if ri > 0 else None
        except Exception:
            rexp = None

    acc = dict(acc_data or {})
    acc.update({
        "uid": str(acc.get("uid") or ""),
        "nickname": str(acc.get("nickname") or ""),
        "uin": acc.get("uin") or "",
        "phoneNumber": acc.get("phoneNumber") or "",
        "type": acc.get("type") or "personal",
        "lastLogin": True,
        "pluginEnabled": True,
    })
    auth = dict(raw)
    auth.update({
        "accessToken": _tv(raw, "accessToken", "access_token"),
        "refreshToken": _tv(raw, "refreshToken", "refresh_token"),
        "tokenType": _tv(raw, "tokenType", "token_type") or "Bearer",
        "domain": str(raw.get("domain") or ""),
        "lastRefreshTime": now,
        "scope": raw.get("scope") or "openid profile offline_access email",
        "notBeforePolicy": raw.get("notBeforePolicy", 0),
        "sessionState": raw.get("sessionState") or "",
    })
    if exp:
        auth["expiresAt"] = exp
        auth["expiresIn"] = max(0, int((exp - now) / 1000))
        auth["refreshExpiresAt"] = rexp or exp
        auth["refreshExpiresIn"] = max(0, int(((rexp or exp) - now) / 1000))
    else:
        auth["expiresIn"] = 0
        auth["refreshExpiresIn"] = 0

    others = []
    try:
        cur = read_plain() or {}
        arr = cur.get("allAccounts") or cur.get("accounts") or []
        if isinstance(arr, list):
            others = [dict(a) for a in arr if isinstance(a, dict)]
    except Exception:
        pass
    others = [a for a in others if str(a.get("uid") or "") != acc["uid"]]
    others.append(acc)
    return {"account": acc, "auth": auth, "accounts": others, "allAccounts": others}


def save_oauth_account(token_data, acc_data):
    uid = str(acc_data.get("uid") or "")
    if not uid:
        raise RuntimeError("没有 uid，无法入库")
    session = build_session(token_data, acc_data)
    p = backup_path(uid)
    written = "plain"
    try:
        template = read_raw(auth_file())          # 借当前登录文件当加密模板
        if _contains_env(template):
            obj = encrypt_tree(session, template)
            _atomic_write(p, json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"))
            written = "encrypted"
        else:
            _atomic_write(p, json.dumps(session, ensure_ascii=False, indent=2).encode("utf-8"))
    except Exception as e:
        log("新账号加密写入失败（退明文）: %s" % e)
        _atomic_write(p, json.dumps(session, ensure_ascii=False, indent=2).encode("utf-8"))
    update_meta(uid, nickname=str(acc_data.get("nickname") or ""),
                phone=str(acc_data.get("phoneNumber") or ""),
                uin=str(acc_data.get("uin") or ""), domain=str(token_data.get("domain") or ""))
    log("无感登录入库账号 %s (%s) [%s]" % (acc_data.get("nickname"), uid[:8], written))
    return {"uid": uid, "nickname": str(acc_data.get("nickname") or ""),
            "phone": str(acc_data.get("phoneNumber") or ""), "written": written}


def open_url(url):
    try:
        os.startfile(url)
        return True
    except Exception as e:
        log("打开浏览器失败: %s" % e)
        return False


# ------------------------------------------------------------------ 模型状态（与账号解耦）
_MODEL_CACHE = {"mtime": 0.0, "model": None}


def active_model():
    c = cfg()
    default = c.get("default_model") or "kimi-k3.1-pro"
    try:
        st = os.stat(MODEL_FILE)
    except Exception:
        return default
    if _MODEL_CACHE["model"] and _MODEL_CACHE["mtime"] == st.st_mtime:
        return _MODEL_CACHE["model"]
    try:
        with open(MODEL_FILE, encoding="utf-8") as f:
            d = json.load(f) or {}
        m = str(d.get("model") or "").strip() or default
    except Exception:
        m = default
    _MODEL_CACHE.update({"mtime": st.st_mtime, "model": m})
    return m


def set_active_model(model):
    ensure_dirs()
    model = str(model or "").strip()
    if not model:
        raise RuntimeError("模型名不能为空")
    payload = {"model": model, "updated_at": int(time.time())}
    _atomic_write(MODEL_FILE, json.dumps(payload, ensure_ascii=False, indent=2))
    _MODEL_CACHE.update({"mtime": 0.0, "model": model})
    log("当前模型切换为 %s（与账号无关）" % model)
    return model


def known_models():
    return list(cfg().get("known_models") or [])


# ------------------------------------------------------------------ 自检
if __name__ == "__main__":
    ensure_dirs()
    import_external_backups()
    print("auth_file:", auth_file())
    print("current_uid:", current_uid())
    print("active_model:", active_model())
    ks = secret_key()
    print("secret_key ok, len=%d" % len(ks))
    rows = list_accounts()
    print("accounts: %d" % len(rows))
    for r in rows:
        print("  %s %-10s %-12s cur=%s exp=%s%s" % (
            r["uid"][:8], r.get("nickname") or "-", r.get("phone") or "-",
            r["is_current"], r.get("expiresAt"),
            ("  ERR:" + r["error"]) if r.get("error") else ""))
