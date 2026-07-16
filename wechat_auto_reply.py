"""
微信消息监听与自动回复脚本
指令: 1=开始监听  2=停止监听  d=切换DeepSeek  a=切换Agnes
"""
import time as _time
import json
import logging
import threading
import sys
import subprocess
import re
import os
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

from openai import OpenAI


# ============================================================
# 配置
# ============================================================

@dataclass
class Config:
    api_key: str = ""
    api_base_url: str = ""
    api_model: str = ""
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
    friend_prompts: dict[str, str] = field(default_factory=dict)  # 针对不同好友的语气定制
    ignore_prefix: list[str] = field(default_factory=lambda: ["[Ai]", "[AI]", "[Bot]", "[bot]"])
    log_dir: str = "logs"
    lobby_session: str = "文件传输助手"


@dataclass
class APIPreset:
    name: str
    api_key: str
    api_base_url: str
    api_model: str


def load_config(path: str = "config.json") -> Config:
    cfg = Config()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for key, value in data.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
    except FileNotFoundError:
        pass
    return cfg


def load_presets(path: str = "config.json") -> list[APIPreset]:
    """从 config.json 的 api_presets 字段加载 API 配置"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("api_presets", [])
        return [APIPreset(**p) for p in raw]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


# ============================================================
# 日志
# ============================================================

def setup_logger(log_dir: str) -> logging.Logger:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("wechat_bot")
    logger.setLevel(logging.INFO)

    fh = logging.FileHandler(
        Path(log_dir) / f"bot_{datetime.now():%Y%m%d}.log",
        encoding="utf-8",
    )
    fh.setLevel(logging.INFO)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s | %(levelname)-5s | %(message)s", datefmt="%m-%d %H:%M:%S")
    fh.setFormatter(fmt)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


# ============================================================
# 消息会话
# ============================================================

@dataclass
class ChatSession:
    name: str
    last_reply_time: float = 0.0
    last_processed_text: str = ""


# ============================================================
# AI 回复
# ============================================================

class AIReply:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.api_key = ""
        self.api_base_url = ""
        self.api_model = ""
        self.system_prompt = ""
        self._client: Optional[OpenAI] = None
        self._total_tokens = 0          # 模拟累计 tokens
        self._request_count = 0          # 调用次数
        self._auto_switched = False      # 是否已自动切换过

    def configure(self, api_key: str, api_base_url: str, api_model: str, system_prompt: str) -> None:
        self.api_key = api_key
        self.api_base_url = api_base_url
        self.api_model = api_model
        self.system_prompt = system_prompt
        self._client = OpenAI(api_key=api_key, base_url=api_base_url)
        self.logger.info(f"AI 切换至: {api_model}")

    def get_reply(self, context: list[dict], extra_prompt: str = "", use_search: bool = False) -> tuple[list[str], int]:
        """返回 (回复消息列表, 模拟token消耗)  可返回多条消息 但不超过3条"""
        if not self._client:
            return [], 0

        system = self.system_prompt
        if extra_prompt:
            system += " " + extra_prompt

        prompt_text = system + "".join(m["content"] for m in context)

        # 如果启用联网搜索，先搜（只针对最新消息中的链接和关键词）
        search_result = ""
        if use_search:
            last_msg = context[-1]["content"] if context else ""
            search_result = self._web_search(last_msg)

        # 估算 token
        input_tokens = len(prompt_text) // 2
        output_tokens = 512
        simulated = input_tokens + output_tokens

        try:
            messages = [{"role": "system", "content": system}]
            messages.extend(context)

            if search_result:
                messages.append({"role": "user", "content": f"请基于以下搜索结果回答问题，如果搜索结果不相关就正常回复。\n\n搜索结果：\n{search_result}"})
                messages.append({"role": "assistant", "content": "好的，我会参考搜索结果"})

            resp = self._client.chat.completions.create(
                model=self.api_model,
                messages=messages,
                temperature=0.7,
                max_tokens=1024,
                timeout=60,
            )
            full = resp.choices[0].message.content.strip()
            if not full:
                return [], simulated
            full = self._strip_thinking(full)

            # 按换行分割成多条，最多3条
            parts = [p.strip() for p in full.replace("\r\n", "\n").replace("。", "\n").split("\n") if p.strip()]
            if not parts:
                return [], simulated
            return parts[:3], simulated
        except Exception as e:
            self.logger.error(f"AI 回复出错: {e}")
            return [], simulated

    def _web_search(self, query: str) -> str:
        """调用 MCP websearch-deepseek 搜索 (JSON-RPC over stdio)"""
        if not query:
            return ""
        try:
            server_path = Path.home() / "AppData/Roaming/npm/node_modules/websearch-deepseek/dist/src/mcp.js"
            if not server_path.exists():
                server_path = "websearch-deepseek"

            env = {**os.environ, "DEEPSEEK_API_KEY": self.api_key}
            proc = subprocess.Popen(
                ["node", str(server_path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=env
            )

            # initialize
            init = json.dumps({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"wechat-bot","version":"1.0"}}}, ensure_ascii=True)
            proc.stdin.write(init.encode("utf-8") + b"\n")
            proc.stdin.flush()
            _time.sleep(1.5)

            # tools/call web_search
            req = json.dumps({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"web_search","arguments":{"query":query}}}, ensure_ascii=True)
            proc.stdin.write(req.encode("utf-8") + b"\n")
            proc.stdin.flush()
            _time.sleep(1)
            proc.stdin.close()

            out, _ = proc.communicate(timeout=25)
            out_text = out.decode("utf-8", errors="replace")

            # 解析最后一个 JSON-RPC 响应
            import re as _re
            matches = _re.findall(r"\{.*\}", out_text)
            if not matches:
                return ""
            resp = json.loads(matches[-1])
            if "result" not in resp:
                return ""
            content = resp["result"].get("content", [])
            result_parts = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    result_parts.append(c.get("text", ""))
            return "\n".join(result_parts)[:2000]
        except Exception as e:
            self.logger.warning(f"联网搜索失败: {e}")
            return ""

    @staticmethod
    def _strip_thinking(text: str) -> str:
        start = chr(60) + "think" + chr(62)
        end = chr(60) + "/think" + chr(62)
        if start in text and end in text:
            text = text.rsplit(end, 1)[-1]
        return text.strip()


# ============================================================
# 微信机器人
# ============================================================

class WeChatBot:
    def __init__(self, cfg: Config, presets: list[APIPreset]):
        self.cfg = cfg
        self.presets = presets
        self.current_preset_idx = 0
        self.token_budget = getattr(cfg, "token_budget", 30000)
        self.logger = setup_logger(self.cfg.log_dir)
        self.ai = AIReply(self.logger)
        self.sessions: dict[str, ChatSession] = {}
        self._listening = True             # 启动后默认开启监听
        self._running = True

        try:
            from wxauto4 import WeChat
            # 先尝试初始化，给微信一点加载时间
            for retry in range(3):
                try:
                    self.wx = WeChat(ads=False)
                    break
                except Exception:
                    if retry < 2:
                        _time.sleep(2)
                        self.logger.warning(f"微信初始化重试 {retry+1}/3...")
                    else:
                        raise
        except Exception as e:
            self.logger.error(f"微信初始化失败，请确认微信已登录且版本兼容: {e}")
            raise

        # DeepSeek 在 presets[0]，启动时默认使用
        if self.presets:
            self._apply_preset(0)

    # ----------------------------------------------------------
    # API Preset 切换
    # ----------------------------------------------------------

    def _apply_preset(self, idx: int) -> None:
        if 0 <= idx < len(self.presets):
            p = self.presets[idx]
            self.current_preset_idx = idx
            self.ai.configure(p.api_key, p.api_base_url, p.api_model, self.cfg.system_prompt)
            self.logger.info(f"当前模型: {p.name} ({p.api_model})  Token: {self.ai._total_tokens}/{self.token_budget}")

    # ----------------------------------------------------------
    # 工具方法
    # ----------------------------------------------------------

    @staticmethod
    def _is_text_only_msg(msg) -> bool:
        class_name = type(msg).__name__
        skip = ["Time", "Image", "Video", "Emotion",
                "Voice", "Location", "Merge", "PersonalCard",
                "Note", "Other", "System"]
        for kw in skip:
            if kw in class_name:
                return False
        return True

    @staticmethod
    def _extract_text(msg) -> Optional[str]:
        content = getattr(msg, "content", None)
        if content:
            return str(content)
        attr_str = getattr(msg, "attr", None)
        if attr_str:
            return str(attr_str)
        return None

    # ----------------------------------------------------------
    # 好友匹配
    # ----------------------------------------------------------

    def _is_friend(self, name: str) -> bool:
        if not self.cfg.auto_reply_friends:
            return True
        for friend in self.cfg.auto_reply_friends:
            if not friend:
                continue
            if name == friend:
                return True
            if name.lower() == friend.lower():
                return True
            if friend in name or name in friend:
                return True
        return False

    def _should_reply(self, sender: str, content: str) -> bool:
        if not content:
            return False
        if sender in self.cfg.blacklist:
            return False
        for prefix in self.cfg.ignore_prefix:
            if content.startswith(prefix):
                return False
        return True

    @staticmethod
    def _needs_search(text: str) -> bool:
        """仅在消息含链接或明显需要查实时信息时搜索"""
        if not text or len(text) < 3:
            return False
        # 链接
        if "http" in text or "https" in text or "www." in text or ".com" in text or ".cn" in text:
            return True
        if "[链接]" in text or "[小程序]" in text:
            return True
        # 搜索意图
        triggers = ["搜","查一下","查查","查个","查","搜索","百度","谷歌","你知道吗",
                     "今天天气","最近新闻","什么情况","怎么回事","哪儿","哪",
                     "帮我查","找一下","看看"]
        for t in triggers:
            if t in text:
                return True
        return False

    # ----------------------------------------------------------
    # 消息处理
    # ----------------------------------------------------------

    def _handle_friend(self, name: str) -> None:
        self.wx.ChatWith(name)
        _time.sleep(0.3)

        texts = []
        for msg in self.wx.GetAllMessage():
            class_name = type(msg).__name__
            if "Time" in class_name or "Self" in class_name:
                continue
            if not self._is_text_only_msg(msg):
                continue
            t = self._extract_text(msg)
            if t:
                texts.append(t)

        if not texts:
            return

        session = self.sessions.setdefault(name, ChatSession(name=name))

        start_idx = 0
        if session.last_processed_text:
            try:
                for i in range(len(texts) - 1, -1, -1):
                    if texts[i] == session.last_processed_text:
                        start_idx = i + 1
                        break
            except Exception:
                start_idx = 0

        if start_idx >= len(texts):
            return

        new_msgs = texts[start_idx:]
        session.last_processed_text = new_msgs[-1]

        context_start = max(0, start_idx - 2)
        context_before = texts[context_start:start_idx]
        reply_text = new_msgs[-1]

        self.logger.info(f"[{name}] -> {reply_text[:80]}")

        if self._should_reply(name, reply_text):
            self._reply_and_back(session, reply_text, context_before)

    def _reply_and_back(self, session: ChatSession, incoming: str, context_before: list[str]) -> None:
        if _time.time() - session.last_reply_time < 3.0:
            return

        # 历史消息只做辅助参考 明确标注
        context_msgs = []
        for t in context_before:
            context_msgs.append({"role": "user", "content": f"[历史参考] {t}"})
        context_msgs.append({"role": "user", "content": f"[最新消息] {incoming}"})

        # 检查是否有针对该好友的语气定制
        extra = self.cfg.friend_prompts.get(session.name, "")

        # 联网搜索只由最新消息触发
        should_search = self._needs_search(incoming)

        self.logger.info(f"[{session.name}] AI 思考中..." + (" (联网搜索)" if should_search else ""))
        replies, simulated = self.ai.get_reply(context_msgs, extra_prompt=extra, use_search=should_search)
        if not replies:
            self.logger.warning(f"[{session.name}] AI 未产生回复")
            return

        # 累加模拟 tokens
        self.ai._total_tokens += simulated
        self.ai._request_count += 1
        self.logger.info(f"  Token: +{simulated}  累计: {self.ai._total_tokens}/{self.token_budget}")

        # 超过预算自动切换
        if self.ai._total_tokens >= self.token_budget and not self.ai._auto_switched:
            self.ai._auto_switched = True
            for i, p in enumerate(self.presets):
                if "agnes" in p.name.lower():
                    self.logger.info(f">> Token 已达 {self.token_budget}，自动切换至 {p.name}")
                    self._apply_preset(i)
                    break

        try:
            for i, part in enumerate(replies):
                # 发每条前重新确认聊天框
                if i > 0 or len(replies) > 1:
                    self.wx.ChatWith(session.name)
                    _time.sleep(0.2)
                self.wx.SendMsg(part, clear=True)
                session.last_reply_time = _time.time()
                self.logger.info(f"[{session.name}] ({i+1}/{len(replies)}) <- {part[:60]}")
                if i < len(replies) - 1:
                    _time.sleep(0.8)
        except Exception as e:
            self.logger.error(f"[{session.name}] 发送失败: {e}")

    # ----------------------------------------------------------
    # 监听主循环
    # ----------------------------------------------------------

    def _main_loop(self) -> None:
        """静默待机 只在有 isnew 通知时处理 不轮询扫描聊天框"""
        while self._running:
            if not self._listening:
                _time.sleep(0.5)
                continue

            # 静默轮询会话列表 只看 isnew
            try:
                sessions = self.wx.GetSession()
            except Exception:
                _time.sleep(2)
                continue

            # 找有通知且在名单内的好友
            target = None
            for sess in sessions:
                try:
                    name = getattr(sess, "name", None)
                    if not name or not getattr(sess, "isnew", False):
                        continue
                    if not self._is_friend(name) or name in self.cfg.blacklist:
                        continue
                    if getattr(sess, "ismute", False):
                        continue
                    target = name
                    break
                except Exception:
                    continue

            if not target:
                _time.sleep(0.5)
                continue

            self.logger.info(f"[{target}] 收到新通知")
            self._handle_friend(target)

            # 回复完后留在该聊天框继续待机 等下一个通知
            _time.sleep(0.3)

    # ----------------------------------------------------------
    # 命令处理
    # ----------------------------------------------------------

    def _command_loop(self) -> None:
        """后台读取键盘指令"""
        while self._running:
            try:
                cmd = sys.stdin.readline().strip().lower()
                if not cmd:
                    continue

                if cmd == "1":
                    if not self._listening:
                        self._listening = True
                        self.logger.info(">> 开始监听")
                    else:
                        self.logger.info(">> 已在监听中")
                elif cmd == "2":
                    if self._listening:
                        self._listening = False
                        self.logger.info(">> 已停止监听")
                    else:
                        self.logger.info(">> 已停止监听")
                elif cmd == "d":
                    for i, p in enumerate(self.presets):
                        if "deepseek" in p.name.lower():
                            self._apply_preset(i)
                            break
                    else:
                        self.logger.info(">> 未找到 DeepSeek 配置")
                elif cmd == "a":
                    for i, p in enumerate(self.presets):
                        if "agnes" in p.name.lower():
                            self._apply_preset(i)
                            break
                    else:
                        self.logger.info(">> 未找到 Agnes 配置")
                elif cmd in ("h", "help"):
                    self.logger.info(">> 指令: 1=开始监听  2=停止监听  d=DeepSeek  a=Agnes  h=帮助  q=退出")
                elif cmd == "q":
                    self.logger.info(">> 退出中...")
                    self._running = False
                    self._listening = False
                else:
                    self.logger.info(f">> 未知指令: {cmd}  (输入 h 查看帮助)")

            except Exception:
                break

    # ----------------------------------------------------------
    # 启动
    # ----------------------------------------------------------

    def start(self) -> None:
        self.logger.info("=" * 50)
        self.logger.info("微信 AI 自动回复机器人")
        self.logger.info(f"好友: {self.cfg.auto_reply_friends or '全部联系人'}")
        self.logger.info(f"待机位置: {self.cfg.lobby_session}")
        self.logger.info("-" * 50)
        self.logger.info("指令:  1=开始监听  2=停止监听")
        self.logger.info("       d=切换DeepSeek  a=切换Agnes  q=退出")
        self.logger.info(f"       Token 上限: {self.token_budget}，超出自动切换")
        self.logger.info(f"       启动后默认开启监听，按 2 停止")
        self.logger.info("=" * 50)

        if not self.wx.IsOnline():
            self.logger.error("微信未登录或不在线")
            return

        try:
            self.wx.ChatWith(self.cfg.lobby_session)
        except Exception:
            pass

        self.logger.info("已切换至「%s」待机，开始监听新消息...", self.cfg.lobby_session)

        # 启动命令线程和主线程
        cmd_thread = threading.Thread(target=self._command_loop, daemon=True)
        cmd_thread.start()

        self._main_loop()
        self.stop()

    def stop(self) -> None:
        self._running = False
        self._listening = False
        self.logger.info("机器人已停止")


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    cfg = load_config("config.json")
    presets = load_presets("config.json")

    bot = WeChatBot(cfg, presets)
    bot.start()
