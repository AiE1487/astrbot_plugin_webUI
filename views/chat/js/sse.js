/**
 * sse.js — 流式订阅管理（M2 ①；全部网络出口之一）。
 *
 * 订阅 GET stream/<gen_id>：
 * - 服务端订阅即从缓冲 seq=0 全量重放，再接实时；同一 gen_id 可重复订阅。
 * - 按 seq 去重 + 严格排序：重复/回放的旧事件丢弃；乱序事件进入 pending 缓冲，
 *   空洞补齐后按序派发（增量渲染合并 DOM 写入由 store/render 层负责）。
 * - 每次重订阅前先 unsubscribeSSE(旧 subscriptionId)（自研 fetch 流 abort 旧连接），
 *   防宿主侧订阅堆积（契约「前端实现约定」）。
 * - 断流（bridge sse_state error / fetch 流异常中断）且未收终态：有限次自动重订阅，
 *   依赖服务端全量重放续流（计划 F3 刷新恢复）。
 * - 终态三分支必居其一：done 落定 / stopped 显「已停止」/ error(fatal) 错误面板；
 *   error.fatal=false 时流继续，仅作为流内事件透传。
 * - 订阅 404（gen_id 不存在或缓冲过期）视为终局：onGone 回调，上层清理
 *   sessionStorage 活跃 gen_id 后不再重试。
 */

import { normalizeError } from "./api.js";

const MAX_RETRIES = 3;
const RETRY_BASE_MS = 1000;

/** 终态事件类型集合（契约 SSE 节）。 */
const TERMINAL_TYPES = new Set(["done", "stopped"]);

/** @typedef {"subscribing"|"open"|"retrying"|"closed"|"gone"} StreamState */

/**
 * @typedef {Object} StreamEvent
 * @property {number} seq
 * @property {string} type 事件类型（reasoning_delta/text_delta/tool_call/tool_result/error/...）
 * @property {any} data 事件 JSON（首字段 seq）
 */

/**
 * @typedef {Object} StreamCallbacks
 * @property {(genId: string, event: StreamEvent) => void} onEvent 非终态事件（含流内非 fatal error）
 * @property {(genId: string, type: "done"|"stopped"|"error", data: any) => void} onTerminal 终态
 * @property {(genId: string, state: StreamState) => void} [onState] 订阅状态变化
 * @property {(genId: string) => void} [onGone] 404 / 缓冲过期（终局，不再重试）
 * @property {(genId: string) => void} [onReset] 每次订阅开始时通知，供上层重置流式
 *   积累状态——服务端从 seq=0 全量重放，重放即重建（断线重连/切回会话时上层
 *   已积累的 text/tools/attachments 若不清空会被重放事件重复累加）
 */

/**
 * @param {import("./bridge-client.js").Transport} transport
 * @param {StreamCallbacks} callbacks
 */
export function createStreamManager(transport, callbacks) {
  /** @type {Map<string, Stream>} */
  const streams = new Map();

  class Stream {
    /**
     * @param {string} genId
     */
    constructor(genId) {
      this.genId = genId;
      /** @type {string|null} */
      this.subId = null;
      this.appliedSeq = -1;
      /** @type {Map<number, StreamEvent>} */
      this.pending = new Map();
      this.closed = false;
      this.retries = 0;
      /** @type {number|null} */
      this.retryTimer = null;
      this.generations = 0; // 重订阅代数（丢弃旧代回调）
    }

    async subscribe() {
      if (this.closed) return;
      this.generations += 1;
      const generation = this.generations;
      // 每次订阅前先退订旧通道（bridge）或 abort 旧 fetch（standalone/mock）
      await this.teardownSubscription();
      // 通知上层重置流式积累状态：服务端将从 seq=0 全量重放，重放即重建，
      // 上层旧积累（text/tools/attachments 等）不清空会被重放事件重复累加
      callbacks.onReset?.(this.genId);
      // 重置游标：服务端将从 seq=0 全量重放
      this.appliedSeq = -1;
      this.pending.clear();
      this.emitState("subscribing");
      try {
        const subId = await transport.subscribeSSE(
          `stream/${encodeURIComponent(this.genId)}`,
          {
            onMessage: (payload) => {
              if (generation !== this.generations || this.closed) return;
              this.handleMessage(payload);
            },
            onOpen: () => {
              if (generation !== this.generations || this.closed) return;
              this.retries = 0;
              this.emitState("open");
            },
            onError: () => {
              if (generation !== this.generations || this.closed) return;
              // 流被服务端关闭/异常（终态关流属正常路径，closed 后不会到这里）
              this.scheduleRetry();
            },
          },
          {},
        );
        if (this.closed || generation !== this.generations) {
          // 订阅期间已被终态/退订：立即清理刚建立的订阅
          transport.unsubscribeSSE(subId).catch(() => {});
          return;
        }
        this.subId = subId;
      } catch (error) {
        if (this.closed || generation !== this.generations) return;
        const apiError = normalizeError(error);
        if (apiError.isStatus(404) || apiError.isSlug("not_found") || /404|not_found/i.test(apiError.message)) {
          // gen_id 不存在或缓冲已过期：终局，不再重试
          this.closed = true;
          streams.delete(this.genId);
          this.emitState("gone");
          callbacks.onGone?.(this.genId);
          return;
        }
        this.scheduleRetry(apiError);
      }
    }

    /** @param {{eventType?: string, parsed?: any, raw?: string}} payload */
    handleMessage(payload) {
      const data = payload?.parsed;
      if (!data || typeof data !== "object") return;
      const seq = Number(data.seq);
      if (!Number.isInteger(seq) || seq < 0) return;
      if (seq <= this.appliedSeq) return; // 重放重复事件
      const type = String(payload.eventType || "message");
      const event = /** @type {StreamEvent} */ ({ seq, type, data });
      if (seq === this.appliedSeq + 1) {
        this.apply(event);
        this.drain();
      } else {
        // 乱序：缓冲等待空洞补齐（超过上限说明流已不可信，放弃等待）
        if (this.pending.size > 256) {
          this.scheduleRetry();
          return;
        }
        this.pending.set(seq, event);
      }
    }

    drain() {
      let guard = 0;
      while (guard < 1024 && this.pending.has(this.appliedSeq + 1)) {
        guard += 1;
        const next = /** @type {StreamEvent} */ (this.pending.get(this.appliedSeq + 1));
        this.pending.delete(this.appliedSeq + 1);
        this.apply(next);
      }
    }

    /** @param {StreamEvent} event */
    apply(event) {
      if (this.closed) return;
      this.appliedSeq = event.seq;
      const data = event.data ?? {};
      if (TERMINAL_TYPES.has(event.type)) {
        this.finish(event.type, data);
        return;
      }
      if (event.type === "error" && data.fatal === true) {
        // error{fatal:true} 终态关流（契约 SSE 节）
        this.finish("error", data);
        return;
      }
      callbacks.onEvent(this.genId, event);
    }

    /**
     * 终态落定：关流、清订阅、通知上层（上层负责清 sessionStorage 活跃 gen_id）。
     * @param {"done"|"stopped"|"error"} type
     * @param {any} data
     */
    finish(type, data) {
      if (this.closed) return;
      this.closed = true;
      streams.delete(this.genId);
      this.teardownSubscription().catch(() => {});
      callbacks.onTerminal(this.genId, type, data);
    }

    /** 本地强制关闭（不产生终态事件；如会话被删除）。 */
    abort() {
      if (this.closed) return;
      this.closed = true;
      streams.delete(this.genId);
      this.clearRetryTimer();
      this.teardownSubscription().catch(() => {});
      this.emitState("closed");
    }

    /** @param {any} [error] */
    scheduleRetry(error) {
      if (this.closed) return;
      if (this.retries >= MAX_RETRIES) {
        // 重试耗尽：彻底关闭（退订残留通道、移出管理表）并以独立 "exhausted"
        // 状态通知 store 清理流式占位（评审发现 3），不再自动重试
        this.closed = true;
        streams.delete(this.genId);
        this.teardownSubscription().catch(() => {});
        this.emitState("exhausted");
        return;
      }
      this.retries += 1;
      this.emitState("retrying");
      this.clearRetryTimer();
      this.retryTimer = window.setTimeout(() => {
        this.retryTimer = null;
        this.subscribe();
      }, RETRY_BASE_MS * this.retries);
      if (error) {
        // 保留诊断信息（console 不进 UI，不违反「零硬编码文案」）
        console.warn(`[webui] stream ${this.genId} subscribe failed, retry ${this.retries}:`, error);
      }
    }

    clearRetryTimer() {
      if (this.retryTimer !== null) {
        window.clearTimeout(this.retryTimer);
        this.retryTimer = null;
      }
    }

    async teardownSubscription() {
      if (this.subId === null) return;
      const subId = this.subId;
      this.subId = null;
      try {
        await transport.unsubscribeSSE(subId);
      } catch {
        /* 退订失败不阻塞 UI */
      }
    }

    /** @param {StreamState} state */
    emitState(state) {
      callbacks.onState?.(this.genId, state);
    }
  }

  return {
    /**
     * 开始（或重新）订阅一个 gen_id。已存在同 genId 的流则直接复用。
     * @param {string} genId
     */
    ensure(genId) {
      let stream = streams.get(genId);
      if (!stream) {
        stream = new Stream(genId);
        streams.set(genId, stream);
      }
      stream.subscribe();
      return stream;
    },
    /**
     * 本地关闭订阅（无终态事件；用于会话删除等场景）。
     * @param {string} genId
     */
    abort(genId) {
      streams.get(genId)?.abort();
    },
    /** 关闭全部订阅（如连接层切换）。 */
    abortAll() {
      for (const stream of streams.values()) stream.abort();
    },
    /** @param {string} genId @returns {boolean} 是否存在活跃订阅 */
    has(genId) {
      return streams.has(genId);
    },
  };
}
