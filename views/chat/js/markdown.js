/**
 * markdown.js — 轻量 Markdown 渲染器（LLM 回复与用户输入；W1）。
 *
 * 安全约定（与 icons.js 同一铁律）：
 * - 全程 DOM 构建（createElement / createTextNode / setAttribute），
 *   禁止 innerHTML —— 模型/用户内容永不进入 HTML 字符串。
 * - 链接与图片 URL 过协议白名单（http/https/data:image/mailto），
 *   其余一律降级为纯文本；a 带 rel="noopener noreferrer"。
 *
 * 支持范围（聊天场景够用）：
 *   块级：标题 #~######、段落（单换行→<br>）、围栏代码块```lang、引用 >、
 *         无序/有序列表（一层嵌套）、表格 |a|b|、分隔线 ---
 *   行内：`code`、**粗体**、__粗体__、*斜体*、_斜体_、~~删除线~~、
 *         [文本](url)、![图](url)、裸链接自动识别
 *
 * 性能：流式 patch 每次整段重渲染（聊天消息量级 O(n) 可接受），
 *       调用方（message.js）用 rAF 合帧，避免每 delta 重排。
 */

const SAFE_URL_RE = /^(https?:\/\/|data:image\/|mailto:)/i;

/** @param {string} url 协议白名单校验，不通过返回 ""（调用方降级为纯文本） */
function safeUrl(url) {
  const value = String(url ?? "").trim();
  return SAFE_URL_RE.test(value) ? value : "";
}

/* ── 行内解析 ─────────────────────────────────────────── */

/**
 * 行内规则（按优先级排列；同位置命中取先者，保证 ** 优先于 *）。
 * re 的捕获组语义见下方 switch。
 * @type {Array<{type: string, re: RegExp}>}
 */
const INLINE_RULES = [
  { type: "code", re: /`([^`\n]+)`/ },
  { type: "image", re: /!\[([^\]]*)\]\(([^)\s]+)\)/ },
  { type: "link", re: /\[([^\]]+)\]\(([^)\s]+)\)/ },
  { type: "bold", re: /\*\*([^*]+)\*\*/ },
  { type: "boldU", re: /__([^_]+)__/ },
  { type: "italic", re: /\*([^*\s][^*]*?)\*/ },
  { type: "italicU", re: /(?<![\w_])_([^_\s][^_]*?)_(?![\w_])/ },
  { type: "strike", re: /~~([^~]+)~~/ },
  { type: "autolink", re: /https?:\/\/[^\s<>()[\]{}"'`]+/ },
];

/**
 * 行内渲染：扫描 → 最早命中规则 → 递归还原前后文。
 * @param {string} text
 * @returns {DocumentFragment}
 */
function renderInline(text) {
  const frag = document.createDocumentFragment();
  let rest = String(text ?? "");
  for (;;) {
    /** @type {{index: number, rule: any, match: RegExpExecArray}|null} */
    let best = null;
    for (const rule of INLINE_RULES) {
      const match = rule.re.exec(rest);
      if (match && match[0].length > 0 && (best === null || match.index < best.index)) {
        best = { index: match.index, rule, match };
      }
    }
    if (!best) {
      if (rest) frag.appendChild(document.createTextNode(rest));
      return frag;
    }
    if (best.index > 0) frag.appendChild(document.createTextNode(rest.slice(0, best.index)));
    const g = best.match;
    const tail = rest.slice(best.index + g[0].length);
    switch (best.rule.type) {
      case "code": {
        const code = document.createElement("code");
        code.className = "md-code-inline";
        code.textContent = g[1];
        frag.appendChild(code);
        break;
      }
      case "image": {
        const url = safeUrl(g[2]);
        if (url) {
          const img = document.createElement("img");
          img.className = "md-img";
          img.src = url;
          img.alt = g[1] || "";
          img.loading = "lazy";
          img.setAttribute("referrerpolicy", "no-referrer");
          frag.appendChild(img);
        } else if (g[1]) {
          frag.appendChild(document.createTextNode(g[1]));
        }
        break;
      }
      case "link": {
        const url = safeUrl(g[2]);
        if (url) {
          const a = document.createElement("a");
          a.className = "md-link";
          a.href = url;
          a.target = "_blank";
          a.rel = "noopener noreferrer";
          a.appendChild(renderInline(g[1]));
          frag.appendChild(a);
        } else {
          frag.appendChild(renderInline(g[1]));
        }
        break;
      }
      case "bold":
      case "boldU": {
        const strong = document.createElement("strong");
        strong.appendChild(renderInline(g[1]));
        frag.appendChild(strong);
        break;
      }
      case "italic":
      case "italicU": {
        const em = document.createElement("em");
        em.appendChild(renderInline(g[1]));
        frag.appendChild(em);
        break;
      }
      case "strike": {
        const del = document.createElement("del");
        del.className = "md-strike";
        del.appendChild(renderInline(g[1]));
        frag.appendChild(del);
        break;
      }
      case "autolink": {
        // 剥掉尾部标点（句号/逗号等属于句子）；() 只在不成对时剥
        let url = g[0];
        let punct = "";
        while (/[.,;:!?]$/.test(url) || (url.endsWith(")") && !url.includes("("))) {
          punct = url.slice(-1) + punct;
          url = url.slice(0, -1);
        }
        const a = document.createElement("a");
        a.className = "md-link";
        a.href = safeUrl(url);
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.textContent = url;
        frag.appendChild(a);
        if (punct) frag.appendChild(document.createTextNode(punct));
        break;
      }
      default:
        frag.appendChild(document.createTextNode(g[0]));
    }
    rest = tail;
  }
}

/* ── 块级解析 ─────────────────────────────────────────── */

const FENCE_RE = /^\s*```([^\n`]*)$/;
const HEADING_RE = /^\s*(#{1,6})\s+(.*)$/;
const HR_RE = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
const QUOTE_RE = /^\s*>/;
const LIST_ITEM_RE = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const TABLE_SEP_RE = /^\s*\|?\s*:?-{3,}.*\|/;

/** 当前分隔符行是否为表格头分隔行（下一行含 | 且形如 |---|---|） */
function isTableStart(line, nextLine) {
  if (!line.includes("|")) return false;
  return Boolean(nextLine && TABLE_SEP_RE.test(nextLine) && nextLine.includes("|"));
}

/** 段落收集的终止条件：空行或块级起始 */
function isBlockBoundary(line, nextLine) {
  return !line.trim()
    || FENCE_RE.test(line)
    || HEADING_RE.test(line)
    || HR_RE.test(line)
    || QUOTE_RE.test(line)
    || LIST_ITEM_RE.test(line)
    || isTableStart(line, nextLine);
}

/** @param {string} row 去首尾管道后按 | 切分 */
function splitTableRow(row) {
  return row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}

function buildTable(headerRow, bodyRows) {
  const table = document.createElement("table");
  table.className = "md-table";
  const thead = document.createElement("thead");
  const headTr = document.createElement("tr");
  for (const cell of headerRow) {
    const th = document.createElement("th");
    th.appendChild(renderInline(cell));
    headTr.appendChild(th);
  }
  thead.appendChild(headTr);
  table.appendChild(thead);
  if (bodyRows.length > 0) {
    const tbody = document.createElement("tbody");
    for (const row of bodyRows) {
      const tr = document.createElement("tr");
      for (let c = 0; c < headerRow.length; c += 1) {
        const td = document.createElement("td");
        if (c < row.length) td.appendChild(renderInline(row[c]));
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
  }
  return table;
}

/**
 * 列表（支持一层嵌套：子项缩进 >= 父项 + 2 空格）。
 * @param {string[]} lines
 * @param {number} start
 * @param {number} indent 首项缩进
 * @returns {{node: HTMLElement, next: number}}
 */
function parseList(lines, start, indent) {
  const first = LIST_ITEM_RE.exec(lines[start]);
  const ordered = /\d/.test(first[2]);
  const list = document.createElement(ordered ? "ol" : "ul");
  if (ordered) {
    const startNum = parseInt(first[2], 10);
    if (Number.isFinite(startNum) && startNum !== 1) list.setAttribute("start", String(startNum));
  }
  let i = start;
  /** @type {HTMLElement|null} 最后一项（嵌套/续行挂进去） */
  let lastLi = null;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      // 空行：下一行仍是同级/子级列表项则继续，否则结束（松散列表按连续处理）
      const next = lines[i + 1] ?? "";
      if (LIST_ITEM_RE.test(next) && (LIST_ITEM_RE.exec(next)?.[1].length ?? 0) >= indent) {
        i += 1;
        continue;
      }
      break;
    }
    const item = LIST_ITEM_RE.exec(line);
    if (!item) {
      // 列表项续行：缩进更深且已有项 → 追加换行续文
      if (lastLi && line.length - line.trimStart().length > indent) {
        lastLi.appendChild(document.createElement("br"));
        lastLi.appendChild(renderInline(line.trim()));
        i += 1;
        continue;
      }
      break;
    }
    const itemIndent = item[1].length;
    if (itemIndent < indent) break; // 回到父级
    if (itemIndent >= indent + 2 && lastLi) {
      // 嵌套子列表
      const sub = parseList(lines, i, itemIndent);
      lastLi.appendChild(sub.node);
      i = sub.next;
      continue;
    }
    lastLi = document.createElement("li");
    lastLi.appendChild(renderInline(item[3]));
    list.appendChild(lastLi);
    i += 1;
  }
  return { node: list, next: i };
}

/**
 * 块级渲染主循环。
 * @param {string} text
 * @returns {DocumentFragment}
 */
function renderBlocks(text) {
  const frag = document.createDocumentFragment();
  const lines = String(text ?? "").replace(/\r\n?/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const nextLine = lines[i + 1] ?? "";
    if (!line.trim()) {
      i += 1;
      continue;
    }

    // 围栏代码块
    const fence = FENCE_RE.exec(line);
    if (fence) {
      const lang = fence[1].trim();
      const body = [];
      i += 1;
      while (i < lines.length && !/^\s*```/.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // 跳过闭合围栏（无闭合则吃到底）
      const wrap = document.createElement("div");
      wrap.className = "md-code";
      if (lang) {
        const head = document.createElement("div");
        head.className = "md-code-lang";
        head.textContent = lang;
        wrap.appendChild(head);
      }
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      code.textContent = body.join("\n");
      pre.appendChild(code);
      wrap.appendChild(pre);
      frag.appendChild(wrap);
      continue;
    }

    // 标题
    const heading = HEADING_RE.exec(line);
    if (heading) {
      const level = Math.min(6, heading[1].length + 1); // # → h2（正文场景 h1 过大）
      const node = document.createElement(`h${level}`);
      node.appendChild(renderInline(heading[2]));
      frag.appendChild(node);
      i += 1;
      continue;
    }

    // 分隔线
    if (HR_RE.test(line)) {
      frag.appendChild(document.createElement("hr"));
      i += 1;
      continue;
    }

    // 引用
    if (QUOTE_RE.test(line)) {
      const quoted = [];
      while (i < lines.length && QUOTE_RE.test(lines[i])) {
        quoted.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      const blockquote = document.createElement("blockquote");
      blockquote.appendChild(renderBlocks(quoted.join("\n")));
      frag.appendChild(blockquote);
      continue;
    }

    // 表格
    if (isTableStart(line, nextLine)) {
      const header = splitTableRow(line);
      i += 2; // 头 + 分隔行
      const bodyRows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        bodyRows.push(splitTableRow(lines[i]));
        i += 1;
      }
      frag.appendChild(buildTable(header, bodyRows));
      continue;
    }

    // 列表
    const listItem = LIST_ITEM_RE.exec(line);
    if (listItem) {
      const list = parseList(lines, i, listItem[1].length);
      frag.appendChild(list.node);
      i = list.next;
      continue;
    }

    // 段落：收集到空行/块级起始；单换行 → <br>
    const paraLines = [];
    while (i < lines.length && !isBlockBoundary(lines[i], lines[i + 1] ?? "")) {
      paraLines.push(lines[i]);
      i += 1;
    }
    const p = document.createElement("p");
    paraLines.forEach((row, index) => {
      if (index > 0) p.appendChild(document.createElement("br"));
      p.appendChild(renderInline(row));
    });
    frag.appendChild(p);
  }
  return frag;
}

/**
 * 渲染 Markdown 为 DocumentFragment（调用方 append 到容器）。
 * 输入为空时返回空 fragment（容器保持 :empty 以便 CSS 隐藏）。
 * @param {string} text
 * @returns {DocumentFragment}
 */
export function renderMarkdown(text) {
  const frag = document.createDocumentFragment();
  const content = document.createElement("div");
  content.className = "md-content";
  content.appendChild(renderBlocks(text));
  // 无任何块级产出（全空文本）时保持容器为空
  if (content.childElementCount > 0) frag.appendChild(content);
  return frag;
}

/**
 * 把「呼吸光标」放进最后一个可承载文本的块级元素末尾。
 * 代码块/表格/分隔线/引用整体不进入（光标落在其后兄弟位置）。
 * @param {HTMLElement} host renderMarkdown 输出的挂载容器
 * @param {HTMLElement} cursor
 */
export function placeCursorAtEnd(host, cursor) {
  let target = /** @type {HTMLElement} */ (host);
  for (;;) {
    const last = /** @type {HTMLElement} */ (target.lastElementChild);
    if (!last) break;
    const tag = last.tagName;
    if (tag === "PRE" || tag === "TABLE" || tag === "HR") break; // 光标落在块之后
    if (tag === "UL" || tag === "OL" || tag === "BLOCKQUOTE") {
      target = last;
      continue;
    }
    target = last;
    break;
  }
  target.appendChild(cursor);
}
