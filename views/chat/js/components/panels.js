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

/* ══ 设置面板（W7：UI 布局与组件自定义；数据经 store.uiSettings 持久化） ══ */

/** 头像文件 → 160px PNG data URL（压缩体积以适配 safeStorage）。 */
async function avatarToDataUrl(file) {
  try {
    const bitmap = await createImageBitmap(file);
    const scale = Math.min(1, 160 / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    const ctx = /** @type {CanvasRenderingContext2D} */ (canvas.getContext("2d"));
    ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    bitmap.close?.();
    return canvas.toDataURL("image/png");
  } catch {
    return "";
  }
}

/**
 * @param {{store: any, t: (key: string, fallback?: string) => string}} deps
 */
export function openSettingsPanel({ store, t }) {
  const fileInput = el("input", { type: "file", accept: "image/*", style: { display: "none" } });
  const unsubscribers = [];
  let pickingKey = "";
  fileInput.addEventListener("change", async () => {
    const file = fileInput.files?.[0] ?? null;
    fileInput.value = "";
    if (!file || !pickingKey) return;
    const dataUrl = await avatarToDataUrl(file);
    if (dataUrl) store.updateUiSettings({ [pickingKey]: dataUrl });
    else toast(t("settings.avatar.decodeFailed", "图片解析失败，请换一张图片"), "error");
  });

  /** 开关行（role=switch，写回 uiSettings）。 */
  function switchRow(labelKey, fallback, settingKey) {
    const sw = el("button", {
      class: "ui-switch",
      type: "button",
      role: "switch",
      "aria-checked": String(Boolean(store.state.uiSettings[settingKey])),
      onclick: () => {
        const next = sw.getAttribute("aria-checked") !== "true";
        sw.setAttribute("aria-checked", String(next));
        store.updateUiSettings({ [settingKey]: next });
      },
    }, el("span", { class: "ui-switch-thumb" }));
    return el("div", { class: "sp-row" },
      el("span", { class: "sp-label", text: t(labelKey, fallback) }),
      sw,
    );
  }

  /** 头像选择行：圆形预览 + 上传/清除。 */
  function avatarRow(labelKey, fallback, settingKey) {
    const preview = el("span", { class: "sp-avatar-preview" });
    const syncPreview = () => {
      const url = store.state.uiSettings[settingKey];
      preview.replaceChildren();
      preview.classList.toggle("has-img", Boolean(url));
      if (url) preview.appendChild(el("img", { src: String(url), alt: "" }));
      else preview.appendChild(icon("camera", { size: 18 }));
    };
    syncPreview();
    // ui topic（设置变更）时刷新预览；面板关闭时退订
    unsubscribers.push(store.on((topics) => {
      if (topics.has("ui")) syncPreview();
    }));
    const uploadBtn = el("button", {
      class: "btn-secondary sp-btn",
      type: "button",
      text: t("settings.avatar.upload", "上传"),
      onclick: () => {
        pickingKey = settingKey;
        fileInput.click();
      },
    });
    const clearBtn = el("button", {
      class: "btn-secondary sp-btn",
      type: "button",
      text: t("settings.avatar.clear", "清除"),
      onclick: () => store.updateUiSettings({ [settingKey]: "" }),
    });
    return el("div", { class: "sp-row" },
      el("span", { class: "sp-label", text: t(labelKey, fallback) }),
      el("span", { class: "sp-avatar-controls" }, preview, uploadBtn, clearBtn),
    );
  }

  /** 消息流宽度分段选择。 */
  function flowWidthRow() {
    const options = [
      { value: "normal", label: t("settings.flowWidth.normal", "标准") },
      { value: "wide", label: t("settings.flowWidth.wide", "宽") },
      { value: "full", label: t("settings.flowWidth.full", "全宽") },
    ];
    const seg = el("div", { class: "sp-seg" });
    const render = () => {
      seg.replaceChildren(...options.map((opt) => el("button", {
        class: `sp-seg-btn${store.state.uiSettings.flowWidth === opt.value ? " is-active" : ""}`,
        type: "button",
        text: opt.label,
        onclick: () => {
          store.updateUiSettings({ flowWidth: opt.value });
          render();
        },
      })));
    };
    render();
    return el("div", { class: "sp-row" },
      el("span", { class: "sp-label", text: t("settings.flowWidth", "消息流宽度") }),
      seg,
    );
  }

  function section(label, ...rows) {
    return el("div", { class: "sc-section" },
      el("div", { class: "sc-section-label", text: label }),
      ...rows,
    );
  }

  const closeButton = el("button", {
    class: "sc-close",
    type: "button",
    "aria-label": t("dialog.close", "关闭"),
    onclick: () => close(),
  }, icon("close", { size: 14 }));

  const card = el("div", { class: "settings-card", role: "document" },
    el("div", { class: "sc-head" },
      el("div", { class: "sc-title", text: t("settings.title", "界面设置") }),
      closeButton,
    ),
    el("div", { class: "sc-body" },
      section(
        t("settings.section.avatars", "头像"),
        switchRow("settings.showAvatars", "显示消息头像", "showAvatars"),
        avatarRow("settings.avatarUser", "用户头像", "avatarUser"),
        avatarRow("settings.avatarAssistant", "AI 头像", "avatarAssistant"),
        el("div", { class: "sc-hint", text: t("settings.avatarHint", "头像默认隐藏；未上传时 AI 显示人格名首字徽标，用户显示图标。") }),
      ),
      section(
        t("settings.section.layout", "布局"),
        flowWidthRow(),
        switchRow("settings.compact", "紧凑模式", "compact"),
      ),
      section(
        t("settings.section.components", "消息组件"),
        switchRow("settings.showTimestamps", "显示时间戳", "showTimestamps"),
        switchRow("settings.showUsage", "显示 TOKENS 用量", "showUsage"),
        switchRow("settings.showReasoningDefault", "思考内容默认展开", "showReasoningDefault"),
      ),
    ),
    fileInput,
  );

  const overlay = el("div", {
    class: "overlay-root",
    role: "dialog",
    "aria-modal": "true",
    "aria-label": t("settings.title", "界面设置"),
    onclick: (event) => {
      if (event.target === overlay) close();
    },
  }, card);

  const onKeydown = (event) => {
    if (event.key === "Escape") close();
  };
  function close() {
    document.removeEventListener("keydown", onKeydown);
    for (const off of unsubscribers) off();
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
