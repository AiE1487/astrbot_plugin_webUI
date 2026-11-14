/**
 * store.js — 前端状态机（M2 ②）。
 *
 * 职责：
 * - 会话列表 / 当前会话 / 人格 / 富历史（分页）/ 流式缓冲状态机 / 连接与 UI 状态。
 * - 活跃 gen_id 存 sessionStorage（沙箱下经 safeStorage 退化为内存），刷新后由
 *   服务端全量重放续流（计划 F3）；收到 404 或任一终态后清理该 gen_id。
 * - busy 会话禁发（M2 ②）；流式中收到 error.code=model_not_multimodal 标记消息失败。
 * - 通知采用 topic 批处理（微任务合并），组件按 topic 粒度重渲染；
 *   增量渲染合并 DOM 写入（text_delta 只增改流式消息的文本节点）。
 *
 * 流式状态机（单条生成）：
 *   sending → streaming（reasoning/text/tool 事件持续合入）
 *   → 终态 done：构造完整 assistant 消息入历史
 *   → 终态 stopped：保留部分文本 + stopped 徽标
 *   → 终态 error(fatal)：错误面板；code=model_not_multimodal 用专门文案并标记失败
 */

import { safeStorage } from "./bridge-client.js";
import { createStreamManager } from "./sse.js";
import { toast } from "./components/panels.js";

const KEY_CURRENT_CID = "webui.currentCid";
const KEY_ACTIVE_GENS = "webui.activeGens";
const KEY_UI_SETTINGS = "webui.uiSettings";

/** UI 自定义默认值（W6/W7）：头像默认隐藏，布局/组件按原始设计。 */
const UI_SETTINGS_DEFAULTS = {
  showAvatars: false,        // 消息头像（默认不显示）
  avatarUser: "",            // 用户头像 data URL（空 = 图标兜底）
  avatarAssistant: "",       // AI（人格）头像 data URL（空 = 人格名首字徽标）
  showTimestamps: true,      // meta 行时间戳
  showUsage: true,           // TOKENS 用量脚注
  showReasoningDefault: false, // 思考内容默认展开（初值取宿主配置）
  flowWidth: "normal",       // 消息流宽度：normal(860) | wide(1080) | full
  compact: false,            // 紧凑模式（更小间距/内边距）
};

/** @returns {string} */
function localId() {
  try {
    if (typeof crypto !== "undefined" && crypto.randomUUID) return `local_${crypto.randomUUID()}`;
  } catch {
    /* 回退 */
  }
  return `local_${Date.now()}_${Math.floor(Math.random() * 1e9)}`;
}

/** @param {string} text @param {Record<string, string>} vars */
function formatVars(text, vars) {
  let out = text;
  for (const [key, value] of Object.entries(vars)) {
    out = out.split(`{${key}}`).join(value);
  }
  return out;
}

/**
 * 历史消息归一化：缺失字段补默认（契约富历史消息节）。
 * @param {any} raw
 */
function normalizeMessage(raw) {
  return {
    id: String(raw?.id ?? localId()),
    role: raw?.role === "assistant" ? "assistant" : "user",
    ts: String(raw?.ts ?? ""),
    text: String(raw?.text ?? ""),
    attachments: Array.isArray(raw?.attachments) ? raw.attachments : [],
    reasoning: raw?.reasoning && typeof raw.reasoning === "object" && raw.reasoning.content
      ? { content: String(raw.reasoning.content) }
      : null,
    tools: Array.isArray(raw?.tools) ? raw.tools : [],
    usage: raw?.usage && typeof raw.usage === "object"
      ? { output: Number(raw.usage.output ?? 0), total: Number(raw.usage.total ?? 0) }
      : null,
    error: raw?.error ? String(raw.error) : null,
    // 失败分类：后端占位写 error_code（下划线），本地流式终态写 errorCode（驼峰）
    errorCode: String(raw?.errorCode ?? raw?.error_code ?? ""),
    stopped: Boolean(raw?.stopped),
    // failed 可由分类 code 派生（如刷新恢复 model_not_multimodal 的专属文案）
    failed: Boolean(raw?.failed) || String(raw?.error_code ?? raw?.errorCode ?? "") === "model_not_multimodal",
    local: Boolean(raw?.local),
    pendingPoll: Boolean(raw?.pendingPoll),
  };
}

/**
 * @typedef {Object} StreamState
 * @property {string} genId
 * @property {string} cid
 * @property {"streaming"|"finalizing"} phase
 * @property {string} text
 * @property {string} reasoning
 * @property {boolean} reasoningActive 思考流式进行中（自动展开 + 呼吸光标依据）
 * @property {boolean} textActive 正文流式进行中（打字光标依据）
 * @property {any[]} tools
 * @property {{message: string, fatal: boolean, code?: string}|null} lastNonFatalError
 * @property {boolean} failed 生成失败标记（model_not_multimodal 等）
 */

/**
 * @param {{api: any, transport: any, t: (key: string, fallback?: string) => string}} deps
 */
export function createStore({ api, transport, t }) {
  /** @type {Set<(topics: Set<string>) => void>} */
  const listeners = new Set();
  /** @type {Set<string>} */
  const dirtyTopics = new Set();
  let flushScheduled = false;

  /**
   * @param {string} topic
   */
  function emit(topic) {
    dirtyTopics.add(topic);
    if (flushScheduled) return;
    flushScheduled = true;
    queueMicrotask(() => {
      flushScheduled = false;
      const topics = new Set(dirtyTopics);
      dirtyTopics.clear();
      for (const listener of listeners) {
        try {
          listener(topics);
        } catch (error) {
          console.error("[webui] store listener failed:", error);
        }
      }
    });
  }

  /** @type {Map<string, {messages: any[], page: number, size: number, total: number, hasMore: boolean, loading: boolean, loaded: boolean}>} */
  const histories = new Map();
  /** @type {Map<string, StreamState>} */
  const streamsByGen = new Map();
  /** @type {Map<string, string>} cid → genId */
  const activeGenByCid = new Map();

  const state = {
    mode: /** @type {string} */ (transport.mode),
    locale: transport.getLocale(),
    needsLogin: Boolean(transport.needsLogin),
    imageCapable: true,
    personas: /** @type {any[]} */ ([]),
    conversations: /** @type {any[]} */ ([]),
    currentCid: /** @type {string|null} */ (null),
    conn: /** @type {"ok"|"reconnecting"|"offline"} */ ("ok"),
    streamAvailable: transport.streamSupported !== false,
    sidebarCollapsed: false,
    switchTick: 0, // 会话切换动画标记（每次切换 +1，组件据此重播动画）
    bannerGone: false, // 流 404/过期横幅
    showReasoningDefault: false,
    uiSettings: { ...UI_SETTINGS_DEFAULTS },
    lastError: /** @type {{kind: string, message: string}|null} */ (null),
  };

  // 配置提示项（show_reasoning_default）：bridge context 若携带 config 则采用
  try {
    const contextConfig = transport.raw?.getContext?.()?.config;
    if (typeof contextConfig?.show_reasoning_default === "boolean") {
      state.showReasoningDefault = contextConfig.show_reasoning_default;
    }
  } catch {
    /* 保持默认 */
  }

  // UI 自定义（W6/W7）：默认 ← 宿主配置 ← 用户设置（持久于 safeStorage）
  try {
    const storedSettings = safeStorage.getJson(KEY_UI_SETTINGS);
    state.uiSettings = {
      ...UI_SETTINGS_DEFAULTS,
      showReasoningDefault: state.showReasoningDefault,
      ...(storedSettings && typeof storedSettings === "object" ? storedSettings : {}),
    };
  } catch {
    state.uiSettings = { ...UI_SETTINGS_DEFAULTS };
  }

  /* ── 活跃 gen_id 持久化（刷新恢复） ─────────────────── */

  function loadActiveGens() {
    const stored = safeStorage.getJson(KEY_ACTIVE_GENS);
    if (stored && typeof stored === "object") {
      for (const [cid, genId] of Object.entries(stored)) {
        if (typeof genId === "string" && genId) activeGenByCid.set(cid, genId);
      }
    }
  }

  function saveActiveGens() {
    /** @type {Record<string, string>} */
    const out = {};
    for (const [cid, genId] of activeGenByCid.entries()) out[cid] = genId;
    safeStorage.setJson(KEY_ACTIVE_GENS, out);
  }

  function clearActiveGen(cid) {
    if (activeGenByCid.delete(cid)) saveActiveGens();
  }

  function setActiveGen(cid, genId) {
    activeGenByCid.set(cid, genId);
    saveActiveGens();
  }

  /* ── 流式状态机 ─────────────────────────────────────── */

  /**
   * 把流式缓冲构造成可入历史的 assistant 消息（终态时调用）。
   * @param {StreamState} stream
   * @param {"done"|"stopped"|"error"} terminalType
   * @param {any} terminalData
   */
  function streamToMessage(stream, terminalType, terminalData) {
    const message = /** @type {any} */ ({
      id: terminalType === "done" && terminalData?.message_id
        ? String(terminalData.message_id)
        : localId(),
      role: "assistant",
      ts: new Date().toISOString(),
      text: stream.text,
    });
    if (stream.reasoning) message.reasoning = { content: stream.reasoning };
    if (stream.tools.length > 0) message.tools = stream.tools;
    if (terminalType === "done" && terminalData?.usage) {
      message.usage = {
        output: Number(terminalData.usage.output ?? 0),
        total: Number(terminalData.usage.total ?? 0),
      };
    }
    if (terminalType === "stopped") message.stopped = true;
    if (terminalType === "error") {
      message.error = terminalData?.message
        ? String(terminalData.message)
        : t("error.loadFailed", "加载失败");
      message.errorCode = terminalData?.code ? String(terminalData.code) : "";
      if (String(terminalData?.code ?? "") === "model_not_multimodal") message.failed = true;
    }
    return message;
  }

  /**
   * @param {string} genId
   * @param {import("./sse.js").StreamEvent} event
   */
  function onStreamEvent(genId, event) {
    const stream = streamsByGen.get(genId);
    if (!stream) return;
    const data = event.data ?? {};
    switch (event.type) {
      case "reasoning_delta": {
        stream.reasoning += String(data.delta ?? "");
        stream.reasoningActive = true;
        emit("stream");
        break;
      }
      case "text_delta": {
        stream.text += String(data.delta ?? "");
        stream.textActive = true;
        if (stream.reasoningActive) {
          stream.reasoningActive = false; // 正文开始 → 思考区自动收起
        }
        emit("stream");
        break;
      }
      case "tool_call": {
        if (data.phase === "start" && data.id) {
          stream.tools.push({
            id: String(data.id),
            name: String(data.name ?? ""),
            args: data.args ?? {},
            phase: "running",
          });
          emit("stream");
        }
        break;
      }
      case "tool_result": {
        const tool = stream.tools.find((item) => item.id === String(data.id));
        if (tool) {
          if (data.result !== undefined && data.result !== null) {
            tool.result = typeof data.result === "string" ? data.result : JSON.stringify(data.result);
          } else if (data.error) {
            tool.error = String(data.error);
          }
          tool.elapsed_ms = Number(data.elapsed_ms ?? 0) || undefined;
          tool.phase = "done";
          emit("stream");
        }
        break;
      }
      case "error": {
        // fatal 已由 sse 层转终态；这里只接流内非 fatal 错误（流继续）
        stream.lastNonFatalError = {
          message: String(data.message ?? ""),
          fatal: false,
          code: data.code ? String(data.code) : undefined,
        };
        emit("stream");
        break;
      }
      default:
        break;
    }
  }

  /**
   * @param {string} genId
   * @param {"done"|"stopped"|"error"} terminalType
   * @param {any} terminalData
   */
  function onStreamTerminal(genId, terminalType, terminalData) {
    const stream = streamsByGen.get(genId);
    if (!stream) return;
    clearActiveGen(stream.cid);
    streamsByGen.delete(genId);
    const message = streamToMessage(stream, terminalType, terminalData);
    const history = ensureHistory(stream.cid);
    // 替换本地占位：移除此前插入的流式占位（若有），追加正式消息
    history.messages = history.messages.filter((item) => item.id !== `stream:${genId}`);
    history.messages.push(normalizeMessage(message));
    state.bannerGone = false;
    emit("messages");
    emit("conversations"); // busy 解除 + updated_at 变化
    refreshConversationsSoon();
  }

  /** 清理未落定的流式占位（评审发现 3：exhausted/gone 路径与终态同样清理）。 */
  function discardStreamPlaceholder(genId) {
    const stream = streamsByGen.get(genId);
    if (!stream) return;
    const { cid } = stream;
    clearActiveGen(cid);
    streamsByGen.delete(genId);
    const history = ensureHistory(cid);
    history.messages = history.messages.filter((item) => item.id !== `stream:${genId}`);
    emit("messages");
  }

  /**
   * @param {string} genId
   * @param {string} streamState
   */
  function onStreamState(genId, streamState) {
    if (streamState === "retrying") {
      state.conn = "reconnecting";
      emit("conn");
    } else if (streamState === "exhausted") {
      // 重试耗尽：与终态同样清理占位，避免「发送中…」永久残留（评审发现 3）
      discardStreamPlaceholder(genId);
      state.bannerGone = true;
      emit("stream");
      refreshConversationsSoon();
    } else if (streamState === "open") {
      if (state.conn !== "ok") {
        state.conn = "ok";
        emit("conn");
      }
    }
    // "closed"：本地主动 abort（会话删除/停止后本地落定），由调用方负责清理
  }

  /**
   * @param {string} genId
   */
  function onStreamGone(genId) {
    discardStreamPlaceholder(genId);
    state.bannerGone = true;
    emit("stream");
    refreshConversationsSoon();
  }

  loadActiveGens();

  // 流式订阅管理：回调直接接入本 store 的事件/终态处理（函数声明已提升）
  const streams = createStreamManager(transport, {
    onEvent: onStreamEvent,
    onTerminal: onStreamTerminal,
    onState: onStreamState,
    onGone: onStreamGone,
  });

  /* ── 历史与数据装载 ─────────────────────────────────── */

  function ensureHistory(cid) {
    let entry = histories.get(cid);
    if (!entry) {
      entry = {
        messages: [],
        page: 0,
        size: 30,
        total: 0,
        hasMore: false,
        loading: false,
        loaded: false,
      };
      histories.set(cid, entry);
    }
    return entry;
  }

  /** @param {string} cid @param {number} page */
  async function fetchHistoryPage(cid, page) {
    const entry = ensureHistory(cid);
    if (entry.loading) return { prepended: [] };
    entry.loading = true;
    emit("messages");
    try {
      const data = await api.getHistory(cid, { page, size: entry.size });
      const incoming = data.messages.map(normalizeMessage);
      if (page <= 1) {
        entry.messages = incoming;
        entry.page = data.page || 1;
      } else {
        // 向上 prepend 更早一页（页内旧→新，直接整页插到头部）
        entry.messages = [...incoming, ...entry.messages];
        entry.page = page;
      }
      entry.total = data.total;
      entry.hasMore = data.hasMore;
      entry.loaded = true;
      return { prepended: page > 1 ? incoming : [] };
    } finally {
      entry.loading = false;
      emit("messages");
    }
  }

  /** @param {string} cid */
  async function loadHistory(cid) {
    const entry = ensureHistory(cid);
    entry.messages = [];
    entry.page = 0;
    entry.hasMore = false;
    await fetchHistoryPage(cid, 1);
    // 若该会话有活跃 gen（刷新恢复），重建流式占位并重订阅；
    // 降级模式（流式不可用）不订阅，活跃 gen 保留供停止/轮询判定
    const genId = activeGenByCid.get(cid);
    if (genId && state.streamAvailable) {
      restoreStream(cid, genId);
    }
  }

  /**
   * 刷新/重进会话时的流恢复：重订阅靠服务端全量重放重建文本。
   * @param {string} cid @param {string} genId
   */
  function restoreStream(cid, genId) {
    const stream = /** @type {StreamState} */ ({
      genId,
      cid,
      phase: "streaming",
      text: "",
      reasoning: "",
      reasoningActive: false,
      textActive: false,
      tools: [],
      lastNonFatalError: null,
      failed: false,
    });
    streamsByGen.set(genId, stream);
    streams.ensure(genId);
    emit("stream");
  }

  /** @param {string} cid @returns {boolean} 是否确实触发了重订阅 */
  function resubscribeIfActive(cid) {
    const genId = activeGenByCid.get(cid);
    if (!genId) return false;
    if (!state.streamAvailable) return false; // 降级模式：不订阅（评审发现 9 配套）
    if (!streamsByGen.has(genId)) restoreStream(cid, genId);
    else streams.ensure(genId);
    return true;
  }

  /* ── 会话/人格 ──────────────────────────────────────── */

  async function loadPersonas() {
    const data = await api.listPersonas();
    state.imageCapable = data.imageCapable;
    state.personas = data.personas;
    emit("personas");
  }

  async function loadConversations() {
    state.conversations = await api.listConversations();
    emit("conversations");
  }

  let convoRefreshTimer = null;
  function refreshConversationsSoon() {
    if (convoRefreshTimer !== null) return;
    convoRefreshTimer = window.setTimeout(async () => {
      convoRefreshTimer = null;
      try {
        await loadConversations();
      } catch {
        /* 列表刷新失败不阻塞 UI */
      }
    }, 600);
  }

  /** @param {string} cid */
  function isBusy(cid) {
    const convo = state.conversations.find((item) => item.cid === cid);
    return Boolean(convo?.busy);
  }

  /** @param {string} cid @returns {any} */
  function conversationOf(cid) {
    return state.conversations.find((item) => item.cid === cid) || null;
  }

  /** @param {string} personaId @returns {any} */
  function personaOf(personaId) {
    return state.personas.find((item) => item.id === personaId) || null;
  }

  /** @param {string} cid */
  async function switchConversation(cid) {
    if (state.currentCid === cid) return;
    state.currentCid = cid;
    safeStorage.set(KEY_CURRENT_CID, cid);
    state.switchTick += 1;
    state.bannerGone = false;
    emit("ui");
    emit("messages");
    const entry = ensureHistory(cid);
    if (!entry.loaded) {
      await loadHistory(cid);
    } else {
      resubscribeIfActive(cid);
    }
  }

  /** @param {string} personaId @param {string} [title] */
  async function createConversation(personaId, title) {
    const cid = await api.createConversation({ personaId, title });
    await loadConversations();
    await switchConversation(cid);
    return cid;
  }

  /** @param {string} cid */
  async function deleteConversation(cid) {
    // abort 该会话所有活跃流（本地关闭，不再重试）并中止降级轮询
    activePolls.delete(cid);
    for (const [genId, stream] of Array.from(streamsByGen.entries())) {
      if (stream.cid === cid) {
        streams.abort(genId);
        streamsByGen.delete(genId);
        clearActiveGen(cid);
      }
    }
    await api.deleteConversation(cid);
    histories.delete(cid);
    await loadConversations();
    if (state.currentCid === cid) {
      const next = state.conversations[0]?.cid ?? null;
      state.currentCid = null;
      if (next) await switchConversation(next);
      else {
        safeStorage.remove(KEY_CURRENT_CID);
        emit("ui");
        emit("messages");
      }
    }
  }

  /* ── 发送 / 停止 ────────────────────────────────────── */

  /** @param {number} ms @returns {Promise<void>} */
  function sleep(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  /** 降级轮询进行中的 cid 集合（stopGeneration / 会话删除据此中止轮询）。@type {Set<string>} */
  const activePolls = new Set();

  /**
   * 流式不可用（兜底通道仅保 REST）时的降级：发送成功后轮询历史。
   * 完成判定用服务端 total 增量——页长封顶 30（size 上限 30），与本地条数
   * 对比在长历史（total≥30）下恒不命中（评审发现 4）。约 2 分钟超时后清理
   * pendingPoll 占位并提示；stopGeneration / 会话删除会中止轮询。
   * @param {string} cid @param {number} baselineTotal 发送前服务端 total
   */
  async function pollHistoryUntilReply(cid, baselineTotal) {
    activePolls.add(cid);
    try {
      for (let attempt = 0; attempt < 30; attempt += 1) {
        await sleep(4000);
        if (!activePolls.has(cid)) return; // 已被停止/删除会话中止
        try {
          const data = await api.getHistory(cid, { page: 1, size: 30 });
          if (Number(data.total) > baselineTotal) {
            const entry = ensureHistory(cid);
            entry.messages = data.messages.map(normalizeMessage);
            entry.page = data.page || 1;
            entry.total = data.total;
            entry.hasMore = data.hasMore;
            entry.loaded = true;
            clearActiveGen(cid); // 回复已落定，清活跃 gen（停止按钮随之切回发送）
            emit("messages");
            refreshConversationsSoon();
            return;
          }
        } catch {
          /* 单次轮询失败继续 */
        }
      }
      // 超时：清理 pendingPoll 占位并提示（评审发现 4）
      const entry = ensureHistory(cid);
      entry.messages = entry.messages.filter((item) => !item.pendingPoll);
      clearActiveGen(cid);
      emit("messages");
      toast(tf("error.pollTimeout", "等待回复超时：生成可能仍在进行，稍后刷新查看"), "warning");
    } finally {
      activePolls.delete(cid);
    }
  }

  /**
   * 发送消息。attachmentMetas 为 composer 上传完成后的附件元数据（契约同构）。
   * @param {{text: string, attachments?: any[]}} params
   */
  async function sendMessage({ text, attachments = [] }) {
    let cid = state.currentCid;
    if (!cid) {
      // 无会话时按默认人格自动建会话（F6：无引导可完成「选人格→发消息」）
      const personaId = state.personas.find((item) => item.is_default)?.id ?? state.personas[0]?.id;
      if (!personaId) throw new Error("no persona available");
      cid = await createConversation(personaId);
    }
    // v1.1 可选字段 legacy:true：旧版归档只读。composer 已禁发（canSend 短路），
    // 这里兜底拦截绕过 UI 的直调路径；deleteConversation 不受限。
    if (conversationOf(cid)?.legacy) {
      throw new Error(t("composer.legacyNotice", "旧版会话为只读归档，无法继续对话"));
    }
    const fileIds = attachments.map((meta) => String(meta.file_id));
    const userMessage = normalizeMessage({
      id: localId(),
      role: "user",
      ts: new Date().toISOString(),
      text,
      attachments,
      local: true,
    });
    const entry = ensureHistory(cid);
    entry.messages.push(userMessage);
    emit("messages");

    // 降级分支：流式不可用 → 不建流式占位，发送后轮询历史（total 增量判定，
    // 评审发现 4）；登记活跃 gen_id 使 stop/<gen_id> 在降级模式同样可用（发现 9）
    if (!state.streamAvailable) {
      const baselineTotal = Number(entry.total ?? 0);
      const genId = await api.send(cid, text, fileIds);
      entry.messages.push(normalizeMessage({
        id: localId(),
        role: "assistant",
        ts: new Date().toISOString(),
        text: "",
        pendingPoll: true,
      }));
      emit("messages");
      setActiveGen(cid, genId);
      pollHistoryUntilReply(cid, baselineTotal);
      refreshConversationsSoon();
      return { cid, genId };
    }

    const genId = await api.send(cid, text, fileIds);
    const stream = /** @type {StreamState} */ ({
      genId,
      cid,
      phase: "streaming",
      text: "",
      reasoning: "",
      reasoningActive: false,
      textActive: false,
      tools: [],
      lastNonFatalError: null,
      failed: false,
    });
    streamsByGen.set(genId, stream);
    setActiveGen(cid, genId);
    // 流式占位消息（done 时替换为正式消息）
    entry.messages.push(normalizeMessage({
      id: `stream:${genId}`,
      role: "assistant",
      ts: new Date().toISOString(),
      text: "",
      streamingGenId: genId,
    }));
    emit("stream");
    streams.ensure(genId);
    refreshConversationsSoon();
    return { cid, genId };
  }

  /** @param {string} cid */
  async function stopGeneration(cid) {
    const genId = activeGenByCid.get(cid);
    if (!genId) return false;
    // 降级模式：无流式订阅，无 SSE 终态事件可等 —— 本地收尾（评审发现 9）
    const isDegrade = !streamsByGen.has(genId);
    try {
      await api.stop(genId);
      if (isDegrade) {
        activePolls.delete(cid);
        clearActiveGen(cid);
        const entry = ensureHistory(cid);
        entry.messages = entry.messages.filter((item) => !item.pendingPoll);
        emit("messages");
        emit("stream");
        refreshConversationsSoon();
      }
      // 流式模式：契约规定服务端先写入 stopped 终态再 cancel，等终态事件落 UI
      return true;
    } catch (error) {
      const status = error?.status ?? 0;
      if (status === 404) {
        // gen 不存在或已结束：本地按 stopped 落定
        streams.abort(genId);
        activePolls.delete(cid);
        const stream = streamsByGen.get(genId);
        if (stream) {
          streamsByGen.delete(genId);
          clearActiveGen(cid);
          const history = ensureHistory(cid);
          history.messages = history.messages.filter((item) => item.id !== `stream:${genId}`);
          history.messages.push(normalizeMessage(streamToMessage(stream, "stopped", { message: "" })));
          emit("messages");
        }
        return true;
      }
      throw error;
    }
  }

  /* ── 连接心跳 ───────────────────────────────────────── */

  async function ping() {
    try {
      await api.health();
      if (state.conn !== "ok") {
        state.conn = "ok";
        emit("conn");
      }
      return true;
    } catch {
      if (state.conn === "ok") {
        state.conn = "reconnecting";
        emit("conn");
      }
      return false;
    }
  }

  /** @param {"ok"|"reconnecting"|"offline"} value */
  function setConn(value) {
    if (state.conn !== value) {
      state.conn = value;
      emit("conn");
    }
  }

  /* ── 派生读取 ───────────────────────────────────────── */

  function currentHistory() {
    return state.currentCid ? histories.get(state.currentCid) ?? null : null;
  }

  /**
   * 渲染用消息列表：历史消息 + 当前会话的流式占位合成。
   * @param {string} cid
   */
  function viewMessages(cid) {
    const entry = histories.get(cid);
    const base = entry ? entry.messages : [];
    const genId = activeGenByCid.get(cid);
    const stream = genId ? streamsByGen.get(genId) : null;
    if (!stream) return base.map((item) => ({ ...item }));
    // 流式占位：动态合成（消息列表里的占位仅保证顺序位置）
    const placeholderIndex = base.findIndex((item) => item.id === `stream:${genId}`);
    const streamView = {
      id: `stream:${genId}`,
      role: "assistant",
      ts: new Date().toISOString(),
      text: stream.text,
      reasoning: stream.reasoning ? { content: stream.reasoning } : null,
      reasoningActive: stream.reasoningActive,
      textActive: stream.textActive,
      tools: stream.tools.map((tool) => ({ ...tool })),
      usage: null,
      error: stream.lastNonFatalError?.message ?? null,
      streaming: true,
      failed: stream.failed,
    };
    if (placeholderIndex >= 0) {
      const copy = base.map((item) => ({ ...item }));
      copy[placeholderIndex] = streamView;
      return copy;
    }
    return [...base.map((item) => ({ ...item })), streamView];
  }

  /* ── i18n 便捷（带 {var} 格式化） ───────────────────── */

  function tf(key, fallback, vars) {
    return formatVars(t(key, fallback), vars || {});
  }

  return {
    state,
    emit,
    on(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    tf,
    t,
    ensureHistory,
    fetchHistoryPage,
    loadHistory,
    loadPersonas,
    loadConversations,
    loadInitial: async () => {
      loadActiveGens();
      await loadPersonas();
      await loadConversations();
      // 恢复上次会话，否则取最新一条
      const stored = safeStorage.get(KEY_CURRENT_CID);
      const initial = (stored && state.conversations.some((item) => item.cid === stored))
        ? stored
        : state.conversations[0]?.cid ?? null;
      if (initial) await switchConversation(initial);
    },
    switchConversation,
    createConversation,
    deleteConversation,
    isBusy,
    conversationOf,
    personaOf,
    sendMessage,
    stopGeneration,
    ping,
    setConn,
    currentHistory,
    viewMessages,
    historyEntryOf: (cid) => histories.get(cid) ?? null,
    activeGenOf: (cid) => activeGenByCid.get(cid) ?? null,
    streamOfGen: (genId) => streamsByGen.get(genId) ?? null,
    streamForCid: (cid) => {
      const genId = activeGenByCid.get(cid);
      return genId ? streamsByGen.get(genId) ?? null : null;
    },
    setLocale: (value) => {
      state.locale = value;
      emit("ui");
    },
    setSidebarCollapsed: (value) => {
      state.sidebarCollapsed = value;
      emit("ui");
    },
    /** UI 自定义合并写入（设置面板；持久化 safeStorage，W7）。 */
    updateUiSettings: (patch) => {
      state.uiSettings = { ...state.uiSettings, ...patch };
      safeStorage.setJson(KEY_UI_SETTINGS, state.uiSettings);
      emit("ui");
    },
    dismissBannerGone: () => {
      state.bannerGone = false;
      emit("ui");
    },
    setStreamAvailable: (value) => {
      state.streamAvailable = value;
      emit("ui");
    },
  };
}
