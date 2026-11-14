"""人格服务：人格列表（数据源 ``context.persona_manager``）。

宿主 API 实证（AstrBot v4.29.4 persona_mgr.py / po.py）：
- ``get_all_personas()`` 返回 **Persona PO 列表**（属性 ``persona_id`` /
  ``system_prompt`` / ``begin_dialogs`` / ``tools``，**没有 name 字段**）；
- ``get_default_persona_v3(umo)`` 返回 **Personality dict**（TypedDict，键
  ``name`` / ``prompt`` / ``tools`` / ``_begin_dialogs_processed`` 等）；v3 映射
  即 ``persona["name"] == persona.persona_id``（persona_mgr.py get_v3_persona_data）。

因此两种对象的访问方式不同：Persona PO 用属性、Personality 用 ``["name"]`` 下标，
勿混写。契约 id/name 均取 ``persona_id``。

v0.2 起：会话人格解析/工具注入全部归还宿主管道（persona_service 仅供
GET personas 列表展示；旧 resolve_persona_tools 已随工具循环废止）。
"""

from astrbot.api.star import Context

# 前端预览摘要长度上限（对接契约 §REST GET personas: system_prompt_preview <=80 字）
PREVIEW_MAX_LEN = 80


async def get_default_persona_v3(context: Context, umo: str) -> dict:
    """取某会话空间的默认人格（v3 Personality dict，键 name/prompt/tools）。

    委托 ``persona_manager.get_default_persona_v3(umo)``；宿主保证至少返回
    内置默认人格（DEFAULT_PERSONALITY），不会返回 None。
    """
    return await context.persona_manager.get_default_persona_v3(umo)


async def get_persona_v3_by_id(context: Context, persona_id: str | None) -> dict | None:
    """按 id 解析 v3 Personality dict；不存在或 id 为空返回 None。"""
    return context.persona_manager.get_persona_v3_by_id(persona_id)


async def get_persona_for_conversation(
    context: Context, umo: str, persona_id: str | None
) -> dict:
    """解析会话生效人格：会话绑定的 persona_id 优先，缺省/已删除时回退默认人格。

    Returns:
        v3 Personality dict（至少含 ``name`` / ``prompt`` / ``tools``）。
    """
    persona = await get_persona_v3_by_id(context, persona_id)
    if persona is not None:
        return persona
    return await get_default_persona_v3(context, umo)


async def persona_exists(context: Context, persona_id: str) -> bool:
    """persona_id 是否存在（GET conversations 新建会话校验用）。

    宿主 ``get_persona`` 对不存在的人格抛 ``ValueError``（persona_mgr.py L66-71）。
    """
    try:
        await context.persona_manager.get_persona(persona_id)
        return True
    except ValueError:
        return False


async def list_personas(context: Context, umo: str | None = None) -> list[dict]:
    """返回人格列表项（GET personas 的数据源）。

    Args:
        context: 宿主上下文（取 persona_manager）。
        umo: 默认人格判定用的会话空间；缺省用本插件命名空间的探针 umo
            （``webui:FriendMessage:webui!default``，无会话级配置时回落全局）。

    Returns:
        dict 列表：``{id, name, system_prompt_preview, has_tools, is_default}``。
        ``id``/``name`` 均取 Persona PO 的 ``persona_id``（PO 无 name 字段）；
        ``is_default`` 与 ``get_default_persona_v3(umo)["name"]`` 比对。
    """
    from . import conversation_service

    if umo is None:
        umo = conversation_service.build_umo("default")
    personas = await context.persona_manager.get_all_personas()
    default_name = (await get_default_persona_v3(context, umo)).get("name")
    items: list[dict] = []
    for persona in personas:
        persona_id = persona.persona_id
        tools = persona.tools
        items.append(
            {
                "id": persona_id,
                "name": persona_id,  # v3 映射即 persona["name"]，契约 id/name 同值
                "system_prompt_preview": summarize_system_prompt(
                    persona.system_prompt or "", PREVIEW_MAX_LEN
                ),
                # tools 语义：None=全部工具、非空列表=白名单 -> 都算「带工具」；[] 禁用
                "has_tools": tools is None or bool(tools),
                "is_default": persona_id == default_name,
            }
        )
    return items


def summarize_system_prompt(system_prompt: str, max_len: int = 120) -> str:
    """人格 system_prompt 摘要（人格下拉卡片展示用，超出 max_len 截断加省略号）。"""
    text = (system_prompt or "").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…"
