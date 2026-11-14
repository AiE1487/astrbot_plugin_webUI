"""stub ``astrbot.core.provider.entities``：ToolCallsResult 最小桩（dataclass）。

字段名取 tool_loop.py 主候选（tool_calls_id/func_name/func_args/func_result/
completion_text）；真实宿主字段名由 tests/probe_toolloop.py（probe P3）真机裁决。
"""

import dataclasses


@dataclasses.dataclass
class ToolCallsResult:
    """stub：回填下一轮流式请求的工具调用结果集（主候选字段名）。"""

    tool_calls_id: list[str] = dataclasses.field(default_factory=list)
    func_name: list[str] = dataclasses.field(default_factory=list)
    func_args: list[str] = dataclasses.field(default_factory=list)
    func_result: list[str] = dataclasses.field(default_factory=list)
    completion_text: str = ""
