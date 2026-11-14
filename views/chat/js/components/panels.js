/**
 * panels.js — 共享 UI 组件：工具调用折叠面板 / 页内自绘确认框 / 灯箱 / Toast。
 * 沙箱 iframe 无原生 alert/confirm —— 一切确认经 confirmDialog（M2 ⑤ / M4 ②）。
 */

import { el, icon } from "../icons.js";

/* ══ 工具调用面板（M3 ③ / §6.3） ═══════════════════════ */

/**
 * 创建一个工具面板实例；调用 update(tool) 原位刷新（增量渲染，避免整列表重建）。
 * 自动展开规则：执行中展开（实时看参数），落定后收起（用户手动展开过则尊重手动）。
 * @param {(key: string, fallback?: string) => string} t
 */
export function createToolPanel(t) {
  const root = el("div", { class: "tool-panel" });
  const head = el("button", { class: "tp-head", type: "button" });
  const body = el("div", { class: "tp-body" });
  root.append(head, body);

  let phase = "running";
  let manualToggled = false;
  let isOpen = true;

  function applyOpen() {
    root.classList.toggle("is-open", isOpen);
  }

  head.addEventListener("click", () => {
    manualToggled = true;
    isOpen = !isOpen;
    applyOpen();
  });

  /**
   * @param {{id: string, name: string, args?: any, result?: string, error?: string, elapsed_ms?: number, phase?: string}} tool
   */
  function update(tool) {
    const running = tool.phase === "running";
    const failed = Boolean(tool.error);

    // 头部：▶ TOOL_CALL · name · 状态
    head.replaceChildren(
      icon("tool", { size: 13, className: "tp-tool-icon" }),
      el("span", { class: "tp-tag", text: t("tools.tag", "TOOL_CALL") }),
      el("span", { class: "tp-name", text: tool.name || "—" }),
      el("span", {
        class: `tp-status${failed ? " is-error" : running ? "" : " is-done"}`,
      }, running
        ? [icon("refresh", { size: 11, className: "spin-icon" }), t("tools.running", "RUNNING")]
        : failed
          ? t("tools.failed", "FAILED")
          : `${t("tools.done", "DONE")}${tool.elapsed_ms ? ` · ${tool.elapsed_ms}ms` : ""}`),
      icon("chevronDown", { size: 13, className: "tp-chevron" }),
    );

    // 内容：参数 JSON 折叠 + 结果/错误
    const argsNode = el("pre", { class: "tp-args", text: safeJson(tool.args ?? {}) });
    const children = [
      el("span", { class: "tp-section-label", text: t("tools.args", "ARGS") }),
      argsNode,
    ];
    if (running) {
      children.push(el("div", { class: "stream-wait" }, el("span", { class: "spinner" }), t("tools.running", "RUNNING")));
    } else if (failed) {
      children.push(
        el("span", { class: "tp-section-label", text: t("tools.result", "RESULT") }),
        el("div", { class: "tp-result is-error", text: String(tool.error) }),
      );
    } else if (tool.result !== undefined) {
      children.push(
        el("span", { class: "tp-section-label", text: t("tools.result", "RESULT") }),
        el("div", { class: "tp-result", text: String(tool.result) }),
      );
    }
    body.replaceChildren(...children);

    if (!manualToggled) {
      isOpen = running; // 执行中自动展开、落定自动收起
    }
    phase = running ? "running" : "done";
    applyOpen();
  }

  return { root, update };
}

/** @param {any} value */
function safeJson(value) {
  try {
    return JSON.stringify(value ?? {}, null, 2);
  } catch {
    return String(value);
  }
}

/* ══ 页内自绘确认框（沙箱无 confirm） ═══════════════════ */

/**
 * @param {{kicker?: string, title: string, body: string, confirmText: string, cancelText: string, danger?: boolean}} options
 * @returns {Promise<boolean>} true = 用户确认
 */
export function confirmDialog(options) {
  return new Promise((resolve) => {
    const cleanup = (confirmed) => {
      document.removeEventListener("keydown", onKeydown);
      overlay.remove();
      resolve(confirmed);
    };
    const onKeydown = (event) => {
      if (event.key === "Escape") cleanup(false);
    };

    const cancelButton = el("button", {
      class: "btn-secondary",
      type: "button",
      text: options.cancelText,
      onclick: () => cleanup(false),
    });
    const confirmButton = el("button", {
      class: `btn-primary${options.danger ? " is-danger" : ""}`,
      type: "button",
      text: options.confirmText,
      onclick: () => cleanup(true),
    });
    const card = el("div", { class: "dialog-card", role: "document" },
      options.kicker ? el("div", { class: "dc-kicker", text: options.kicker }) : null,
      el("div", { class: "dc-title", text: options.title }),
      el("div", { class: "dc-body", text: options.body, style: { whiteSpace: "pre-line" } }),
      el("div", { class: "dc-actions" }, cancelButton, confirmButton),
    );
    const overlay = el("div", {
      class: "overlay-root",
      role: "dialog",
      "aria-modal": "true",
      onclick: (event) => {
        if (event.target === overlay) cleanup(false);
      },
    }, card);

    document.addEventListener("keydown", onKeydown);
    document.body.appendChild(overlay);
    confirmButton.focus();
  });
}

/* ══ 灯箱（页内，禁止 window.open；M3 ④） ══════════════ */

/**
 * @param {{
 *   meta: {file_id: string, name?: string, preview?: string, kind?: string},
 *   api: {downloadFile: Function, mediaUrl: Function},
 *   t: (key: string, fallback?: string) => string,
 *   canFetchOriginal: boolean,
 * }} options
 */
export function openLightbox({ meta, api, t, canFetchOriginal }) {
  const originalUrl = canFetchOriginal && typeof api.mediaUrl === "function"
    ? api.mediaUrl(meta.file_id)
    : "";
  const previewUrl = meta.preview || "";

  const img = el("img", { src: previewUrl, alt: meta.name || "image" });
  if (originalUrl) {
    // 先显示缩略预览，原图加载成功后换入
    const full = new Image();
    full.onload = () => {
      img.src = originalUrl;
    };
    full.src = originalUrl;
  }

  const downloadButton = el("button", { class: "btn-secondary", type: "button" },
    icon("download", { size: 13 }),
    ` ${t("lightbox.download", "下载原图")}`);
  downloadButton.addEventListener("click", () => {
    try {
      api.downloadFile(meta.file_id, meta.name || undefined);
    } catch {
      /* 下载失败静默（预览仍可看） */
    }
  });

  const overlay = el("div", {
    class: "lightbox",
    role: "dialog",
    "aria-modal": "true",
    onclick: (event) => {
      if (event.target === overlay) close();
    },
  },
    el("button", {
      class: "lb-close",
      type: "button",
      "aria-label": t("dialog.close", "关闭"),
      onclick: () => close(),
    }, icon("close", { size: 16 })),
    el("div", { class: "lb-img-wrap" }, img),
    el("div", { class: "lb-note", text: originalUrl ? "" : t("lightbox.previewOnly", "内嵌模式受沙箱限制，仅显示缩略预览") }),
    el("div", { class: "lb-actions" }, downloadButton),
  );

  const onKeydown = (event) => {
    if (event.key === "Escape") close();
  };
  function close() {
    document.removeEventListener("keydown", onKeydown);
    overlay.remove();
  }
  document.addEventListener("keydown", onKeydown);
  document.body.appendChild(overlay);
}

/* ══ Toast（413/415/409 等即时提示；M3 ② / M4 ②） ══════ */

let toastRoot = null;

/**
 * @param {string} message
 * @param {"info"|"warning"|"error"|"success"} [kind]
 */
export function toast(message, kind = "info") {
  if (!toastRoot || !toastRoot.isConnected) {
    toastRoot = el("div", { id: "toast-root", "aria-live": "polite" });
    document.body.appendChild(toastRoot);
  }
  const item = el("div", { class: `toast is-${kind}`, role: "status", text: message });
  toastRoot.appendChild(item);
  window.setTimeout(() => {
    item.classList.add("is-leaving");
    window.setTimeout(() => item.remove(), 220);
  }, 4200);
  // 同时最多 4 条
  while (toastRoot.children.length > 4) {
    /** @type {HTMLElement} */ (toastRoot.firstChild)?.remove();
  }
}
