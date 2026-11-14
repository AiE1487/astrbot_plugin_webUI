"""最小桩宿主 ``astrbot`` 包标记（仅用于 tests/selftest.py，不随插件逻辑运行）。

做法遵循 astrbot-plugin-dev skill references/09 的「stub 宿主自测法」：
在 sys.path 前面放一套最小的假 ``astrbot`` 包，只提供 core/ 实际 import 到的
名字；main.py 依赖真实宿主运行时注入，桩下不可导入，因此只测 core/。
"""
