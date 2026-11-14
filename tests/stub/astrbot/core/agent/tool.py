"""stub ``astrbot.core.agent.tool``：ToolSet 最小桩（构造参数为工具对象列表）。"""


class ToolSet:
    """stub：包装工具对象列表（references/04 用法 ``ToolSet([tool1, tool2])``）。"""

    def __init__(self, func_list: list | None = None) -> None:
        self.func_list = list(func_list or [])

    def __iter__(self):
        return iter(self.func_list)

    def __len__(self) -> int:
        return len(self.func_list)
