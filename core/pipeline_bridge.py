"""宿主对接唯一层：webchat 队列注入 + back_queue 消费翻译 + 级联删除（v0.2）。

契约（UserPlan/开发计划.md §0.2，已对照宿主 4.29.4 实源逐条验证）：

- 插件事务层 = 「webchat 事件注入者 + back_queue 消费者」：会话管理、请求
  拼接（人格/历史/begin_dialogs）、工具注入（全宿主工具面）全部归还后端；
- 会话标识映射：插件 ``cid``（uuid4hex）= 宿主 ``platform_sessions.session_id``；
  umo = ``webchat:FriendMessage:webchat!astrbot_plugin_webui!<cid>``（固定
  creator，与官方 WebChat 会话列表天然隔离——dashboard 会话页按 creator 过滤）；
- 注入顺序（对标 dashboard/services/chat_service.py:1332-1385 实源）：
  ① 用户消息先落平台历史 ``message_history_manager.insert``（dashboard 对
  webchat 私聊的唯一用户落库点，插件注入必须自补）；② ``get_or_create_back_queue``
  （首参名 ``request_id``，按位置传参）；③ 会话队列 ``put((creator, cid, payload))``；
  **绝不可调用 ``set_listener``**（会顶掉 webchat 适配器的全局唯一回调）；
- back_queue 载荷形状见宿主 ``webchat_event.py``（统一
  ``{"type","data","streaming","message_id","chain_type?"}``），翻译表：
  plain+chain_type="reasoning" -> reasoning_delta；plain 无 chain_type 且
  streaming -> text_delta；plain+tool_call（data=JSON 串）-> tool_call{start}；
  plain+tool_call_result -> tool_result{result|error 互斥, elapsed_ms=本地计时}；
  plain+agent_stats -> 暂存 usage；image/file/record/video 帧（宿主已落盘
  ``data/attachments/``，宿主不注册 db）-> 插件注册 db 附件 + 插件索引；
  end -> bot 历史落库后 done{message_id, usage}；
- LLM 错误语义（与官方 WebChat 一致）：provider 错误以 bot 文本透传，不再有
  插件侧 fatal error 事件；本模块只对**传输层异常**（空闲超时、排水循环崩溃）
  写 error 终态；
- 停止：``active_event_registry.request_agent_stop_all(umo)``（优雅中止，
  不 stop_event：runner 保存部分历史、管道仍发 end 帧）；排水循环带
  **空闲超时兜底**（``IDLE_TIMEOUT_SECONDS`` 无任何载荷 -> error 终态并清理，
  防 end 帧异常缺失导致 single-flight 永久占用）；
- 级联删除对标宿主 chat_service.delete_session_internal（:1415-1463 实源）：
  平台历史删除**必须显式传超大 offset_sec**（宿主默认 86400 只删最近 24h）。

宿主 API 实证（v4.29.4）：
- ``context.message_history_manager``（PlatformMessageHistoryManager，
  core_lifecycle.py:235）/ ``context.conversation_manager``（:232）/
  ``context.get_db()``（star/context.py:846）；
- ``webchat_queue_mgr`` 为模块级单例（webchat_queue_mgr.py:207）；
  ``active_event_registry`` 同（active_event_registry.py:192）；
- webchat 适配器无条件实例化（platform/manager.py:115-117），注入必被消费；
- bot 历史落库形状 = ``build_bot_history_content`` 同构
  ``{"type":"bot","message":parts, "reasoning"?, "agent_stats"?}``，sender
  固定 ``"bot"``（chat_service.py:906-923 实源）。

关于 ``BotMessageAccumulator``：契约允许二选一（导入宿主实现 / 本地复刻）。
本地复刻的理由：宿主实现位于 ``astrbot.dashboard.services.chat_service``，
导入即拖起 dashboard 全栈（chunked_upload_service、SQLAlchemy 模型树等），
插件加载期代价高且随宿主版本漂移风险大；本地复刻逐行对齐宿主实源（:160-275）
与 ``build_bot_history_content``（:141-157），并注明对照行号。
"""

import asyncio
import json
import time
import uuid
from pathlib import Path, PurePosixPath

from astrbot.api import logger
from astrbot.api.star import Context

# ---------------------------------------------------------------------------
# 常量（会话标识映射，§0.2）
# ---------------------------------------------------------------------------

PLUGIN_CREATOR = "astrbot_plugin_webui"
UMO_PLATFORM = "webchat"
UMO_TYPE = "FriendMessage"
SESSION_PREFIX = f"webchat!{PLUGIN_CREATOR}!"

# 排水循环空闲超时兜底（120s 无任何载荷 -> error 终态并清理，§0.2 停止）
IDLE_TIMEOUT_SECONDS = 120.0
# 平台历史删除 offset：宿主默认 86400 只删最近 24h，级联删除必须显式传超大值
HISTORY_DELETE_OFFSET_SEC = 99999999
# 会话列表分页大小：宿主 db 默认 20 会静默截断，必须显式传大值（§0.2 CRUD）
LIST_PAGE_SIZE = 10000

# back_queue 媒体帧前缀 -> (帧类型, 媒体 part 类型)；file/video 帧的 data 为
# "[FILE]<stored>|<display>"（webchat_event.py:114-183 实源）
_MEDIA_FRAME_PREFIXES = {
    "image": "[IMAGE]",
    "record": "[RECORD]",
    "file": "[FILE]",
    "video": "[VIDEO]",
}

# 宿主把工具执行异常以 "error: " 前缀塞进 result 内容
# （tool_loop_agent_runner.py:1414-1418 实源），据此做 result|error 互斥切分
TOOL_ERROR_PREFIX = "error: "


class PipelineBridgeError(Exception):
    """宿主管道对接层异常（注入/消费失败，chat_service -> error 终态）。"""


def build_umo(cid: str) -> str:
    """由会话 id 构造宿主 ``unified_msg_origin``。

    Returns:
        形如 ``webchat:FriendMessage:webchat!astrbot_plugin_webui!<cid>`` 的
        字符串（与宿主 ``build_webchat_unified_msg_origin`` 逐字段同构，
        chat_service.py:361-374 实源）。
    """
    return f"{UMO_PLATFORM}:{UMO_TYPE}:{SESSION_PREFIX}{cid}"


def parse_umo(umo: str) -> str | None:
    """从 ``unified_msg_origin`` 反解插件命名空间的 cid；不符返回 None。"""
    parts = str(umo).split(":")
    if len(parts) != 3:
        return None
    platform, msg_type, session_id = parts
    if platform != UMO_PLATFORM or msg_type != UMO_TYPE:
        return None
    if not session_id.startswith(SESSION_PREFIX):
        return None
    cid = session_id[len(SESSION_PREFIX):]
    return cid or None


# ---------------------------------------------------------------------------
# 宿主单例/管理器获取（全部经 context，桩环境惰性失败）
# ---------------------------------------------------------------------------


def webchat_queue_mgr():
    """宿主 webchat 队列管理器单例（惰性导入；桩环境抛 PipelineBridgeError）。"""
    try:
        from astrbot.core.platform.sources.webchat.webchat_queue_mgr import (
            webchat_queue_mgr as mgr,
        )
    except Exception as exc:  # noqa: BLE001 —— 桩环境/宿主缺失
        raise PipelineBridgeError(f"webchat_queue_mgr 不可用: {exc}") from exc
    return mgr


def active_event_registry():
    """宿主活跃事件注册表单例（惰性导入；桩环境抛 PipelineBridgeError）。"""
    try:
        from astrbot.core.utils.active_event_registry import active_event_registry
    except Exception as exc:  # noqa: BLE001 —— 桩环境/宿主缺失
        raise PipelineBridgeError(f"active_event_registry 不可用: {exc}") from exc
    return active_event_registry


def get_db(context: Context):
    """宿主数据库（``context.get_db()``）。"""
    return context.get_db()


def message_history_manager(context: Context):
    """平台历史管理器（``context.message_history_manager``）。"""
    mgr = getattr(context, "message_history_manager", None)
    if mgr is None:
        raise PipelineBridgeError("context.message_history_manager 不可用")
    return mgr


def conversation_manager(context: Context):
    """宿主会话管理器（``context.conversation_manager``）。"""
    mgr = getattr(context, "conversation_manager", None)
    if mgr is None:
        raise PipelineBridgeError("context.conversation_manager 不可用")
    return mgr


# ---------------------------------------------------------------------------
# bot 历史积累器（本地复刻宿主 BotMessageAccumulator，对照
# dashboard/services/chat_service.py:102-275 实源逐行对齐）
# ---------------------------------------------------------------------------


def normalize_reasoning_message_parts(message_parts, reasoning: str = "") -> list[dict]:
    """reasoning part 归一为 think part，并补插 ``reasoning`` 顶层字段内容。"""
    parts: list[dict] = []
    for part in message_parts or []:
        if not isinstance(part, dict):
            continue
        copied = dict(part)
        if copied.get("type") == "reasoning":
            copied = {"type": "think", "think": copied.get("text", "")}
        parts.append(copied)
    if reasoning and not any(part.get("type") == "think" for part in parts):
        parts.insert(0, {"type": "think", "think": reasoning})
    return parts


def extract_reasoning_from_message_parts(message_parts: list[dict]) -> str:
    """从 think parts 拼接 reasoning 全文。"""
    return "".join(
        part.get("think")
        for part in message_parts
        if isinstance(part, dict)
        and part.get("type") == "think"
        and isinstance(part.get("think"), str)
    )


def collect_plain_text_from_message_parts(message_parts: list[dict]) -> str:
    """从 plain parts 拼接正文全文（history 读取同款）。"""
    return "".join(
        part.get("text")
        for part in message_parts
        if isinstance(part, dict)
        and part.get("type") == "plain"
        and isinstance(part.get("text"), str)
    )


def build_bot_history_content(
    message_parts: list[dict],
    *,
    agent_stats: dict | None = None,
) -> dict:
    """构造 bot 平台历史 content（宿主 ``build_bot_history_content`` 同构）。"""
    normalized_parts = normalize_reasoning_message_parts(message_parts)
    content: dict = {"type": "bot", "message": normalized_parts}
    reasoning = extract_reasoning_from_message_parts(normalized_parts)
    if reasoning:
        content["reasoning"] = reasoning
    if agent_stats:
        content["agent_stats"] = agent_stats
    return content


class BotMessageAccumulator:
    """bot 侧消息 parts 积累器（宿主同名类 :160-275 的本地复刻）。"""

    def __init__(self) -> None:
        self.parts: list[dict] = []
        self.pending_text = ""
        self.pending_tool_calls: dict[str, dict] = {}

    def has_content(self) -> bool:
        return bool(self.parts or self.pending_text or self.pending_tool_calls)

    def add_plain(self, result_text: str, *, chain_type: str | None, streaming: bool) -> None:
        """按载荷类型积累：reasoning -> think，tool_call(_result) -> 工具段，
        其余按 streaming 追加/替换正文缓冲。"""
        if chain_type == "tool_call":
            self._flush_pending_text()
            self._store_tool_call(result_text)
            return
        if chain_type == "tool_call_result":
            self._flush_pending_text()
            self._store_tool_call_result(result_text)
            return
        if chain_type == "reasoning":
            self._flush_pending_text()
            self._append_think_part(result_text)
            return
        if streaming:
            self.pending_text += result_text
        else:
            self.pending_text = result_text

    def add_part(self, part: dict | None) -> None:
        """追加结构化非文本 part（媒体/actionrow）。"""
        if not part:
            return
        self._flush_pending_text()
        self.parts.append(part)

    def build_message_parts(self, *, include_pending_tool_calls: bool = False) -> list[dict]:
        """产出最终 parts（可选把未回填的 tool_call 也落进历史）。"""
        self._flush_pending_text()
        if include_pending_tool_calls and self.pending_tool_calls:
            for tool_call in self.pending_tool_calls.values():
                self.parts.append({"type": "tool_call", "tool_calls": [tool_call]})
            self.pending_tool_calls = {}
        return self.parts

    def _flush_pending_text(self) -> None:
        if not self.pending_text:
            return
        if self.parts and self.parts[-1].get("type") == "plain":
            last_text = self.parts[-1].get("text")
            self.parts[-1]["text"] = f"{last_text or ''}{self.pending_text}"
        else:
            self.parts.append({"type": "plain", "text": self.pending_text})
        self.pending_text = ""

    def _append_think_part(self, text: str) -> None:
        if not text:
            return
        if self.parts and self.parts[-1].get("type") == "think":
            last_text = self.parts[-1].get("think")
            self.parts[-1]["think"] = f"{last_text or ''}{text}"
        else:
            self.parts.append({"type": "think", "think": text})

    def _store_tool_call(self, result_text: str) -> None:
        tool_call = _parse_json_object(result_text)
        if not tool_call:
            return
        tool_call_id = str(tool_call.get("id") or "")
        if not tool_call_id:
            return
        self.pending_tool_calls[tool_call_id] = tool_call

    def _store_tool_call_result(self, result_text: str) -> None:
        tool_result = _parse_json_object(result_text)
        if not tool_result:
            return
        tool_call_id = str(tool_result.get("id") or "")
        if not tool_call_id:
            return
        tool_call = self.pending_tool_calls.pop(tool_call_id, None) or {"id": tool_call_id}
        tool_call["result"] = tool_result.get("result")
        tool_call["finished_ts"] = tool_result.get("ts")
        self.parts.append({"type": "tool_call", "tool_calls": [tool_call]})


def _parse_json_object(raw_text: str) -> dict | None:
    try:
        parsed = json.loads(raw_text)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# 注入（webchat 会话队列）
# ---------------------------------------------------------------------------


def strip_message_parts_path_fields(message_parts: list[dict]) -> list[dict]:
    """剥掉 parts 的 ``path`` 字段（宿主落库同款，message_parts_helper.py:57）。"""
    return [{k: v for k, v in part.items() if k != "path"} for part in message_parts if isinstance(part, dict)]


async def inject_user_message(
    context: Context,
    cid: str,
    parts: list[dict],
    gen_id: str,
) -> dict:
    """把一条用户消息注入 webchat 管道（§0.2 注入三步，顺序固定）。

    ① 用户消息先落平台历史（sender/llm_checkpoint_id 自补）；② 创建 back_queue
    （``get_or_create_back_queue(gen_id, cid)`` 按位置传参，首参名 request_id）；
    ③ 会话队列 ``put((creator, cid, payload))``，payload 含
    ``{"message", "flags", "message_id", "llm_checkpoint_id"}``
    （selected_provider/selected_model 留空走 umo 绑定）。**不调用 set_listener**。

    Returns:
        ``{"back_queue": asyncio.Queue, "llm_checkpoint_id": str}``。

    Raises:
        PipelineBridgeError: 宿主队列设施不可用或注入失败。
    """
    mgr = webchat_queue_mgr()
    checkpoint = uuid.uuid4().hex
    try:
        # ① 用户消息落平台历史（dashboard 唯一用户落库点在 chat_service.py:1332-1339，
        #    插件注入必须自补；签名按 platform_message_history_mgr.py:11-31 实源）
        await message_history_manager(context).insert(
            platform_id=UMO_PLATFORM,
            user_id=cid,
            content={"type": "user", "message": strip_message_parts_path_fields(parts)},
            sender_id=PLUGIN_CREATOR,
            sender_name=PLUGIN_CREATOR,
            llm_checkpoint_id=checkpoint,
        )
        # ② back_queue（按位置传参：request_id, conversation_id）
        back_queue = mgr.get_or_create_back_queue(gen_id, cid)
        # ③ 会话队列注入（tuple 形状同宿主 chat_service.py:1366-1385）
        chat_queue = mgr.get_or_create_queue(cid)
        await chat_queue.put(
            (
                PLUGIN_CREATOR,
                cid,
                {
                    "message": parts,
                    "flags": {"enable_streaming": True, "enable_reasoning": True},
                    "message_id": gen_id,
                    "llm_checkpoint_id": checkpoint,
                },
            )
        )
    except PipelineBridgeError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 注入失败统一包装
        raise PipelineBridgeError(f"webchat 注入失败 cid={cid}: {exc}") from exc
    return {"back_queue": back_queue, "llm_checkpoint_id": checkpoint}


def remove_back_queue(gen_id: str) -> None:
    """移除某 gen_id 的 back_queue（幂等；close_event 置位使宿主停止投递）。"""
    try:
        webchat_queue_mgr().remove_back_queue(gen_id)
    except PipelineBridgeError:
        pass  # 桩/卸载环境：无宿主队列可清理
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[webui] remove_back_queue 失败 gen_id={gen_id}: {exc}")


# ---------------------------------------------------------------------------
# back_queue 排水 + SSE 翻译（§0.2 翻译表）
# ---------------------------------------------------------------------------


def usage_from_agent_stats(agent_stats: dict | None) -> dict:
    """agent_stats（AgentStats.to_dict()）-> 契约 usage ``{"output","total"}``。

    token_usage = TokenUsage ``{input_other, input_cached, output}``
    （provider/entities.py:422-436 实源，total = input_other+input_cached+output）。
    """
    stats = agent_stats if isinstance(agent_stats, dict) else {}
    token_usage = stats.get("token_usage")
    token_usage = token_usage if isinstance(token_usage, dict) else {}
    output = _to_int(token_usage.get("output"))
    total = _to_int(token_usage.get("input_other")) + _to_int(token_usage.get("input_cached")) + output
    return {"output": output, "total": total}


def _to_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _split_media_payload(frame_type: str, data) -> tuple[str, str | None]:
    """解析媒体帧 data（``[IMAGE]文件名`` / ``[FILE]文件名|展示名``）。

    Returns:
        ``(stored_filename, display_name|None)``；解析失败返回 ``("", None)``。
    """
    prefix = _MEDIA_FRAME_PREFIXES.get(frame_type, "")
    text = str(data or "")
    if prefix and text.startswith(prefix):
        text = text[len(prefix):]
    if frame_type in ("file", "video") and "|" in text:
        stored, display = text.split("|", 1)
        return _safe_basename(stored), _safe_basename(display) or None
    return _safe_basename(text), None


def _safe_basename(filename) -> str:
    name = PurePosixPath(str(filename or "").replace("\\", "/")).name.replace("\x00", "").strip()
    return "" if name in ("", ".", "..") else name


async def consume_generation(
    context: Context,
    cid: str,
    gen_id: str,
    back_queue,
    llm_checkpoint_id: str | None,
    emit,
    idle_timeout: float | None = None,
    data_dir: Path | None = None,
    cid_bind: str | None = None,
) -> None:
    """排水 back_queue 并翻译为 SSE 事件，直到 end 帧（或空闲超时/取消）。

    Args:
        emit: ``emit(event, data)`` 回调（chat_service 注入；终态事件由其负责
            幂等落终态标记，本模块只管翻译）。
        idle_timeout: 空闲超时兜底（秒）；None 时调用时取模块常量
            ``IDLE_TIMEOUT_SECONDS``（测试可注入短超时触发兜底分支）。
        data_dir: 插件数据目录（bot 媒体帧注册插件索引用；None 跳过索引）。
        cid_bind: 媒体附件绑定的会话 cid（None 不绑定）。

    finally 中**必须** ``remove_back_queue(gen_id)``（队列泄漏纪律，
    webchat_queue_mgr.py:48-84 + chat_service.py:1263 实源）。
    """
    mgr = webchat_queue_mgr()
    timeout = IDLE_TIMEOUT_SECONDS if idle_timeout is None else float(idle_timeout)
    accumulator = BotMessageAccumulator()
    agent_stats: dict = {}
    tool_started_at: dict[str, float] = {}
    try:
        while True:
            try:
                payload = await asyncio.wait_for(back_queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.error(
                    f"[webui] 排水空闲超时（{timeout:g}s 无载荷），写 error 终态并退出 gen_id={gen_id} cid={cid}"
                )
                emit(
                    "error",
                    {
                        "message": "生成超时：管道长时间未返回数据",
                        "fatal": True,
                        "code": "server_error",
                    },
                )
                return
            if not isinstance(payload, dict):
                continue  # 坏载荷防御：不炸流
            payload_message_id = payload.get("message_id")
            if payload_message_id and str(payload_message_id) != gen_id:
                logger.warning(f"[webui] back_queue message_id 不匹配，丢弃 gen_id={gen_id}")
                continue

            frame_type = payload.get("type")
            data = payload.get("data", "")
            streaming = bool(payload.get("streaming", False))
            chain_type = payload.get("chain_type")

            if chain_type == "agent_stats":
                agent_stats = _parse_json_object(str(data)) or {}
                continue  # 暂存 usage，done 事件使用；不产 SSE 事件

            if frame_type == "plain":
                text = data if isinstance(data, str) else str(data or "")
                if chain_type == "reasoning":
                    accumulator.add_plain(text, chain_type="reasoning", streaming=streaming)
                    if text:
                        emit("reasoning_delta", {"delta": text})
                elif chain_type == "tool_call":
                    call = _parse_json_object(text) or {}
                    accumulator.add_plain(text, chain_type="tool_call", streaming=streaming)
                    call_id = str(call.get("id") or "")
                    if call_id:
                        # 坏 JSON/缺 id 的载荷不产无意义事件（前端按 id 关联步骤），
                        # 历史积累器自防御（_store_tool_call 静默丢弃）
                        tool_started_at[call_id] = time.monotonic()
                        emit(
                            "tool_call",
                            {
                                "phase": "start",
                                "id": call_id,
                                "name": str(call.get("name") or ""),
                                "args": call.get("args") if isinstance(call.get("args"), dict) else {},
                            },
                        )
                elif chain_type == "tool_call_result":
                    result_payload = _parse_json_object(text) or {}
                    accumulator.add_plain(text, chain_type="tool_call_result", streaming=streaming)
                    call_id = str(result_payload.get("id") or "")
                    if call_id:
                        started = tool_started_at.pop(call_id, None)
                        elapsed_ms = int((time.monotonic() - started) * 1000) if started is not None else 0
                        body: dict = {"id": call_id, "elapsed_ms": max(0, elapsed_ms)}
                        raw_result = result_payload.get("result")
                        result_text = "" if raw_result is None else str(raw_result)
                        if result_text.startswith(TOOL_ERROR_PREFIX):
                            body["error"] = result_text[len(TOOL_ERROR_PREFIX):]
                        else:
                            body["result"] = result_text
                        emit("tool_result", body)
                else:
                    # 正文：streaming 增量；无 chain_type 的非流式整段按防御也走
                    # text_delta（注入恒 enable_streaming=true，非流式帧理论上不出现）
                    accumulator.add_plain(text, chain_type=None, streaming=streaming)
                    if text:
                        emit("text_delta", {"delta": text})
            elif frame_type in _MEDIA_FRAME_PREFIXES:
                # 宿主已自行落盘 data/attachments/（webchat_event.py:114-183 实源，
                # 宿主不注册 db）：插件解析文件名 -> db.insert_attachment -> 插件索引
                stored_name, display_name = _split_media_payload(frame_type, data)
                part = await register_media_frame(
                    context, cid, frame_type, stored_name, display_name,
                    data_dir=data_dir, cid_bind=cid_bind,
                )
                if part is not None:
                    accumulator.add_part(part)
            elif frame_type == "actionrow" and isinstance(data, dict):
                accumulator.add_part({"type": "actionrow", **data})  # 历史忠实（无 SSE 映射）
            elif frame_type == "end":
                # 管道收尾（scheduler.py:96-100 event.send(None)）：bot 历史落库后 done
                saved_row = await save_bot_history(context, cid, accumulator, agent_stats, llm_checkpoint_id)
                row_id = getattr(saved_row, "id", None) if saved_row is not None else None
                message_id = f"m_{row_id}" if row_id else f"m_{uuid.uuid4().hex}"
                usage = usage_from_agent_stats(agent_stats)
                emit("done", {"message_id": message_id, "usage": usage})
                return
            # 其余帧（complete/run_started/audio_chunk/follow_up_captured）：宿主
            # 供官方前端使用的冗余载荷，本契约无 SSE 映射，忽略（正文/思考已由
            # 流式 plain 帧覆盖）
    except asyncio.CancelledError:
        raise  # 插件 terminate / 级联删除取消：finally 清理后照常上抛
    except Exception as exc:  # noqa: BLE001 —— 排水循环崩溃：error 终态
        logger.exception(f"[webui] back_queue 排水异常 gen_id={gen_id} cid={cid}")
        emit(
            "error",
            {
                "message": f"生成失败：{exc or type(exc).__name__}",
                "fatal": True,
                "code": "server_error",
            },
        )
    finally:
        mgr.remove_back_queue(gen_id)


async def register_media_frame(
    context: Context,
    cid: str,
    frame_type: str,
    stored_name: str,
    display_name: str | None,
    *,
    data_dir: Path | None = None,
    cid_bind: str | None = None,
) -> dict | None:
    """把宿主已落盘的 bot 媒体文件注册进 db 与插件索引，返回消息 part。

    宿主发媒体帧时已把文件写入 ``data/attachments/<stored_name>`` 并**不注册
    db**（webchat_event.py:114-183 实源）；插件补注册（等同官方
    ``create_attachment_part_from_existing_file`` 行为，message_parts_helper.py:423-457）。

    Returns:
        ``{"type", "attachment_id", "filename"}`` part；文件缺失/注册失败返回
        None（不炸流）。
    """
    if not stored_name:
        return None
    from . import attachment_service

    try:
        return await attachment_service.register_host_attachment(
            context,
            frame_type,
            stored_name,
            display_name,
            data_dir=data_dir,
            cid=cid_bind or cid,
        )
    except Exception as exc:  # noqa: BLE001 —— 单帧注册失败不影响主流程
        logger.warning(f"[webui] bot 媒体帧注册失败（跳过）type={frame_type} name={stored_name}: {exc}")
        return None


async def save_bot_history(
    context: Context,
    cid: str,
    accumulator: BotMessageAccumulator,
    agent_stats: dict,
    llm_checkpoint_id: str | None,
):
    """bot 历史落库（宿主 ``save_bot_message`` 同款，chat_service.py:906-923 实源）。

    Returns:
        平台历史记录（含自增 id，供 done.message_id）；无内容或失败返回 None。
    """
    parts = accumulator.build_message_parts(include_pending_tool_calls=True)
    if not (accumulator.has_content() or agent_stats):
        return None
    content = build_bot_history_content(parts, agent_stats=agent_stats or None)
    try:
        return await message_history_manager(context).insert(
            platform_id=UMO_PLATFORM,
            user_id=cid,
            content=content,
            sender_id="bot",
            sender_name="bot",
            llm_checkpoint_id=llm_checkpoint_id,
        )
    except Exception as exc:  # noqa: BLE001 —— 落库失败不影响 SSE 终态
        logger.exception(f"[webui] bot 历史落库失败 cid={cid}: {exc}")
        return None


# ---------------------------------------------------------------------------
# 停止 / 级联删除 / 会话 CRUD 宿主侧原语
# ---------------------------------------------------------------------------


def request_stop(umo: str) -> int:
    """优雅中止某 umo 的全部活跃 Agent 运行（不 stop_event，部分回复照常落历史）。

    对标宿主 stop_session（chat_service.py:1393-1402 实源）。
    """
    return active_event_registry().request_agent_stop_all(umo)


async def get_platform_session(context: Context, cid: str):
    """按 cid 取 platform_session（不存在返回 None）。"""
    return await get_db(context).get_platform_session_by_id(cid)


async def count_platform_history(context: Context, cid: str) -> int:
    """平台历史条数（首条消息自动改题判定数据源，§0.2）。"""
    return await message_history_manager(context).count(UMO_PLATFORM, cid)


async def read_platform_history(context: Context, cid: str, page: int, page_size: int) -> list:
    """分页读平台历史（manager.get 已把页内反转成旧->新）。"""
    return await message_history_manager(context).get(
        platform_id=UMO_PLATFORM, user_id=cid, page=page, page_size=page_size
    )


async def create_platform_session(context: Context, cid: str) -> None:
    """platform_session 落库（creator 固定插件名，platform_id="webchat"，is_group=0）。"""
    await get_db(context).create_platform_session(
        creator=PLUGIN_CREATOR,
        platform_id=UMO_PLATFORM,
        session_id=cid,
        is_group=0,
    )


async def delete_platform_session(context: Context, cid: str) -> None:
    """platform_session 删除。"""
    await get_db(context).delete_platform_session(cid)


async def platform_sessions_by_creator(context: Context, page_size: int = LIST_PAGE_SIZE) -> list[dict]:
    """按 creator 取全部 platform_session（**显式传大 page_size**，默认 20 会截断）。"""
    return await get_db(context).get_platform_sessions_by_creator(
        PLUGIN_CREATOR,
        platform_id=UMO_PLATFORM,
        page=1,
        page_size=page_size,
    )


async def new_conversation(
    context: Context,
    umo: str,
    title: str | None,
    persona_id: str | None,
) -> str:
    """宿主新建 conversation（自动写 sel_conv_id，conversation_mgr.py:122-123 实源）。

    Returns:
        conv_id（宿主 uuid）——**与 cid 是双主键**。
    """
    return await conversation_manager(context).new_conversation(
        umo,
        platform_id=UMO_PLATFORM,
        title=title,
        persona_id=persona_id,
    )


async def curr_conversation_id(context: Context, umo: str) -> str | None:
    """解析 umo 当前 conversation_id（**conversation 侧操作一律经此解析 conv_id**，
    禁止把 cid 直接传给 conversation_id 参数——会静默 no-op，§0.2 双主键）。"""
    return await conversation_manager(context).get_curr_conversation_id(umo)


async def get_conversation_by_id(context: Context, umo: str, conv_id: str):
    """按 conv_id 取 Conversation PO（不存在返回 None）。"""
    return await conversation_manager(context).get_conversation(umo, conv_id)


async def update_conversation(
    context: Context,
    umo: str,
    conv_id: str | None,
    title: str | None = None,
    persona_id: str | None = None,
) -> None:
    """更新 conversation（title/persona_id；conv_id 缺省时宿主取当前会话）。"""
    await conversation_manager(context).update_conversation(
        umo,
        conversation_id=conv_id,
        title=title,
        persona_id=persona_id,
    )


async def delete_conversations_by_user_id(context: Context, umo: str) -> None:
    """删除 umo 名下全部 conversation（含级联回调）。"""
    await conversation_manager(context).delete_conversations_by_user_id(umo)


async def cascade_delete_session(context: Context, cid: str, data_dir: Path | None = None) -> None:
    """会话级联删除（§0.2；对标宿主 delete_session_internal :1415-1463 实源）。

    步骤（消费者任务由 chat_service.cancel_generation_tasks 先行取消）：
    ① request_agent_stop_all(umo)；② delete_conversations_by_user_id(umo)；
    ③ 提取平台历史中的 attachment_id -> 删 db 附件记录与磁盘文件；④ 平台历史
    删除（**显式 offset_sec=99999999**，宿主默认只删 24h）；⑤ remove_queues(cid)；
    ⑥ delete_platform_session；⑦ 清插件附件索引（调用方经 attachment_service）。

    Args:
        data_dir: 插件数据目录；传入时同步清理 bot 帧注册的插件索引条目。
    """
    umo = build_umo(cid)
    request_stop(umo)
    await delete_conversations_by_user_id(context, umo)

    # 先取历史提取附件 id（删历史前），再删历史
    attachment_ids: list[str] = []
    try:
        rows = await read_platform_history(context, cid, page=1, page_size=100000)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[webui] 级联删除读取平台历史失败（继续删历史）cid={cid}: {exc}")
        rows = []
    for row in rows:
        content = getattr(row, "content", None)
        if not isinstance(content, dict):
            continue
        for part in content.get("message") or []:
            if isinstance(part, dict) and part.get("attachment_id"):
                attachment_ids.append(str(part["attachment_id"]))

    await message_history_manager(context).delete(
        platform_id=UMO_PLATFORM,
        user_id=cid,
        offset_sec=HISTORY_DELETE_OFFSET_SEC,
    )
    webchat_queue_mgr().remove_queues(cid)
    await delete_platform_session(context, cid)

    # db 附件记录与磁盘文件（宿主 delete_attachments 同款，chat_service.py:1533-1553）
    db = get_db(context)
    for attachment_id in attachment_ids:
        try:
            attachment = await db.get_attachment_by_id(attachment_id)
            if attachment is not None:
                path = Path(str(getattr(attachment, "path", "")))
                if path.is_file():
                    path.unlink(missing_ok=True)
                await db.delete_attachment(attachment_id)
        except Exception as exc:  # noqa: BLE001 —— 单个附件清理失败不影响删除
            logger.warning(f"[webui] 级联删除附件失败 attachment_id={attachment_id}: {exc}")

    # 插件索引里 bot 帧注册（attachment_id 无 db 行可依）的条目按 cid 清理
    if data_dir is not None:
        try:
            from . import attachment_service

            await attachment_service.delete_attachments_by_ids(data_dir, attachment_ids)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[webui] 级联清理插件附件索引失败 cid={cid}: {exc}")
    logger.info(f"[webui] 会话宿主级联删除完成 cid={cid}")
