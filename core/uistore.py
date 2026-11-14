"""UI 富历史存储：``data/plugin_data/astrbot_plugin_webui/ui_history/<cid>.json``（历史双写的另一半）。

契约（UserPlan §3.1 + 对接契约 §富历史；M2 工单刻意细化）：
- **每个会话一个 JSON 文件**（对计划 §3.1 单文件 ``ui_history.json`` 示意图的
  刻意细化）：不同会话的写互不竞争，无需跨 cid 写锁；读侧依靠 ``tmp 写 +
  os.replace`` 原子替换保证只读到完整文件。计划示意图与实现的差异在此注明；
- 富历史含工具调用步骤、reasoning、附件引用与消息元数据；刷新/重载后 UI 从
  富历史渲染，LLM 上下文从 conversation_manager 权威历史组装（§3.1 双写）；
- 文档带 ``version`` 字段留迁移（当前 1）；加载不识别的版本时尽力读取
  ``messages`` 并记 warning，**不清空用户数据**；
- **持久化绝不写插件目录**（插件更新/重装会丢）——路径由调用方传入插件数据目录
  （``data/plugin_data/astrbot_plugin_webui/``，main.py 经 get_astrbot_data_path 计算）。

文件结构::

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


# per-cid 写锁（读-改-写串行化；asyncio.Lock 在 3.10+ 惰性绑定事件循环，
# 模块级复用安全；跨 cid 不同键天然无竞争）
_CID_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(cid: str) -> asyncio.Lock:
    lock = _CID_LOCKS.get(cid)
    if lock is None:
        lock = asyncio.Lock()
        _CID_LOCKS[cid] = lock
    return lock


def now_iso() -> str:
    """当前时刻的 ISO8601 带时区字符串（UTC）。"""
    return datetime.now(timezone.utc).isoformat()


def new_message_id() -> str:
    """消息 id：``m_<uuid4hex>``（对接契约 §id 规范）。"""
    return f"m_{uuid.uuid4().hex}"


def ui_store_path(data_dir: Path) -> Path:
    """返回富历史存储目录：``<data_dir>/ui_history/``。

    M2 细化：M1 示意图是单文件 ``<data_dir>/ui_history.json``；实现改为每
    cid 一文件避免跨会话写锁，本函数返回的是目录，单会话文件见
    ``conversation_file``。
    """
    return Path(data_dir) / HISTORY_DIR_NAME


def conversation_file(data_dir: Path, cid: str) -> Path:
    """返回某会话的富历史文件路径：``<data_dir>/ui_history/<cid>.json``。

    cid 由路由层保证为 uuid4hex（构造时由宿主生成），直接拼接不会引入路径
    分隔符；此处仍做一次防御性校验。
    """
    if not cid or "/" in cid or "\\" in cid or cid in (".", ".."):
        raise ValueError(f"非法会话 id: {cid!r}")
    return ui_store_path(data_dir) / f"{cid}.json"


def _empty_doc(cid: str) -> dict:
    return {"version": SCHEMA_VERSION, "cid": cid, "updated_at": now_iso(), "messages": []}


def _read_doc(path: Path) -> dict | None:
    """同步读单个会话文档；不存在/损坏返回 None（调用方决定兜底行为）。"""
    try:
        raw = _retry_file_io(lambda: path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except OSError as exc:
        from astrbot.api import logger

        logger.warning(f"[uistore] 读取富历史失败 {path}: {exc}")
        return None
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        from astrbot.api import logger

        logger.warning(f"[uistore] 富历史 JSON 损坏（保留原文件不清空）{path}: {exc}")
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("messages"), list):
        from astrbot.api import logger

        logger.warning(f"[uistore] 富历史结构不符（保留原文件不清空）{path}")
        return None
    version = doc.get("version")
    if version != SCHEMA_VERSION:
        from astrbot.api import logger

        logger.warning(
            f"[uistore] 富历史版本 {version!r} 与当前 {SCHEMA_VERSION} 不同，"
            f"尽力读取 messages（迁移预留）{path}"
        )
    return doc


def _write_doc(path: Path, doc: dict) -> None:
    """同步原子写：先写同目录临时文件，再 ``os.replace`` 原子替换。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _retry_file_io(
            lambda: tmp.write_text(
                json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        )
        _retry_file_io(lambda: os.replace(tmp, path))
    finally:
        if tmp.exists():  # replace 失败时清理临时文件
            tmp.unlink(missing_ok=True)


async def load_conversation(data_dir: Path, cid: str) -> dict:
    """读取某会话的富历史文档；不存在或损坏返回空结构（不抛错、不清盘）。"""
    doc = await asyncio.to_thread(_read_doc, conversation_file(data_dir, cid))
    if doc is None:
        return _empty_doc(cid)
    return doc


async def load_ui_history(data_dir: Path) -> dict:
    """读取全部富历史（扫描 ui_history 目录下所有 ``<cid>.json``）。

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


async def append_message(data_dir: Path, cid: str, message: dict) -> dict:
    """追加一条 UI 消息（读-改-原子写；message 即对接契约 §富历史的消息对象）。

    同 cid 的读-改-写全程持 per-cid 锁（``_lock_for``）：并发追加（如 single-flight
    窗口外的边界场景）串行化，杜绝后写覆盖前写丢消息；跨 cid 天然无竞争。

    Returns:
        追加后的完整消息（补全 id/ts 缺省值；ts 缺省当前 ISO8601 UTC）。
    """
    path = conversation_file(data_dir, cid)
    stored = dict(message)
    stored.setdefault("id", new_message_id())
    stored.setdefault("ts", now_iso())

    def _update() -> dict:
        doc = _read_doc(path) or _empty_doc(cid)
        doc["version"] = SCHEMA_VERSION
        doc["cid"] = cid
        doc["messages"] = list(doc.get("messages") or [])
        doc["messages"].append(stored)
        doc["updated_at"] = now_iso()
        _write_doc(path, doc)
        return stored

    async with _lock_for(cid):
        return await asyncio.to_thread(_update)


async def has_user_message(data_dir: Path, cid: str) -> bool:
    """该会话是否已存在用户消息（「首条用户消息后自动改题」判定用）。"""
    doc = await load_conversation(data_dir, cid)
    return any(msg.get("role") == "user" for msg in doc.get("messages") or [])


async def page_messages(data_dir: Path, cid: str, page: int = 1, size: int = 30) -> dict:
    """分页读取某会话富历史（GET history/<cid> 数据源）。

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
    """删除会话时清理其富历史文件（与 conversation_service.delete_conversation 配套）。

    与 ``append_message`` 共用 per-cid 锁：删除与追加串行化，杜绝「先删后追加」
    造成的已删会话文件复活。
    """
    path = conversation_file(data_dir, cid)

    def _unlink() -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            from astrbot.api import logger

            logger.warning(f"[uistore] 删除富历史失败 {path}: {exc}")

    async with _lock_for(cid):
        await asyncio.to_thread(_unlink)
