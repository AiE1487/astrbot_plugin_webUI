"""stub ``astrbot.core.utils.astrbot_path``：宿主数据路径桩。

宿主实现返回宿主进程的 ``data/`` 目录；桩用模块级 ``_DATA_PATH``（测试用
``set_stub_data_path`` 注入沙盒目录，插件代码 ``get_astrbot_data_path()``
语义不变：附件落点 = ``<data>/attachments/``）。
"""

from pathlib import Path

_DATA_PATH = Path.cwd() / "data"


def get_astrbot_data_path() -> str:
    """返回桩宿主数据目录（字符串，与宿主签名一致）。"""
    return str(_DATA_PATH)


def set_stub_data_path(path) -> None:
    """测试注入桩数据目录（不产生任何宿主行为差异）。"""
    global _DATA_PATH
    _DATA_PATH = Path(path)
