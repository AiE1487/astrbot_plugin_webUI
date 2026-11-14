/**
 * message.js — 消息流渲染（M2 ③ / M3 ④⑤ / M4 ②）。
 *
 * 渲染策略（增量渲染合并 DOM 写入）：
 * - 'messages' topic → 全量重建（页面加载 / 发送 / 终态落定 / 分页，频率低）；
 * - 'stream' topic → 仅原位更新流式占位消息的文本节点 / 思考区 / 工具面板，
 *   不重建整列表；store 层已按微任务合并同帧多次事件。
 * - 用户消息右对齐青描边；AI 卡片 #282828 + 左青线（§6.2）；
 *   reasoning 折叠区流式自动展开 + 呼吸光标、完成自动收起、可点击回看；
 *   历史分页向上 prepend（页+1），滚动位置保持。
 */

import { el, icon } from "../icons.js";
import { createToolPanel, openLightbox, toast } from "./panels.js";

/** @param {string} iso */
function hhmm(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

/** @param {number} size */
function formatSize(size) {
  if (!Number.isFinite(size) || size < 0) return "";
  if (size < 1024) return `${size}B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)}KB`;
  return `${(size / (1024 * 1024)).toFixed(1)}MB`;
}

/**
 * @param {{
 *   scrollEl: HTMLElement,      // #chat-scroll（滚动容器）
 *   store: any,
 *   api: any,
 *   transport: any,
 *   t: (key: string, fallback?: string, vars?: Record<string, string>) => string,
 *   onPinnedChange?: (pinned: boolean) => void,
 *   onPersonaPick: (personaId: string) => void,
 * }} deps
 */
export function createMessageFlow({ scrollEl, store, api, transport, t, onPinnedChange, onPersonaPick }) {
  const flow = el("div", { class: "message-flow" });
  scrollEl.appendChild(flow);

  let currentCid = /** @type {string|null} */ (null);
  let pinned = true;
  /** @type {Map<string, boolean>} 用户手动开合的 reasoning（id → open） */
  const reasoningToggled = new Map();
  /** @type {{genId: string, textNode: Text, cursor: HTMLElement, reasoningWrap: HTMLElement, reasoningText: Text, reasoningCursor: HTMLElement, reasoningHeadState: HTMLElement, reasoningBlock: HTMLElement, toolsHolder: HTMLElement, waitRow: HTMLElement, panels: Map<string, {update: Function}>, msgId: string} | null} */
  let streamingRefs = null;
  let loadMoreBusy = false;

  const canFetchOriginal = transport.mode === "standalone" || transport.mode === "mock";

  /* ── 滚动与分页 ─────────────────────────────────────── */

  function isNearBottom() {
    return scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight < 90;
  }

  function scrollToBottom(instant = false) {
    if (instant) {
      scrollEl.scrollTop = scrollEl.scrollHeight;
      return;
    }
    requestAnimationFrame(() => {
      scrollEl.scrollTop = scrollEl.scrollHeight;
    });
  }

  async function loadMore() {
    const cid = currentCid;
    const entry = cid ? store.historyEntryOf(cid) : null;
    if (!cid || !entry?.hasMore || entry.loading || loadMoreBusy) return;
    loadMoreBusy = true;
    const prevHeight = scrollEl.scrollHeight;
    const prevTop = scrollEl.scrollTop;
    try {
      await store.fetchHistoryPage(cid, entry.page + 1);
      // onStoreChange 里的 renderAll 已重建；恢复滚动锚点，使视觉停留在原消息
      requestAnimationFrame(() => {
        scrollEl.scrollTop = prevTop + (scrollEl.scrollHeight - prevHeight);
      });
    } catch (error) {
      toast(error?.message || t("error.loadFailed", "加载失败"), "error");
    } finally {
      loadMoreBusy = false;
    }
  }

  let loadMoreTimer = 0;
  scrollEl.addEventListener("scroll", () => {
    const nearBottom = isNearBottom();
    if (nearBottom !== pinned) {
      pinned = nearBottom;
      onPinnedChange?.(pinned);
    }
    if (scrollEl.scrollTop < 80 && !loadMoreBusy) {
      window.clearTimeout(loadMoreTimer);
      loadMoreTimer = window.setTimeout(loadMore, 180);
    }
  });

  /* ── 子结构构建 ─────────────────────────────────────── */

  /** @param {any} meta 附件元数据（契约 upload 响应 / 富历史 attachments 项） */
  function buildAttachment(meta) {
    const isImage = meta.kind === "image";
    if (isImage) {
      const thumb = el("button", {
        class: "attach-thumb",
        type: "button",
        "aria-label": meta.name || "image",
        onclick: () => openLightbox({ meta, api, t, canFetchOriginal }),
      });
      if (meta.preview) {
        thumb.appendChild(el("img", { src: meta.preview, alt: meta.name || "image" }));
      } else {
        // preview 缺失（如非图像素材）：占位图标而非空框
        thumb.appendChild(el("span", { class: "ap-file-icon", style: { width: "100%", height: "100%" } },
          icon("image", { size: 20 })));
      }
      return thumb;
    }
    return el("button", {
      class: "attach-file",
      type: "button",
      onclick: () => openLightbox({ meta, api, t, canFetchOriginal }),
    },
      icon("fileText", { size: 18 }),
      el("span", { class: "af-info" },
        el("span", { class: "af-name", text: meta.name || "file" }),
        el("span", { class: "af-size" },
          `${meta.mime || ""} · ${formatSize(Number(meta.size ?? 0))}`,
          meta.truncated ? el("span", { class: "af-truncated", text: ` · ${t("attach.truncated", "已截断")}` }) : null,
        ),
      ),
    );
  }

  /**
   * 思考折叠区。
   * @param {{id: string, reasoning?: any, reasoningActive?: boolean, streaming?: boolean}} msg
   */
  function buildReasoning(msg) {
    const block = el("div", { class: "reasoning" });
    const headState = el("span", { class: "reasoning-state" });
    const reasoningText = document.createTextNode("");
    const reasoningCursor = el("span", { class: "breathe-cursor", style: { display: "none" } });
    const body = el("div", { class: "reasoning-body" },
      el("span", { class: "reasoning-text" }, reasoningText, reasoningCursor),
    );
    const chevron = icon("chevronDown", { size: 12, className: "reasoning-chevron" });
    const head = el("button", {
      class: "reasoning-head",
      type: "button",
      onclick: () => {
        reasoningToggled.set(msg.id, !block.classList.contains("is-open"));
        syncReasoning();
      },
    },
      icon("terminal", { size: 12 }),
      el("span", { class: "reasoning-title", text: t("msg.reasoning", "THINKING · 思考内容") }),
      headState,
      chevron,
    );
    block.append(head, body);

    /** 按当前状态决定开合与文案。 */
    function syncReasoning() {
      const active = Boolean(msg.reasoningActive);
      const content = msg.reasoning?.content ?? "";
      reasoningText.textContent = content;
      reasoningCursor.style.display = active && msg.streaming ? "" : "none";
      let open;
      if (reasoningToggled.has(msg.id)) {
        open = /** @type {boolean} */ (reasoningToggled.get(msg.id));
      } else if (msg.streaming) {
        open = active; // 流式：思考中自动展开、正文开始自动收起
      } else {
        open = Boolean(store.state.showReasoningDefault);
      }
      block.classList.toggle("is-open", open && Boolean(content));
      headState.textContent = msg.streaming && active
        ? t("msg.thinking", "THINKING · 思考中")
        : t("msg.reasoningDone", "已完成思考");
      block.style.display = content || active ? "" : "none";
    }
    syncReasoning();
    return { block, syncReasoning };
  }

  /**
   * @param {any} msg 归一化消息或流式合成视图
   */
  function buildMessage(msg) {
    const isUser = msg.role === "user";
    const wrap = el("div", { class: `msg ${isUser ? "is-user" : "is-assistant"}` });
    const meta = el("div", { class: "msg-meta" },
      el("span", { text: isUser ? t("msg.roleUser", "USER") : t("msg.roleAssistant", "AI") }),
      hhmm(msg.ts) ? el("span", { text: hhmm(msg.ts) }) : null,
    );
    wrap.appendChild(meta);

    if (isUser) {
      if (msg.text) wrap.appendChild(el("div", { class: "msg-bubble", text: msg.text }));
      if (Array.isArray(msg.attachments) && msg.attachments.length > 0) {
        wrap.appendChild(el("div", { class: "msg-attachments" }, ...msg.attachments.map(buildAttachment)));
      }
      return wrap;
    }

    // ── AI 卡片 ──
    const card = el("div", { class: "msg-card" });
    wrap.appendChild(card);
    const streaming = Boolean(msg.streaming) || Boolean(msg.pendingPoll);

    // 思考区（流式占位始终渲染：空内容时隐藏，便于增量 patch）
    if (msg.reasoning || msg.reasoningActive || msg.streaming) {
      card.appendChild(buildReasoning(msg).block);
    }

    // 正文（流式增量只改这个 text node）
    const textNode = document.createTextNode(msg.text ?? "");
    const textCursor = el("span", {
      class: "breathe-cursor",
      style: { display: msg.textActive ? "" : "none" },
    });
    if (msg.text || msg.streaming || msg.pendingPoll) {
      card.appendChild(el("div", { class: "msg-text" }, textNode, textCursor));
    }

    // 附件（v1.1：助手消息 attachments[] 与用户消息同构，渲染路径角色无关，
    // 复用同一 buildAttachment / .msg-attachments 样式，容器内左对齐见 CSS）
    if (Array.isArray(msg.attachments) && msg.attachments.length > 0) {
      card.appendChild(el("div", { class: "msg-attachments" }, ...msg.attachments.map(buildAttachment)));
    }

    // 工具面板（历史与流式共用；M3 ⑤ 分页 prepend 同样渲染 tools）
    /** @type {Map<string, {update: Function}>} */
    const panels = new Map();
    const toolsHolder = el("div", { style: { display: "flex", flexDirection: "column", gap: "6px" } });
    if (Array.isArray(msg.tools) && msg.tools.length > 0) {
      for (const tool of msg.tools) {
        const panel = createToolPanel(t);
        panel.update(tool);
        panels.set(String(tool.id), panel);
        toolsHolder.appendChild(panel.root);
      }
      card.appendChild(toolsHolder);
    }

    // 等待首增量
    const waitRow = el("div", { class: "stream-wait" }, el("span", { class: "spinner" }), t("composer.sending", "发送中…"));
    const showWait = Boolean(msg.pendingPoll)
      || (Boolean(msg.streaming) && !msg.text && !msg.reasoningActive && !(msg.tools?.length));
    waitRow.style.display = showWait ? "" : "none";
    if (msg.streaming || msg.pendingPoll) card.appendChild(waitRow);

    // 终态修饰
    if (msg.stopped) {
      card.appendChild(el("span", { class: "msg-stopped" }, icon("stop", { size: 10 }), t("msg.stopped", "已停止")));
    }
    if (msg.failed) {
      // model_not_multimodal（契约：按「发图被拒」呈现）
      card.appendChild(el("div", { class: "msg-failed" },
        icon("warning", { size: 14 }),
        t("error.modelNotMultimodal", "当前模型不支持图片输入"),
      ));
    } else if (msg.error) {
      const panel = el("div", { class: "error-panel" },
        el("div", { class: "ep-head" }, icon("warning", { size: 13 }), t("msg.failed", "生成失败")),
        el("div", { class: "ep-message", text: msg.error }),
        msg.errorCode ? el("div", { class: "ep-code", text: `code: ${msg.errorCode}` }) : null,
      );
      card.appendChild(panel);
    }

    if (msg.usage) {
      card.appendChild(el("div", { class: "msg-usage", text: t("msg.usage", "TOKENS {out}/{total}")
        .replace("{out}", String(msg.usage.output ?? 0))
        .replace("{total}", String(msg.usage.total ?? 0)) }));
    }

    // 流式占位登记（供 patchStreaming 原位更新）
    if (msg.streaming) {
      const genId = store.activeGenOf(currentCid);
      if (genId) {
        const reasoningBlock = card.querySelector(".reasoning");
        streamingRefs = {
          genId,
          msgId: msg.id,
          textNode,
          cursor: textCursor,
          reasoningWrap: /** @type {HTMLElement} */ (reasoningBlock),
          reasoningText: /** @type {Text} */ (reasoningBlock?.querySelector(".reasoning-text")?.firstChild ?? document.createTextNode("")),
          reasoningCursor: /** @type {HTMLElement} */ (reasoningBlock?.querySelector(".breathe-cursor")),
          reasoningHeadState: /** @type {HTMLElement} */ (reasoningBlock?.querySelector(".reasoning-state")),
          reasoningBlock: /** @type {HTMLElement} */ (reasoningBlock),
          toolsHolder,
          waitRow,
          panels,
        };
      }
    }
    return wrap;
  }

  /* ── 空态 ───────────────────────────────────────────── */

  function buildEmptyNoConversation() {
    return el("div", { class: "empty-state" },
      el("img", { src: "./assets/empty-state.svg", alt: "", class: "es-decoration" }),
      el("div", { class: "es-kicker", text: t("empty.kicker", "SELECT PERSONA") }),
      el("div", { class: "es-title", text: t("empty.title", "开始对话") }),
      el("div", { class: "es-hint", text: t("empty.noConvoHint", "选择一个人格建立会话，然后发送消息即可开始。") }),
      el("div", { class: "es-personas" },
        ...store.state.personas.map((persona) => el("button", {
          class: "es-persona-chip",
          type: "button",
          onclick: () => onPersonaPick(persona.id),
        },
          el("span", { class: "espc-name", text: persona.name ?? persona.id }),
          persona.system_prompt_preview
            ? el("span", { class: "espc-preview", text: persona.system_prompt_preview })
            : null,
        )),
      ),
    );
  }

  /** @param {string} cid */
  function buildEmptyConversation(cid) {
    const personaName = store.personaOf(store.conversationOf(cid)?.persona_id)?.name ?? "";
    return el("div", { class: "empty-state" },
      el("img", { src: "./assets/empty-state.svg", alt: "", class: "es-decoration" }),
      el("div", { class: "es-kicker", text: t("empty.kicker", "SELECT PERSONA") }),
      el("div", { class: "es-hint", text: t("empty.emptyConvoHint", "发送第一条消息，开始与「{persona}」的对话。")
        .replace("{persona}", personaName) }),
    );
  }

  /* ── 全量渲染 ───────────────────────────────────────── */

  function renderAll() {
    const cid = store.state.currentCid;
    const switched = cid !== currentCid;
    currentCid = cid;
    streamingRefs = null;
    flow.replaceChildren();

    if (!cid) {
      flow.appendChild(buildEmptyNoConversation());
      return;
    }

    const entry = store.historyEntryOf(cid);
    // 历史分页行（顶部）
    if (entry?.hasMore) {
      flow.appendChild(el("div", { class: "history-more is-clickable", onclick: loadMore },
        entry.loading ? el("span", { class: "spinner" }) : null,
        t("history.more", "加载更早消息"),
      ));
    } else if (entry?.loaded && entry.page > 1) {
      flow.appendChild(el("div", { class: "history-more", text: t("history.end", "已到最早消息") }));
    }

    const messages = store.viewMessages(cid);
    if (messages.length === 0) {
      flow.appendChild(buildEmptyConversation(cid));
    } else {
      for (const msg of messages) flow.appendChild(buildMessage(msg));
    }

    if (switched) {
      // 会话切换：200ms 淡入 + 8px 上移（纯 CSS 动画）
      flow.classList.remove("switch-enter");
      void flow.offsetWidth; // 重置动画
      flow.classList.add("switch-enter");
      scrollToBottom(true);
    } else if (pinned) {
      scrollToBottom();
    }
  }

  /* ── 流式原位更新 ───────────────────────────────────── */

  function patchStreaming() {
    const cid = store.state.currentCid;
    if (!cid) return;
    const genId = store.activeGenOf(cid);
    const stream = genId ? store.streamOfGen(genId) : null;
    if (!genId || !stream) return;
    if (!streamingRefs || streamingRefs.genId !== genId) {
      renderAll(); // 占位刚建立（或切回会话后首次渲染）
      return;
    }
    const refs = streamingRefs;
    // 正文
    refs.textNode.textContent = stream.text;
    refs.cursor.style.display = stream.textActive ? "" : "none";
    // 思考区
    if ((stream.reasoning || stream.reasoningActive) && refs.reasoningBlock) {
      const active = stream.reasoningActive;
      refs.reasoningText.textContent = stream.reasoning;
      if (refs.reasoningCursor) refs.reasoningCursor.style.display = active ? "" : "none";
      const userToggled = reasoningToggled.has(`stream:${genId}`);
      const open = userToggled
        ? /** @type {boolean} */ (reasoningToggled.get(`stream:${genId}`))
        : active; // 流式：自动展开；正文开始（active=false）自动收起
      refs.reasoningBlock.classList.toggle("is-open", open);
      refs.reasoningBlock.style.display = "";
      if (refs.reasoningHeadState) {
        refs.reasoningHeadState.textContent = active
          ? t("msg.thinking", "THINKING · 思考中")
          : t("msg.reasoningDone", "已完成思考");
      }
    }
    // 工具面板
    for (const tool of stream.tools) {
      const id = String(tool.id);
      let panel = refs.panels.get(id);
      if (!panel) {
        const created = createToolPanel(t);
        created.update(tool);
        refs.panels.set(id, created);
        refs.toolsHolder.appendChild(created.root);
        refs.toolsHolder.style.display = "";
        panel = created;
      } else {
        panel.update(tool);
      }
    }
    // 等待行
    const showWait = !stream.text && !stream.reasoningActive && stream.tools.length === 0;
    refs.waitRow.style.display = showWait ? "" : "none";
    if (pinned) scrollToBottom();
  }

  /* ── 通知入口 ───────────────────────────────────────── */

  /** @param {Set<string>} topics */
  function onStoreChange(topics) {
    if (topics.has("stream")) patchStreaming();
    if (topics.has("messages") || topics.has("ui")) renderAll();
  }

  return {
    flow,
    onStoreChange,
    renderAll,
    isPinned: () => pinned,
    scrollToBottom,
  };
}
