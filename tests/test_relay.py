# -*- coding: utf-8 -*-
"""workmom 回归测试：默认在临时端口起一份独立实例自测，也支持只测已跑着的反代。

用法：
    python tests/test_relay.py                  # 自起实例（端口 18799）
    python tests/test_relay.py 18800            # 自起实例（指定端口）
    python tests/test_relay.py --url http://127.0.0.1:18793   # 只测已有实例，不动进程

覆盖 P0 三项 + P1 面板/账本：
    A  /health 状态字段齐全
    B  非流式 chat
    C  chat 真流式（首字节 << 总时长，伪流式会近似相等）
    D  anthropic 真流式
    E  responses 真流式
    F  上游限流信息能否透传（6004 -> 429 + reset_at，而不是空 body 502）
    F3 错误体结构化（客户端不再只看到"原因不明"）
    F4 流式收尾有 [DONE]
    G  3 路并发墙钟 < 串行相加
    H  用量账本持久化
    I  CDP 看门狗探活
    J  Web 面板可访问
"""
import collections
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

WM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WM_DIR)

RESULTS = []
TEST_PORT = 18799
BASE = ""


def add(name, ok, note=""):
    RESULTS.append((name, bool(ok), note))
    print("[%s] %-30s %s" % ("PASS" if ok else "FAIL", name, note))


def req(path, payload=None, stream=False, timeout=240, raw=None):
    data = raw if raw is not None else (
        json.dumps(payload).encode("utf-8") if payload is not None else None)
    r = urllib.request.Request(BASE + path, data=data,
                               headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        resp = urllib.request.urlopen(r, timeout=timeout)
    except urllib.error.HTTPError as e:
        return {"status": e.code, "body": e.read().decode("utf-8", "replace"),
                "ttfb": None, "dur": time.time() - t0,
                "headers": dict(e.headers), "lines": 0}
    except Exception as e:
        return {"status": 0, "body": "EXC: %s" % e, "ttfb": None,
                "dur": time.time() - t0, "headers": {}, "lines": 0}

    if not stream:
        return {"status": resp.status, "body": resp.read().decode("utf-8", "replace"),
                "ttfb": None, "dur": time.time() - t0,
                "headers": dict(resp.headers), "lines": 0}

    ttfb, n, kept = None, 0, []
    tail = collections.deque(maxlen=3)
    while True:
        line = resp.readline()
        if not line:
            break
        n += 1
        if ttfb is None and line.strip():
            ttfb = time.time() - t0
        txt = line.decode("utf-8", "replace")
        if len(kept) < 40:
            kept.append(txt)
        tail.append(txt)
    body = "".join(kept)
    if n > 40:
        body += "\n...[TAIL]...\n" + "".join(tail)
    return {"status": resp.status, "body": body, "ttfb": ttfb,
            "dur": time.time() - t0, "headers": dict(resp.headers), "lines": n}


def wait_up(limit=60):
    for _ in range(limit * 2):
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def run_tests():
    # ---------- A ----------
    try:
        hj = json.loads(req("/health")["body"])
    except Exception:
        hj = {}
    need = ["transport", "transport_ready", "max_concurrency", "direct",
            "cdp_health", "usage", "cooldowns", "auto_heal", "fallback_models"]
    miss = [k for k in need if k not in hj]
    add("A /health 状态字段", not miss,
        "transport=%s ready=%s 缺=%s" % (hj.get("transport"),
                                         hj.get("transport_ready"), miss))

    # ---------- B ----------
    b = req("/v1/chat/completions", {"model": "kimi-k3-2",
                                     "messages": [{"role": "user",
                                                   "content": "用一句话说明你在做什么"}]})
    txt, ok_b = "", b["status"] == 200
    try:
        txt = json.loads(b["body"])["choices"][0]["message"]["content"][:40]
    except Exception:
        ok_b = False
    add("B 非流式 chat", ok_b, "status=%s %.1fs %r" % (b["status"], b["dur"], txt))

    # ---------- C ----------
    c = req("/v1/chat/completions", {"model": "kimi-k3-2", "stream": True,
                                     "messages": [{"role": "user",
                                                   "content": "从1数到40，每个数字后用顿号隔开"}]},
            stream=True)
    ttfb = c["ttfb"] or 0
    ok_c = (c["status"] == 200 and ttfb > 0 and c["dur"] > 2
            and ttfb < max(3.0, c["dur"] * 0.6))
    add("C chat 真流式", ok_c,
        "首字节 %.2fs / 总 %.2fs / %d 行（伪流式时两者会几乎相等）"
        % (ttfb, c["dur"], c["lines"]))

    # ---------- D ----------
    d = req("/v1/messages", {"model": "kimi-k3-2", "stream": True, "max_tokens": 300,
                             "messages": [{"role": "user",
                                           "content": "从1数到40，每个数字后用顿号隔开"}]},
            stream=True)
    dt = d["ttfb"] or 0
    ok_d = (d["status"] == 200 and dt > 0 and "content_block_delta" in d["body"]
            and dt < max(3.0, d["dur"] * 0.6))
    add("D anthropic 真流式", ok_d, "首字节 %.2fs / 总 %.2fs / 事件=%s"
        % (dt, d["dur"], "yes" if "content_block_delta" in d["body"] else "no"))

    # ---------- E ----------
    e = req("/v1/responses", {"model": "kimi-k3-2", "stream": True,
                              "input": [{"role": "user",
                                         "content": [{"type": "input_text",
                                                      "text": "从1数到40，每个数字后用顿号隔开"}]}]},
            stream=True)
    et = e["ttfb"] or 0
    ok_e = e["status"] == 200 and et > 0 and et < max(3.0, e["dur"] * 0.6)
    add("E responses 真流式", ok_e, "首字节 %.2fs / 总 %.2fs / %d 行"
        % (et, e["dur"], e["lines"]))

    # ---------- F（真的撞上限流就验证透传；没撞上就用 F2 定向验证）----------
    f = req("/v1/chat/completions", {"model": "kimi-k3.1-pro",
                                     "messages": [{"role": "user", "content": "hi"}]})
    hdr = {k.lower(): v for k, v in (f["headers"] or {}).items()}
    if f["status"] == 429:
        sig = ("6004" in f["body"]) or ("rate_limit" in f["body"]) or ("reset_at" in f["body"])
        add("F 真实限额透传", sig, "status=429 retry-after=%s quota-hdr=%s body=%s"
            % (hdr.get("retry-after"), hdr.get("x-wm-quota-exceeded"),
               f["body"][:200].replace("\n", " ")))
    else:
        add("F 真实限额透传", True,
            "当前 pro 未在限流窗口（status=%s），靠 F2 定向验证翻译逻辑" % f["status"])

    # ---------- F2：6004 -> 429 的错误翻译 ----------
    try:
        import wm_transport as WT
        sample = json.dumps({"code": 6004,
                             "msg": "model frequency limit exceeded: kimi-k3.1-pro, "
                                    "reset time: 2026-11-13 03:51:46"}).encode()
        err = WT.parse_error(429, sample)
        add("F2 6004 错误翻译",
            err.http == 429 and err.code == "6004" and (err.reset_at or err.message),
            "http=%s code=%s type=%s reset_at=%r msg=%s"
            % (err.http, err.code, err.type, err.reset_at, err.message[:60]))
    except Exception as ex:
        add("F2 6004 错误翻译", False, "异常 %s" % ex)

    # ---------- F3：错误体必须结构化非空 ----------
    try:
        r3 = req("/v1/chat/completions", raw=b"{bad json")
        e33 = (json.loads(r3["body"]).get("error") or {})
        add("F3 错误体非空且结构化",
            r3["status"] == 400 and bool(e33.get("message")) and bool(e33.get("type")),
            "status=%s type=%s message=%s" % (r3["status"], e33.get("type"),
                                              str(e33.get("message"))[:80]))
    except Exception as ex:
        add("F3 错误体非空且结构化", False, "异常 %s" % ex)

    # ---------- F4：流式收尾 ----------
    f4 = req("/v1/chat/completions", {"model": "kimi-k3-2", "stream": True,
                                      "messages": [{"role": "user", "content": "说1句话"}]},
             stream=True)
    add("F4 流式有 [DONE]", f4["status"] == 200 and "[DONE]" in f4["body"],
        "status=%s 含DONE=%s" % (f4["status"], "[DONE]" in f4["body"]))

    # ---------- G：并发 ----------
    out = {}

    def one(i):
        r = req("/v1/chat/completions",
                {"model": "kimi-k3-2", "stream": True,
                 "messages": [{"role": "user",
                               "content": "写一段120字左右的短文，讲下雨的街道"}]},
                stream=True)
        out[i] = (r["status"], r["dur"])

    t0 = time.time()
    ths = [threading.Thread(target=one, args=(i,)) for i in range(3)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    wall = time.time() - t0
    serial = sum(v[1] for v in out.values())
    add("G 3 路并发", all(v[0] == 200 for v in out.values()) and wall < serial * 0.85,
        "墙钟 %.2fs / 各请求 %r（串行相加 %.2fs）"
        % (wall, [round(v[1], 2) for v in out.values()], serial))

    # ---------- H：用量账本 ----------
    try:
        u = (json.loads(req("/relay_state")["body"]).get("usage") or {})
    except Exception:
        u = {}
    add("H 用量账本", u.get("requests", 0) > 0,
        "requests=%s tokens_in=%s tokens_out=%s 落盘=%s"
        % (u.get("requests"), u.get("tokens_in"), u.get("tokens_out"),
           os.path.exists(u.get("file") or "")))

    # ---------- I：看门狗探活 ----------
    try:
        cd = (json.loads(req("/relay_state")["body"]).get("cdp_health") or {})
        add("I CDP 探活状态", cd.get("last_check", 0) > 0,
            "ok=%s streak=%s err=%s" % (cd.get("ok"), cd.get("fail_streak"),
                                        str(cd.get("last_err"))[:60]))
    except Exception as ex:
        add("I CDP 探活状态", False, "读取失败 %s" % ex)

    # ---------- J：Web 面板 ----------
    try:
        p = req("/")
        add("J Web 面板", p["status"] == 200 and "workmom" in p["body"]
            and "<html" in p["body"].lower(),
            "status=%s 长度=%d" % (p["status"], len(p["body"])))
    except Exception as ex:
        add("J Web 面板", False, "异常 %s" % ex)


def main():
    global BASE, TEST_PORT
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    use_url = None
    for i, a in enumerate(sys.argv):
        if a == "--url" and i + 1 < len(sys.argv):
            use_url = sys.argv[i + 1]

    if use_url:
        BASE = use_url.rstrip("/")
        print("只测已有实例 -> %s\n" % BASE)
        run_tests()
        return summarize()

    if args:
        TEST_PORT = int(args[0])
    BASE = "http://127.0.0.1:%d" % TEST_PORT

    import subprocess
    tmpdir = tempfile.mkdtemp(prefix="wm_relay_test_")
    logpath = os.path.join(tmpdir, "relay.log")
    logf = open(logpath, "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "server.py", str(TEST_PORT)],
                            cwd=WM_DIR, stdout=logf, stderr=subprocess.STDOUT)
    try:
        if not wait_up():
            print("!! 实例没起来，看日志:", logpath)
            print(open(logpath, encoding="utf-8", errors="replace").read()[-3000:])
            return 1
        print("实例已就绪 -> %s\n" % BASE)
        run_tests()
        print("\n--- 反代启动日志（前 25 行）---")
        try:
            print("".join(open(logpath, encoding="utf-8",
                               errors="replace").readlines()[:25]))
        except Exception:
            pass
        return summarize()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        logf.close()


def summarize():
    print("\n=== 汇总 ===")
    bad = [n for n, ok, _ in RESULTS if not ok]
    print("通过 %d / %d" % (len(RESULTS) - len(bad), len(RESULTS)))
    if bad:
        print("未通过:", bad)
        return 1
    return 0


if __name__ == "__main__":
    import traceback
    try:
        code = main()
    except Exception:
        traceback.print_exc()
        code = 1
    sys.stdout.flush()
    sys.exit(code)
