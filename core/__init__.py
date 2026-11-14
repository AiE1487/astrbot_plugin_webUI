"""astrbot_plugin_webui 核心业务逻辑包（v0.2：全托管宿主管道架构）。

模块划分见 UserPlan/开发计划.md §0.2/§0.3：

- routes.py               REST/SSE 路由表与薄 handler（内嵌与独立端口共用）
- pipeline_bridge.py      宿主对接唯一层：webchat 注入 + back_queue 排水翻译 + 级联删除
- chat_service.py         生成任务表/gen_id/single-flight/SSE 缓冲/stop 终态语义
- conversation_service.py cid/conv_id 双主键、会话 CRUD 映射宿主、历史读取映射
- persona_service.py      人格列表展示（人格解析/工具注入已归还宿主管道）
- attachment_service.py   附件上传（宿主附件区 data/attachments/ + db 注册）、索引、孤儿清理
- uistore.py              v0.1.x 旧会话只读归档层（读/分页/删档）
- sse.py                  SSE 事件封装与缓冲（纯逻辑，桩测友好）
- standalone.py           aiohttp 独立端口（静态托管 + API 转发 + 密码鉴权）

架构约定（v0.2）：
- 生成内核 = 「webchat 事件注入者 + back_queue 消费者」：会话管理、请求拼接
  （人格/历史）、工具注入（全宿主工具面）全部归还宿主管道；本包不直调 Provider；
- 业务逻辑全部收敛在本包，main.py 只做转发 —— 纯逻辑模块可在没有宿主的环境下
  经 stub 导入测试（skill references/09 桩宿主自测法）；
- 插件持久化写 ``data/plugin_data/astrbot_plugin_webui/``；附件落宿主附件区
  ``data/attachments/``（经 db 注册），均不写插件目录。
"""
