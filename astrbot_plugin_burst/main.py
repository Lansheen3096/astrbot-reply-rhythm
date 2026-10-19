"""astrbot_plugin_burst —— 消息防抖聚合：静默 20 秒 / 单条 ≥25 字才放行。

设计稿：~/Documents/qqbotdev_advanced/触发结构改造-消息聚合与输入状态.md

为什么挂在适配器层：包住 Platform.commit_event（消息进事件队列的唯一咽喉，
`platform.py:147` → `_event_queue.put_nowait`，aiocqhttp 适配器在
`aiocqhttp_platform_adapter.py:510` 调它）。
未到静默期的消息**根本不进流水线**，所以不存在「让位事件落不落历史」的问题 ——
这一轮从头到尾只有一个事件。

触发（谁先满足谁赢）：
  A 静默：距最后一条满 20 秒
  B 长消息：单条去空白后 ≥25 字符（标点、emoji 都算）

★ 排队分支（2026-10-14 加，取代「0/N 静默丢整轮」）：
  **上一轮回复还在处理（pacer 的 in_flight）而且一段都还没发出去**时，
  新消息先不进流水线、不进上下文，挂在队里等那一轮发完，由 pacer 的 _round_end 回调放行。
  少了这一条，它会顶掉 pacer 的代号，把上一轮整轮掐掉（历史上的「实发 0/4 段」就是这么来的）。
  已经发出至少一段之后【维持原来的打断语义】—— 那种情况用户确实想掐掉后面那些。
  所以判定必须读 pacer 的 in_flight()：见 _busy_no_output() / release_pending()。

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

  · 时间戳只写【时分秒】—— 写成带日期的会被每天 05:00 记忆整理的行首时间锚校验
    误认成候选锚（合法锚是分钟精度），模型照抄就 bad_anchor 整条丢
  · <system_reminder> 用【最后一条】的到达时刻（不是出手时刻），
    跟 astrbot_plugin_pacer 的规矩一致（"时间是收到消息那一刻"）
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
HOLD_MAX_SECONDS = 300.0  # C：排队兜底上限 —— 只在【上一轮没正常收尾】时才触发
#  ⚠️ 别把它当"最长等待"来理解：正常排队要等上一轮把 20+ 段发完（3~8s 一段，实测最长 ~3.7 分钟），
#   所以这个值只是防"状态卡死"，要大于最长一轮的发送耗时。
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
    不是小写字符串 —— 直接跟 "image" 比会永远为 False（astrbot_plugin_pacer 里就踩了这个坑）。
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
        self.config = config or {}
        self._installed = False
        self._loop = None
        self.bursts: dict[str, dict] = {}

        # 运行参数：默认值与旧版硬编码一致，用户不动配置时行为不变
        self.idle_seconds = self._cfg_num("idle_seconds", IDLE_SECONDS)
        self.instant_chars = self._cfg_num("instant_chars", INSTANT_CHARS, int)
        self.hold_max_seconds = self._cfg_num("hold_max_seconds", HOLD_MAX_SECONDS)
        self.timezone_label = str(self.config.get("timezone_label") or "CST")

    def _cfg_num(self, key: str, default, cast=float):
        try:
            return cast(self.config.get(key, default))
        except (TypeError, ValueError):
            logger.warning("[burst] 配置项 %s 不是有效数字，用默认值 %s", key, default)
            return default

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
            n, self.idle_seconds, self.instant_chars,
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

        # ★ 排队分支：上一轮还在处理（pacer 的在飞状态）而且【一段都还没发出去】⇒
        #   这条先不进流水线、不进上下文，等上一轮回复发完由 pacer 回调放行。
        #   否则它会顶掉代号，把上一轮整轮掐掉 —— 历史上那次「实发 0/4 段」就是这么来的。
        if self._busy_no_output(umo):
            b = self._bucket(umo, orig)
            if structural:
                b["held_events"].append((event, orig))
                what = "结构性消息"
            else:
                now = float(getattr(event, "created_at", None) or time.time())
                b["items"].append(
                    (time.strftime("%H:%M:%S", time.localtime(now)), text, images)
                )
                b["event"] = event
                b["last_ts"] = now
                what = f"{n_chars} 字"
            if b["timer"] is not None:
                b["timer"].cancel()
                b["timer"] = None
            if b["hold_timer"] is None:
                loop = self._loop or asyncio.get_event_loop()
                b["hold_timer"] = loop.call_later(
                    self.hold_max_seconds, self._on_hold_timeout, umo
                )
            logger.info(
                "[burst] 上一轮还没发出第一段 → 这条排队等它发完（%s）；队里 %d 条文本 / %d 条结构性",
                what, len(b["items"]), len(b["held_events"]),
            )
            return

        if structural:                        # 语音/文件/引用/表情：flush 批 + 原样放行
            self._flush(umo, "结构性消息")
            orig(event)
            return

        b = self._bucket(umo, orig)

        now = float(getattr(event, "created_at", None) or time.time())
        gap = now - b["last_ts"] if b["last_ts"] else 0.0
        b["items"].append((time.strftime("%H:%M:%S", time.localtime(now)), text, images))
        b["event"] = event
        b["last_ts"] = now
        if b["timer"] is not None:
            b["timer"].cancel()
            b["timer"] = None

        if n_chars >= self.instant_chars:         # B：这条本身够长 → 立刻出手
            logger.info(
                "[burst] 单条 %d 字 ≥%d → 立刻出手（本批 %d 条）",
                n_chars, self.instant_chars, len(b["items"]),
            )
            self._flush(umo, f"长消息 {n_chars} 字")
            return

        loop = self._loop or asyncio.get_event_loop()
        b["timer"] = loop.call_later(self.idle_seconds, self._on_timeout, umo)
        logger.info(
            "[burst] 攒住第 %d 条（本条 %d 字 / %d 图，距上条 %.1fs）→ %.0fs 后出手",
            len(b["items"]), n_chars, len(images), gap, self.idle_seconds,
        )

    def _on_timeout(self, umo: str) -> None:
        self._flush(umo, f"静默 {self.idle_seconds:.0f}s")

    # ───────────────────── 与 pacer 的「排队」协作 ─────────────────────

    def _pacer(self):
        """拿 astrbot_plugin_pacer 的实例（同一个进程，按插件名查注册表）。"""
        try:
            md = self.context.get_registered_star("astrbot_plugin_pacer")
            return getattr(md, "star_cls", None) if md else None
        except Exception:
            return None

    def _busy_no_output(self, umo: str) -> bool:
        """上一轮是否「正在处理、且一段都还没发出去」—— 只有这种时候才排队。

        已经发出至少一段时保持原来的打断语义（用户确实想掐掉后面那些）。
        """
        p = self._pacer()
        if p is None:
            return False
        try:
            busy, sent = p.in_flight(umo)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[burst] 读 pacer 状态失败，按不排队处理：%s", exc)
            return False
        return bool(busy) and int(sent) == 0

    def _bucket(self, umo: str, orig) -> dict:
        b = self.bursts.get(umo)
        if b is None:
            b = {
                "items": [], "timer": None, "event": None, "last_ts": 0.0, "orig": orig,
                "held_events": [], "hold_timer": None,
            }
            self.bursts[umo] = b
        b.setdefault("held_events", [])
        b.setdefault("hold_timer", None)
        return b

    def release_pending(self, umo: str, reason: str = "上一轮已发完") -> None:
        """pacer 的 _round_end 回调：那一轮结束了，把排队的一批放出来。"""
        b = self.bursts.get(umo)
        if not b or (not b["items"] and not b.get("held_events")):
            return
        logger.info("[burst] 放行排队的一批（%s）", reason)
        self._flush(umo, reason)

    def _on_hold_timeout(self, umo: str) -> None:
        """兜底：上一轮一直没收尾（崩了 / 没回复），不能把消息永远卡在队里。"""
        b = self.bursts.get(umo)
        if b is not None:
            b["hold_timer"] = None
        logger.warning(
            "[burst] 排队超时（%.0fs）—— 先放行，可能是上一轮没正常收尾", self.hold_max_seconds
        )
        self._flush(umo, f"排队超时 {self.hold_max_seconds:.0f}s")

    # ───────────────────── 出手 ─────────────────────

    def _flush(self, umo: str, reason: str) -> None:
        b = self.bursts.pop(umo, None)
        if not b or (not b["items"] and not b.get("held_events")):
            return
        if b["timer"] is not None:
            b["timer"].cancel()
            b["timer"] = None
        hold = b.get("hold_timer")
        if hold is not None:
            hold.cancel()
            b["hold_timer"] = None

        if b["items"]:
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

        # 排队时攒下的【结构性】消息（语音/文件/引用…）不能合并，原样补发
        for ev, o in b.get("held_events") or []:
            try:
                logger.info("[burst] 补发排队的结构性消息（%s）", reason)
                o(ev)
            except Exception as exc:  # noqa: BLE001
                logger.error("[burst] 补发排队的结构性消息失败：%s", exc)

    # ───────────────────── 组装 ─────────────────────

    def _compose(self, items: list, last_ts: float) -> str:
        lines = []
        for stamp, txt, imgs in items:
            one = WS_RE.sub(" ", (txt or "").strip())
            if imgs:
                one = f"{one} [图片]" if one else "[图片]"
            lines.append(f"[{stamp}] {one}")
        t = time.localtime(last_ts or time.time())
        lines.append(
            "<system_reminder>Current datetime: %s (%s), Weekday: %s</system_reminder>"
            % (
                time.strftime("%Y-%m-%d %H:%M", t),
                self.timezone_label,
                time.strftime("%A", t),
            )
        )
        return "\n".join(lines)
