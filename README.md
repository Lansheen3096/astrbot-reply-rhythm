# astrbot-reply-rhythm

三个把「对话节奏」当一等公民的 AstrBot 插件：**聚合 → 投递 → 主动**。

| 插件 | 作用 |
|---|---|
| [`astrbot_plugin_burst`](astrbot_plugin_burst) | 消息防抖聚合：静默 20 秒 / 单条 ≥25 字才放行，把一批消息合成一条再交给流水线 |
| [`astrbot_plugin_flush`](astrbot_plugin_flush) | 接管分段发送：用户插话就停发剩下的分段，并把历史改成「实际发出去的部分」 |
| [`astrbot_plugin_proactive`](astrbot_plugin_proactive) | 条件式主动消息：用户安静够久，bot 自己发一条 |

在 AstrBot v4.28.x + OneBot v11（适配器名 `aiocqhttp`，协议端 NapCat / SnowLuma 都实测过）上跑出来的。
三个插件互相独立，可以只装其中一个；建议 burst + flush 一起用，它们共享「一轮 = 一个事件」的前提。

## 安装

```bash
git clone https://github.com/<你的账号>/astrbot-reply-rhythm
cp -r astrbot-reply-rhythm/astrbot_plugin_* <AstrBot>/data/plugins/
# docker 部署：拷进宿主机挂载的数据目录后重启容器
docker compose restart astrbot
```

插件目录名要和 `metadata.yaml` 里的 `name` 一致。日志里带 `[burst]` / `[flush]` / `[主动]` 前缀，方便 grep。

## 1. astrbot_plugin_burst —— 消息防抖聚合

**要解决的问题**：用户连发三条，模型就被触发三次，每次看到的上下文都不全，回复也碎。

**做法**：包住 `Platform.commit_event`（消息进事件队列的唯一咽喉）。没到静默期的消息
**根本不进流水线** —— 所以这一轮从头到尾只有一个事件，不存在「让位事件落不落历史」的问题。

```
触发（谁先满足谁赢）：
  A 静默：距最后一条满 20 秒
  B 长消息：单条去掉空白后 ≥ 25 字符（标点、emoji 都算）

立刻放行、不进缓冲：
  · 空文本通知（"对方正在输入"这类）
  · 指令（以 / 开头）—— 否则 /stop、/new 也要等 20 秒
  · 结构性消息（语音/文件/引用/QQ表情/转发）—— 先把已挂起的那批出手，再原样放行

进缓冲：纯文字 + 图片。图片不单独直通，作为附件挂在聚合后的整条消息上。
```

合并后的消息形态（每行是一条消息 + 到达时刻，末尾一条 `system_reminder` 带上最后一条的到达时间）：

```
[14:03:12] 今天好累啊
[14:03:15] 刚下课
<system_reminder>Current datetime: 2026-10-12 14:03 (CST), Weekday: Monday</system_reminder>
```

参数在文件顶部：`IDLE_SECONDS = 20.0` / `INSTANT_CHARS = 25`。

⚠️ **坑**：`ComponentType` 枚举的值是**大写**（`"Image"` / `"Plain"`），直接跟 `"image"` 比恒为 `False`。
本仓库里统一先取 `.value` 再 `lower()`。

## 2. astrbot_plugin_flush —— 分段投递 + 插话打断

**要解决的问题**：`respond` 阶段按 `segmented_reply` 逐条发（`asyncio.sleep(8~15s)` 后发下一条），
那个循环里**没有任何中断检查**；而事件总线是一条入站消息一个 `create_task` 并发跑
⇒ 用户已经插话了，bot 还会把旧内容一条条发完。

**做法**（纯插件，不动镜像）：

- `event_message_type` 钩子：每收到一条真实入站消息，把该会话的「代号」+1
- `on_decorating_result`：截下 LLM 回复，按与核心相同的分段规则自己发；
  **每次发送前比对代号**，变了就丢弃剩下的分段
- 只认真正有内容的消息（空文本通知也带 user_id，用它计数会把刚生成的回复误判成"被打断"）
- 安全阀：出异常且一段都没发出去时，把消息链还原，交回核心按原样发送

### 两个必须一起处理的坑（血泪）

**① 发送必须丢进后台任务。** AstrBot 是洋葱模型：`process` 阶段发完 LLM 请求就 `yield`，
调度器去跑 `decorate` → `respond`，**回来再执行 `process` 的后半段 —— 而写会话历史就在那后半段里**。
插件若在 `decorate` 里原地 `await` 几十秒，这一步会被拖住/跳过 ⇒ 整轮不进历史。
⇒ `asyncio.create_task` 发出去，让流水线立刻返回。

**② 框架那条保存路径不一定成功。** v4.28.2 的 `InternalAgentSubStage._save_to_history`
开头就是 `if not llm_response.completion_text and not req.tool_calls_result and not user_aborted: return`
—— 实测每轮都是空跑（文本走 `result_chain`），库里的会话能停几个小时不动，模型一直拿旧上下文回话。
⇒ 接管发送的插件必须同时接管历史写入：

- 框架存过这轮 → 按内容对号找到那条 assistant，改写成「实际发出去的部分」+`（话没说完）`
- 框架没存 → 自己补写，且补写前要保证 `user`/`assistant` 交替

⚠️ **绝不能按位置盲改历史**：没写的时候"最后一条 assistant"可能是几小时前的旧记录；
`（话没说完）` 这个标记也别丢，否则下一轮会引用用户根本没看到的内容。

### 诊断开关

`touch /AstrBot/data/trace-stop.on` 开启对 `stop_event()` 与 `_save_to_history` 的调用栈追踪
（事件被 stop 过整轮也不进历史），删掉即关。

（文件里还有 `_reminder` 给 user 消息补真实时间戳的逻辑，配套的下游如果按行首时间锚解析要一起看。）

## 3. astrbot_plugin_proactive —— 条件式主动消息

人设里通常写着「等得够久了，我会自己发一条过去」，但框架里没有这条链路，
于是模型只能在对话里"编"自己发过。这个插件把这句话落实，顺便少一个幻觉来源。

```
IDLE_HOURS   = 5.0        # 用户安静这么久才考虑发
COOLDOWN_HOURS = 3.0      # 两条主动消息之间至少隔这么久
DAILY_MAX    = 2          # 每天上限
ACTIVE_FROM, ACTIVE_TO = 8, 23   # 允许主动的时段（含起不含止）
CHECK_EVERY  = 300        # 每 5 分钟看一次
SEG_GAP      = (6.0, 12.0)  # 分条发送间隔
MAX_SEG      = 3
```

- 内容由 LLM **按当前会话的人设**现写（`personas` 表里取），并带最近 8 条历史做上下文
- 状态文件：`/AstrBot/data/proactive_state.json`
- 手动触发：`/主动` 或 `/proactive`（走同一条生成 + 发送路径，不影响计数）

## 通用注意事项

- 三个插件都假设容器里的数据目录是 `/AstrBot/data`（proactive 的状态文件、flush 的诊断开关），非容器部署按需改。
- 只对 OneBot v11（平台名 `aiocqhttp`）生效。
- `flush` 的分段规则读的是框架自己的 `platform_settings.segmented_reply`（`interval` / `interval_method` / `regex` / `content_cleanup_rule`），
  没有自定义配置项 —— 想要什么节奏就在框架里配。
- 本仓库代码来自一个长期在跑的自用部署，注释里保留了踩坑原因，请当"经验笔记 + 可用代码"看。

## License

MIT
