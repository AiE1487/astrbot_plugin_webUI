/**
 * bridge-client.js — 前端唯一传输层入口（计划 §4 / 契约「前端实现约定」）。
 *
 * 四种模式，按优先级探测：
 *   1. mock       —— URL ?mock=1 强制（本机视觉验收唯一途径）。
 *   2. bridge     —— 宿主注入 window.AstrBotPluginView（旧名 AstrBotPluginPage 兼容），
 *                    health 探测通过即用。
 *   3. fallback   —— 注入 SDK 缺失/401 时，自研 postMessage 客户端直接与父页面
 *                    （Dashboard PluginViewPage）握手。协议与 AstrBot v4.29.4
 *                    astrbot/dashboard/plugin_page_bridge.js 完全一致
 *                    （channel "astrbot-plugin-page"；action: api:get / api:post /
 *                    files:upload / files:download / sse:subscribe / sse:unsubscribe），
 *                    含 SSE 通道。方案参考 NoelOrin/astrbot_plugin_webchat（MIT），
 *                    README 已注明借鉴来源。
 *   4. standalone —— 顶层窗口（独立端口页）：同款 endpoint 直挂 /api/<endpoint>，
 *                    POST /api/login 换 token，全部请求带 Bearer（GET 亦可用 ?token=）。
 *
 * 若 fallback 连上了父页面但父页面不支持 SSE 通道（sse:subscribe 失败），传输层
 * 保持 REST 可用并置 streamSupported=false —— UI 顶部出现「流式不可用」降级横幅，
 * 发送后轮询历史代替实时流（M1 工单要求）。
 *
 * 注意：
 * - 沙箱 iframe（无 allow-same-origin）访问 sessionStorage/localStorage 会抛
 *   SecurityError，一律经 safeStorage 包装，异常时退化为内存 Map。
 * - endpoint 一律为插件内相对路径，不以 / 开头（契约）。
 * - 端点由后端逐条精确注册，动态段 <name> 只匹配单段（勿拼多级路径）。
 */

import { createMockTransport } from "./mock.js";

/** 与 metadata.yaml version 保持一致（加载屏/侧栏脚注显示用）。 */
export const PLUGIN_NAME = "astrbot_plugin_webui";
export const PLUGIN_VERSION = "0.4.2";

const CHANNEL = "astrbot-plugin-page";
const READY_TIMEOUT = 4000;
const HANDSHAKE_TIMEOUT = 3000;
const PROBE_TIMEOUT = 6000;

/* ── 错误类型（api.js 归一化前的原始错误也用它承载） ─────── */

export class TransportError extends Error {
  /**
   * @param {string} message
   * @param {{status?: number, slug?: string, code?: number}} [meta]
   */
  constructor(message, meta = {}) {
    super(message);
    this.name = "TransportError";
    this.status = meta.status ?? 0;
    this.slug = meta.slug ?? "";
    this.code = meta.code ?? 0;
  }
}

/* ── 安全存储（沙箱 iframe 下 sessionStorage 可能抛 SecurityError） ── */

const memStorage = new Map();

export const safeStorage = {
  /** @returns {string | null} */
  get(key) {
    try {
      return window.sessionStorage.getItem(key);
    } catch {
      return memStorage.has(key) ? /** @type {string} */ (memStorage.get(key)) : null;
    }
  },
  /** @param {string} value */
  set(key, value) {
    try {
      window.sessionStorage.setItem(key, value);
    } catch {
      memStorage.set(key, value);
    }
  },
  /** @param {string} key */
  remove(key) {
    try {
      window.sessionStorage.removeItem(key);
    } catch {
      memStorage.delete(key);
    }
  },
  /** @returns {any | null} JSON 反序列化，坏数据返回 null */
  getJson(key) {
    const raw = this.get(key);
    if (!raw) return null;
    try {
      return JSON.parse(raw);
    } catch {
      return null;
    }
  },
  /** @param {any} value */
  setJson(key, value) {
    this.set(key, JSON.stringify(value));
  },
};

/* ── i18n：bridge.t 优先；fallback/standalone/mock 用内置字典 ── */
/* 与 .astrbot-plugin/i18n/zh-CN.json、en-US.json 的 views.chat 键保持一致。 */

const TRANSLATIONS = {
  "zh-CN": {
    "title": "对话",
    "description": "酒馆风对话界面：选择人格、发送消息、查看流式回复。",
    "loading.initial": "连接宿主中",
    "loading.data": "加载数据中",
    "loading.done": "就绪",
    "conn.ok": "已连接",
    "conn.reconnecting": "重连中",
    "conn.offline": "离线",
    "banner.reconnecting": "与后端的连接中断，正在自动重连…",
    "banner.streamUnavailable": "流式不可用：回复将在生成完成后经历史刷新显示",
    "banner.mock": "演示模式（mock）：当前数据为本地假数据，不影响真实宿主",
    "banner.gone": "生成流已失效（会话已结束或缓冲过期），已停止恢复",
    "persona.label": "PERSONA · 人格",
    "persona.hint": "选择人格将新建一个绑定该人格的会话",
    "persona.empty": "宿主暂未配置人格",
    "persona.badge.tools": "TOOLS",
    "persona.badge.default": "DEFAULT",
    "convo.label": "CONVERSATIONS · 会话",
    "convo.new": "新建会话",
    "convo.empty": "暂无会话：选择上方人格，或点右上 + 新建",
    "convo.untitled": "新会话",
    "convo.delete": "删除会话",
    "convo.busy": "生成中",
    "convo.legacyBadge": "旧版",
    "dialog.deleteTitle": "删除会话",
    "dialog.deleteBody": "将删除「{title}」的权威历史、富历史与全部附件，且不可恢复。\n确认删除？",
    "dialog.cancel": "取消",
    "dialog.confirm": "确认删除",
    "dialog.ok": "知道了",
    "dialog.close": "关闭",
    "empty.noConvoTitle": "暂无对话",
    "empty.noConvoHint": "在下方输入框发送消息，即以宿主默认人格开启会话。",
    "empty.emptyConvoHint": "发送第一条消息，开始与「{persona}」的对话。",
    "history.loading": "加载更早消息中",
    "history.more": "加载更早消息",
    "history.end": "已到最早消息",
    "msg.roleUser": "USER",
    "msg.roleAssistant": "AI",
    "msg.thinking": "THINKING · 思考中",
    "msg.reasoning": "THINKING · 思考内容",
    "msg.reasoningDone": "已完成思考",
    "msg.stopped": "已停止",
    "msg.failed": "生成失败",
    "msg.usage": "TOKENS {out}/{total}",
    "attach.truncated": "已截断",
    "tools.tag": "TOOL_CALL",
    "tools.args": "ARGS · 参数",
    "tools.result": "RESULT · 结果",
    "tools.running": "RUNNING · 执行中",
    "tools.done": "DONE · 完成",
    "tools.failed": "FAILED · 失败",
    "composer.placeholder": "输入消息，Enter 发送 / Shift+Enter 换行",
    "composer.send": "发送",
    "composer.stop": "停止",
    "composer.attach": "添加附件",
    "composer.attachImage": "图片",
    "composer.attachText": "文本文件",
    "composer.imageDisabled": "当前人格的模型可能不支持图片输入，图片附件已禁用",
    "composer.uploading": "上传中…",
    "composer.uploadProgress": "上传中 {pct}%",
    "composer.dropHint": "松开以添加附件",
    "composer.sending": "发送中…",
    "composer.legacyNotice": "旧版会话为只读归档，无法继续对话",
    "composer.mergeTip": "并入下一条（合并发送）",
    "composer.queuedBadge": "待合并",
    "composer.queuedRemove": "移除",
    "composer.replying": "{persona} 正在回复中",
    "stats.cache": "缓存 {pct}",
    "stats.context": "上下文 {used}/{max}",
    "stats.contextNoMax": "上下文 {used}",
    "error.sessionBusy": "该会话正在生成回复，请等待完成或先停止",
    "error.concurrencyLimit": "全局并发已达上限，请稍后再试",
    "error.payloadTooLarge": "文件超过上传大小上限",
    "error.unsupportedMedia": "文件类型不在白名单内",
    "error.modelNotMultimodal": "当前模型不支持图片输入",
    "error.sendFailed": "发送失败",
    "error.stopFailed": "停止失败",
    "error.loadFailed": "加载失败",
    "error.streamUnavailable": "流式不可用",
    "error.subscribeGone": "生成流不存在或已过期",
    "error.pollTimeout": "等待回复超时：生成可能仍在进行，稍后刷新查看",
    "login.title": "独立端口访问",
    "login.hint": "已配置访问授权码",
    "login.placeholder": "访问密码",
    "login.submit": "进入",
    "login.checking": "验证中…",
    "login.failed": "密码错误或服务不可用",
    "login.locked": "错误次数过多，请 60 秒后再试",
    "lightbox.download": "下载原图",
    "lightbox.previewOnly": "内嵌模式受沙箱限制，仅显示缩略预览",
    "rail.label": "SESSION",
    "toast.deleted": "会话已删除",
    "toast.created": "会话已创建",
    "toast.personaSwitched": "已新建人格会话",
    "settings.title": "界面设置",
    "settings.section.avatars": "头像",
    "settings.section.layout": "布局",
    "settings.section.components": "消息组件",
    "settings.showAvatars": "显示消息头像",
    "settings.avatarUser": "用户头像",
    "settings.avatarAssistant": "AI 头像",
    "settings.avatar.upload": "上传",
    "settings.avatar.clear": "清除",
    "settings.avatar.decodeFailed": "图片解析失败，请换一张图片",
    "settings.avatarHint": "头像默认隐藏；未上传时 AI 显示人格名首字徽标，用户显示图标。",
    "settings.flowWidth": "消息流宽度",
    "settings.flowWidth.normal": "标准",
    "settings.flowWidth.wide": "宽",
    "settings.flowWidth.full": "全宽",
    "settings.compact": "紧凑模式",
    "settings.showTimestamps": "显示时间戳",
    "settings.showUsage": "显示 TOKENS 用量",
    "settings.showReasoningDefault": "思考内容默认展开",
  },
  "en-US": {
    "title": "Chat",
    "description": "Tavern-style chat: pick a persona, send messages, watch streaming replies.",
    "loading.initial": "Connecting host",
    "loading.data": "Loading data",
    "loading.done": "Ready",
    "conn.ok": "Online",
    "conn.reconnecting": "Reconnecting",
    "conn.offline": "Offline",
    "banner.reconnecting": "Connection to backend lost, reconnecting…",
    "banner.streamUnavailable": "Streaming unavailable: replies will appear via history refresh after completion",
    "banner.mock": "Demo mode (mock): data is local fake data and never touches a real host",
    "banner.gone": "Generation stream expired (finished or buffer TTL passed); recovery stopped",
    "persona.label": "PERSONA",
    "persona.hint": "Picking a persona opens a new conversation bound to it",
    "persona.empty": "No personas configured on the host",
    "persona.badge.tools": "TOOLS",
    "persona.badge.default": "DEFAULT",
    "convo.label": "CONVERSATIONS",
    "convo.new": "New conversation",
    "convo.empty": "No conversations yet: pick a persona above, or press + to create one",
    "convo.untitled": "New chat",
    "convo.delete": "Delete conversation",
    "convo.busy": "busy",
    "convo.legacyBadge": "Legacy",
    "dialog.deleteTitle": "Delete conversation",
    "dialog.deleteBody": 'This permanently deletes "{title}" with its history and all attachments.\nContinue?',
    "dialog.cancel": "Cancel",
    "dialog.confirm": "Delete",
    "dialog.ok": "OK",
    "dialog.close": "Close",
    "empty.noConvoTitle": "No conversations",
    "empty.noConvoHint": "Send a message below to start a chat with the host's default persona.",
    "empty.emptyConvoHint": 'Send the first message to start talking with "{persona}".',
    "history.loading": "Loading earlier messages",
    "history.more": "Load earlier messages",
    "history.end": "Beginning of history",
    "msg.roleUser": "USER",
    "msg.roleAssistant": "AI",
    "msg.thinking": "THINKING",
    "msg.reasoning": "THINKING · REASONING",
    "msg.reasoningDone": "Reasoning complete",
    "msg.stopped": "Stopped",
    "msg.failed": "Generation failed",
    "msg.usage": "TOKENS {out}/{total}",
    "attach.truncated": "Truncated",
    "tools.tag": "TOOL_CALL",
    "tools.args": "ARGS",
    "tools.result": "RESULT",
    "tools.running": "RUNNING",
    "tools.done": "DONE",
    "tools.failed": "FAILED",
    "composer.placeholder": "Type a message; Enter to send, Shift+Enter for newline",
    "composer.send": "Send",
    "composer.stop": "Stop",
    "composer.attach": "Add attachments",
    "composer.attachImage": "Image",
    "composer.attachText": "Text file",
    "composer.imageDisabled": "Image input may be unsupported by this persona's model; image attachments disabled",
    "composer.uploading": "Uploading…",
    "composer.uploadProgress": "Uploading {pct}%",
    "composer.dropHint": "Drop to add attachments",
    "composer.sending": "Sending…",
    "composer.legacyNotice": "Legacy conversations are read-only archives.",
    "composer.mergeTip": "Queue & merge into next send",
    "composer.queuedBadge": "QUEUED",
    "composer.queuedRemove": "Remove queued",
    "composer.replying": "{persona} is replying…",
    "stats.cache": "Cache {pct}",
    "stats.context": "Context {used}/{max}",
    "stats.contextNoMax": "Context {used}",
    "error.sessionBusy": "This conversation is already generating. Wait for it to finish or stop it first.",
    "error.concurrencyLimit": "Global concurrency limit reached. Try again later.",
    "error.payloadTooLarge": "File exceeds the upload size limit",
    "error.unsupportedMedia": "File type not in the whitelist",
    "error.modelNotMultimodal": "The current model does not support image input",
    "error.sendFailed": "Failed to send",
    "error.stopFailed": "Failed to stop",
    "error.loadFailed": "Failed to load",
    "error.streamUnavailable": "Streaming unavailable",
    "error.subscribeGone": "Generation stream not found or expired",
    "error.pollTimeout": "Timed out waiting for the reply; it may still be generating — refresh later",
    "login.title": "Standalone access",
    "login.hint": "An access authorization code has been configured",
    "login.placeholder": "Access password",
    "login.submit": "Sign in",
    "login.checking": "Verifying…",
    "login.failed": "Wrong password or service unavailable",
    "login.locked": "Too many attempts; wait 60 seconds",
    "lightbox.download": "Download original",
    "lightbox.previewOnly": "Embedded mode is sandboxed; only the preview is shown",
    "rail.label": "SESSION",
    "toast.deleted": "Conversation deleted",
    "toast.created": "Conversation created",
    "toast.personaSwitched": "New persona conversation created",
    "settings.title": "Interface settings",
    "settings.section.avatars": "Avatars",
    "settings.section.layout": "Layout",
    "settings.section.components": "Message components",
    "settings.showAvatars": "Show message avatars",
    "settings.avatarUser": "User avatar",
    "settings.avatarAssistant": "AI avatar",
    "settings.avatar.upload": "Upload",
    "settings.avatar.clear": "Clear",
    "settings.avatar.decodeFailed": "Could not decode the image; try another one",
    "settings.avatarHint": "Avatars are hidden by default; without one, the AI shows the persona's initial and the user shows an icon.",
    "settings.flowWidth": "Message flow width",
    "settings.flowWidth.normal": "Normal",
    "settings.flowWidth.wide": "Wide",
    "settings.flowWidth.full": "Full",
    "settings.compact": "Compact mode",
    "settings.showTimestamps": "Show timestamps",
    "settings.showUsage": "Show TOKENS usage",
    "settings.showReasoningDefault": "Expand reasoning by default",
  },
};

function lookupDict(locale, key) {
  const dict = TRANSLATIONS[locale];
  if (dict && dict[key] !== undefined && dict[key] !== null) return String(dict[key]);
  return undefined;
}

/* ── 工具 ─────────────────────────────────────────────── */

/**
 * 给 Promise 加超时。超时抛 TransportError（网络级）。
 * @template T
 * @param {Promise<T>} promise
 * @param {number} ms
 * @param {string} label
 * @returns {Promise<T>}
 */
async function timeoutOf(promise, ms, label) {
  let timer = 0;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = window.setTimeout(() => reject(new TransportError(`${label} timeout`)), ms);
      }),
    ]);
  } finally {
    window.clearTimeout(timer);
  }
}

/** 当前页面是否为顶层窗口（独立端口页判定）。 */
function isTopLevel() {
  try {
    return window.parent === window;
  } catch {
    return false;
  }
}

/** @param {string} href */
function guessLocale() {
  try {
    const lang = (navigator.language || "zh-CN").toLowerCase();
    return lang.startsWith("zh") ? "zh-CN" : "en-US";
  } catch {
    return "zh-CN";
  }
}

/* ══ postMessage 兜底客户端（协议与宿主 plugin_page_bridge.js 一致） ══ */

/**
 * @typedef {Object} SseMessagePayload
 * @property {string} raw
 * @property {any} parsed
 * @property {string} eventType
 * @property {string} lastEventId
 */

/**
 * 自研 bridge 客户端：视图内直接与父页面握手（方案参考 NoelOrin/webchat MIT，
 * 协议字段与 AstrBot v4.29.4 plugin_page_bridge.js 对齐）。
 * @param {Window} win
 */
export function createPostMessageBridge(win = window) {
  /** @type {Map<string, {resolve: (v: any) => void, reject: (e: any) => void}>} */
  const pending = new Map();
  /** @type {Map<string, {onMessage?: (p: SseMessagePayload) => void, onOpen?: () => void, onError?: () => void}>} */
  const sseHandlers = new Map();
  /** @type {Set<(ctx: any) => void>} */
  const contextHandlers = new Set();
  let requestCounter = 0;
  let subscriptionCounter = 0;
  let context = null;
  let parentOrigin = null;
  let disposed = false;
  let resolveReady = null;
  const readyPromise = new Promise((resolve) => {
    resolveReady = resolve;
  });

  // opaque origin 下 location.origin 为 "null"，postMessage 目标用 "*" 兜底；
  // 接收侧以 event.source === window.parent 且 channel 匹配为准。
  function send(payload) {
    const parent = win.parent;
    if (!parent || parent === win) return false;
    try {
      const origin = typeof win.location?.origin === "string" && win.location.origin !== "null"
        ? win.location.origin
        : "*";
      parent.postMessage({ channel: CHANNEL, ...payload }, origin);
      return true;
    } catch {
      return false;
    }
  }

  function nextRequestId() {
    requestCounter += 1;
    return `webui_req_${requestCounter}`;
  }

  /**
   * @param {string} action
   * @param {Record<string, any>} payload
   * @param {Transferable[]} [transfer]
   */
  function request(action, payload, transfer) {
    const requestId = nextRequestId();
    return new Promise((resolve, reject) => {
      const ok = send({ kind: "request", requestId, action, ...(payload || {}) });
      if (!ok || disposed) {
        reject(new TransportError("no parent bridge target"));
        return;
      }
      pending.set(requestId, { resolve, reject });
    });
  }

  /** 只发不等回执（unsubscribe 等指令，防挂起）。 */
  function fire(action, payload) {
    return send({ kind: "request", requestId: nextRequestId(), action, ...(payload || {}) });
  }

  function applyContext(next) {
    if (!next || typeof next !== "object") return;
    context = { ...(context || {}), ...next };
    if (resolveReady) {
      resolveReady(context);
      resolveReady = null;
    }
    contextHandlers.forEach((handler) => {
      try {
        handler(context);
      } catch {
        /* 单个 handler 失败不影响其它 */
      }
    });
  }

  /** @param {MessageEvent} event */
  function onMessage(event) {
    if (event.source !== win.parent) return;
    if (parentOrigin !== null && event.origin !== parentOrigin) return;
    if (!parentOrigin && typeof event.origin === "string") parentOrigin = event.origin;
    const message = event.data;
    if (!message || message.channel !== CHANNEL) return;

    if (message.kind === "context") {
      applyContext(message.context);
      return;
    }
    if (message.kind === "response") {
      const entry = pending.get(message.requestId);
      if (!entry) return;
      pending.delete(message.requestId);
      if (message.ok) entry.resolve(message.data);
      else entry.reject(new TransportError(String(message.error ?? "bridge request failed")));
      return;
    }
    if (message.kind === "sse_message") {
      const handlers = sseHandlers.get(message.subscriptionId);
      if (handlers?.onMessage) {
        handlers.onMessage({
          raw: message.data,
          parsed: parseMaybeJson(message.data),
          eventType: message.eventType || "message",
          lastEventId: message.lastEventId ?? "",
        });
      }
      return;
    }
    if (message.kind === "sse_state") {
      const handlers = sseHandlers.get(message.subscriptionId);
      if (message.state === "open") handlers?.onOpen?.();
      if (message.state === "error") handlers?.onError?.();
    }
  }

  function parseMaybeJson(value) {
    if (typeof value !== "string") return value;
    try {
      return JSON.parse(value);
    } catch {
      return value;
    }
  }

  win.addEventListener("message", onMessage);
  // 主动报 ready：父页面收到后回 context（即使它已在 iframe load 时推过一次）
  send({ kind: "ready" });

  return {
    __source: "webui-fallback",
    /** @returns {Promise<any>} */
    ready() {
      return readyPromise;
    },
    /** 等待父页面 context；超时返回 null（用于探测父页面是否实现协议）。 */
    async waitContext(ms) {
      if (context) return context;
      try {
        return await timeoutOf(readyPromise, ms, "handshake");
      } catch {
        return null;
      }
    },
    getContext() {
      return context;
    },
    getLocale() {
      const locale = context?.locale;
      return typeof locale === "string" && locale ? locale : guessLocale();
    },
    /** @param {(ctx: any) => void} handler @returns {() => void} */
    onContext(handler) {
      if (typeof handler !== "function") return () => {};
      contextHandlers.add(handler);
      if (context) handler(context);
      return () => contextHandlers.delete(handler);
    },
    /** @param {string} endpoint @param {Record<string, any>} [params] */
    apiGet(endpoint, params) {
      return request("api:get", { endpoint, params: params || {} });
    },
    /** @param {string} endpoint @param {any} body */
    apiPost(endpoint, body) {
      return request("api:post", { endpoint, body: body || {} });
    },
    /**
     * 单文件上传（协议 action = files:upload，ArrayBuffer 以 transferable 传递）。
     * @param {string} endpoint
     * @param {File} file
     */
    async upload(endpoint, file) {
      if (!file || typeof file.arrayBuffer !== "function") {
        throw new TransportError("missing file payload");
      }
      const fileBuffer = await file.arrayBuffer();
      return request(
        "files:upload",
        {
          endpoint,
          fileName: file.name || "upload.bin",
          fileType: file.type || "application/octet-stream",
          fileLastModified: typeof file.lastModified === "number" ? file.lastModified : null,
          fileBuffer,
        },
        [fileBuffer],
      );
    },
    /**
     * 触发浏览器下载（父页面代为取文件）。
     * @param {string} endpoint @param {Record<string, any>} [params] @param {string} [filename]
     */
    download(endpoint, params, filename) {
      return request("files:download", { endpoint, params: params || {}, filename: filename || null });
    },
    /**
     * @param {string} endpoint
     * @param {{onMessage?: (p: SseMessagePayload) => void, onOpen?: () => void, onError?: () => void}} handlers
     * @param {Record<string, any>} [params]
     * @returns {Promise<string>} subscriptionId
     */
    async subscribeSSE(endpoint, handlers, params) {
      subscriptionCounter += 1;
      const subscriptionId = `webui_sse_${subscriptionCounter}`;
      sseHandlers.set(subscriptionId, handlers || {});
      try {
        await request("sse:subscribe", { endpoint, params: params || {}, subscriptionId });
        return subscriptionId;
      } catch (error) {
        sseHandlers.delete(subscriptionId);
        throw error;
      }
    },
    /** @param {string} subscriptionId */
    async unsubscribeSSE(subscriptionId) {
      sseHandlers.delete(subscriptionId);
      // 不等回执：父页面不回也不能拖住 UI（切会话/重订阅路径要求立即返回）
      fire("sse:unsubscribe", { subscriptionId });
    },
    dispose() {
      disposed = true;
      win.removeEventListener("message", onMessage);
      pending.clear();
      sseHandlers.clear();
      contextHandlers.clear();
    },
  };
}

/* ══ standalone 直连客户端（独立端口 aiohttp；契约「入口与鉴权」） ══ */

/**
 * @param {string} endpoint
 * @param {Record<string, any>} [params]
 */
function buildUrl(endpoint, params) {
  const base = `/api/${String(endpoint).replace(/^\/+/, "")}`;
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== undefined && value !== null) search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `${base}?${qs}` : base;
}

/**
 * 解析非 2xx 响应为 TransportError（兼容「包裹」与「裸 dict」两种形状，契约错误包节）。
 * @param {Response} response
 */
async function responseToError(response) {
  let body = null;
  try {
    body = await response.json();
  } catch {
    /* 非 JSON 错误体 */
  }
  const obj = body && typeof body === "object" ? body : {};
  const slug = String(obj.error ?? obj.slug ?? "");
  const message = String(obj.message ?? obj.detail ?? obj.error ?? `HTTP ${response.status}`);
  const code = typeof obj.code === "number" ? obj.code : response.status;
  return new TransportError(message, { status: response.status, slug, code });
}

/** SSE 裸流解析器：把 fetch ReadableStream 拆成 (eventType, dataJson) 帧。 */
async function consumeSseStream(response, onEvent, onOpen, signal) {
  if (onOpen) onOpen();
  const reader = /** @type {ReadableStreamDefaultReader<Uint8Array>} */ (response.body?.getReader());
  if (!reader) throw new TransportError("response body is not readable");
  const decoder = new TextDecoder();
  let buffer = "";
  let eventType = "";
  let dataLines = [];
  const flush = () => {
    if (dataLines.length === 0 && !eventType) return;
    const raw = dataLines.join("\n");
    let parsed = raw;
    try {
      parsed = JSON.parse(raw);
    } catch {
      /* 保留原文 */
    }
    onEvent({ raw, parsed, eventType: eventType || "message", lastEventId: "" });
    eventType = "";
    dataLines = [];
  };
  while (!signal.aborted) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // 逐行消费：search 只负责定位，切片必须按「行内容 + 终止符长度」前移，
    // 否则终止符在缓冲中部时 buffer 不变 → 原地死循环（评审修复：按匹配长度切片）
    for (;;) {
      const idx = buffer.search(/\r\n|\n|\r/);
      if (idx < 0) break;
      // 结尾的孤立 \r 可能是被分块截断的 \r\n 前半，等下一块再切
      if (buffer[idx] === "\r" && idx === buffer.length - 1) break;
      const termLen = buffer.startsWith("\r\n", idx) ? 2 : 1;
      const line = buffer.slice(0, idx);
      buffer = buffer.slice(idx + termLen);
      if (line === "") {
        flush();
        continue;
      }
      if (line.startsWith(":")) continue; // 注释/心跳
      if (line.startsWith("event:")) eventType = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
    }
  }
  flush();
}

/**
 * @param {string} token
 */
function authHeaders(token) {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function createStandaloneTransport() {
  let token = safeStorage.get("webui.standaloneToken") || "";
  let locale = guessLocale();
  /** @type {Set<(ctx: any) => void>} */
  const contextHandlers = new Set();

  async function probe() {
    const response = await fetch(buildUrl("health"), {
      headers: authHeaders(token),
      signal: timeoutSignal(PROBE_TIMEOUT),
    });
    if (response.status === 401) return { status: 401 };
    if (!response.ok) throw await responseToError(response);
    const data = await response.json();
    return { status: 200, data };
  }

  function timeoutSignal(ms) {
    const anyWin = /** @type {any} */ (window);
    if (typeof anyWin.AbortSignal?.timeout === "function") return AbortSignal.timeout(ms);
    return undefined;
  }

  return {
    __source: "webui-standalone",
    hasToken: () => Boolean(token),
    /** @param {string} value */
    setToken(value) {
      token = value || "";
      if (token) safeStorage.set("webui.standaloneToken", token);
      else safeStorage.remove("webui.standaloneToken");
    },
    probe,
    /**
     * POST /api/login {password} → {token}（契约「入口与鉴权」）。
     * @param {string} password
     */
    async login(password) {
      const response = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
        signal: timeoutSignal(PROBE_TIMEOUT),
      });
      if (!response.ok) throw await responseToError(response);
      const data = await response.json();
      if (data?.token) this.setToken(String(data.token));
      return data;
    },
    async ready() {
      /* standalone 无握手 */
    },
    getLocale: () => locale,
    /** @param {(ctx: any) => void} handler @returns {() => void} */
    onContext(handler) {
      contextHandlers.add(handler);
      return () => contextHandlers.delete(handler);
    },
    /** @param {string} value */
    setLocale(value) {
      locale = value;
      contextHandlers.forEach((handler) => handler({ locale }));
    },
    /** @param {string} endpoint @param {Record<string, any>} [params] */
    async apiGet(endpoint, params) {
      const url = buildUrl(endpoint, params);
      const response = await fetch(url, { headers: authHeaders(token) });
      if (!response.ok) throw await responseToError(response);
      return response.json();
    },
    /** @param {string} endpoint @param {any} body */
    async apiPost(endpoint, body) {
      const response = await fetch(buildUrl(endpoint), {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders(token) },
        body: JSON.stringify(body ?? {}),
      });
      if (!response.ok) throw await responseToError(response);
      return response.json();
    },
    /**
     * multipart 单文件上传（字段名 file，契约 upload 节）。
     * 独立端口用 XHR 以获得真实上传进度（fetch 无法观察 upload 流）。
     * @param {string} endpoint @param {File} file
     * @param {((p: {loaded: number, total: number}) => void) | undefined} [onProgress]
     */
    async upload(endpoint, file, onProgress) {
      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", buildUrl(endpoint), true);
        if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
        xhr.upload.onprogress = (event) => {
          if (event.lengthComputable && typeof onProgress === "function") {
            onProgress({ loaded: event.loaded, total: event.total });
          }
        };
        xhr.onerror = () => reject(new TransportError("upload network error", { status: 0 }));
        xhr.onabort = () => reject(new TransportError("upload aborted", { status: 0 }));
        xhr.onload = () => {
          let body = null;
          try {
            body = JSON.parse(xhr.responseText);
          } catch {
            body = null;
          }
          if (xhr.status >= 200 && xhr.status < 300) {
            resolve(body ?? xhr.responseText);
            return;
          }
          // 错误包归一：兼容「包裹」{code,error,message} 与「裸 dict」两种形状
          const obj = body && typeof body === "object" ? body : {};
          reject(new TransportError(String(obj.message ?? obj.detail ?? obj.error ?? `HTTP ${xhr.status}`), {
            status: xhr.status,
            slug: String(obj.error ?? obj.slug ?? ""),
            code: typeof obj.code === "number" ? obj.code : xhr.status,
          }));
        };
        const form = new FormData();
        form.append("file", file, file.name || "upload.bin");
        xhr.send(form);
      });
    },
    /**
     * 浏览器下载（带 token 直链 anchor）。
     * @param {string} endpoint @param {Record<string, any>} [params] @param {string} [filename]
     */
    async download(endpoint, params, filename) {
      const url = buildUrl(endpoint, { ...(params || {}), ...(token ? { token } : {}) });
      const anchor = document.createElement("a");
      anchor.href = url;
      if (filename) anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
    },
    /**
     * 原图 <img src> 直链（契约：独立端口 <img src="/api/files/<id>?token=...">）。
     * @param {string} endpoint @param {Record<string, any>} [params]
     */
    mediaUrl(endpoint, params) {
      return buildUrl(endpoint, { ...(params || {}), ...(token ? { token } : {}) });
    },
    /**
     * fetch 流式 SSE（可 abort；EventSource 无法带 Bearer 头）。
     * @param {string} endpoint
     * @param {{onMessage?: (p: SseMessagePayload) => void, onOpen?: () => void, onError?: () => void}} handlers
     * @param {Record<string, any>} [params]
     * @returns {Promise<string>} subscriptionId（内部为 controller 引用键）
     */
    subscribeSSE(endpoint, handlers, params) {
      const controller = new AbortController();
      const subscriptionId = `standalone_sse_${Math.random().toString(36).slice(2)}`;
      controllerBySub.set(subscriptionId, controller);
      const run = (async () => {
        const response = await fetch(buildUrl(endpoint, params), {
          headers: { Accept: "text/event-stream", ...authHeaders(token) },
          signal: controller.signal,
        });
        if (!response.ok) throw await responseToError(response);
        await consumeSseStream(
          response,
          (event) => handlers?.onMessage?.(event),
          () => handlers?.onOpen?.(),
          controller.signal,
        );
      })();
      run.catch((error) => {
        controllerBySub.delete(subscriptionId);
        if (controller.signal.aborted || error?.name === "AbortError") return;
        handlers?.onError?.(error);
      });
      return run.then(() => subscriptionId);
    },
    /** @param {string} subscriptionId */
    async unsubscribeSSE(subscriptionId) {
      const controller = controllerBySub.get(subscriptionId);
      controllerBySub.delete(subscriptionId);
      if (controller) controller.abort();
    },
  };
}

/** @type {Map<string, AbortController>} */
const controllerBySub = new Map();

/* ══ 模式探测主入口 ═════════════════════════════════════ */

/**
 * 统一传输对象在四种模式下暴露同款接口：
 *   ready/getLocale/onContext/apiGet/apiPost/upload/download/mediaUrl?/
 *   subscribeSSE/unsubscribeSSE/t/mode/version/streamSupported
 *
 * @typedef {Object} Transport
 * @property {"bridge"|"fallback"|"standalone"|"mock"} mode
 * @property {string} version
 * @property {boolean} streamSupported
 * @property {boolean} needsLogin standalone 且未通过鉴权时 true
 * @property {() => Promise<void>} ready
 * @property {() => string} getLocale
 * @property {(cb: (ctx: any) => void) => () => void} onContext
 * @property {(value: string) => void} [setLocale]
 * @property {(endpoint: string, params?: Record<string, any>) => Promise<any>} apiGet
 * @property {(endpoint: string, body?: any) => Promise<any>} apiPost
 * @property {(endpoint: string, file: File, onProgress?: (p: {loaded: number, total: number}) => void) => Promise<any>} upload
 *           bridge/fallback 协议无进度事件（onProgress 不会被调用）；standalone 为 XHR 真进度；mock 为模拟进度
 * @property {(endpoint: string, params?: Record<string, any>, filename?: string) => Promise<any>} download
 * @property {(endpoint: string, params?: Record<string, any>) => string} [mediaUrl]
 * @property {(endpoint: string, handlers: any, params?: Record<string, any>) => Promise<string>} subscribeSSE
 * @property {(subscriptionId: string) => Promise<void>} unsubscribeSSE
 * @property {(key: string, fallback?: string) => string} t
 * @property {any} [raw]
 */

/**
 * 探测并返回传输对象。任何一条路失败都会落到下一级，最终保证返回可用 transport
 * （全部失败 → mock，保证 UI 永远可渲染）。
 * @param {{forceMock?: boolean, onProgress?: (pct: number, labelKey: string) => void}} [options]
 */
export async function initTransport(options = {}) {
  const { forceMock } = options;

  const makeT = (inner) => {
    /** @type {Transport["t"]} */
    const t = (key, fallback = "") => {
      // 内层各模式的 t 约定：查不到时返回 ""（不自行回退到 fallback/key）
      let viaInner;
      try {
        viaInner = inner ? inner(key, "") : undefined;
      } catch {
        viaInner = undefined;
      }
      if (typeof viaInner === "string" && viaInner !== "") return viaInner;
      const locale = transport ? transport.getLocale() : guessLocale();
      return lookupDict(locale, key)
        ?? lookupDict("zh-CN", key)
        ?? (fallback !== "" ? fallback : key);
    };
    return t;
  };

  /** @type {any} */
  let transport = null;

  /* 1) bridge：注入 SDK 存在且 health 探测通过 */
  if (!forceMock) {
    const injected = /** @type {any} */ (window).AstrBotPluginView
      || /** @type {any} */ (window).AstrBotPluginPage;
    if (injected && typeof injected.ready === "function") {
      try {
        await timeoutOf(Promise.resolve(injected.ready()), READY_TIMEOUT, "bridge ready");
        const data = await timeoutOf(
          Promise.resolve(injected.apiGet("health", {})),
          PROBE_TIMEOUT,
          "bridge health",
        );
        transport = wrapInjectedBridge(injected, data);
      } catch {
        transport = null; // 401/缺失 → 落到 fallback
      }
    }
  }

  /* 2) fallback：iframe 内无可用 SDK → 自研 postMessage 客户端 */
  if (!forceMock && !transport && !isTopLevel()) {
    const client = createPostMessageBridge(window);
    const context = await client.waitContext(HANDSHAKE_TIMEOUT);
    if (context) {
      try {
        const data = await timeoutOf(client.apiGet("health", {}), PROBE_TIMEOUT, "fallback health");
        transport = wrapFallbackBridge(client, data);
      } catch {
        client.dispose();
      }
    } else {
      client.dispose();
    }
  }

  /* 3) standalone：顶层窗口直连独立端口 */
  /** @type {boolean} */
  let needsLogin = false;
  if (!forceMock && !transport && isTopLevel()) {
    const client = createStandaloneTransport();
    try {
      const result = await client.probe();
      if (result.status === 401) {
        needsLogin = true;
        transport = wrapStandalone(client, null);
      } else {
        transport = wrapStandalone(client, result.data);
      }
    } catch {
      transport = null; // 网络不可达（本机直接开 index.html 预览）→ mock
    }
  }

  /* 4) mock：?mock=1 或全部探测失败 */
  if (!transport) {
    transport = createMockTransport();
  }

  const t = makeT(/** @type {any} */ (transport).t);
  transport.t = t;
  transport.needsLogin = Boolean(needsLogin);
  return /** @type {Transport} */ (transport);
}

/* ── 注入 SDK 包装（bridge 模式） ──────────────────────── */

function wrapInjectedBridge(inner, healthData) {
  // 约定：内层 t 查不到返回 ""，由外层 makeT 统一回退（本地字典 → 调用点 fallback）
  const t = (key) => {
    try {
      const value = inner.t ? inner.t(key, undefined) : undefined;
      if (typeof value === "string" && value !== "") return value;
    } catch {
      /* t 失败走本地字典 */
    }
    return "";
  };
  return {
    mode: "bridge",
    version: String(healthData?.version ?? PLUGIN_VERSION),
    streamSupported: true,
    needsLogin: false,
    ready: () => Promise.resolve(),
    getLocale: () => {
      try {
        const locale = inner.getLocale?.();
        return typeof locale === "string" && locale ? locale : guessLocale();
      } catch {
        return guessLocale();
      }
    },
    onContext: (cb) => {
      if (typeof inner.onContext === "function") {
        try {
          return inner.onContext(() => cb(null));
        } catch {
          return () => {};
        }
      }
      return () => {};
    },
    apiGet: (endpoint, params) => inner.apiGet(endpoint, params || {}),
    apiPost: (endpoint, body) => inner.apiPost(endpoint, body ?? {}),
    // 注入 SDK 无进度事件：onProgress 不会被调用（UI 走不确定态）
    upload: (endpoint, file) => inner.upload(endpoint, file),
    download: (endpoint, params, filename) => inner.download?.(endpoint, params, filename),
    subscribeSSE: (endpoint, handlers, params) => inner.subscribeSSE(endpoint, handlers, params || {}),
    unsubscribeSSE: (id) => inner.unsubscribeSSE(id),
    t,
    raw: inner,
  };
}

/* ── 自研 postMessage 客户端包装（fallback 模式） ──────── */

function wrapFallbackBridge(client, healthData) {
  const t = (key) => {
    // 与官方 SDK translate 相同的 locale 回退链：context.locale → zh-CN → en-US
    const locales = [client.getLocale?.(), "zh-CN", "en-US"].filter(Boolean);
    for (const locale of locales) {
      const messages = client.getContext?.()?.i18n?.[locale];
      const value = getByPath(messages, key);
      if (value !== undefined && value !== null) return String(value);
    }
    return "";
  };
  const transport = {
    mode: "fallback",
    version: String(healthData?.version ?? PLUGIN_VERSION),
    streamSupported: true, // 官方协议含 sse:subscribe；首次订阅失败时置 false
    needsLogin: false,
    ready: () => client.ready().then(() => undefined),
    getLocale: () => client.getLocale(),
    onContext: (cb) => client.onContext(() => cb(null)),
    apiGet: (endpoint, params) => client.apiGet(endpoint, params),
    apiPost: (endpoint, body) => client.apiPost(endpoint, body),
    // postMessage 协议无进度事件：onProgress 不会被调用（UI 走不确定态）
    upload: (endpoint, file) => client.upload(endpoint, file),
    download: (endpoint, params, filename) => client.download(endpoint, params, filename),
    subscribeSSE: async (endpoint, handlers, params) => {
      try {
        return await client.subscribeSSE(endpoint, handlers, params || {});
      } catch (error) {
        const text = String(error?.message ?? error ?? "");
        if (/unknown action|not support|invalid action/i.test(text)) {
          transport.streamSupported = false; // 父页面仅保 REST → UI 明示降级
        }
        throw error;
      }
    },
    unsubscribeSSE: (id) => client.unsubscribeSSE(id),
    t,
    raw: client,
  };
  return transport;
}

function getByPath(source, key) {
  if (!source || typeof source !== "object" || !key) return undefined;
  return String(key)
    .split(".")
    .reduce(
      (current, part) => (current && typeof current === "object" && part in current ? current[part] : undefined),
      source,
    );
}

/* ── standalone 客户端包装 ─────────────────────────────── */

function wrapStandalone(client, healthData) {
  const transport = {
    mode: "standalone",
    version: String(healthData?.version ?? PLUGIN_VERSION),
    streamSupported: true,
    needsLogin: false,
    ready: () => Promise.resolve(),
    getLocale: () => client.getLocale(),
    onContext: (cb) => client.onContext(() => cb(null)),
    setLocale: (value) => client.setLocale(value),
    login: (password) => client.login(password),
    hasToken: () => client.hasToken(),
    apiGet: (endpoint, params) => client.apiGet(endpoint, params),
    apiPost: (endpoint, body) => client.apiPost(endpoint, body),
    upload: (endpoint, file, onProgress) => client.upload(endpoint, file, onProgress),
    download: (endpoint, params, filename) => client.download(endpoint, params, filename),
    mediaUrl: (endpoint, params) => client.mediaUrl(endpoint, params),
    subscribeSSE: (endpoint, handlers, params) => client.subscribeSSE(endpoint, handlers, params || {}),
    unsubscribeSSE: (id) => client.unsubscribeSSE(id),
    /** 独立模式下重新探测（登录成功后调用）。 */
    reprobe: async () => {
      const result = await client.probe();
      if (result.status === 200) {
        transport.version = String(result.data?.version ?? PLUGIN_VERSION);
        transport.needsLogin = false;
      }
      return result;
    },
    t: (key) => lookupDict(client.getLocale(), key) ?? "",
    raw: client,
  };
  if (healthData === null) transport.needsLogin = true;
  return transport;
}
