/**
 * api.js — REST 契约封装（契约「REST」节；全部网络出口之一）。
 *
 * - endpoint 均为插件内相对路径，勿以 / 开头（bridge / standalone 共用）。
 * - 删除会话统一走 POST conversations/<cid>/delete（契约别名端点，语义与
 *   DELETE conversations/<cid> 完全一致；bridge SDK 无 DELETE 方法，统一走
 *   别名可在两种入口共用同一条路径）。
 * - 错误归一化：同时兼容「包裹」{code,error,message} 与「裸 dict」两种形状；
 *   归一为 ApiError{status, slug, code, message}。
 */

import { TransportError } from "./bridge-client.js";

export class ApiError extends Error {
  /**
   * @param {string} message
   * @param {{status?: number, slug?: string, code?: number, cause?: any}} [meta]
   */
  constructor(message, meta = {}) {
    super(message);
    this.name = "ApiError";
    this.status = meta.status ?? 0;
    this.slug = meta.slug ?? "";
    this.code = meta.code ?? 0;
    this.cause = meta.cause ?? null;
  }

  /** @param {string} slug */
  isSlug(slug) {
    return this.slug === slug;
  }

  /** @param {number} status */
  isStatus(status) {
    return this.status === status;
  }
}

/**
 * 把传输层抛出的任意错误归一为 ApiError。
 * bridge 父页面 reject 的 message 可能是字符串化 JSON，尝试解析还原包裹形状。
 * @param {any} error
 * @returns {ApiError}
 */
export function normalizeError(error) {
  if (error instanceof ApiError) return error;
  if (error instanceof TransportError) {
    return new ApiError(error.message, {
      status: error.status,
      slug: error.slug,
      code: error.code,
      cause: error,
    });
  }
  // 兼容带 HTTP 元数据的普通 Error / plain object（如 mock 层）
  if (error && typeof error === "object" && typeof /** @type {any} */ (error).status === "number") {
    const anyError = /** @type {any} */ (error);
    return new ApiError(String(anyError.message ?? anyError.error ?? "request failed"), {
      status: anyError.status,
      slug: String(anyError.slug ?? ""),
      code: typeof anyError.code === "number" ? anyError.code : anyError.status,
      cause: error,
    });
  }
  if (error instanceof Error) {
    const text = error.message ?? String(error);
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === "object") {
        return new ApiError(String(parsed.message ?? parsed.error ?? text), {
          status: typeof parsed.code === "number" ? parsed.code : 0,
          slug: String(parsed.error ?? parsed.slug ?? ""),
          code: typeof parsed.code === "number" ? parsed.code : 0,
          cause: error,
        });
      }
    } catch {
      /* 非 JSON 字符串错误 */
    }
    return new ApiError(text, { cause: error });
  }
  return new ApiError(String(error ?? "unknown error"), { cause: error });
}

/**
 * @param {import("./bridge-client.js").Transport} transport
 */
export function createApi(transport) {
  /**
   * GET health —— 连通性探测。
   * @returns {Promise<{status?: string, plugin?: string, version?: string}>}
   */
  async function health() {
    try {
      return await transport.apiGet("health", {});
    } catch (error) {
      throw normalizeError(error);
    }
  }

  /**
   * GET personas → {"image_capable": bool, "personas": [...]}
   */
  async function listPersonas() {
    try {
      const data = await transport.apiGet("personas", {});
      return {
        imageCapable: Boolean(data?.image_capable),
        personas: Array.isArray(data?.personas) ? data.personas : [],
      };
    } catch (error) {
      throw normalizeError(error);
    }
  }

  /**
   * GET conversations → {"conversations":[{cid,title,persona_id,updated_at,busy}]}
   */
  async function listConversations() {
    try {
      const data = await transport.apiGet("conversations", {});
      return Array.isArray(data?.conversations) ? data.conversations : [];
    } catch (error) {
      throw normalizeError(error);
    }
  }

  /**
   * POST conversations {persona_id, title?} → {cid}
   * @param {{personaId: string, title?: string}} params
   */
  async function createConversation({ personaId, title }) {
    try {
      const body = { persona_id: personaId };
      if (title) body.title = title;
      const data = await transport.apiPost("conversations", body);
      return String(data?.cid ?? "");
    } catch (error) {
      throw normalizeError(error);
    }
  }

  /**
   * 删除会话：POST conversations/<cid>/delete（契约别名端点）。
   * @param {string} cid
   * @returns {Promise<{ok: boolean}>}
   */
  async function deleteConversation(cid) {
    try {
      const data = await transport.apiPost(`conversations/${encodeURIComponent(cid)}/delete`, {});
      return { ok: Boolean(data?.ok) };
    } catch (error) {
      throw normalizeError(error);
    }
  }

  /**
   * GET history/<cid>?page=&size= → 富历史分页；page≥1，page=1 为最新一页，
   * 页内消息旧→新。
   * @param {string} cid
   * @param {{page?: number, size?: number}} [params]
   */
  async function getHistory(cid, params = {}) {
    const page = Math.max(1, params.page ?? 1);
    const size = Math.min(100, Math.max(1, params.size ?? 30));
    try {
      const data = await transport.apiGet(`history/${encodeURIComponent(cid)}`, {
        page,
        size,
      });
      return {
        cid: String(data?.cid ?? cid),
        page: Number(data?.page ?? page),
        size: Number(data?.size ?? size),
        total: Number(data?.total ?? 0),
        hasMore: Boolean(data?.has_more),
        messages: Array.isArray(data?.messages) ? data.messages : [],
      };
    } catch (error) {
      throw normalizeError(error);
    }
  }

  /**
   * POST send {cid, text, file_ids[]} → {gen_id}（恒 200；校验失败才 4xx）。
   * 409 双分支：session_busy（同会话生成中）/ concurrency_limit（全局并发满）。
   * @param {string} cid
   * @param {string} text
   * @param {string[]} fileIds
   */
  async function send(cid, text, fileIds) {
    try {
      const data = await transport.apiPost("send", {
        cid,
        text,
        file_ids: Array.isArray(fileIds) ? fileIds : [],
      });
      return String(data?.gen_id ?? "");
    } catch (error) {
      throw normalizeError(error);
    }
  }

  /**
   * POST stop/<gen_id> → {ok:true, stopped:true}；gen_id 不存在或已结束 → 404。
   * @param {string} genId
   */
  async function stop(genId) {
    try {
      const data = await transport.apiPost(`stop/${encodeURIComponent(genId)}`, {});
      return { ok: Boolean(data?.ok), stopped: Boolean(data?.stopped) };
    } catch (error) {
      throw normalizeError(error);
    }
  }

  /**
   * POST upload（multipart 字段名 file，单文件）→ 附件元数据。
   * 413 payload_too_large / 415 unsupported_media_type 由 ApiError.slug 区分。
   * onProgress 仅 standalone（XHR 真进度）与 mock（模拟进度）会回调；
   * bridge/fallback 协议无进度事件，回调不会触发（UI 走不确定态）。
   * @param {File} file
   * @param {((p: {loaded: number, total: number}) => void) | undefined} [onProgress]
   */
  async function upload(file, onProgress) {
    try {
      return await transport.upload("upload", file, onProgress);
    } catch (error) {
      throw normalizeError(error);
    }
  }

  /**
   * 触发浏览器下载原图（内嵌 bridge.download("files/<file_id>")；standalone 带 token 直链）。
   * @param {string} fileId
   * @param {string} [filename]
   */
  function downloadFile(fileId, filename) {
    return transport.download(`files/${encodeURIComponent(fileId)}`, {}, filename);
  }

  /**
   * 独立端口原图 <img src>（契约：/api/files/<id>?token=...）。
   * 内嵌模式无此能力（沙箱 iframe 无法带鉴权头），返回 null。
   * @param {string} fileId
   */
  function mediaUrl(fileId) {
    if (typeof transport.mediaUrl === "function") {
      return transport.mediaUrl(`files/${encodeURIComponent(fileId)}`, {});
    }
    return null;
  }

  return {
    health,
    listPersonas,
    listConversations,
    createConversation,
    deleteConversation,
    getHistory,
    send,
    stop,
    upload,
    downloadFile,
    mediaUrl,
  };
}
