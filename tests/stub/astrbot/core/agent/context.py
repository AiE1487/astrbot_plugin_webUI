"""stub ``astrbot.core.agent.context``：ContextWrapper / AstrAgentContext 最小桩。

形状按 references/04 的描述：外层 ContextWrapper 包 AstrAgentContext，
``wrapper.context`` 是 AstrAgentContext，再 ``.context`` 才是真正的宿主
Context，``.event`` 为消息事件（本插件无 event，传 None）。
真实宿主的模块路径与构造参数由 tests/probe_toolloop.py（probe P2）真机裁决。
"""


class AstrAgentContext:
    """stub：Agent 运行上下文（构造参数按 references/04 的成员命名）。"""

    def __init__(self, context=None, event=None, **kwargs) -> None:
        self.context = context
        self.event = event
        for key, value in kwargs.items():
            setattr(self, key, value)


class ContextWrapper:
    """stub：工具执行入口的第一个参数（``tool.call(wrapper, **kwargs)``）。"""

    def __init__(self, context) -> None:
        self.context = context
