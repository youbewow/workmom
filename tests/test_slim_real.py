# -*- coding: utf-8 -*-
"""按真实审计画像（91 条消息 / 45 条 tool / 平均 2k 字符）构造样本，验证精简效果。"""
import sys, json
sys.path.insert(0, r"D:\workmom")
import server as S

CN = "这是一段模拟真实工具返回与提示词的说明文字，包含中文与 English mixed content."

def big(n, seed=1):
    k = n // len(CN) + 2
    return (CN * k)[:n]

# system：23k 字符，且其中一段 1.2k 字符重复两遍（对应真实客户端 system 里的重复策略段）
dup_para = "【破甲策略】" + big(1200, 3)
sys_prompt = "【系统提示】\n" + big(9000) + "\n\n" + dup_para + "\n\n【中间】\n" + big(8000) + "\n\n" + dup_para

msgs = [{"role": "system", "content": sys_prompt}]
# 5 条 user（其中 2 条是同一份长期记忆块，可触发行级去重）
mem_block = big(5200, 9)
msgs.append({"role": "user", "content": "老马的第一条需求：" + big(1200)})
msgs.append({"role": "user", "content": mem_block})

# 42 组 assistant+tool，模拟 agent 跑 45 次工具
for i in range(45):
    msgs.append({"role": "assistant", "content": "", "reasoning_content": "思考 %d：%s" % (i, big(500)),
                 "tool_calls": [{"id": "c%d" % i, "type": "function",
                                 "function": {"name": "read_file", "arguments": "{\"p\":\"f%d\"}" % i}}]})
    msgs.append({"role": "tool", "tool_call_id": "c%d" % i,
                 "content": "工具返回 %d：\n" % i + big(1900, i)})   # 平均约 2k 字符
    if i == 10:
        msgs.append({"role": "user", "content": mem_block})        # 重复注入的记忆块
msgs.append({"role": "user", "content": "现在精简到最近就好"})

tools = [{"type": "function", "function": {"name": "t%d" % i,
                                            "description": big(900, i),
                                            "parameters": {"type": "object", "properties": {}}}}
         for i in range(32)]

def est_all(ms, tl):
    e = sum(S._est_tokens(json.dumps(m, ensure_ascii=False)) for m in ms)
    return e, S._est_tokens(json.dumps(tl, ensure_ascii=False))

e_msg, e_tools = est_all(msgs, tools)
print("=== 原始（贴近真实审计画像）===")
print("消息 %d 条，估算 %s tok | 工具定义 32 个 ≈ %s tok | 合计 %s tok" % (
    len(msgs), format(e_msg, ","), format(e_tools, ","), format(e_msg + e_tools, ",")))
tool_chars = sum(len(m.get("content") or "") for m in msgs if m.get("role") == "tool")
print("其中 tool 消息 %d 条 共 %s 字符" % (
    sum(1 for m in msgs if m.get("role") == "tool"), format(tool_chars, ",")))

out, st = S.slim_messages(msgs, model="kimi-k3.1-pro")
e2, _ = est_all(out, tools)
print("\n=== 精简后 ===")
print("消息 %d 条，估算 %s tok" % (len(out), format(e2, ",")))
print("消息部分省 %s tok（%.1f%%）；含固定工具定义后总输入 %s → %s tok（省 %.1f%%）" % (
    format(st["saved"], ","), st["pct"], format(e_msg + e_tools, ","), format(e2 + e_tools, ","),
    100.0 * (e_msg - e2) / (e_msg + e_tools)))
print("明细:", {k: v for k, v in st.items() if isinstance(v, int) and k not in ("before", "after", "saved")})
print("多得字段:", {k: st.get(k) for k in ("msgs_before", "msgs_after", "pct")})

# 结构校验
assert out[0]["role"] == "system", "首条必须是 system"
ids_before = [m.get("tool_call_id") for m in msgs if m.get("role") == "tool"]
ids_after = [m.get("tool_call_id") for m in out if m.get("role") == "tool"]
print("\ntool 配对完整性: %d → %d，顺序一致: %s" % (len(ids_before), len(ids_after), ids_before == ids_after))
print("最后 3 条 tool 是否保留原文（最近结果不截）:",
      all(len(m.get("content") or "") > 1500 for m in [x for x in out if x.get("role") == "tool"][-3:]))
print("每条 assistant 的 tool_calls 仍在:",
      sum(1 for m in out if m.get("tool_calls")) == sum(1 for m in msgs if m.get("tool_calls")))
print("残留 reasoning_content:",
      any("reasoning_content" in m for m in out))
