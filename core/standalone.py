"""独立端口页：aiohttp 静态托管 + 同款 API 转发 + 密码鉴权 + 登录限速（M1 工单 ⑥ 实现）。

契约（对接契约 §入口与鉴权 / UserPlan §1 F7、§3.1）：
- 入口 ``http://<host>:<port>/`` 静态页（公开）；API = 同款 endpoint 挂
  ``/api/<endpoint>``，与内嵌视图共用 core/ 同一套 service（本模块只是另一个
  HTTP 壳，业务零重复）；
- POST ``/api/login`` body ``{password}`` → ``{token}``；此后全部 ``/api/*`` 带
  ``Authorization: Bearer <token>``；GET 支持 ``?token=`` 等价（EventSource/img
  无法带头）；静态资源公开，``/api/*`` 除 login 外全鉴权；
- 连续 5 次密码错误锁 60 秒（按**传输层对端**地址计数）；
- ``standalone_password`` 为空时免密仅限传输层对端 ``127.0.0.1``/``::1``——
  地址取 aiohttp ``transport peername`` / ``request.remote``，**忽略一切
  X-Forwarded-For 等转发头**（防伪造）。

实现注意：
- aiohttp 延迟导入（桩环境无 aiohttp 也可导入本模块做纯逻辑测试；``start`` 在
  aiohttp 缺失时抛 RuntimeError，main.initialize 捕获后降级仅警告）；
- Dashboard 侧 handler 依赖 ``astrbot.api.web.request`` 代理，本模块为每个请求
  经 ``bind_request_context(PluginRequest(shim))`` 构造等价上下文后**复用
  core.routes 的同款 handler**；
- upload 端点走独立的 aiohttp multipart 实现（``_AiohttpUpload.save`` 自实现，
  不经宿主 ``save_upload_stream``——其内部接口未核实），与 store_upload 的
  duck-typing 协议兼容（filename/content_type/save(max_bytes)）。
"""

import re
import secrets
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl

from astrbot.api import logger
from astrbot.api.star import Star

from .attachment_service import UploadTooLargeError

PLUGIN_NAME = "astrbot_plugin_webui"

LOGIN_MAX_FAILURES = 5
LOGIN_LOCK_SECONDS = 60.0
_LOCAL_ADDRESSES = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})

# 登录限速状态：{传输层对端地址: {"count": 失败次数, "locked_until": monotonic 时间戳}}
_LOGIN_FAILURES: dict[str, dict] = {}
# 登录后签发的 token（进程内有效；插件重载/重启即失效，前端重新登录）
_TOKENS: set[str] = set()

# 同款 API 端点表（method, endpoint 正则, core.routes.handle_* 名）——
# 与 routes.register_routes 的精确路径一一对应；动态段 <name> 编译为单段 [^/]+
_API_ROUTES: tuple[tuple[str, str, str], ...] = (
    ("GET", r"health", "handle_health"),
    ("GET", r"config", "handle_get_config"),
    ("GET", r"personas", "handle_personas"),
    ("GET", r"conversations", "handle_list_conversations"),
    ("POST", r"conversations", "handle_create_conversation"),
    ("DELETE", r"conversations/(?P<cid>[^/]+)", "handle_delete_conversation"),
    ("POST", r"conversations/(?P<cid>[^/]+)/delete", "handle_delete_conversation"),
    ("GET", r"history/(?P<cid>[^/]+)", "handle_history"),
    ("POST", r"send", "handle_send"),
    ("POST", r"stop/(?P<gen_id>[^/]+)", "handle_stop"),
    ("GET", r"stream/(?P<gen_id>[^/]+)", "handle_stream"),
    ("GET", r"files/(?P<file_id>[^/]+)", "handle_files"),
    ("GET", r"ui/settings", "handle_get_ui_settings"),
    ("POST", r"ui/settings", "handle_post_ui_settings"),
)


# ---------------------------------------------------------------------------
# 密码鉴权 + 登录限速（纯逻辑，桩测友好）
# ---------------------------------------------------------------------------


def check_password(password: str, config_password: str, remote_addr: str) -> bool:
    """独立端口鉴权（含登录限速状态机）。

    Args:
        password: 请求方提交的密码。
        config_password: 配置的 ``standalone_password``。
        remote_addr: **传输层对端**地址（本机回退判定与限速计数键；调用方必须
            传 peername/request.remote，忽略转发头）。

    Returns:
        放行返回 True。规则：锁定期间一律 False；配置密码为空 -> 仅放行
        ``_LOCAL_ADDRESSES``；否则常量时间比对（``secrets.compare_digest``）。
        密码错误计数 ``LOGIN_MAX_FAILURES`` 次后锁定 ``LOGIN_LOCK_SECONDS`` 秒，
        鉴权成功清零计数。
    """
    state = _LOGIN_FAILURES.get(remote_addr)
    now = time.monotonic()
    if state is not None and now < state["locked_until"]:
        return False  # 锁定期间一律拒绝（余量经 _login_locked_remaining 提示）

    config_password = config_password or ""
    if not config_password:
        # 空密码：仅传输层对端 127.0.0.1/::1 放行（X-Forwarded-For 等转发头已由
        # 调用方忽略——本函数只认传输层地址）
        if remote_addr in _LOCAL_ADDRESSES:
            if state is not None:
                state["count"] = 0
            return True
        return False

    if secrets.compare_digest(str(password or ""), config_password):
        if state is not None:
            state["count"] = 0
        return True

    fails = (state["count"] if state else 0) + 1
    locked_until = now + LOGIN_LOCK_SECONDS if fails >= LOGIN_MAX_FAILURES else 0.0
    _LOGIN_FAILURES[remote_addr] = {"count": fails, "locked_until": locked_until}
    return False


def _login_locked_remaining(remote_addr: str) -> float:
    """该地址的登录锁定剩余秒数（0 = 未锁定；login handler 提示用）。"""
    state = _LOGIN_FAILURES.get(remote_addr)
    if not state:
        return 0.0
    return max(0.0, state["locked_until"] - time.monotonic())


def _issue_token() -> str:
    """签发登录 token（secrets 随机；进程内有效）。"""
    token = secrets.token_urlsafe(32)
    _TOKENS.add(token)
    return token


def _verify_token(token: str | None) -> bool:
    """校验 token（Authorization Bearer 或 ?token= 提取后传入）。"""
    return bool(token) and token in _TOKENS


def _extract_token(request) -> str | None:  # noqa: ANN001 —— aiohttp Request（延迟导入）
    """提取 token：优先 ``Authorization: Bearer``，缺省 ``?token=``（契约等价）。"""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip() or None
    return request.query.get("token")


def _remote_addr(request) -> str:  # noqa: ANN001
    """传输层对端地址：peername 优先，request.remote 兜底；忽略一切转发头。"""
    transport = getattr(request, "transport", None)
    if transport is not None:
        peer = transport.get_extra_info("peername")
        if isinstance(peer, tuple) and peer:
            return str(peer[0])
    remote = getattr(request, "remote", None)
    return str(remote) if remote else ""


# ---------------------------------------------------------------------------
# aiohttp 应用壳（start/stop；aiohttp 延迟导入）
# ---------------------------------------------------------------------------


async def start(plugin: Star, static_root: Path, host: str, port: int) -> object:
    """启动独立端口 aiohttp 应用（静态页公开 + /api/* 鉴权转发 + 登录限速）。

    Args:
        plugin: 插件实例（handler 转发 core service；upload 直接调 attachment_service）。
        static_root: views/chat 静态目录（views/chat/）。
        host: 监听地址（由插件按配置/安全默认值传入）。
        port: 监听端口（配置 ``standalone_port``）。

    Returns:
        runner（``AppRunner`` 句柄），供 ``terminate`` -> ``stop`` 清理。

    Raises:
        RuntimeError: aiohttp 不可用（宿主内置 aiohttp；桩环境会走到这里）。
    """
    try:
        from aiohttp import web as aio_web
    except ImportError as exc:
        raise RuntimeError(f"aiohttp 不可用，独立端口无法启动: {exc}") from exc

    async def _login_handler(request):  # noqa: ANN001
        return await _handle_login(request, plugin, aio_web)

    async def _api_handler(request):  # noqa: ANN001
        return await _handle_api(request, plugin, aio_web)

    async def _index_handler(request):  # noqa: ANN001
        return aio_web.FileResponse(Path(static_root) / "index.html")

    app = aio_web.Application()
    app.router.add_post("/api/login", _login_handler)
    app.router.add_route("*", "/api/{endpoint:.*}", _api_handler)
    app.router.add_get("/", _index_handler)
    app.router.add_static("/", str(static_root))  # 兜底静态（css/js/logo，公开）

    runner = aio_web.AppRunner(app, access_log=None)
    await runner.setup()
    site = aio_web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"[webui] 独立端口已监听 http://{host}:{port}/")
    return runner


async def stop(runner: object) -> None:
    """停止独立端口并清理 runner（幂等：runner 为 None 时直接返回）。"""
    if runner is None:
        return
    await runner.cleanup()  # type: ignore[attr-defined]


def _error_body(status_code: int, slug: str, message: str) -> dict:
    """契约错误包（与 routes.api_error 同形状）。"""
    return {"code": status_code, "error": slug, "message": message}


async def _handle_login(request, plugin: Star, aio_web) -> "aio_web.Response":  # noqa: ANN001
    """POST /api/login：{password} → {token}；限速锁定期间 401 并提示剩余秒数。"""
    remote = _remote_addr(request)
    remaining = _login_locked_remaining(remote)
    if remaining > 0:
        return aio_web.json_response(
            _error_body(401, "unauthorized", f"密码错误次数过多，已锁定，请 {int(remaining) + 1} 秒后重试"),
            status=401,
        )
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 —— 非 JSON body
        body = None
    password = body.get("password") if isinstance(body, dict) else None
    config_password = plugin.config["standalone_password"]
    if check_password(password or "", config_password, remote):
        return aio_web.json_response({"token": _issue_token()})
    if not config_password:
        # 未设密码时远程访问必被拒：给出可行动的指引而不是笼统的「密码错误」
        message = (
            "未设置访问密码：独立端口仅允许本机 127.0.0.1 访问；"
            "如需远程访问，请在插件配置 standalone_password 设置密码并重载插件"
        )
    else:
        message = "密码错误或无本机访问权限"
    return aio_web.json_response(
        _error_body(401, "unauthorized", message),
        status=401,
    )


async def _handle_api(request, plugin: Star, aio_web) -> "aio_web.Response":  # noqa: ANN001
    """/api/{endpoint}：鉴权 -> 端点匹配 -> 复用 core.routes 同款 handler -> 响应适配。"""
    from astrbot.api.web import PluginRequest, bind_request_context

    from . import routes

    endpoint = request.match_info["endpoint"]

    # 鉴权先行（/api/login 走独立路由不受影响）：upload 在内的一切 /api/* 端点
    # 必须先验 token 再分发，杜绝未鉴权端点绕过独立端口鉴权
    if not _verify_token(_extract_token(request)):
        return aio_web.json_response(
            _error_body(401, "unauthorized", "未登录或令牌无效"), status=401
        )

    # upload 特例：aiohttp multipart 独立实现（不经 astrbot.api.web.request.files 代理）
    if endpoint == "upload":
        if request.method != "POST":
            return aio_web.json_response(_error_body(404, "not_found", "不支持的请求方法"), status=404)
        return await _handle_upload(request, plugin, aio_web)

    for method, pattern, handler_name in _API_ROUTES:
        if request.method != method:
            continue
        matched = re.fullmatch(pattern, endpoint)
        if not matched:
            continue
        handler = getattr(routes, handler_name)
        shim = _ShimRequest(request)
        with bind_request_context(PluginRequest(shim)):
            resp = await handler(plugin, **matched.groupdict())
        return await _to_web_response(resp, request, aio_web)
    return aio_web.json_response(
        _error_body(404, "not_found", f"未知端点: {endpoint}"), status=404
    )


async def _handle_upload(request, plugin: Star, aio_web) -> "aio_web.Response":  # noqa: ANN001
    """POST /api/upload：aiohttp multipart（字段名 file）→ 契约附件元数据。"""
    from . import attachment_service

    if not str(request.content_type or "").startswith("multipart/"):
        return aio_web.json_response(
            _error_body(400, "invalid_request", "需要 multipart 表单（字段名 file）"), status=400
        )
    reader = await request.multipart()
    upload = None
    while True:
        part = await reader.next()
        if part is None:
            break
        if getattr(part, "name", None) == "file" and getattr(part, "filename", None):
            upload = _AiohttpUpload(
                part.filename, part.headers.get("Content-Type", ""), part
            )
            break
    if upload is None:
        return aio_web.json_response(
            _error_body(400, "invalid_request", "没有拿到文件（multipart 字段名须为 file）"),
            status=400,
        )
    try:
        meta = await attachment_service.store_upload(
            upload, plugin.context, plugin.config, plugin.data_dir()
        )
    except attachment_service.InvalidFilenameError as exc:
        return aio_web.json_response(_error_body(400, "invalid_request", str(exc)), status=400)
    except attachment_service.UnsupportedMediaTypeError as exc:
        return aio_web.json_response(
            _error_body(415, "unsupported_media_type", str(exc)), status=415
        )
    except attachment_service.UploadTooLargeError:
        return aio_web.json_response(
            _error_body(413, "payload_too_large", "上传文件超出大小限制"), status=413
        )
    return aio_web.json_response(meta)


async def _to_web_response(resp, request, aio_web) -> "aio_web.Response":  # noqa: ANN001
    """Starlette 响应（routes handler 返回值）→ aiohttp 响应适配。"""
    body_iterator = getattr(resp, "body_iterator", None)
    if body_iterator is not None:  # StreamingResponse（SSE）
        stream = aio_web.StreamResponse(status=getattr(resp, "status_code", 200))
        stream.content_type = getattr(resp, "media_type", None) or "text/event-stream"
        for key, value in dict(getattr(resp, "headers", {}) or {}).items():
            stream.headers[key] = value
        await stream.prepare(request)
        try:
            async for chunk in body_iterator:
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                await stream.write(chunk)
            await stream.write_eof()
        except ConnectionResetError:  # 客户端断开（重订阅属正常流程）
            pass
        return stream
    file_path = getattr(resp, "path", None)
    if file_path is not None:  # FileResponse（附件原文件）
        return aio_web.FileResponse(file_path)
    body = getattr(resp, "body", None)
    return aio_web.Response(
        body=body if body is not None else b"",
        status=getattr(resp, "status_code", 200),
        content_type=getattr(resp, "media_type", None) or "application/json",
    )


class _AiohttpUpload:
    """aiohttp multipart Part -> store_upload 兼容对象（save 自实现分块落盘）。

    协议与 attachment_service 的 duck-typing 约定一致：filename / content_type /
    ``save(destination, max_bytes=...) -> int``，超限抛 UploadTooLargeError 并清理
    半截文件（与宿主 PluginUploadFile.save 行为对齐）。
    """

    def __init__(self, filename: str, content_type: str, part) -> None:  # noqa: ANN001
        self.filename = filename
        self.content_type = content_type
        self._part = part

    async def save(self, destination, *, max_bytes: int | None = None) -> int:  # noqa: ANN001
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            with path.open("wb") as fh:
                while True:
                    chunk = await self._part.read_chunk(64 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if max_bytes is not None and written > max_bytes:
                        raise UploadTooLargeError("too large")
                    fh.write(chunk)
            return written
        except Exception:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise


class _ShimRequest:
    """Starlette Request 接口的 aiohttp 适配层（PluginRequest 依赖面）。

    PluginRequest 只访问：method / url.path / headers(.get) / cookies /
    headers.get("content-type") / client.host / query_params.multi_items() /
    json() / form()（本插件 upload 走独立实现，form 恒空）。
    """

    def __init__(self, aio_request) -> None:  # noqa: ANN001
        self._aio = aio_request
        self.method = aio_request.method
        self.url = SimpleNamespace(path=str(aio_request.rel_url))
        self.headers = aio_request.headers  # CIMultiDict：.get 语义一致
        self.cookies = dict(aio_request.cookies)
        self.client = SimpleNamespace(host=_remote_addr(aio_request))
        self.query_params = SimpleNamespace(
            multi_items=lambda: list(
                parse_qsl(aio_request.query_string, keep_blank_values=True)
            )
        )

    async def json(self):  # noqa: ANN201 —— PluginRequest 自带解析失败兜底
        return await self._aio.json()

    async def form(self):  # noqa: ANN201 —— 空 multi_items 容器
        return _EmptyMultiDict()


class _EmptyMultiDict:
    """空表单容器（PluginRequest._load_form_parts 需要 .multi_items()）。"""

    def multi_items(self) -> list[tuple[str, str]]:
        return []
