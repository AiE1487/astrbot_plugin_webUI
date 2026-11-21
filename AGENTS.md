# AGENTS.md — astrbot_plugin_webui

AstrBot 插件「酒馆风对话 WebUI」（类 SillyTavern 网页聊天视图）。基准宿主
AstrBot 4.29.4，插件 Python ≥3.12，无第三方 pip 依赖。当前版本 **v0.4.0**，
origin = `https://github.com/AiE1487/astrbot_plugin_webUI.git`（main）。

## 目录布局（2026-11-14 起扁平化，先读）

**git 仓库根 = 插件根 = 本目录 `astrbot_plugin_webUI/`**：main.py、core/、
views/、tests/、metadata.yaml 等全部在仓库根，所有命令一律在仓库根执行。
旧的两级布局（外层工作区 + `astrbot_plugin_webui/` 内层仓库）已废弃；
`UserPlan/`（原始需求/开发计划/hostmock 仿真宿主）已整体移除。

## 目录结构

- `main.py` —— 只做生命周期与转发 → `core/`（routes/chat_service/
  pipeline_bridge/conversation_service/persona_service/attachment_service/
  uistore/sse/standalone）。v0.2.0 起插件只做前端 + 消息注入，对话全托管宿主管道。
- `views/chat/` —— 纯原生 ESM 前端，无构建步骤；`js/bridge-client.js` 是唯一
  传输层（bridge / postMessage fallback / standalone / mock 四模式自动探测），
  `js/components/`（composer/message/panels/sidebar）、`store.js`/`api.js`/
  `sse.js`/`markdown.js` 按职责分文件。
- `tests/selftest.py`（stub 宿主自测，342 项断言）+ `tests/stub/astrbot/`
  （宿主 API 桩）。
- `.astrbot-plugin/i18n/` —— zh-CN / en-US 文案字典（与 bridge-client.js 内
  字典双份，键两侧对齐）。
- `dist/` 发布 zip（gitignored）；`docs/preview.png` README 预览图。
- `.astrbot-src/` —— AstrBot v4.29.4 源码克隆，仅供比对，禁止修改；已入
  .gitignore 不跟踪，但打包脚本不读 .gitignore，打包前仍需移出仓库
  （见「已知坑」打包体积）。
- `.zcode/` —— ZCode 本地工具（workflow 命令/历史草稿/探针/宿主源码摘录），
  gitignored；其中 workflow 命令与草稿仍引用已删除的 `UserPlan/`，仅作历史存档。
- `.venv/` —— Unix 布局虚拟环境（`.venv/bin/python.exe`，没有 `Scripts\`）。
- `AGENTS.md` —— 本文件；打包脚本会排除它，是否入库由用户决定（当前未跟踪）。

## 环境硬约束（Windows + Git Bash）

- 本机 shell 是 Git Bash（POSIX）：丢弃输出 `>/dev/null 2>/dev/null`，严禁 `> nul`；
  禁用 cmd/PowerShell 语法。
- 宿主 Python 是 MSYS2（PEP 668 externally-managed）：禁止向宿主 Python
  pip install；一律用 `./.venv/bin/python.exe`。ruff 无 mingw wheel 不可用。
- 修改完成后必须回复可审计报告（用户全局规则）。

## 验证命令（改动后全部跑一遍，均在仓库根执行）

```bash
cd F:/work/AICoding/UniversalFolder/astrbot_plugin_webUI
./.venv/bin/python.exe -m compileall -q main.py core
./.venv/bin/python.exe tests/selftest.py        # 342 项断言全过
node --check views/chat/js/<改动的js>           # 逐文件语法
./.venv/bin/python.exe "C:/Users/14835/.agents/skills/astrbot-plugin-dev/scripts/package_plugin.py" . --check
# 打包：同命令去掉 --check（产物 dist/astrbot_plugin_webui-v<版本>.zip）
```

package --check 前需把 `.astrbot-src/` 移出仓库（见「已知坑」打包体积）。

UI 视觉预览：浏览器直开 `views/chat/index.html?mock=1`。

## 架构边界与规则

- **版本三处一致**：`metadata.yaml` version = `CHANGELOG.md` 顶部 =
  `views/chat/js/bridge-client.js` 的 `PLUGIN_VERSION`（health 运行时读
  metadata）；改版本必须同步三处 + CHANGELOG 新条目。
- **路由**：经 `context.register_web_api(f"/{PLUGIN_NAME}/{route}", ...)` 注册；
  逐端点精确路径，禁用 `<route>` 通配（动态段 `<name>` 只匹配单段，多级路径全
  404 且桩测/mock 测不出）。bridge 端点一律相对路径、不以 `/` 开头。错误出口
  统一 `routes.api_error` → `{"code","error","message"}` 契约形状（宿主
  `error_response` 的包裹形状不同，勿混用）。
- **LLM（全托管）**：插件是「webchat 事件注入者 + back_queue 消费者」，对话全
  托管宿主完整管道（人格解析/历史/provider 选择/工具循环+MCP/记忆·知识库）。
  对接唯一层 `core/pipeline_bridge.py`：注入三步（平台历史自补用户记录 →
  `get_or_create_back_queue(request_id=gen_id, cid)` → 会话队列 put tuple，
  绝不可调 `set_listener`）；排水循环 120s 空闲兜底；finally 必
  `remove_back_queue`。会话挂 webchat 命名空间
  `webchat:FriendMessage:webchat!astrbot_plugin_webui!<cid>`（固定 creator，与
  官方会话页隔离）；**插件 cid 与宿主 conv_id 是双主键**——conversation 侧
  操作一律经 `get_curr_conversation_id(umo)` 解析，禁止把 cid 直接当
  conversation_id。平台历史删除必须 `offset_sec=99999999`（默认只删 24h）。
  LLM 错误以 bot 文本透传（同官方 WebChat），无插件侧 fatal error。
- **前端**：ESM 相对路径（宿主经带 token 的 content_path 鉴权，禁止硬编码资源
  URL/拼 asset_token）；文案一律 `t()`；内嵌视图运行在**无 allow-same-origin
  的沙箱 iframe**——无原生 alert/confirm（用 `confirmDialog`），sessionStorage
  走 `safeStorage` 包装。
- **独立端口**（`core/standalone.py`）：aiohttp 延迟导入；空密码仅放行传输层
  对端 127.0.0.1/::1（忽略 X-Forwarded-For）；`standalone_enabled/port/password`
  改动需重载插件；启动日志必输出宿主版本与入口状态。
- **配置**：默认值只在 `_conf_schema.json` 维护一份，代码直接
  `self.config["键"]` 读，不做第二重兜底；bool 类型不是 boolean。
- **UI**：设计 token 在 `views/chat/css/tokens.css`（明日方舟风格：青 `#0098DC` /
  品牌绿 `#CBFF40` / 深表面 `#191919/#282828`）；禁止使用参考站任何素材、字体
  （Bender/SDK_Sans）或插画，字体用系统栈模拟。
- **持久化**：插件自有数据只写 `data/plugin_data/astrbot_plugin_webui/`，禁止
  写插件目录（更新/重装会丢）；附件例外——落宿主附件区 `data/attachments/` 并
  经 `db.insert_attachment` 注册（`data/` 属宿主数据区）。

## 已知坑

- 注入版 bridge SDK 在 4.29.x 有 401 案例 → 前端自动降级自研 postMessage 客户端
  （协议与宿主 `plugin_page_bridge.js` 逐字段对齐，勿改动握手字段）。
- SSE 经 bridge 由父页面转发，空闲 >15s 发 `event: ping` 保活（不占业务 seq）。
- 独立端口 `/api/*` 全部先鉴权再分发（含 upload），勿回退。
- **打包体积**：打包脚本内置排除表不含 `.astrbot-src/`（约 35MB 参考源码）——
  扁平化后在仓库根打包会撞 16MB 市场上限（2026-11-14 实测 23MB）；打包前需把
  `.astrbot-src/` 移出仓库或自行排除。
- 对照宿主行为以 `.astrbot-src/` 实源为准（`astrbot/api/web.py`、
  `astrbot/core/platform/sources/webchat/webchat_queue_mgr.py`、
  `astrbot/dashboard/services/chat_service.py`、
  `astrbot/dashboard/services/plugin_page_service.py`、
  `dashboard/src/views/PluginViewPage.vue` 是关键文件）。
- AstrBot 插件开发规范全文：`C:/Users/14835/.agents/skills/astrbot-plugin-dev/`
  （SKILL.md + references/01–09，改 main.py/metadata/_conf_schema 前先读）。
