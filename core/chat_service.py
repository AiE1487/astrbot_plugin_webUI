"""聊天服务：流式生成任务表、SSE 事件缓冲、中止与生命周期（v0.2 生成内核）。

契约（开发计划 §0.2 + 对接契约 §SSE/§REST）：
- **生成内核 = webchat 队列注入 + back_queue 消费**（旧直调路径已废止）：
  拼消息 parts（文本 plain + 媒体 attachment_id）-> pipeline_bridge 注入 ->
  排水翻译 back_queue 载荷 -> ``_append_event`` 进 SSE 缓冲；会话管理、请求
  拼接（人格/历史）、工具注入全部归还宿主管道（工具/记忆全生效）；
- ``gen_id = uuid4 hex``；任务表 ``{gen_id: {task, buffer, cid, ...}}``；
  ``terminate()`` 时全部先落终态再 cancel；
- 同会话 single-flight：发送前检查该 cid 是否有进行中任务，有则 409
  session_busy；全局并发上限实时读配置 ``max_concurrent_generations``，满则
  409 concurrency_limit；
- SSE 事件（契约不变）：reasoning_delta/text_delta/tool_call/tool_result/
  done{message_id,usage}/stopped/error；seq 从 0 单调、订阅即全量重放；
- **stop 先落 stopped 终态**再 ``request_agent_stop_all(umo)`` 优雅中止，
  **不再 cancel 消费任务**（等 end 帧自然收尾，部分回复照常落平台历史）；
  未知/过期 gen_id -> 404（routes 映射）；
- 每个 gen_id 的 finally **必须** ``remove_back_queue(gen_id)``（队列泄漏纪律）；
- 排水循环的空闲超时兜底（120s）在 pipeline_bridge.consume_generation 内实现；
- LLM 错误语义与官方 WebChat 一致：provider 错误以 bot 文本透传，插件侧仅对
  传输层异常（注入失败/排水崩溃/空闲超时）写 error 终态；
- 首条消息自动改题判定：平台历史 ``count(platform_id, user_id)==0``；
- 流式循环内不做阻塞 IO（历史落库经宿主 db）。
"""

import asyncio
import time
import uuid

from astrbot.api import logger
from astrbot.api.star import Star

from . import conversation_service, pipeline_bridge, sse

BUFFER_TTL_SECONDS = 300.0  # 缓冲保留 >=5 分钟（契约 §SSE）

# 任务表：{gen_id: {"gen_id", "cid", "task", "buffer", "waiters", "append_event",
# "finished", "finished_at", "last_seq"}}
_GENERATIONS: dict[str, dict] = {}


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


def _make_emit(entry: dict):
    """构造排水翻译回调：业务事件进缓冲，终态事件幂等落终态标记。"""

    def _emit(event: str, data: dict) -> None:
        if event in ("done", "stopped", "error"):
            _write_terminal(entry, event, data)
        else:
            _append_event(entry, event, data)

    return _emit


# ---------------------------------------------------------------------------
# 发送 / 中止 / 订阅
# ---------------------------------------------------------------------------


async def start_generation(
    plugin: Star,
    cid: str,
    text: str,
    media_parts: list[dict] | None = None,
    attachment_metas: list[dict] | None = None,
) -> str:
    """创建并登记一次流式生成任务（POST send 的实现体）。

    Args:
        plugin: 插件实例（取 context 与 config）。
        cid: 会话 id（= 宿主 platform_sessions.session_id）。
        text: 用户文本（可与附件二选一非空）。
        media_parts: 媒体 parts（routes 按附件索引解析，含 attachment_id/path）。
        attachment_metas: 契约附件元数据（保留参数位，历史 attachments 反查
            经插件索引进行，不再单独落库）。

    Returns:
        gen_id（uuid4 hex）。

    Raises:
        SendRejected: 会话不存在（-> 404）/ session_busy（-> 409）/
            concurrency_limit（-> 409）。
    """
    _sweep_expired()
    context = plugin.context
    data_dir = plugin.data_dir()

    # —— 同步段：检查与占位登记之间**无任何 await**，对事件循环原子（TOCTOU 修复）。
    # 并发 send 到达时第二条必然看到第一条的占位条目；会话存在性等需要 await 的
    # 校验移到占位之后，失败时摘除占位条目（gen_id 尚未返回给客户端，摘除无副作用）。
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
        session = await pipeline_bridge.get_platform_session(context, cid)
        if session is None or getattr(session, "creator", "") != pipeline_bridge.PLUGIN_CREATOR:
            raise SendRejected(404, "not_found", f"会话不存在: {cid}")

        # 首条消息自动改题：判定数据源 = 平台历史 count==0（§0.2，注入前判定）
        try:
            if text.strip() and await pipeline_bridge.count_platform_history(context, cid) == 0:
                await conversation_service.rename_conversation(context, cid, text)
        except Exception as exc:  # noqa: BLE001 —— 改题失败不阻断发送
            logger.warning(f"[webui] 自动改题失败（不阻断发送）cid={cid}: {exc}")

        entry["task"] = asyncio.create_task(
            _run_generation(plugin, entry, cid, text, media_parts or [], data_dir)
        )
    except BaseException:
        # 校验失败/取消：摘除占位条目（task 尚未创建，无事件无订阅者）
        if entry["task"] is None:
            _GENERATIONS.pop(gen_id, None)
        raise
    logger.debug(f"[webui] 生成任务已登记 gen_id={gen_id} cid={cid}")
    return gen_id


async def _run_generation(
    plugin: Star,
    entry: dict,
    cid: str,
    text: str,
    media_parts: list[dict],
    data_dir,
) -> None:
    """生成任务体：拼 parts -> webchat 注入 -> 排水翻译 -> 终态。

    部分回复/异常历史由宿主 runner 与 pipeline_bridge.save_bot_history 负责，
    本函数只管任务生命周期与 SSE 终态。
    """
    gen_id = entry["gen_id"]
    emit = _make_emit(entry)
    try:
        # 拼 parts：文本 plain + 媒体 attachment parts（宿主 build_webchat_message_parts
        # 按 attachment_id 解析；path 供适配器定位文件，落库前由 bridge 剥除）
        parts: list[dict] = []
        if text.strip():
            parts.append({"type": "plain", "text": text})
        parts.extend(part for part in media_parts if isinstance(part, dict))

        injection = await pipeline_bridge.inject_user_message(
            plugin.context, cid, parts, gen_id
        )
        await pipeline_bridge.consume_generation(
            plugin.context,
            cid,
            gen_id,
            injection["back_queue"],
            injection["llm_checkpoint_id"],
            emit,
            data_dir=data_dir,
            cid_bind=cid,
        )
    except asyncio.CancelledError:
        # 仅插件 terminate / 级联删除会取消消费任务（POST stop 不再取消）：
        # stop 端点已先写终态；此处补终态（幂等）后上抛。
        _write_terminal(entry, "stopped", {"message": "已手动停止"})
        raise
    except pipeline_bridge.PipelineBridgeError as exc:
        logger.exception(f"[webui] 管道注入失败 gen_id={gen_id} cid={cid}")
        _write_terminal(
            entry,
            "error",
            {"message": f"生成失败：{exc}", "fatal": True, "code": "server_error"},
        )
    except Exception as exc:  # noqa: BLE001 —— 生成异常 -> error 终态
        logger.exception(f"[webui] 生成任务异常 gen_id={gen_id} cid={cid}")
        message = str(exc) or type(exc).__name__
        _write_terminal(
            entry,
            "error",
            {"message": f"生成失败：{message}", "fatal": True, "code": "server_error"},
        )
    finally:
        # 队列泄漏纪律：每个 gen_id 的 finally 必须 remove_back_queue（幂等；
        # consume_generation 内部已清理，此处覆盖注入中途失败的路径）
        pipeline_bridge.remove_back_queue(gen_id)
        # 防御：任何路径漏写终态时补一个（正常路径 finished 已为 True，幂等跳过）
        if not entry["finished"]:
            _write_terminal(
                entry,
                "error",
                {"message": "生成异常终止", "fatal": True, "code": "server_error"},
            )


async def abort_generation(gen_id: str, wait: bool = False) -> bool:
    """中止生成任务（POST stop/<gen_id>）。

    契约：**先向缓冲写终态 stopped**，再 ``request_agent_stop_all(umo)`` 优雅
    中止（runner 保存部分历史、管道仍发 end 帧）；**不再 cancel 消费任务**，
    排水循环继续到 end 帧后自然退出并 remove_back_queue；空闲超时兜底在
    pipeline_bridge（120s 无载荷 -> error 终态并清理）。
    gen_id 不存在或已结束返回 False（routes 映射 404）。

    Args:
        wait: 兼容保留参数（级联删除应改用 ``cancel_generation_tasks``）。
    """
    try:
        entry = _get_live_entry(gen_id)
    except GenerationNotFoundError:
        return False
    if entry["finished"]:
        return False
    _write_terminal(entry, "stopped", {"message": "已手动停止"})
    try:
        pipeline_bridge.request_stop(pipeline_bridge.build_umo(entry["cid"]))
    except pipeline_bridge.PipelineBridgeError as exc:
        logger.warning(f"[webui] 优雅中止请求失败（任务仍会按超时兜底收尾）gen_id={gen_id}: {exc}")
    return True


async def cancel_generation_tasks(cid: str, wait: bool = True) -> None:
    """取消某会话的全部消费任务（级联删除专用，POST stop 不走此路径）。

    先落终态 stopped 再 cancel，避免删除后排水任务又向平台历史写回数据。
    """
    _sweep_expired()
    tasks: list[asyncio.Task] = []
    for entry in list(_GENERATIONS.values()):
        if entry["cid"] != cid:
            continue
        if not entry["finished"]:
            _write_terminal(entry, "stopped", {"message": "会话已删除"})
        task = entry["task"]
        if task is not None and not task.done():
            task.cancel()
            tasks.append(task)
    if wait and tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


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
    任务表随之重置。back_queue 清理由各任务的 finally（remove_back_queue）
    保证，不漏队列。
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
