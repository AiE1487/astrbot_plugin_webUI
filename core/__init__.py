"""astrbot_plugin_webui 核心业务逻辑包（M1 骨架）。

模块划分见 UserPlan/开发计划.md §4：

- routes.py               REST/SSE 路由表与薄 handler（内嵌与独立端口共用）
- persona_service.py      人格列表、system_prompt 摘要、工具白名单解析
- conversation_service.py 独立命名空间 umo、会话 CRUD、权威历史组装（双写）
- chat_service.py         流式生成、任务表、abort、事件缓冲、工具循环（M2/M3）
- attachment_service.py   上传保存、类型白名单、文本抽取、大小限制（M3）
- uistore.py              UI 富历史 JSON 读写（原子写 + 迁移预留）
- sse.py                  SSE 事件封装与缓冲（纯逻辑，桩测友好）
- standalone.py           aiohttp 独立端口（静态托管 + API 转发 + 密码鉴权）

骨架约定（M1）：
- 所有公开函数/方法体当前为 ``raise NotImplementedError`` 桩，
  签名与 docstring 即 M2/M3 的实现契约（tests/selftest.py 会校验该契约）；
- 业务逻辑全部收敛在本包，main.py 只做转发 —— core/ 的纯逻辑模块可以在
  没有宿主的环境下经 stub 导入测试（skill references/09 桩宿主自测法）；
- 持久化一律写 ``data/plugin_data/astrbot_plugin_webui/``，不要写插件目录。
"""
