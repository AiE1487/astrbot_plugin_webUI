"""会话服务：独立命名空间 umo、会话 CRUD 与权威历史组装（历史双写的一半）。

契约（UserPlan §2 拍板 + M1/M2 工单实证）：
- 会话挂靠插件**独立命名空间** ``webui:FriendMessage:webui!<cid>``，与内置
  WebChat / Live Chat 互不可见；历史/人格自管，不被官方页误改；
- LLM 权威历史存 ``context.conversation_manager``（``add_message_pair``），
  UI 富历史（工具步骤、reasoning、附件引用、消息元数据）存 uistore；
- 权威历史侧不做 reasoning 特殊处理（宿主以 ThinkPart 包装），组装
  ``contexts`` 时**不要剥掉签名**（§3.1）；
- 改题用 ``update_conversation(umo, conversation_id=cid, title=...)``——
  ``update_conversation_title`` 已 @deprecated（v4.29.4 conversation_mgr.py
  L309-310），禁用。

宿主 API 实证（v4.29.4 conversation_mgr.py）：
- ``get_conversations(unified_msg_origin=None, platform_id=None)``：按 umo 过滤
  只能取到单会话，**列表用 platform_id="webui" 过滤后插件侧再按 updated_at 降序**；
- ``new_conversation(umo, ..., persona_id=)`` 返回宿主生成的对话 id（uuid）；
- ``add_message_pair(cid, user_message, assistant_message)`` 接受 OpenAI 格式
  dict，直接追加进 ``Conversation.history``。
"""

import json
import uuid
from datetime import datetime, timezone

from astrbot.api import logger
from astrbot.api.star import Context

UMO_PLATFORM = "webui"
UMO_TYPE = "FriendMessage"
SESSION_PREFIX = "webui!"

DEFAULT_TITLE = "新会话"
TITLE_MAX_LEN = 20


class ConversationNotFoundError(Exception):
    """会话不存在（权威历史里查不到该 cid）。"""


def new_cid_seed() -> str:
    """插件侧生成的 uuid4hex 种子（new_conversation 需要一个完整 umo 传参）。

    宿主 ``new_conversation`` 内部生成并返回真正的对话 id，传参 umo 的
    session_id 段仅用于解析 platform_id 与记录 sel_conv_id；真正的单会话操作
    都按返回的 cid 显式传参并按 ``build_umo(cid)`` 重建 umo，该种子不外露。
    """
    return uuid.uuid4().hex


def build_umo(cid: str) -> str:
    """由会话 id 构造独立命名空间 ``unified_msg_origin``。

    Returns:
        形如 ``webui:FriendMessage:webui!<cid>`` 的字符串。
    """
    return f"{UMO_PLATFORM}:{UMO_TYPE}:{SESSION_PREFIX}{cid}"


def parse_umo(umo: str) -> str | None:
    """从 ``unified_msg_origin`` 反解会话 id。

    Returns:
        cid；不属于本插件命名空间（platform/type 不符或缺 ``webui!`` 前缀）
        返回 ``None``。
    """
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


def _to_iso(ts: int | float | None) -> str:
    """宿主存的是 epoch 秒 int；转 ISO8601 带时区（UTC）。"""
    if not ts:
        return datetime.now(timezone.utc).isoformat()
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return datetime.now(timezone.utc).isoformat()


async def get_conversation(context: Context, cid: str):
    """按 cid 取权威会话（Conversation PO），不存在返回 None。"""
    umo = build_umo(cid)
    return await context.conversation_manager.get_conversation(umo, cid)


async def list_conversations(context: Context) -> list[dict]:
    """会话列表（GET conversations 数据源）。

    平台过滤取 ``platform_id="webui"``（按 umo 过滤只能取到单会话），再插件侧
    用 ``parse_umo`` 双重确认属于本插件命名空间，最后按 updated_at 降序。

    Returns:
        dict 列表：``{cid, title, persona_id, updated_at}``（updated_at 为
        ISO8601 带时区字符串；busy 标志由 routes 层并入）。
    """
    convs = await context.conversation_manager.get_conversations(platform_id=UMO_PLATFORM)
    items: list[dict] = []
    for conv in convs:
        if parse_umo(getattr(conv, "user_id", "")) is None:
            continue  # 非本插件命名空间的同名平台会话，不展示
        items.append(
            {
                "cid": conv.cid,
                "title": conv.title or DEFAULT_TITLE,
                "persona_id": conv.persona_id or None,
                "updated_at": _to_iso(conv.updated_at),
                "_sort_ts": float(conv.updated_at or 0),
            }
        )
    items.sort(key=lambda item: item["_sort_ts"], reverse=True)
    for item in items:
        item.pop("_sort_ts", None)
    return items


async def create_conversation(
    context: Context,
    persona_id: str | None,
    title: str | None = None,
) -> dict:
    """新建会话（``conversation_manager.new_conversation`` 绑定 persona_id）。

    persona_id 的存在性校验由 routes 层先做（非法 -> 400）。

    Returns:
        新会话信息 dict：``{cid, title, persona_id, updated_at}``；cid 为宿主
        ``new_conversation`` 返回的 uuid4hex（契约 id 规范）。
    """
    umo = build_umo(new_cid_seed())
    cid = await context.conversation_manager.new_conversation(
        umo,
        title=(title or DEFAULT_TITLE),
        persona_id=persona_id,
    )
    return {
        "cid": cid,
        "title": title or DEFAULT_TITLE,
        "persona_id": persona_id,
        "updated_at": _to_iso(None),
    }


async def rename_conversation(context: Context, cid: str, title: str) -> None:
    """改会话标题：``update_conversation(umo, conversation_id=cid, title=...)``。

    首条用户消息后取其前 ``TITLE_MAX_LEN`` 字自动改题（M2 工单 ③）。
    """
    title = (title or "").strip()
    if not title:
        return
    umo = build_umo(cid)
    await context.conversation_manager.update_conversation(
        umo,
        conversation_id=cid,
        title=title[:TITLE_MAX_LEN],
    )


async def delete_conversation(context: Context, cid: str, data_dir=None) -> None:
    """删除会话：权威历史 + UI 富历史（uistore）+ 附件引用一并清理。

    幂等：权威会话不存在时跳过宿主删除，仍清理插件侧富历史/附件。
    附件级联删除在 M3 接入 attachment_service（当前无附件索引，跳过）。

    Args:
        context: 宿主上下文。
        cid: 会话 id。
        data_dir: 插件数据目录（None 时跳过富历史清理，仅供测试）。
    """
    umo = build_umo(cid)
    conv = await context.conversation_manager.get_conversation(umo, cid)
    if conv is not None:
        await context.conversation_manager.delete_conversation(umo, cid)
    if data_dir is not None:
        from . import uistore

        await uistore.clear_conversation(data_dir, cid)
        try:
            from . import attachment_service

            removed = await attachment_service.delete_conversation_files(data_dir, cid)
            if removed:
                logger.info(f"[webui] 会话级联删除附件文件与索引 {removed} 个 cid={cid}")
        except Exception as exc:  # noqa: BLE001 —— 附件清理失败不影响会话删除
            logger.warning(f"[webui] 会话附件级联删除失败 cid={cid}: {exc}")
    logger.debug(f"[webui] 会话已删除 cid={cid}")


async def append_history_pair(
    context: Context,
    cid: str,
    user_text: str,
    assistant_text: str,
) -> None:
    """LLM 权威历史双写（``add_message_pair``），done/stopped 后由 chat_service 调用。

    消息为 OpenAI 格式 dict（content 为纯文本）；生成失败时不调用本函数
    （权威历史不落半截回复）。
    """
    umo = build_umo(cid)
    await context.conversation_manager.add_message_pair(
        cid=cid,
        user_message={"role": "user", "content": user_text},
        assistant_message={"role": "assistant", "content": assistant_text},
    )


async def build_contexts(context: Context, cid: str) -> list[dict]:
    """组装发起 LLM 请求用的 ``contexts``（权威历史）。

    注意：宿主以 ThinkPart 包装 reasoning 入历史，组装时**原样透传**，不剥
    signature/ThinkPart（§3.1）。

    Raises:
        ConversationNotFoundError: 会话不存在。
    """
    conv = await get_conversation(context, cid)
    if conv is None:
        raise ConversationNotFoundError(f"会话不存在: {cid}")
    raw = getattr(conv, "history", "") or "[]"
    try:
        history = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"[webui] 权威历史 JSON 解析失败，按空历史处理 cid={cid}")
        return []
    if not isinstance(history, list):
        return []
    return [entry for entry in history if isinstance(entry, dict)]
