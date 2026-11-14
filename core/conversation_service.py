"""会话服务：会话 CRUD 映射宿主、cid/conv_id 双主键解析与历史读取（v0.2）。

契约（开发计划 §0.2/§0.3）：
- 会话标识映射：插件 ``cid``（uuid4hex，前端/sessionStorage 兼容）= 宿主
  ``platform_sessions.session_id``；宿主 conversation 主键 ``conv_id``
  （``new_conversation`` 返回的宿主 uuid）是**另一个 id**——conversation 侧
  操作（get/rename/persona）一律用
  ``conv_id = conversation_manager.get_curr_conversation_id(umo)`` 解析，
  **禁止把 cid 直接传给 conversation_id 参数**（会静默 no-op）；
  前提：new_conversation 的 umo 与注入 umo 逐字符一致；
- create = ``db.create_platform_session(creator, platform_id="webchat",
  session_id=cid, is_group=0)`` + ``conversation_manager.new_conversation(
  umo, platform_id="webchat", title, persona_id)``（自动设当前会话）；
- list = ``db.get_platform_sessions_by_creator(creator, page_size=<大值>)``
  （**默认 20 会静默截断**）-> 逐会话取 conversation（title/persona_id）+ busy
  （routes 层并入）+ 追加 legacy 归档项（legacy:true、busy:false，来源
  uistore 目录扫描——v0.1.x 会话只读归档）；
- delete = pipeline_bridge 级联（新建会话）/ 删 uistore 归档文件 + 不触宿主
  （legacy 会话）；
- 历史读取：新建会话读平台历史（page 1 最新、页内旧->新、has_more 配
  count()），映射富历史 schema；无宿主会话且有归档文件时读 uistore 归档，
  响应附 ``legacy: true``；
- v1.0 的 LLM 权威历史双写（append_history_pair/build_contexts）随旧直调
  路径一并废止。

宿主 API 实证（v4.29.4）：全部经 core/pipeline_bridge.py（宿主对接唯一层）。
"""

import uuid
from datetime import datetime, timezone

from astrbot.api import logger
from astrbot.api.star import Context

from . import attachment_service, pipeline_bridge, uistore

# 转发（persona_service / 老调用方沿用 conversation_service.build_umo 入口）
PLUGIN_CREATOR = pipeline_bridge.PLUGIN_CREATOR
build_umo = pipeline_bridge.build_umo
parse_umo = pipeline_bridge.parse_umo

DEFAULT_TITLE = "新会话"
TITLE_MAX_LEN = 20


class ConversationNotFoundError(Exception):
    """会话不存在（宿主无 platform_session 且无 uistore 归档）。"""


def new_cid_seed() -> str:
    """插件侧生成的 uuid4hex 种子（cid = platform_sessions.session_id）。"""
    return uuid.uuid4().hex


def _to_iso(value) -> str:
    """宿主时间字段（datetime / epoch 秒 / ISO 字符串）-> ISO8601 带时区。"""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, (int, float)) and value:
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    if isinstance(value, str) and value:
        return value
    return datetime.now(timezone.utc).isoformat()


async def create_conversation(
    context: Context,
    persona_id: str | None,
    title: str | None = None,
) -> dict:
    """新建会话（platform_session + conversation 双建，new_conversation 自动设当前）。

    persona_id 的存在性校验由 routes 层先做（非法 -> 400）。

    Returns:
        新会话信息 dict：``{cid, conversation_id, title, persona_id, updated_at}``；
        cid 为插件侧 uuid4hex（= platform_session.session_id），conversation_id
        为宿主 new_conversation 返回的 uuid（双主键）。
    """
    cid = new_cid_seed()
    await pipeline_bridge.create_platform_session(context, cid)
    conv_id = ""
    try:
        conv_id = await pipeline_bridge.new_conversation(
            context,
            build_umo(cid),
            title=(title or DEFAULT_TITLE),
            persona_id=persona_id,
        )
    except Exception:
        # conversation 建失败：回滚 platform_session，避免残留无 conversation 的空壳会话
        try:
            await pipeline_bridge.delete_platform_session(context, cid)
        except Exception:  # noqa: BLE001
            logger.exception(f"[webui] 空壳 platform_session 回滚失败 cid={cid}")
        raise
    return {
        "cid": cid,
        "conversation_id": conv_id,
        "title": title or DEFAULT_TITLE,
        "persona_id": persona_id,
        "updated_at": _to_iso(None),
    }


async def resolve_conversation_id(context: Context, cid: str) -> str | None:
    """cid -> conv_id 双主键解析（conversation 侧操作的前置步骤）。

    Returns:
        宿主 conversation uuid；无绑定（异常/未建）返回 None。
    """
    try:
        return await pipeline_bridge.curr_conversation_id(context, build_umo(cid))
    except Exception as exc:  # noqa: BLE001 —— 桩/宿主缺失防御
        logger.warning(f"[webui] conv_id 解析失败 cid={cid}: {exc}")
        return None


async def get_conversation(context: Context, cid: str):
    """按 cid 取宿主 conversation（先双主键解析 conv_id；不存在返回 None）。"""
    conv_id = await resolve_conversation_id(context, cid)
    if not conv_id:
        return None
    return await pipeline_bridge.get_conversation_by_id(context, build_umo(cid), conv_id)


async def rename_conversation(context: Context, cid: str, title: str) -> None:
    """改会话标题：update_conversation(umo, conversation_id=conv_id, title=...)。

    conv_id 经 ``get_curr_conversation_id`` 解析（禁传 cid）；标题取前
    ``TITLE_MAX_LEN`` 字、折叠空白（首条消息自动改题，M2 工单 ③ 延续）。
    """
    title = " ".join(str(title or "").split())
    if not title:
        return
    conv_id = await resolve_conversation_id(context, cid)
    if not conv_id:
        logger.warning(f"[webui] 改题跳过（无当前 conversation）cid={cid}")
        return
    await pipeline_bridge.update_conversation(
        context,
        build_umo(cid),
        conv_id,
        title=title[:TITLE_MAX_LEN],
    )


async def list_conversations(context: Context, data_dir=None) -> list[dict]:
    """会话列表（GET conversations 数据源）：宿主 platform_sessions + legacy 归档。

    Args:
        data_dir: 插件数据目录（None 时跳过 legacy 归档扫描，仅供测试）。

    Returns:
        dict 列表（updated_at 降序）：宿主项 ``{cid, title, persona_id,
        updated_at}``；legacy 项追加 ``legacy: True, busy: False, persona_id:
        None``（busy 由 routes 层并入宿主项）。
    """
    sessions = await pipeline_bridge.platform_sessions_by_creator(context)
    items: list[dict] = []
    for row in sessions:
        session = row.get("session") if isinstance(row, dict) else getattr(row, "session", None)
        if session is None:
            continue
        cid = str(getattr(session, "session_id", "") or "")
        if not cid:
            continue
        conv = await get_conversation(context, cid)
        items.append(
            {
                "cid": cid,
                "title": (getattr(conv, "title", None) if conv is not None else None) or DEFAULT_TITLE,
                "persona_id": (getattr(conv, "persona_id", None) if conv is not None else None) or None,
                "updated_at": _to_iso(getattr(session, "updated_at", None)),
                "_sort_ts": _sort_ts(getattr(session, "updated_at", None)),
            }
        )

    legacy = await list_legacy_archives(data_dir)
    for item in legacy:
        items.append(item)

    items.sort(key=lambda item: item.pop("_sort_ts", 0.0), reverse=True)
    return items


async def list_legacy_archives(data_dir) -> list[dict]:
    """扫描 uistore 归档目录产出 legacy 会话项（v0.1.x 只读归档）。"""
    if data_dir is None:
        return []
    archives = await uistore.list_archives(data_dir)
    items: list[dict] = []
    for archive in archives:
        text = str(archive.get("first_user_text") or "").strip()
        items.append(
            {
                "cid": archive["cid"],
                "title": text[:TITLE_MAX_LEN] if text else DEFAULT_TITLE,
                "persona_id": None,
                "updated_at": archive.get("updated_at") or _to_iso(None),
                "busy": False,
                "legacy": True,
                "_sort_ts": _sort_ts(archive.get("updated_at")),
            }
        )
    return items


def _sort_ts(value) -> float:
    """排序键：ISO/epoch/datetime -> epoch 秒（解析失败回 0）。"""
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return 0.0
    return 0.0


async def delete_conversation(context: Context, cid: str, data_dir=None) -> None:
    """删除会话：新建会话走宿主级联；legacy 归档走「删归档文件 + 不触宿主」。

    Args:
        context: 宿主上下文。
        cid: 会话 id。
        data_dir: 插件数据目录（None 时跳过插件索引/归档清理，仅供测试）。
    """
    session = None
    try:
        session = await pipeline_bridge.get_platform_session(context, cid)
    except Exception as exc:  # noqa: BLE001 —— 桩/宿主缺失防御
        logger.warning(f"[webui] platform_session 查询失败（按 legacy 分支处理）cid={cid}: {exc}")

    if session is not None and getattr(session, "creator", "") == pipeline_bridge.PLUGIN_CREATOR:
        await pipeline_bridge.cascade_delete_session(context, cid, data_dir=data_dir)
    else:
        # legacy（或未知 cid）：只删 uistore 归档文件与插件附件索引，不触宿主
        logger.info(f"[webui] legacy 归档会话删除（不触宿主）cid={cid}")
    if data_dir is not None:
        await uistore.clear_conversation(data_dir, cid)
        try:
            removed = await attachment_service.delete_conversation_files(data_dir, cid)
            if removed:
                logger.info(f"[webui] 会话级联删除附件文件与索引 {removed} 个 cid={cid}")
        except Exception as exc:  # noqa: BLE001 —— 附件清理失败不影响会话删除
            logger.warning(f"[webui] 会话附件级联删除失败 cid={cid}: {exc}")
    logger.debug(f"[webui] 会话已删除 cid={cid}")


# ---------------------------------------------------------------------------
# 历史读取（GET history/<cid> 数据源）
# ---------------------------------------------------------------------------

MEDIA_PART_TYPES = {"image", "record", "file", "video"}


async def page_history(context: Context, data_dir, cid: str, page: int, size: int) -> dict:
    """分页读取会话历史（新建会话读平台历史；legacy 会话读 uistore 归档）。

    分页语义（对接契约 §REST）：page=1 为**最新一页**，页内消息旧 -> 新；
    ``has_more`` 表示更早的数据还有。

    Raises:
        ConversationNotFoundError: 无宿主会话且无归档（-> 404）。
    """
    session = await pipeline_bridge.get_platform_session(context, cid)
    if session is None or getattr(session, "creator", "") != pipeline_bridge.PLUGIN_CREATOR:
        # legacy 归档分支（v0.1.x 只读）
        doc = await uistore.load_conversation(data_dir, cid)
        if not (doc.get("messages") or []):
            raise ConversationNotFoundError(f"会话不存在: {cid}")
        result = await uistore.page_messages(data_dir, cid, page, size)
        result["legacy"] = True
        for message in result.get("messages") or []:
            message["legacy"] = True
        return result

    total = await pipeline_bridge.count_platform_history(context, cid)
    rows: list = []
    if total:
        rows = await pipeline_bridge.read_platform_history(context, cid, page, size)
    messages: list[dict] = []
    for row in rows:
        item = await _map_platform_row(row, data_dir)
        if item is not None:
            messages.append(item)
    return {
        "cid": cid,
        "page": page,
        "size": size,
        "total": total,
        "has_more": total > page * size,
        "messages": messages,
    }


async def _map_platform_row(row, data_dir) -> dict | None:
    """平台历史行 -> 富历史消息对象（§0.2 历史读取映射表）。"""
    content = getattr(row, "content", None)
    if not isinstance(content, dict):
        return None
    record_type = content.get("type")
    if record_type not in ("user", "bot"):
        return None
    parts = [part for part in (content.get("message") or []) if isinstance(part, dict)]

    row_id = getattr(row, "id", None)
    message: dict = {
        "id": f"m_{row_id}" if row_id else f"m_{uuid.uuid4().hex}",
        "role": "user" if record_type == "user" else "assistant",
        "ts": _to_iso(getattr(row, "created_at", None)),
        "text": pipeline_bridge.collect_plain_text_from_message_parts(parts),
    }

    attachments = await _attachments_from_parts(parts, data_dir)
    if attachments:
        message["attachments"] = attachments

    if record_type == "bot":
        reasoning = content.get("reasoning") or pipeline_bridge.extract_reasoning_from_message_parts(parts)
        if reasoning:
            message["reasoning"] = {"content": reasoning}
        tools = _tools_from_parts(parts)
        if tools:
            message["tools"] = tools
        usage = _usage_from_content(content)
        if usage:
            message["usage"] = usage
    return message


async def _attachments_from_parts(parts: list[dict], data_dir) -> list[dict]:
    """媒体 parts 经插件附件索引反查 -> 契约 attachments 元数据（索引缺失则跳过）。"""
    attachments: list[dict] = []
    for part in parts:
        if part.get("type") not in MEDIA_PART_TYPES:
            continue
        attachment_id = str(part.get("attachment_id") or "")
        meta = await attachment_service.find_by_attachment_id(data_dir, attachment_id)
        if meta is None:
            logger.debug(f"[webui] 附件索引反查失败（跳过）attachment_id={attachment_id}")
            continue
        attachments.append(attachment_service.public_meta(meta))
    return attachments


def _tools_from_parts(parts: list[dict]) -> list[dict]:
    """tool_call parts（宿主形状）-> 富历史工具步骤（沿用 v0.1 步骤形状）。"""
    steps: list[dict] = []
    for part in parts:
        if part.get("type") != "tool_call":
            continue
        for call in part.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            step: dict = {
                "id": str(call.get("id") or ""),
                "name": str(call.get("name") or ""),
            }
            if call.get("args") is not None:
                step["args"] = call.get("args")
            result = call.get("result")
            if result is not None:
                text = str(result)
                if text.startswith(pipeline_bridge.TOOL_ERROR_PREFIX):
                    step["error"] = text[len(pipeline_bridge.TOOL_ERROR_PREFIX):]
                else:
                    step["result"] = text
            if call.get("finished_ts") is not None:
                step["finished_ts"] = call.get("finished_ts")
            steps.append(step)
    return steps


def _usage_from_content(content: dict) -> dict | None:
    """agent_stats（AgentStats.to_dict()）-> 契约 usage；缺失返回 None。"""
    stats = content.get("agent_stats")
    if not isinstance(stats, dict):
        return None
    return pipeline_bridge.usage_from_agent_stats(stats)
