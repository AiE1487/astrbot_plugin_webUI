# 更新日志

## v0.4.2（2026-11-21）

统计栏常驻：缓存命中率/上下文用量不再「发消息后才出现」，刷新/切会话后恢复最近一次统计。

- **统计栏常驻渲染**（composer）：`.stats-bar` 两项恒渲染，无数据（新会话/
  agent_stats 缺省/cache_rate=null，`Number(null)` 会塌成 0 故先判空）以
  "--" 占位（「缓存 --」「上下文 --」）；移除 `.stats-bar:empty` 空态隐藏。
  此前 stats 仅由生成期 SSE 事件写入前端内存，刷新/切会话后 statsOf 为空 →
  栏清空 + `:empty` 隐藏，故只在发消息后可见。
- **历史统计恢复**：后端历史 bot 行附带 `stats` 字段（conversation_service
  复用 `stats_from_agent_stats`，agent_stats 缺失的 v0.4.0 前旧行/非 LLM
  回复不造值）；前端 normalizeMessage 白名单透传 stats，页1历史加载用最后
  一条带 stats 的 assistant 消息恢复 statsByCid（内存已有 live 值时不覆盖，
  流中较新值优先），降级轮询落定路径 force 覆盖（该模式无 SSE，seeding 是
  唯一写入方）。
- **mock 同构**：mock 历史（c1/c2）与流式落定消息补 stats，c2 演示
  cache_rate=null 的「缓存 --」占位；getHistory 透传。
- 自测 343 项断言全过（新增 C20-1 历史 stats 映射）。

## v0.4.1（2026-11-21）

修复附件预览破图（composer 把 text 附件的 preview 内容摘要当图片 URL 渲染）。

- **composer 预览条**：缩略图 img 仅在 `kind === "image"` 且有 preview 时渲染
  （契约中 text 附件 preview 是前 500 字内容摘要，此前被塞进 `<img src>` 触发
  浏览器破图 + 文件名 alt 换行）；img 换入时回收上传期占位图标，消除双框；
  preview 加载失败回退占位图标而非破图（`buildThumbImg` onerror）。
- **消息气泡缩略图**：`.attach-thumb` img 加载失败同样回退占位图标；补
  `.attach-thumb .ap-file-icon` 居中样式（原回退 span 只有内联宽高、无样式）。
- **灯箱**：text 附件点击改为文本块呈现（`pre.lb-text`，此前同样把内容摘要
  当 img src 破图）；图片灯箱占位/原图失败状态机——preview 失败等原图裁决，
  原图也失败才回退占位图标，preview 存活时原图失败不打断预览。
- 自测 342 项断言全过；浏览器 mock 实测：text chip 无破图、image chip 单缩略
  图、灯箱三态（文本块/正常图/损坏回退）全部符合预期。

## v0.4.0（2026-11-21）

多功能特性集（前后端，7 项）：多轮防抖合并发送、LLM 媒体实时上屏、后台回复
恢复、思考自动滚底、非流式配置适配、缓存/上下文统计栏、工具调用默认折叠。

- **多轮防抖合并发送**：输入区发送按钮左侧新增「并入下一条」按钮（手写 merge
  SVG 图标）——生成中（busy）也可把当前输入文本 + 就绪附件并入按会话的合并
  队列，队列消息以「待合并」徽标上屏、可单独移除；按原发送按钮/Enter 时队列
  各项与当前输入按空行拼接、附件拼接到队尾一次发出。合并发生在 composer 组装
  层，`sendMessage` 签名不变；成功后清队列、失败保留可重试。
- **LLM 媒体实时上屏**：back_queue 媒体帧（image/record/video/file，LLM 经
  send_message_to_user 发图/发文件）注册成功后逐帧发 SSE `media` 事件（载荷
  `{"attachment": public_meta}`，kind 归一 image|file，image 帧带 PIL 缩略
  preview）；前端流式占位内按 file_id 去重增量渲染附件，done 后透传进终态
  消息。任意后缀/无后缀文件按 file 卡片（名称/大小/mime）展示，图片可点开
  lightbox 预览。历史 part 与 save_bot_history 行为零变化。
- **后台回复支持**：`GET conversations` 非 legacy 项新增 `gen_id`（busy 时为
  进行中的 gen_id）；前端装载会话列表后对 busy 且有 gen_id 且本地无活跃 gen
  的会话自动 `setActiveGen` + `restoreStream`——关标签页/换浏览器回来不依赖
  sessionStorage 即可恢复订阅续流（服务端缓冲保留 ≥5 分钟，TTL 过期流 404
  时补拉一次最新历史 + 横幅提示）。生成任务本就在服务端与连接解耦，关闭窗口
  不中断生成。
- **思考内容自动滚到最新**：流式展开态（含手动点开）每次增量把
  `.reasoning-body` 滚到底（该容器本就有 max-height:320px + overflow-y:auto）。
- **非流式配置适配**：新增 `read_streaming_response(context)` 回读宿主
  `provider_settings.streaming_response`（桩/缺失兜底 True）作为注入
  `flags.enable_streaming`——原硬编码 True 等于强制流式，覆盖宿主配置；
  `GET config` 新增 `streaming_response` 透传；前端 `state.streamingResponse`
  装载，为 false 时流式占位等待行显示「{人格名} 正在回复中」。**升级行为
  变化**：宿主 `provider_settings.streaming_response` 默认 False，升级后插件
  默认显示「{人格名} 正在回复中」、回复生成完成后整段上屏，需逐字流式的
  用户请在宿主配置开启该项；官方 WebChat 不发送 enable_streaming（webchat
  场景恒流式），本插件与其语义有意不同。
- **缓存/上下文统计栏**：agent_stats 帧现额外发 SSE `stats` 事件
  `{cache_rate, context_tokens, max_context_tokens, input_cached, input_other,
  output}`（cache_rate = input_cached/(input_cached+input_other)，分母 0 为
  null；max_context_tokens 现阶段恒 null——宿主 per-provider
  max_context_tokens 不随 AgentStats 帧携带，前端有 noMax 兜底文案）；
  composer 顶部新增 `.stats-bar` 窄横栏（20px/11px/tabular-nums，:empty
  隐藏），显示「缓存 87% · 上下文 12.4k」。
- **工具调用默认折叠**：`createToolPanel` isOpen 初始 false，并移除 update()
  的自动开合逻辑（手动开合保留，RUNNING/DONE 头部可见）。
- **兼容性**：新增 SSE 事件类型 `media`/`stats` 与 REST 字段
  （`conversations[].gen_id`、`config.streaming_response`）均为增量字段，
  对独立端口与 bridge 两条传输链路无破坏性变更，旧前端忽略未知事件/字段。
- **i18n**：新增 `composer.mergeTip/queuedBadge/queuedRemove/replying` 与
  `stats.cache/context/contextNoMax`，zh/en 双语字典与 `.astrbot-plugin/i18n/`
  两侧键集合对齐；selftest 共 342 项断言全过。

## v0.3.6（2026-11-15）

用户显示名可自定义 + 宿主「用户识别」联动（前后端，1 新端点）。

- **新配置** `user_display_name`（string，默认 `USER`）：对话界面用户消息的
  显示名。前端经新增 `GET config` 端点装载（内嵌/独立端口共用，routes.py +
  standalone `_API_ROUTES` 各入一条；启动时拉取，改动后刷新页面生效）。
- **用户识别联动**：宿主 `provider_settings.identifier`（用户识别）开启时，
  插件命名空间 webchat 事件的 sender 身份被改写为配置名，随宿主
  `_append_system_reminders` 前缀进系统提示（"User ID: <名字>, Nickname:
  <名字>"），模型由此识别用户身份。实现为 main.py
  `@filter.platform_adapter_type(WEBCHAT)` 事件监听（ProcessStage 先于 LLM
  请求执行，不 yield 不停事件），核心逻辑在
  `pipeline_bridge.apply_sender_identity`（parse_umo 门控 + 桩环境降级）。
  **不改注入 creator**——宿主 webchat 适配器从队列 tuple 首元素同时派生
  sender 与 session_id/umo（webchat_adapter.py convert_message 实源），改
  creator 会搬家会话命名空间、断掉既有绑定，故走事件监听改写 sender 两字段。
- **i18n**：`msg.roleUser`（"USER"）保留为前端回退默认；配置名为空时回退。
- selftest 新增 9 项断言（C6a–f 联动语义 / E50–51 GET config / L-23 独立
  端口路由表），共 323 项。

## v0.3.5（2026-11-15）

独立端口登录卡的文案与表单布局微调（纯前端，无逻辑改动）。

- **文案**：登录卡提示语由「请输入访问密码（独立入口鉴权；留空密码时仅本机
  可访问）」改为「已配置访问授权码」；同步 `app.js` 兜底文案与 zh-CN / en-US
  双语字典（en 译作 "An access authorization code has been configured"）。
- **布局**：登录卡的密码输入框与「进入」按钮水平居中——表单改 flex 纵向布局，
  输入框 / 按钮等宽 `min(240px, 100%)`，窄屏自适应收窄；错误提示文案同步居中。

## v0.3.4（2026-11-14）

输入文字后发送按钮不点亮的修复（纯前端，1 文件）。

- **修复**：发送按钮的 `disabled` 只在 `updateButtons()` 里刷新，触发时机为
  附件变化 / store 通知（stream/conversations/ui/messages）/ 发送完成，而
  输入框的 `input` 事件此前只挂了 `autoGrow()`（调整高度）——文字从空到有
  不会经过任何一条路径，按钮永远停在旧态。回复完成后重新输入也表现为
  「有字但按钮灰」，日常靠 Enter 发送（`onSend` 实时算 `canSend`）掩盖了
  它；切语言 `emit("ui")` 顺带触发一次重算，即「切换中英文后就能发送」的
  现象。现 `input` 事件同时调 `updateButtons()`，图文混合粘贴的
  `insertTextAtCursor`（程序化改 value，不触发 input 事件）同样补齐；
  版本号 v0.3.3 → v0.3.4。

## v0.3.3（2026-11-14）

独立端口访问卡死在「连接宿主中 - 30%」的修复 + 登录期连接徽标误报（纯前端）。

- **修复（主）**：standalone 首次探测 `/api/health` 必然 401（未登录），前端转入
  登录分支调 `showLogin()`，但该分支从不淡出加载屏——登录卡
  （`.overlay-root.overlay-deep`，z-index 90）被全屏不透明加载屏
  （`#loading-screen`，z-index 100）完全盖住，页面上只剩永远停在 30% 的
  进度条，服务端无任何报错（日志为空属正常）。现给 `.overlay-deep`
  （唯一使用处即登录卡）提层至 z-index 110（高于加载屏 100、低于 toast 120），
  登录卡以 `--mask-deep` 罩在加载屏上呈现；登录成功遮罩移除后
  `loadData()` 的 40→100 进度继续可见。
- **修复（次）**：30s 心跳在未登录时照常 `ping()` `/api/health` → 必 401 →
  连接徽标误置「重连中」；且登录成功后 conn 不复位，徽标要再等 ≤30s 心跳才
  恢复「已连接」。现心跳（interval + visibilitychange）在 `needsLogin` 期间
  跳过，登录装载完成后立即 `ping()` 落定徽标；版本号 v0.3.2 → v0.3.3。

## v0.3.2（2026-11-14）

空态装饰图随「消息流宽度」设置拉伸变形的修复（纯前端，2 文件）。

- **修复**：空态插画（`empty-state.svg`，无会话/空会话两处）此前以
  仅含 `viewBox` 的 `<img>` 引入——SVG 无确定固有尺寸，flex 布局将其拉伸至
  容器全宽，随消息流宽度档位在 764/984/1200px 间跳动，构图全乱；
  现根节点补 `width="240" height="160"` 固有尺寸 + `.es-decoration` 显式
  `width: 240px; height: auto; flex: none`，三档位下装饰恒为 240×160
  （等比例、与宽度设置无关）；版本号 v0.3.1 → v0.3.2。

## v0.3.1（2026-11-14）

UI 设置宿主侧持久化（跨设备/跨入口生效）+ 附件按钮图标调整。

- **界面设置持久化到宿主**：新增 `GET/POST ui/settings` 端点（Dashboard 内嵌
  与独立端口两入口共用）——设置面板的全部自定义（头像开关、头像图、消息流
  宽度、紧凑模式、时间戳、TOKENS 用量、思考默认展开）现持久化到
  ``data/plugin_data/astrbot_plugin_webui/ui_settings.json``，任何设备/入口/
  浏览器打开都生效，不再局限于浏览器 sessionStorage（沙箱内嵌视图此前刷新
  即丢）；前端启动时拉取合并（宿主为准），本地 sessionStorage 仍作为会话级
  兜底，宿主不可达（mock/旧版后端）时静默降级；
- **头像文件化存储**：头像图不再存浏览器——上传的新头像统一命名为
  ``User_head.png``（用户）/ ``LLM_head.png``（AI）写入插件数据目录，
  **覆盖替换旧头像文件**；清除头像即删除文件；仅接受前端压缩产物
  PNG data URL（≤160px，防御上限 ≈1MB），设置 JSON 本身不落头像数据
  （读侧从文件回填），文件为唯一权威存储；
- **附件按钮图标**：输入框左侧附件按钮由「图片」图标改为「链接」图标，
  语义覆盖「上传附件（含图片与文本文件）」，不再暗示仅支持图片；
- mock 模式同步模拟 `ui/settings` 端点（内存文件层往返，`?mock=1` 可验收
  持久化交互）；版本号 v0.3.0 → v0.3.1；后端新增纯逻辑持久化层
  （`core/uistore.py` 增设「UI 设置与头像」节，原子写 + Windows 重试，
  自测覆盖）。

## v0.3.0（2026-11-14）

UI 界面批量改进（7 项）：Markdown 渲染、人格名标签、折叠布局修复、空态简化、
粘贴附件、消息头像与界面设置面板。后端零改动（纯前端 `views/chat/` + i18n）。

- **Markdown 渲染（LLM 回复与用户输入）**：新增 `views/chat/js/markdown.js`
  轻量渲染器——全程 DOM 构建、零 `innerHTML`（沿用项目安全铁律），URL 过协议
  白名单（http/https/data:image/mailto）；支持标题、列表（一层嵌套）、围栏
  代码块（语言标签）、引用、表格、分隔线、行内代码/粗斜体/删除线/链接/图片/
  自动链接；流式期间 markdown 重渲染经 rAF 合帧（text_delta 高频下只渲染最新
  一帧），呼吸光标跟进到最后一个文本块内；
- **AI 消息标签显示人格名**：消息 meta 行的 "AI" 改为当前会话人格名
  （无人格时回退 "AI"）；
- **侧栏折叠布局修复**：折叠类改挂 `#app`（原实现只把 `#sidebar` 宽度归零，
  网格列仍占 280px 导致对话区停在原地）——桌面端折叠时对话区/输入区真正
  扩展占满；手机端竖屏（≤900px）侧栏改为抽屉浮层：默认收起，点右上按钮滑出，
  选中会话/人格或点击遮罩自动收起；
- **无会话空态简化**：移除「开始对话」人格速选卡，仅提示「暂无对话」；
  发送消息即以宿主配置的默认人格自动建会话（原有行为，保持）；
- **Ctrl+V 直接粘贴附件**：输入框与全文档两级 paste 监听进入既有附件管线
  （白名单/压缩/预览条）；剪贴板文件无名时按 MIME 兜底命名；图文混合粘贴
  保留文字部分插入输入框；
- **消息头像（默认隐藏）**：设置面板开启后，AI/用户消息行显示头像——自定义
  头像图优先，未上传时 AI 显示人格名首字徽标、用户显示图标；
- **界面设置面板**：顶栏新增设置按钮，可自定义——头像开关与用户/AI 头像上传
  （160px PNG 压缩存储）、消息流宽度（标准 860/宽 1080/全宽）、紧凑模式、
  时间戳开关、TOKENS 用量开关、思考内容默认展开；设置持久化于
  `sessionStorage`（沙箱下自动退化为内存）；
- i18n 双字典同步新增 23 键；mock 模式新增 Markdown 演示消息（本机 `?mock=1`
  视觉验收用）；版本号 v0.2.0 → v0.3.0。

## v0.2.0（2026-11-14）

架构重构：插件退化为「纯前端 + 宿主管道对接层」。对话不再由插件直调 Provider、
自管人格/历史/工具循环，全部托管给 AstrBot 内置消息管道——插件只做
「webchat 队列注入 + back_queue 消费」，请求拼接、人格解析、历史、工具循环
全部归还宿主（前端 REST/SSE 契约逐字段保持，新增可选字段见下）。

- **工具调用 / 记忆 / RAG 全面生效**：宿主已注册的全部 LLM 工具与 MCP 工具在
  本 UI 对话中可用，工具调用折叠块照常实时展示「工具名 → 参数 → 结果」；
  长记忆、知识库（RAG）等依赖消息管道的插件在本界面照常触发——v0.1.x
  「直调不走管道」的已知限制解除；
- **LLM 错误以 bot 文本透传**（与官方 WebChat 一致）：Provider 错误作为一条
  bot 文本消息出现在对话里（如「LLM 响应错误: …」），不再有插件侧 fatal
  error 事件；发送前置校验（会话不存在等）仍返回 4xx；
- **旧版会话只读归档**：v0.1.x 产生的会话更新后保留为只读归档——列表带
  「旧版」徽标，可查看历史、可删除归档，不可继续对话，不做迁移；
- **附件存储位置变化**：上传附件改存宿主附件目录 `data/attachments/` 并注册
  宿主附件数据库（与官方 WebChat 同一存储），插件侧保留限额/白名单/索引；
  宿主 bot 侧发出的图片/文件同样进助手消息附件展示；
- **文本附件不再全文注入 prompt**：v0.2 起文本附件作为 file part 交宿主管道
  处理（不再拼接进本轮消息全文）；界面预览超过 `upload_text_max_kb` 仍截断；
- **配置项 `tool_loop_mode` 移除**：工具循环完全复用宿主 tool_loop，该配置项
  从 schema 中删除，更新后配置面板自动生效；
- 会话管理（新建/改名/删除/首条消息自动改题）与历史读取映射宿主
  ConversationManager 与平台历史，权威历史与官方 WebChat 同源；
- 前端新增增量能力：legacy 会话「旧版」徽标与续写禁用、助手消息附件渲染，
  i18n 双字典补齐对应文案。

**升级注意（v0.1.x → v0.2.0）**：

1. **旧会话变只读归档**：更新后 v0.1.x 的会话自动变为只读归档（「旧版」徽标），
   如需继续对话请新建会话；
2. **`tool_loop_mode` 配置移除**：已保存配置中的该项失效，从配置面板移除，
   无需手动清理；
3. **附件存储位置变化**：新附件存放于宿主数据区 `data/attachments/`；
   v0.1.x 时代存于 `data/plugin_data/astrbot_plugin_webui/uploads/` 的旧附件
   仅随归档历史引用展示，删除归档会话只清插件索引、不触宿主附件目录。

## v0.1.1

修复「无法加载 webUI 且日志无报错」的可诊断性问题（对照 AstrBot v4.29.4 源码
逐环节核查：插件视图发现/内容服务/bridge SDK 注入/父页面 postMessage 协议/插件
API 挂载均与宿主一致；前端在本机仿真宿主（UserPlan/debug/hostmock，复刻 v4.29.4
的 iframe sandbox、SDK 注入、安全响应头与 bridge 协议）实测 bridge 模式与
fallback 模式均完整渲染。症状根因不在代码，而是**独立端口默认关闭时打开
`http://<host>:6186` 连接被拒绝、且插件对「入口未启用」零日志输出**，用户无从
判断）：

- **独立端口默认改为开启**（`standalone_enabled` 默认 `true`）：安装即可直接
  打开 `http://127.0.0.1:6186/`；未设密码时仍仅允许本机 127.0.0.1/::1 访问，
  局域网访问需设置 `standalone_password`（安全默认不变）；
- **入口状态全程可见**：启动时无论独立端口「已启动 / 启动失败 / 未启用」，
  日志均有对应 INFO/WARNING 行注明原因与可用入口（未启用时注明应改用内嵌
  视图或开启配置），并记录宿主版本与内嵌视图能力（< 4.29 明示不可用），
  不再出现「日志一片空白」；
- **未设密码时的远程登录报错改为可行动文案**：明确提示「未设置访问密码，仅
  允许本机访问；远程访问需设置 standalone_password 并重载插件」，替代笼统的
  「密码错误或无本机访问权限」；
- **README 全面重写（面向大众）**：预览图 + 特性表 + 与内置 WebChat 对比表 +
  三种安装方式 + 快速开始 + 常见问题 FAQ + 故障排查对照表；开发向内容收进
  折叠区；
- **绑定正式仓库**：`metadata.yaml` 的 `author: AiE1487`、
  `repo: https://github.com/AiE1487/astrbot_plugin_webUI`（替换占位值
  `your-name`）；新增 `LICENSE`（MIT）文件，含第三方致谢声明。

## v0.1.0

首个发布版本（M1–M4 全量实现；功能验收需真实 AstrBot 环境，见 README 真机验收清单）。

- **对话与流式**：直调 Provider `text_chat_stream` 流式生成；SSE 事件
  （reasoning_delta / text_delta / done / stopped / error）按 seq 全量重放 +
  实时推送，缓冲保留 ≥5 分钟，刷新/断线后续流；同会话 single-flight（409
  `session_busy`）与全局并发上限（409 `concurrency_limit`，默认 2）；停止按钮
  先落终态再 cancel，保留已生成部分；历史双写（宿主 `conversation_manager`
  权威历史 + 插件侧每会话一份富历史 JSON，原子写）；
- **附件**：图片（png/jpg/jpeg/webp/gif）+ 文本文件（白名单后缀可配）；前端
  canvas 压缩长边 ≤2048（GIF 跳过）；图片进 `image_urls`，文本内容以
  `<attachment>` 区块注入 prompt（超 200KB 截断提示）；图片超限 413 拒收、
  文本落盘 16MB 硬上限；图片 preview 缩略（Pillow 缺失自动回退原图）；孤儿
  附件 24h 后台清理，删除会话级联清理附件；
- **工具调用**：`tool_loop_mode` 开关——`stream`（默认，自实现流式工具循环，
  正文保持流式）→ 不可用时自动降级 `loop_agent`（宿主 tool_loop_agent 逐步
  循环）→ 再降级无工具单轮（附非致命提示）；人格 `tools` 语义
  （None=全部 / []=禁用 / 列表=白名单）；UI 折叠块实时显示工具名、参数、结果
  与耗时，单次生成最多 8 轮；
- **双入口**：Dashboard 内嵌 Plugin Views（复用登录态；注入 bridge 401 时
  自动切换自研 postMessage 客户端兜底）+ 独立端口页（aiohttp，密码 + Bearer
  token，连错 5 次锁 60 秒，空密码仅允许 127.0.0.1，忽略转发头防伪造），
  两入口共用同一套 service 与会话池；
- **UI**：类 SillyTavern 布局 × 明日方舟官网风设计 token（深色 + 青色主视觉）；
  加载屏、思考折叠区（流式自动展开/收起）、工具折叠块、附件预览与 lightbox、
  页内自绘对话框（沙箱无 alert/confirm）、断线重连横幅、mock 模式
  （`?mock=1` 本机无宿主预览）；i18n 中文/英文（99 键两侧对齐）；
- **配置**：9 项 `_conf_schema.json`（独立端口开关/端口/密码、全局并发上限、
  图片与文本附件限额、思考区默认展开、文本附件白名单、`tool_loop_mode`），
  除注明需重载的三项外实时热生效；
- **已知限制**：直调 Provider 不经过消息管道，记忆类管道插件在本 UI 对话中
  不触发；工具循环方案 A 依赖宿主内部 API，未真机核实（probe 脚本
  `tests/probe_toolloop.py` 随仓库提供、不进 zip）；`metadata.yaml` 的
  `author` / `repo` 为占位值 `your-name`（v0.1.1 已替换为正式仓库）；ruff 未运行
  （本机环境无 ruff）。
