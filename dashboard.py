# -*- coding: utf-8 -*-
"""
workmom —— WorkBuddy 账号 / 模型 总控面板

功能（全部自研，不依赖 WorkDaddy 桌面 UI）：
  · 无感登录 WorkBuddy 账号：面板里点「+ 无感登录新号」，浏览器授权完自动入库
  · 无感切换账号：双击账号行，写登录文件 + CDP 刷新页面，不重启 WorkBuddy
  · 自动领积分：启动时自动补领今日积分，也可一键领全部 / 双击单号领
  · 模型切换：双击模型行即切模型，与账号完全解耦
        —— 模型状态存 data/model_state.json，切账号不动它
        —— 账号状态是 WB 登录文件，切模型不动它
        结果：选完模型，切哪个号都是这个模型；选完账号，换哪个模型都还在这个号

数据源：WorkDaddy 的 account-credit-cache.json / credit-usage.db（只读复用）+
        rate_limit_ledger.json + workmom 反代 /relay_state + 自己的 data/*.json
"""
import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import tkinter as tk
from tkinter import ttk

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import workmom_core as WM          # 账号 / 密钥 / 切号 / 签到 / 登录 / 模型状态

WD = WM.cfg().get("wd_dir") or ""     # WorkDaddy 目录（可选），留空则该部分数据不展示
CREDIT_CACHE = os.path.join(WD, "data", "account-credit-cache.json") if WD else ""
USAGE_DB = os.path.join(WD, "data", "credit-usage.db") if WD else ""
LEDGER = os.path.join(BASE, "rate_limit_ledger.json")
STATE = os.path.join(BASE, "guard_state.json")   # 与 workmom_guard.py 的 STATE_FILE 保持一致
PORT = int(WM.cfg().get("relay_port") or 18793)
RELAY = "http://127.0.0.1:%d/relay_state" % PORT

ACCOUNTS = []          # [(uid, 显示名)]
NAME = {}


def display_name(rec):
    nm = (rec.get("nickname") or "").strip()
    if nm:
        return nm
    ph = (rec.get("phone") or "").strip()
    if ph:
        return "***" + ph[-4:] if len(ph) >= 4 else ph
    return (rec.get("uid") or "")[:8]


def sync_accounts():
    """从 workmom 自己的账号库（accounts/*.info）拉列表，昵称是解密出来的真名。"""
    global ACCOUNTS, NAME
    rows = WM.list_accounts()
    ACCOUNTS = [(r["uid"], display_name(r)) for r in rows]
    NAME = {u: n for u, n in ACCOUNTS}
    return len(ACCOUNTS)


BG = "#181825"
PANEL = "#1e1e2e"
HEAD = "#313244"
FG = "#cdd6f4"
MUTED = "#7f849c"
GREEN = "#a6e3a1"
RED = "#f38ba8"
YELLOW = "#f9e2af"
BLUE = "#89b4fa"
ACCENT = "#cba6f7"
CYAN = "#94e2d5"


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def load_credits():
    d = read_json(CREDIT_CACHE, {})
    return {u: (v.get("credits") if isinstance(v, dict) else None) for u, v in d.items()}


def load_usage():
    """uid -> {total, count, models:{model:[credit,count]}}"""
    out = {}
    if not os.path.exists(USAGE_DB):
        return out
    try:
        con = sqlite3.connect("file:%s?mode=ro" % USAGE_DB.replace("\\", "/"), uri=True)
        rows = con.execute(
            "select uid, model, sum(credit), count(*) from credit_usage_records "
            "group by uid, model").fetchall()
        con.close()
    except Exception:
        return out
    for uid, model, credit, cnt in rows:
        rec = out.setdefault(uid, {"total": 0.0, "count": 0, "models": {}})
        credit = credit or 0
        rec["total"] += credit
        rec["count"] += cnt
        rec["models"][model or "-"] = [credit, cnt]
    return out


def load_relay():
    try:
        req = urllib.request.Request(RELAY, headers={"Accept": "application/json"})
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(req, timeout=2) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return {}


def port_alive(port=PORT):
    import socket
    s = socket.socket()
    s.settimeout(0.6)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def fmt_credits(v):
    if v is None:
        return "—"
    return "{:,.2f}".format(v)


def fmt_left(clear_at):
    if not clear_at:
        return "—"
    left = clear_at - time.time()
    if left <= 0:
        return "已解禁"
    h, r = divmod(int(left), 3600)
    m, s = divmod(r, 60)
    if h:
        return "剩 %d时%02d分" % (h, m)
    return "剩 %d分%02d秒" % (m, s)


def fmt_exp(exp_ms):
    """accessToken 过期时间 → 本地时间文本。"""
    try:
        n = float(exp_ms)
    except Exception:
        return "—"
    if n <= 0:
        return "—"
    if n > 1e11:
        n = n / 1000.0
    if n < time.time() - 86400:
        return "已过期"
    return time.strftime("%m-%d %H:%M", time.localtime(n))


class App:
    def __init__(self, root):
        self.root = root
        self.auto = tk.BooleanVar(value=True)
        self.auto_claim = tk.BooleanVar(value=True)
        # 双击切号相关
        self.busy = False                 # 正在切号，禁止并发
        self.busy_model = False           # 正在切模型
        self.pending_force = None         # （uid, ts）：双击限流号时的强制确认窗口
        self.msgq = queue.Queue()         # 工作线程 → UI 线程的消息队列
        self.rowmap = {}
        self.mmodel = {}                  # 模型表 iid -> model

        root.title("%s · WorkBuddy 账号与模型总控" % WM.APP_NAME)
        root.geometry("1240x780")
        root.configure(bg=BG)
        root.minsize(1020, 640)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=FG, rowheight=27, borderwidth=0, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", background=HEAD, foreground=FG,
                        relief="flat", font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Treeview.Heading", background=[("active", "#45475a")])
        style.map("Treeview", background=[("selected", "#45475a")], foreground=[("selected", FG)])

        # ---------- 顶部 ----------
        top = tk.Frame(root, bg=BG)
        top.pack(fill="x", padx=16, pady=(14, 6))

        tk.Label(top, text=WM.APP_NAME, bg=BG, fg=ACCENT,
                 font=("Microsoft YaHei UI", 16, "bold")).pack(side="left")
        tk.Label(top, text="账号 · 模型 · 积分", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(8, 0))

        self.lbl_now = tk.Label(top, text="", bg=BG, fg=GREEN,
                                font=("Microsoft YaHei UI", 10, "bold"))
        self.lbl_now.pack(side="left", padx=(18, 0))
        self.lbl_model = tk.Label(top, text="", bg=BG, fg=BLUE,
                                  font=("Microsoft YaHei UI", 10, "bold"))
        self.lbl_model.pack(side="left", padx=(14, 0))

        def mkbtn(parent, text, cmd, color=FG, pad=12):
            b = tk.Button(parent, text=text, command=cmd, bg=HEAD, fg=color,
                          activebackground="#45475a", activeforeground=color, relief="flat",
                          font=("Microsoft YaHei UI", 9), padx=pad, pady=3, cursor="hand2")
            return b

        mkbtn(top, "立即刷新", self.refresh_all).pack(side="right")
        mkbtn(top, "重启 WB(兜底)", self.restart_wb_manual, YELLOW, 10).pack(side="right", padx=(0, 8))

        tk.Checkbutton(top, text="自动刷新(5s)", variable=self.auto, bg=BG, fg=MUTED,
                       selectcolor=PANEL, activebackground=BG, activeforeground=FG,
                       font=("Microsoft YaHei UI", 9)).pack(side="right", padx=(0, 10))

        self.on_top = tk.BooleanVar(value=False)
        tk.Checkbutton(top, text="窗口置顶", variable=self.on_top, command=self._toggle_top,
                       bg=BG, fg=MUTED, selectcolor=PANEL, activebackground=BG,
                       activeforeground=FG, font=("Microsoft YaHei UI", 9)).pack(side="right", padx=(0, 10))

        # 第二行按钮：登录 / 签到
        top2 = tk.Frame(root, bg=BG)
        top2.pack(fill="x", padx=16, pady=(0, 8))
        self.btn_login = mkbtn(top2, "+ 无感登录新号", self.start_oauth, CYAN, 12)
        self.btn_login.pack(side="left")
        self.btn_claim = mkbtn(top2, "一键领全部积分", self.claim_all_accounts, GREEN, 12)
        self.btn_claim.pack(side="left", padx=(8, 0))
        tk.Checkbutton(top2, text="启动时自动领今日积分", variable=self.auto_claim, bg=BG, fg=MUTED,
                       selectcolor=PANEL, activebackground=BG, activeforeground=FG,
                       font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(12, 0))
        self.lbl_hint = tk.Label(top2, text="双击账号行=无感切号　双击模型行=切模型（两者互不影响）",
                                 bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 9))
        self.lbl_hint.pack(side="right")

        # ---------- 统计条 ----------
        stat = tk.Frame(root, bg=PANEL)
        stat.pack(fill="x", padx=16, pady=(0, 10))
        self.stat_labels = {}
        for key, text in [("total", "总余额"), ("ok", "可用账号"), ("limited", "限流中"),
                          ("usage", "累计消耗 credits"), ("req", "总请求数"),
                          ("claim", "今日签到"), ("relay", "反代")]:
            box = tk.Frame(stat, bg=PANEL)
            box.pack(side="left", padx=(16, 18), pady=10)
            tk.Label(box, text=text, bg=PANEL, fg=MUTED,
                     font=("Microsoft YaHei UI", 8)).pack(anchor="w")
            lb = tk.Label(box, text="—", bg=PANEL, fg=FG,
                          font=("Microsoft YaHei UI", 12, "bold"))
            lb.pack(anchor="w")
            self.stat_labels[key] = lb

        # ---------- 账号表 ----------
        tk.Label(root, text="账号额度与限流状态　（双击某一行 = 无感切到那个号，不重启 WorkBuddy）",
                 bg=BG, fg=FG, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=16, pady=(2, 4))

        wrap1 = tk.Frame(root, bg=BG)
        wrap1.pack(fill="both", expand=True, padx=16)
        cols1 = ("name", "credits", "used", "req", "models", "flow", "state", "left",
                 "checkin", "expire", "checked")
        heads1 = ("账号", "余额 credits", "累计消耗", "请求数", "用过模型", "占比", "pro 状态",
                  "限流倒计时", "今日签到", "凭证到期", "上次检测")
        widths1 = (128, 108, 92, 66, 74, 60, 84, 100, 78, 92, 78)
        anchors1 = ("w", "e", "e", "e", "center", "e", "center", "center", "center", "center", "center")
        self.t1 = ttk.Treeview(wrap1, columns=cols1, show="headings", height=8)
        for c, h, w, a in zip(cols1, heads1, widths1, anchors1):
            self.t1.heading(c, text=h)
            self.t1.column(c, width=w, anchor=a)
        sb1 = ttk.Scrollbar(wrap1, orient="vertical", command=self.t1.yview)
        self.t1.configure(yscrollcommand=sb1.set)
        self.t1.pack(side="left", fill="both", expand=True)
        sb1.pack(side="right", fill="y")
        self.t1.tag_configure("ok", foreground=GREEN)
        self.t1.tag_configure("limited", foreground=RED)
        self.t1.tag_configure("now", background="#313244")

        # ---------- 模型表 ----------
        tk.Label(root, text="模型使用情况　（双击某一行 = 把全部请求切到这个模型，跟换账号完全无关）",
                 bg=BG, fg=FG, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=16, pady=(12, 4))

        wrap2 = tk.Frame(root, bg=BG)
        wrap2.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        cols2 = ("state", "model", "accounts", "who", "used", "req")
        heads2 = ("当前", "模型", "使用账号数", "哪些账号用过", "累计消耗", "请求数")
        widths2 = (60, 190, 92, 470, 110, 80)
        anchors2 = ("center", "w", "center", "w", "e", "e")
        self.t2 = ttk.Treeview(wrap2, columns=cols2, show="headings", height=7)
        for c, h, w, a in zip(cols2, heads2, widths2, anchors2):
            self.t2.heading(c, text=h)
            self.t2.column(c, width=w, anchor=a)
        sb2 = ttk.Scrollbar(wrap2, orient="vertical", command=self.t2.yview)
        self.t2.configure(yscrollcommand=sb2.set)
        self.t2.pack(side="left", fill="both", expand=True)
        sb2.pack(side="right", fill="y")
        self.t2.tag_configure("pro", foreground=BLUE)
        self.t2.tag_configure("cur", background="#313244", foreground=CYAN)

        # ---------- 状态条 ----------
        self.switch_bar = tk.Label(
            root, text="双击账号行=无感切号（写登录文件+CDP刷新页面）　双击模型行=切模型（持久化，跨账号保持不变）",
            bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 9), anchor="w",
            padx=10, pady=5)
        self.switch_bar.pack(fill="x", padx=16, pady=(0, 6))

        self.t1.bind("<Double-1>", self.on_double_click)
        self.t2.bind("<Double-1>", self.on_double_click_model)

        self.foot = tk.Label(root, text="", bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 8))
        self.foot.pack(anchor="w", padx=16, pady=(0, 10))

        self.cache = {}
        self.refresh_all()
        self.tick()
        self.root.after(2500, self._auto_claim_once)

    # ------------------------------------------------------------------ 刷新
    def refresh_all(self):
        sync_accounts()
        credits = load_credits()
        usage = load_usage()
        ledger = read_json(LEDGER, {})
        relay = load_relay()
        cur_uid = WM.current_uid()
        cur_model = WM.active_model()

        self.t1.delete(*self.t1.get_children())
        self.rowmap = {}
        total_credits = 0.0
        n_ok = n_lim = 0
        n_claimed = 0

        rows = []
        for uid, name in ACCOUNTS:
            cr = credits.get(uid)
            u = usage.get(uid, {"total": 0.0, "count": 0, "models": {}})
            led = ledger.get(uid, {})
            status = led.get("status", "unknown")
            clear_at = led.get("clear_at") or 0
            clear_text = led.get("clear_text") or "—"
            checked = led.get("checked_at") or 0
            checked_txt = time.strftime("%H:%M:%S", time.localtime(checked)) if checked else "—"
            ck = WM.checkin_state(uid)
            if ck.get("ok"):
                n_claimed += 1
            if cr:
                total_credits += cr
            if status == "ok":
                n_ok += 1
                st_txt = "可用"
            elif status == "limited":
                if clear_at and clear_at <= time.time():
                    st_txt = "待复测"
                else:
                    n_lim += 1
                    st_txt = "限流中"
            else:
                st_txt = "未测"
            rows.append((u["total"], uid, name, cr, u, st_txt, clear_at,
                         checked_txt, status, ck))

        rows.sort(key=lambda x: -x[0])
        for total, uid, name, cr, u, st_txt, clear_at, checked_txt, status, ck in rows:
            share = ""
            if cr and cr > 0:
                share = "%.0f%%" % (total / (total + cr) * 100) if (total + cr) else ""
            tag = "limited" if st_txt == "限流中" else ("ok" if st_txt == "可用" else "")
            tags = [tag]
            if uid == cur_uid:
                tags.append("now")
                name = "▶ " + name
            iid = self.t1.insert("", "end", values=(
                name, fmt_credits(cr), fmt_credits(total) if total else "0.00",
                u["count"] if u["count"] else "未用过",
                ("%d 个" % len(u["models"])) if u["models"] else "未用过", share, st_txt,
                fmt_left(clear_at) if st_txt == "限流中" else "—",
                ck.get("text") or "-",
                fmt_exp(WM.account_summary(uid).get("expiresAt")),
                checked_txt), tags=tuple(tags))
            self.rowmap[iid] = uid

        # 模型汇总（把 known_models 全列出来，没用过的也列，方便直接双击切）
        self.t2.delete(*self.t2.get_children())
        self.mmodel = {}
        models = {}
        for uid, rec in usage.items():
            for m, (credit, cnt) in rec["models"].items():
                mm = models.setdefault(m, {"credit": 0.0, "count": 0, "who": []})
                mm["credit"] += credit
                mm["count"] += cnt
                mm["who"].append(NAME.get(uid, uid[:8]))
        grand_req = sum(v["count"] for v in usage.values())
        grand_used = sum(v["total"] for v in usage.values())

        order = [cur_model] + [m for m in WM.known_models() if m != cur_model]
        for m in list(models.keys()):
            if m not in order:
                order.append(m)
        for m in order:
            v = models.get(m)
            is_cur = (m == cur_model)
            if v:
                who = "、".join(v["who"][:8]) + ("…" if len(v["who"]) > 8 else "")
                vals = ("▶" if is_cur else "", m, "%d / %d" % (len(v["who"]), len(ACCOUNTS)),
                        who, fmt_credits(v["credit"]), v["count"])
            else:
                vals = ("▶" if is_cur else "", m, "0 / %d" % len(ACCOUNTS), "没人用过", "—", "0")
            tags = ["cur"] if is_cur else (["pro"] if "kimi" in m else [])
            iid = self.t2.insert("", "end", values=vals, tags=tuple(tags))
            self.mmodel[iid] = m

        # 统计条
        self.stat_labels["total"].config(text=fmt_credits(total_credits))
        self.stat_labels["ok"].config(text="%d / %d" % (n_ok, len(ACCOUNTS)), fg=GREEN)
        self.stat_labels["limited"].config(text="%d 个" % n_lim, fg=RED if n_lim else MUTED)
        self.stat_labels["usage"].config(text=fmt_credits(grand_used))
        self.stat_labels["req"].config(text=str(grand_req))
        self.stat_labels["claim"].config(
            text="%d / %d" % (n_claimed, len(ACCOUNTS)),
            fg=GREEN if n_claimed else MUTED)

        if cur_uid:
            nm = NAME.get(cur_uid) or WM.current_account().get("nickname") or cur_uid[:8]
            self.stat_labels["relay"].config(text="运行中", fg=GREEN if port_alive() else RED)
            self.lbl_now.config(text="在用账号：%s" % nm, fg=GREEN)
        else:
            self.stat_labels["relay"].config(text="未运行", fg=RED)
            self.lbl_now.config(text="没读到登录态", fg=RED)
        self.lbl_model.config(text="当前模型：%s" % cur_model, fg=BLUE)

        self.cache = {"ledger": ledger, "relay": relay, "usage": usage,
                      "credits": credits, "model": cur_model}
        self.foot.config(
            text="反代 127.0.0.1:%d · 账号库 %s（%d 个）· 模型状态 %s · 更新于 %s"
                 % (PORT, WM.ACCOUNTS_DIR, len(ACCOUNTS), WM.MODEL_FILE,
                    time.strftime("%H:%M:%S")))

    # ---------------------------------------------------------------- 双击切号
    def set_bar(self, text, color=MUTED):
        try:
            self.switch_bar.config(text=text, fg=color)
        except Exception:
            pass

    def _uid_of_row(self, iid):
        try:
            vals = self.t1.item(iid, "values")
        except Exception:
            return None, None
        if not vals:
            return None, None
        return (getattr(self, "rowmap", {}).get(iid), str(vals[0]).replace("▶ ", "").strip())

    def _start_busy(self, what, text, color=YELLOW):
        self.busy = True
        self.set_bar(text, color)
        try:
            self.lbl_hint.config(text=what)
        except Exception:
            pass

    def on_double_click(self, event):
        iid = self.t1.identify_row(event.y)
        if not iid:
            return
        uid, name = self._uid_of_row(iid)
        if not uid:
            return
        if self.busy:
            self.set_bar("正在切号中，等这次跑完再点……", YELLOW)
            return
        if uid == WM.current_uid():
            self.set_bar("现在用的就是 %s，已经在这个号上了。" % name, GREEN)
            return
        led = ((self.cache.get("ledger") or {}).get(uid) or {})
        clear_at = led.get("clear_at") or 0
        if led.get("status") == "limited" and clear_at > time.time():
            pf = self.pending_force
            if pf and pf[0] == uid and time.time() - pf[1] < 5:
                self.pending_force = None
                self.start_switch(uid, name, force=True)
            else:
                self.pending_force = (uid, time.time())
                self.set_bar("%s 本地记录限流到 %s，切过去多半也是 6004。真要用就 5 秒内再双击这一行。"
                             % (name, time.strftime("%m-%d %H:%M:%S", time.localtime(clear_at))), YELLOW)
            return
        self.pending_force = None
        self.start_switch(uid, name)

    def start_switch(self, uid, name, force=False):
        self._start_busy("切号中…",
                         "→ 无感切到 %s%s（写登录文件 + CDP 刷新页面，不重启 WorkBuddy）"
                         % (name, "（强制）" if force else ""))
        threading.Thread(target=self._switch_worker, args=(uid, name, force), daemon=True).start()

    def _switch_worker(self, uid, name, force):
        def bar(t, c=YELLOW):
            self.msgq.put(("bar", t, c))
        try:
            bar("1/3 备份当前登录态 → 写 %s 的登录文件 ……" % name)
            ok, info = WM.seamless_switch(uid)
            if not ok:
                self.msgq.put(("done", "无感切号没成：%s —— 想走老路就点右上「重启 WB(兜底)」"
                               % str(info.get("error") or info)[:150], RED))
                return
            bar("2/3 页面已切到 %s（%s），反代 CDP 会自动重连 ……"
                % (info.get("nickname") or name, "跳过" if info.get("skipped") else
                   ("daemon" if info.get("fallback") else "自研刷新")))
            time.sleep(1.5)
            ok2 = port_alive()
            bar("3/3 反代 %s" % ("在线，切号完成" if ok2 else "离线（点右上角重启 WB 或手动启反代）"),
                GREEN if ok2 else YELLOW)
            self.msgq.put(("done", "✔ 切好了：现在用 %s（%s）%s。模型仍是 %s，没被换掉。"
                           % (name, (info.get("secs") or 0), "",
                              (self.cache.get("model") or WM.active_model())),
                           GREEN))
        except Exception as e:
            self.msgq.put(("done", "切号过程炸了: %s: %s" % (type(e).__name__, e), RED))

    # ---------------------------------------------------------------- 双击切模型
    def on_double_click_model(self, event):
        iid = self.t2.identify_row(event.y)
        if not iid:
            return
        model = (getattr(self, "mmodel", {}) or {}).get(iid)
        if not model:
            return
        if self.busy_model:
            self.set_bar("正在切模型，等一下……", YELLOW)
            return
        cur = self.cache.get("model") or WM.active_model()
        if model == cur:
            self.set_bar("当前模型就是 %s，不用切。" % model, GREEN)
            return
        self.busy_model = True
        self.set_bar("→ 切换模型：%s → %s（与账号无关，账号不会动）" % (cur, model), YELLOW)
        threading.Thread(target=self._model_worker, args=(model,), daemon=True).start()

    def _model_worker(self, model):
        try:
            WM.set_active_model(model)
            self.msgq.put(("model", "✔ 模型已切到 %s。当前账号 %s 保持不变，以后换任何账号都还是这个模型。"
                           % (model, WM.current_account().get("nickname") or WM.current_uid()[:8]), GREEN))
        except Exception as e:
            self.msgq.put(("model", "切模型失败: %s: %s" % (type(e).__name__, e), RED))

    # ---------------------------------------------------------------- 无感登录
    def start_oauth(self):
        if getattr(self, "_oauth_busy", False):
            self.set_bar("授权流程还在跑，先等它出结果……", YELLOW)
            return
        self._oauth_busy = True
        self.set_bar("→ 正在申请授权链接（不会打断当前 WorkBuddy 登录）……", YELLOW)
        self.btn_login.config(state="disabled")
        threading.Thread(target=self._oauth_worker, daemon=True).start()

    def _oauth_worker(self):
        try:
            state, url = WM.oauth_start()
            WM.open_url(url)
            self.msgq.put(("bar", "浏览器已打开授权页（state=%s…），请在网页里登录目标 WorkBuddy 账号，登录完会自动入库……"
                           % state[:8], YELLOW))
            deadline = time.time() + 300
            while time.time() < deadline:
                time.sleep(3)
                try:
                    r = WM.oauth_poll_once(state)
                except Exception as e:
                    r = {"done": False, "error": str(e)}
                if r.get("done"):
                    uid = r.get("uid") or ""
                    nm = r.get("nickname") or (uid[:8] if uid else "新号")
                    self.msgq.put(("oauth", "✔ 无感登录成功：%s 已写入账号库（%s）" % (nm, WM.ACCOUNTS_DIR), GREEN))
                    return
            self.msgq.put(("oauth", "授权超时（5 分钟没检测到登录完成）。可以再点一次「+ 无感登录新号」。", YELLOW))
        except Exception as e:
            self.msgq.put(("oauth", "无感登录失败: %s: %s" % (type(e).__name__, e), RED))

    # ---------------------------------------------------------------- 领积分
    def claim_all_accounts(self):
        if getattr(self, "_claim_busy", False):
            self.set_bar("正在领积分，别急……", YELLOW)
            return
        self._claim_busy = True
        self.btn_claim.config(state="disabled")
        self.set_bar("→ 开始给全部账号领今日积分（凭证过期会自动刷新）……", YELLOW)
        threading.Thread(target=self._claim_worker, args=(list(NAME.keys()), False), daemon=True).start()

    def _claim_worker(self, uids, quiet):
        try:
            res = WM.claim_all(uids)
            ok_n = sum(1 for r in res if r.get("ok"))
            already = sum(1 for r in res if r.get("already"))
            bad = [r for r in res if not r.get("ok")]
            txt = "✔ 领积分完成：%d 个成功（其中 %d 个今天早领过了）" % (ok_n, already)
            if bad:
                first = "%s…%s" % (NAME.get(str(bad[0]["uid"])[:8], str(bad[0]["uid"])[:8]),
                                   str(bad[0].get("message") or "")[:60])
                txt += "，%d 个失败（例：%s）" % (len(bad), first)
            self.msgq.put(("claim", txt, GREEN if ok_n else YELLOW))
        except Exception as e:
            self.msgq.put(("claim", "领积分失败: %s: %s" % (type(e).__name__, e), RED))

    def _auto_claim_once(self):
        """启动后自动补领今日积分：只对今天还没领过的号动手，避免重复请求。"""
        if not self.auto_claim.get():
            return
        try:
            todo = [u for u in NAME.keys() if not WM.checkin_state(u).get("today")]
        except Exception:
            todo = []
        if not todo:
            self.set_bar("今日积分：全部账号都已领过，无需重复请求。", MUTED)
            return
        self.set_bar("启动自检：发现 %d 个账号今天还没领积分，后台自动领……" % len(todo), YELLOW)
        self._claim_busy = True
        threading.Thread(target=self._claim_worker, args=(todo, True), daemon=True).start()

    # ---------------------------------------------------------------- 兜底重启
    def restart_wb_manual(self):
        if self.busy:
            self.set_bar("正在忙，等这次跑完再点。", YELLOW)
            return
        self._start_busy("重启 WB 中…",
                         "→ 兜底重启 WorkBuddy（约 20~90 秒，期间反代拿不到号）", YELLOW)
        threading.Thread(target=self._restart_worker, daemon=True).start()

    def _restart_worker(self):
        try:
            ok = WM.restart_wb()
            if ok:
                self.msgq.put(("done", "✔ WorkBuddy 已带 CDP %d 重启完成" % int(WM.cfg()["cdp_port"]), GREEN))
            else:
                self.msgq.put(("done", "重启失败，看 %s" % WM.LOG_FILE, RED))
        except Exception as e:
            self.msgq.put(("done", "重启炸了: %s: %s" % (type(e).__name__, e), RED))

    # ---------------------------------------------------------------- 消息 / 心跳
    def _drain_msgs(self):
        done = False
        while True:
            try:
                item = self.msgq.get_nowait()
            except queue.Empty:
                break
            kind, text, color = item[0], item[1], (item[2] if len(item) > 2 else FG)
            if kind == "bar":
                self.set_bar(text, color or YELLOW)
            elif kind in ("done", "model"):
                self.set_bar(text, color or FG)
                self.busy = False
                self.busy_model = False
                done = True
            elif kind == "oauth":
                self.set_bar(text, color or FG)
                self._oauth_busy = False
                try:
                    self.btn_login.config(state="normal")
                except Exception:
                    pass
                done = True
            elif kind == "claim":
                self.set_bar(text, color or FG)
                self._claim_busy = False
                try:
                    self.btn_claim.config(state="normal")
                except Exception:
                    pass
                done = True
        if done:
            try:
                self.lbl_hint.config(text="双击账号行=无感切号　双击模型行=切模型（两者互不影响）")
            except Exception:
                pass
            self.refresh_all()

    def _toggle_top(self):
        try:
            self.root.attributes("-topmost", bool(self.on_top.get()))
        except Exception:
            pass

    def tick(self):
        """每秒刷新倒计时，5 秒重载一次数据；顺带把后台线程进度搬到界面"""
        self._drain_msgs()
        ledger = self.cache.get("ledger") or {}
        for iid in self.t1.get_children():
            uid = (getattr(self, "rowmap", {}) or {}).get(iid)
            if not uid:
                continue
            v = list(self.t1.item(iid, "values"))
            if len(v) > 7 and v[6] == "限流中" and v[7] not in ("—", "已解禁"):
                clear_at = (ledger.get(uid) or {}).get("clear_at") or 0
                left = fmt_left(clear_at)
                if left != v[7]:
                    v[7] = left
                    self.t1.item(iid, values=v)
        if self.auto.get():
            self._cnt = getattr(self, "_cnt", 0) + 1
            if self._cnt % 5 == 0:
                self.refresh_all()
        self.root.after(1000, self.tick)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
