#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workmom 一键初始化。

把「clone 下来怎么用」从一堆手工步骤压成双击一次：

  1) 检查 Python 版本与工作目录
  2) 检查并安装 Python 依赖
  3) 自动定位 WorkBuddy 安装位置（注册表 -> 常见目录 -> 运行中进程）
  4) 生成 / 补全 config.json 与 pythonw.txt
  5) 检查 WorkBuddy 的 CDP 调试端口（不通会先问你，不擅自杀进程）
  6) 拉起本地反代做一次端到端自检
  7) 建桌面快捷方式

用法:
    setup.bat                            双击就是交互式
    python setup.py                      交互式
    python setup.py -y                   全部按默认走（无人值守，危险操作仍默认不执行）
    python setup.py --no-shortcut        不建快捷方式
    python setup.py --no-selftest        跳过反代自检

设计原则:
  * 只做幂等操作，重复跑不会破坏已有配置
  * 涉及关闭 WorkBuddy 这类有副作用的动作，一律先问，默认不执行
  * 依赖缺失不影响脚本自身运行（workmom_core 只用标准库）
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")
EXAMPLE_FILE = os.path.join(HERE, "config.example.json")
REQ_FILE = os.path.join(HERE, "requirements.txt")
PYW_FILE = os.path.join(HERE, "pythonw.txt")

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DETACHED = 0x00000008 | 0x00000200

AUTO = False
SKIP_SHORTCUT = False
SKIP_SELFTEST = False

_STEP = [0]
TOTAL_STEPS = 7
FAILS = []
WARNS = []
WB_EXE = ""


# --------------------------------------------------------------------------- 输出
def _fix_console():
    """避免中文在个别代码页下直接抛 UnicodeEncodeError。"""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:
            pass


def step(title):
    _STEP[0] += 1
    print("")
    print("-" * 70)
    print("[%d/%d] %s" % (_STEP[0], TOTAL_STEPS, title))
    print("-" * 70)


def ok(msg):
    print("    [ OK ] " + msg)


def warn(msg):
    print("    [WARN] " + msg)
    WARNS.append(msg)


def fail(msg):
    print("    [FAIL] " + msg)
    FAILS.append(msg)


def info(msg):
    print("    [ -- ] " + msg)


def ask(q, default=True):
    """交互询问。AUTO 模式 / 无终端时直接返回默认值。"""
    if AUTO:
        print("    [ -- ] %s -> 自动选 %s" % (q, "是" if default else "否"))
        return default
    if not sys.stdin or not sys.stdin.isatty():
        print("    [ -- ] %s -> 非交互环境，按默认 %s" % (q, "是" if default else "否"))
        return default
    tip = "[Y/n]" if default else "[y/N]"
    try:
        r = input("    %s %s " % (q, tip)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("")
        return default
    if r == "":
        return default
    return r in ("y", "yes", "是", "好", "1", "ok", "继续")


# --------------------------------------------------------------------------- 工具
def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def run(cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, creationflags=NO_WINDOW)
    except Exception:
        return None


def port_open(port, timeout=2.0):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def core_mod():
    """尽量复用 workmom_core 的实现；失败返回 None（脚本自身不依赖它）。"""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        import workmom_core
        return workmom_core
    except Exception as e:
        info("未能导入 workmom_core（%s），改用内置探测" % e)
        return None


# --------------------------------------------------------------------------- 步骤 1
def check_env():
    step("检查环境")
    v = sys.version_info
    info("Python %d.%d.%d   %s" % (v.major, v.minor, v.micro, sys.executable))
    if v < (3, 9):
        fail("Python 版本过低，需要 3.9 以上")
        return False
    ok("Python 版本满足要求（>= 3.9）")

    info("工作目录 " + HERE)
    need = ["server.py", "workmom_core.py", "requirements.txt"]
    missing = [f for f in need if not os.path.isfile(os.path.join(HERE, f))]
    if missing:
        fail("当前目录不完整，缺少: " + ", ".join(missing))
        info("请确认 setup.py 与 server.py 放在同一目录（即仓库根目录）")
        return False
    ok("目录结构完整")

    if os.name != "nt":
        warn("当前不是 Windows，WorkBuddy 桌面端只支持 Windows，后续步骤会跳过")
    return True


# --------------------------------------------------------------------------- 步骤 2
def ensure_deps():
    step("检查 Python 依赖")
    mapping = [("websocket", "websocket-client"), ("cryptography", "cryptography")]
    missing = []
    for mod, pkg in mapping:
        try:
            __import__(mod)
            ok("%s 已安装" % pkg)
        except Exception:
            missing.append(pkg)
            warn("%s 缺失" % pkg)

    if not missing:
        return True

    if not os.path.isfile(REQ_FILE):
        fail("requirements.txt 不存在，无法自动安装")
        return False

    info("将执行: %s -m pip install -r requirements.txt" % os.path.basename(sys.executable))
    if not ask("现在安装上面缺的依赖？", True):
        fail("依赖未安装，反代无法启动（稍后可自己执行 pip install -r requirements.txt）")
        return False

    try:
        rc = subprocess.call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                              "-r", REQ_FILE], cwd=HERE)
    except Exception as e:
        fail("pip 调用失败: %s" % e)
        return False

    if rc != 0:
        fail("pip 安装失败（退出码 %d），可换国内源重试：" % rc)
        info("pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple")
        return False

    still = []
    for mod, pkg in mapping:
        try:
            __import__(mod)
        except Exception:
            still.append(pkg)
    if still:
        fail("安装后仍缺: " + ", ".join(still))
        return False
    ok("依赖安装完成")
    return True


# --------------------------------------------------------------------------- 步骤 3
def _wb_from_registry_fallback():
    """core 不可用时的极简注册表探测（App Paths + 卸载信息 DisplayIcon）。"""
    try:
        import winreg
    except Exception:
        return ""
    target = "workbuddy.exe"

    def as_exe(raw):
        p = str(raw or "").strip().strip('"')
        if "," in p:
            p = p.split(",")[0]
        p = p.strip().strip('"')
        return p if p and os.path.basename(p).lower() == target and os.path.isfile(p) else ""

    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            k = winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\WorkBuddy.exe")
            p = as_exe(winreg.QueryValueEx(k, "")[0])
            if p:
                return p
        except Exception:
            pass
        try:
            base = winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")
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
                p = as_exe(val)
                if p:
                    return p
                cand = os.path.join(val.strip().strip('"'), "WorkBuddy.exe")
                if val.strip() and os.path.isfile(cand):
                    return cand
    return ""


def _wb_from_common_fallback():
    la = os.environ.get("LOCALAPPDATA") or ""
    pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
    pf86 = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    cands = [os.path.join(la, "Programs", "WorkBuddy", "WorkBuddy.exe") if la else "",
             os.path.join(pf, "WorkBuddy", "WorkBuddy.exe"),
             os.path.join(pf86, "WorkBuddy", "WorkBuddy.exe")]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


def find_workbuddy():
    """返回 WorkBuddy.exe 绝对路径，找不到返回空串。"""
    global WB_EXE
    step("定位 WorkBuddy")

    if os.name != "nt":
        warn("非 Windows，跳过 WorkBuddy 定位")
        return ""

    wm = core_mod()
    p = ""
    if wm is not None:
        try:
            p = wm._guess_wb_exe() or ""
            if p:
                ok("自动探测命中（注册表 / 常见目录 / 运行中进程）")
        except Exception as e:
            info("core 探测异常: %s" % e)

    if not p:
        p = _wb_from_registry_fallback() or _wb_from_common_fallback()
        if p:
            ok("内置兜底探测命中")

    if p:
        ok("WorkBuddy.exe -> " + p)
        try:
            size = os.path.getsize(p) / 1024.0 / 1024.0
            info("文件大小 %.1f MB" % size)
        except Exception:
            pass
        WB_EXE = p
        return p

    warn("没找到 WorkBuddy.exe（可能还没装，或装在非常规位置）")
    info("下载地址: https://www.workbuddy.cn")
    info("如果你知道它在哪，直接粘贴完整路径回车即可（留空则跳过）")
    if not AUTO and sys.stdin and sys.stdin.isatty():
        try:
            typed = input("    路径: ").strip().strip('"')
        except (EOFError, KeyboardInterrupt):
            typed = ""
        if typed and os.path.isfile(typed):
            ok("使用手工指定路径")
            WB_EXE = typed
            return typed
        if typed:
            fail("该路径不存在: " + typed)
    else:
        fail("未定位到 WorkBuddy，后续 CDP 相关步骤会跳过")
    return ""


# --------------------------------------------------------------------------- 步骤 4
def write_config():
    step("生成配置")
    data = {}
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8-sig") as f:
                data = json.load(f) or {}
            ok("已读取现有 config.json（会保留你的自定义项）")
        except Exception as e:
            warn("现有 config.json 解析失败: %s" % e)
            broken = CONFIG_FILE + ".broken"
            try:
                os.replace(CONFIG_FILE, broken)
                info("原文件已改名为 %s 保留，未删除" % os.path.basename(broken))
            except Exception:
                pass
            data = {}
    elif os.path.isfile(EXAMPLE_FILE):
        try:
            with open(EXAMPLE_FILE, encoding="utf-8-sig") as f:
                data = json.load(f) or {}
            info("以 config.example.json 为模板")
        except Exception:
            data = {}
    else:
        info("从内置默认值生成")

    defaults = {
        "wb_exe": "",
        "cdp_port": 9226,
        "cdp_timeout_sec": 300,
        "api_host": "https://www.workbuddy.cn",
        "oauth_platform": "workbuddy",
        "relay_port": 18793,
        "wd_dir": "",
        "default_model": "kimi-k3.1-pro",
        "checkin_concurrency": 3,
    }
    for k, v in defaults.items():
        data.setdefault(k, v)

    if WB_EXE:
        old = data.get("wb_exe")
        data["wb_exe"] = WB_EXE
        if old and old != WB_EXE:
            info("wb_exe 已更新: %s -> %s" % (old, WB_EXE))
        else:
            ok("wb_exe = " + WB_EXE)
    elif not data.get("wb_exe"):
        warn("wb_exe 为空，运行时将由 workmom_core 自动探测")
    else:
        ok("沿用已配置的 wb_exe = " + str(data["wb_exe"]))

    # 端口合理性检查：避开 WorkBuddy 自身常用端口
    for key in ("cdp_port", "relay_port"):
        try:
            data[key] = int(data[key])
            port_open(data[key])
            ok("%s = %d" % (key, data[key]))
        except Exception:
            warn("%s 不是合法整数，回退默认值" % key)
            data[key] = defaults[key]

    try:
        with open(CONFIG_FILE, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        ok("已写入 config.json（UTF-8 无 BOM）")
    except Exception as e:
        fail("写入 config.json 失败: %s" % e)
        return data

    # pythonw.txt：让 VBS 用无控制台解释器启动服务
    if os.name == "nt":
        if os.path.isfile(PYW_FILE):
            try:
                cur = open(PYW_FILE, encoding="utf-8-sig").read().strip()
            except Exception:
                cur = ""
            if cur and os.path.isfile(cur):
                ok("pythonw.txt 已存在且有效，保持不动")
                return data
            info("pythonw.txt 里的路径无效，将重写")
        exe = sys.executable
        cand = ""
        if exe.lower().endswith("python.exe"):
            cand = exe[:-len("python.exe")] + "pythonw.exe"
        if cand and os.path.isfile(cand):
            try:
                with open(PYW_FILE, "w", encoding="utf-8", newline="\n") as f:
                    f.write(cand + "\n")
                ok("已写入 pythonw.txt -> " + cand)
            except Exception as e:
                warn("写入 pythonw.txt 失败: %s" % e)
        else:
            warn("未找到 pythonw.exe，VBS 将回退到 PATH 里查找")
    return data


# --------------------------------------------------------------------------- 步骤 5
def check_cdp(cfgdata):
    step("检查 WorkBuddy 调试端口")
    if os.name != "nt":
        warn("非 Windows，跳过")
        return False
    port = int(cfgdata.get("cdp_port") or 9226)

    def probe():
        if not port_open(port, 2.0):
            return False, "127.0.0.1:%d 未监听" % port
        try:
            with opener().open("http://127.0.0.1:%d/json/version" % port, timeout=3) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            return True, d.get("Browser") or "CDP 可用"
        except Exception as e:
            return False, "端口开着但 /json/version 不通: %s" % e

    good, detail = probe()
    if good:
        ok("CDP 已就绪（%s）" % detail)
        return True

    warn("CDP 不通: " + detail)
    wm = core_mod()

    r = run(["tasklist", "/FI", "IMAGENAME eq WorkBuddy.exe", "/NH"])
    running = bool(r and "workbuddy.exe" in (r.stdout or "").lower())

    if running:
        info("WorkBuddy 正在运行，但没带 --remote-debugging-port=%d 参数启动" % port)
        info("要打开调试端口只能重启它，重启会关掉当前窗口，未保存的内容可能丢失")
        if not ask("现在重启 WorkBuddy？（默认否，你可以稍后自己重启）", False):
            info("已跳过。手动做法：完全退出 WorkBuddy，然后命令行运行")
            info('    "%s" --remote-debugging-port=%d' % (WB_EXE or "WorkBuddy.exe", port))
            warn("CDP 未就绪，反代连不上，第 6 步自检很可能失败")
            return False
    else:
        info("WorkBuddy 当前没在运行")
        if not ask("现在带调试端口启动它？", True):
            info("已跳过。手动做法：")
            info('    "%s" --remote-debugging-port=%d' % (WB_EXE or "WorkBuddy.exe", port))
            return False

    if wm is not None and hasattr(wm, "restart_wb"):
        info("正在重启 WorkBuddy 并等待 CDP 就绪（最多 120 秒，别急）")
        try:
            done = wm.restart_wb()
        except Exception as e:
            fail("restart_wb 异常: %s" % e)
            done = False
    else:
        fail("workmom_core 不可用，无法自动重启")
        info('请手动执行: "%s" --remote-debugging-port=%d' % (WB_EXE or "WorkBuddy.exe", port))
        return False

    good, detail = probe() if done else (False, "重启后仍未就绪")
    if good:
        ok("CDP 已就绪（%s）" % detail)
        return True
    fail("CDP 仍未就绪: " + detail)
    info("常见原因：WorkBuddy 被别的实例占用单例锁；或端口被改过")
    return False


# --------------------------------------------------------------------------- 步骤 6
def selftest(cfgdata):
    step("反代自检")
    if SKIP_SELFTEST:
        info("已按参数跳过")
        return None
    port = int(cfgdata.get("relay_port") or 18793)

    def health(timeout=3):
        try:
            with opener().open("http://127.0.0.1:%d/health" % port, timeout=timeout) as r:
                return True, r.read(1500).decode("utf-8", "replace")
        except Exception as e:
            return False, str(e)

    good, body = health()
    started = False
    if good:
        ok("反代已在运行（127.0.0.1:%d）" % port)
    else:
        vbs = os.path.join(HERE, "run_relay.vbs")
        if not os.path.isfile(vbs):
            warn("run_relay.vbs 不存在，无法自动启动反代")
            info("可手动执行: python server.py %d" % port)
            return False
        info("反代未运行，正在后台启动（127.0.0.1:%d）" % port)
        try:
            subprocess.Popen(["wscript.exe", vbs], cwd=HERE, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            started = True
        except Exception as e:
            fail("启动反代失败: %s" % e)
            return False
        t0 = time.time()
        while time.time() - t0 < 30:
            good, body = health()
            if good:
                break
            time.sleep(1.5)

    if not good:
        fail("反代没有响应: " + str(body)[:200])
        info("看日志: %s" % os.path.join(HERE, "logs", "relay.log"))
        info("最常见原因就是上一步 CDP 没就绪")
        return False

    ok("反代 /health 正常")
    snippet = (body or "").strip().replace("\n", " ")
    if snippet:
        info("响应片段: " + snippet[:160])

    # 客户端真正要用的地址
    ok("OpenAI 兼容地址: http://127.0.0.1:%d/v1" % port)
    info("在 Marvis 等客户端的自定义模型里填这个 Base URL，模型名填 kimi-k3.1-pro")
    if started:
        info("刚拉起的是隐藏后台进程，关掉用 stop_all.bat")
    return True


# --------------------------------------------------------------------------- 步骤 7
def finish():
    step("收尾")
    if os.name == "nt" and not SKIP_SHORTCUT:
        vbs = os.path.join(HERE, "start_all.vbs")
        if not os.path.isfile(vbs):
            warn("start_all.vbs 不存在，跳过快捷方式")
        else:
            r = run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "[Environment]::GetFolderPath('Desktop')"], timeout=25)
            desk = (r.stdout or "").strip() if r else ""
            if not desk or not os.path.isdir(desk):
                desk = os.path.join(os.path.expanduser("~"), "Desktop")
            lnk = os.path.join(desk, "workmom.lnk")
            icon = WB_EXE or sys.executable
            ps = ('$W=New-Object -ComObject WScript.Shell;'
                  '$s=$W.CreateShortcut("%s");'
                  '$s.TargetPath="wscript.exe";'
                  '$s.Arguments=\'"%s"\';'
                  '$s.WorkingDirectory="%s";'
                  '$s.IconLocation="%s";'
                  '$s.Description="workmom - WorkBuddy 本地反代与账号模型总控";'
                  '$s.Save()') % (lnk, vbs, HERE, icon)
            r2 = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=25)
            if r2 is not None and r2.returncode == 0 and os.path.isfile(lnk):
                ok("桌面快捷方式: " + lnk)
            else:
                warn("快捷方式创建失败，可直接双击 start_all.vbs")
    elif SKIP_SHORTCUT:
        info("已按参数跳过快捷方式")

    # 汇总
    print("")
    print("=" * 70)
    if FAILS:
        print(" 初始化完成，但有 %d 项没通过：" % len(FAILS))
        for f in FAILS:
            print("   x " + f)
    else:
        print(" 初始化完成，全部检查通过。")
    if WARNS:
        print("")
        print(" 提醒（%d 项）：" % len(WARNS))
        for w in WARNS:
            print("   ! " + w)
    print("=" * 70)
    print("")
    print(" 下一步：")
    print("   1) 双击桌面 workmom 图标，或双击 start_all.vbs 启动全部服务")
    print("   2) 浏览器打开 http://127.0.0.1:18793  看监控面板")
    print("   3) 客户端里配 Base URL: http://127.0.0.1:18793/v1")
    print("      API Key 随便填，模型名填 kimi-k3.1-pro")
    print("")
    print(" 停止服务: 双击 stop_all.bat")
    print(" 详细说明: README.md；架构与踩坑 docs/ARCHITECTURE.md")
    print("")
    return 0 if not FAILS else 1


# --------------------------------------------------------------------------- main
def main():
    global AUTO, SKIP_SHORTCUT, SKIP_SELFTEST
    ap = argparse.ArgumentParser(add_help=True, description="workmom 一键初始化")
    ap.add_argument("-y", "--yes", action="store_true", help="全部按默认值，无人值守")
    ap.add_argument("--no-shortcut", action="store_true", help="不创建桌面快捷方式")
    ap.add_argument("--no-selftest", action="store_true", help="跳过反代自检")
    args = ap.parse_args()
    AUTO = args.yes
    SKIP_SHORTCUT = args.no_shortcut
    SKIP_SELFTEST = args.no_selftest

    _fix_console()
    print("=" * 70)
    print(" workmom 一键初始化")
    print(" 本地反代 + WorkBuddy 账号 / 模型总控")
    print("=" * 70)
    if AUTO:
        print(" 运行模式: 无人值守（-y）")

    if not check_env():
        print("\n环境不满足，已中止。")
        return 2

    ensure_deps()
    find_workbuddy()
    cfgdata = write_config()
    check_cdp(cfgdata)
    selftest(cfgdata)
    return finish()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(130)
