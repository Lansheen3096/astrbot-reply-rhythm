# astrbot-reply-rhythm

三个把「对话节奏」当一等公民的 AstrBot 插件：**聚合 → 投递 → 主动**。

| 插件 | 作用 |
|---|---|
| [`astrbot_plugin_burst`](astrbot_plugin_burst) | 消息防抖聚合：静默 20 秒 / 单条 ≥25 字 / 上一轮还没发出第一段时先排队，把一批消息合成一条再交给流水线 |
| [`astrbot_plugin_pacer`](astrbot_plugin_pacer) | 接管分段发送：用户插话就停发剩下的分段，并把历史截成「实际发出去的部分」 |
| [`astrbot_plugin_proactive`](astrbot_plugin_proactive) | 条件式主动消息：用户安静够久，bot 自己发一条 |

在 AstrBot v4.28.x + OneBot v11（适配器名 `aiocqhttp`，协议端 NapCat / SnowLuma 都实测过）上跑出来的。

> **0.1.0 → 0.2.0 更名**：原来的 `astrbot_plugin_flush` 已更名为 **`astrbot_plugin_pacer`**
> —— 它现在不只负责「打断时清空剩余分段」，还负责怎么切分。**升级前请先删掉旧目录**
> （插件名换了之后框架不认旧名，两份同时放着会重复接管发送）。

**依赖关系**：`proactive` 独立。`burst` 与 `pacer` 现在是**互相依赖**的（`pacer` 对外暴露
「这一轮在飞吗 / 已经发出去几段」，`burst` 拿它决定新消息是排队还是打断）——
要么两个都装，要么都不装；只装一个时另一个的能力会缺一半，代码里做了降级（查不到对方
就按「不排队 / 直接打断」处理），不会报错但语义不完整。

## 安装

```bash
git clone https://github.com/Lansheen3096/astrbot-reply-rhythm
cp -r astrbot-reply-rhythm/astrbot_plugin_* <AstrBot>/data/plugins/
# docker 部署：拷进宿主机挂载的数据目录后重启容器
docker compose restart astrbot
```

插件目录名要和 `metadata.yaml` 里的 `name` 一致。日志里带 `[burst]` / `[pacer]` / `[主动]` 前缀，方便 grep。

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

### 排队：上一轮还没开口时，插话不算打断

**要解决的问题**：bot 正在处理上一条，用户又发了一条 —— 如果直接当「插话打断」，
上一轮会被取消，用户等于白等一次生成。

**做法**：新消息到达时先问 `pacer`：`(在飞吗, 已经发出去几段)`。

```
在飞 且 已发出 0 段  → 新消息【排队】，上一轮继续跑（它还没开口，不算被打断）
在飞 且 已发出 ≥1 段 → 正常打断语义（用户确实想掐掉后面那些）
不在飞               → 走原来的聚合逻辑
```

上一轮收尾（发完 / 被打断 / 没东西可发）时，`pacer` 回调 `burst.release_pending()` 放行排队的那批。
`HOLD_MAX_SECONDS` 是排队上限，超时无条件放行 —— 万一 `pacer` 那边出问题，不要把人卡死。

合并后的消息形态（每行是一条消息 + 到达时刻，末尾一条 `system_reminder` 带上最后一条的到达时间）：

```
[14:03:12] 今天好累啊
[14:03:15] 刚下课
<system_reminder>Current datetime: 2026-10-12 14:03 (CST), Weekday: Monday</system_reminder>
```

参数在文件顶部：`IDLE_SECONDS = 20.0` / `INSTANT_CHARS = 25` / `HOLD_MAX_SECONDS`。

⚠️ **坑**：`ComponentType` 枚举的值是**大写**（`"Image"` / `"Plain"`），直接跟 `"image"` 比恒为 `False`。
本仓库里统一先取 `.value` 再 `lower()`。

## 2. astrbot_plugin_pacer —— 分段投递 + 插话打断

**要解决的问题（一）**：`respond` 阶段按 `segmented_reply` 逐条发（`asyncio.sleep(8~15s)` 后发下一条），
那个循环里**没有任何中断检查**；而事件总线是一条入站消息一个 `create_task` 并发跑
⇒ 用户已经插话了，bot 还会把旧内容一条条发完。

**要解决的问题（二）**：框架的切分规则只在「句末标点」上下刀，模型不写标点时整个失效，
写了又会被切得过碎；括号动作还会被从中间劈开。

**做法**（纯插件，不动镜像）：

- `event_message_type` 钩子：每收到一条真实入站消息，把该会话的「代号」+1
- `on_decorating_result`：截下 LLM 回复，按自己的规则切分后自己发；
  **每次发送前比对代号**，变了就丢弃剩下的分段
- 只认真正有内容的消息（空文本通知也带 user_id，用它计数会把刚生成的回复误判成"被打断"）
- 安全阀：出异常且一段都没发出去时，把消息链还原，交回核心按原样发送

### 原子切分器

`_atoms` 先把整段拆成「括号块 / 普通片段」两类原子 —— 括号块**整体独立成一条**，
按配对深度找闭合（`（）` `()` `【】` `「」` `《》` `〔〕`），没闭合就把余下当普通文本。

`_cut_end` 在普通片段里找下一次可以下刀的位置：**标点跟着前一句走**
（`。？！—— …… ♡ ? ! … ~ ～` 以及换行）。后面没有实义内容就不切，避免切出一段光秃秃的标点，
也避免把括号里的句末标点连带着吃掉。

规则集中在 `SPLIT_SYMBOLS` / `BRACKETS` 两个常量里，想改节奏改这两处即可。

### 在飞状态（给 burst 用的）

```python
in_flight(umo) -> (还在处理吗, 已经发出去几段)     # burst 拿它决定排队还是打断
_round_begin / _mark_sent / _round_end            # 一轮的起、记数、收尾
```

`_round_end` 会回调 `astrbot_plugin_burst` 放行排队的那批；找不到对方就安静跳过。
按插件名查注册表，所以**改了目录名要两边一起改**。

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
- **补写 user 条目时要连图片附件路径一起写**：框架给每张图配的 `[Image Attachment: path …]`
  只在「框架自己保存成功」时才落库，走补写这条路的话不补就当场丢 —— 下一轮要引用这张图
  （例如需要 `image_ref` 的图片工具）时，模型手里没有可抄的东西，只能自己编一条图片链接

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
IDLE_HOURS   = 2.0        # 用户安静这么久才考虑发
COOLDOWN_HOURS = 3.0      # 两条主动消息之间至少隔这么久
DAILY_MAX    = 5          # 每天上限
ACTIVE_FROM, ACTIVE_TO = 8, 23   # 允许主动的时段（含起不含止）
CHECK_EVERY  = 300        # 每 5 分钟看一次
SEG_GAP      = (6.0, 12.0)  # 分条发送间隔
MAX_SEG      = 3
```

（`IDLE_HOURS` / `DAILY_MAX` 是自用部署的档位，想要更克制就往下调。）

- 内容由 LLM **按当前会话的人设**现写（`personas` 表里取），并带最近 8 条历史做上下文
- 状态文件：`/AstrBot/data/proactive_state.json`
- 手动触发：`/主动` 或 `/proactive`（走同一条生成 + 发送路径，不影响计数）

## 通用注意事项

- 三个插件都假设容器里的数据目录是 `/AstrBot/data`（proactive 的状态文件、pacer 的诊断开关），非容器部署按需改。
- 只对 OneBot v11（平台名 `aiocqhttp`）生效。
- `pacer` 的分段规则**不再读**框架 `platform_settings.segmented_reply.regex` —— 切分完全由插件自己的
  `SPLIT_SYMBOLS` / `BRACKETS` 决定。框架那边仍然生效的是 `interval`（分条间隔）、
  `interval_method`（`random` / `log`）和 `content_cleanup_rule`（发送前对全文做一次括号清洗）。
  想改节奏先看这三处。
- 本仓库代码来自一个长期在跑的自用部署，注释里保留了踩坑原因，请当"经验笔记 + 可用代码"看。

## License

MIT
