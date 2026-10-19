"""条件式主动消息：用户多久没说话，bot 自己发一条过来。

人设里可能写了关于主动发消息的提示词，但框架里没有这条链路，
于是模型在对话里只能"编"自己发过。这个插件把这句话落实，顺便少一个幻觉来源。

规则（都可调）：
  · 用户空闲 >= IDLE_HOURS 小时才发
  · 两条主动消息之间 >= COOLDOWN_HOURS 小时
  · 每天最多 DAILY_MAX 条，只在 ACTIVE_FROM ~ ACTIVE_TO 时段内
  · 内容由 LLM 按**当前会话的人设**现写，并带最近对话做上下文（不许编造）

手动触发：`/主动`（或 `/proactive`）—— 立刻走同一条生成 + 发送路径（测试用，不改计数）
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
import traceback

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.core.agent.message import Message
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.star import StarTools

# 默认值；实际运行值由插件配置页覆盖
IDLE_HOURS = 2.0  # 用户安静这么久才考虑发
COOLDOWN_HOURS = 3.0  # 两条之间至少隔这么久
DAILY_MAX = 5  # 每天上限
ACTIVE_FROM, ACTIVE_TO = 8, 23  # 允许主动的时段（24 小时制，含起不含止）
CHECK_EVERY = 300  # 每 5 分钟看一次
SEG_GAP = (6.0, 12.0)  # 分条发送间隔
MAX_SEG = 3


class Main(star.Star):
    def __init__(self, context: star.Context, config=None) -> None:
        self.context = context
        self.config = config or {}

        # 运行参数：默认值与旧版硬编码一致，用户不动配置时行为不变
        self.idle_hours = self._cfg_num("idle_hours", IDLE_HOURS)
        self.cooldown_hours = self._cfg_num("cooldown_hours", COOLDOWN_HOURS)
        self.daily_max = self._cfg_num("daily_max", DAILY_MAX, int)
        self.active_from = self._cfg_num("active_from", ACTIVE_FROM, int)
        self.active_to = self._cfg_num("active_to", ACTIVE_TO, int)
        self.check_every = self._cfg_num("check_every", CHECK_EVERY, int)
        self.max_seg = self._cfg_num("max_seg", MAX_SEG, int)
        gap_lo = self._cfg_num("seg_gap_min", SEG_GAP[0])
        gap_hi = self._cfg_num("seg_gap_max", SEG_GAP[1])
        self.seg_gap = (gap_lo, max(gap_lo, gap_hi))

        # 状态文件放在 data/plugin_data/ 下，卸载或重装插件不会丢
        data_dir = StarTools.get_data_dir("astrbot_plugin_proactive")
        self.state_path = os.path.join(str(data_dir), "state.json")

        self.state: dict = self._load()
        self._task: asyncio.Task | None = None

    def _cfg_num(self, key: str, default, cast=float):
        try:
            return cast(self.config.get(key, default))
        except (TypeError, ValueError):
            logger.warning("[主动] 配置项 %s 不是有效数字，用默认值 %s", key, default)
            return default

    # ---------------- 状态 ----------------
    def _load(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self) -> None:
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)
        except Exception as exc:
            logger.error("[主动] 状态写入失败: %s", exc)

    # ---------------- 记用户上次说话 ----------------
    @staticmethod
    def _is_real_message(event: AstrMessageEvent) -> bool:
        if (event.message_str or "").strip():
            return True
        try:
            for comp in event.message_obj.message or []:
                t = getattr(comp, "type", None)
                # ⚠️ ComponentType 枚举的值是大写（"Image"/"Plain"…），直接跟小写比恒为 False
                v = str(getattr(t, "value", t) or "").lower()
                if v in ("image", "record", "video", "file"):
                    return True
                if v == "plain" and (getattr(comp, "text", "") or "").strip():
                    return True
        except Exception:
            pass
        return False

    async def _touch(self, event: AstrMessageEvent) -> None:
        if not self._is_real_message(event):
            return
        umo = event.unified_msg_origin
        st = self.state.setdefault(umo, {})
        if not isinstance(st, dict):
            st = {}
            self.state[umo] = st
        st["last_user"] = time.time()
        self._save()

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE)
    async def touch_private(self, event: AstrMessageEvent) -> None:
        await self._touch(event)

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def touch_group(self, event: AstrMessageEvent) -> None:
        await self._touch(event)

    # ---------------- 取人设 / 历史 ----------------
    async def _conversation(self, umo: str):
        cm = self.context.conversation_manager
        cid = await cm.get_curr_conversation_id(umo)
        if not cid:
            return None, None
        return cid, await cm.get_conversation(umo, cid)

    @staticmethod
    def _history_of(conv) -> list:
        raw = getattr(conv, "history", None)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "[]")
            except Exception:
                raw = []
        return raw if isinstance(raw, list) else []

    async def _persona_text(self, umo: str) -> str:
        try:
            _, conv = await self._conversation(umo)
            pid = getattr(conv, "persona_id", None) if conv else None
            if pid:
                p = await self.context.get_db().get_persona_by_id(pid)
                text = getattr(p, "system_prompt", None) if p else None
                if text:
                    return text
        except Exception as exc:
            logger.error("[主动] 取人设失败: %s", exc)
        return ""

    async def _history_msgs(self, umo: str, limit: int = 8) -> list[Message]:
        try:
            _, conv = await self._conversation(umo)
            out: list[Message] = []
            for m in self._history_of(conv)[-limit:]:
                if m.get("role") in ("user", "assistant"):
                    out.append(Message(role=m["role"], content=m.get("content")))
            return out
        except Exception:
            return []

    # ---------------- 生成 ----------------
    async def _gen(self, umo: str) -> str:
        try:
            pid = await self.context.get_current_chat_provider_id(umo=umo)
        except Exception:
            pid = None
        if not pid:
            logger.error("[主动] 拿不到当前会话的 provider")
            return ""
        st = self.state.get(umo) or {}
        idle = (time.time() - float(st.get("last_user") or time.time())) / 3600
        prompt = (
            "（现在是 %s，距离上次对话已经 %s。"
            "按当前人设主动发一条过去——接着上次的话题，或者只是打个招呼。"
            "一句话一件事，别写成整段；不要解释为什么突然发消息；"
            "不要提提醒、定时、系统、插件这些字眼。）"
            % (
                time.strftime("%m月%d日 %H:%M"),
                ("%.1f 小时" % idle) if idle > 0 else "一会儿",
            )
        )
        resp = await self.context.llm_generate(
            chat_provider_id=pid,
            prompt=prompt,
            system_prompt=await self._persona_text(umo) or None,
            contexts=await self._history_msgs(umo),
        )
        return (getattr(resp, "completion_text", "") or "").strip()

    # ---------------- 发送 ----------------
    @staticmethod
    def _split(text: str) -> list[str]:
        parts = re.findall(r"[^。？！~…\n]+[。？！~…]*", text)
        segs = [p.strip() for p in parts if p.strip()]
        return segs or [text.strip()]

    async def _append_history(self, umo: str, text: str) -> None:
        try:
            cid, conv = await self._conversation(umo)
            if not cid or conv is None:
                return
            h = self._history_of(conv)
            h.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
            await self.context.conversation_manager.update_conversation(
                umo, cid, history=h
            )
        except Exception as exc:
            logger.error("[主动] 写历史失败: %s", exc)

    async def _send(self, umo: str, text: str) -> None:
        segs = self._split(text)[: self.max_seg]
        for i, s in enumerate(segs):
            if i:
                await asyncio.sleep(random.uniform(*self.seg_gap))
            await self.context.send_message(umo, MessageChain([Plain(s)]))
        await self._append_history(umo, text)

    # ---------------- 主循环 ----------------
    @filter.on_astrbot_loaded()
    async def start_loop(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
        logger.info(
            "[主动] 条件式主动消息已启动：空闲 %.0f 小时触发、间隔 %.0f 小时、每天最多 %d 条、时段 %d~%d 点",
            self.idle_hours,
            self.cooldown_hours,
            self.daily_max,
            self.active_from,
            self.active_to,
        )

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.check_every)
            try:
                await self._tick()
            except Exception as exc:
                logger.error("[主动] 轮询出错: %s\n%s", exc, traceback.format_exc())

    async def _tick(self) -> None:
        now = time.time()
        day = time.strftime("%Y-%m-%d")
        hour = time.localtime(now).tm_hour
        if not (self.active_from <= hour < self.active_to):
            return
        for umo, st in list(self.state.items()):
            if not isinstance(st, dict) or not st.get("last_user"):
                continue
            idle = now - float(st["last_user"])
            if idle < self.idle_hours * 3600:
                continue
            if now - float(st.get("last_sent") or 0) < self.cooldown_hours * 3600:
                continue
            sent = st.setdefault("sent", {})
            if not isinstance(sent, dict):
                sent = {}
                st["sent"] = sent
            if sent.get(day, 0) >= self.daily_max:
                continue
            text = await self._gen(umo)
            if not text:
                continue
            await self._send(umo, text)
            st["last_sent"] = time.time()
            sent[day] = sent.get(day, 0) + 1
            self._save()
            logger.info(
                "[主动] 已主动发一条（空闲 %.1f 小时，今日第 %d 条）",
                idle / 3600,
                sent[day],
            )

    # ---------------- 手动触发（测试） ----------------
    @filter.command("主动")
    @filter.command("proactive")
    async def manual(self, event: AstrMessageEvent):
        """手动触发一次主动消息的生成 + 发送（不影响计数）。"""
        umo = event.unified_msg_origin
        text = await self._gen(umo)
        if not text:
            yield event.plain_result("（没有生成出内容）")
            return
        logger.info("[主动] 手动触发：%s", text[:40])
        await self._send(umo, text)
