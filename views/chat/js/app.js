/**
 * app.js — 装配与启动（M1 ③ / M4 ①②③⑤）。
 *
 * 启动顺序：
 *   加载屏（#1e1e1e 底、通长进度条、左下版本、右下 astrbot_plugin_webui // vX.Y.Z）
 *   → initTransport（bridge 探测 / postMessage 兜底 / standalone / mock）
 *   → standalone 401 时登录卡（页内自绘，POST /api/login）
 *   → personas/conversations/history 装载（进度条推进）→ 加载屏淡出（数据就绪后）
 *   → 心跳（30s + visibilitychange）驱动断线重连横幅。
 *
 * 顶栏：Logo+插件名（左）、当前人格名+青色下划线（中）、连接状态/语言/侧栏折叠（右）。
 * 右轨：大号青色会话编号 01 / ∞ + 半调网点装饰（§6.2）。
 */

import { PLUGIN_NAME, PLUGIN_VERSION, initTransport } from "./bridge-client.js";
import { createApi } from "./api.js";
import { createStore } from "./store.js";
import { createSidebar } from "./components/sidebar.js";
import { createMessageFlow } from "./components/message.js";
import { createComposer } from "./components/composer.js";
import { toast } from "./components/panels.js";
import { el, icon } from "./icons.js";

/** @param {string} id */
function byId(id) {
  return /** @type {HTMLElement} */ (document.getElementById(id));
}

async function boot() {
  const loadingScreen = byId("loading-screen");
  const barFill = /** @type {HTMLElement} */ (loadingScreen?.querySelector(".ls-bar-fill"));
  const statusNode = /** @type {HTMLElement} */ (loadingScreen?.querySelector(".ls-status"));
  const leftCorner = /** @type {HTMLElement} */ (loadingScreen?.querySelector(".ls-left"));
  const rightCorner = /** @type {HTMLElement} */ (loadingScreen?.querySelector(".ls-right"));
  const appRoot = byId("app");
  const bannerSlot = byId("banner-slot");
  const topbar = byId("topbar");
  const sidebarRoot = byId("sidebar");
  const chatColumn = byId("chat-column");
  const chatScroll = byId("chat-scroll");
  const rightRail = byId("right-rail");
  const composerRoot = byId("composer");

  /* ── 加载屏 ─────────────────────────────────────────── */

  let t = (key, fallback = "") => fallback || key;

  function setProgress(pct, labelKey) {
    if (barFill) barFill.style.width = `${pct}%`;
    if (statusNode) statusNode.textContent = `➤ ${t(labelKey, "LOADING")} - ${pct}% ......`;
  }
  if (rightCorner) rightCorner.textContent = `${PLUGIN_NAME} // v${PLUGIN_VERSION}`;
  if (leftCorner) leftCorner.textContent = `VERSION v${PLUGIN_VERSION}`;
  setProgress(4, "loading.initial");

  /* ── 传输层 ─────────────────────────────────────────── */

  const forceMock = new URLSearchParams(window.location.search).get("mock") === "1";
  const transport = await initTransport({ forceMock });
  t = transport.t;
  setProgress(22, "loading.initial");

  const api = createApi(transport);
  const store = createStore({ api, transport, t });

  /* ── 顶栏 ───────────────────────────────────────────── */

  const brandName = el("span", { class: "brand-name" });
  const brandSub = el("span", { class: "brand-sub", text: "ASTRBOT // PLUGIN VIEW" });
  const brand = el("div", { class: "brand" },
    (() => {
      const img = el("img", { src: "./assets/logo-mark.svg", alt: "", style: { width: "26px", height: "26px" } });
      return img;
    })(),
    el("div", { style: { display: "flex", flexDirection: "column", lineHeight: "1.15" } },
      brandName,
      brandSub,
    ),
  );

  const piLabel = el("span", { class: "pi-label", text: "PERSONA" });
  const piName = el("span", { class: "pi-name", text: "—" });
  const personaIndicator = el("div", { id: "persona-indicator" }, piLabel, piName);

  const connDot = el("span", { class: "conn-dot" });
  const connText = el("span", { text: "" });
  const connChip = el("div", { class: "conn-chip" }, connDot, connText);

  const localeBtn = el("button", {
    class: "locale-btn",
    type: "button",
    title: "language",
    onclick: () => {
      const next = store.state.locale.startsWith("zh") ? "en-US" : "zh-CN";
      if (typeof transport.setLocale === "function") transport.setLocale(next);
      store.setLocale(next);
      renderTexts();
    },
  });
  const sidebarToggle = el("button", {
    class: "icon-btn",
    type: "button",
    "aria-label": "sidebar",
    title: "sidebar",
    onclick: () => store.setSidebarCollapsed(!store.state.sidebarCollapsed),
  }, icon("menu", { size: 15 }));

  topbar.append(
    brand,
    personaIndicator,
    el("div", { class: "topbar-spacer" }),
    el("div", { class: "topbar-actions" }, connChip, localeBtn, sidebarToggle),
  );

  /* ── 右轨 ───────────────────────────────────────────── */

  const railNum = el("div", { class: "rail-num", text: "00" });
  const railTotal = el("div", { class: "rail-total", text: "/ ∞" });
  const railLabel = el("div", { class: "rail-label", text: "SESSION" });
  rightRail.append(
    el("div", { class: "rail-deco halftone halftone-fade" }),
    el("div", { class: "rail-content" }, railNum, railTotal, railLabel),
  );

  /* ── 组件 ───────────────────────────────────────────── */

  const sidebar = createSidebar({ root: sidebarRoot, store, t, version: transport.version });
  const jumpChip = byId("jump-bottom");
  jumpChip?.addEventListener("click", () => messageFlow.scrollToBottom());
  const messageFlow = createMessageFlow({
    scrollEl: chatScroll,
    store,
    api,
    transport,
    t,
    onPinnedChange: (pinned) => {
      jumpChip?.classList.toggle("is-visible", !pinned);
    },
    onPersonaPick: async (personaId) => {
      try {
        await store.createConversation(personaId);
      } catch (error) {
        toast(error?.message || t("error.loadFailed", "加载失败"), "error");
      }
    },
  });
  const composer = createComposer({ root: composerRoot, store, api, t });

  store.on((topics) => {
    sidebar.onStoreChange(topics);
    messageFlow.onStoreChange(topics);
    composer.onStoreChange(topics);
    renderChrome(topics);
  });

  /* ── 横幅 / 顶栏 / 右轨（M4 ②③） ───────────────────── */

  /** @type {HTMLElement|null} */
  let mockBanner = null;
  /** @type {HTMLElement|null} */
  let streamBanner = null;
  /** @type {HTMLElement|null} */
  let goneBanner = null;
  // 用户关闭标记（评审发现 7）：关闭后短路重建条件，横幅不再随 store flush 复活
  let mockBannerDismissed = false;
  let streamBannerDismissed = false;

  function buildBanner(kind, text, dismissible, onDismiss) {
    const banner = el("div", { class: `banner ${kind}`, role: "status" });
    banner.appendChild(el("span", { class: "banner-text", text }));
    if (dismissible) {
      banner.appendChild(el("button", {
        class: "banner-close",
        type: "button",
        "aria-label": t("dialog.close", "关闭"),
        onclick: () => {
          banner.remove();
          if (banner === mockBanner) mockBanner = null;
          if (banner === streamBanner) streamBanner = null;
          if (banner === goneBanner) {
            goneBanner = null;
            store.dismissBannerGone();
          }
          onDismiss?.();
        },
      }, icon("close", { size: 12 })));
    }
    return banner;
  }

  function renderChrome() {
    // 连接状态
    const conn = store.state.conn;
    connChip.className = `conn-chip${conn === "reconnecting" ? " is-reconnecting" : conn === "offline" ? " is-offline" : ""}`;
    connText.textContent = conn === "reconnecting"
      ? t("conn.reconnecting", "重连中")
      : conn === "offline"
        ? t("conn.offline", "离线")
        : t("conn.ok", "已连接");

    // 当前人格名 + 青色下划线
    const convo = store.conversationOf(store.state.currentCid);
    const personaName = store.personaOf(convo?.persona_id)?.name
      ?? store.state.personas.find((p) => p.is_default)?.name
      ?? "—";
    piName.textContent = personaName;

    // 右轨编号：当前会话在列表中的序号（01 起）
    const index = store.state.conversations.findIndex((item) => item.cid === store.state.currentCid);
    railNum.textContent = index >= 0 ? String(index + 1).padStart(2, "0") : "00";

    // 横幅组（重建轻量，频率低）
    bannerSlot.replaceChildren();
    if (conn === "reconnecting" || conn === "offline") {
      bannerSlot.appendChild(buildBanner(
        conn === "offline" ? "is-error" : "is-warning",
        t("banner.reconnecting", "与后端的连接中断，正在自动重连…"),
        false,
      ));
    }
    if (store.state.streamAvailable) {
      // 流式能力恢复后重置关闭标记（再次失效时横幅重新出现）
      streamBannerDismissed = false;
    }
    if (!store.state.streamAvailable && !streamBanner && !streamBannerDismissed) {
      streamBanner = buildBanner(
        "is-warning",
        t("banner.streamUnavailable", "流式不可用：回复将在生成完成后经历史刷新显示"),
        true,
        () => {
          streamBannerDismissed = true;
        },
      );
    }
    if (streamBanner) bannerSlot.appendChild(streamBanner);
    if (store.state.bannerGone) {
      goneBanner = buildBanner("is-warning", t("banner.gone", "生成流已失效（会话已结束或缓冲过期），已停止恢复"), true);
      bannerSlot.appendChild(goneBanner);
    }
    if (store.state.mode === "mock" && !mockBannerDismissed) {
      mockBanner = buildBanner(
        "is-info",
        t("banner.mock", "演示模式（mock）：当前数据为本地假数据，不影响真实宿主"),
        true,
        () => {
          mockBannerDismissed = true;
        },
      );
      bannerSlot.appendChild(mockBanner);
    }

    // 侧栏折叠
    sidebarRoot.classList.toggle("sidebar-collapsed", store.state.sidebarCollapsed);
    localeBtn.style.display = store.state.mode === "bridge" || store.state.mode === "fallback" ? "none" : "";
    localeBtn.textContent = store.state.locale.startsWith("zh") ? "EN" : "中";
  }

  function renderTexts() {
    brandName.textContent = t("title", "对话");
    piLabel.textContent = "PERSONA";
    connText.textContent = store.state.conn === "reconnecting"
      ? t("conn.reconnecting", "重连中")
      : store.state.conn === "offline"
        ? t("conn.offline", "离线")
        : t("conn.ok", "已连接");
    document.title = `${t("title", "对话")} · astrbot_plugin_webui`;
    sidebar.renderTexts();
    composer.renderTexts();
    messageFlow.renderAll();
    renderChrome();
  }

  /* ── standalone 登录卡（401 时；页内自绘） ──────────── */

  function showLogin() {
    const errorNode = el("div", { class: "lc-error" });
    const input = el("input", {
      type: "password",
      placeholder: t("login.placeholder", "访问密码"),
      "aria-label": t("login.placeholder", "访问密码"),
    });
    const submit = el("button", { class: "btn-primary", type: "submit", text: t("login.submit", "进入") });
    const form = el("form", {
      onsubmit: async (event) => {
        event.preventDefault();
        errorNode.textContent = "";
        submit.disabled = true;
        submit.textContent = t("login.checking", "验证中…");
        try {
          await transport.login(input.value);
          if (typeof transport.reprobe === "function") await transport.reprobe();
          store.state.needsLogin = false;
          overlay.remove();
          await loadData();
        } catch (error) {
          const locked = error?.status === 429 || /lock|限速|too many/i.test(String(error?.message ?? ""));
          errorNode.textContent = locked
            ? t("login.locked", "错误次数过多，请 60 秒后再试")
            : (error?.message || t("login.failed", "密码错误或服务不可用"));
          submit.disabled = false;
          submit.textContent = t("login.submit", "进入");
        }
      },
    },
      input,
      errorNode,
      submit,
    );
    const card = el("div", { class: "login-card" },
      el("div", { class: "lc-logo" },
        el("img", { src: "./assets/logo-mark.svg", alt: "" }),
        el("div", { class: "lc-title", text: t("login.title", "独立端口访问") }),
      ),
      el("div", { class: "lc-hint", text: t("login.hint", "请输入访问密码（独立入口鉴权；留空密码时仅本机可访问）") }),
      form,
      el("div", { class: "lc-foot", text: `${PLUGIN_NAME} // v${transport.version}` }),
    );
    const overlay = el("div", { class: "overlay-root overlay-deep", role: "dialog", "aria-modal": "true" }, card);
    document.body.appendChild(overlay);
    input.focus();
  }

  /* ── 数据装载与加载屏收尾 ───────────────────────────── */

  async function loadData() {
    setProgress(40, "loading.data");
    try {
      await store.loadInitial();
    } catch (error) {
      toast(error?.message || t("error.loadFailed", "加载失败"), "error");
    }
    setProgress(100, "loading.done");
    // 数据就绪 → 加载屏淡出（M4 ①：纯 CSS transition）
    window.setTimeout(() => {
      loadingScreen?.classList.add("is-done");
      appRoot.classList.add("app-ready");
      composer.focus();
    }, 120);
  }

  renderChrome();
  renderTexts();

  if (store.state.needsLogin) {
    setProgress(30, "loading.initial");
    showLogin();
  } else {
    await loadData();
  }

  /* ── 心跳（断线重连横幅驱动） ───────────────────────── */

  if (store.state.mode !== "mock") {
    window.setInterval(() => {
      if (!document.hidden) store.ping();
    }, 30000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) store.ping();
    });
  }

  /* ── 语言/上下文变化（bridge onContext；语言切换重渲染） ── */

  transport.onContext(() => {
    store.setLocale(transport.getLocale());
    renderTexts();
  });
}

boot().catch((error) => {
  console.error("[webui] boot failed:", error);
  const statusNode = document.querySelector("#loading-screen .ls-status");
  if (statusNode) {
    statusNode.textContent = `✕ BOOT FAILED · ${String(error?.message ?? error).slice(0, 120)}`;
  }
});
