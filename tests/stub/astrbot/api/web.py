"""stub ``astrbot.api.web``：响应封装与上传类型的最小桩（对齐 v4.29.4 真实签名）。

仅覆盖 core/ 实际 import 的名字（routes.py / attachment_service.py）；
返回值用带 ``__type`` 标记的 dict，方便 selftest 断言（references/09 模式）。

签名以真实宿主 v4.29.4 api/web.py 为准（关键字限定参数）：
- ``json_response(data=None, *, status_code=200, headers=None)``
- ``error_response(message, *, status_code=400, data=None, headers=None)``
  （真实包裹形状为 {"status":"error","message","data"}，本插件契约错误包由
  routes.py 适配层经 json_response 直出，不经此函数）
- ``file_response(path, *, filename=None, content_type=None, headers=None)``
- ``stream_response(content, *, content_type="text/event-stream", status_code=200,
  headers=None)``
- ``request``：PluginRequestProxy 等价物，``await request.json(default=None)`` /
  ``await request.form()`` / ``await request.files()`` /
  ``request.query.get(key, default, type=)``（PluginMultiDict 语义）。
"""

from contextlib import contextmanager
from pathlib import Path


class PluginRequest:
    """stub：请求对象占位（真实宿主包装 Starlette Request，供 request 代理取值）。"""

    def __init__(self, request_, *, path_params=None, plugin_name=None, username=None) -> None:
        self._request = request_
        self.path_params = dict(path_params or {})
        self.plugin_name = plugin_name
        self.username = username

    async def json(self, default=None):  # noqa: ANN201
        return await self._request.json(default=default)


@contextmanager
def bind_request_context(request_):
    """stub：绑定当前请求上下文（真实宿主经 contextvars 实现）。"""
    yield request_


class PluginUploadFile:
    """stub：上传文件对象占位（真实宿主为 Starlette UploadFile 的包装）。"""

    def __init__(self, filename: str = "", content_type: str = "") -> None:
        self.filename = filename
        self.content_type = content_type

    async def save(self, destination, *, max_bytes: int | None = None) -> int:
        raise NotImplementedError


class _StubQuery:
    """stub：PluginMultiDict 等价物（get(key, default=None, type=None)）。"""

    def __init__(self) -> None:
        self._pairs: list[tuple[str, object]] = []

    def set_pairs(self, pairs: list[tuple[str, object]]) -> None:
        self._pairs = list(pairs)

    def get(self, key: str, default=None, type: type | None = None):  # noqa: A002
        for item_key, item_value in reversed(self._pairs):
            if item_key != key:
                continue
            if type is None:
                return item_value
            try:
                return type(item_value)
            except (TypeError, ValueError):
                return default
        return default


class _StubRequest:
    """stub：请求上下文占位（真实宿主经 register_web_api 的请求上下文提供）。"""

    def __init__(self) -> None:
        self.headers: dict = {}
        self.remote = "127.0.0.1"
        self.client_host = "127.0.0.1"
        self.query = _StubQuery()
        self._json_payload: object = None
        self._form_payload: dict = {}
        self._files_payload: dict = {}

    # ---- selftest 装载请求内容的助手 ----
    def set_json(self, payload: object) -> None:
        self._json_payload = payload

    def set_query(self, pairs: list[tuple[str, object]]) -> None:
        self.query.set_pairs(pairs)

    def set_files(self, files: dict) -> None:
        self._files_payload = dict(files)

    async def json(self, default=None):
        return self._json_payload if self._json_payload is not None else default

    async def form(self) -> dict:
        return dict(self._form_payload)

    async def files(self) -> dict:
        return dict(self._files_payload)


request = _StubRequest()


def json_response(data=None, *, status_code: int = 200, headers=None) -> dict:
    return {"__type": "json", "status": status_code, "data": data}


def error_response(message: str, *, status_code: int = 400, data=None, headers=None) -> dict:
    # 真实宿主包裹形状：{"status": "error", "message": ..., "data": ...}
    return json_response(
        {"status": "error", "message": message, "data": data}, status_code=status_code
    )


def file_response(path, *, filename: str | None = None, content_type: str | None = None, headers=None) -> dict:
    return {"__type": "file", "path": str(path), "filename": filename}


def stream_response(content, *, content_type: str = "text/event-stream", status_code: int = 200, headers=None) -> dict:
    return {
        "__type": "stream",
        "status": status_code,
        "content_type": content_type,
        "content": content,
        "headers": headers or {},
    }
