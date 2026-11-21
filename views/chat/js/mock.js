/**
 * mock.js — mock 传输层（契约「前端实现约定」：同接口同事件序列；?mock=1 或无 bridge
 * 时驱动全 UI，本机视觉验收唯一途径）。
 *
 * 覆盖分支（M1 ⑥ / M2 ⑥ / M3 ⑥）：
 * - 正常流式：reasoning_delta → text_delta → done{message_id, usage}
 * - 工具流式：tool_call{phase:"start"} → tool_result → text_delta → done
 * - 手动停止：stop/<gen_id> 后缓冲写入终态 stopped（契约：先落终态再 cancel）
 * - fatal 错误：error{fatal:true}（错误面板）；非 fatal error 仅透传、流继续
 * - 多模态拒绝：向 vision-off 人格发图片 → error{fatal:true, code:"model_not_multimodal"}
 * - 409 双分支：session_busy（同会话）/ concurrency_limit（全局并发满，mock 上限 2）
 * - 400/404：send 校验、未知 cid/gen_id
 * - upload：图片 canvas 压缩预览（≤256px data URL）、文本前 500 字摘要、
 *   >200KB 截断 truncated=true、图片 >20MB 413、类型不在白名单 415
 * - 富历史：attachments/reasoning/tools/usage/stats/error 字段齐全；page=1 最新一页、
 *   页内旧→新、has_more；首条用户消息后标题改为其前 20 字
 * - media/stats 事件（T4）：正常流各发一次——media（流中附件 meta，f_mock1，
 *   preview 直出 data URL，置于正文流式前便于观察）在正文前、stats（上下文
 *   统计 cache_rate/context_tokens/max=null）在终态前；终态消息 attachments/stats
 *   落历史，done 重建后附件仍可见、统计栏常驻恢复
 * - GET conversations 项带 gen_id（REST 契约：busy 时为进行中 gen_id，否则 null），
 *   供前端 T2 会话列表兜底恢复订阅演示
 *
 * 数据形状与 .astrbot-plugin/i18n、UserPlan 契约逐字段一致。
 */

/* ── 小工具 ───────────────────────────────────────────── */

/** uuid4 hex（契约 id 规范：URL 安全） */
function uuidHex() {
  try {
    if (typeof crypto !== "undefined" && crypto.randomUUID) {
      return crypto.randomUUID().replace(/-/g, "");
    }
  } catch {
    /* 回退 */
  }
  let out = "";
  for (let i = 0; i < 32; i += 1) {
    out += Math.floor(Math.random() * 16).toString(16);
  }
  return out;
}

/** mock 错误（非 Error 的 plain object 会被 api.normalizeError 兼容，这里用 Error 子类保堆栈） */
class MockHttpError extends Error {
  /**
   * @param {number} status
   * @param {string} slug
   * @param {string} message
   */
  constructor(status, slug, message) {
    super(message);
    this.name = "MockHttpError";
    this.status = status;
    this.slug = slug;
    this.code = status;
  }
}

/** @param {number} ms @returns {Promise<void>} */
function delay(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function netDelay() {
  return delay(90 + Math.floor(Math.random() * 160));
}

function nowIso(offsetMinutes = 0) {
  return new Date(Date.now() - offsetMinutes * 60000).toISOString();
}

/* ── 种子数据 ─────────────────────────────────────────── */

const MOCK_VERSION = "0.3.1";
const MOCK_CONCURRENCY = 2;
const IMAGE_KINDS = new Set(["image"]);
// 白名单与后端默认值对齐（attachment_service.py IMAGE_EXTENSIONS；
// _conf_schema.json attachment_text_whitelist 默认值），保证 mock 验收不演示真后端拒收的类型
const IMAGE_EXTS = ["png", "jpg", "jpeg", "webp", "gif"];
const TEXT_EXTS = ["txt", "md", "json", "csv", "py"];

/** 生成一张小的占位图 data URL（装饰用，运行时画，不引外部素材，R5） */
function makePlaceholderImage(label, hue) {
  const size = 128;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = /** @type {CanvasRenderingContext2D} */ (canvas.getContext("2d"));
  const grad = ctx.createLinearGradient(0, 0, size, size);
  grad.addColorStop(0, `hsl(${hue} 60% 22%)`);
  grad.addColorStop(1, `hsl(${hue} 70% 38%)`);
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, size, size);
  ctx.strokeStyle = "rgba(255,255,255,0.35)";
  ctx.strokeRect(6.5, 6.5, size - 13, size - 13);
  ctx.fillStyle = "rgba(255,255,255,0.88)";
  ctx.font = "600 20px Consolas, monospace";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(label, size / 2, size / 2);
  return canvas.toDataURL("image/png");
}

function attachmentMeta(fileId, kind, name, size, mime, preview, truncated) {
  /** @type {Record<string, any>} */
  const meta = { file_id: fileId, kind, name, size, mime, preview };
  if (kind === "text") meta.truncated = Boolean(truncated);
  return meta;
}

/** 富历史填充消息（演示分页） */
function fillerMessage(index, role) {
  const lines = [
    "巡督记录：本段为 mock 历史填充数据，用于验证分页 prepend。",
    `批次 ${String(index).padStart(3, "0")} —— 罗德岛例行通讯测试。`,
    "可自由删除此会话；分页向上滚动会加载更早消息。",
  ];
  return {
    id: `m_${uuidHex()}`,
    role,
    ts: nowIso(600 + index * 7),
    text: lines[index % lines.length],
  };
}

function seedDatasets() {
  const imgA = makePlaceholderImage("SEA-01", 200);
  const imgB = makePlaceholderImage("SEA-02", 120);

  /** @type {Record<string, any>} */
  const personas = {
    image_capable: true,
    personas: [
      {
        id: "default",
        name: "default",
        system_prompt_preview: "通用助手（默认人格）：严谨、简洁，用中文回答，擅长日常问答与信息整理。",
        has_tools: false,
        is_default: true,
      },
      {
        id: "deepthink",
        name: "deepthink",
        system_prompt_preview: "深度思考人格：回答前会输出思考过程（reasoning），适合分析类问题。",
        has_tools: false,
        is_default: false,
      },
      {
        id: "toolmaster",
        name: "toolmaster",
        system_prompt_preview: "工具演示人格：绑定了天气/搜索等 LLM 工具，回答时会触发 TOOL_CALL。",
        has_tools: true,
        is_default: false,
      },
      {
        id: "vision-off",
        name: "vision-off",
        system_prompt_preview: "纯文本模型人格：不支持图片输入（发送图片会触发 model_not_multimodal）。",
        has_tools: false,
        is_default: false,
      },
    ],
  };

  const c1 = "conv0001c1a7b4c3d2e1f0a9b8c7d6e5f0";
  const c2 = "conv0002c1a7b4c3d2e1f0a9b8c7d6e5f0";
  const c3 = "conv0003c1a7b4c3d2e1f0a9b8c7d6e5f0";

  /** @type {Record<string, any>} */
  const conversations = {
    conversations: [
      { cid: c1, title: "明天的安排讨论", persona_id: "default", updated_at: nowIso(12), busy: false },
      { cid: c2, title: "天气查询测试", persona_id: "toolmaster", updated_at: nowIso(95), busy: false },
      { cid: c3, title: "哲学三问", persona_id: "deepthink", updated_at: nowIso(1500), busy: false },
    ],
  };

  /** @type {Map<string, any[]>} */
  const histories = new Map();

  // 会话 1：40 条填充（跨页，演示 page=1 has_more 与向上 prepend）+ 带附件的用户消息 + 带思考/用量的助手消息
  const hist1 = [];
  for (let i = 0; i < 40; i += 1) {
    hist1.push(fillerMessage(i, i % 2 === 0 ? "user" : "assistant"));
  }
  const seededFiles = [
    attachmentMeta(`f_${uuidHex()}`, "image", "sea-01.png", 148_233, "image/png", imgA, false),
    attachmentMeta(`f_${uuidHex()}`, "image", "sea-02.png", 98_112, "image/png", imgB, false),
    attachmentMeta(`f_${uuidHex()}`, "text", "notes.md", 3_211, "text/markdown",
      "# 随行笔记\n1. 明早 09:30 例会；\n2. 输出周报草稿；\n3. 检查附件清单。", false),
  ];
  hist1.push({
    id: `m_${uuidHex()}`,
    role: "user",
    ts: nowIso(20),
    text: "这两张图帮我归档，笔记内容一并整理进今天的纪要。",
    attachments: seededFiles,
  });
  hist1.push({
    id: `m_${uuidHex()}`,
    role: "assistant",
    ts: nowIso(19),
    text: "已归档两张图片（SEA-01 / SEA-02），并按笔记要点整理纪要：\n1. 明早 09:30 例会；\n2. 输出周报草稿；\n3. 检查附件清单。\n需要我再补充行动计划吗？",
    reasoning: {
      content: "用户给了一张会议相关的图和一份笔记。需要：1) 确认收到两张图片；2) 逐条整理笔记为纪要；3) 保持简洁并列出可执行的下一步。",
    },
    usage: { output: 96, total: 512 },
    // 历史携带最近一次 stats（与真后端 v0.4.2 起一致，供常驻统计栏恢复）
    stats: { cache_rate: 0.62, context_tokens: 512, max_context_tokens: null, input_cached: 282, input_other: 173, output: 96 },
  });
  // W1：markdown 渲染演示消息（标题/列表/代码块/表格/引用全覆盖，供视觉验收）
  hist1.push({
    id: `m_${uuidHex()}`,
    role: "assistant",
    ts: nowIso(18),
    text: [
      "## Markdown 渲染验证",
      "",
      "支持 **加粗**、*斜体*、~~删除线~~、行内代码 `astrbot_execute_shell`，以及自动链接 https://astrbot.app 。",
      "",
      "- 列表项一：流式增量逐段渲染",
      "- 列表项二：代码块与表格",
      "  - 嵌套项：一层缩进",
      "",
      "```python",
      "def greet(name: str) -> str:",
      '    return f"你好，{name}！"  # 围栏代码块',
      "",
      'print(greet("astrbot"))',
      "```",
      "",
      "| 通道 | 状态 |",
      "| --- | --- |",
      "| 流式回复 | OK |",
      "| 附件 | OK |",
      "",
      "> 引用块：酒馆风 UI，深色底 + 青色锚线。",
    ].join("\n"),
    usage: { output: 210, total: 1240 },
    stats: { cache_rate: 0.87, context_tokens: 12400, max_context_tokens: null, input_cached: 10788, input_other: 1612, output: 210 },
  });
  histories.set(c1, hist1);

  // 会话 2：工具调用示例（含一个 error 结果的工具）
  histories.set(c2, [
    { id: `m_${uuidHex()}`, role: "user", ts: nowIso(95), text: "北京今天天气怎么样？顺便查一下上海。" },
    {
      id: `m_${uuidHex()}`,
      role: "assistant",
      ts: nowIso(94),
      text: "北京：晴，26℃。\n上海：查询工具本次执行失败（超时），稍后可重试。",
      tools: [
        { id: `call_${uuidHex().slice(0, 8)}`, name: "get_weather", args: { location: "北京" }, result: "晴 26℃", elapsed_ms: 812 },
        { id: `call_${uuidHex().slice(0, 8)}`, name: "get_weather", args: { location: "上海" }, error: "tool timeout after 5000ms", elapsed_ms: 5003 },
      ],
      usage: { output: 48, total: 620 },
      stats: { cache_rate: null, context_tokens: 620, max_context_tokens: null, input_cached: 0, input_other: 620, output: 48 },
    },
  ]);

  // 会话 3：空会话
  histories.set(c3, []);

  return { personas, conversations, histories, seededFiles, cids: { c1, c2, c3 } };
}

/* ── 假回复文案 ───────────────────────────────────────── */

const CANNED_REPLIES = [
  [
    "收到。用一段 markdown 验证渲染（W1）：",
    "",
    "## 要点小结",
    "",
    "- **加粗**、*斜体*、~~删除线~~ 与行内代码 `get_weather`",
    "- 自动链接：https://github.com/AstrBotDevs/AstrBot",
    "  - 一层嵌套列表项",
    "",
    "```python",
    'print("hello from astrbot")  # 围栏代码块',
    "```",
    "",
    "| 通道 | 状态 |",
    "| --- | --- |",
    "| 流式渲染 | OK |",
    "| 工具面板 | OK |",
    "",
    "> 深色卡片 + 左侧青线是 AI 消息的视觉锚点，直到 done 事件落定。",
  ].join("\n"),
  "好的，这里是一段用于验证流式渲染的 mock 回复：包含多行文本、标点，与较长段落，用来观察自动滚动与打字光标行为。\n第二行用来验证换行保持。\n第三行验证段落间距。",
  "示例答复：会话列表按 NO.01 编号装饰，右轨显示大号青色会话编号与半调网点纹理。你可以在侧栏新建、切换或删除会话（删除会弹页内自绘确认框）。",
];

function cannedReplyFor(personaId, text) {
  if (/总结|纪要/.test(text)) return CANNED_REPLIES[0];
  if (personaId === "toolmaster") return "工具调用完成：已把查询结果整理成上面的 TOOL_CALL 面板。如需再次触发，可在消息里包含「天气」或「工具」。";
  return CANNED_REPLIES[Math.floor(Math.random() * CANNED_REPLIES.length)];
}

function cannedReasoningFor(text) {
  const topic = text.length > 30 ? `${text.slice(0, 30)}…` : text;
  return `用户输入：「${topic}」。\n先判断意图：属于示例问答，走默认回复模板。\n核对输出要求：保持简洁、使用中文、演示思考流式的自动展开与完成收起。\n结论：输出组织好的答复，并在 done 时落定。`;
}

/* ── mock 传输层 ──────────────────────────────────────── */

/**
 * @returns {import("./bridge-client.js").Transport}
 */
export function createMockTransport() {
  const seed = seedDatasets();
  /** @type {Map<string, any>} */
  const files = new Map();
  for (const meta of seed.seededFiles) files.set(meta.file_id, meta);

  /** mock 界面设置存储（模拟宿主 ui_settings.json；页面会话内有效） */
  let mockUiSettings = {};
  /** mock 头像“文件”层（模拟 User_head.png / LLM_head.png：POST 落盘、GET 回填） */
  const mockAvatarFiles = { avatarUser: "", avatarAssistant: "" };

  /** @type {Map<string, any>} */
  const gens = new Map();
  /** @type {Map<string, Set<{id: string, onMessage: (p: any) => void, onOpen?: () => void, onError?: () => void, timers: number[]}>>} */
  const clients = new Map();
  let subCounter = 0;

  let locale = (() => {
    try {
      return (navigator.language || "zh-CN").toLowerCase().startsWith("zh") ? "zh-CN" : "en-US";
    } catch {
      return "zh-CN";
    }
  })();

  function conversationByCid(cid) {
    return seed.conversations.conversations.find((item) => item.cid === cid) || null;
  }

  function historyOf(cid) {
    if (!seed.histories.has(cid)) seed.histories.set(cid, []);
    return /** @type {any[]} */ (seed.histories.get(cid));
  }

  function touchConversation(cid) {
    const convo = conversationByCid(cid);
    if (convo) convo.updated_at = new Date().toISOString();
  }

  function setBusy(cid, busy) {
    const convo = conversationByCid(cid);
    if (convo) convo.busy = busy;
  }

  function runningGenCount() {
    let count = 0;
    for (const gen of gens.values()) if (!gen.closed) count += 1;
    return count;
  }

  /** 该会话当前进行中的 gen（GET conversations 下发 gen_id 用；无则 null）。 */
  function runningGenForCid(cid) {
    for (const gen of gens.values()) {
      if (gen.cid === cid && !gen.closed) return gen;
    }
    return null;
  }

  /** T4 media 事件附件 meta（惰性生成一次占位图 data URL；f_mock1 不入 files 层，preview 直出）。 */
  let mockMediaMeta = null;
  function mediaEventAttachment() {
    if (!mockMediaMeta) {
      mockMediaMeta = attachmentMeta(
        "f_mock1",
        "image",
        "mock-inline-media.png",
        24_576,
        "image/png",
        makePlaceholderImage("MEDIA", 190),
        false,
      );
    }
    return { ...mockMediaMeta };
  }

  /** 向缓冲写入事件并实时派发给订阅者（每事件首字段 seq，契约 SSE 节）。 */
  function pushEvent(gen, type, data) {
    const seq = gen.events.length;
    const payload = { seq, ...data };
    gen.events.push({ type, data: payload });
    const subs = clients.get(gen.genId);
    if (subs) {
      for (const sub of subs) {
        sub.onMessage({
          raw: JSON.stringify(payload),
          parsed: payload,
          eventType: type,
          lastEventId: "",
        });
      }
    }
  }

  function closeGen(gen) {
    gen.closed = true;
    for (const timer of gen.timers) window.clearTimeout(timer);
    gen.timers.length = 0;
    setBusy(gen.cid, false);
    touchConversation(gen.cid);
  }

  /** 取消某个 gen 的后续脚本（stop 用）：仅停止未执行步骤，不写事件。 */
  function cancelScript(gen) {
    for (const timer of gen.timers) window.clearTimeout(timer);
    gen.timers.length = 0;
  }

  /** @param {number} ms @param {any} gen @param {() => void} fn */
  function schedule(gen, ms, fn) {
    if (gen.closed) return;
    gen.timers.push(window.setTimeout(() => {
      if (!gen.closed) fn();
    }, ms));
  }

  /** 生成期脚本：按 persona/文本/附件选择分支。 */
  function runGeneration(gen, { text, fileIds }) {
    const convo = conversationByCid(gen.cid);
    const personaId = convo?.persona_id || "default";
    const attachments = (fileIds || [])
      .map((fid) => files.get(fid))
      .filter(Boolean);
    const hasImage = attachments.some((meta) => IMAGE_KINDS.has(meta.kind));

    // 分支：多模态拒绝（vision-off + 图片）
    if (hasImage && personaId === "vision-off") {
      schedule(gen, 350, () => {
        pushEvent(gen, "error", {
          message: "vision-off persona rejected image input",
          fatal: true,
          code: "model_not_multimodal",
        });
        closeGen(gen);
      });
      return;
    }

    // 分支：fatal 错误（文本触发词「触发错误」）
    if (/触发错误|fatal error/.test(text)) {
      schedule(gen, 500, () => {
        pushEvent(gen, "error", { message: "provider 超时（mock fatal 示例）", fatal: true });
        closeGen(gen);
      });
      return;
    }

    // 分支：非 fatal 错误（「警告一下」）——流继续
    if (/警告一下/.test(text)) {
      schedule(gen, 300, () => {
        pushEvent(gen, "error", { message: "一次可恢复的限流警告（mock 非 fatal 示例）", fatal: false });
      });
    }

    let cursor = 0;
    const step = (fn) => {
      schedule(gen, cursor, fn);
      cursor += 120;
    };

    // 思考流式：deepthink 人格或文本含「思考」
    const wantsReasoning = personaId === "deepthink" || /思考|reasoning/.test(text);
    if (wantsReasoning) {
      const reasoning = cannedReasoningFor(text);
      for (let i = 0; i < reasoning.length; i += 5) {
        const chunk = reasoning.slice(i, i + 5);
        step(() => pushEvent(gen, "reasoning_delta", { delta: chunk }));
      }
    }

    // 工具分支：toolmaster 人格或文本含「天气/工具」
    const wantsTools = (personaId === "toolmaster" && /天气|工具|查询/.test(text))
      || /天气|调用工具/.test(text);
    if (wantsTools) {
      const callId = `call_${uuidHex().slice(0, 8)}`;
      step(() => pushEvent(gen, "tool_call", { phase: "start", id: callId, name: "get_weather", args: { location: "北京" } }));
      cursor += 900;
      step(() => pushEvent(gen, "tool_result", { id: callId, result: "晴 26℃（mock）", elapsed_ms: 812 }));
    }

    // T4：media 事件（流中附件 meta）——置于正文流式之前，整段生成期间均可观察
    step(() => pushEvent(gen, "media", { attachment: mediaEventAttachment() }));

    // 正文流式：2~6 字一块（步长与切片一致，避免重叠/丢字）
    const reply = cannedReplyFor(personaId, text);
    for (let i = 0; i < reply.length;) {
      const size = 2 + Math.floor(Math.random() * 5);
      const chunk = reply.slice(i, i + size);
      i += size;
      step(() => pushEvent(gen, "text_delta", { delta: chunk }));
    }

    // T4：stats 事件（上下文统计），终态前发一次
    step(() => pushEvent(gen, "stats", {
      cache_rate: 0.87,
      context_tokens: 12400,
      max_context_tokens: null,
      input_cached: 10788,
      input_other: 1612,
      output: 512,
    }));

    // 终态：done
    schedule(gen, cursor + 200, () => {
      pushEvent(gen, "done", {
        message_id: `m_${uuidHex()}`,
        usage: { output: Math.max(1, Math.floor(reply.length / 2)), total: 800 + Math.floor(Math.random() * 400) },
      });
      // 落定：把助手消息写入 mock 历史（与前端本地落定同构）
      const assistant = {
        id: `m_${uuidHex()}`,
        role: "assistant",
        ts: new Date().toISOString(),
        text: reply,
      };
      if (wantsReasoning) assistant.reasoning = { content: cannedReasoningFor(text) };
      if (wantsTools) {
        assistant.tools = [{
          id: `call_${uuidHex().slice(0, 8)}`,
          name: "get_weather",
          args: { location: "北京" },
          result: "晴 26℃（mock）",
          elapsed_ms: 812,
        }];
      }
      assistant.usage = { output: Math.max(1, Math.floor(reply.length / 2)), total: 800 };
      // 历史携带 stats（与流中 stats 事件同值，真后端 v0.4.2 起同构：常驻统计栏刷新恢复）
      assistant.stats = {
        cache_rate: 0.87,
        context_tokens: 12400,
        max_context_tokens: null,
        input_cached: 10788,
        input_other: 1612,
        output: 512,
      };
      // media 事件附件同步落历史（与真后端一致：终态消息携带流中附件，
      // done 重建后附件仍可见，预览不至于只在毫秒级窗口闪现）
      assistant.attachments = [mediaEventAttachment()];
      historyOf(gen.cid).push(assistant);
      closeGen(gen);
    });
  }

  /* ── 请求处理（带模拟网络延迟） ─────────────────────── */

  async function apiGet(endpoint, params = {}) {
    await netDelay();
    if (endpoint === "health") {
      return { status: "ok", plugin: "astrbot_plugin_webui", version: MOCK_VERSION };
    }
    if (endpoint === "personas") return seed.personas;
    if (endpoint === "conversations") {
      // REST 契约：非 legacy 项带 gen_id（busy 时为进行中 gen_id，否则 null）
      return {
        conversations: seed.conversations.conversations.map((convo) => ({
          ...convo,
          gen_id: convo.busy ? (runningGenForCid(convo.cid)?.genId ?? null) : null,
        })),
      };
    }
    if (endpoint === "ui/settings") {
      // 模拟宿主 GET：设置 JSON 不存头像，从“文件层”回填 data URL
      return { settings: { ...mockUiSettings, ...mockAvatarFiles } };
    }
    const historyMatch = /^history\/([A-Za-z0-9_-]+)$/.exec(endpoint);
    if (historyMatch) {
      const cid = historyMatch[1];
      const history = historyOf(cid);
      const page = Math.max(1, Number(params.page ?? 1) || 1);
      const size = Math.min(100, Math.max(1, Number(params.size ?? 30) || 30));
      const total = history.length;
      const start = Math.max(0, total - page * size); // page=1 为最新一页
      const end = Math.max(0, total - (page - 1) * size);
      const messages = history.slice(start, end); // 页内旧→新
      return {
        cid,
        page,
        size,
        total,
        has_more: start > 0,
        messages,
      };
    }
    const fileMatch = /^files\/(f_[0-9a-f]+)$/.exec(endpoint);
    if (fileMatch) {
      const meta = files.get(fileMatch[1]);
      if (!meta) throw new MockHttpError(404, "not_found", "file not found (mock)");
      return meta;
    }
    throw new MockHttpError(404, "not_found", `unknown GET endpoint (mock): ${endpoint}`);
  }

  async function apiPost(endpoint, body = {}) {
    await netDelay();
    if (endpoint === "ui/settings") {
      if (!body || typeof body !== "object" || Array.isArray(body)) {
        throw new MockHttpError(400, "invalid_request", "settings must be an object (mock)");
      }
      // 模拟宿主 POST：头像 data URL 落“盘”（内存文件层，新头像覆盖旧文件），
      // 设置 JSON 仅存非头像字段（头像字段空串占位）；响应从文件层回填
      mockUiSettings = { ...body, avatarUser: "", avatarAssistant: "" };
      for (const key of ["avatarUser", "avatarAssistant"]) {
        const value = String(body[key] ?? "");
        mockAvatarFiles[key] = value.startsWith("data:image/png;base64,") ? value : "";
      }
      return { ok: true, settings: { ...mockUiSettings, ...mockAvatarFiles } };
    }
    if (endpoint === "conversations") {
      const personaId = String(body?.persona_id ?? "");
      if (!seed.personas.personas.some((p) => p.id === personaId)) {
        throw new MockHttpError(400, "invalid_request", "persona_id invalid (mock)");
      }
      const cid = `conv${uuidHex().slice(0, 28)}`;
      seed.conversations.conversations.unshift({
        cid,
        title: String(body?.title ?? "新会话"),
        persona_id: personaId,
        updated_at: new Date().toISOString(),
        busy: false,
      });
      seed.histories.set(cid, []);
      return { cid };
    }
    const deleteMatch = /^conversations\/([A-Za-z0-9_-]+)\/delete$/.exec(endpoint);
    if (deleteMatch) {
      const cid = deleteMatch[1];
      const index = seed.conversations.conversations.findIndex((item) => item.cid === cid);
      if (index < 0) throw new MockHttpError(404, "not_found", "conversation not found (mock)");
      seed.conversations.conversations.splice(index, 1);
      seed.histories.delete(cid);
      for (const [genId, gen] of Array.from(gens.entries())) {
        if (gen.cid === cid && !gen.closed) {
          closeGen(gen);
          gens.delete(genId);
        }
      }
      return { ok: true };
    }
    if (endpoint === "send") {
      const cid = String(body?.cid ?? "");
      const text = String(body?.text ?? "");
      const fileIds = Array.isArray(body?.file_ids) ? body.file_ids.map(String) : [];
      if (!conversationByCid(cid)) throw new MockHttpError(404, "not_found", "conversation not found (mock)");
      if (!text.trim() && fileIds.length === 0) {
        throw new MockHttpError(400, "invalid_request", "text and file_ids cannot both be empty (mock)");
      }
      const convo = conversationByCid(cid);
      if (convo?.busy) throw new MockHttpError(409, "session_busy", "conversation is generating (mock)");
      if (runningGenCount() >= MOCK_CONCURRENCY) {
        throw new MockHttpError(409, "concurrency_limit", "global concurrency limit reached (mock)");
      }
      const genId = uuidHex();
      /** @type {any} */
      const gen = { genId, cid, events: [], closed: false, timers: [] };
      gens.set(genId, gen);
      setBusy(cid, true);
      touchConversation(cid);
      // 用户消息入历史（附件引用绑定；孤儿文件 24h 清理由真后端负责）
      const attachments = fileIds.map((fid) => files.get(fid)).filter(Boolean);
      const userMessage = /** @type {any} */ ({
        id: `m_${uuidHex()}`,
        role: "user",
        ts: new Date().toISOString(),
        text,
      });
      if (attachments.length > 0) userMessage.attachments = attachments;
      const history = historyOf(cid);
      history.push(userMessage);
      // 首条用户消息后标题自动改为其前 20 字（契约 conversations 节）
      if (convo && (convo.title === "新会话" || convo.title === "New chat")) {
        const source = text.trim() || attachments[0]?.name || "";
        if (source) convo.title = source.slice(0, 20);
      }
      runGeneration(gen, { text, fileIds });
      return { gen_id: genId };
    }
    const stopMatch = /^stop\/([0-9a-f]+)$/.exec(endpoint);
    if (stopMatch) {
      const genId = stopMatch[1];
      const gen = gens.get(genId);
      if (!gen || gen.closed) throw new MockHttpError(404, "not_found", "gen not found or finished (mock)");
      // 契约：先向缓冲写入终态 stopped，再 cancel 任务
      pushEvent(gen, "stopped", { message: "已手动停止" });
      closeGen(gen);
      return { ok: true, stopped: true };
    }
    throw new MockHttpError(404, "not_found", `unknown POST endpoint (mock): ${endpoint}`);
  }

  /**
   * 校验白名单/大小 → 模拟上传进度（与 standalone XHR 的 onProgress 同形状）→ 生成元数据。
   * @param {File} file
   * @param {((p: {loaded: number, total: number}) => void) | undefined} [onProgress]
   */
  async function uploadFile(file, onProgress) {
    const name = String(file?.name ?? "file");
    const ext = name.split(".").pop()?.toLowerCase() ?? "";
    const isImage = (file?.type || "").startsWith("image/") || IMAGE_EXTS.includes(ext);
    const isText = !isImage && ((file?.type || "").startsWith("text/") || TEXT_EXTS.includes(ext));
    if (!isImage && !isText) {
      throw new MockHttpError(415, "unsupported_media_type", `file type not allowed (mock): .${ext || "?"}`);
    }
    if (isImage && Number(file?.size ?? 0) > 20 * 1024 * 1024) {
      throw new MockHttpError(413, "payload_too_large", "image exceeds 20MB (mock)");
    }
    // 进度模拟：8 档递增，loaded/total 与 XHR upload.onprogress 同形状
    const total = Math.max(1, Number(file?.size ?? 1));
    for (let i = 1; i <= 8; i += 1) {
      await delay(40);
      onProgress?.({ loaded: Math.round((total * i) / 8), total });
    }
    const fileId = `f_${uuidHex()}`;
    if (isImage) {
      const preview = await imagePreviewDataUrl(file);
      const meta = attachmentMeta(fileId, "image", name, Number(file.size ?? 0), file.type || "image/png", preview, false);
      files.set(fileId, meta);
      return meta;
    }
    // 文本：读出内容做前 500 字摘要；>200KB 截断标记
    const raw = await file.text();
    const truncated = raw.length > 200 * 1024;
    const mimeByExt = {
      md: "text/markdown",
      json: "application/json",
      csv: "text/csv",
      py: "text/x-python",
      html: "text/html",
    };
    const meta = attachmentMeta(
      fileId,
      "text",
      name,
      Number(file.size ?? 0),
      mimeByExt[ext] || file.type || "text/plain",
      raw.slice(0, 500),
      truncated,
    );
    files.set(fileId, meta);
    return meta;
  }

  /** 图片压缩预览：长边 ≤256 的 data URL（契约 preview 节）。 */
  async function imagePreviewDataUrl(file) {
    try {
      const url = URL.createObjectURL(file);
      try {
        const image = await loadImage(url);
        const scale = Math.min(1, 256 / Math.max(image.width, image.height));
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(image.width * scale));
        canvas.height = Math.max(1, Math.round(image.height * scale));
        const ctx = /** @type {CanvasRenderingContext2D} */ (canvas.getContext("2d"));
        ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
        let dataUrl = canvas.toDataURL("image/webp", 0.8);
        if (!dataUrl.startsWith("data:image/webp")) {
          dataUrl = canvas.toDataURL("image/jpeg", 0.8);
        }
        return dataUrl;
      } finally {
        URL.revokeObjectURL(url);
      }
    } catch {
      return "";
    }
  }

  function loadImage(src) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = () => reject(new Error("image decode failed"));
      image.src = src;
    });
  }

  /* ── SSE 订阅（全量重放 + 实时） ─────────────────────── */

  function subscribeSSE(endpoint, handlers) {
    const match = /^stream\/([0-9a-f]+)$/.exec(endpoint);
    if (!match) {
      return Promise.reject(new MockHttpError(404, "not_found", `unknown SSE endpoint (mock): ${endpoint}`));
    }
    const genId = match[1];
    const gen = gens.get(genId);
    if (!gen) {
      // 缓冲过期/未知 gen_id：404（前端视为终局，清活跃 gen_id 不再重试）
      return Promise.reject(new MockHttpError(404, "not_found", "gen not found or buffer expired (mock)"));
    }
    subCounter += 1;
    const sub = {
      id: `mock_sse_${subCounter}`,
      onMessage: (payload) => handlers?.onMessage?.(payload),
      onOpen: () => handlers?.onOpen?.(),
      onError: () => handlers?.onError?.(),
      timers: [],
    };
    if (!clients.has(genId)) clients.set(genId, new Set());
    /** @type {Set<any>} */ (clients.get(genId)).add(sub);

    // 全量重放 seq=0..N（15ms 间隔模拟网络），再接实时
    let replayIndex = 0;
    const replayStep = () => {
      if (!clients.get(genId)?.has(sub)) return; // 已退订
      if (replayIndex >= gen.events.length) {
        sub.onOpen();
        return;
      }
      const event = gen.events[replayIndex];
      replayIndex += 1;
      sub.onMessage({
        raw: JSON.stringify(event.data),
        parsed: event.data,
        eventType: event.type,
        lastEventId: "",
      });
      sub.timers.push(window.setTimeout(replayStep, 15));
    };
    sub.timers.push(window.setTimeout(replayStep, 30));
    return Promise.resolve(sub.id);
  }

  async function unsubscribeSSE(subscriptionId) {
    for (const [genId, subs] of Array.from(clients.entries())) {
      const found = [...subs].find((sub) => sub.id === subscriptionId);
      if (found) {
        for (const timer of found.timers) window.clearTimeout(timer);
        subs.delete(found);
        if (subs.size === 0) clients.delete(genId);
      }
    }
  }

  return {
    mode: "mock",
    version: MOCK_VERSION,
    streamSupported: true,
    needsLogin: false,
    ready: () => Promise.resolve(),
    getLocale: () => locale,
    /** @param {string} value */
    setLocale: (value) => {
      locale = value;
    },
    // mock 不带字典：bridge-client 的 makeT 会用内置词典 + 调用点 fallback 兜底
    onContext: () => () => {},
    apiGet: (endpoint, params) => apiGet(endpoint, params),
    apiPost: (endpoint, body) => apiPost(endpoint, body),
    upload: (endpoint, file, onProgress) => {
      if (endpoint !== "upload") {
        return Promise.reject(new MockHttpError(404, "not_found", `unknown upload endpoint (mock): ${endpoint}`));
      }
      return uploadFile(file, onProgress);
    },
    download: (endpoint) => {
      const match = /^files\/(f_[0-9a-f]+)$/.exec(endpoint);
      if (!match) return Promise.reject(new MockHttpError(404, "not_found", "file not found (mock)"));
      const meta = files.get(match[1]);
      if (!meta) return Promise.reject(new MockHttpError(404, "not_found", "file not found (mock)"));
      if (meta.objectUrl) {
        const anchor = document.createElement("a");
        anchor.href = meta.objectUrl;
        anchor.download = meta.name;
        anchor.click();
      }
      return Promise.resolve({ ok: true });
    },
    mediaUrl: (endpoint) => {
      const match = /^files\/(f_[0-9a-f]+)$/.exec(endpoint);
      if (!match) return "";
      const meta = files.get(match[1]);
      if (!meta) return "";
      if (!meta.objectUrl && meta.kind === "image" && meta.preview) {
        // mock 没有原图存储：灯箱直接用预览图
        return meta.preview;
      }
      return meta.objectUrl || meta.preview || "";
    },
    subscribeSSE: (endpoint, handlers) => subscribeSSE(endpoint, handlers),
    unsubscribeSSE: (id) => unsubscribeSSE(id),
    t: () => "",
    raw: null,
  };
}
