"""UI 富历史存储：``data/plugin_data/astrbot_plugin_webui/ui_history/<cid>.json``（v0.2 降级为只读归档层）。

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
