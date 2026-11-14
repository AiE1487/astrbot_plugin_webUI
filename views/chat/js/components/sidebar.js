/**
 * sidebar.js — 侧栏内容：人格下拉卡片 + 会话列表（NO.01 编号装饰）+ 新建/删除。
 * 删除确认使用页内自绘 confirmDialog（沙箱无原生 confirm）。
 */

import { el, icon } from "../icons.js";
import { confirmDialog, toast } from "./panels.js";

/** @param {number} value */
function pad2(value) {
  return String(value + 1).padStart(2, "0");
}

/** 手机端竖屏（抽屉模式，W3）：选中会话/人格后自动收起抽屉。 */
function collapseDrawerOnMobile(store) {
  if (window.matchMedia("(max-width: 900px)").matches && !store.state.sidebarCollapsed) {
    store.setSidebarCollapsed(true);
  }
}

/** @param {string} iso */
function shortDate(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return `${String(date.getMonth() + 1).padStart(2, "0")}/${String(date.getDate()).padStart(2, "0")}`;
}

/**
 * @param {{root: HTMLElement, store: any, t: Function, version: string}} deps
 */
export function createSidebar({ root, store, t, version }) {
  const inner = el("div", { class: "sidebar-inner" });

  /* ── 人格下拉卡片 ────────────────────────────────────── */
  const personaMenu = el("div", { class: "persona-menu", role: "listbox", "aria-label": "persona" });
  const personaButtonName = el("span", { class: "psb-name", text: "—" });
  const personaButton = el("button", {
    class: "persona-select-btn",
    type: "button",
    "aria-expanded": "false",
    onclick: () => toggleMenu(),
  },
    icon("user", { size: 15 }),
    personaButtonName,
    icon("chevronDown", { size: 14, className: "psb-chevron" }),
  );
  const personaHint = el("div", { class: "pb-hint", text: t("persona.hint", "选择人格将新建一个绑定该人格的会话") });
  const personaBlock = el("div", { class: "persona-block" },
    el("div", { class: "pb-label", text: t("persona.label", "PERSONA") }),
    personaButton,
    personaMenu,
    personaHint,
  );

  function closeMenu() {
    personaMenu.classList.remove("is-open");
    personaButton.setAttribute("aria-expanded", "false");
  }

  function toggleMenu() {
    const open = !personaMenu.classList.contains("is-open");
    personaMenu.classList.toggle("is-open", open);
    personaButton.setAttribute("aria-expanded", String(open));
  }

  document.addEventListener("click", (event) => {
    if (!personaBlock.contains(/** @type {Node} */ (event.target))) closeMenu();
  });

  /** @param {any} persona */
  async function pickPersona(persona) {
    closeMenu();
    const current = store.conversationOf(store.state.currentCid);
    if (current?.persona_id === persona.id) {
      collapseDrawerOnMobile(store);
      return; // 当前会话已是该人格
    }
    try {
      await store.createConversation(persona.id);
      toast(t("toast.personaSwitched", "已新建人格会话"), "success");
    } catch (error) {
      toast(error?.message || t("error.loadFailed", "加载失败"), "error");
    }
    collapseDrawerOnMobile(store);
  }

  /** @param {any[]} personas */
  function renderPersonaMenu(personas) {
    const currentPersonaId = store.conversationOf(store.state.currentCid)?.persona_id;
    personaButtonName.textContent = store.personaOf(currentPersonaId)?.name
      ?? personas.find((p) => p.is_default)?.name
      ?? "—";
    personaMenu.replaceChildren(...(personas.length === 0
      ? [el("div", { class: "convo-empty", text: t("persona.empty", "宿主暂未配置人格") })]
      : personas.map((persona) => el("button", {
          class: `persona-item${persona.id === currentPersonaId ? " is-active" : ""}`,
          type: "button",
          onclick: () => pickPersona(persona),
        },
          el("div", { class: "pi-row" },
            el("span", { class: "pi-name", text: persona.name ?? persona.id }),
            persona.has_tools ? el("span", { class: "badge is-tools", text: t("persona.badge.tools", "TOOLS") }) : null,
            persona.is_default ? el("span", { class: "badge is-default", text: t("persona.badge.default", "DEFAULT") }) : null,
          ),
          persona.system_prompt_preview
            ? el("div", { class: "pi-preview", text: persona.system_prompt_preview })
            : null,
        ))));
  }

  /* ── 会话列表 ───────────────────────────────────────── */

  const convoList = el("div", { class: "convo-list", role: "list" });
  const newButton = el("button", {
    class: "ch-new",
    type: "button",
    "aria-label": t("convo.new", "新建会话"),
    title: t("convo.new", "新建会话"),
    onclick: async () => {
      const personaId = store.conversationOf(store.state.currentCid)?.persona_id
        ?? store.state.personas.find((p) => p.is_default)?.id
        ?? store.state.personas[0]?.id;
      if (!personaId) {
        toast(t("persona.empty", "宿主暂未配置人格"), "warning");
        return;
      }
      try {
        await store.createConversation(personaId);
        toast(t("toast.created", "会话已创建"), "success");
      } catch (error) {
        toast(error?.message || t("error.loadFailed", "加载失败"), "error");
        return;
      }
      collapseDrawerOnMobile(store);
    },
  }, icon("plus", { size: 14 }));

  /** @param {any} convo @param {number} index */
  async function onDelete(convo) {
    const confirmed = await confirmDialog({
      kicker: "DELETE",
      title: t("dialog.deleteTitle", "删除会话"),
      body: t("dialog.deleteBody", "将删除「{title}」的权威历史、富历史与全部附件，且不可恢复。\n确认删除？")
        .replace("{title}", convo.title || t("convo.untitled", "新会话")),
      confirmText: t("dialog.confirm", "确认删除"),
      cancelText: t("dialog.cancel", "取消"),
      danger: true,
    });
    if (!confirmed) return;
    try {
      await store.deleteConversation(convo.cid);
      toast(t("toast.deleted", "会话已删除"), "success");
    } catch (error) {
      toast(error?.message || t("error.loadFailed", "加载失败"), "error");
    }
  }

  /** @param {any[]} conversations @param {string|null} currentCid */
  function renderConversations(conversations, currentCid) {
    if (conversations.length === 0) {
      convoList.replaceChildren(el("div", { class: "convo-empty", text: t("convo.empty", "暂无会话：选择上方人格，或点右上 + 新建") }));
      return;
    }
    convoList.replaceChildren(...conversations.map((convo, index) => {
      const busy = Boolean(convo.busy) || Boolean(store.streamForCid(convo.cid));
      // v1.1 可选字段 legacy:true = 旧版归档（只读），列表项加「旧版」徽标；
      // 删除按钮保持可用（后端支持删除归档）
      const legacy = Boolean(convo.legacy);
      const deleteBtn = el("button", {
        class: "ci-del",
        type: "button",
        "aria-label": t("convo.delete", "删除会话"),
        title: t("convo.delete", "删除会话"),
        onclick: (event) => {
          event.stopPropagation();
          onDelete(convo);
        },
      }, icon("trash", { size: 13 }));
      return el("button", {
        class: `convo-item${convo.cid === currentCid ? " is-active" : ""}`,
        type: "button",
        role: "listitem",
        onclick: () => {
          store.switchConversation(convo.cid);
          collapseDrawerOnMobile(store); // 手机端：选中后收起抽屉看对话
        },
      },
        el("span", { class: "ci-no", text: `NO.${pad2(index)}` }),
        el("span", { class: "ci-main" },
          el("span", { class: "ci-title", text: convo.title || t("convo.untitled", "新会话") }),
          el("span", { class: "ci-meta" },
            legacy ? el("span", { class: "badge is-legacy", text: t("convo.legacyBadge", "旧版") }) : null,
            el("span", { class: "ci-persona", text: store.personaOf(convo.persona_id)?.name ?? convo.persona_id ?? "" }),
            el("span", { text: shortDate(convo.updated_at) }),
            busy ? el("span", { class: "ci-busy" }, el("span", { class: "busy-dot" }), t("convo.busy", "生成中")) : null,
          ),
        ),
        busy ? el("span", { class: "busy-dot" }) : null,
        deleteBtn,
      );
    }));
  }

  /* ── 装配 ───────────────────────────────────────────── */

  inner.append(
    personaBlock,
    el("div", { class: "convo-head" },
      el("span", { class: "ch-label", text: t("convo.label", "CONVERSATIONS") }),
      newButton,
    ),
    convoList,
    el("div", { class: "sidebar-foot", text: `astrbot_plugin_webui // v${version}` }),
  );
  root.appendChild(inner);

  /** @param {Set<string>} topics */
  function onStoreChange(topics) {
    if (topics.has("personas") || topics.has("conversations") || topics.has("ui") || topics.has("stream")) {
      renderPersonaMenu(store.state.personas);
      renderConversations(store.state.conversations, store.state.currentCid);
    }
  }

  function renderTexts() {
    personaHint.textContent = t("persona.hint", "选择人格将新建一个绑定该人格的会话");
    personaBlock.querySelector(".pb-label")?.replaceChildren(document.createTextNode(t("persona.label", "PERSONA")));
    newButton.setAttribute("aria-label", t("convo.new", "新建会话"));
    newButton.setAttribute("title", t("convo.new", "新建会话"));
    root.querySelector(".ch-label")?.replaceChildren(document.createTextNode(t("convo.label", "CONVERSATIONS")));
    root.querySelector(".sidebar-foot")?.replaceChildren(
      document.createTextNode(`astrbot_plugin_webui // v${version}`),
    );
    renderPersonaMenu(store.state.personas);
    renderConversations(store.state.conversations, store.state.currentCid);
  }

  return { onStoreChange, renderTexts };
}
