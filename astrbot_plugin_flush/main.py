"""接管分段发送 + 被新消息打断就停（correction 语义）。

背景（实测代码）：AstrBot 的 respond 阶段按分段发送（`asyncio.sleep(8~15s)` 后逐条发），
那个循环里**没有任何中断检查**；而事件总线是一条入站消息一个 `asyncio.create_task` 并发跑
⇒ 用户已经插话了，bot 还会把旧内容一条条发完。

本插件的做法（纯插件，不动镜像）：
  · `event_message_type` 钩子 —— 每收到一条入站消息，把该会话的"代"号 +1，
    并把这个代号盖在当时那条事件上
  · `on_decorating_result` —— 截下 LLM 回复，按与核心完全相同的分段规则自己发送；
    **每次发送前比对代号**，变了就说明用户插话了 ⇒ 剩下的分段全部丢弃
  · 丢弃后把历史里那条 assistant 消息改写成"实际发出去的部分"，免得她
    "记得"自己说过没发出去的话（否则下一轮会引用用户根本没看到的内容）

安全阀：出异常且**一段都没发出去**时，把消息链还原，交回核心按原样发送
（宁可发得不优雅，也不要静默吞消息）。
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re
import time
import traceback

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.core.message.components import At, Plain, Reply

DEFAULT_REGEX = r"[^\n]+"   # 兜底：按行切（切分只认换行）
ONLY_PUNCT_RE = re.compile(r"[。？！~…，、；：,.?!~\-—\s]+")   # 整段只有标点
PUNCT_TAIL_RE = re.compile(r"[。？！~…]+\s*$")                  # 段末/括号内的句末标点
BRACKETS = (("（", "）"), ("(", ")"))
MARK = "（话没说完）"     # 写进历史的标记：让她知道自己刚被打断
CFG_TTL = 30
TRACE_FLAG = "/AstrBot/data/trace-stop.on"   # 诊断开关（存在=开）


def _parse_interval(s: str) -> tuple[float, float]:
    try:
        a, b = [float(x) for x in str(s).replace(" ", "").split(",")[:2]]
        return (a, b)
    except Exception:
        return (8.0, 15.0)


class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        self.context = context
        self.gen: dict[str, int] = {}
        self.seen: dict[str, str] = {}   # umo -> 最近处理过的 message_id（防重复计数）
        # (umo, gen) -> (收到消息的时刻, 星期)。给历史里的 user 消息打真时间用
        self.round_ts: dict[tuple[str, int], tuple[str, str]] = {}
        self._tasks: set[asyncio.Task] = set()   # 后台发送任务（防止被 GC）
        self._cfg: dict | None = None
        self._cfg_ts = 0.0

    # ---------------- 配置（30 秒内复用） ----------------
    def _seg(self) -> dict:
        now = time.time()
        if self._cfg is None or now - self._cfg_ts > CFG_TTL:
            try:
                seg = (
                    (self.context.get_config() or {})
                    .get("platform_settings", {})
                    .get("segmented_reply", {})
                )
                self._cfg = {
                    "interval": _parse_interval(seg.get("interval", "8,15")),
                    "method": seg.get("interval_method", "random"),
                    "log_base": float(seg.get("log_base", 2.6)),
                    "regex": seg.get("regex") or DEFAULT_REGEX,
                    "cleanup": seg.get("content_cleanup_rule") or "",
                }
                self._cfg_ts = now
            except Exception as exc:
                logger.error("[flush] 读配置失败，用默认值: %s", exc)
                self._cfg = {
                    "interval": (8.0, 15.0),
                    "method": "random",
                    "log_base": 2.6,
                    "regex": DEFAULT_REGEX,
                    "cleanup": "",
                }
                self._cfg_ts = now
        return self._cfg

    # ---------------- 每次发送前的等待 ----------------
    def _interval(self, cfg: dict, text: str) -> float:
        if cfg["method"] == "log":
            wc = len(text.split()) if all(ord(c) < 128 for c in text) else len(
                [c for c in text if c.isalnum()]
            )
            base = cfg["log_base"] or 2.6
            i = math.log(wc + 1, base) if base > 1 else 1.0
            return random.uniform(i, i + 0.5)
        lo, hi = cfg["interval"]
        return random.uniform(lo, hi)

    # ---------------- 分段（照抄核心的规则） ----------------
    @staticmethod
    def _clean(seg: str, cleanup: str) -> str:
        """按配置的 content_cleanup_rule 清掉括号动作。

        ★ 括号【里面】若是句末标点结尾（"（笑。）"），把那个标点留在原地 ——
          否则删掉动作，这句话的句号就跟着一起没了。不无中生有，只留住模型写过的。
        """

        def _repl(m) -> str:
            s = m.group(0)
            inner = s
            for lb, rb in BRACKETS:
                if len(s) >= 2 and s.startswith(lb) and s.endswith(rb):
                    inner = s[len(lb) : len(s) - len(rb)]
                    break
            tail = PUNCT_TAIL_RE.search(inner)
            return tail.group(0).rstrip() if tail else ""

        try:
            return re.sub(cleanup, _repl, seg)
        except re.error:
            return seg

    def _split(self, text: str, cfg: dict) -> list[str]:
        """按【换行】切句子 —— 换行是唯一的切分依据。

        人设要求她"每句话单独占一行，两句话中间空一行"，所以模型的行边界就是
        消息边界。标点不再参与切分：从前按句末标点切，既会把一句话切碎，
        又会在她不写标点的时候整个失效。
        """
        src = text
        if cfg.get("cleanup"):
            # ★ 括号清洗在切分【之前】对全文做一次：动作可能跨行，
            #   先切再清的话，半个括号会被当成一条消息发出去。
            src = self._clean(src, cfg["cleanup"])
        try:
            parts = re.findall(cfg["regex"], src, re.DOTALL | re.MULTILINE)
        except re.error:
            logger.error("[flush] 分段正则无效，用默认式")
            parts = re.findall(DEFAULT_REGEX, src, re.DOTALL | re.MULTILINE)
        if not parts:
            parts = [src]
        out: list[str] = []
        pending = ""   # 攒着"还没找到归属"的标点
        for seg in parts:
            seg = seg.strip()
            if not seg:
                continue
            # ★ 纯标点段：切分正则会把它切出来（"他说（笑）。我懂了。" 里的 "。"），
            #   留着就会让她发一条只有一个句号的消息。并回前一段末尾。
            if ONLY_PUNCT_RE.fullmatch(seg):
                if out:
                    out[-1] += seg
                else:
                    pending += seg
                continue
            out.append(pending + seg)
            pending = ""
        if pending:
            if out:
                out[-1] += pending
            else:
                out.append(pending)   # 整条都是标点，照原样发，别静默吞掉
        return out

    # ---------------- 代号：每条入站消息 +1 ----------------
    @staticmethod
    def _is_real_message(event: AstrMessageEvent) -> bool:
        """只认真正有内容的消息。

        QQ/NapCat 会推一些空文本的通知事件（"对方正在输入" 这类），它们也带 user_id，
        如果拿它们计数，回复刚生成就会被误判成"被用户打断了"（实测踩过）。
        """
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

    async def _bump(self, event: AstrMessageEvent) -> None:
        umo = event.unified_msg_origin
        if not self._is_real_message(event):
            return  # 空通知事件不算"插话"
        # 同一条消息可能被流水线走两遍（waking_check / runner 各一次），
        # 只按 message_id 计一次，否则回复刚生成就会被误判成"被打断"
        mid = ""
        try:
            mid = str(event.message_obj.message_id or "")
        except Exception:
            mid = ""
        if mid and self.seen.get(umo) == mid:
            return
        if mid:
            self.seen[umo] = mid
        g = self.gen.get(umo, 0) + 1
        self.gen[umo] = g
        event.set_extra("pacer_gen", g)
        self.round_ts[(umo, g)] = (
            time.strftime("%Y-%m-%d %H:%M"),
            time.strftime("%A"),
        )
        if len(self.round_ts) > 300:  # 别让这张表无限长
            for k in sorted(self.round_ts)[:150]:
                self.round_ts.pop(k, None)
        logger.info("[flush] 新一代 gen=%d（%s）", g, (event.message_str or "")[:20])

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE)
    async def bump_private(self, event: AstrMessageEvent) -> None:
        await self._bump(event)

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def bump_group(self, event: AstrMessageEvent) -> None:
        await self._bump(event)

    # ---------------- 历史修正（含"框架没存就我们补"） ----------------
    @staticmethod
    def _key(text: str) -> str:
        return re.sub(r"\s+", "", text or "")[:24]

    @staticmethod
    def _text_of(msg: dict) -> str:
        c = msg.get("content")
        if isinstance(c, list):
            return "".join(x.get("text", "") for x in c if isinstance(x, dict))
        return str(c or "")

    @staticmethod
    def _reminder(ts: str | None = None, wd: str | None = None) -> str:
        """这一轮 user 消息要带的时间标记。

        时间是【收到用户这条消息的那一刻】（_bump 里记的），不是写历史的时刻 ——
        分段发送 + TTS 会让写入时刻晚几十秒到几分钟。
        """
        return "<system_reminder>Current datetime: %s (CST), Weekday: %s</system_reminder>" % (
            ts or time.strftime("%Y-%m-%d %H:%M"),
            wd or time.strftime("%A"),
        )

    def _round_time(self, umo: str, gen) -> tuple[str | None, str | None]:
        """取这一轮的时间（收到消息那一刻）。取不到就返回 (None, None)。"""
        try:
            ts, wd = self.round_ts.get((umo, int(gen)), (None, None))
            return ts, wd
        except Exception:
            return None, None

    def _stamp_user(self, h: list, umo: str, gen, user_text: str) -> bool:
        """给这一轮的 user 消息补上真实时间（框架自己存的那条不带时间）。

        只认【内容对得上号】的那条：拿本轮用户文本的 key 去匹配最后一条 user 消息，
        对不上就不动 —— 历史上因为按位置盲改，覆盖过旧的记录。
        返回是否真的打上了。
        """
        key = self._key(user_text)
        if not key:
            return False
        for i in range(len(h) - 1, -1, -1):
            if h[i].get("role") != "user":
                continue
            if key not in self._key(self._text_of(h[i])):
                return False  # 对不上号，宁可不动
            content = h[i].get("content")
            if not isinstance(content, list):
                content = [{"type": "text", "text": str(content or "") + " "}]
                h[i]["content"] = content
            if any(
                "<system_reminder>" in (b.get("text") or "")
                for b in content
                if isinstance(b, dict)
            ):
                return False  # 这条已经带时间了
            ts, wd = self._round_time(umo, gen)
            content.append({"type": "text", "text": self._reminder(ts, wd)})
            return True
        return False

    async def _ensure_history(
        self,
        event: AstrMessageEvent,
        user_text: str,
        reply_full: str,
        sent: list[str],
        total: int,
        gen=None,
    ) -> None:
        """保证这一轮真的进了历史。

        两个目的：
          ① 打断时把她的记录截成"实际发出去的"，别让她以为自己说完了；
          ② 框架那条保存路径不一定成功（实测 completion_text 为空时会直接 return），
             那就我们自己补上——否则她整晚都在拿旧上下文回话。

        安全底线：**只动能对上号的条目**，绝不按位置盲改（曾经把几条旧的 assistant
        记录覆盖掉过），宁可不动也不能改错。
        """
        umo = event.unified_msg_origin
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return
            conv = await cm.get_conversation(umo, cid)
            if conv is None:
                return
            h = conv.history
            if isinstance(h, str):
                h = json.loads(h or "[]")
            if not isinstance(h, list):
                logger.error("[flush] 历史格式看不懂: %r", type(h))
                return

            sent_text = "".join(sent)
            key = self._key(reply_full)

            # ① 框架若已存了本轮（完整版），按内容对上号再截断
            idx = -1
            for i in range(len(h) - 1, -1, -1):
                if h[i].get("role") != "assistant":
                    continue
                if key and key in self._key(self._text_of(h[i])):
                    idx = i
                break  # 只看最后一条 assistant
            if idx >= 0:
                if sent_text:
                    h[idx]["content"] = [{"type": "text", "text": sent_text + MARK}]
                else:
                    del h[idx]
                stamped = self._stamp_user(h, umo, gen, user_text)
                await cm.update_conversation(umo, cid, history=h)
                logger.info(
                    "[flush] 历史截断：实发 %d/%d 段（user 打时间：%s）",
                    len(sent),
                    total,
                    stamped,
                )
                return

            # ② 框架没存 → 我们自己补这一轮
            # ★ 只丢【真正挂在尾巴上、后面没有回复】的那条 user 消息。
            #   从前是"找最后一条 user 消息"再比内容 —— 但框架其实一次都没存过历史
            #   （实测 220/220 全走这条补写路径），尾巴上永远是 [.., U, A]，
            #   于是每轮都删掉用户上一轮真实发过的消息，历史里 user 恒为 1 条。
            if h and h[-1].get("role") == "user":
                tail_user = self._text_of(h[-1])
                if self._key(tail_user) != self._key(user_text):
                    del h[-1]
                    logger.info(
                        "[flush] 丢掉尾巴上没人回的 user 消息（%d 字）", len(tail_user)
                    )
            # 当前这条已经在了（框架存了用户那半）就别重复写
            last_user = ""
            for i in range(len(h) - 1, -1, -1):
                if h[i].get("role") == "user":
                    last_user = self._text_of(h[i])
                    break
            if self._key(last_user) != self._key(user_text):
                h.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_text + " "},
                            {
                                "type": "text",
                                "text": self._reminder(*self._round_time(umo, gen)),
                            },
                        ],
                    }
                )
            if sent_text:
                # 同一轮被调用两次的话，别把回复也写两遍
                dup = (
                    bool(h)
                    and h[-1].get("role") == "assistant"
                    and self._key(self._text_of(h[-1])) == self._key(sent_text)
                )
                if not dup:
                    h.append(
                        {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "text",
                                    "text": sent_text
                                    + (MARK if len(sent) < total else ""),
                                }
                            ],
                        }
                    )
            await cm.update_conversation(umo, cid, history=h)
            logger.info(
                "[flush] 框架没存这轮，已由我补写历史：实发 %d/%d 段", len(sent), total
            )
        except Exception as exc:
            logger.error("[flush] 写历史失败: %s\n%s", exc, traceback.format_exc())

    # ---------------- 临时诊断：谁把事件 stop 了 ----------------
    @filter.on_astrbot_loaded()
    async def install_stop_trace(self) -> None:
        """存在标记文件时追踪 stop_event() 的调用来源。

        起因：AstrBot 保存历史前有 `if not event.is_stopped()` 的判定，
        事件被 stop 过 ⇒ 整轮不进历史。查不到谁干的时先装追踪。
        开关：`touch /AstrBot/data/trace-stop.on`（重启后生效），删掉即关。
        """
        if not os.path.exists(TRACE_FLAG):
            return
        try:
            from astrbot.core.platform.astr_message_event import (
                AstrMessageEvent as _E,
            )
            if getattr(_E, "_pacer_stop_traced", False):
                return
            _orig = _E.stop_event

            def _traced(self, *a, **k):
                try:
                    logger.warning(
                        "[trace] stop_event() 调用栈:\n%s",
                        "".join(traceback.format_stack()[-9:-1]),
                    )
                except Exception:
                    pass
                return _orig(self, *a, **k)

            _E.stop_event = _traced
            _E._pacer_stop_traced = True
            logger.warning("[trace] stop_event 追踪已开启")
        except Exception as exc:
            logger.error("[trace] 装不上: %s", exc)

    # ---------------- 临时诊断：历史保存为什么没发生 ----------------
    @filter.on_astrbot_loaded()
    async def install_save_trace(self) -> None:
        """追踪 `InternalAgentSubStage._save_to_history` 的调用与返回。

        用来分清两种可能：① 根本没被调用（保存前的判定没过）；② 调了但提前 return。
        """
        if not os.path.exists(TRACE_FLAG):
            return
        try:
            from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import (
                InternalAgentSubStage as _S,
            )
            if getattr(_S, "_pacer_save_traced", False):
                return
            _orig = _S._save_to_history

            async def _traced(
                self, event, req, llm_response, all_messages, runner_stats,
                user_aborted=False,
            ):
                try:
                    logger.warning(
                        "[trace] _save_to_history 进入: conv=%s all_msgs=%d role=%s text_len=%d aborted=%s",
                        bool(getattr(req, "conversation", None)),
                        len(all_messages or []),
                        getattr(llm_response, "role", None),
                        len(getattr(llm_response, "completion_text", "") or ""),
                        user_aborted,
                    )
                except Exception:
                    pass
                try:
                    r = await _orig(
                        self, event, req, llm_response, all_messages, runner_stats,
                        user_aborted,
                    )
                    logger.warning("[trace] _save_to_history 正常返回")
                    return r
                except Exception as exc:
                    logger.error(
                        "[trace] _save_to_history 抛异常: %r\n%s",
                        exc,
                        traceback.format_exc(),
                    )
                    raise

            _S._save_to_history = _traced
            _S._pacer_save_traced = True
            logger.warning("[trace] _save_to_history 追踪已开启")
        except Exception as exc:
            logger.error("[trace] 装不上保存追踪: %s", exc)

    # ---------------- 接管发送 ----------------
    @filter.on_decorating_result()
    async def takeover(self, event: AstrMessageEvent) -> None:
        result = event.get_result()
        if result is None or not result.chain:
            return
        if os.path.exists(TRACE_FLAG):
            try:
                logger.warning(
                    "[trace] 接管点 is_stopped=%s（True 就意味着这轮不会被存进历史）",
                    event.is_stopped(),
                )
            except Exception:
                pass
        if not result.is_llm_result():
            return  # 只管 LLM 的回复，指令回复走核心原路
        if (event.message_str or "").strip().startswith("/"):
            return

        umo = event.unified_msg_origin
        cfg = self._seg()
        my_gen = event.get_extra("pacer_gen")
        if my_gen is None:
            my_gen = self.gen.get(umo, 0)

        headers = [c for c in result.chain if isinstance(c, (Reply, At))]
        segs: list[list] = []
        for comp in result.chain:
            if isinstance(comp, (Reply, At)):
                continue
            if isinstance(comp, Plain):
                for part in self._split(comp.text, cfg):
                    segs.append([Plain(part)])
            else:
                segs.append([comp])  # 非文本段（图片/语音等）自己一段，不分
        if not segs:
            return

        snapshot = list(result.chain)
        try:
            result.chain.clear()  # 核心别再发了，下面我们自己发
        except Exception:
            return

        # ★ 关键：发送放到后台任务里，**不要阻塞流水线**。
        # AstrBot 是洋葱模型（process 阶段 yield → 跑完 respond 再回来执行 process 后半段），
        # 而"写会话历史"就在那后半段里。在这里原地 await 几十秒会把那一步卡掉 ⇒ 整轮不进历史。
        full_text = "".join(c.text for c in snapshot if isinstance(c, Plain))
        user_text = (event.message_str or "").strip()
        task = asyncio.create_task(
            self._send_segments(
                event, result, segs, list(headers), my_gen, umo, cfg, full_text, user_text
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send_segments(
        self,
        event: AstrMessageEvent,
        result,
        segs: list[list],
        head: list,
        my_gen,
        umo: str,
        cfg: dict,
        full_text: str,
        user_text: str,
    ) -> None:
        sent: list[str] = []
        try:
            for seg in segs:
                text = seg[0].text if isinstance(seg[0], Plain) else ""
                await asyncio.sleep(self._interval(cfg, text))
                if self.gen.get(umo, 0) != my_gen:
                    logger.info(
                        "[flush] 被打断 —— 已发 %d/%d 段，剩下的丢弃", len(sent), len(segs)
                    )
                    break
                await event.send(result.derive([*head, *seg]))
                if head:
                    head.clear()
                if text:
                    sent.append(text)
        except Exception as exc:
            logger.error("[flush] 发送出错: %s\n%s", exc, traceback.format_exc())
            if not sent and full_text:
                # 一段都没发出去 → 退化成整条发出，别静默吞消息
                try:
                    await event.send(result.derive([Plain(full_text)]))
                    sent.append(full_text)
                except Exception as exc2:
                    logger.error("[flush] 兜底整条发送也失败: %s", exc2)
        # 不管有没有被全部发完，都走一遍：框架那条保存路径不一定成功
        await self._ensure_history(
            event, user_text, full_text, sent, len(segs), my_gen
        )
