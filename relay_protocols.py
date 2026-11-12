# -*- coding: utf-8 -*-
"""
relay_protocols —— workmom 反代的多协议适配层

把三种客户端协议统一翻译成上游 OpenAI Chat Completions，再把上游结果翻译回去：

  1) OpenAI Chat Completions   /v1/chat/completions   原生（Marvis / Cherry / 任意 OpenAI 兼容客户端）
  2) Anthropic Messages        /v1/messages           Claude Code / claude-code-router / 各类 Anthropic SDK
  3) OpenAI Responses          /v1/responses          Codex CLI / 新版 openai SDK（DSH 之类走 OpenAI 协议的直接用 1）

本模块只做纯数据转换，不碰网络、不碰 CDP，方便单测。
"""

import json
import time
import uuid


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _rid(prefix):
    return prefix + uuid.uuid4().hex[:24]


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _json_dumps(o):
    return json.dumps(o, ensure_ascii=False)


def _map_finish_to_anthropic(finish, has_tools):
    if has_tools or finish == "tool_calls":
        return "tool_use"
    if finish == "length":
        return "max_tokens"
    if finish in ("content_filter",):
        return "refusal"
    return "end_turn"


def _map_finish_to_responses(finish, has_tools):
    if has_tools or finish == "tool_calls":
        return "completed"          # Responses 用 output item 表达工具调用，status 仍是 completed
    return "completed"


# ---------------------------------------------------------------------------
# 一、Anthropic Messages  ->  OpenAI Chat Completions
# ---------------------------------------------------------------------------
def _anth_blocks_to_openai_content(blocks):
    """Anthropic content block 数组 -> OpenAI content（str 或 parts 数组）。"""
    texts, parts, has_img = [], [], False
    for b in _as_list(blocks):
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            txt = b.get("text") or ""
            texts.append(txt)
            parts.append({"type": "text", "text": txt})
        elif t == "image":
            src = b.get("source") or {}
            url = ""
            if src.get("type") == "base64":
                url = "data:%s;base64,%s" % (src.get("media_type") or "image/png", src.get("data") or "")
            elif src.get("type") == "url":
                url = src.get("url") or ""
            if url:
                has_img = True
                parts.append({"type": "image_url", "image_url": {"url": url}})
    if has_img:
        return parts
    return "\n".join([x for x in texts])


def anthropic_to_openai(req):
    """返回 (openai_body, meta)。meta 记录原始 Anthropic 请求里的可选字段。"""
    messages = []

    sys_txt = req.get("system")
    if isinstance(sys_txt, str) and sys_txt.strip():
        messages.append({"role": "system", "content": sys_txt})
    elif isinstance(sys_txt, list):
        joined = "\n".join([(b.get("text") or "") for b in sys_txt
                            if isinstance(b, dict) and b.get("type") == "text"])
        if joined.strip():
            messages.append({"role": "system", "content": joined})

    for m in _as_list(req.get("messages")):
        if not isinstance(m, dict):
            continue
        role = m.get("role") or "user"
        content = m.get("content")

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        blocks = _as_list(content)

        if role == "assistant":
            texts, tool_calls = [], []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    if b.get("text"):
                        texts.append(b["text"])
                elif bt == "tool_use":
                    tool_calls.append({
                        "id": b.get("id") or _rid("call_"),
                        "type": "function",
                        "function": {
                            "name": b.get("name") or "",
                            "arguments": _json_dumps(b.get("input") or {}),
                        },
                    })
                # thinking / redacted_thinking 直接丢弃（上游不需要回传）
            msg = {"role": "assistant"}
            if tool_calls:
                msg["content"] = "\n".join(texts) if texts else None
                msg["tool_calls"] = tool_calls
            else:
                msg["content"] = "\n".join(texts)
            messages.append(msg)
            continue

        # user：tool_result 必须紧跟在 assistant 的 tool_calls 后面，先发 tool 再发 user 文本
        tool_msgs, plain_blocks = [], []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result":
                c = b.get("content")
                if isinstance(c, str):
                    txt = c
                else:
                    txt = "\n".join([(x.get("text") or "") for x in _as_list(c)
                                     if isinstance(x, dict) and x.get("type") == "text"])
                tool_msgs.append({
                    "role": "tool",
                    "tool_call_id": b.get("tool_use_id") or "",
                    "content": txt,
                })
            else:
                plain_blocks.append(b)

        messages.extend(tool_msgs)
        if plain_blocks:
            conv = _anth_blocks_to_openai_content(plain_blocks)
            if isinstance(conv, str):
                if conv.strip() or not tool_msgs:
                    messages.append({"role": "user", "content": conv})
            else:
                messages.append({"role": "user", "content": conv})

    body = {"messages": messages, "stream": True}

    # tools
    tools = []
    for t in _as_list(req.get("tools")):
        if not isinstance(t, dict):
            continue
        if t.get("type") not in (None, "custom"):
            continue                      # 内置工具（web_search / bash_20250124 之类）上游没有，跳过
        name = t.get("name")
        if not name:
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": t.get("description") or "",
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    if tools:
        body["tools"] = tools

    tc = req.get("tool_choice")
    if isinstance(tc, dict):
        tctype = tc.get("type")
        if tctype == "auto":
            body["tool_choice"] = "auto"
        elif tctype == "any":
            body["tool_choice"] = "required"
        elif tctype == "tool" and tc.get("name"):
            body["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
    elif isinstance(tc, str) and tc in ("auto", "any", "none", "required"):
        body["tool_choice"] = "required" if tc == "any" else tc

    if req.get("temperature") is not None:
        body["temperature"] = req["temperature"]
    if req.get("top_p") is not None:
        body["top_p"] = req["top_p"]
    seq = req.get("stop_sequences")
    if seq:
        body["stop"] = seq
    if req.get("max_tokens") is not None:
        body["max_tokens"] = req["max_tokens"]

    meta = {"stream": bool(req.get("stream")), "model": req.get("model") or ""}
    return body, meta


def openai_to_anthropic(res, model):
    """上游聚合结果 -> Anthropic Messages 非流式响应体。"""
    content_blocks = []
    reasoning = res.get("reasoning") or ""
    text = res.get("content") or ""
    if reasoning:
        content_blocks.append({"type": "thinking", "thinking": reasoning, "signature": ""})
    if text:
        content_blocks.append({"type": "text", "text": text})
    for tc in res.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {"_raw": fn.get("arguments") or ""}
        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id") or _rid("toolu_"),
            "name": fn.get("name") or "",
            "input": args,
        })
    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})

    usage = res.get("usage") or {}
    return {
        "id": _rid("msg_"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": _map_finish_to_anthropic(res.get("finish"), bool(res.get("tool_calls"))),
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    }


def anthropic_stream_events(res, model):
    """上游聚合结果 -> Anthropic SSE 事件序列 [(event_name, data_dict), ...]"""
    usage = res.get("usage") or {}
    in_tok = int(usage.get("prompt_tokens") or 0)
    out_tok = int(usage.get("completion_tokens") or 0)
    text = res.get("content") or ""
    reasoning = res.get("reasoning") or ""
    tool_calls = res.get("tool_calls") or []
    stop_reason = _map_finish_to_anthropic(res.get("finish"), bool(tool_calls))

    ev = [("message_start", {
        "type": "message_start",
        "message": {
            "id": _rid("msg_"), "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": in_tok, "output_tokens": 0},
        },
    })]

    idx = 0
    if reasoning:
        ev.append(("content_block_start", {
            "type": "content_block_start", "index": idx,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        }))
        ev.append(("content_block_delta", {
            "type": "content_block_delta", "index": idx,
            "delta": {"type": "thinking_delta", "thinking": reasoning},
        }))
        ev.append(("content_block_stop", {"type": "content_block_stop", "index": idx}))
        idx += 1

    if text:
        ev.append(("content_block_start", {
            "type": "content_block_start", "index": idx,
            "content_block": {"type": "text", "text": ""},
        }))
        step = 512
        for i in range(0, len(text), step):
            ev.append(("content_block_delta", {
                "type": "content_block_delta", "index": idx,
                "delta": {"type": "text_delta", "text": text[i:i + step]},
            }))
        ev.append(("content_block_stop", {"type": "content_block_stop", "index": idx}))
        idx += 1

    for tc in tool_calls:
        fn = tc.get("function") or {}
        ev.append(("content_block_start", {
            "type": "content_block_start", "index": idx,
            "content_block": {"type": "tool_use", "id": tc.get("id") or _rid("toolu_"),
                              "name": fn.get("name") or "", "input": {}},
        }))
        ev.append(("content_block_delta", {
            "type": "content_block_delta", "index": idx,
            "delta": {"type": "input_json_delta", "partial_json": fn.get("arguments") or "{}"},
        }))
        ev.append(("content_block_stop", {"type": "content_block_stop", "index": idx}))
        idx += 1

    if not text and not tool_calls and not reasoning:
        ev.append(("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        }))
        ev.append(("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": ""},
        }))
        ev.append(("content_block_stop", {"type": "content_block_stop", "index": 0}))

    ev.append(("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
    }))
    ev.append(("message_stop", {"type": "message_stop"}))
    return ev


def estimate_tokens(text):
    """粗估 token：中文按 1 字≈1.6 token，英文按 4 字符≈1 token。只用于 count_tokens 应答。"""
    if not text:
        return 0
    zh = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - zh
    return int(zh * 1.6 + other / 4.0) + 1


def anthropic_count_input_tokens(req):
    total = 0
    sys_txt = req.get("system")
    if isinstance(sys_txt, str):
        total += estimate_tokens(sys_txt)
    else:
        for b in _as_list(sys_txt):
            if isinstance(b, dict) and b.get("type") == "text":
                total += estimate_tokens(b.get("text") or "")
    for m in _as_list(req.get("messages")):
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            total += estimate_tokens(c)
        else:
            for b in _as_list(c):
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    total += estimate_tokens(b.get("text") or "")
                elif b.get("type") == "tool_result":
                    cc = b.get("content")
                    if isinstance(cc, str):
                        total += estimate_tokens(cc)
                    else:
                        for x in _as_list(cc):
                            if isinstance(x, dict):
                                total += estimate_tokens(x.get("text") or "")
                elif b.get("type") == "tool_use":
                    total += estimate_tokens(_json_dumps(b.get("input") or {}))
    for t in _as_list(req.get("tools")):
        if isinstance(t, dict):
            total += estimate_tokens((t.get("name") or "") + (t.get("description") or ""))
            total += estimate_tokens(_json_dumps(t.get("input_schema") or {}))
    return max(total, 1)


# ---------------------------------------------------------------------------
# 二、OpenAI Responses  ->  OpenAI Chat Completions   （Codex CLI）
# ---------------------------------------------------------------------------
def _resp_content_to_openai(content):
    if isinstance(content, str):
        return content
    texts, parts, has_img = [], [], False
    for b in _as_list(content):
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t in ("input_text", "output_text", "text"):
            txt = b.get("text") or ""
            texts.append(txt)
            parts.append({"type": "text", "text": txt})
        elif t == "input_image":
            url = b.get("image_url")
            if isinstance(url, dict):
                url = url.get("url")
            if url:
                has_img = True
                parts.append({"type": "image_url", "image_url": {"url": url}})
    if has_img:
        return parts
    return "\n".join(texts)


def responses_to_openai(req):
    messages = []
    instr = req.get("instructions")
    if isinstance(instr, str) and instr.strip():
        messages.append({"role": "system", "content": instr})

    inp = req.get("input")
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    else:
        for item in _as_list(inp):
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue
            if not isinstance(item, dict):
                continue
            it = item.get("type")
            if it in (None, "message"):
                role = item.get("role") or "user"
                if role == "developer":
                    role = "system"
                messages.append({"role": role,
                                 "content": _resp_content_to_openai(item.get("content"))})
            elif it == "function_call":
                # 历史里的助手工具调用，归并到上一条 assistant
                call = {
                    "id": item.get("call_id") or item.get("id") or _rid("call_"),
                    "type": "function",
                    "function": {"name": item.get("name") or "",
                                 "arguments": item.get("arguments") or "{}"},
                }
                if messages and messages[-1].get("role") == "assistant":
                    messages[-1].setdefault("tool_calls", []).append(call)
                else:
                    messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
            elif it == "function_call_output":
                out = item.get("output")
                if not isinstance(out, str):
                    out = _json_dumps(out)
                messages.append({"role": "tool",
                                 "tool_call_id": item.get("call_id") or "",
                                 "content": out})
            elif it == "reasoning":
                continue          # 历史思考内容上游不需要
            else:
                continue

    body = {"messages": messages, "stream": True}

    tools = []
    for t in _as_list(req.get("tools")):
        if not isinstance(t, dict):
            continue
        if t.get("type") != "function":
            continue
        name = t.get("name") or (t.get("function") or {}).get("name")
        if not name:
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": t.get("description") or (t.get("function") or {}).get("description") or "",
                "parameters": t.get("parameters") or (t.get("function") or {}).get("parameters")
                or {"type": "object", "properties": {}},
            },
        })
    if tools:
        body["tools"] = tools

    tc = req.get("tool_choice")
    if isinstance(tc, str) and tc in ("auto", "none", "required"):
        body["tool_choice"] = tc
    elif isinstance(tc, dict) and tc.get("type") == "function" and tc.get("name"):
        body["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}

    if req.get("temperature") is not None:
        body["temperature"] = req["temperature"]
    if req.get("top_p") is not None:
        body["top_p"] = req["top_p"]
    if req.get("max_output_tokens") is not None:
        body["max_tokens"] = req["max_output_tokens"]
    if req.get("parallel_tool_calls") is not None:
        body["parallel_tool_calls"] = req["parallel_tool_calls"]

    meta = {"stream": bool(req.get("stream")), "model": req.get("model") or ""}
    return body, meta


def _resp_usage(usage):
    p = int(usage.get("prompt_tokens") or 0)
    c = int(usage.get("completion_tokens") or 0)
    t = int(usage.get("total_tokens") or (p + c))
    return {
        "input_tokens": p,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": c,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": t,
    }


def _resp_output_items(res):
    items = []
    text = res.get("content") or ""
    reasoning = res.get("reasoning") or ""
    if reasoning:
        items.append({
            "type": "reasoning",
            "id": _rid("rs_"),
            "summary": [{"type": "summary_text", "text": reasoning[:2000]}],
        })
    if text:
        items.append({
            "type": "message",
            "id": _rid("msg_"),
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        })
    for tc in res.get("tool_calls") or []:
        fn = tc.get("function") or {}
        items.append({
            "type": "function_call",
            "id": _rid("fc_"),
            "call_id": tc.get("id") or _rid("call_"),
            "name": fn.get("name") or "",
            "arguments": fn.get("arguments") or "{}",
            "status": "completed",
        })
    return items


def _resp_envelope(model, output, usage, status="completed"):
    return {
        "id": _rid("resp_"),
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": model,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": _resp_usage(usage or {}),
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "metadata": {},
        "temperature": None,
        "top_p": None,
        "max_output_tokens": None,
        "previous_response_id": None,
        "reasoning": {"effort": None, "summary": None},
        "store": False,
        "truncation": "disabled",
        "user": None,
    }


def openai_to_responses(res, model):
    usage = res.get("usage") or {}
    return _resp_envelope(model, _resp_output_items(res), usage)


def responses_stream_events(res, model):
    """上游聚合结果 -> Responses API SSE 事件序列 [(event_name, data_dict), ...]"""
    usage = res.get("usage") or {}
    text = res.get("content") or ""
    reasoning = res.get("reasoning") or ""
    tool_calls = res.get("tool_calls") or []
    env = _resp_envelope(model, [], usage, status="in_progress")

    ev = [("response.created", {"type": "response.created", "response": env, "sequence_number": 0}),
          ("response.in_progress", {"type": "response.in_progress", "response": env, "sequence_number": 1})]

    seq = 2
    out_index = 0

    if reasoning:
        rid = _rid("rs_")
        ev.append(("response.output_item.added", {
            "type": "response.output_item.added", "output_index": out_index, "sequence_number": seq,
            "item": {"type": "reasoning", "id": rid, "summary": []},
        }))
        seq += 1
        ev.append(("response.reasoning_summary_text.delta", {
            "type": "response.reasoning_summary_text.delta", "item_id": rid,
            "output_index": out_index, "summary_index": 0, "delta": reasoning,
            "sequence_number": seq,
        }))
        seq += 1
        ev.append(("response.output_item.done", {
            "type": "response.output_item.done", "output_index": out_index, "sequence_number": seq,
            "item": {"type": "reasoning", "id": rid, "summary": [{"type": "summary_text", "text": reasoning[:2000]}]},
        }))
        seq += 1
        out_index += 1

    if text:
        msg_id = _rid("msg_")
        ev.append(("response.output_item.added", {
            "type": "response.output_item.added", "output_index": out_index, "sequence_number": seq,
            "item": {"type": "message", "id": msg_id, "status": "in_progress",
                     "role": "assistant", "content": []},
        }))
        seq += 1
        ev.append(("response.content_part.added", {
            "type": "response.content_part.added", "item_id": msg_id, "output_index": out_index,
            "content_index": 0, "sequence_number": seq,
            "part": {"type": "output_text", "text": "", "annotations": []},
        }))
        seq += 1
        step = 512
        for i in range(0, len(text), step):
            ev.append(("response.output_text.delta", {
                "type": "response.output_text.delta", "item_id": msg_id, "output_index": out_index,
                "content_index": 0, "delta": text[i:i + step], "sequence_number": seq,
            }))
            seq += 1
        ev.append(("response.output_text.done", {
            "type": "response.output_text.done", "item_id": msg_id, "output_index": out_index,
            "content_index": 0, "text": text, "sequence_number": seq,
        }))
        seq += 1
        ev.append(("response.content_part.done", {
            "type": "response.content_part.done", "item_id": msg_id, "output_index": out_index,
            "content_index": 0, "sequence_number": seq,
            "part": {"type": "output_text", "text": text, "annotations": []},
        }))
        seq += 1
        ev.append(("response.output_item.done", {
            "type": "response.output_item.done", "output_index": out_index, "sequence_number": seq,
            "item": {"type": "message", "id": msg_id, "status": "completed", "role": "assistant",
                     "content": [{"type": "output_text", "text": text, "annotations": []}]},
        }))
        seq += 1
        out_index += 1

    for tc in tool_calls:
        fn = tc.get("function") or {}
        fc_id = _rid("fc_")
        seq_before = seq
        ev.append(("response.output_item.added", {
            "type": "response.output_item.added", "output_index": out_index, "sequence_number": seq_before,
            "item": {"type": "function_call", "id": fc_id,
                     "call_id": tc.get("id") or _rid("call_"), "name": fn.get("name") or "",
                     "arguments": "", "status": "in_progress"},
        }))
        seq += 1
        ev.append(("response.function_call_arguments.delta", {
            "type": "response.function_call_arguments.delta", "item_id": fc_id,
            "output_index": out_index, "delta": fn.get("arguments") or "{}", "sequence_number": seq,
        }))
        seq += 1
        ev.append(("response.function_call_arguments.done", {
            "type": "response.function_call_arguments.done", "item_id": fc_id,
            "output_index": out_index, "arguments": fn.get("arguments") or "{}", "sequence_number": seq,
        }))
        seq += 1
        ev.append(("response.output_item.done", {
            "type": "response.output_item.done", "output_index": out_index, "sequence_number": seq,
            "item": {"type": "function_call", "id": fc_id,
                     "call_id": tc.get("id") or _rid("call_"), "name": fn.get("name") or "",
                     "arguments": fn.get("arguments") or "{}", "status": "completed"},
        }))
        seq += 1
        out_index += 1

    final = _resp_envelope(model, _resp_output_items(res), usage, status="completed")
    ev.append(("response.completed", {"type": "response.completed", "response": final,
                                      "sequence_number": seq}))
    return ev
