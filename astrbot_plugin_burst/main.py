"""burst —— 消息防抖聚合：静默 20 秒 / 单条 ≥25 字才放行。

把「收到一条回一条」改成「静默一小会儿，把这批消息合成一条再回」：
用户连发三条时模型只会被触发一次，而它看到的是完整的上下文。

为什么挂在适配器层：包住 Platform.commit_event（消息进事件队列的唯一咽喉，
`platform.py:147` → `_event_queue.put_nowait`，aiocqhttp 适配器在
`aiocqhttp_platform_adapter.py:510` 调它）。
未到静默期的消息**根本不进流水线**，所以不存在「让位事件落不落历史」的问题 ——
这一轮从头到尾只有一个事件。

触发（谁先满足谁赢）：
  A 静默：距最后一条满 20 秒
  B 长消息：单条去空白后 ≥25 字符（标点、emoji 都算）

立刻放行、不进缓冲的：
  · 非真实消息（"对方正在输入"这类空通知）
  · 指令（文本以 / 开头）—— 否则 /stop、/记住 要等 20 秒
  · 「结构性」消息：语音/文件/引用/QQ表情/转发 —— 先把已挂起的那批出手，再原样放行

进缓冲的：纯文字 + 图片。
图片不会单独直通，而是**作为附件挂在聚合后的整条大消息上**
（合并文本里对应位置写 `[图片]`，Image 组件按原顺序接在 Plain 之后；
框架正是从 `event.message_obj.message` 里 `isinstance(comp, Image)` 取图的，
所以这样挂能被正常送去模型）。

合并后的消息形态：
    [14:03:12] 今天好累啊
    [14:03:15] 刚下课
    <system_reminder>Current datetime: 2026-10-12 14:03 (CST), Weekday: Monday</system_reminder>

  · 时间戳只写【时分秒】—— 带日期的时间戳容易被下游「按行首时间锚解析」的模块
    误认成候选锚（合法锚是分钟精度）
  · <system_reminder> 用【最后一条】的到达时刻（不是出手时刻），
    跟 flush 插件的规矩一致（"时间是收到消息那一刻"）
  · 载体用【最后一条】的事件对象 —— message_id / created_at 都是新的那条，
    pacer 的 _bump 判重和 _stamp_user 按内容匹配都天然对得上
"""

from __future__ import annotations

import asyncio
import re
import time

from astrbot.api import logger, star
from astrbot.api.event import filter

# ───────────────────── 可调参数 ─────────────────────
IDLE_SECONDS = 20.0      # A：距最后一条满这么久才出手
INSTANT_CHARS = 25       # B：单条去空白后 ≥ 这么多字符就立刻出手
# ───────────────────────────────────────────────────

ATTACHMENT_TYPES = ("image", "record", "video", "file")   # 仅作文档说明，判定用 ComponentType.value
REMINDER_RE = re.compile(r"<system_reminder>.*?</system_reminder>", re.S)
QUOTED_RE = re.compile(r"<Quoted Message>.*?</Quoted Message>", re.S)
WS_RE = re.compile(r"\s+")


def visible_chars(text: str) -> int:
    """字符数口径：去掉时间标记、引用块和空白之后，剩下几个字符（标点 emoji 都算）。"""
    t = REMINDER_RE.sub("", text or "")
    t = QUOTED_RE.sub("", t)
    return len(WS_RE.sub("", t))


def classify(event) -> tuple[str, list, bool]:
    """(文本, 图片组件列表, 有没有结构性组件)。

    ⚠️ 组件的 `type` 是 `ComponentType` 枚举，**值是大写**（"Plain"/"Image"/"Record"…），
    不是小写字符串 —— 直接跟 "image" 比会永远为 False（flush 插件里就踩过这个坑）。
    这里统一先取 .value 再转小写。

    可进缓冲的：Plain（文字）+ Image（图片，会作为附件挂在合并后的整条消息上）
    其余一律算「结构性」（语音/文件/引用/QQ表情/转发…）→ flush 已有的批 + 原样放行，
    免得合并时把这些东西弄丢。
    """
    text = (getattr(event, "message_str", "") or "").strip()
    images: list = []
    structural = False
    try:
        for comp in (event.message_obj.message or []):
            t = getattr(comp, "type", None)
            v = str(getattr(t, "value", t) or "").lower()
            if v == "plain":
                continue
            if v == "image":
                images.append(comp)
            else:
                structural = True
                break
    except Exception:
        structural = True          # 判不准就别聚合，宁可直通
    return text, images, structural


class Main(star.Star):
    def __init__(self, context, config=None) -> None:
        super().__init__(context, config)
        self._installed = False
        self._loop = None
        self.bursts: dict[str, dict] = {}

    # ───────────────────── 安装 ─────────────────────

    @filter.on_astrbot_loaded()
    async def install(self) -> None:
        if self._installed:
            return
        self._loop = asyncio.get_running_loop()
        n = 0
        for inst in self.context.platform_manager.get_insts():
            try:
                name = inst.meta().name
            except Exception:
                name = ""
            if name != "aiocqhttp":
                continue
            self._wrap(inst)
            n += 1
        self._installed = True
        logger.info(
            "[burst] 已接管 %d 个 aiocqhttp 适配器（静默 %.0fs / 单条 ≥%d 字）",
            n, IDLE_SECONDS, INSTANT_CHARS,
        )

    def _wrap(self, inst) -> None:
        orig = inst.commit_event

        def wrapper(event) -> None:
            try:
                self._on_event(event, orig)
            except Exception as exc:  # noqa: BLE001  聚合坏了也绝不能吞消息
                logger.warning("[burst] 聚合异常，本条直通：%s", exc)
                try:
                    orig(event)
                except Exception:
                    pass

        inst.commit_event = wrapper
        try:
            logger.info("[burst] 包住适配器 %s 的 commit_event", inst.meta().id)
        except Exception:
            pass

    # ───────────────────── 入口 ─────────────────────

    def _on_event(self, event, orig) -> None:
        umo = event.unified_msg_origin
        text, images, structural = classify(event)
        n_chars = visible_chars(text)

        if not n_chars and not images and not structural:   # 空文本通知（"对方正在输入"这类）
            orig(event)
            return

        if text.lstrip().startswith("/"):    # 指令：先把攒着的出手，再原样放行
            self._flush(umo, "指令")
            orig(event)
            return

        if structural:                        # 语音/文件/引用/表情：flush 批 + 原样放行
            self._flush(umo, "结构性消息")
            orig(event)
            return

        b = self.bursts.get(umo)
        if b is None:
            b = {"items": [], "timer": None, "event": None, "last_ts": 0.0, "orig": orig}
            self.bursts[umo] = b

        now = float(getattr(event, "created_at", None) or time.time())
        gap = now - b["last_ts"] if b["last_ts"] else 0.0
        b["items"].append((time.strftime("%H:%M:%S", time.localtime(now)), text, images))
        b["event"] = event
        b["last_ts"] = now
        if b["timer"] is not None:
            b["timer"].cancel()
            b["timer"] = None

        if n_chars >= INSTANT_CHARS:         # B：这条本身够长 → 立刻出手
            logger.info(
                "[burst] 单条 %d 字 ≥%d → 立刻出手（本批 %d 条）",
                n_chars, INSTANT_CHARS, len(b["items"]),
            )
            self._flush(umo, f"长消息 {n_chars} 字")
            return

        loop = self._loop or asyncio.get_event_loop()
        b["timer"] = loop.call_later(IDLE_SECONDS, self._on_timeout, umo)
        logger.info(
            "[burst] 攒住第 %d 条（本条 %d 字 / %d 图，距上条 %.1fs）→ %.0fs 后出手",
            len(b["items"]), n_chars, len(images), gap, IDLE_SECONDS,
        )

    def _on_timeout(self, umo: str) -> None:
        self._flush(umo, f"静默 {IDLE_SECONDS:.0f}s")

    # ───────────────────── 出手 ─────────────────────

    def _flush(self, umo: str, reason: str) -> None:
        b = self.bursts.pop(umo, None)
        if not b or not b["items"]:
            return
        if b["timer"] is not None:
            b["timer"].cancel()
            b["timer"] = None

        merged = self._compose(b["items"], b["last_ts"])
        longest = max((visible_chars(t) for _s, t, _i in b["items"]), default=0)
        n_imgs = sum(len(i) for _s, _t, i in b["items"])
        event = b["event"]
        try:
            from astrbot.api.message_components import Plain
            chain: list = [Plain(text=merged)]
            for _s, _t, imgs in b["items"]:     # 图片按原顺序挂在合并后的整条消息上
                chain.extend(imgs)
            event.message_str = merged
            event.message_obj.message = chain
        except Exception as exc:  # noqa: BLE001
            logger.warning("[burst] 替换消息内容失败（继续送原来的）：%s", exc)

        logger.info(
            "[burst] 出手（%s）：%d 条 / 最长 %d 字 / %d 图 → 合并 %d 字",
            reason, len(b["items"]), longest, n_imgs, len(merged),
        )
        try:
            b["orig"](event)
        except Exception as exc:  # noqa: BLE001
            logger.error("[burst] 送出失败（这条消息可能丢了）：%s", exc)

    # ───────────────────── 组装 ─────────────────────

    @staticmethod
    def _compose(items: list, last_ts: float) -> str:
        lines = []
        for stamp, txt, imgs in items:
            one = WS_RE.sub(" ", (txt or "").strip())
            if imgs:
                one = f"{one} [图片]" if one else "[图片]"
            lines.append(f"[{stamp}] {one}")
        t = time.localtime(last_ts or time.time())
        lines.append(
            "<system_reminder>Current datetime: %s (CST), Weekday: %s</system_reminder>"
            % (time.strftime("%Y-%m-%d %H:%M", t), time.strftime("%A", t))
        )
        return "\n".join(lines)
