"""stub ``astrbot.core.utils.active_event_registry``：优雅中止注册表最小桩。

形状按宿主 astrbot/core/utils/active_event_registry.py:151-192（模块级单例
``active_event_registry``）；桩仅实现 ``request_agent_stop_all`` 的调用记录
（无真实事件循环交互）。
"""


class ActiveEventRegistry:
    """stub：活跃事件注册表（request_agent_stop_all 调用记录）。"""

    def __init__(self) -> None:
        self.stop_requests: list[str] = []

    def request_agent_stop_all(self, umo: str, exclude=None) -> int:
        """记录停止请求并返回 0（桩下无活跃事件）。"""
        self.stop_requests.append(umo)
        return 0


active_event_registry = ActiveEventRegistry()
