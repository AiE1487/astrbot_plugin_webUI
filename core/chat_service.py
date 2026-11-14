"""聊天服务：流式生成、任务表、abort、事件缓冲（M2）与附件/工具循环接线（M3）。

UserPlan §2/§3.3（拍板）+ 对接契约 §SSE/§REST：
- **直调 Provider** ``provider.text_chat_stream(...)``（流式唯一官方路径），自管
  人格/历史；不走 webchat 管道。**text_chat_stream 只出现在本模块**（单一适配
  层，R2；M3 工具循环经注入的 ``consume`` 闭包复用本模块的单轮消费）；
- ``gen_id = uuid4 hex``；任务表 ``{gen_id: {task, buffer, cid, ...}}``；
  ``terminate()`` 时全部先落终态再 cancel；
- 同会话 single-flight：发送前检查该 cid 是否有进行中任务，有则 409
  session_busy；全局并发上限实时读配置 ``max_concurrent_generations``，满则
  409 concurrency_limit；
- 流式来源映射（§3.2）：chunk 的 ``result_chain`` = 文本增量；chunk 的
  ``reasoning_content`` = 思考增量；末次 ``is_chunk=False`` 的完整响应含全量
  ``completion_text`` / ``reasoning_content`` / ``usage`` -> done 事件
  ``{message_id, usage}``；异常 -> error 事件 ``{fatal: true, code}``；
- **stop 必须先向缓冲写终态 stopped 再 task.cancel()**（否则流永不关、重订阅
  挂起）；未知/过期 gen_id -> 404（routes 映射）；
- 事件缓冲保留 >=5 分钟（``BUFFER_TTL_SECONDS``，终态后仍可全量重放）；
- 附件（M3）：图片路径直入 ``image_urls``（模型不支持在生成期抛错，无同步
  判定，v4.29.4 provider.py L174 docstring 实证）-> 捕获后发 SSE
  ``error{fatal:true, code:"model_not_multimodal"}``（错误分类为启发式，真机
  核实后收紧）；文本附件按 ``<attachment name=...>`` 区块**拼接进 prompt**
  （extra_user_content_parts 经 **kwargs 透传未证实，probe P4 探测项，落选
  方案接受历史污染，README 已注明）；
- 工具循环（M3）：方案 A/B 见 core/tool_loop.py；本模块注入单轮消费闭包与
  事件通道，降级阶梯 A->B->无工具单轮（非致命 SSE error）；
- 流式循环内不做阻塞 IO（富历史/权威历史写入在终态后进行，文件 IO 走
  ``asyncio.to_thread``）。
"""

import asyncio
import time
import uuid

from astrbot.api import logger
from astrbot.api.star import Star

from . import conversation_service, persona_service, sse, tool_loop, uistore

BUFFER_TTL_SECONDS = 300.0  # 缓冲保留 >=5 分钟（契约 §SSE）

# 任务表：{gen_id: {"gen_id", "cid", "task", "buffer", "waiters", "append_event",
# "finished", "finished_at", "last_seq"}}
_GENERATIONS: dict[str, dict] = {}

# 多模态不支持错误启发式关键词（真机核实后收紧；契约允许最终裁决在生成期）
_MULTIMODAL_HINTS = (
    "image",
    "multimodal",
    "vision",
    "visual",
    "图片",
    "图像",
    "not support",
    "unsupported",
    "invalid content",
    "content part",
)


class SendRejected(Exception):
    """发送/订阅被业务规则拒绝，routes 适配层映射为契约错误包。"""

    def __init__(self, status_code: int, slug: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.slug = slug
        self.message = message


class SessionBusyError(SendRejected):
    """同会话已有进行中生成任务（single-flight，409 session_busy）。"""

    def __init__(self, message: str = "该会话正在生成回复，请稍候再发送") -> None:
        super().__init__(409, "session_busy", message)


class ConcurrencyLimitError(SendRejected):
    """全局并发生成数已达上限（409 concurrency_limit）。"""

    def __init__(self, message: str = "并发生成数已达上限，请稍候再发送") -> None:
        super().__init__(409, "concurrency_limit", message)


class GenerationNotFoundError(SendRejected):
    """gen_id 不存在或缓冲已过期（404 not_found）。"""

    def __init__(self, message: str = "生成任务不存在或已过期") -> None:
        super().__init__(404, "not_found", message)


# ---------------------------------------------------------------------------
# 任务表维护
# ---------------------------------------------------------------------------


def _sweep_expired() -> None:
    """惰性清理已终态且超过缓冲保留期的任务条目。"""
    now = time.monotonic()
    for gen_id, entry in list(_GENERATIONS.items()):
        finished_at = entry["finished_at"]
        if entry["finished"] and finished_at is not None and now - finished_at > BUFFER_TTL_SECONDS:
            _GENERATIONS.pop(gen_id, None)


def _get_live_entry(gen_id: str) -> dict:
    """取存活条目；不存在或缓冲已过期抛 GenerationNotFoundError。"""
    _sweep_expired()
    entry = _GENERATIONS.get(gen_id)
    if entry is None:
        raise GenerationNotFoundError(f"生成任务不存在或缓冲已过期: {gen_id}")
    return entry


def generation_exists(gen_id: str) -> bool:
    """gen_id 是否存活（stream 订阅前置校验，未知/过期 -> routes 404）。"""
    try:
        _get_live_entry(gen_id)
        return True
    except GenerationNotFoundError:
        return False


def _concurrency_limit(plugin: Star) -> int:
    """实时读取全局并发上限配置（键由 _conf_schema.json 生成恒存在，热生效）。"""
    return max(1, int(plugin.config["max_concurrent_generations"]))


def _tool_loop_mode(plugin: Star) -> str:
    """实时读取工具循环模式（stream=方案 A / loop_agent=方案 B；非法值回 schema 默认）。"""
    mode = str(plugin.config["tool_loop_mode"])
    return mode if mode in ("stream", "loop_agent") else tool_loop.TOOL_LOOP_MODE


def _active_count() -> int:
    """当前进行中（未终态）的生成任务数。"""
    return sum(1 for entry in _GENERATIONS.values() if not entry["finished"])


def active_generation_id(cid: str) -> str | None:
    """查询某会话是否有进行中的生成任务（single-flight 检查用）。

    Returns:
        进行中的 gen_id；无则 ``None``。
    """
    _sweep_expired()
    for entry in _GENERATIONS.values():
        if entry["cid"] == cid and not entry["finished"]:
            return entry["gen_id"]
    return None


def _notify(entry: dict) -> None:
    """唤醒所有 SSE 订阅等待者（广播；Event 一对一会丢唤醒，故用 waiter 集合）。"""
    for waiter in list(entry["waiters"]):
        waiter.set()


def _append_event(entry: dict, event: str, data: dict) -> dict:
    """落缓冲并广播；返回带 seq 的事件数据（SSE 响应体）。"""
    body = sse.buffer_append(entry["buffer"], event, data)
    entry["last_seq"] = body["seq"]
    _notify(entry)
    return body


def _write_terminal(entry: dict, event_type: str, data: dict) -> dict | None:
    """写入终态事件（幂等：首个终态生效）；记录保留期起点并广播。"""
    if entry["finished"]:
        return None
    body = _append_event(entry, event_type, data)
    entry["finished"] = True
    entry["finished_at"] = time.monotonic()
    return body


# ---------------------------------------------------------------------------
# 发送 / 中止 / 订阅
# ---------------------------------------------------------------------------


async def start_generation(
    plugin: Star,
    cid: str,
    text: str,
    image_urls: list[str] | None = None,
    attachment_blocks: list[str] | None = None,
    attachment_meta: list[dict] | None = None,
) -> str:
    """创建并登记一次流式生成任务（POST send 的实现体）。

    Args:
        plugin: 插件实例（取 context/provider 与 config）。
        cid: 会话 id。
        text: 用户文本（可与附件二选一非空）。
        image_urls: 图片本地路径列表（attachment_service 落盘后的绝对路径）。
        attachment_blocks: 文本附件区块（``<attachment ...>``，拼进 prompt）。
        attachment_meta: 契约附件元数据列表（写入富历史 user 消息 attachments）。

    Returns:
        gen_id（uuid4 hex）。

    Raises:
        ConversationNotFoundError: 会话不存在（-> 404）。
        SessionBusyError: 该会话已有进行中任务（-> 409 session_busy）。
        ConcurrencyLimitError: 超出全局并发上限（-> 409 concurrency_limit）。
    """
    _sweep_expired()
    context = plugin.context
    data_dir = plugin.data_dir()

    # —— 同步段：检查与占位登记之间**无任何 await**，对事件循环原子（TOCTOU 修复）。
    # 并发 send 到达时第二条必然看到第一条的占位条目，409 single-flight 与全局并发
    # 上限在并发下不再失效；会话存在性等需要 await 的校验移到占位之后，失败时摘除
    # 占位条目（gen_id 尚未返回给客户端，摘除无副作用）。
    if active_generation_id(cid) is not None:
        raise SessionBusyError()
    if _active_count() >= _concurrency_limit(plugin):
        raise ConcurrencyLimitError()

    gen_id = uuid.uuid4().hex
    entry: dict = {
        "gen_id": gen_id,
        "cid": cid,
        "task": None,
        "buffer": sse.new_buffer(),
        "waiters": set(),
        "append_event": lambda event, data: _append_event(entry, event, data),
        "finished": False,
        "finished_at": None,
        "last_seq": -1,
    }
    _GENERATIONS[gen_id] = entry  # 同步占位：此后该 cid 立即处于 single-flight 保护下
    try:
        conv = await conversation_service.get_conversation(context, cid)
        if conv is None:
            raise conversation_service.ConversationNotFoundError(f"会话不存在: {cid}")

        umo = conversation_service.build_umo(cid)
        persona = await persona_service.get_persona_for_conversation(context, umo, conv.persona_id)
        contexts = await conversation_service.build_contexts(context, cid)

        # 首条用户消息后自动改题（update_conversation_title 已废弃，改用 update_conversation）
        if text.strip() and not await uistore.has_user_message(data_dir, cid):
            try:
                await conversation_service.rename_conversation(context, cid, text)
            except Exception as exc:  # noqa: BLE001 —— 改题失败不阻断发送
                logger.warning(f"[webui] 自动改题失败（不阻断发送）cid={cid}: {exc}")

        # 富历史先落用户消息（与生成成败无关，刷新后仍在；附件元数据同构契约）
        await uistore.append_message(
            data_dir,
            cid,
            {
                "id": uistore.new_message_id(),
                "role": "user",
                "ts": uistore.now_iso(),
                "text": text,
                "attachments": list(attachment_meta or []),
            },
        )

        entry["task"] = asyncio.create_task(
            _run_generation(
                plugin, entry, cid, text, image_urls or None, attachment_blocks or None,
                attachment_meta or None, persona, contexts,
            )
        )
    except BaseException:
        # 校验失败/取消：摘除占位条目（task 尚未创建，无事件无订阅者）
        if entry["task"] is None:
            _GENERATIONS.pop(gen_id, None)
        raise
    logger.debug(f"[webui] 生成任务已登记 gen_id={gen_id} cid={cid}")
    return gen_id


async def _consume_round(
    provider,
    entry: dict,
    *,
    prompt: str,
    image_urls: list[str] | None,
    contexts: list[dict] | None,
    system_prompt: str | None,
    func_tool=None,
    tool_calls_result=None,
    text_parts: list[str] | None = None,
    reasoning_parts: list[str] | None = None,
):
    """消费一轮 ``text_chat_stream``（**全插件唯一调用点**），产增量事件。

    Args:
        text_parts / reasoning_parts: 跨轮累加器（中止时拼接部分回复用）。

    Returns:
        末次完整响应（``is_chunk=False`` 的 LLMResponse）；缺失返回 None。
    """
    agen = provider.text_chat_stream(
        prompt=prompt or None,
        image_urls=image_urls or None,
        contexts=contexts or None,
        system_prompt=system_prompt,
        func_tool=func_tool,
        tool_calls_result=tool_calls_result,
    )
    final_resp = None
    try:
        async for chunk in agen:
            if bool(getattr(chunk, "is_chunk", False)):
                delta = _chain_text(getattr(chunk, "result_chain", None))
                if delta:
                    if text_parts is not None:
                        text_parts.append(delta)
                    _append_event(entry, "text_delta", {"delta": delta})
                reasoning = getattr(chunk, "reasoning_content", None)
                if reasoning:
                    if reasoning_parts is not None:
                        reasoning_parts.append(reasoning)
                    _append_event(entry, "reasoning_delta", {"delta": reasoning})
            else:
                final_resp = chunk  # 末次完整响应（全量文本/usage/reasoning/工具调用）
    finally:
        aclose = getattr(agen, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001 —— 清理失败不影响主流程
                pass
    return final_resp


async def _run_generation(
    plugin: Star,
    entry: dict,
    cid: str,
    text: str,
    image_urls: list[str] | None,
    attachment_blocks: list[str] | None,
    attachment_meta: list[dict] | None,
    persona: dict | None,
    contexts: list[dict],
) -> None:
    """生成任务体：消费 text_chat_stream -> SSE 事件 -> 终态 -> 历史双写。"""
    gen_id = entry["gen_id"]
    context = plugin.context
    data_dir = plugin.data_dir()
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_steps: list[dict] = []
    try:
        provider = context.get_using_provider(umo=conversation_service.build_umo(cid))
        if asyncio.iscoroutine(provider):
            provider = await provider
        if provider is None or not hasattr(provider, "text_chat_stream"):
            raise RuntimeError("当前未配置可用的聊天模型提供商（provider）")

        system_prompt = (persona or {}).get("prompt") or None
        prompt = _build_prompt(text, attachment_blocks)
        req_contexts = _with_begin_dialogs(persona, contexts)

        consumed_final: object | None = None
        toolset, tool_objs = tool_loop.build_toolset(plugin, persona)
        if toolset is not None:
            mode = _tool_loop_mode(plugin)

            async def consume(**kwargs):
                return await _consume_round(
                    provider, entry, prompt=prompt, image_urls=image_urls,
                    contexts=req_contexts, system_prompt=system_prompt,
                    text_parts=text_parts, reasoning_parts=reasoning_parts, **kwargs,
                )

            try:
                if mode == "loop_agent":
                    consumed_final, tool_steps = await tool_loop.run_agent_loop(
                        plugin, entry, umo=conversation_service.build_umo(cid),
                        prompt=prompt, system_prompt=system_prompt,
                        contexts=req_contexts, image_urls=image_urls, toolset=toolset,
                    )
                else:
                    consumed_final, tool_steps = await tool_loop.run_stream_loop(
                        plugin, entry, consume=consume, toolset=toolset, tool_objs=tool_objs,
                    )
            except tool_loop.ToolLoopUnavailable as exc:
                if mode != "loop_agent":
                    # 降级阶梯：方案 A 不可用 -> 方案 B -> 无工具单轮
                    logger.warning(f"[webui] 方案 A 工具循环不可用，尝试降级方案 B: {exc}")
                    _append_event(
                        entry, "error",
                        {"message": "流式工具循环不可用，改用宿主逐步执行（正文非流式）", "fatal": False},
                    )
                    try:
                        consumed_final, tool_steps = await tool_loop.run_agent_loop(
                            plugin, entry, umo=conversation_service.build_umo(cid),
                            prompt=prompt, system_prompt=system_prompt,
                            contexts=req_contexts, image_urls=image_urls, toolset=toolset,
                        )
                    except tool_loop.ToolLoopUnavailable as exc2:
                        logger.warning(f"[webui] 方案 B 亦不可用，降级无工具单轮: {exc2}")
                        _append_event(
                            entry, "error",
                            {"message": "工具调用暂不可用，已跳过工具继续生成", "fatal": False},
                        )
                        consumed_final = await consume(func_tool=None, tool_calls_result=None)
                else:
                    logger.warning(f"[webui] 方案 B 工具循环不可用，降级无工具单轮: {exc}")
                    _append_event(
                        entry, "error",
                        {"message": "工具调用暂不可用，已跳过工具继续生成", "fatal": False},
                    )
                    consumed_final = await consume(func_tool=None, tool_calls_result=None)
        else:
            consumed_final = await _consume_round(
                provider, entry, prompt=prompt, image_urls=image_urls,
                contexts=req_contexts, system_prompt=system_prompt,
                text_parts=text_parts, reasoning_parts=reasoning_parts,
            )

        completion = ""
        reasoning_full = ""
        signature = None
        usage_raw = None
        if consumed_final is not None:
            completion = getattr(consumed_final, "completion_text", None) or ""
            reasoning_full = getattr(consumed_final, "reasoning_content", None) or ""
            signature = getattr(consumed_final, "reasoning_signature", None)
            usage_raw = getattr(consumed_final, "usage", None)
        if not completion:
            completion = "".join(text_parts)
        if not reasoning_full:
            reasoning_full = "".join(reasoning_parts)
        usage = _normalize_usage(usage_raw)

        message_id = uistore.new_message_id()
        assistant_msg: dict = {
            "id": message_id,
            "role": "assistant",
            "ts": uistore.now_iso(),
            "text": completion,
            "usage": usage,
        }
        if reasoning_full:
            reasoning_block = {"content": reasoning_full}
            if signature:
                reasoning_block["signature"] = signature
            assistant_msg["reasoning"] = reasoning_block
        if tool_steps:
            assistant_msg["tools"] = tool_steps  # 富历史工具步骤（契约 §富历史）
        # 历史先落盘、后发 done：done 即「已持久化」，前端刷新立即可见
        await uistore.append_message(data_dir, cid, assistant_msg)
        try:
            await conversation_service.append_history_pair(context, cid, text, completion)
        except Exception:  # noqa: BLE001 —— 权威历史失败不影响 SSE 终态
            logger.exception(f"[webui] 权威历史写入失败 gen_id={gen_id} cid={cid}")

        _write_terminal(entry, "done", {"message_id": message_id, "usage": usage})
    except asyncio.CancelledError:
        # stop 已先写终态；terminate/其他取消路径在此补终态（幂等）。
        _write_terminal(entry, "stopped", {"message": "已手动停止"})
        partial = "".join(text_parts)
        if partial:
            try:
                partial_msg: dict = {
                    "id": uistore.new_message_id(),
                    "role": "assistant",
                    "ts": uistore.now_iso(),
                    "text": partial,
                    "stopped": True,
                }
                if tool_steps:
                    partial_msg["tools"] = tool_steps
                await uistore.append_message(data_dir, cid, partial_msg)
                await conversation_service.append_history_pair(context, cid, text, partial)
            except Exception:  # noqa: BLE001
                logger.exception(f"[webui] 中止后的历史写入失败 gen_id={gen_id} cid={cid}")
        raise
    except Exception as exc:  # noqa: BLE001 —— 生成异常 -> error 终态
        logger.exception(f"[webui] 生成任务异常 gen_id={gen_id} cid={cid}")
        message = str(exc) or type(exc).__name__
        code = _classify_error(exc, bool(image_urls))
        _write_terminal(
            entry,
            "error",
            {
                "message": f"生成失败：{message}",
                "fatal": True,
                "code": code,
            },
        )
        try:
            await uistore.append_message(
                data_dir,
                cid,
                {
                    "id": uistore.new_message_id(),
                    "role": "assistant",
                    "ts": uistore.now_iso(),
                    "text": "",
                    "error": message,
                    # 失败分类 slug 随占位持久化：刷新后前端可恢复专属文案与失败标记
                    # （评审修复；契约富历史消息节的附加可选键，前端按可选字段消费）
                    "error_code": code,
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception(f"[webui] 失败占位消息写入失败 gen_id={gen_id} cid={cid}")
    finally:
        # 防御：任何路径漏写终态时补一个（正常路径 finished 已为 True，幂等跳过）
        if not entry["finished"]:
            _write_terminal(
                entry,
                "error",
                {"message": "生成异常终止", "fatal": True, "code": "server_error"},
            )


def _build_prompt(text: str, attachment_blocks: list[str] | None) -> str:
    """拼装本轮 prompt：用户文本 + 文本附件区块（文本为空时仅附件区块）。"""
    joined = "\n\n".join(attachment_blocks or [])
    if text.strip() and joined:
        return f"{text}\n\n{joined}"
    return text or joined


def _classify_error(exc: Exception, had_images: bool) -> str:
    """生成期错误分类（启发式；契约 slug，真机核实后收紧）。"""
    if had_images:
        lowered = f"{type(exc).__name__} {exc}".lower()
        if any(hint in lowered for hint in _MULTIMODAL_HINTS):
            return "model_not_multimodal"
    return "server_error"


def _with_begin_dialogs(persona: dict | None, contexts: list[dict]) -> list[dict]:
    """人格 begin_dialogs 预置对话拼在权威历史前（与宿主 agent 行为一致）。"""
    dialogs = (persona or {}).get("_begin_dialogs_processed") or []
    prefix = [
        {"role": d.get("role", "user"), "content": d.get("content", "")}
        for d in dialogs
        if isinstance(d, dict)
    ]
    return prefix + list(contexts or [])


def _chain_text(chain) -> str:
    """从 result_chain 提取本次文本增量（优先 MessageChain.get_plain_text）。"""
    if chain is None:
        return ""
    get_plain = getattr(chain, "get_plain_text", None)
    if callable(get_plain):
        try:
            return get_plain() or ""
        except Exception:  # noqa: BLE001
            pass
    try:
        return "".join(getattr(part, "text", "") or "" for part in chain)
    except TypeError:
        return ""


def _normalize_usage(usage) -> dict:
    """归一 usage 为契约形状 ``{"output": int, "total": int}``（缺失兜底 0）。"""
    usage = usage if isinstance(usage, dict) else {}
    output = usage.get("output")
    if output is None:
        output = usage.get("completion_tokens") or 0
    total = usage.get("total")
    if total is None:
        input_tokens = usage.get("input")
        if input_tokens is None:
            input_tokens = usage.get("prompt_tokens") or 0
        total = int(input_tokens or 0) + int(output or 0)
    try:
        return {"output": int(output or 0), "total": int(total or 0)}
    except (TypeError, ValueError):
        return {"output": 0, "total": 0}


async def abort_generation(gen_id: str, wait: bool = True) -> bool:
    """中止生成任务（POST stop/<gen_id>）。

    契约：**先向缓冲写终态 stopped 再 task.cancel()**（否则流永不关、重订阅
    挂起）；gen_id 不存在或已结束返回 False（routes 映射 404）。

    Args:
        wait: 默认 True——cancel 后等待任务收尾（吸收 CancelledError）再返回，
            保证中止分支的部分回复/权威历史写入先于调用方的后续操作（如删除
            会话的清理），杜绝「删除先行、中止分支随后写回」复活已删数据；
            SSE 终态事件在 cancel 前已入缓冲，等待不影响订阅端及时收帧。
    """
    try:
        entry = _get_live_entry(gen_id)
    except GenerationNotFoundError:
        return False
    if entry["finished"]:
        return False
    _write_terminal(entry, "stopped", {"message": "已手动停止"})
    task = entry["task"]
    if task is not None and not task.done():
        task.cancel()
        if wait:
            await asyncio.gather(task, return_exceptions=True)
    elif wait and task is not None:
        await asyncio.gather(task, return_exceptions=True)
    return True


async def replay_events(gen_id: str, after_seq: int = 0) -> list[dict]:
    """取某次生成 seq > after_seq 的全部缓冲事件（SSE 订阅前重放，§3.2）。

    重复调用（幂等）返回相同结果；gen_id 未知/过期抛 GenerationNotFoundError。
    """
    entry = _get_live_entry(gen_id)
    return sse.buffer_replay(entry["buffer"], after_seq)


async def iter_events(gen_id: str, after_seq: int = 0):
    """异步生成器：先重放 seq > after_seq 的缓冲，再持续产出新事件（SSE handler 用）。

    每个订阅者持有一个独立 waiter（同一 gen_id 可重复订阅）；终态事件产出后
    结束。订阅者断开（生成器被关闭）只影响自身，不影响生成任务。

    Yields:
        缓冲条目 ``{"event": <类型>, "data": {含 seq 的事件数据}}``。
    """
    entry = _get_live_entry(gen_id)
    waiter = asyncio.Event()
    entry["waiters"].add(waiter)
    cursor = after_seq
    try:
        while True:
            for item in sse.buffer_replay(entry["buffer"], cursor):
                cursor = item["data"]["seq"]
                yield item
            if entry["finished"]:
                return
            await waiter.wait()
            waiter.clear()
    finally:
        entry["waiters"].discard(waiter)


async def shutdown() -> None:
    """插件 terminate 清理：全部任务先落终态 stopped 再 cancel，并等待收尾。

    缓冲条目保留至 TTL（同进程重载前仍可重放）；重载会重新导入本模块，
    任务表随之重置。
    """
    for entry in list(_GENERATIONS.values()):
        if not entry["finished"]:
            _write_terminal(entry, "stopped", {"message": "已手动停止"})
    tasks = [
        entry["task"]
        for entry in list(_GENERATIONS.values())
        if entry["task"] is not None and not entry["task"].done()
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    logger.debug(f"[webui] 生成任务清理完成，共 {len(tasks)} 个任务被取消")
