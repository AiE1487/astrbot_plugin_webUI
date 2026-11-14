"""工具调用循环（M3，计划 §3.3 方案 A 为主 / 方案 B 降级）。

- **方案 A（TOOL_LOOP_MODE="stream"，默认）**：自实现流式工具循环 —— 每轮经
  chat_service 注入的 ``consume`` 闭包消费 ``text_chat_stream``（本模块**不直接
  触碰** text_chat_stream，保持 M2 的单一适配层约束），末次完整响应含
  ``tools_call_name/args/ids`` 时逐个执行工具（FunctionTool.call，构造方式按
  references/04 的 ContextWrapper/FunctionTool 示例），以 ToolCallsResult 回填
  ``tool_calls_result`` 再流式下一轮；
- **方案 B（TOOL_LOOP_MODE="loop_agent"）**：降级为宿主
  ``tool_loop_agent(agent_hooks=...)``（正文非流式，步骤事件齐全）。合成最小
  event 的可行性未证实（本插件不经过消息管道），列为 probe 探测项；
- 人格 ``tools`` 语义（拍板）：``None``=全部工具、``[]``=禁用、列表=白名单，
  经 persona_service.resolve_persona_tools 过滤 ToolSet；
- 事件契约：``tool_call {phase:"start", id, name, args}``（执行中前端转圈）→
  ``tool_result {id, result|error, elapsed_ms}``（result 与 error 互斥）；
  富历史工具步骤 ``{id, name, args, result?|error?, elapsed_ms}``；
- 工具事件经 chat_service 注入 ``entry["append_event"]`` 落缓冲（广播一致）。

⚠ 内部 API 未在真机核实（ContextWrapper/AstrAgentContext 的模块路径与构造参数、
ToolCallsResult 字段名、tool_loop_agent 合成 event/agent_hooks 签名）——全部经
try/except 防御并给出 ``ToolLoopUnavailable``，由 chat_service 降级（A→B→无
工具单轮 + 非致命 SSE error 事件）；``tests/probe_toolloop.py`` 在真机裁决后
可收紧实现（probe 脚本不进插件 zip）。
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable

from astrbot.api import logger

from . import persona_service

TOOL_LOOP_MODE = "stream"  # 模块常量：stream=方案 A（默认）/ loop_agent=方案 B
MAX_TOOL_ROUNDS = 8  # 单次生成的最大工具轮数（防失控循环）


class ToolLoopUnavailable(Exception):
    """工具循环基础设施不可用（内部 API 缺失/构造失败），由 chat_service 降级。"""


# ---------------------------------------------------------------------------
# 内部 API 延迟导入（宿主缺失/桩环境无对应模块时返回 None，走降级）
# ---------------------------------------------------------------------------


def _import_toolset():
    try:
        from astrbot.core.agent.tool import ToolSet

        return ToolSet
    except Exception:  # noqa: BLE001
        return None


def _import_tool_calls_result():
    try:
        from astrbot.core.provider.entities import ToolCallsResult

        return ToolCallsResult
    except Exception:  # noqa: BLE001
        return None


def build_exec_context(plugin):
    """构造工具执行所需的 ContextWrapper（内部 API，probe P2 真机裁决）。

    尝试候选模块路径并按 references/04 的结构构造（外层 ContextWrapper 包
    AstrAgentContext，内含真正的 Context 与 event）；本插件无消息 event，传
    None（工具实现若强依赖 event 会在 call 时报错，走工具级 error 结果）。
    失败返回 None（视为方案 A 不可用）。
    """
    for module_path in ("astrbot.core.agent.context", "astrbot.core.agent"):
        try:
            module = __import__(module_path, fromlist=("AstrAgentContext", "ContextWrapper"))
            astr_agent_context_cls = getattr(module, "AstrAgentContext", None)
            context_wrapper_cls = getattr(module, "ContextWrapper", None)
            if astr_agent_context_cls is None or context_wrapper_cls is None:
                continue
            agent_ctx = astr_agent_context_cls(context=plugin.context, event=None)
            return context_wrapper_cls(agent_ctx)
        except Exception as exc:  # noqa: BLE001 —— 路径/构造参数不确定，逐候选尝试
            logger.debug(f"[webui] 工具执行上下文构造失败（候选 {module_path}）: {exc}")
    return None


# ---------------------------------------------------------------------------
# ToolSet 构建（人格 tools 语义过滤）
# ---------------------------------------------------------------------------


def build_toolset(plugin, persona: dict | None) -> tuple[object | None, dict[str, object]]:
    """按人格 tools 语义从宿主工具管理器过滤出 ToolSet。

    Returns:
        ``(toolset, name->tool对象)``；该人格无可用工具时 ``(None, {})``
        （禁用/白名单空/宿主工具管理器缺失/ToolSet 不可导入）。
    """
    allow, whitelist = persona_service.resolve_persona_tools((persona or {}).get("tools"))
    if not allow:
        return None, {}
    try:
        manager = plugin.context.get_llm_tool_manager()
        func_list = list(getattr(manager, "func_list", None) or [])
    except Exception:  # noqa: BLE001
        func_list = []
    if whitelist is not None:
        func_list = [t for t in func_list if getattr(t, "name", None) in whitelist]
    tool_objs = {str(getattr(t, "name", "")): t for t in func_list if getattr(t, "name", None)}
    if not tool_objs:
        return None, {}
    toolset_cls = _import_toolset()
    if toolset_cls is None:
        return None, {}
    try:
        return toolset_cls(list(tool_objs.values())), tool_objs
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[webui] ToolSet 构造失败（工具循环停用）: {exc}")
        return None, {}


# ---------------------------------------------------------------------------
# 工具执行（probe P2 裁决项）
# ---------------------------------------------------------------------------


def _extract_result_text(result) -> str:
    """ToolExecResult 兜底提取：str 直用，容器取 content/text 属性，否则 str()。"""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    for attr in ("content", "text", "message", "result"):
        value = getattr(result, attr, None)
        if isinstance(value, str):
            return value
    return str(result)


async def execute_tool(tool_obj, exec_context, name: str, args: dict) -> tuple[str | None, str | None]:
    """执行单个工具（references/04 文档接口 ``tool.call(context, **kwargs)``）。

    Returns:
        ``(result_text, error_text)``，互斥其一非空；接口缺失/执行异常均转为
        error 文本（不抛出），保证循环继续、模型能看到失败原因。
    """
    call = getattr(tool_obj, "call", None)
    if exec_context is None or not callable(call):
        return None, "工具执行接口不可用（内部 API 构造失败，probe P2 待真机裁决）"
    try:
        result = call(exec_context, **args)
        if asyncio.iscoroutine(result):
            result = await result
        return _extract_result_text(result), None
    except Exception as exc:  # noqa: BLE001 —— 工具级失败不终止循环
        logger.warning(f"[webui] 工具执行失败 name={name}: {type(exc).__name__}: {exc}")
        return None, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# LLMResponse 工具调用字段解析 / ToolCallsResult 回填（probe P1/P3 裁决项）
# ---------------------------------------------------------------------------


def extract_tool_calls(final_resp) -> list[dict]:
    """从末次完整响应解析工具调用列表 ``[{id, name, args(dict)}]``（可空）。"""
    if final_resp is None:
        return []
    names = list(getattr(final_resp, "tools_call_name", None) or [])
    ids = list(getattr(final_resp, "tools_call_ids", None) or [])
    raw_args = list(getattr(final_resp, "tools_call_args", None) or [])
    calls: list[dict] = []
    for index, name in enumerate(names):
        raw = raw_args[index] if index < len(raw_args) else {}
        calls.append(
            {
                "id": str(ids[index]) if index < len(ids) else f"call_{index}",
                "name": str(name),
                "args": _coerce_args(raw),
            }
        )
    return calls


def _coerce_args(raw) -> dict:
    """tools_call_args 条目归一为 dict（dict 直用；str 尝试 JSON 解析）。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"raw": raw}
        except json.JSONDecodeError:
            return {"raw": raw}
    return {}


def build_tool_calls_result(ids: list[str], names: list[str], args_strs: list[str], results: list[str], completion_text: str = ""):
    """构造回填下一轮流式请求的 ToolCallsResult（字段名未真机核实，按序尝试）。

    Raises:
        ToolLoopUnavailable: 类不可导入或全部候选字段名构造失败（probe P3）。
    """
    cls = _import_tool_calls_result()
    if cls is None:
        raise ToolLoopUnavailable("ToolCallsResult 不可导入（宿主内部 API 变动）")
    # 候选字段名（probe P3 裁决后可收敛为主候选）
    candidates = (
        {"ids": "tool_calls_id", "names": "func_name", "args": "func_args", "results": "func_result"},
        {"ids": "tool_calls_id", "names": "function_name", "args": "function_args", "results": "function_result"},
        {"ids": "id", "names": "name", "args": "args", "results": "result"},
    )
    last_error: Exception | None = None
    for fields in candidates:
        kwargs = {
            fields["ids"]: ids,
            fields["names"]: names,
            fields["args"]: args_strs,
            fields["results"]: results,
            "completion_text": completion_text,
        }
        try:
            return cls(**kwargs)
        except TypeError as exc:
            last_error = exc
            continue
    raise ToolLoopUnavailable(f"ToolCallsResult 字段名不匹配（probe P3 裁决）: {last_error}")


# ---------------------------------------------------------------------------
# 方案 A：自实现流式工具循环
# ---------------------------------------------------------------------------


async def run_stream_loop(
    plugin,
    entry: dict,
    *,
    consume: Callable[..., Awaitable[object]],
    toolset: object,
    tool_objs: dict[str, object],
    max_rounds: int = MAX_TOOL_ROUNDS,
) -> tuple[object | None, list[dict]]:
    """方案 A：流式工具循环（工具执行上下文不可用即抛 ToolLoopUnavailable）。

    Args:
        plugin: 插件实例（构造工具执行上下文用）。
        entry: chat_service 任务条目（须含 ``append_event`` 落事件通道）。
        consume: chat_service 注入的单轮消费闭包 ``consume(func_tool=...,
            tool_calls_result=...) -> 末次完整响应``（text_chat_stream 唯一
            触碰点在 chat_service）。
        toolset: 过滤后的 ToolSet（build_toolset 产出）。
        tool_objs: name -> 工具对象映射。
        max_rounds: 最大工具轮数。

    Returns:
        ``(末次完整响应, 工具步骤列表)``；工具步骤即契约富历史 ``tools`` 项。
    """
    exec_context = build_exec_context(plugin)
    if exec_context is None:
        raise ToolLoopUnavailable("工具执行上下文构造失败（内部 API 路径/参数未证实，probe P2）")

    tool_steps: list[dict] = []
    tool_calls_result = None
    final_resp = None
    rounds = max(1, max_rounds)
    for round_index in range(rounds):
        final_resp = await consume(func_tool=toolset, tool_calls_result=tool_calls_result)
        calls = extract_tool_calls(final_resp)
        if not calls or round_index == rounds - 1:
            if calls and round_index == rounds - 1:
                logger.warning(f"[webui] 工具轮数达上限 {rounds}，返回当前响应 gen_id={entry.get('gen_id')}")
            return final_resp, tool_steps

        args_strs: list[str] = []
        results: list[str] = []
        ids: list[str] = []
        names: list[str] = []
        for call in calls:
            started = time.monotonic()
            append = _append_event_fn(entry)
            append("tool_call", {"phase": "start", "id": call["id"], "name": call["name"], "args": call["args"]})
            result_text, error_text = await execute_tool(
                tool_objs.get(call["name"]), exec_context, call["name"], call["args"]
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            payload: dict = {"id": call["id"], "elapsed_ms": elapsed_ms}
            if error_text is not None:
                payload["error"] = error_text
            else:
                payload["result"] = result_text or ""
            append("tool_result", payload)

            step: dict = {"id": call["id"], "name": call["name"], "args": call["args"], "elapsed_ms": elapsed_ms}
            if error_text is not None:
                step["error"] = error_text
            else:
                step["result"] = result_text or ""
            tool_steps.append(step)

            ids.append(call["id"])
            names.append(call["name"])
            args_strs.append(json.dumps(call["args"], ensure_ascii=False))
            results.append(payload.get("error") or payload.get("result", ""))

        tool_calls_result = build_tool_calls_result(ids, names, args_strs, results)
    return final_resp, tool_steps


def _append_event_fn(entry: dict) -> Callable[[str, dict], dict]:
    """取 chat_service 注入的事件落缓冲通道（缺失即视为方案 A 不可用）。"""
    append = entry.get("append_event")
    if append is None:
        raise ToolLoopUnavailable("事件通道缺失（entry.append_event 未注入）")
    return append


# ---------------------------------------------------------------------------
# 方案 B：宿主 tool_loop_agent（正文非流式，步骤事件齐全；probe P5 裁决项）
# ---------------------------------------------------------------------------


def _synthetic_event(umo: str):
    """合成最小 event（tool_loop_agent 首参必填 event；可行性 probe P5 未证实）。

    仅提供宿主 agent 路径常用的 duck-typed 成员；缺成员时由上层 try/except
    捕获并降级。
    """
    from types import SimpleNamespace

    return SimpleNamespace(
        unified_msg_origin=umo,
        get_sender_id=lambda: umo,
        get_sender_name=lambda: "webui",
        get_platform_name=lambda: "webui",
        stop_event=lambda: None,
        is_stopped=lambda: False,
        get_extra=lambda key, default=None: default,
        set_extra=lambda key, value: None,
    )


def _make_agent_hooks(entry: dict) -> dict:
    """agent_hooks：工具开始/返回时发 tool_call/tool_result 事件（签名未证实）。"""

    async def on_using_llm_tool(*args, **kwargs):
        tool = args[2] if len(args) >= 3 else kwargs.get("tool")
        tool_args = args[3] if len(args) >= 4 else kwargs.get("tool_args")
        _append_event_fn(entry)(
            "tool_call",
            {
                "phase": "start",
                "id": kwargs.get("id") or f"call_{len(entry.get('tool_steps') or [])}",
                "name": str(tool),
                "args": _coerce_args(tool_args),
            },
        )

    async def on_llm_tool_respond(*args, **kwargs):
        tool = args[2] if len(args) >= 3 else kwargs.get("tool")
        tool_args = args[3] if len(args) >= 4 else kwargs.get("tool_args")
        tool_result = args[4] if len(args) >= 5 else kwargs.get("tool_result")
        elapsed_ms = int(kwargs.get("elapsed_ms") or 0)
        step: dict = {
            "id": kwargs.get("id") or f"call_{len(entry.get('tool_steps') or [])}",
            "name": str(tool),
            "args": _coerce_args(tool_args),
            "elapsed_ms": elapsed_ms,
        }
        payload: dict = {"id": step["id"], "elapsed_ms": elapsed_ms}
        if isinstance(tool_result, Exception):
            step["error"] = f"{type(tool_result).__name__}: {tool_result}"
            payload["error"] = step["error"]
        else:
            step["result"] = _extract_result_text(tool_result)
            payload["result"] = step["result"]
        _append_event_fn(entry)("tool_result", payload)
        entry.setdefault("tool_steps", []).append(step)

    return {"on_using_llm_tool": on_using_llm_tool, "on_llm_tool_respond": on_llm_tool_respond}


async def run_agent_loop(
    plugin,
    entry: dict,
    *,
    umo: str,
    prompt: str,
    system_prompt: str | None,
    contexts: list[dict],
    image_urls: list[str] | None,
    toolset: object,
) -> tuple[object | None, list[dict]]:
    """方案 B：宿主 ``tool_loop_agent``（合成 event 未证实，失败抛 ToolLoopUnavailable）。

    Note:
        image_urls 在方案 B 下透传给 tool_loop_agent 不确定是否支持（其签名含
        image_urls，references/04 v4.29.0+ 记载），缺失即走文本路径。
    """
    context = plugin.context
    try:
        provider_id = await context.get_current_chat_provider_id(umo=umo)
    except Exception as exc:  # noqa: BLE001
        raise ToolLoopUnavailable(f"获取当前 provider 失败: {exc}") from exc
    if not provider_id:
        raise ToolLoopUnavailable("未配置可用的聊天模型")

    kwargs: dict = {
        "event": _synthetic_event(umo),
        "chat_provider_id": provider_id,
        "prompt": prompt or None,
        "system_prompt": system_prompt,
        "contexts": contexts or None,
        "tools": toolset,
    }
    if image_urls:
        kwargs["image_urls"] = list(image_urls)
    entry["tool_steps"] = []
    try:
        try:
            resp = await context.tool_loop_agent(**kwargs, agent_hooks=_make_agent_hooks(entry))
        except TypeError:
            # agent_hooks 参数签名不符（probe P5），退化为无钩子逐步执行
            resp = await context.tool_loop_agent(**kwargs)
        return resp, list(entry.get("tool_steps") or [])
    except Exception as exc:  # noqa: BLE001
        raise ToolLoopUnavailable(f"tool_loop_agent 执行失败: {type(exc).__name__}: {exc}") from exc
