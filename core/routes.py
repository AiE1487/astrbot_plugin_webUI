"""REST/SSE 路由表：注册到 Dashboard 的插件 API（内嵌视图与独立端口共用）。

端点清单（对接契约 §REST；路径省略插件名前缀，实际挂载于
``/api/v1/plugins/extensions/astrbot_plugin_webui/<route>``，经
``context.register_web_api(route, handler, methods, desc)`` 注册；独立端口页
（standalone.py）挂同路径的同款 handler）::

    GET    health                    连通性探测
    GET    personas                  人格列表（id、名称、system_prompt 摘要、工具、默认）
    GET    conversations             会话列表（cid、title、persona_id、updated_at、busy）
    POST   conversations             新建 {persona_id, title?} -> {cid}
    DELETE conversations/<cid>       删除会话（权威历史 + 富历史 + 附件级联清理）
    POST   conversations/<cid>/delete  同上（bridge SDK 无 DELETE 方法的等价别名）
    GET    history/<cid>             UI 富历史分页 ?page=&size=（page=1 最新一页）
    POST   send                      {cid, text, file_ids[]} -> {gen_id}；409 见契约
    POST   stop/<gen_id>             中止生成（先落终态 stopped 再 task.cancel()）
    GET    stream/<gen_id>           SSE（bridge.subscribeSSE；seq=0 全量重放 -> 实时）
    POST   upload                    multipart 单文件 -> 附件元数据；413/415 见契约
    GET    files/<file_id>           原文件（file_response，经索引定位）

⚠ 逐端点独立注册**精确路径**，禁止单一 ``<route>`` 通配：宿主把动态段
``<name>`` 编译为单段 ``(?P<name>[^/]+)``，只有 ``<path:name>`` 才跨段
（v4.29.4 dashboard/api/plugins.py L196-205 实证），通配会令多级端点全 404
且桩测/mock 测不出。

错误出口：所有非 2xx 响应经 ``api_error`` 统一产出契约错误包
``{"code": <状态码 int>, "error": <slug>, "message": <人话>}``。宿主
``astrbot.api.web.error_response`` 的实际包裹形状是
``{"status": "error", "message", "data"}``（v4.29.4 api/web.py L366-388 实证），
与本契约不同，故适配层用 ``json_response`` 直出契约形状；真机核对如发现
包裹/状态码透传差异，只改适配层、不改契约语义（M4 工单收口）。

薄封装契约 = 参数校验 -> 调 core service -> 用 astrbot.api.web 响应封装返回
（业务逻辑不直接触碰 Quart/FastAPI 请求对象，统一走 ``astrbot.api.web.request``）。
"""

import asyncio
import functools
from pathlib import Path

from astrbot.api import logger
from astrbot.api.star import Star
from astrbot.api.web import (
    PluginUploadFile,
    file_response,
    json_response,
    request,
    stream_response,
)

from . import attachment_service, chat_service, conversation_service, persona_service, sse, uistore

PLUGIN_NAME = "astrbot_plugin_webui"

HISTORY_DEFAULT_PAGE_SIZE = 30
HISTORY_MAX_PAGE_SIZE = 100
SSE_PING_INTERVAL_SECONDS = 15.0  # SSE 保活 ping 间隔（空闲超时发 ping 帧防中间层掐断）

# slug 表（对接契约 §错误包）
_ERROR_SLUGS: dict[int, str] = {
    400: "invalid_request",
    401: "unauthorized",
    404: "not_found",
    409: "session_busy",
    413: "payload_too_large",
    415: "unsupported_media_type",
    500: "server_error",
}
_ERROR_MESSAGES: dict[int, str] = {
    400: "请求参数不合法",
    401: "未登录或令牌无效",
    404: "资源不存在",
    409: "请求冲突",
    413: "上传内容超出大小限制",
    415: "不支持的媒体类型",
    500: "服务器内部错误",
}


def api_error(status_code: int, message: str | None = None, slug: str | None = None):
    """统一错误出口适配层：产出契约错误包 + HTTP 状态码。

    Args:
        status_code: HTTP 状态码（同时作为包内 ``code`` 字段）。
        message: 人话描述；缺省用状态码默认文案。
        slug: 错误 slug；缺省按状态码取表（409 需显式传 session_busy /
            concurrency_limit 之一）。

    Returns:
        ``json_response({"code", "error", "message"}, status_code=...)``。
    """
    return json_response(
        {
            "code": status_code,
            "error": slug or _ERROR_SLUGS.get(status_code, "server_error"),
            "message": message or _ERROR_MESSAGES.get(status_code, "服务器内部错误"),
        },
        status_code=status_code,
    )


def _endpoint(func):
    """薄 handler 公共包装：业务拒绝 -> 契约错误包；意外异常 -> 500 错误包。"""

    @functools.wraps(func)
    async def wrapper(plugin: Star, *args, **kwargs):
        try:
            return await func(plugin, *args, **kwargs)
        except chat_service.SendRejected as exc:
            return api_error(exc.status_code, exc.message, exc.slug)
        except conversation_service.ConversationNotFoundError as exc:
            return api_error(404, str(exc), "not_found")
        except Exception:  # noqa: BLE001 —— 兜底 500，不让异常裸抛给宿主
            logger.exception(f"[webui] {func.__name__} 处理异常")
            return api_error(500)

    return wrapper


async def _json_body() -> dict:
    """读取 JSON 请求体；非 dict 抛 400 invalid_request。"""
    body = await request.json(default=None)
    if not isinstance(body, dict):
        raise chat_service.SendRejected(400, "invalid_request", "请求体不是合法 JSON 对象")
    return body


def _query_int(query, key: str, default: int) -> int:
    """读查询参数 int（转换失败/缺省回默认值）。"""
    try:
        value = query.get(key, default, type=int)
    except Exception:  # noqa: BLE001 —— 桩/宿主 query 实现差异防御
        return default
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _read_int_config(config: dict, key: str) -> int:
    """实时读配置 int（键由 _conf_schema.json 生成恒存在；默认值单一来源 = schema）。"""
    return max(1, int(config[key]))


async def _plugin_version(plugin: Star) -> str:
    """插件版本：优先宿主 StarMetadata，兜底读插件目录 metadata.yaml（IO 走线程）。"""
    try:
        meta = plugin.context.get_registered_star(getattr(plugin, "name", "") or PLUGIN_NAME)
        version = getattr(meta, "version", None)
        if version:
            return str(version)
    except Exception:  # noqa: BLE001
        pass
    try:
        import yaml

        meta_path = Path(__file__).resolve().parent.parent / "metadata.yaml"
        data = await asyncio.to_thread(
            lambda: yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
        )
        return str(data.get("version") or "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


def register_routes(plugin: Star) -> None:
    """把端点清单全部注册到 ``plugin.context``（逐端点精确路径）。

    Args:
        plugin: 插件实例（WebUIPlugin）；路由经其 ``context.register_web_api``
            注册，``plugin.config`` 读取运行时配置。

    每个 handler 都是模块级 ``handle_*`` 薄封装（经闭包绑定插件实例）；宿主
    调用 handler 时注入路径参数关键字（``view(**path_values)``）。
    """
    context = plugin.context

    def _reg(route: str, methods: list[str], desc: str, handler) -> None:
        async def view(**kwargs):
            return await handler(plugin, **kwargs)

        view.__doc__ = handler.__doc__
        view.__name__ = f"webui_{handler.__name__}"
        context.register_web_api(f"/{PLUGIN_NAME}/{route}", view, methods, desc)

    _reg("health", ["GET"], "连通性探测", handle_health)
    _reg("personas", ["GET"], "人格列表", handle_personas)
    _reg("conversations", ["GET"], "会话列表", handle_list_conversations)
    _reg("conversations", ["POST"], "新建会话", handle_create_conversation)
    _reg("conversations/<cid>", ["DELETE"], "删除会话", handle_delete_conversation)
    _reg(
        "conversations/<cid>/delete",
        ["POST"],
        "删除会话（bridge SDK 无 DELETE 方法的等价别名）",
        handle_delete_conversation,
    )
    _reg("history/<cid>", ["GET"], "富历史分页", handle_history)
    _reg("send", ["POST"], "发送消息并创建流式生成任务", handle_send)
    _reg("stop/<gen_id>", ["POST"], "中止生成任务", handle_stop)
    _reg("stream/<gen_id>", ["GET"], "SSE 订阅（全量重放 + 实时）", handle_stream)
    _reg("upload", ["POST"], "上传单个附件（multipart 字段名 file）", handle_upload)
    _reg("files/<file_id>", ["GET"], "获取附件原文件", handle_files)


@_endpoint
async def handle_health(plugin: Star):
    """GET health —— 连通性探测：{"status":"ok","plugin","version"}。"""
    return json_response(
        {"status": "ok", "plugin": PLUGIN_NAME, "version": await _plugin_version(plugin)}
    )


@_endpoint
async def handle_personas(plugin: Star):
    """GET personas —— 人格列表 + 多模态启发式开关（image_capable）。

    image_capable 恒 true（provider 无公开能力查询 API，契约约定不可靠时恒
    true，仅前端预禁用；最终裁决在生成期，M3 接入附件后按真机核实收紧）。
    """
    items = await persona_service.list_personas(plugin.context)
    return json_response({"image_capable": True, "personas": items})


@_endpoint
async def handle_list_conversations(plugin: Star):
    """GET conversations —— 会话列表（updated_at 降序，busy=该 cid 有进行中任务）。"""
    items = await conversation_service.list_conversations(plugin.context)
    for item in items:
        item["busy"] = chat_service.active_generation_id(item["cid"]) is not None
    return json_response({"conversations": items})


@_endpoint
async def handle_create_conversation(plugin: Star):
    """POST conversations —— body {persona_id, title?} -> {cid}；persona_id 非法 400。"""
    body = await _json_body()
    persona_id = body.get("persona_id")
    title = body.get("title")
    if persona_id is not None:
        if not isinstance(persona_id, str) or not persona_id.strip():
            raise chat_service.SendRejected(400, "invalid_request", "persona_id 必须为非空字符串")
        if not await persona_service.persona_exists(plugin.context, persona_id):
            raise chat_service.SendRejected(400, "invalid_request", f"人格不存在: {persona_id}")
    if title is not None and not isinstance(title, str):
        raise chat_service.SendRejected(400, "invalid_request", "title 必须为字符串")
    info = await conversation_service.create_conversation(plugin.context, persona_id, title)
    return json_response({"cid": info["cid"]})


@_endpoint
async def handle_delete_conversation(plugin: Star, cid: str):
    """DELETE conversations/<cid> 与 POST .../delete —— 幂等删除 + 级联清理。

    若该会话有进行中生成任务，先中止再删（避免删后任务又写回富历史）。
    """
    gen_id = chat_service.active_generation_id(cid)
    if gen_id is not None:
        # wait=True：等被取消任务完全收尾（CancelledError 分支的历史写入完成）后再删，
        # 否则删除先执行、中止分支随后写回富历史，复活已删除会话的数据。
        await chat_service.abort_generation(gen_id, wait=True)
    await conversation_service.delete_conversation(plugin.context, cid, plugin.data_dir())
    return json_response({"ok": True})


@_endpoint
async def handle_history(plugin: Star, cid: str):
    """GET history/<cid>?page=&size= —— 富历史分页（page=1 最新一页，页内旧->新）。"""
    conv = await conversation_service.get_conversation(plugin.context, cid)
    if conv is None:
        raise chat_service.SendRejected(404, "not_found", f"会话不存在: {cid}")
    page = max(1, _query_int(request.query, "page", 1))
    size = _query_int(request.query, "size", HISTORY_DEFAULT_PAGE_SIZE)
    size = min(max(1, size), HISTORY_MAX_PAGE_SIZE)
    result = await uistore.page_messages(plugin.data_dir(), cid, page, size)
    return json_response(result)


@_endpoint
async def handle_send(plugin: Star):
    """POST send —— body {cid, text, file_ids[]} -> {gen_id}（恒 200；校验失败 4xx）。

    409 两种：同会话生成中 session_busy / 全局并发满 concurrency_limit；
    会话不存在 404；未知/非法 file_ids -> 400。图片经索引取本地路径直入
    image_urls（多模态不支持不在 send 同步判定，生成期捕获后经 SSE
    error{code:model_not_multimodal} 呈现）；文本附件读出内容拼附件区块。
    """
    body = await _json_body()
    cid = body.get("cid")
    if not isinstance(cid, str) or not cid.strip():
        raise chat_service.SendRejected(400, "invalid_request", "cid 必须为非空字符串")
    text = body.get("text")
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise chat_service.SendRejected(400, "invalid_request", "text 必须为字符串")
    file_ids = body.get("file_ids") or []
    if not isinstance(file_ids, list):
        raise chat_service.SendRejected(400, "invalid_request", "file_ids 必须为数组")
    if not text.strip() and not file_ids:
        raise chat_service.SendRejected(
            400, "invalid_request", "text 与 file_ids 至少其一非空"
        )

    metas: list[dict] = []
    image_urls: list[str] = []
    attachment_blocks: list[str] = []
    resolved_ids: list[str] = []
    for file_id in file_ids:
        if not isinstance(file_id, str) or not file_id.startswith("f_"):
            raise chat_service.SendRejected(400, "invalid_request", f"file_ids 含非法项: {file_id!r}")
        meta = await attachment_service.get_file(plugin.data_dir(), file_id)
        if meta is None:
            raise chat_service.SendRejected(400, "invalid_request", f"附件不存在: {file_id}")
        disk_path = plugin.data_dir() / str(meta.get("path", ""))
        if not disk_path.is_file():
            raise chat_service.SendRejected(400, "invalid_request", f"附件文件已丢失: {file_id}")
        metas.append(attachment_service.public_meta(meta))
        if meta["kind"] == "image":
            image_urls.append(str(disk_path))  # 本地路径直入 image_urls（生成期才判多模态）
        else:
            content, _truncated = await asyncio.to_thread(
                attachment_service.extract_text_content,
                disk_path,
                _read_int_config(plugin.config, "upload_text_max_kb"),
            )
            attachment_blocks.append(attachment_service.build_attachment_block(meta["name"], content))
        resolved_ids.append(file_id)

    gen_id = await chat_service.start_generation(
        plugin, cid.strip(), text,
        image_urls=image_urls or None,
        attachment_blocks=attachment_blocks or None,
        attachment_meta=metas or None,
    )
    for file_id in resolved_ids:  # 引用绑定（失败不阻断：孤儿清理只清未引用附件）
        try:
            await attachment_service.bind_file(plugin.data_dir(), file_id, cid.strip())
        except Exception:  # noqa: BLE001
            logger.warning(f"[webui] 附件引用绑定失败 file_id={file_id} cid={cid}")
    return json_response({"gen_id": gen_id})


@_endpoint
async def handle_upload(plugin: Star):
    """POST upload —— multipart 单文件（字段名 file）-> 契约附件元数据。

    类型不在白名单 415 unsupported_media_type；图片超限（upload.save(max_bytes)
    抛 UploadTooLargeError）413 payload_too_large 拒绝不截断；文本落盘套 16MB
    硬上限，注入内容超 upload_text_max_kb 截断置 truncated=true。
    """
    files = await request.files()
    upload = files.get("file") if files else None
    if not isinstance(upload, PluginUploadFile):
        raise chat_service.SendRejected(400, "invalid_request", "没有拿到文件（multipart 字段名须为 file）")
    try:
        meta = await attachment_service.store_upload(upload, plugin.data_dir(), plugin.config)
    except attachment_service.InvalidFilenameError as exc:
        raise chat_service.SendRejected(400, "invalid_request", str(exc))
    except attachment_service.UnsupportedMediaTypeError as exc:
        raise chat_service.SendRejected(415, "unsupported_media_type", str(exc))
    except attachment_service.UploadTooLargeError:
        raise chat_service.SendRejected(413, "payload_too_large", "上传文件超出大小限制")
    return json_response(meta)


@_endpoint
async def handle_files(plugin: Star, file_id: str):
    """GET files/<file_id> —— 原文件（file_response，经索引定位，绝不暴露本地路径）。"""
    meta = await attachment_service.get_file(plugin.data_dir(), file_id)
    if meta is None:
        raise chat_service.SendRejected(404, "not_found", f"附件不存在: {file_id}")
    disk_path = plugin.data_dir() / str(meta.get("path", ""))
    if not disk_path.is_file():
        raise chat_service.SendRejected(404, "not_found", "附件文件已丢失")
    return file_response(disk_path, content_type=meta.get("mime"))


@_endpoint
async def handle_stop(plugin: Star, gen_id: str):
    """POST stop/<gen_id> —— 先落终态 stopped 再 cancel；未知/已结束 404。"""
    stopped = await chat_service.abort_generation(gen_id)
    if not stopped:
        raise chat_service.SendRejected(404, "not_found", f"生成任务不存在或已结束: {gen_id}")
    return json_response({"ok": True, "stopped": True})


@_endpoint
async def handle_stream(plugin: Star, gen_id: str):
    """GET stream/<gen_id> —— SSE 订阅（seq=0 全量重放 -> 实时；终态后关流）。

    gen_id 不存在或缓冲已过期 -> 404（前端视为终局，清理本地活跃 gen_id）。
    空闲超过 ``SSE_PING_INTERVAL_SECONDS`` 时发 ``event: ping`` 保活帧（不进
    缓冲、不占业务 seq），防止工具执行/长首 token 期间连接被反代或弱网中间层
    掐断；前端/EventSource 对未监听的事件类型天然忽略。
    """
    if not chat_service.generation_exists(gen_id):
        raise chat_service.SendRejected(404, "not_found", f"生成任务不存在或缓冲已过期: {gen_id}")

    async def _frame_source():
        # 注意不能用 wait_for(__anext__)：超时会取消挂起的 __anext__ 并把
        # CancelledError 注入底层生成器（iter_events 被杀、流提前结束）。
        # 这里用 asyncio.wait 超时不取消任务：超时发 ping 后复用同一任务继续等。
        agen = chat_service.iter_events(gen_id)
        pending = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(agen.__anext__())
                done, _ = await asyncio.wait({pending}, timeout=SSE_PING_INTERVAL_SECONDS)
                if not done:
                    yield sse.encode_event("ping", {"ts": uistore.now_iso()})
                    continue
                try:
                    item = pending.result()
                except StopAsyncIteration:
                    return
                pending = None
                yield sse.encode_event(item["event"], item["data"])
        finally:
            if pending is not None:
                pending.cancel()
                try:
                    await pending
                except (asyncio.CancelledError, StopAsyncIteration):
                    pass
            await agen.aclose()

    return stream_response(
        _frame_source(),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
