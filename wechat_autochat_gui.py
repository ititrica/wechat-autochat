"""
WeChat AutoChat GUI — 微信 AI 自动回复机器人 图形界面
功能：启动/停止监听、切换模型、管理好友、本地上下文持久化、系统托盘
"""
import time as _time
import json
import threading
import sys
import os
import subprocess
import re
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import ctypes
import logging

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

import pystray
from PIL import Image, ImageDraw
from openai import OpenAI

# ── 单实例锁 ──────────────────────────────────────────────

import win32event
import win32api
import win32gui
import win32con
import winerror
MUTEX_NAME = "WeChatAutoChat_SingleInstance_Mutex"

def _ensure_single_instance() -> bool:
    """返回 True = 是第一个实例；False = 已有实例在运行"""
    mutex = win32event.CreateMutex(None, False, MUTEX_NAME)
    err = win32api.GetLastError()
    if err == winerror.ERROR_ALREADY_EXISTS:
        return False
    return True

def _bring_existing_to_front():
    """找到已运行的窗口并激活到前台"""
    def _enum_cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if "WeChat AutoChat" in title:
                found_hwnds.append(hwnd)
    found_hwnds = []
    win32gui.EnumWindows(_enum_cb, None)
    if found_hwnds:
        hwnd = found_hwnds[0]
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        win32gui.BringWindowToTop(hwnd)

# ── 日志捕获 ──────────────────────────────────────────────

class TextHandler(logging.Handler):
    def __init__(self, widget: tk.Text):
        super().__init__()
        self.widget = widget
        self.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%H:%M:%S"))
    def emit(self, record):
        self.widget.after(0, lambda: (self.widget.insert(tk.END, self.format(record) + "\n"), self.widget.see(tk.END)))

# ── 配置 ──────────────────────────────────────────────────

MAX_HISTORY = 10

@dataclass
class Config:
    system_prompt: str = (
        "你是我本人（Lai）在回复微信好友消息。你的聊天风格特征如下：\n\n"
        "1. 简短直接 — 能用3个字说清绝不写5个字，大量单字回复（嗯、行、好、对、en）\n"
        "2. 不解释 — 不说背景不铺垫因果\n"
        "3. 脏话当语气词 — sb、tm、wc、nm，不带攻击性\n"
        "4. 中英混写 — pr、ai、nas、jpg等\n"
        "5. 几乎不用句号，用空格或换行分隔。问句用问号\n"
        "6. 话题切换跳跃，没有过渡句\n"
        "7. 复杂话题拆成2-5条短消息连续发\n"
        "8. 句子太长可以拆成短句分多条发 每条单独成句 最多3条 不要硬拆\n"
        "9. 如果聊天氛围合适可以使用emoji或表情包 优先抽象风格 但不要刷屏\n"
        "10. 根据聊天氛围可以自己选择加入轻度成人话语\n"
        "11. 如果收到联网搜索结果请优先参考"
    )
    auto_reply_friends: list[str] = field(default_factory=list)
    blacklist: list[str] = field(default_factory=list)
    friend_prompts: dict[str, str] = field(default_factory=dict)
    ignore_prefix: list[str] = field(default_factory=lambda: ["[Ai]", "[AI]", "[Bot]", "[bot]"])
    log_dir: str = "logs"

@dataclass
class APIPreset:
    name: str
    api_key: str
    api_base_url: str
    api_model: str

CONFIG_PATH = (Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent) / "config.json"
HISTORY_PATH = (Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent) / "history.json"
_presets: list[APIPreset] = []
_token_budget = 30000

def load_config() -> Config:
    cfg = Config()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            for k, v in json.load(f).items():
                if hasattr(cfg, k): setattr(cfg, k, v)
    except FileNotFoundError:
        # 自动生成默认配置模板
        _generate_default_config()
        with open(CONFIG_PATH, encoding="utf-8") as f:
            for k, v in json.load(f).items():
                if hasattr(cfg, k): setattr(cfg, k, v)
    return cfg

def _generate_default_config():
    """生成默认 config.json 模板"""
    template = {
        "system_prompt": "你是我本人（Lai）在回复微信好友消息。你的聊天风格特征如下：\n\n1. 简短直接 — 能用3个字说清绝不写5个字，大量单字回复（嗯、行、好、对、en）\n2. 不解释 — 不说背景不铺垫因果\n3. 脏话当语气词 — sb、tm、wc、nm，不带攻击性\n4. 中英混写 — pr、ai、nas、jpg等\n5. 几乎不用句号，用空格或换行分隔。问句用问号\n6. 话题切换跳跃，没有过渡句\n7. 复杂话题拆成2-5条短消息连续发\n8. 句子太长可以拆成短句分多条发 每条单独成句 最多3条 不要硬拆\n9. 如果聊天氛围合适可以使用emoji或表情包 优先抽象风格 但不要刷屏\n10. 根据聊天氛围可以自己选择加入轻度成人话语\n11. 如果收到联网搜索结果请优先参考",
        "auto_reply_friends": ["好友A", "好友B"],
        "friend_prompts": {},
        "blacklist": [],
        "ignore_prefix": ["[Ai]", "[AI]", "[Bot]", "[bot]"],
        "log_dir": "logs",
        "api_presets": [
            {"name": "Agnes", "api_key": "YOUR_API_KEY", "api_base_url": "https://apihub.agnes-ai.com/v1", "api_model": "agnes-2.0-flash"},
            {"name": "DeepSeek", "api_key": "YOUR_API_KEY", "api_base_url": "https://api.deepseek.com", "api_model": "deepseek-v4-flash"}
        ],
        "token_budget": 30000
    }
    write_full_config(template)

def save_config(cfg: Config) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "system_prompt": cfg.system_prompt, "auto_reply_friends": cfg.auto_reply_friends,
            "friend_prompts": cfg.friend_prompts, "blacklist": cfg.blacklist,
            "ignore_prefix": cfg.ignore_prefix, "log_dir": cfg.log_dir,
            "api_presets": [{"name": p.name, "api_key": p.api_key, "api_base_url": p.api_base_url, "api_model": p.api_model} for p in _presets],
            "token_budget": _token_budget,
        }, f, ensure_ascii=False, indent=4)

def load_presets() -> list[APIPreset]:
    global _presets, _token_budget
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f: d = json.load(f)
        _presets = [APIPreset(**p) for p in d.get("api_presets", [])]
        _token_budget = d.get("token_budget", 30000)
    except FileNotFoundError: _presets = []
    return _presets

# 找到 DeepSeek 索引，默认使用
def _default_preset_idx() -> int:
    for i, p in enumerate(_presets):
        if "deepseek" in p.name.lower(): return i
    return 0

# ── 本地上下文持久化 ─────────────────────────────────────

def load_history() -> dict[str, list[str]]:
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f: return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): return {}

def save_history(history: dict[str, list[str]]) -> None:
    trimmed = {k: v[-MAX_HISTORY:] for k, v in history.items()}
    with open(HISTORY_PATH, "w", encoding="utf-8") as f: json.dump(trimmed, f, ensure_ascii=False, indent=2)

def add_to_history(name: str, text: str) -> list[str]:
    h = load_history()
    msgs = h.get(name, [])
    msgs.append(text)
    if len(msgs) > MAX_HISTORY: msgs = msgs[-MAX_HISTORY:]
    h[name] = msgs
    save_history(h)
    return msgs

# ── AI 回复引擎 ──────────────────────────────────────────

class AIReply:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.api_key = self.api_base_url = self.api_model = self.system_prompt = ""
        self._client: Optional[OpenAI] = None
        self._total_tokens = self._request_count = 0
        self._auto_switched = False

    def configure(self, api_key, api_base_url, api_model, system_prompt):
        self.api_key, self.api_base_url, self.api_model, self.system_prompt = api_key, api_base_url, api_model, system_prompt
        if api_key:
            self._client = OpenAI(api_key=api_key, base_url=api_base_url)
        else:
            self._client = None

    def get_reply(self, context, extra_prompt="", use_search=False):
        if not self._client: return [], 0
        system = self.system_prompt + (" " + extra_prompt if extra_prompt else "")
        sim = (len(system + "".join(m["content"] for m in context)) // 2) + 512
        try:
            msgs = [{"role": "system", "content": system}] + context
            if use_search and context:
                sr = self._web_search(context[-1]["content"])
                if sr: msgs.append({"role": "user", "content": f"请参考搜索结果回答，不相关则正常回复。\n\n搜索结果：\n{sr}"})
            resp = self._client.chat.completions.create(model=self.api_model, messages=msgs, temperature=0.7, max_tokens=1024, timeout=60)
            full = (resp.choices[0].message.content or "").strip()
            if not full: return [], sim
            full = self._strip_thinking(full)
            parts = [p.strip() for p in full.replace("\r\n", "\n").replace("。", "\n").split("\n") if p.strip()]
            return parts[:3], sim
        except Exception as e:
            self.logger.error(f"AI 回复出错: {e}")
            return None, sim  # 返回 None 而非空列表，区分"出错"和"无回复"

    def _web_search(self, query):
        if not query: return ""
        try:
            sp = Path.home() / "AppData/Roaming/npm/node_modules/websearch-deepseek/dist/src/mcp.js"
            if not sp.exists(): return ""
            env = {**os.environ, "DEEPSEEK_API_KEY": self.api_key}
            proc = subprocess.Popen(["node", str(sp)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
            for msg, delay in [
                ({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"bot","version":"1.0"}}}, 1.5),
                ({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"web_search","arguments":{"query":query}}}, 1),
            ]:
                proc.stdin.write(json.dumps(msg, ensure_ascii=True).encode("utf-8") + b"\n"); proc.stdin.flush(); _time.sleep(delay)
            proc.stdin.close()
            out, _ = proc.communicate(timeout=25)
            matches = re.findall(r"\{.*\}", out.decode("utf-8", errors="replace"))
            if not matches: return ""
            parts = []
            for c in (json.loads(matches[-1]).get("result") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "text": parts.append(c["text"])
            return "\n".join(parts)[:2000]
        except Exception as e: self.logger.warning(f"搜索失败: {e}"); return ""

    @staticmethod
    def _strip_thinking(text):
        sp, ep = chr(60)+"think"+chr(62), chr(60)+"/think"+chr(62)
        return text.rsplit(ep, 1)[-1].strip() if sp in text and ep in text else text.strip()

# ── 微信机器人 ────────────────────────────────────────────

@dataclass
class ChatSession:
    name: str
    last_reply_time: float = 0.0
    last_processed_text: str = ""

class WeChatBot:
    def __init__(self, cfg, presets, logger, status_callback=None):
        self.cfg, self.presets = cfg, presets
        self.current_preset_idx = 0
        self.token_budget = getattr(cfg, "token_budget", 30000)
        self.logger, self.ai = logger, AIReply(logger)
        self.sessions = {}
        self._listening, self._running = True, True
        self._status_callback, self.wx = status_callback, None
        if self.presets:
            self.current_preset_idx = _default_preset_idx()
            self._apply_preset(self.current_preset_idx)

    def init_wx(self):
        try:
            from wxauto4 import WeChat
            for i in range(3):
                try: self.wx = WeChat(ads=False); return True
                except Exception:
                    if i < 2: _time.sleep(2)
                    else: raise
        except Exception as e: self.logger.error(f"微信初始化失败: {e}"); return False

    def _apply_preset(self, idx):
        if 0 <= idx < len(self.presets):
            p = self.presets[idx]; self.current_preset_idx = idx
            self.ai.configure(p.api_key, p.api_base_url, p.api_model, self.cfg.system_prompt)
            self.logger.info(f"模型切换至: {p.name} ({p.api_model})")

    @staticmethod
    def _is_text_only_msg(msg):
        cn = type(msg).__name__
        return not any(k in cn for k in ["Time","Image","Video","Emotion","Voice","Location","Merge","PersonalCard","Note","Other","System"])

    @staticmethod
    def _extract_text(msg):
        c = getattr(msg, "content", "") or ""
        return str(c) if c else (str(getattr(msg, "attr", "") or "") or None)

    def _is_friend(self, name):
        if not self.cfg.auto_reply_friends: return True
        return any(name == f or name.lower() == f.lower() or f in name or name in f for f in self.cfg.auto_reply_friends if f)

    def _should_reply(self, sender, content):
        return bool(content and sender not in self.cfg.blacklist and not any(content.startswith(p) for p in self.cfg.ignore_prefix))

    @staticmethod
    def _needs_search(text):
        if not text or len(text) < 3: return False
        if any(k in text for k in ("http","https","www.",".com",".cn","[链接]","[小程序]")): return True
        return any(t in text for t in ["搜","查一下","查查","查个","查","搜索","百度","谷歌","你知道吗","今天天气","最近新闻","什么情况","怎么回事","哪儿","哪","帮我查","找一下","看看"])

    def _handle_friend(self, name):
        if not self.wx: return
        self.wx.ChatWith(name); _time.sleep(0.3)
        texts = []
        for msg in self.wx.GetAllMessage():
            cn = type(msg).__name__
            if "Time" in cn or "Self" in cn or not self._is_text_only_msg(msg): continue
            t = self._extract_text(msg)
            if t: texts.append(t)
        if not texts: return
        session = self.sessions.setdefault(name, ChatSession(name=name))
        si = 0
        if session.last_processed_text:
            try:
                for i in range(len(texts)-1, -1, -1):
                    if texts[i] == session.last_processed_text: si = i+1; break
            except Exception: si = 0
        if si >= len(texts): return
        new_msgs, session.last_processed_text = texts[si:], texts[-1]
        reply_text = new_msgs[-1]
        self.logger.info(f"[{name}] -> {reply_text[:80]}")
        if self._should_reply(name, reply_text): self._reply_and_back(name, reply_text)

    def _reply_and_back(self, name, incoming):
        session = self.sessions.get(name)
        if not session or _time.time() - session.last_reply_time < 3.0: return
        history = load_history()
        ctx = history.get(name, [])[-5:]
        ctx_msgs = [{"role": "user", "content": f"[历史参考] {t}"} for t in ctx[:-1]] if ctx else []
        ctx_msgs.append({"role": "user", "content": f"[最新消息] {incoming}"})
        extra = self.cfg.friend_prompts.get(name, "")
        search = self._needs_search(incoming)
        self.logger.info(f"[{name}] AI 思考中..." + (" (搜索)" if search else ""))
        replies, sim = self.ai.get_reply(ctx_msgs, extra_prompt=extra, use_search=search)
        # 出错时跳过回复（None 表示 API 调用失败）
        if replies is None:
            self.logger.warning(f"[{name}] API 调用失败，跳过回复")
            return
        if not replies:
            self.logger.warning(f"[{name}] AI 未产生回复")
            return
        self.ai._total_tokens += sim; self.ai._request_count += 1
        if self.ai._total_tokens >= self.token_budget and not self.ai._auto_switched:
            self.ai._auto_switched = True
            for i, p in enumerate(self.presets):
                if "agnes" in p.name.lower(): self._apply_preset(i); self.logger.info(f">> 已达限额，自动切换至 {p.name}"); break
        try:
            for i, part in enumerate(replies):
                if i > 0 or len(replies) > 1: self.wx.ChatWith(name); _time.sleep(0.2)
                self.wx.SendMsg(part, clear=True)
                session.last_reply_time = _time.time()
                self.logger.info(f"  [{name}] ({i+1}/{len(replies)}) <- {part[:60]}")
                if i < len(replies)-1: _time.sleep(0.8)
        except Exception as e: self.logger.error(f"[{name}] 发送失败: {e}")
        add_to_history(name, incoming)
        for r in replies: add_to_history(name, r)

    def _main_loop(self):
        while self._running:
            if not self._listening or not self.wx: _time.sleep(0.5); continue
            try: sessions = self.wx.GetSession()
            except Exception: _time.sleep(2); continue
            target = None
            for sess in sessions:
                try:
                    n = getattr(sess, "name", None)
                    if n and getattr(sess, "isnew", False) and self._is_friend(n) and n not in self.cfg.blacklist and not getattr(sess, "ismute", False): target = n; break
                except Exception: continue
            if not target: _time.sleep(0.5); continue
            self.logger.info(f"[{target}] 收到新通知"); self._handle_friend(target); _time.sleep(0.3)

    def start_listening(self):
        self._listening = True
        if self._status_callback: self._status_callback(True)
    def stop_listening(self):
        self._listening = False
        if self._status_callback: self._status_callback(False)
    def switch_model(self, idx):
        if 0 <= idx < len(self.presets): self._apply_preset(idx)
    def run(self): self._main_loop()

# ── 对话框 ────────────────────────────────────────────────

class PresetDialog:
    def __init__(self, parent, title, preset=None):
        self.result = None
        d = tk.Toplevel(parent); d.title(title); d.geometry("460x220"); d.resizable(False,False); d.transient(parent); d.grab_set()
        f = ttk.Frame(d, padding=12); f.pack(fill=tk.BOTH, expand=True)
        self.vars = {}
        for i, (label, key) in enumerate([("名称","name"),("API Key","api_key"),("Base URL","api_base_url"),("模型","api_model")]):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky=tk.W, pady=4)
            self.vars[key] = tk.StringVar(value=getattr(preset, key, "") if preset else "")
            ttk.Entry(f, textvariable=self.vars[key], width=50).grid(row=i, column=1, pady=4, padx=(6,0))
        bf = ttk.Frame(f); bf.grid(row=4, column=0, columnspan=2, pady=(12,0))
        ttk.Button(bf, text="取消", command=d.destroy).pack(side=tk.LEFT, padx=6)
        ttk.Button(bf, text="确定", command=lambda: self._ok(d)).pack(side=tk.LEFT, padx=6)
        d.wait_window()
    def _ok(self, d):
        if not self.vars["name"].get().strip(): return messagebox.showwarning("提示", "名称不能为空")
        self.result = APIPreset(name=self.vars["name"].get(), api_key=self.vars["api_key"].get(), api_base_url=self.vars["api_base_url"].get(), api_model=self.vars["api_model"].get())
        d.destroy()

class FriendDialog:
    def __init__(self, parent, current, prompts, on_save_callback):
        """on_save_callback(friends, prompts) 在确定时调用"""
        d = tk.Toplevel(parent); d.title("修改回复对象"); d.geometry("480x400"); d.resizable(False,False); d.transient(parent); d.grab_set()
        f = ttk.Frame(d, padding=12); f.pack(fill=tk.BOTH, expand=True)
        ttk.Label(f, text="每行一个好友昵称，支持中/英/日文").pack(anchor=tk.W)
        text = tk.Text(f, height=8, font=("Microsoft YaHei", 9))
        text.insert(tk.END, "\n".join(current)); text.pack(fill=tk.BOTH, expand=True, pady=4)
        ttk.Label(f, text="语气定制（好友名: 语气）").pack(anchor=tk.W)
        pt = tk.Text(f, height=5, font=("Microsoft YaHei", 9))
        pt.insert(tk.END, "\n".join(f"{k}: {v}" for k,v in prompts.items())); pt.pack(fill=tk.BOTH, expand=True, pady=4)
        bf = ttk.Frame(f); bf.pack(pady=(6,0))
        ttk.Button(bf, text="取消", command=d.destroy).pack(side=tk.LEFT, padx=6)
        ttk.Button(bf, text="确定", command=lambda: self._ok(d, text, pt, on_save_callback)).pack(side=tk.LEFT, padx=6)
        d.wait_window()
    def _ok(self, d, text, pt, callback):
        lines = [l.strip() for l in text.get("1.0", tk.END).strip().split("\n") if l.strip()]
        prompts = {}
        for line in pt.get("1.0", tk.END).strip().split("\n"):
            if ":" in line: k, v = line.split(":", 1); prompts[k.strip()] = v.strip()
        callback(lines, prompts)
        d.destroy()

# ── 主窗口 ────────────────────────────────────────────────

class App:
    def __init__(self):
        self.root = tk.Tk(); self.root.title("WeChat AutoChat"); self.root.geometry("800x680"); self.root.minsize(740, 600)
        self._tray_icon, self._show_tray = None, True
        self.cfg, self.presets = load_config(), load_presets()
        self._setup_logger()
        self.bot = WeChatBot(self.cfg, self.presets, self.logger, self._on_status_change)
        self._bot_thread = None
        self._build_ui()
        self.root.after(500, self._init_bot)
        self.root.after(1000, self._poll_bot)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _setup_logger(self):
        self.logger = logging.getLogger("wechat_bot_gui"); self.logger.setLevel(logging.INFO)
        Path(self.cfg.log_dir).mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(Path(self.cfg.log_dir)/f"bot_{datetime.now():%Y%m%d}.log", encoding="utf-8")
        fh.setLevel(logging.INFO); fh.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%m-%d %H:%M:%S"))
        self.logger.addHandler(fh)

    def _build_ui(self):
        mf = ttk.Frame(self.root, padding=10); mf.pack(fill=tk.BOTH, expand=True)
        # 左面板
        left = ttk.Frame(mf, width=320); left.pack(side=tk.LEFT, fill=tk.Y, padx=(0,8)); left.pack_propagate(False)
        # 右面板
        right = ttk.Frame(mf); right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        # ── 左侧内容 ──
        ttk.Label(left, text="WeChat AutoChat", font=("Segoe UI", 14, "bold")).pack(anchor=tk.W, pady=(0,2))
        ttk.Label(left, text="微信 AI 自动回复机器人").pack(anchor=tk.W, pady=(0,6))

        # 状态
        self.status_var = tk.StringVar(value="● 监听中（启动后自动开启）")
        ttk.Label(left, textvariable=self.status_var, font=("Segoe UI", 10)).pack(anchor=tk.W, pady=(0,4))

        # 控制按钮
        bf = ttk.Frame(left); bf.pack(fill=tk.X, pady=(0,8))
        self.btn_start = ttk.Button(bf, text="▶ 开始监听", command=self._on_start, width=14, state=tk.DISABLED)
        self.btn_start.pack(side=tk.LEFT, padx=(0,4))
        self.btn_stop = ttk.Button(bf, text="■ 停止监听", command=self._on_stop, width=14)
        self.btn_stop.pack(side=tk.LEFT)

        # 模型管理
        mlf = ttk.LabelFrame(left, text="模型管理", padding=4)
        mlf.pack(fill=tk.X, pady=(0,6))
        mr = ttk.Frame(mlf); mr.pack(fill=tk.X)
        self.model_var = tk.StringVar()
        self.model_combo = ttk.Combobox(mr, textvariable=self.model_var, state="readonly", width=26)
        self._refresh_model_list(); self.model_combo.pack(side=tk.LEFT, padx=(0,4))
        ttk.Button(mr, text="切换", width=5, command=self._on_switch_model).pack(side=tk.LEFT, padx=1)
        ttk.Button(mr, text="新增", width=5, command=self._on_add_preset).pack(side=tk.LEFT, padx=1)
        ttk.Button(mr, text="删除", width=5, command=self._on_del_preset).pack(side=tk.LEFT)
        self.model_combo.bind("<<ComboboxSelected>>", lambda e: self._show_preset(self.model_combo.current()))

        # API 设置
        af = ttk.LabelFrame(left, text="API 设置", padding=4)
        af.pack(fill=tk.X, pady=(0,6))
        self.api_key_var, self.api_url_var, self.api_model_name_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        for label, var in [("API Key",self.api_key_var),("Base URL",self.api_url_var),("模型",self.api_model_name_var)]:
            r = ttk.Frame(af); r.pack(fill=tk.X, pady=1)
            ttk.Label(r, text=label, width=8).pack(side=tk.LEFT)
            ttk.Entry(r, textvariable=var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(af, text="保存", command=self._on_save_api).pack(anchor=tk.E, pady=(2,0))
        if self.presets: self._show_preset(0)

        # 回复对象
        ff = ttk.LabelFrame(left, text="回复对象", padding=4)
        ff.pack(fill=tk.X, pady=(0,6))
        self.friend_var = tk.StringVar(value=", ".join(self.cfg.auto_reply_friends) if self.cfg.auto_reply_friends else "全部联系人")
        ttk.Label(ff, textvariable=self.friend_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(ff, text="修改", width=5, command=self._on_edit_friends).pack(side=tk.RIGHT)

        # 语气
        ttk.Label(left, text="自定义语气").pack(anchor=tk.W, pady=(2,2))
        pf = ttk.Frame(left); pf.pack(fill=tk.BOTH, expand=True, pady=(0,4))
        self.prompt_text = tk.Text(pf, height=6, font=("Microsoft YaHei",9), wrap=tk.WORD)
        self.prompt_text.insert(tk.END, self.cfg.system_prompt)
        sp = ttk.Scrollbar(pf, command=self.prompt_text.yview); self.prompt_text.configure(yscrollcommand=sp.set)
        self.prompt_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True); sp.pack(side=tk.RIGHT, fill=tk.Y)
        ttk.Button(left, text="保存语气", command=self._on_save_prompt).pack(anchor=tk.E)

        # ── 右侧日志 ──
        ttk.Label(right, text="运行日志").pack(anchor=tk.W)
        lf = ttk.Frame(right); lf.pack(fill=tk.BOTH, expand=True)
        self.log_text = tk.Text(lf, font=("Consolas",9), wrap=tk.WORD, bg="#1e1e1e", fg="#d4d4d4", insertbackground="white")
        sl = ttk.Scrollbar(lf, command=self.log_text.yview); self.log_text.configure(yscrollcommand=sl.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True); sl.pack(side=tk.RIGHT, fill=tk.Y)
        self.logger.addHandler(TextHandler(self.log_text))

    def _refresh_model_list(self):
        names = [p.name for p in self.presets]; self.model_combo["values"] = names
        idx = _default_preset_idx()
        self.model_var.set(names[idx] if names else "")

    def _show_preset(self, idx):
        if 0 <= idx < len(self.presets):
            p = self.presets[idx]; self.api_key_var.set(p.api_key); self.api_url_var.set(p.api_base_url); self.api_model_name_var.set(p.api_model)

    def _on_status_change(self, listening):
        if listening:
            self.status_var.set("● 监听中"); self.btn_start.configure(state=tk.DISABLED); self.btn_stop.configure(state=tk.NORMAL)
        else:
            self.status_var.set("● 已停止"); self.btn_start.configure(state=tk.NORMAL); self.btn_stop.configure(state=tk.DISABLED)

    def _init_bot(self):
        self.logger.info("正在初始化微信...")
        if self.bot.init_wx():
            self.logger.info("微信初始化成功，自动开始监听")
            self._bot_thread = threading.Thread(target=self.bot.run, daemon=True); self._bot_thread.start()
            self._on_status_change(True)
        else:
            self.logger.error("微信初始化失败"); messagebox.showerror("错误", "微信初始化失败，请确认已登录")
            self._on_status_change(False)

    def _on_start(self):
        if not self.bot.wx: return self.logger.warning("微信未初始化")
        self.bot.start_listening()
    def _on_stop(self): self.bot.stop_listening()
    def _on_switch_model(self):
        idx = self.model_combo.current()
        if idx >= 0:
            global _presets
            _presets = self.presets
            self.bot.switch_model(idx)
            self.logger.info(f">> 已切换至: {self.presets[idx].name}")

    def _on_add_preset(self):
        d = PresetDialog(self.root, "新增模型")
        if d.result:
            global _presets
            self.presets.append(d.result); _presets = self.presets; save_config(self.cfg); self._refresh_model_list()
            self.model_combo.current(len(self.presets)-1); self._show_preset(len(self.presets)-1)
            self.logger.info(f"新增模型: {d.result.name}")

    def _on_del_preset(self):
        idx = self.model_combo.current()
        if idx < 0 or len(self.presets) <= 1: return messagebox.showwarning("提示", "至少保留一个模型")
        name = self.presets[idx].name
        if messagebox.askyesno("确认", f"确认删除模型「{name}」？"):
            global _presets
            self.presets.pop(idx); _presets = self.presets; save_config(self.cfg); self._refresh_model_list(); self._show_preset(0)
            self.logger.info(f"删除模型: {name}")

    def _on_save_api(self):
        idx = self.model_combo.current()
        if idx < 0: return
        p = self.presets[idx]; p.api_key = self.api_key_var.get(); p.api_base_url = self.api_url_var.get(); p.api_model = self.api_model_name_var.get()
        # 立即重新配置 bot 的 AI 客户端
        self.bot.ai.configure(p.api_key, p.api_base_url, p.api_model, self.cfg.system_prompt)
        self.bot.current_preset_idx = idx
        save_config(self.cfg); self.logger.info(f"API 已保存 ({p.name})")

    def _on_edit_friends(self):
        def on_save(friends, prompts):
            self.cfg.auto_reply_friends = friends
            self.cfg.friend_prompts = prompts
            self.bot.cfg.auto_reply_friends = friends
            self.bot.cfg.friend_prompts = prompts
            save_config(self.cfg)
            self.friend_var.set(", ".join(friends) if friends else "全部联系人")
            self.logger.info(f"回复对象已更新: {friends}")
        FriendDialog(self.root, self.cfg.auto_reply_friends, self.cfg.friend_prompts, on_save)

    def _on_save_prompt(self):
        self.cfg.system_prompt = self.prompt_text.get("1.0", tk.END).strip()
        self.bot.ai.system_prompt = self.cfg.system_prompt
        save_config(self.cfg); self.logger.info("语气已保存")

    def _poll_bot(self):
        if self._bot_thread and not self._bot_thread.is_alive():
            self.logger.warning("机器人线程已退出，尝试重启...")
            self._bot_thread = threading.Thread(target=self.bot.run, daemon=True); self._bot_thread.start()
        self.root.after(2000, self._poll_bot)

    def _create_tray_icon(self):
        img = Image.new("RGBA", (64,64), (0,0,0,0))
        ImageDraw.Draw(img).ellipse([4,4,60,60], fill="#0078D4")
        self._tray_icon = pystray.Icon("wechat_autochat", img, "WeChat AutoChat", menu=pystray.Menu(
            pystray.MenuItem("打开主界面", lambda: self.root.after(0, self._show_window_impl), default=True),
            pystray.MenuItem("退出程序", self._quit_app),
        ))
        self._tray_icon.run_detached()

    def _show_window_impl(self): self.root.deiconify(); self.root.lift(); self.root.focus_force()
    def _on_close(self):
        self.root.withdraw()
        if not self._tray_icon: self._create_tray_icon()
        self.logger.info("已最小化到系统托盘")

    def _quit_app(self, icon=None):
        if self._tray_icon:
            # 停止托盘图标
            self._tray_icon.stop()
            self._tray_icon = None
        self.bot._running = False; self.bot._listening = False
        self.root.quit()
        self.root.destroy()
        # 强制退出进程
        os._exit(0)

    def run(self): self.root.mainloop(); os._exit(0)  # mainloop 退出后也强杀

if __name__ == "__main__":
    if not _ensure_single_instance():
        _bring_existing_to_front()
        sys.exit(0)
    load_presets(); App().run()
