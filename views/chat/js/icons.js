/**
 * icons.js — DOM 构建助手 + 内联 SVG 图标工厂。
 *
 * 安全约定：
 * - 所有动态文本一律 textContent（el() 的 text/children 走 textNode），
 *   全项目禁止 innerHTML 拼接用户/模型内容。
 * - 图标 SVG 为开发者手写的静态受信字符串，经 DOMParser 解析后 clone，
 *   任何用户/模型数据都不会进入该字符串。
 * - 与 assets/icon-*.svg 保持同一套路径（assets 为静态引用副本）。
 */

/* ── DOM 助手 ─────────────────────────────────────────── */

/**
 * @param {string} tag
 * @param {Record<string, any>} [props]
 * @param {any[]} children
 */
export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = String(value);
    else if (key === "text") node.textContent = String(value); // 动态文本一律 textContent
    else if (key === "dataset" && typeof value === "object") Object.assign(node.dataset, value);
    else if (key === "style" && typeof value === "object") Object.assign(node.style, value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  appendChildren(node, children);
  return node;
}

function appendChildren(node, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false || child === "") continue;
    if (Array.isArray(child)) {
      appendChildren(node, child);
      continue;
    }
    if (child instanceof Node) {
      node.appendChild(child);
      continue;
    }
    node.appendChild(document.createTextNode(String(child)));
  }
}

/* ── 图标工厂（静态受信字符串；stroke 用 currentColor 着色） ── */

const STROKE_ATTRS = `fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"`;

/** @type {Record<string, string>} 图标名 → SVG 内部标记（viewBox 0 0 24 24） */
const ICON_PATHS = {
  send: `<path d="M3 11.5 21 3l-8.5 18-2.3-7.2L3 11.5Z" fill="currentColor" stroke="none"/>`,
  stop: `<rect x="6.5" y="6.5" width="11" height="11" rx="1.5" fill="currentColor" stroke="none"/>`,
  plus: `<path ${STROKE_ATTRS} d="M12 5v14M5 12h14"/>`,
  trash: `<path ${STROKE_ATTRS} d="M4 7h16M10 7V4.5h4V7M6.5 7 7.5 20h9L17.5 7M10 11v5.5M14 11v5.5"/>`,
  image: `<rect ${STROKE_ATTRS} x="3" y="5" width="18" height="14" rx="2"/><circle ${STROKE_ATTRS} cx="8.5" cy="9.5" r="1.6"/><path ${STROKE_ATTRS} d="M3 16.5 8 12l4 3.5 4-4 5 5"/>`,
  fileText: `<path ${STROKE_ATTRS} d="M6 3h8l5 5v13H6V3Z"/><path ${STROKE_ATTRS} d="M14 3v5h5M9 12h6M9 16h6"/>`,
  chevronDown: `<path ${STROKE_ATTRS} d="M6 9.5 12 15.5 18 9.5"/>`,
  close: `<path ${STROKE_ATTRS} d="M6 6l12 12M18 6 6 18"/>`,
  user: `<circle ${STROKE_ATTRS} cx="12" cy="8" r="3.6"/><path ${STROKE_ATTRS} d="M4.5 20.5c.8-4 3.9-6 7.5-6s6.7 2 7.5 6"/>`,
  download: `<path ${STROKE_ATTRS} d="M12 4v11M7 11l5 5 5-5M5 20h14"/>`,
  warning: `<path ${STROKE_ATTRS} d="M12 3.5 22 20.5H2L12 3.5Z"/><path ${STROKE_ATTRS} d="M12 10v4.5"/><circle cx="12" cy="17.4" r="1.1" fill="currentColor" stroke="none"/>`,
  menu: `<path ${STROKE_ATTRS} d="M4 6h16M4 12h16M4 18h16"/>`,
  arrowDown: `<path ${STROKE_ATTRS} d="M12 5v13M6.5 12.5 12 18l5.5-5.5"/>`,
  refresh: `<path ${STROKE_ATTRS} d="M20 12a8 8 0 1 1-2.34-5.66M20 3v4h-4"/>`,
  tool: `<path d="M13.2 2 5 13.2h5.2L9 22l8.6-11.2h-5.4L13.2 2Z" fill="currentColor" stroke="none"/>`,
  check: `<path ${STROKE_ATTRS} d="M5 13l4.2 4.2L19 7"/>`,
  terminal: `<rect ${STROKE_ATTRS} x="3" y="4.5" width="18" height="15" rx="2"/><path ${STROKE_ATTRS} d="M7 9.5l3 2.8-3 2.8M12.5 15.5H17"/>`,
  settings: `<path ${STROKE_ATTRS} d="M5 21v-6M5 11V3M12 21v-9M12 8V3M19 21v-4M19 13V3"/><path ${STROKE_ATTRS} d="M2.5 15h5M9.5 8h5M16.5 17h5"/>`,
  camera: `<path ${STROKE_ATTRS} d="M4 8h3l2-2.5h6L17 8h3a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1Z"/><circle ${STROKE_ATTRS} cx="12" cy="13.5" r="3.4"/>`,
  link: `<path ${STROKE_ATTRS} d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path ${STROKE_ATTRS} d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>`,
};

/** @type {Map<string, SVGSVGElement>} */
const templates = new Map();
const svgParser = new DOMParser();

/**
 * 生成内联 SVG 图标（随 currentColor 着色）。
 * @param {string} name ICON_PATHS 的键
 * @param {{size?: number, className?: string}} [options]
 */
export function icon(name, options = {}) {
  const { size = 16, className = "" } = options;
  let template = templates.get(name);
  if (!template) {
    const inner = ICON_PATHS[name];
    if (!inner) throw new Error(`unknown icon: ${name}`);
    const doc = svgParser.parseFromString(
      `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">${inner}</svg>`,
      "image/svg+xml",
    );
    if (doc.querySelector("parsererror")) {
      throw new Error(`icon svg parse error: ${name}`);
    }
    template = /** @type {SVGSVGElement} */ (doc.documentElement);
    templates.set(name, template);
  }
  const svg = /** @type {SVGSVGElement} */ (template.cloneNode(true));
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("aria-hidden", "true");
  const holder = el("span", { class: `icon${className ? ` ${className}` : ""}` });
  holder.appendChild(svg);
  return holder;
}
