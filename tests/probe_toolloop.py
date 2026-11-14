#!/usr/bin/env python3
"""M3 工具循环真机探测（probe 裁决脚本；**不进插件 zip**，打包时排除本文件）。

在真实 AstrBot 宿主的 Python 环境（>=3.12，``import astrbot`` 可用）运行::

    <AstrBot环境python> tests/probe_toolloop.py

P1/P4/P5 需要已配置可用的聊天模型；P2 需要至少一个已注册 LLM 工具（如任意
带 @filter.llm_tool 的插件）。脚本只读不改任何宿主状态（P1/P4/P5 发起的
LLM 调用为只读对话）。输出各项 PASS/FAIL/UNKNOWN 结论，用于裁决
``core/tool_loop.py`` 的 TOOL_LOOP_MODE（stream=方案 A / loop_agent=方案 B）
与 docstring 中标注的待证实项。

探测项（与 core/tool_loop.py 内 try/except 防御一一对应）：
- P0 静态发现：内部 API 模块路径与字段名（ContextWrapper/AstrAgentContext、
  ToolCallsResult、UploadTooLargeError、tool manager func_list）；
- P1 方案 A 前提：text_chat_stream(func_tool=ToolSet) 末次响应是否携带
  tools_call_name/args/ids；
- P2 方案 A 执行：FunctionTool.call(ContextWrapper, **kwargs) 能否跑通
  （本插件无消息 event，传 None）；
- P3 方案 A 回填：ToolCallsResult 真实字段名（tool_loop.py 按候选序尝试）；
- P4 文本附件注入：text_chat_stream 是否透传 extra_user_content_parts
  （**kwargs 恒可传，验证点在 provider 是否真正消费）；
- P5 方案 B：tool_loop_agent 合成最小 event / agent_hooks 签名可行性。
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import inspect
import json
import sys
import traceback

VERDICT: dict[str, str] = {}


def report(key: str, verdict: str, detail: str = "") -> None:
    VERDICT[key] = verdict
    print(f"[{verdict}] {key}" + (f" —— {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# P0 静态发现
# ---------------------------------------------------------------------------


def probe_p0() -> None:
    section("P0 静态发现（内部 API 路径与字段名）")
    for module_path, names in (
        ("astrbot.core.agent.tool", ("ToolSet", "FunctionTool")),
        ("astrbot.core.agent.context", ("ContextWrapper", "AstrAgentContext")),
        ("astrbot.core.agent", ("ContextWrapper", "AstrAgentContext")),
        ("astrbot.core.provider.entities", ("ToolCallsResult", "LLMResponse")),
        ("astrbot.core.utils.upload", ("UploadTooLargeError", "save_upload_stream")),
    ):
        try:
            module = importlib.import_module(module_path)
        except Exception as exc:  # noqa: BLE001
            report(f"P0.{module_path}", "FAIL", f"导入失败: {exc}")
            continue
        missing = [n for n in names if not hasattr(module, n)]
        report(
            f"P0.{module_path}",
            "PASS" if not missing else "PARTIAL",
            "全部存在" if not missing else f"缺失 {missing}",
        )

    try:
        from astrbot.core.provider.entities import ToolCallsResult

        if dataclasses.is_dataclass(ToolCallsResult):
            fields = [f.name for f in dataclasses.fields(ToolCallsResult)]
            report("P3.ToolCallsResult字段", "PASS", f"字段名 = {fields}")
            print("    -> 请把该字段名回填 core/tool_loop.py _TOOL_CALLS_RESULT 候选首位")
        else:
            report("P3.ToolCallsResult字段", "UNKNOWN", "非 dataclass，打印 __init__ 签名")
            print(f"    {inspect.signature(ToolCallsResult.__init__)}")
    except Exception as exc:  # noqa: BLE001
        report("P3.ToolCallsResult字段", "FAIL", str(exc))

    try:
        from astrbot.core.agent.tool import FunctionTool

        print(f"    FunctionTool.call 签名: {inspect.signature(FunctionTool.call)}")
    except Exception as exc:  # noqa: BLE001
        report("P2.FunctionTool.call签名", "FAIL", str(exc))


# ---------------------------------------------------------------------------
# P1 text_chat_stream(func_tool=...) 是否回传工具调用（需已配置 provider）
# ---------------------------------------------------------------------------


class _EchoTool:
    """最小 FunctionTool 兼容对象（名称/描述/参数 schema + call 实现）。"""

    name = "probe_echo"
    description = "probe：原样返回输入文本"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "任意文本"}},
        "required": ["text"],
    }

    async def call(self, context, **kwargs):  # noqa: ANN001
        return f"echo:{kwargs.get('text', '')}"


def _get_provider(context):
    provider = context.get_using_provider()
    return provider


async def probe_p1() -> None:
    section("P1 方案 A 前提：text_chat_stream(func_tool=ToolSet) 回传工具调用")
    try:
        from astrbot.core.agent.tool import ToolSet
    except Exception as exc:  # noqa: BLE001
        report("P1", "FAIL", f"ToolSet 导入失败: {exc}")
        return
    try:
        from astrbot.core.star.context import Context as _Ctx  # noqa: F401

        from astrbot.api.star import Context  # noqa: F401
    except Exception:  # noqa: BLE001
        pass
    # 宿主内运行：通过已加载的本插件实例取 context 最稳；脚本独立运行则尝试全局
    context = _find_context()
    if context is None:
        report("P1", "UNKNOWN", "未找到宿主 Context（请在插件内或宿主进程环境运行）")
        return
    provider = _get_provider(context)
    if provider is None or not hasattr(provider, "text_chat_stream"):
        report("P1", "UNKNOWN", "未配置可用的聊天模型 provider")
        return
    try:
        agen = provider.text_chat_stream(
            prompt="请调用 probe_echo 工具，参数 text=hi",
            func_tool=ToolSet([_EchoTool()]),
        )
        final = None
        async for chunk in agen:
            if not bool(getattr(chunk, "is_chunk", False)):
                final = chunk
        names = list(getattr(final, "tools_call_name", None) or [])
        if names:
            report(
                "P1",
                "PASS",
                f"tools_call_name={names}, args={getattr(final, 'tools_call_args', None)}, "
                f"ids={getattr(final, 'tools_call_ids', None)}",
            )
        else:
            report("P1", "FAIL", "末次响应未携带 tools_call_*（方案 A 回传假设不成立）")
    except Exception as exc:  # noqa: BLE001
        report("P1", "FAIL", f"{type(exc).__name__}: {exc}")
        traceback.print_exc()


def _find_context():
    """从宿主进程内取 Context（优先已加载的本插件实例）。"""
    try:
        from astrbot.core.star.context import Context  # noqa: F401
    except Exception:  # noqa: BLE001
        pass
    try:
        manager = importlib.import_module("astrbot.core.star.star_manager")
        star_manager = getattr(manager, "star_manager", None) or getattr(manager, "StarManager", None)
        instance = getattr(star_manager, "instance", None) if star_manager else None
        if instance is not None and hasattr(instance, "stars_data"):
            for metadata, star in (getattr(instance, "stars_data", {}) or {}).items():
                if getattr(star, "name", "") == "astrbot_plugin_webui" or "webui" in str(metadata):
                    return getattr(star, "context", None)
    except Exception:  # noqa: BLE001
        pass
    return None


# ---------------------------------------------------------------------------
# P2 FunctionTool.call 执行（需已注册工具；无消息 event 场景验证）
# ---------------------------------------------------------------------------


async def probe_p2() -> None:
    section("P2 方案 A 执行：tool.call(ContextWrapper, **kwargs)")
    wrapper = None
    for module_path in ("astrbot.core.agent.context", "astrbot.core.agent"):
        try:
            module = importlib.import_module(module_path)
            astr_agent_context_cls = getattr(module, "AstrAgentContext", None)
            context_wrapper_cls = getattr(module, "ContextWrapper", None)
            if not (astr_agent_context_cls and context_wrapper_cls):
                continue
        except Exception:  # noqa: BLE001
            continue
        try:
            agent_ctx = astr_agent_context_cls(context=_find_context(), event=None)
            wrapper = context_wrapper_cls(agent_ctx)
            report("P2.ContextWrapper构造", "PASS", f"模块 {module_path}")
            break
        except Exception as exc:  # noqa: BLE001
            report("P2.ContextWrapper构造", "FAIL", f"{module_path}: {exc}")
    if wrapper is None:
        report("P2", "UNKNOWN", "无可用构造路径（打印 AstrAgentContext.__init__ 签名辅助裁决）")
        for module_path in ("astrbot.core.agent.context", "astrbot.core.agent"):
            try:
                module = importlib.import_module(module_path)
                cls = getattr(module, "AstrAgentContext", None)
                if cls is not None:
                    print(f"    {module_path}.AstrAgentContext.__init__ = {inspect.signature(cls.__init__)}")
            except Exception:  # noqa: BLE001
                continue
        return
    try:
        result = _EchoTool().call(wrapper, text="hi")
        if asyncio.iscoroutine(result):
            result = await result
        report("P2", "PASS", f"call 返回: {result!r}")
    except Exception as exc:  # noqa: BLE001
        report("P2", "FAIL", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# P4 extra_user_content_parts 透传（文本附件注入的落选方案探测）
# ---------------------------------------------------------------------------


async def probe_p4() -> None:
    section("P4 文本附件注入：text_chat_stream(**kwargs) 是否消费 extra_user_content_parts")
    try:
        from astrbot.core.agent.message import TextPart
    except Exception as exc:  # noqa: BLE001
        report("P4", "UNKNOWN", f"TextPart 导入失败（构造方式待核）: {exc}")
        return
    context = _find_context()
    provider = _get_provider(context) if context else None
    if provider is None or not hasattr(provider, "text_chat_stream"):
        report("P4", "UNKNOWN", "未配置可用的聊天模型 provider")
        return
    try:
        part = TextPart(text="<attachment name=\"probe.md\">PROBE_MARKER_CONTENT</attachment>")
        mark = getattr(part, "mark_as_temp", None)
        if callable(mark):
            part.mark_as_temp()
    except Exception as exc:  # noqa: BLE001
        report("P4", "FAIL", f"TextPart 构造失败: {exc}")
        return
    try:
        # 传参本身恒成功（**kwargs）；验证点：请求体是否真的带上了该内容块
        agen = provider.text_chat_stream(
            prompt="请只回复 OK",
            extra_user_content_parts=[part],
        )
        async for _chunk in agen:  # noqa: B007
            pass
        report(
            "P4",
            "UNKNOWN",
            "调用未报错；请结合本次请求日志/抓包确认请求体是否包含 "
            "PROBE_MARKER_CONTENT（确认后回填 core/tool_loop.py / chat_service 注入策略）",
        )
    except Exception as exc:  # noqa: BLE001
        report("P4", "FAIL", f"透传被拒: {type(exc).__name__}: {exc}（落选方案：拼 prompt 注入）")


# ---------------------------------------------------------------------------
# P5 方案 B tool_loop_agent（合成 event / agent_hooks 可行性）
# ---------------------------------------------------------------------------


async def probe_p5() -> None:
    section("P5 方案 B：tool_loop_agent(event=..., agent_hooks=...) 可行性")
    context = _find_context()
    if context is None:
        report("P5", "UNKNOWN", "未找到宿主 Context")
    else:
        try:
            print(f"    tool_loop_agent 签名: {inspect.signature(context.tool_loop_agent)}")
        except Exception as exc:  # noqa: BLE001
            report("P5.signature", "FAIL", str(exc))
    provider_id = None
    if context is not None:
        try:
            provider_id = await context.get_current_chat_provider_id(umo="webui:FriendMessage:webui!probe")
        except Exception as exc:  # noqa: BLE001
            report("P5.get_current_chat_provider_id", "FAIL", str(exc))
    if not provider_id:
        report("P5", "UNKNOWN", "未配置可用的聊天模型，跳过真调用")
        return

    from types import SimpleNamespace

    event = SimpleNamespace(
        unified_msg_origin="webui:FriendMessage:webui!probe",
        get_sender_id=lambda: "probe",
        stop_event=lambda: None,
        is_stopped=lambda: False,
        get_extra=lambda key, default=None: default,
        set_extra=lambda key, value: None,
    )
    hooks_fired: list[str] = []

    async def on_using_llm_tool(*args, **kwargs):
        hooks_fired.append("on_using_llm_tool")

    async def on_llm_tool_respond(*args, **kwargs):
        hooks_fired.append("on_llm_tool_respond")

    try:
        resp = await context.tool_loop_agent(
            event=event,
            chat_provider_id=provider_id,
            prompt="请调用 probe_echo 工具，参数 text=hi",
            tools=None,  # None=全部已注册工具（含其他插件）；若失败再传 ToolSet([_EchoTool()])
            agent_hooks={"on_using_llm_tool": on_using_llm_tool, "on_llm_tool_respond": on_llm_tool_respond},
        )
        report(
            "P5",
            "PASS",
            f"合成 event + agent_hooks 可用；completion={getattr(resp, 'completion_text', None)!r}; "
            f"hooks={hooks_fired}",
        )
    except TypeError as exc:
        try:
            resp = await context.tool_loop_agent(
                event=event,
                chat_provider_id=provider_id,
                prompt="请只回复 OK",
            )
            report("P5", "PARTIAL", f"agent_hooks 签名不符（{exc}），无钩子调用可用 -> 需改用逐步 on_llm_response 钩子")
        except Exception as exc2:  # noqa: BLE001
            report("P5", "FAIL", f"合成 event 不可用: {type(exc2).__name__}: {exc2}")
    except Exception as exc:  # noqa: BLE001
        report("P5", "FAIL", f"{type(exc).__name__}: {exc}")


def main() -> int:
    print("astrbot_plugin_webui M3 probe（裁决 TOOL_LOOP_MODE；不进 zip）")
    probe_p0()
    asyncio.run(probe_p1())
    asyncio.run(probe_p2())
    asyncio.run(probe_p4())
    asyncio.run(probe_p5())

    print("\n=== 裁决建议 ===")
    p1 = VERDICT.get("P1", "UNKNOWN")
    p2 = VERDICT.get("P2", "UNKNOWN")
    p3 = VERDICT.get("P3.ToolCallsResult字段", "UNKNOWN")
    p5 = VERDICT.get("P5", "UNKNOWN")
    if p1 == "PASS" and p2 in ("PASS", "UNKNOWN") and p3 == "PASS":
        print("-> 建议 TOOL_LOOP_MODE=stream（方案 A 前提成立；P2 UNKNOWN 时依赖工具级 error 降级）")
    elif p5 == "PASS":
        print("-> 建议 TOOL_LOOP_MODE=loop_agent（方案 B 可用，正文非流式）")
    else:
        print("-> 两方案前提均未证实：保持 stream 默认（运行期自动降级无工具单轮），复核 P0-P5 输出")
    print(json.dumps(VERDICT, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
