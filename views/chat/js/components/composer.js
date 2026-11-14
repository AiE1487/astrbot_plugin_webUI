/**
 * composer.js — 输入区（M2 ④ / M3 ①②）。
 *
 * - 发送 → POST send 得 gen_id 即订阅（订阅在 store.sendMessage → streams.ensure）。
 * - 停止 → POST stop/<gen_id> 后等 stopped 终态落 UI（契约：服务端先落终态再 cancel）。
 * - 409 双分支文案：session_busy / concurrency_limit。
 * - 附件：附件按钮 / 拖拽 / Ctrl+V 直接粘贴（W5）→ image/text 白名单过滤 →
 *   图片 canvas 压缩长边≤2048 再 upload（toBlob 异步完成后再提交 FormData）→
 *   预览条（缩略图/文件卡 + 可移除）→ 发送携 file_ids；upload 413/415 立即提示并阻断；
 *   personas.image_capable=false 预禁图片入口（仅启发式预检，最终裁决在生成期）。
 */

import { el, icon } from "../icons.js";
import { toast } from "./panels.js";

// 白名单与后端默认值对齐（attachment_service.py IMAGE_EXTENSIONS；
// _conf_schema.json attachment_text_whitelist 默认值），避免「前端收了、后端必拒」
const IMAGE_EXTS = new Set(["png", "jpg", "jpeg", "webp", "gif"]);
const TEXT_EXTS = new Set(["txt", "md", "json", "csv", "py"]);
const IMAGE_MAX_EDGE = 2048;

/** @param {string} name */
function extOf(name) {
  const match = /\.([A-Za-z0-9]+)$/.exec(name || "");
  return match ? match[1].toLowerCase() : "";
}

/** @param {number} size */
function formatSize(size) {
  if (!Number.isFinite(size) || size < 0) return "";
  if (size < 1024) return `${size}B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)}KB`;
  return `${(size / (1024 * 1024)).toFixed(1)}MB`;
}

/** @param {DataTransfer|null} clipboardData */
function filesFromClipboard(clipboardData) {
  if (!clipboardData) return [];
  /** @type {File[]} */
  const files = [];
  for (const item of Array.from(clipboardData.items ?? [])) {
    if (item.kind !== "file") continue;
    const file = item.getAsFile();
    if (file) files.push(file);
  }
  return files;
}

/** @param {string} mime */
function fallbackClipboardName(mime) {
  const map = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/svg+xml": "svg",
    "text/plain": "txt",
    "text/markdown": "md",
    "application/json": "json",
    "text/csv": "csv",
    "text/x-python": "py",
    "application/pdf": "pdf",
  };
  const ext = map[String(mime || "").toLowerCase()] || "bin";
  const stamp = new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
  return `clipboard-${stamp}.${ext}`;
}

/**
 * 图片压缩：长边 > 2048 时经 canvas 重采样；toBlob 异步完成后再返回新 File。
 * GIF 跳过（canvas 会丢帧）；解码失败回退原文件（由服务端白名单/大小裁决）。
 * @param {File} file
 */
async function compressImage(file) {
  if (file.type === "image/gif") return file;
  try {
    const bitmap = await createImageBitmap(file);
    const longEdge = Math.max(bitmap.width, bitmap.height);
    const needsResize = longEdge > IMAGE_MAX_EDGE;
    const needsReencode = file.size > 3 * 1024 * 1024;
    if (!needsResize && !needsReencode) return file;
    const scale = Math.min(1, IMAGE_MAX_EDGE / longEdge);
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    const ctx = /** @type {CanvasRenderingContext2D} */ (canvas.getContext("2d"));
    ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    bitmap.close?.();
    const mime = file.type === "image/png" ? "image/png" : "image/jpeg";
    const blob = await new Promise((resolve, reject) => {
      canvas.toBlob((result) => (result ? resolve(result) : reject(new Error("toBlob failed"))), mime, 0.85);
    });
    return new File([blob], file.name, { type: mime, lastModified: Date.now() });
  } catch {
    return file;
  }
}

/**
 * @param {{
 *   root: HTMLElement,       // #composer
 *   store: any,
 *   api: any,
 *   t: (key: string, fallback?: string) => string,
 * }} deps
 */
export function createComposer({ root, store, api, t }) {
  const inner = el("div", { class: "composer-inner" });

  /* ── 附件预览条 ─────────────────────────────────────── */
  /** @type {Map<string, {meta?: any, file?: File, uploading: boolean, chip: HTMLElement, metaNode: HTMLElement, infoNode: HTMLElement, progressNode: HTMLElement | null, fillNode: HTMLElement | null}>} */
  const pending = new Map();
  const previewBar = el("div", { class: "attach-preview", "aria-label": "attachments" });

  function refreshPreviewBar() {
    previewBar.classList.toggle("has-items", pending.size > 0);
  }

  /** @param {string} key */
  function removePending(key) {
    const item = pending.get(key);
    if (!item) return;
    item.chip.remove();
    pending.delete(key);
    refreshPreviewBar();
    updateButtons();
  }

  /**
   * @param {string} key
   * @param {{file?: File, meta?: any}} data
   */
  function addChip(key, { file, meta }) {
    const uploading = !meta;
    const isImage = meta ? meta.kind === "image" : (file?.type || "").startsWith("image/");
    const infoNode = el("span", { class: "ap-info" },
      el("span", { class: "ap-name", text: meta?.name ?? file?.name ?? "" }),
      el("span", { class: "ap-meta" }),
    );
    const metaNode = /** @type {HTMLElement} */ (infoNode.querySelector(".ap-meta"));
    const chip = el("div", { class: "ap-chip" });
    if (meta?.preview) {
      chip.appendChild(el("img", { class: "ap-thumb", src: meta.preview, alt: meta.name || "" }));
    } else {
      chip.appendChild(el("span", { class: "ap-file-icon" },
        icon(isImage ? "image" : "fileText", { size: 20 })));
    }
    chip.appendChild(infoNode);
    // 上传进度条（M3 要点：上传进度；无确定进度时为不确定态滑动条）
    const progressNode = el("div", { class: "ap-progress is-indeterminate" },
      el("div", { class: "ap-progress-fill" }));
    progressNode.style.display = uploading ? "" : "none";
    chip.appendChild(progressNode);
    const removeBtn = el("button", {
      class: "ap-remove",
      type: "button",
      "aria-label": t("dialog.cancel", "取消"),
      title: t("dialog.cancel", "取消"),
      onclick: () => removePending(key),
    }, icon("close", { size: 10 }));
    chip.appendChild(removeBtn);
    previewBar.appendChild(chip);
    pending.set(key, {
      file,
      meta,
      uploading,
      chip,
      metaNode,
      infoNode,
      progressNode,
      fillNode: /** @type {HTMLElement} */ (progressNode.querySelector(".ap-progress-fill")),
    });
    refreshPreviewBar();
    return chip;
  }

  /**
   * @param {string} key
   * @param {{meta?: any, error?: boolean, progress?: {loaded: number, total: number}}} patch
   */
  function updateChip(key, patch) {
    const item = pending.get(key);
    if (!item) return;
    if (patch.meta) item.meta = patch.meta;
    item.uploading = !item.meta && !patch.error;
    item.metaNode.classList.toggle("is-busy", item.uploading && !patch.error);
    if (item.meta) {
      const parts = [item.meta.mime || "", formatSize(Number(item.meta.size ?? 0))].filter(Boolean);
      item.metaNode.textContent = parts.join(" · ");
      if (item.meta.preview && !item.chip.querySelector("img")) {
        const img = el("img", { class: "ap-thumb", src: item.meta.preview, alt: item.meta.name || "" });
        item.chip.insertBefore(img, item.chip.firstChild);
      }
      item.progressNode?.remove();
      item.progressNode = null;
    } else if (patch.error) {
      item.metaNode.textContent = t("error.sendFailed", "发送失败");
      item.metaNode.classList.add("is-error");
      item.progressNode?.remove();
      item.progressNode = null;
    } else {
      const p = patch.progress;
      if (p && Number.isFinite(p.total) && p.total > 0) {
        const pct = Math.max(0, Math.min(100, Math.round((p.loaded / p.total) * 100)));
        item.metaNode.textContent = t("composer.uploadProgress", "上传中 {pct}%").replace("{pct}", String(pct));
        item.progressNode?.classList.remove("is-indeterminate");
        if (item.fillNode) item.fillNode.style.width = `${pct}%`;
      } else {
        item.metaNode.textContent = t("composer.uploading", "上传中…");
        item.progressNode?.classList.add("is-indeterminate");
      }
    }
    updateButtons();
  }

  /* ── 文件选择与上传 ─────────────────────────────────── */

  const fileInput = el("input", {
    type: "file",
    multiple: true,
    style: { display: "none" },
    onchange: () => {
      const files = Array.from(fileInput.files ?? []);
      fileInput.value = "";
      addFiles(files);
    },
  });

  /** @param {File[]} files */
  async function addFiles(files) {
    if (isLegacyConvo()) {
      // legacy 归档只读：拖拽/粘贴等入口统一拦截（附件按钮已禁用）
      toast(legacyNoticeText(), "warning");
      return;
    }
    const imageCapable = store.state.imageCapable;
    for (const rawFile of files) {
      // 粘贴的剪贴板文件可能没有文件名（截图等）：按 MIME 兜底命名
      const file = rawFile && rawFile.name
        ? rawFile
        : new File([rawFile], fallbackClipboardName(rawFile?.type), {
            type: rawFile?.type || "application/octet-stream",
            lastModified: Date.now(),
          });
      const ext = extOf(file.name);
      const isImage = (file.type || "").startsWith("image/") || IMAGE_EXTS.has(ext);
      const isText = !isImage && ((file.type || "").startsWith("text/") || TEXT_EXTS.has(ext));
      if (!isImage && !isText) {
        // 白名单外立即提示并跳过（服务端 415 兜底）
        toast(`${t("error.unsupportedMedia", "文件类型不在白名单内")}: .${ext || "?"}`, "error");
        continue;
      }
      if (isImage && !imageCapable) {
        toast(t("composer.imageDisabled", "当前人格的模型可能不支持图片输入，图片附件已禁用"), "warning");
        continue;
      }
      const key = `${Date.now()}_${Math.random().toString(36).slice(2)}`;
      addChip(key, { file });
      updateChip(key, {});
      try {
        const prepared = isImage ? await compressImage(file) : file;
        // 进度回调：standalone（XHR 真进度）/ mock（模拟）会触发；bridge 协议无进度事件
        const meta = await api.upload(prepared, (p) => updateChip(key, { progress: p })); // 413/415 由 ApiError.slug 区分
        updateChip(key, { meta });
      } catch (error) {
        removePending(key);
        if (error?.slug === "payload_too_large" || error?.status === 413) {
          toast(t("error.payloadTooLarge", "文件超过上传大小上限"), "error");
        } else if (error?.slug === "unsupported_media_type" || error?.status === 415) {
          toast(t("error.unsupportedMedia", "文件类型不在白名单内"), "error");
        } else {
          toast(error?.message || t("error.loadFailed", "加载失败"), "error");
        }
      }
    }
    updateButtons();
  }

  /* ── 拖拽上传（M3 要点：按钮 + 拖拽） ─────────────────── */

  let dragHideTimer = 0;

  /** @param {DragEvent} event */
  function dragHasFiles(event) {
    const types = event.dataTransfer?.types;
    return Boolean(types && Array.from(types).includes("Files"));
  }

  function showDragState() {
    window.clearTimeout(dragHideTimer);
    root.classList.add("is-dragover");
    // legacy 只读会话：保留原说明条，不显示拖拽提示
    if (!isLegacyConvo()) setNote(t("composer.dropHint", "松开以添加附件"));
  }

  /** @param {boolean} [immediate] */
  function hideDragState(immediate = false) {
    window.clearTimeout(dragHideTimer);
    const clear = () => {
      root.classList.remove("is-dragover");
      // 仅当脚注仍是拖拽提示时清空，避免覆盖其它提示
      if (noteNode.textContent === t("composer.dropHint", "松开以添加附件")) setNote("");
    };
    if (immediate) clear();
    else dragHideTimer = window.setTimeout(clear, 150);
  }

  // document 级监听：放行点若不在输入区（如消息流），仍拦截浏览器默认行为
  // （沙箱 iframe 里拖入文件默认会整页导航），并统一进入 addFiles 管线
  document.addEventListener("dragenter", (event) => {
    if (dragHasFiles(event)) event.preventDefault();
  });
  document.addEventListener("dragover", (event) => {
    if (!dragHasFiles(event)) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    showDragState();
  });
  document.addEventListener("dragleave", (event) => {
    // relatedTarget 为空 = 拖拽离开窗口；否则只是元素间移动，由 dragover 维持状态
    if (event.relatedTarget) return;
    hideDragState();
  });
  document.addEventListener("drop", (event) => {
    event.preventDefault();
    hideDragState(true);
    const files = Array.from(event.dataTransfer?.files ?? []);
    if (files.length > 0) addFiles(files);
  });

  /* ── 输入行 ─────────────────────────────────────────── */

  const textarea = el("textarea", {
    id: "composer-input",
    rows: "1",
    placeholder: t("composer.placeholder", "输入消息，Enter 发送 / Shift+Enter 换行"),
    "aria-label": t("composer.placeholder", "输入消息，Enter 发送 / Shift+Enter 换行"),
  });

  const attachBtn = el("button", {
    class: "attach-btn",
    type: "button",
    "aria-label": t("composer.attach", "添加附件"),
    title: t("composer.attach", "添加附件"),
    onclick: () => {
      if (isLegacyConvo()) return; // legacy 归档只读：上传入口禁用（防御性短路）
      // image_capable=false → accept 收窄为文本白名单（启发式预禁图片入口）
      const accepts = store.state.imageCapable
        ? [...IMAGE_EXTS, ...TEXT_EXTS].map((ext) => `.${ext}`).join(",")
        : [...TEXT_EXTS].map((ext) => `.${ext}`).join(",");
      fileInput.setAttribute("accept", accepts);
      fileInput.click();
      if (!store.state.imageCapable) {
        setNote(t("composer.imageDisabled", "当前人格的模型可能不支持图片输入，图片附件已禁用"), "warning");
      }
    },
  }, icon("link", { size: 17 }));

  const sendBtn = el("button", {
    class: "send-btn",
    type: "button",
    "aria-label": t("composer.send", "发送"),
    title: t("composer.send", "发送"),
    onclick: () => onSend(),
  }, icon("send", { size: 16 }));

  const stopBtn = el("button", {
    class: "stop-btn",
    type: "button",
    "aria-label": t("composer.stop", "停止"),
    title: t("composer.stop", "停止"),
    onclick: () => onStop(),
  }, icon("stop", { size: 15 }));

  const row = el("div", { class: "composer-row" }, attachBtn, textarea, sendBtn, stopBtn);

  const noteNode = el("span", { class: "cf-note" });
  const foot = el("div", { class: "composer-foot" }, noteNode);

  function setNote(text, kind = "") {
    noteNode.textContent = text;
    noteNode.className = `cf-note${kind ? ` is-${kind}` : ""}`;
    foot.classList.toggle("has-note", Boolean(text));
  }

  /* ── legacy 归档会话（v1.1 可选字段 legacy:true）：只读 ── */

  /** @returns {boolean} 当前会话是否为 legacy 只读归档 */
  function isLegacyConvo() {
    return Boolean(store.conversationOf(store.state.currentCid)?.legacy);
  }

  /** @returns {string} legacy 说明条文案 */
  function legacyNoticeText() {
    return t("composer.legacyNotice", "旧版会话为只读归档，无法继续对话");
  }

  /* ── 发送 / 停止 ────────────────────────────────────── */

  let sending = false;
  /** legacy 说明条是否正由本组件展示（切走会话时据此判断是否需要清空脚注） */
  let legacyNoteShown = false;

  function attachmentsReady() {
    for (const item of pending.values()) {
      if (!item.meta) return false; // 仍有上传中的附件
    }
    return true;
  }

  function canSend() {
    if (isLegacyConvo()) return false; // legacy 归档只读：禁发
    const cid = store.state.currentCid;
    if (sending || !attachmentsReady()) return false;
    if (cid && store.isBusy(cid)) return false; // busy 禁发
    const hasText = textarea.value.trim().length > 0;
    const hasFiles = [...pending.values()].some((item) => item.meta);
    return hasText || hasFiles;
  }

  function updateButtons() {
    sendBtn.disabled = !canSend();
    // legacy 归档只读：输入区与上传入口一并禁用（发送按钮经 canSend 已禁）
    const legacy = isLegacyConvo();
    textarea.disabled = legacy;
    attachBtn.disabled = legacy;
    // 说明条：选中 legacy 会话时显示；切走时若脚注仍是该说明则清空（不覆盖其它提示）
    if (legacy) {
      setNote(legacyNoticeText(), "warning");
      legacyNoteShown = true;
    } else if (legacyNoteShown) {
      legacyNoteShown = false;
      if (noteNode.textContent === legacyNoticeText()) setNote("");
    }
    // 流式态：有订阅中的流（正常/降级重试）或存在活跃 gen_id（降级轮询模式，
    // 无流式状态但有可停止的生成，契约 stop/<gen_id> 仍可用）
    const currentCid = store.state.currentCid;
    row.classList.toggle(
      "is-streaming",
      Boolean(store.streamForCid(currentCid)) || Boolean(store.activeGenOf(currentCid)),
    );
  }

  async function onSend() {
    if (!canSend()) return;
    const text = textarea.value.trim();
    const attachments = [...pending.values()]
      .filter((item) => item.meta)
      .map((item) => /** @type {any} */ (item.meta));
    if (!text && attachments.length === 0) return;
    sending = true;
    updateButtons();
    setNote(t("composer.sending", "发送中…"));
    try {
      await store.sendMessage({ text, attachments });
      textarea.value = "";
      autoGrow();
      for (const key of [...pending.keys()]) removePending(key);
      setNote("");
    } catch (error) {
      // 409 双分支按 slug 先行判定（契约：session_busy 与 concurrency_limit 同为 409）；
      // 仅当两个 slug 都未命中时才把裸 409 兜底为 session_busy
      if (error?.slug === "session_busy" || (error?.status === 409 && error?.slug !== "concurrency_limit")) {
        toast(t("error.sessionBusy", "该会话正在生成回复，请等待完成或先停止"), "warning");
      } else if (error?.slug === "concurrency_limit") {
        toast(t("error.concurrencyLimit", "全局并发已达上限，请稍后再试"), "warning");
      } else {
        toast(error?.message || t("error.sendFailed", "发送失败"), "error");
      }
      setNote("");
    } finally {
      sending = false;
      updateButtons();
      textarea.focus();
    }
  }

  async function onStop() {
    const cid = store.state.currentCid;
    if (!cid) return;
    try {
      await store.stopGeneration(cid);
      // 终态事件（stopped）落 UI 后按钮自动切回
    } catch (error) {
      toast(error?.message || t("error.stopFailed", "停止失败"), "error");
    }
  }

  textarea.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      onSend();
    }
  });

  /* ── 粘贴上传（W5：Ctrl+V 直接粘贴附件） ──────────────── */

  /**
   * 粘贴含文件时的处理：进附件管线；随带的纯文本仍插入输入框（图文混合粘贴）。
   * @param {ClipboardEvent} event
   * @returns {boolean} 是否接管了该事件（含文件）
   */
  function handlePaste(event) {
    const files = filesFromClipboard(event.clipboardData);
    if (files.length === 0) return false;
    event.preventDefault();
    if (isLegacyConvo()) {
      toast(legacyNoticeText(), "warning");
      return true;
    }
    const text = event.clipboardData?.getData("text/plain") ?? "";
    if (text) insertTextAtCursor(text);
    addFiles(files);
    return true;
  }

  // 输入框内粘贴（主入口）
  textarea.addEventListener("paste", (event) => {
    handlePaste(event);
  });
  // 焦点不在输入框时（消息流等区域）Ctrl+V 也能粘贴附件；输入框已接管过则跳过
  document.addEventListener("paste", (event) => {
    if (event.defaultPrevented) return;
    handlePaste(event);
  });

  function autoGrow() {
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(160, Math.max(42, textarea.scrollHeight))}px`;
  }
  textarea.addEventListener("input", autoGrow);

  /** 在光标处插入文本（图文混合粘贴时保留文字部分）。@param {string} text */
  function insertTextAtCursor(text) {
    const start = textarea.selectionStart ?? textarea.value.length;
    const end = textarea.selectionEnd ?? textarea.value.length;
    textarea.value = textarea.value.slice(0, start) + text + textarea.value.slice(end);
    const pos = start + text.length;
    textarea.setSelectionRange(pos, pos);
    autoGrow();
  }

  /* ── 装配 ───────────────────────────────────────────── */

  inner.append(previewBar, row, foot, fileInput);
  root.appendChild(inner);
  autoGrow();
  updateButtons();

  /** @param {Set<string>} topics */
  function onStoreChange(topics) {
    if (topics.has("stream") || topics.has("conversations") || topics.has("ui") || topics.has("messages")) {
      updateButtons();
    }
  }

  function renderTexts() {
    const placeholder = t("composer.placeholder", "输入消息，Enter 发送 / Shift+Enter 换行");
    textarea.placeholder = placeholder;
    textarea.setAttribute("aria-label", placeholder);
    attachBtn.setAttribute("aria-label", t("composer.attach", "添加附件"));
    attachBtn.setAttribute("title", t("composer.attach", "添加附件"));
    sendBtn.setAttribute("aria-label", t("composer.send", "发送"));
    sendBtn.setAttribute("title", t("composer.send", "发送"));
    stopBtn.setAttribute("aria-label", t("composer.stop", "停止"));
    stopBtn.setAttribute("title", t("composer.stop", "停止"));
    // 语言切换后刷新 legacy 说明条文案
    if (legacyNoteShown) {
      if (isLegacyConvo()) setNote(legacyNoticeText(), "warning");
      else legacyNoteShown = false;
    }
  }

  return { onStoreChange, renderTexts, focus: () => textarea.focus() };
}
