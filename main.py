"""astrbot_plugin_webui —— 酒馆风对话 WebUI 插件入口。

约定：
- 继承 ``Star`` 即被宿主自动注册，不使用已废弃的 ``@register`` 装饰器；
- ``config`` 由 ``_conf_schema.json`` 生成，作为第二个构造参数注入（本质是 dict）。
  配置默认值**只在 schema 维护一份**：AstrBotConfig 由 schema 生成、键恒存在，
  代码直接按键读取（``self.config["键"]``，不做第二重默认值兜底，references/03
  开发规范），读取处实时取值即热生效；``standalone_enabled`` / ``standalone_port``
  / ``standalone_password`` 改动需重载插件生效（见 _conf_schema.json description）；
- 持久化数据一律写 ``data/plugin_data/astrbot_plugin_webui/``（get_astrbot_data_path
  计算），禁止写插件目录 —— 插件更新/重装会丢；
- 宿主以 ``data.plugins.<目录名>.main`` 加载本文件（star_manager.py），因此插件内部
  使用相对导入（``from .core.routes import ...``）；
- 业务逻辑全部在 ``core/``，本文件只做生命周期与转发（references/09 职责分离）。
"""

from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star

from .core import attachment_service, chat_service
from .core.routes import PLUGIN_NAME, register_routes
from .core.standalone import start as standalone_start, stop as standalone_stop


class WebUIPlugin(Star):
    """酒馆风对话 WebUI：Dashboard 内嵌视图 + 独立端口页双入口的单用户共享会话池。

    功能范围：人格选择、流式回复、思考与工具调用展示、
    附件对话、会话管理；UI 内不提供模型参数调节入口。
    """

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._standalone_runner = None

    def _register_web_api(self) -> None:
        """把 core/routes.py 的 REST/SSE 路由表注册到 Dashboard。

        实际注册逻辑在 ``core.routes.register_routes(plugin)``（内嵌与独立端口
        共用同一套 service）。
        """
        register_routes(self)

    @staticmethod
    def _log_host_capability() -> None:
        """记录宿主版本与内嵌视图能力（版本低于 4.29 无 Plugin Views，内嵌入口不存在）。"""
        try:
            from astrbot import __version__ as host_version
        except Exception:  # noqa: BLE001 —— 版本号拿不到不影响功能
            host_version = ""
        try:
            parts = tuple(
                int(p) for p in str(host_version).split(".")[:2] if p.isdigit()
            )
        except ValueError:  # noqa: PERF203 —— 防御非数字版本号
            parts = ()
        if len(parts) == 2 and parts < (4, 29):
            logger.warning(
                f"[webui] 宿主版本 {host_version} 低于 4.29，不支持插件视图"
                "（Plugin Views）——Dashboard 内嵌入口不可用；独立端口入口"
                "（standalone_enabled）不受影响"
            )
        elif len(parts) == 2:
            logger.info(f"[webui] 宿主版本 {host_version}，插件视图可用")
        else:
            logger.info(
                f"[webui] 宿主版本 {host_version or '未知'} 无法解析，"
                "内嵌视图是否可用以宿主实际能力为准（需 >= 4.29）"
            )

    def data_dir(self) -> Path:
        """插件持久化数据目录：``data/plugin_data/astrbot_plugin_webui/``。

        uistore 富历史 / M3 附件都落在这里；目录不存在时创建。
        """
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        path = Path(get_astrbot_data_path()) / "plugin_data" / (
            getattr(self, "name", "") or PLUGIN_NAME
        )
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def initialize(self) -> None:
        """插件被激活时调用：注册路由 + 孤儿附件清理后台任务 + 按配置启动独立端口。

        启动即记录宿主版本与两个入口的可用状态（内嵌视图需宿主 >= 4.29 的
        Plugin Views 能力；独立端口按配置）——保证「打不开 WebUI」时日志里
        一定有可对照的诊断信息。
        """
        self._log_host_capability()
        self._register_web_api()
        try:
            await attachment_service.start_orphan_cleanup(self)
        except Exception:
            logger.exception("[webui] 孤儿附件清理任务启动失败（不影响其他功能）")
        if bool(self.config["standalone_enabled"]):
            try:
                static_root = Path(__file__).resolve().parent / "views" / "chat"
                port = int(self.config["standalone_port"])
                self._standalone_runner = await standalone_start(
                    self,
                    static_root,
                    host="0.0.0.0",
                    port=port,
                )
                has_password = bool(self.config["standalone_password"])
                scope = "已设密码" if has_password else "未设密码，仅本机 127.0.0.1 可用"
                logger.info(
                    f"[webui] 独立端口已启动: http://127.0.0.1:{port}/（{scope}；"
                    "局域网访问需设密码）"
                )
            except (NotImplementedError, ImportError, RuntimeError) as exc:
                logger.warning(f"[webui] 独立端口未启动（不影响内嵌入口）: {exc}")
            except Exception:
                logger.exception("[webui] 独立端口启动失败（不影响内嵌入口）")
        else:
            logger.info(
                "[webui] 独立端口未启用（standalone_enabled=false），"
                "打开 http://<host>:6186 会连接被拒绝；请改用 Dashboard 内嵌视图"
                "（WebUI → 插件管理 → 本插件 → 打开「对话」视图），"
                "或开启配置后重载插件"
            )

    async def terminate(self) -> None:
        """插件被停用/重载/卸载时调用：清理孤儿清理任务、全部生成任务与自持资源。"""
        try:
            await attachment_service.stop_orphan_cleanup()
        except Exception:
            logger.exception("[webui] 孤儿清理任务停止失败")
        try:
            await chat_service.shutdown()
        except Exception:
            logger.exception("[webui] 生成任务清理失败")
        if self._standalone_runner is not None:
            try:
                await standalone_stop(self._standalone_runner)
            except Exception:
                logger.exception("[webui] 独立端口停止失败")
            finally:
                self._standalone_runner = None
