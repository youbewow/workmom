# -*- coding: utf-8 -*-
"""
relay_stream —— 增量流式翻译器（真流式的协议侧一半）

与 relay_protocols.py 的区别：
    relay_protocols  吃的是【已聚合的完整结果】，一次性吐完整事件序列 —— 伪流式；
    relay_stream     吃的是【上游 SSE 的每个 chunk】，来一个吐一个 —— 真流式。

三个类：
    OpenAIPassthrough      OpenAI Chat 协议：上游 chunk 基本原样透传（只归一 id / model / 首包 role）
    AnthropicIncremental   Anthropic Messages：OpenAI delta -> anthropic SSE 事件增量翻译
    ResponsesIncremental   OpenAI Responses：OpenAI delta -> responses SSE 事件增量翻译

三者接口统一：
    .start_events()  -> [(event_name_or_None, dict), ...]      # 建流时立刻发
    .feed(chunk)     -> [...]                                  # 每收到一个上游 chunk
    .finish_events() -> [...]                                  # 上游流结束（或客户端断开前）
    .result()        -> 聚合结果 dict（给非流式 / 落账用）

设计约束：
  * 绝不缓存全文再吐：feed() 里产出的每个事件都只依赖当次 chunk，不等待 finish。
  * 状态机只维护"当前打开的块"和必要计数，任何时刻可被打断。
  * 不依赖网络、不依赖 CDP，纯数据变换，便于单测。
"""

import json
import time
import uuid


def _rid(prefix):
    return prefix + uuid.uuid4().hex[:24]


def _pick(delta, key):
    """delta 里取字段，兼容 None / 空串。"""
    v = delta.get(key)
    return v if isinstance(v, str) else ("" if v is None else str(v))


# ---------------------------------------------------------------------------
# 一、OpenAI Chat Completions —— 直接透传
# ---------------------------------------------------------------------------
class OpenAIPassthrough(object):
    """上游本来就是 OpenAI Chat SSE，无需翻译，只做规范化。

    规范化目的（都是客户端会踩的坑）：
      1) 首包必须带 delta.role=assistant，否则部分 SDK 认为响应非法；
      2) id 全流一致（上游偶尔每包 id 不同）；
      3) model 回填成客户端请求的模型名（客户端按自己请求的名字记账/显示）；
      4) 没 usage 的流补一个 usage 包（可选，usage_chunk=True 时）。
    """

    def __init__(self, model, chat_id=None, emit_usage=True):
        self.model = model
        self.cid = chat_id or _rid("chatcmpl-")
        self.created = int(time.time())
        self.role_sent = False
        self.emit_usage = emit_usage
        self.finish = None
        self.usage = {}
        self._content = []
        self._reasoning = []
        self._tools = {}
        self.saw_any = False

    # -- 统一接口 ----------------------------------------------------------
    def start_events(self):
        self.role_sent = True
        return [(None, {
            "id": self.cid, "object": "chat.completion.chunk", "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                         "finish_reason": None}],
        })]

    def feed(self, chunk):
        self.saw_any = True
        out = []
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for ch in (chunk.get("choices") or []):
            delta = ch.get("delta") or {}
            if delta.get("content"):
                self._content.append(delta["content"])
            if delta.get("reasoning_content"):
                self._reasoning.append(delta["reasoning_content"])
            for tc in (delta.get("tool_calls") or []):
                i = tc.get("index", 0)
                slot = self._tools.setdefault(i, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
            fr = ch.get("finish_reason")
            if fr:
                self.finish = fr

        delta_role = any((c.get("delta") or {}).get("role") for c in (chunk.get("choices") or []))
        if delta_role:
            self.role_sent = True

        # 原样转发，只改 id/model
        norm = dict(chunk)
        norm["id"] = self.cid
        norm["object"] = "chat.completion.chunk"
        norm["created"] = self.created
        norm["model"] = self.model
        out.append((None, norm))
        return out

    def finish_events(self):
        out = []
        fin = self.finish or ("tool_calls" if self._tools else "stop")
        tail = {"id": self.cid, "object": "chat.completion.chunk", "created": self.created,
                "model": self.model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": fin}]}
        if self.emit_usage and self.usage:
            tail["usage"] = self.usage
        out.append((None, tail))
        return out

    def result(self):
        tools = []
        for i in sorted(self._tools):
            s = self._tools[i]
            tools.append({"id": s["id"] or _rid("call_"), "type": "function",
                          "function": {"name": s["name"], "arguments": s["arguments"] or "{}"}})
        return {"id": self.cid, "model": self.model, "content": "".join(self._content),
                "reasoning": "".join(self._reasoning), "tool_calls": tools,
                "finish": self.finish or ("tool_calls" if tools else "stop"),
                "usage": self.usage or {"prompt_tokens": 0, "completion_tokens": 0,
                                        "total_tokens": 0}}


# ---------------------------------------------------------------------------
# 二、Anthropic Messages
# ---------------------------------------------------------------------------
class AnthropicIncremental(object):
    """OpenAI chunk -> Anthropic Messages SSE。

    事件顺序（Anthropic 规范）：
        message_start
        [content_block_start -> content_block_delta* -> content_block_stop] * N
        message_delta
        message_stop

    块类型映射：
        reasoning_content -> thinking      (thinking_delta)
        content           -> text          (text_delta)
        tool_calls        -> tool_use      (input_json_delta)
    """

    def __init__(self, model, msg_id=None):
        self.model = model
        self.msg_id = msg_id or _rid("msg_")
        self.started = False
        self.next_index = 0
        self.cur = None            # {'index','kind','id','name'} 当前打开块
        self.tool_blocks = {}      # upstream tool index -> block info
        self.finish = None
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self._content = []
        self._reasoning = []
        self._tools = {}

    # -- 内部 --------------------------------------------------------------
    def _open_block(self, kind, **kw):
        idx = self.next_index
        self.next_index += 1
        b = {"index": idx, "kind": kind}
        b.update(kw)
        self.cur = b
        ev = []
        if kind == "thinking":
            ev.append(("content_block_start", {
                "type": "content_block_start", "index": idx,
                "content_block": {"type": "thinking", "thinking": ""}}))
        elif kind == "text":
            ev.append(("content_block_start", {
                "type": "content_block_start", "index": idx,
                "content_block": {"type": "text", "text": ""}}))
        elif kind == "tool_use":
            ev.append(("content_block_start", {
                "type": "content_block_start", "index": idx,
                "content_block": {"type": "tool_use", "id": b.get("id") or _rid("toolu_"),
                                  "name": b.get("name") or "", "input": {}}}))
        return ev

    def _close_block(self):
        if not self.cur:
            return []
        idx = self.cur["index"]
        self.cur = None
        return [("content_block_stop", {"type": "content_block_stop", "index": idx})]

    # -- 统一接口 ----------------------------------------------------------
    def start_events(self):
        self.started = True
        return [("message_start", {
            "type": "message_start",
            "message": {
                "id": self.msg_id, "type": "message", "role": "assistant",
                "content": [], "model": self.model,
                "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }}),
            ("ping", {"type": "ping"})]

    def feed(self, chunk):
        ev = []
        if chunk.get("usage"):
            u = chunk["usage"]
            self.usage["input_tokens"] = int(u.get("prompt_tokens") or self.usage["input_tokens"] or 0)
            self.usage["output_tokens"] = int(u.get("completion_tokens") or self.usage["output_tokens"] or 0)

        for ch in (chunk.get("choices") or []):
            delta = ch.get("delta") or {}

            # 1) 思考
            rc = _pick(delta, "reasoning_content")
            if rc:
                self._reasoning.append(rc)
                if not self.cur or self.cur["kind"] != "thinking":
                    ev.extend(self._close_block())
                    ev.extend(self._open_block("thinking"))
                ev.append(("content_block_delta", {
                    "type": "content_block_delta", "index": self.cur["index"],
                    "delta": {"type": "thinking_delta", "thinking": rc}}))

            # 2) 正文
            txt = _pick(delta, "content")
            if txt:
                self._content.append(txt)
                if not self.cur or self.cur["kind"] != "text":
                    ev.extend(self._close_block())
                    ev.extend(self._open_block("text"))
                ev.append(("content_block_delta", {
                    "type": "content_block_delta", "index": self.cur["index"],
                    "delta": {"type": "text_delta", "text": txt}}))

            # 3) 工具调用
            for tc in (delta.get("tool_calls") or []):
                ui = tc.get("index", 0)
                slot = self._tools.setdefault(ui, {"id": "", "name": "", "arguments": ""})
                fn = tc.get("function") or {}
                if tc.get("id"):
                    slot["id"] = tc["id"]
                if fn.get("name"):
                    slot["name"] = fn["name"]

                if ui not in self.tool_blocks:
                    ev.extend(self._close_block())
                    info = {"id": slot["id"] or _rid("toolu_"), "name": slot["name"]}
                    ev.extend(self._open_block("tool_use", **info))
                    self.tool_blocks[ui] = {"index": self.cur["index"],
                                            "id": info["id"], "name": info["name"]}
                elif self.cur is None or self.cur.get("kind") != "tool_use" \
                        or self.cur["index"] != self.tool_blocks[ui]["index"]:
                    # 回头又来一个 tool 的增量：重开该块（Anthropic 允许同 index 只开一次，
                    # 这里属于上游 tool_calls index 乱序，按新块处理以免索引冲突）
                    ev.extend(self._close_block())
                    info = {"id": slot["id"] or _rid("toolu_"), "name": slot["name"]}
                    ev.extend(self._open_block("tool_use", **info))
                    self.tool_blocks[ui] = {"index": self.cur["index"],
                                            "id": info["id"], "name": info["name"]}

                args = fn.get("arguments")
                if args:
                    slot["arguments"] += args
                    ev.append(("content_block_delta", {
                        "type": "content_block_delta", "index": self.tool_blocks[ui]["index"],
                        "delta": {"type": "input_json_delta", "partial_json": args}}))

            fr = ch.get("finish_reason")
            if fr:
                self.finish = fr
        return ev

    def finish_events(self):
        ev = list(self._close_block())
        stop = "end_turn"
        if self._tools or self.finish == "tool_calls":
            stop = "tool_use"
        elif self.finish == "length":
            stop = "max_tokens"
        elif self.finish == "content_filter":
            stop = "refusal"

        if not self.usage["output_tokens"]:
            # 上游没给 usage 时用字符数粗估，避免客户端显示 0 触发配额误判
            self.usage["output_tokens"] = max(1, int(len("".join(self._content)) * 0.75))
        ev.append(("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": self.usage["output_tokens"],
                      "input_tokens": self.usage["input_tokens"]}}))
        ev.append(("message_stop", {"type": "message_stop"}))
        return ev

    def result(self):
        tools = []
        for i in sorted(self._tools):
            s = self._tools[i]
            try:
                parsed = json.loads(s["arguments"] or "{}")
            except Exception:
                parsed = {"_raw": s["arguments"]}
            tools.append({"id": s["id"] or _rid("call_"), "type": "function",
                          "function": {"name": s["name"], "arguments": s["arguments"] or "{}"},
                          "_parsed": parsed})
        return {"id": self.msg_id, "model": self.model, "content": "".join(self._content),
                "reasoning": "".join(self._reasoning), "tool_calls": tools,
                "finish": self.finish or ("tool_calls" if tools else "stop"),
                "usage": {"prompt_tokens": self.usage["input_tokens"],
                          "completion_tokens": self.usage["output_tokens"],
                          "total_tokens": self.usage["input_tokens"] + self.usage["output_tokens"]}}


# ---------------------------------------------------------------------------
# 三、OpenAI Responses
# ---------------------------------------------------------------------------
class ResponsesIncremental(object):
    """OpenAI chunk -> Responses API SSE。

    Responses 的事件粒度比 Anthropic 细：文本要 output_item.added ->
    content_part.added -> output_text.delta* -> output_text.done ->
    content_part.done -> output_item.done，缺一步客户端就报状态错。
    """

    def __init__(self, model, resp_id=None):
        self.model = model
        self.resp_id = resp_id or _rid("resp_")
        self.created = int(time.time())
        self.seq = 0
        self.out_index = 0
        self.finish = None
        self.usage = {}
        self._content = []
        self._reasoning = []
        self._tools = {}

        self.reason_item = None     # {'id','out_index','open'}
        self.text_item = None
        self.tool_items = {}        # upstream idx -> {'id','out_index','call_id','name','args'}
        self.done_items = []        # 已完成的 output item（给 response.completed）

    def _n(self):
        self.seq += 1
        return self.seq - 1

    # -- 统一接口 ----------------------------------------------------------
    def start_events(self):
        env = self._envelope([], "in_progress")
        return [("response.created", {"type": "response.created", "response": env,
                                      "sequence_number": self._n()}),
                ("response.in_progress", {"type": "response.in_progress", "response": env,
                                          "sequence_number": self._n()})]

    def feed(self, chunk):
        ev = []
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for ch in (chunk.get("choices") or []):
            delta = ch.get("delta") or {}

            rc = _pick(delta, "reasoning_content")
            if rc:
                self._reasoning.append(rc)
                if self.reason_item is None:
                    rid = _rid("rs_")
                    self.reason_item = {"id": rid, "out_index": self.out_index}
                    self.out_index += 1
                    ev.append(("response.output_item.added", {
                        "type": "response.output_item.added",
                        "output_index": self.reason_item["out_index"],
                        "sequence_number": self._n(),
                        "item": {"type": "reasoning", "id": rid, "summary": []}}))
                ev.append(("response.reasoning_summary_text.delta", {
                    "type": "response.reasoning_summary_text.delta",
                    "item_id": self.reason_item["id"],
                    "output_index": self.reason_item["out_index"],
                    "summary_index": 0, "delta": rc, "sequence_number": self._n()}))

            txt = _pick(delta, "content")
            if txt:
                self._content.append(txt)
                if self.text_item is None:
                    mid = _rid("msg_")
                    self.text_item = {"id": mid, "out_index": self.out_index}
                    self.out_index += 1
                    ev.append(("response.output_item.added", {
                        "type": "response.output_item.added",
                        "output_index": self.text_item["out_index"],
                        "sequence_number": self._n(),
                        "item": {"type": "message", "id": mid, "status": "in_progress",
                                 "role": "assistant", "content": []}}))
                    ev.append(("response.content_part.added", {
                        "type": "response.content_part.added", "item_id": mid,
                        "output_index": self.text_item["out_index"],
                        "content_index": 0, "sequence_number": self._n(),
                        "part": {"type": "output_text", "text": "", "annotations": []}}))
                ev.append(("response.output_text.delta", {
                    "type": "response.output_text.delta", "item_id": self.text_item["id"],
                    "output_index": self.text_item["out_index"],
                    "content_index": 0, "delta": txt, "sequence_number": self._n()}))

            for tc in (delta.get("tool_calls") or []):
                ui = tc.get("index", 0)
                fn = tc.get("function") or {}
                it = self.tool_items.get(ui)
                if it is None:
                    it = {"id": _rid("fc_"), "call_id": tc.get("id") or _rid("call_"),
                          "name": fn.get("name") or "", "args": "",
                          "out_index": self.out_index}
                    self.out_index += 1
                    self.tool_items[ui] = it
                    ev.append(("response.output_item.added", {
                        "type": "response.output_item.added", "output_index": it["out_index"],
                        "sequence_number": self._n(),
                        "item": {"type": "function_call", "id": it["id"],
                                 "call_id": it["call_id"], "name": it["name"],
                                 "arguments": "", "status": "in_progress"}}))
                else:
                    if tc.get("id"):
                        it["call_id"] = tc["id"]
                    if fn.get("name") and not it["name"]:
                        it["name"] = fn["name"]
                args = fn.get("arguments")
                if args:
                    it["args"] += args
                    ev.append(("response.function_call_arguments.delta", {
                        "type": "response.function_call_arguments.delta", "item_id": it["id"],
                        "output_index": it["out_index"], "delta": args,
                        "sequence_number": self._n()}))

            fr = ch.get("finish_reason")
            if fr:
                self.finish = fr
        return ev

    def _close_reasoning(self):
        if not self.reason_item:
            return []
        txt = "".join(self._reasoning)
        item = {"type": "reasoning", "id": self.reason_item["id"],
                "summary": [{"type": "summary_text", "text": txt[:4000]}] if txt else []}
        self.done_items.append(item)
        ev = [("response.reasoning_summary_text.done", {
                  "type": "response.reasoning_summary_text.done",
                  "item_id": self.reason_item["id"],
                  "output_index": self.reason_item["out_index"],
                  "summary_index": 0, "text": txt, "sequence_number": self._n()}),
              ("response.output_item.done", {
                  "type": "response.output_item.done",
                  "output_index": self.reason_item["out_index"],
                  "sequence_number": self._n(), "item": item})]
        self.reason_item = None
        return ev

    def _close_text(self):
        if not self.text_item:
            return []
        txt = "".join(self._content)
        item = {"type": "message", "id": self.text_item["id"], "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": txt, "annotations": []}]}
        self.done_items.append(item)
        ev = [("response.output_text.done", {
                  "type": "response.output_text.done", "item_id": self.text_item["id"],
                  "output_index": self.text_item["out_index"], "content_index": 0,
                  "text": txt, "sequence_number": self._n()}),
              ("response.content_part.done", {
                  "type": "response.content_part.done", "item_id": self.text_item["id"],
                  "output_index": self.text_item["out_index"], "content_index": 0,
                  "sequence_number": self._n(),
                  "part": {"type": "output_text", "text": txt, "annotations": []}}),
              ("response.output_item.done", {
                  "type": "response.output_item.done",
                  "output_index": self.text_item["out_index"],
                  "sequence_number": self._n(), "item": item})]
        self.text_item = None
        return ev

    def _close_tools(self):
        ev = []
        for ui in sorted(self.tool_items):
            it = self.tool_items[ui]
            if it.get("done"):
                continue
            it["done"] = True
            args = it["args"] or "{}"
            item = {"type": "function_call", "id": it["id"], "call_id": it["call_id"],
                    "name": it["name"], "arguments": args, "status": "completed"}
            self.done_items.append(item)
            ev.append(("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done", "item_id": it["id"],
                "output_index": it["out_index"], "arguments": args,
                "sequence_number": self._n()}))
            ev.append(("response.output_item.done", {
                "type": "response.output_item.done", "output_index": it["out_index"],
                "sequence_number": self._n(), "item": item}))
        return ev

    def finish_events(self):
        ev = []
        ev.extend(self._close_reasoning())
        ev.extend(self._close_text())
        ev.extend(self._close_tools())
        ev.append(("response.completed", {
            "type": "response.completed",
            "response": self._envelope(self.done_items, "completed"),
            "sequence_number": self._n()}))
        return ev

    def _envelope(self, output, status):
        u = self.usage or {}
        p = int(u.get("prompt_tokens") or 0)
        c = int(u.get("completion_tokens") or 0)
        return {
            "id": self.resp_id, "object": "response", "created_at": self.created,
            "status": status, "model": self.model, "output": output,
            "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
            "usage": {"input_tokens": p, "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens": c, "output_tokens_details": {"reasoning_tokens": 0},
                      "total_tokens": int(u.get("total_tokens") or (p + c))},
            "error": None, "incomplete_details": None, "instructions": None,
            "metadata": {}, "temperature": None, "top_p": None, "max_output_tokens": None,
            "previous_response_id": None,
            "reasoning": {"effort": None, "summary": None},
            "store": False, "truncation": "disabled", "user": None,
        }

    def result(self):
        tools = [{"id": it["call_id"], "type": "function",
                  "function": {"name": it["name"], "arguments": it["args"] or "{}"}}
                 for it in sorted(self.tool_items.values(), key=lambda x: x["out_index"])]
        return {"id": self.resp_id, "model": self.model,
                "content": "".join(self._content),
                "reasoning": "".join(self._reasoning), "tool_calls": tools,
                "finish": self.finish or ("tool_calls" if tools else "stop"),
                "usage": self.usage or {"prompt_tokens": 0, "completion_tokens": 0,
                                        "total_tokens": 0}}


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def make(protocol, model):
    """protocol: 'openai' | 'anthropic' | 'responses'"""
    if protocol == "anthropic":
        return AnthropicIncremental(model)
    if protocol == "responses":
        return ResponsesIncremental(model)
    return OpenAIPassthrough(model)


def sse_bytes(event_name, obj):
    """统一 SSE 序列化：有 event 名就带 event 行（anthropic / responses 用）。"""
    body = json.dumps(obj, ensure_ascii=False)
    if event_name:
        return ("event: %s\ndata: %s\n\n" % (event_name, body)).encode("utf-8")
    return ("data: %s\n\n" % body).encode("utf-8")
