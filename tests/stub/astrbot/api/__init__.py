"""stub ``astrbot.api``：提供 logger 与 AstrBotConfig 最小桩。"""

import logging

# 宿主真实 logger 是上下文感知代理；桩用普通 logger 即可（core/ 骨架不产生日志输出）。
logger = logging.getLogger("astrbot.stub")


class AstrBotConfig(dict):
    """stub：继承 dict 的配置容器（与宿主同名同基类，满足 isinstance 语义）。"""
