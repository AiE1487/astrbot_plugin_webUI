"""SSE 事件封装与缓冲（纯逻辑，无宿主依赖，桩测友好）。

契约（对接契约 v2 §SSE / UserPlan §3.2）：
- 事件类型：reasoning_delta / text_delta / tool_call / tool_result / media /
  stats / done / stopped / error；终态三类 = done / stopped / error(fatal=True)；
- 每个事件的 data 首字段 ``seq`` 从 0 递增；订阅时从 seq=0 全量重放缓冲，
  再接实时；缓冲保留 >=5 分钟（由 chat_service 的 TTL 控制，本模块不感知时间）；
- 帧格式（``stream_response`` + ``text/event-stream``）::

      event: text_delta
      data: {"seq": 7, "delta": "今天北京晴。"}

缓冲条目形状：``{"event": <类型>, "data": {含 seq 的事件数据}}``。
``buffer_append`` 返回其中的 ``data``（即响应体），缓冲里存的是含类型的外层
条目 —— 重放方（chat_service.iter_events）需要同时拿到类型与数据。
"""

import json
from collections import deque

EVENT_TYPES = (
    "reasoning_delta",
    "text_delta",
    "tool_call",
    "tool_result",
    "media",
    "stats",
    "done",
    "stopped",
    "error",
    "ping",
)

TERMINAL_EVENTS = ("done", "stopped")

# ping 不进缓冲、不占业务 seq（契约外保活扩展：前端/EventSource 对未监听的
# 事件类型天然忽略；写入 deviations）
NO_SEQ_EVENTS = ("ping",)


def encode_event(event: str, data: dict) -> str:
    """编码为 SSE 帧：``event: <type>\\ndata: <json>\\n\\n``。

    Args:
        event: 事件类型，必须在 ``EVENT_TYPES`` 内。
        data: 事件数据（须含 ``seq``；``ping`` 保活帧除外，见 ``NO_SEQ_EVENTS``）。

    Raises:
        ValueError: event 不在 EVENT_TYPES，或非 ping 事件缺 seq。
    """
    if event not in EVENT_TYPES:
        raise ValueError(f"未知 SSE 事件类型: {event!r}")
    if event not in NO_SEQ_EVENTS and "seq" not in data:
        raise ValueError("SSE 事件数据缺少 seq 字段")
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def new_buffer(maxlen: int | None = None) -> deque:
    """新建事件缓冲（订阅前重放用）。

    契约要求订阅时从 seq=0 **全量**重放，因此默认不设条数上限（否则长回复
    会挤掉头部事件、破坏全量重放）；保留期由 chat_service 的 5 分钟 TTL 控制。
    ``maxlen`` 仅作为调用方显式要求的兜底上限传入。
    """
    return deque(maxlen=maxlen)


def buffer_append(buffer: deque, event: str, data: dict) -> dict:
    """分配递增 seq、落缓冲并返回带 ``seq`` 的事件数据（返回值即 SSE 响应体）。

    seq 取缓冲中最后一个事件的 seq + 1（空缓冲从 0 开始）。缓冲无条数上限时
    seq 严格单调递增、永不回绕；显式传了 ``maxlen`` 的缓冲在满后丢头部事件，
    此时 seq 仍按全局计数递增（重放只保证可见范围）。

    Args:
        buffer: ``new_buffer`` 产出的缓冲。
        event: 事件类型（须在 ``EVENT_TYPES`` 内）。
        data: 事件数据（不含 seq，由本函数注入）。

    Returns:
        注入 ``seq`` 后的事件数据 dict（SSE 响应体）。
    """
    if event not in EVENT_TYPES:
        raise ValueError(f"未知 SSE 事件类型: {event!r}")
    last_seq = buffer[-1]["data"]["seq"] if buffer else -1
    body = {"seq": last_seq + 1}  # seq 为首字段（契约 §SSE / 模块 docstring 示例）
    body.update(data)
    buffer.append({"event": event, "data": body})
    return body


def buffer_replay(buffer: deque, after_seq: int = 0) -> list[dict]:
    """取缓冲中 ``seq >= after_seq`` 的全部缓冲条目（订阅重放 / 断线恢复）。

    语义：``after_seq`` 是「期望收到的下一个 seq」——默认 0 即契约要求的从
    seq=0 全量重放；客户端已收到 seq N 后以 ``after_seq=N+1`` 续传。

    Args:
        buffer: ``new_buffer`` 产出的缓冲。
        after_seq: 返回 seq 不小于该值的事件；0 表示全量重放。

    Returns:
        按 seq 升序的缓冲条目列表，条目形状 ``{"event", "data"}``；重复调用
        （幂等）返回相同结果。
    """
    return [item for item in buffer if item["data"]["seq"] >= after_seq]
