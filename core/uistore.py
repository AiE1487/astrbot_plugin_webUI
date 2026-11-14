"""UI 富历史存储：``data/plugin_data/astrbot_plugin_webui/ui_history/<cid>.json``（v0.2 降级为只读归档层）。

v0.3.1 起本模块同时承担 **界面设置持久化**（``ui_settings.json``）与 **消息头像
文件**（``User_head.png`` / ``LLM_head.png``，新头像统一命名覆盖旧文件），见文末
「UI 设置与头像持久化」节。

契约（开发计划 §0.3 处置清单）：
- **v0.2 起新会话历史全量托管给宿主平台历史**（platform_message_history），
  本模块**不再写入**：只保留读/分页/删除归档文件能力，作为 v0.1.x 旧会话的
  只读归档（可查看不可续聊，不迁移）；
- ``conversations`` 列表与 ``history/<cid>`` 的 legacy 分支数据源即本模块
  （``list_archives`` / ``page_messages``）；legacy 删除走 ``clear_conversation``
  （删归档文件、不触宿主）；
- 文档带 ``version`` 字段留迁移（当前 1）；加载不识别的版本时尽力读取
  ``messages`` 并记 warning，**不清空用户数据**；
- **持久化绝不写插件目录**（插件更新/重装会丢）——路径由调用方传入插件数据目录
  （``data/plugin_data/astrbot_plugin_webui/``，main.py 经 get_astrbot_data_path 计算）。

文件结构（v0.1.x 遗留，只读）::

    {"version": 1, "cid": "<cid>", "updated_at": "<ISO8601>",
     "messages": [<对接契约 §富历史的消息对象>...]}

消息按时间升序存储（旧 -> 新）；分页语义见 ``page_messages``。
"""

import asyncio
import json
import os
import time
import uuid
import base64
import binascii
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
HISTORY_DIR_NAME = "ui_history"


def _retry_file_io(func, attempts: int = 4, delay: float = 0.02):
    """Windows 瞬时共享冲突重试（AV/索引服务短暂锁定刚被 os.replace 的文件）。

    仅对 PermissionError 重试（FileNotFoundError 是合法结果，不重试）。
    """
    for attempt in range(attempts):
        try:
            return func()
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def now_iso() -> str:
    """当前时刻的 ISO8601 带时区字符串（UTC）。"""
    return datetime.now(timezone.utc).isoformat()


def new_message_id() -> str:
    """消息 id：``m_<uuid4hex>``（对接契约 §id 规范）。"""
    return f"m_{uuid.uuid4().hex}"


def ui_store_path(data_dir: Path) -> Path:
    """返回富历史存储目录：``<data_dir>/ui_history/``。"""
    return Path(data_dir) / HISTORY_DIR_NAME


def conversation_file(data_dir: Path, cid: str) -> Path:
    """返回某会话的归档文件路径：``<data_dir>/ui_history/<cid>.json``。

    cid 由路由层保证为 uuid4hex；此处仍做一次防御性校验。
    """
    if not cid or "/" in cid or "\\" in cid or cid in (".", ".."):
        raise ValueError(f"非法会话 id: {cid!r}")
    return ui_store_path(data_dir) / f"{cid}.json"


def _read_doc(path: Path) -> dict | None:
    """同步读单个归档文档；不存在/损坏返回 None（调用方决定兜底行为）。"""
    try:
        raw = _retry_file_io(lambda: path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except OSError as exc:
        from astrbot.api import logger

        logger.warning(f"[uistore] 读取归档失败 {path}: {exc}")
        return None
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        from astrbot.api import logger

        logger.warning(f"[uistore] 归档 JSON 损坏（保留原文件不清空）{path}: {exc}")
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("messages"), list):
        from astrbot.api import logger

        logger.warning(f"[uistore] 归档结构不符（保留原文件不清空）{path}")
        return None
    version = doc.get("version")
    if version != SCHEMA_VERSION:
        from astrbot.api import logger

        logger.warning(
            f"[uistore] 归档版本 {version!r} 与当前 {SCHEMA_VERSION} 不同，"
            f"尽力读取 messages（迁移预留）{path}"
        )
    return doc


async def load_conversation(data_dir: Path, cid: str) -> dict:
    """读取某会话归档文档；不存在或损坏返回空结构（不抛错、不清盘）。"""
    path = conversation_file(data_dir, cid)
    doc = await asyncio.to_thread(_read_doc, path)
    if doc is None:
        return {"version": SCHEMA_VERSION, "cid": cid, "updated_at": now_iso(), "messages": []}
    return doc


async def load_ui_history(data_dir: Path) -> dict:
    """读取全部归档（扫描 ui_history 目录下所有 ``<cid>.json``）。

    Returns:
        ``{version, conversations: {cid: {version, cid, updated_at, messages}}}``；
        目录不存在返回空结构。
    """
    root = ui_store_path(data_dir)

    def _scan() -> dict:
        conversations: dict[str, dict] = {}
        if not root.is_dir():
            return conversations
        for path in sorted(root.glob("*.json")):
            doc = _read_doc(path)
            if doc is not None and doc.get("cid"):
                conversations[str(doc["cid"])] = doc
        return conversations

    conversations = await asyncio.to_thread(_scan)
    return {"version": SCHEMA_VERSION, "conversations": conversations}


async def list_archives(data_dir: Path) -> list[dict]:
    """扫描归档目录产出 legacy 会话项（conversations 列表 legacy 追加项数据源）。

    Returns:
        ``[{cid, updated_at, first_user_text}]``（updated_at 为文档 ISO 时间；
        first_user_text 取首条用户消息文本，供标题派生；空目录返回 []）。
    """
    root = ui_store_path(data_dir)

    def _scan() -> list[dict]:
        items: list[dict] = []
        if not root.is_dir():
            return items
        for path in sorted(root.glob("*.json")):
            doc = _read_doc(path)
            if doc is None or not doc.get("cid"):
                continue
            first_user_text = ""
            for message in doc.get("messages") or []:
                if isinstance(message, dict) and message.get("role") == "user":
                    first_user_text = str(message.get("text") or "")
                    break
            items.append(
                {
                    "cid": str(doc["cid"]),
                    "updated_at": doc.get("updated_at") or "",
                    "first_user_text": first_user_text,
                }
            )
        return items

    return await asyncio.to_thread(_scan)


async def page_messages(data_dir: Path, cid: str, page: int = 1, size: int = 30) -> dict:
    """分页读取某会话归档（GET history/<cid> legacy 分支数据源）。

    分页语义（对接契约 §REST）：page=1 为**最新一页**，页内消息旧 -> 新；
    ``has_more`` 表示更早的数据还有。

    Returns:
        ``{cid, page, size, total, has_more, messages}``。
    """
    doc = await load_conversation(data_dir, cid)
    messages = list(doc.get("messages") or [])
    total = len(messages)
    page = max(1, int(page))
    size = max(1, int(size))
    start = max(0, total - page * size)  # 本页最早一条的下标
    end = total - (page - 1) * size
    items = messages[start:end]
    return {
        "cid": cid,
        "page": page,
        "size": size,
        "total": total,
        "has_more": start > 0,
        "messages": items,
    }


async def clear_conversation(data_dir: Path, cid: str) -> None:
    """删除归档文件（legacy 会话删除分支；与读侧共用文件路径校验）。"""
    path = conversation_file(data_dir, cid)

    def _unlink() -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            from astrbot.api import logger

            logger.warning(f"[uistore] 删除归档失败 {path}: {exc}")

    await asyncio.to_thread(_unlink)


# ═══════════════════════════════════════════════════════════════════════
# UI 设置与头像持久化（v0.3.1）
#
# - 界面设置 JSON：``<data_dir>/ui_settings.json``（前端 uiSettings 全量，
#   头像字段不落 JSON —— 头像只存文件，读侧回填 data URL）；
# - 头像文件固定命名（用户约定）：``User_head.png`` / ``LLM_head.png``，
#   新上传头像**覆盖写同一文件名**（替换旧头像文件），清除即删除文件；
# - 全部原子写（tmp + os.replace + Windows 重试），文件名均为常量、无
#   用户输入拼接路径；头像仅接受 PNG data URL（前端 canvas 压缩产物）。


UI_SETTINGS_FILE_NAME = "ui_settings.json"

# uiSettings 头像字段键 → 插件数据目录内固定文件名
AVATAR_FILE_NAMES = {
    "avatarUser": "User_head.png",
    "avatarAssistant": "LLM_head.png",
}

AVATAR_DATA_URL_PREFIX = "data:image/png;base64,"
# 头像 data URL 防御上限（前端压缩为 ≤160px PNG 通常 <100KB；≈1MB 解码后）
AVATAR_MAX_DATA_URL_CHARS = 1_400_000


def ui_settings_file(data_dir: Path) -> Path:
    """返回界面设置 JSON 路径：``<data_dir>/ui_settings.json``。"""
    return Path(data_dir) / UI_SETTINGS_FILE_NAME


def avatar_file(data_dir: Path, key: str) -> Path:
    """返回头像文件路径：``<data_dir>/<User_head|LLM_head>.png``。

    键必须是 ``AVATAR_FILE_NAMES`` 的字段（文件名常量，绝不接受外部输入拼接）。
    """
    name = AVATAR_FILE_NAMES.get(key)
    if not name:
        raise ValueError(f"未知头像键: {key!r}")
    return Path(data_dir) / name


def _read_settings_doc(path: Path) -> dict:
    """同步读设置 JSON；不存在/损坏返回 {}（保留原文件不清空，读侧尽力兜底）。"""
    try:
        raw = _retry_file_io(lambda: path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError):
        return {}
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        from astrbot.api import logger

        logger.warning(f"[uistore] UI 设置 JSON 损坏（保留原文件，读侧兜底空）{path}: {exc}")
        return {}
    return doc if isinstance(doc, dict) else {}


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    """原子写字节文件（tmp + os.replace，PermissionError 按 Windows 瞬时锁重试）。"""
    tmp = path.with_suffix(path.suffix + ".tmp")

    def _write() -> None:
        tmp.write_bytes(payload)
        _retry_file_io(lambda: os.replace(tmp, path))

    _write()


async def load_ui_settings(data_dir: Path) -> dict:
    """读取界面设置；不存在/损坏返回 {}（不抛错、不清盘）。"""
    return await asyncio.to_thread(_read_settings_doc, ui_settings_file(data_dir))


async def save_ui_settings(data_dir: Path, settings: dict) -> dict:
    """原子写界面设置（浅拷贝快照），返回写入内容。"""
    doc = dict(settings)
    payload = json.dumps(doc, ensure_ascii=False).encode("utf-8")
    await asyncio.to_thread(_write_bytes_atomic, ui_settings_file(data_dir), payload)
    return doc


async def read_avatar_data_url(data_dir: Path, key: str) -> str:
    """读头像文件 → ``data:image/png;base64,...``；不存在/读失败返回 ""。"""
    path = avatar_file(data_dir, key)

    def _read() -> str:
        try:
            payload = _retry_file_io(lambda: path.read_bytes())
        except (FileNotFoundError, OSError):
            return ""
        if not payload:
            return ""
        return AVATAR_DATA_URL_PREFIX + base64.b64encode(payload).decode("ascii")

    return await asyncio.to_thread(_read)


async def write_avatar_data_url(data_dir: Path, key: str, data_url: str) -> None:
    """PNG data URL → 覆盖写头像文件（新头像统一命名并替换旧文件）。

    校验 data URL 前缀与 base64 解码；非法抛 ``ValueError``（路由层转 400）。
    内容与磁盘现有文件一致时跳过写（避免每次设置开关都重写头像）。
    """
    if not isinstance(data_url, str) or not data_url.startswith(AVATAR_DATA_URL_PREFIX):
        raise ValueError("头像仅支持 PNG data URL（data:image/png;base64,）")
    if len(data_url) > AVATAR_MAX_DATA_URL_CHARS:
        raise ValueError("头像 data URL 超出大小上限")
    payload_b64 = data_url[len(AVATAR_DATA_URL_PREFIX):]
    try:
        payload = base64.b64decode(payload_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"头像 data URL 解码失败: {exc}") from exc
    if not payload:
        raise ValueError("头像 data URL 内容为空")
    path = avatar_file(data_dir, key)

    def _write() -> None:
        try:
            if _retry_file_io(lambda: path.read_bytes()) == payload:
                return  # 与现有文件一致，跳过（GET 侧回填结果不变）
        except (FileNotFoundError, OSError):
            pass
        _write_bytes_atomic(path, payload)

    await asyncio.to_thread(_write)


async def clear_avatar(data_dir: Path, key: str) -> None:
    """删除头像文件（幂等；不存在视为已清除）。"""
    path = avatar_file(data_dir, key)

    def _unlink() -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            from astrbot.api import logger

            logger.warning(f"[uistore] 删除头像失败 {path}: {exc}")

    await asyncio.to_thread(_unlink)
