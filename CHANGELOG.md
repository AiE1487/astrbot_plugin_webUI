# 更新日志

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
