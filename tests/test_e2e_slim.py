# -*- coding: utf-8 -*-
"""端到端验证：向反代发一个带旧 thinking / 多 system / 重复块的中型请求，看 slim 是否生效且上游正常。"""
import json, urllib.request, sys

BASE = "http://127.0.0.1:18793"
CN = "这是一段用来撑大输入的说明文字，模拟客户端每轮重复注入的上下文与工具返回内容。"

def big(n, seed=1):
    s = (CN * (n // len(CN) + 2))
    return s[:n]

sys1 = "【系统】" + big(1800)
sys2 = "【记忆】" + big(900)
dup = big(2500, 7)

msgs = [
    {"role": "system", "content": sys1},
    {"role": "system", "content": sys2},
    {"role": "user", "content": dup},
    {"role": "assistant", "content": "收到。", "reasoning_content": "旧思考：" + big(2000)},
    {"role": "user", "content": "帮我看看这个文件"},
    {"role": "assistant", "content": "", "reasoning_content": "调用工具：" + big(1500),
     "tool_calls": [{"id": "c1", "type": "function",
                     "function": {"name": "noop", "arguments": "{\"path\":\"a.txt\"}"}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "工具返回：" + big(1500)},
    {"role": "assistant", "content": "看完了。"},
    {"role": "user", "content": dup},
    {"role": "user", "content": "只回四个字：精简成功"},
]

body = {"model": "kimi-k3.1-pro", "stream": False, "max_tokens": 32,
        "messages": msgs,
        "tools": [{"type": "function", "function": {
            "name": "noop", "description": "占位工具",
            "parameters": {"type": "object", "properties": {}}}}]}

req = urllib.request.Request(BASE + "/v1/chat/completions",
                             data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                             headers={"Content-Type": "application/json", "X-WM-Audit": "1"})
try:
    with urllib.request.urlopen(req, timeout=180) as r:
        raw = r.read().decode("utf-8", "replace")
        print("HTTP", r.status)
        print("--- slim/quota 头 ---")
        for k, v in r.getheaders():
            if k.lower().startswith("x-wm"):
                print("  %-26s %s" % (k, v))
        d = json.loads(raw)
        msg = d["choices"][0]["message"]
        print("--- 上游返回 ---")
        print("  正文:", (msg.get("content") or "")[:80])
        print("  usage:", json.dumps(d.get("usage"), ensure_ascii=False))
except urllib.error.HTTPError as e:
    print("HTTP", e.code)
    print("--- 头 ---")
    for k, v in e.headers.items():
        if k.lower().startswith("x-wm"):
            print("  %-26s %s" % (k, v))
    print(e.read().decode("utf-8", "replace")[:800])
except Exception as e:
    print("ERR", type(e).__name__, e)
