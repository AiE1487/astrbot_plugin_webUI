"""stub ``astrbot.api.star``：Context / Star 最小桩。"""


class Context:
    """stub：宿主上下文占位（selftest 仅验证可导入与可实例化）。"""


class Star:
    """stub：插件基类占位。"""

    def __init__(self, context: Context | None = None) -> None:
        self.context = context
